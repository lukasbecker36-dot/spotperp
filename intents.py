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


_LEG_PHASE = {"pent": "entry", "pext": "exit", "stop": "exit"}


def parse_client_order_id(client_id: str) -> tuple[int, str] | None:
    """(position_id, phase) encoded in one of our client ids, or None.

    Lets recovery attribute a fill back to its position when the in-memory
    task that placed the order is gone — after a restart the client id is the
    only link left between a venue order and the position it belongs to.
    """
    parts = (client_id or "").split("_")
    if len(parts) < 4 or parts[0] != "sp":
        return None
    phase = _LEG_PHASE.get(parts[1])
    if phase is None:
        return None
    try:
        return int(parts[2]), phase
    except ValueError:
        return None
