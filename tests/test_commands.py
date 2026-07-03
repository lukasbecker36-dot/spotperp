"""Command queue crash-safety: TTL, claim-before-execute, orphan cleanup."""
import time

import config
import database
from database import (
    abandon_running_commands,
    claim_command,
    enqueue_command,
    pending_commands,
)


def test_claim_is_exclusive_and_removes_from_pending(tmp_path):
    conn = database.init_db(tmp_path / "t.db")
    cid = enqueue_command(conn, "enter", {"symbol": "X"})
    assert claim_command(conn, cid) is True
    assert claim_command(conn, cid) is False          # already running
    assert [r["id"] for r in pending_commands(conn)] == []
    conn.close()


def test_abandon_running_commands(tmp_path):
    conn = database.init_db(tmp_path / "t.db")
    cid = enqueue_command(conn, "enter", {})
    claim_command(conn, cid)
    assert abandon_running_commands(conn) == 1
    row = conn.execute(
        "SELECT status, response FROM commands WHERE id=?", (cid,)
    ).fetchone()
    assert row["status"] == "error" and "abandoned" in row["response"]
    conn.close()


async def test_drain_expires_stale_commands(tmp_path, monkeypatch):
    """A command older than the TTL is expired, not executed."""
    import live_monitor
    monkeypatch.setattr(config, "COMMAND_TTL_SECONDS", 60)
    conn = database.init_db(tmp_path / "t.db")
    cid = enqueue_command(conn, "flatten", {})
    # Back-date it well past the TTL.
    conn.execute(
        "UPDATE commands SET created_ms=? WHERE id=?",
        (int(time.time() * 1000) - 120_000, cid),
    )
    conn.commit()

    eng = live_monitor.Engine.__new__(live_monitor.Engine)
    eng.conn = conn
    handled = []

    async def spy(command, args):
        handled.append(command)
        return "ok"
    eng._handle_command = spy

    await eng._drain_commands()
    assert handled == []                              # never executed
    row = conn.execute("SELECT status FROM commands WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "expired"
    conn.close()


def test_prune_old_rows_keeps_pending_and_recent(tmp_path):
    import time as _t
    from database import prune_old_rows, resolve_command
    conn = database.init_db(tmp_path / "t.db")
    old = enqueue_command(conn, "status", {})
    resolve_command(conn, old, "done", "ok")
    conn.execute(
        "UPDATE commands SET resolved_ms=? WHERE id=?",
        (int(_t.time() * 1000) - 30 * 86_400_000, old),  # 30 days old
    )
    fresh_pending = enqueue_command(conn, "enter", {})   # pending, recent
    conn.commit()
    prune_old_rows(conn)
    ids = [r["id"] for r in conn.execute("SELECT id FROM commands").fetchall()]
    assert old not in ids and fresh_pending in ids
    conn.close()


async def test_drain_executes_fresh_command(tmp_path, monkeypatch):
    import live_monitor
    monkeypatch.setattr(config, "COMMAND_TTL_SECONDS", 60)
    conn = database.init_db(tmp_path / "t.db")
    cid = enqueue_command(conn, "status", {})

    eng = live_monitor.Engine.__new__(live_monitor.Engine)
    eng.conn = conn

    async def spy(command, args):
        return "done-ok"
    eng._handle_command = spy

    await eng._drain_commands()
    row = conn.execute(
        "SELECT status, response FROM commands WHERE id=?", (cid,)
    ).fetchone()
    assert row["status"] == "done" and row["response"] == "done-ok"
    conn.close()
