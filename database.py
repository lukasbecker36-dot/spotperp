"""SQLite persistence: positions, fills, intents, commands, journal.

Single-file DB shared by the engine and the control bot (WAL mode so both
processes can read/write concurrently). All writes are short transactions.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,               -- canonical symbol, e.g. BTCUSDT
    direction TEXT NOT NULL,            -- 'premium' (short perp / long spot)
    state TEXT NOT NULL,                -- PENDING_ENTRY/ENTERING/OPEN/EXITING/UNWINDING/CLOSED/CANCELLED
    paper INTEGER NOT NULL,
    target_notional TEXT NOT NULL,      -- requested USD per leg
    perp_qty TEXT NOT NULL DEFAULT '0', -- abs base qty short on Aster
    spot_qty TEXT NOT NULL DEFAULT '0', -- abs base qty held on MEXC
    entry_basis_bps TEXT,               -- realised entry basis (from fills)
    exit_mode TEXT,                     -- 'now' | 'passive' | NULL
    exit_target_bps TEXT,               -- passive exit target basis
    exit_target_qty TEXT,               -- perp qty to stop at (partial exit floor); NULL=full
    perp_entry_avg TEXT, spot_entry_avg TEXT,
    perp_exit_avg TEXT, spot_exit_avg TEXT,
    fees_usd TEXT NOT NULL DEFAULT '0',
    funding_usd TEXT NOT NULL DEFAULT '0',
    realized_pnl_usd TEXT,
    opened_ms INTEGER, closed_ms INTEGER,
    created_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    min_entry_bps TEXT,                 -- per-entry basis floor (stop chasing below)
    trade_kind TEXT NOT NULL DEFAULT 'convergence',  -- 'convergence' | 'carry'
    note TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    venue TEXT NOT NULL,                -- aster | mexc
    phase TEXT NOT NULL,                -- entry | exit | unwind
    side TEXT NOT NULL,                 -- BUY | SELL
    qty TEXT NOT NULL,
    price TEXT NOT NULL,
    fee_usd TEXT NOT NULL DEFAULT '0',
    order_id TEXT,
    ts_ms INTEGER NOT NULL
);

-- Crash-safe intent journal: written BEFORE any live order is sent, resolved
-- after the outcome is known. Unresolved intents are reconciled on startup.
CREATE TABLE IF NOT EXISTS intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER,
    venue TEXT NOT NULL,
    action TEXT NOT NULL,               -- place | cancel
    payload TEXT NOT NULL,              -- JSON: symbol/side/type/qty/price/client_order_id
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | ambiguous
    result TEXT,
    created_ms INTEGER NOT NULL,
    resolved_ms INTEGER
);

-- Control-bot -> engine command queue.
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,              -- enter | exit | cancel | flatten
    args TEXT NOT NULL,                 -- JSON
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error
    response TEXT,
    created_ms INTEGER NOT NULL,
    resolved_ms INTEGER
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_state ON positions(state);
CREATE INDEX IF NOT EXISTS idx_fills_position ON fills(position_id);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
"""


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_MIGRATIONS = [
    "ALTER TABLE positions ADD COLUMN min_entry_bps TEXT",
    "ALTER TABLE positions ADD COLUMN trade_kind TEXT NOT NULL DEFAULT 'convergence'",
    "ALTER TABLE positions ADD COLUMN exit_target_qty TEXT",
]


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    conn = get_connection(db_path)
    conn.executescript(_SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


def journal(conn: sqlite3.Connection, message: str, level: str = "INFO") -> None:
    conn.execute(
        "INSERT INTO journal (ts_ms, level, message) VALUES (?, ?, ?)",
        (int(time.time() * 1000), level, message),
    )
    conn.commit()


def enqueue_command(conn: sqlite3.Connection, command: str, args: dict) -> int:
    cur = conn.execute(
        "INSERT INTO commands (command, args, created_ms) VALUES (?, ?, ?)",
        (command, json.dumps(args), int(time.time() * 1000)),
    )
    conn.commit()
    return int(cur.lastrowid)


def resolve_command(
    conn: sqlite3.Connection, command_id: int, status: str, response: str
) -> None:
    conn.execute(
        "UPDATE commands SET status=?, response=?, resolved_ms=? WHERE id=?",
        (status, response, int(time.time() * 1000), command_id),
    )
    conn.commit()


def pending_commands(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM commands WHERE status='pending' ORDER BY id"
    ).fetchall()


def claim_command(conn: sqlite3.Connection, command_id: int) -> bool:
    """Atomically move a command pending -> running so a crash mid-execution
    can't replay it (pending_commands never returns 'running'). Returns True if
    this call won the claim."""
    cur = conn.execute(
        "UPDATE commands SET status='running' WHERE id=? AND status='pending'",
        (command_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def prune_old_rows(conn: sqlite3.Connection, *, journal_days: int = 14,
                   ledger_days: int = 7) -> None:
    """Delete resolved commands/intents and old journal lines so the DB (and
    its WAL/backups) doesn't grow without bound. Positions and fills are the
    permanent record and are never pruned."""
    now = int(time.time() * 1000)
    conn.execute(
        "DELETE FROM journal WHERE ts_ms < ?", (now - journal_days * 86_400_000,)
    )
    cutoff = now - ledger_days * 86_400_000
    conn.execute(
        "DELETE FROM commands WHERE status != 'pending' AND"
        " COALESCE(resolved_ms, created_ms) < ?", (cutoff,)
    )
    conn.execute(
        "DELETE FROM intents WHERE status != 'pending' AND"
        " COALESCE(resolved_ms, created_ms) < ?", (cutoff,)
    )
    conn.commit()


def abandon_running_commands(conn: sqlite3.Connection) -> int:
    """On startup, fail any command left 'running' by a crash so it neither
    replays nor blocks a waiting caller. Returns how many were abandoned."""
    cur = conn.execute(
        "UPDATE commands SET status='error', response='abandoned (engine restarted"
        " mid-command)', resolved_ms=? WHERE status='running'",
        (int(time.time() * 1000),),
    )
    conn.commit()
    return cur.rowcount
