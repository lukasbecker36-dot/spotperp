"""Crash-safe intent journaling: write the intent BEFORE sending any live
order, resolve it once the outcome is known. recovery.py reconciles anything
left pending/ambiguous after a crash.
"""
from __future__ import annotations

import json
import sqlite3
import time
from decimal import Decimal


def _now_ms() -> int:
    return int(time.time() * 1000)


def record_intent(
    conn: sqlite3.Connection,
    position_id: int | None,
    venue: str,
    action: str,
    payload: dict,
) -> int:
    cur = conn.execute(
        "INSERT INTO intents (position_id, venue, action, payload, created_ms)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            position_id,
            venue,
            action,
            json.dumps(payload, default=str),
            _now_ms(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def resolve_intent(
    conn: sqlite3.Connection, intent_id: int, status: str, result: dict | str | None = None
) -> None:
    conn.execute(
        "UPDATE intents SET status=?, result=?, resolved_ms=? WHERE id=?",
        (
            status,
            json.dumps(result, default=str) if result is not None else None,
            _now_ms(),
            intent_id,
        ),
    )
    conn.commit()


def unresolved_intents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM intents WHERE status IN ('pending', 'ambiguous') ORDER BY id"
    ).fetchall()


def make_client_order_id(position_id: int, leg: str) -> str:
    """Deterministic client order id encoding the position and leg, so recovery
    can find/cancel orphan orders and the sweep can match by leg prefix
    (sp_pent_ entry maker, sp_pext_ exit maker, sp_stop_ protective stop).

    Aster allows ^[.A-Z:/a-z0-9_-]{1,36}$, MEXC similar; keep it short.
    """
    return f"sp_{leg}_{position_id}_{int(time.time())}"
