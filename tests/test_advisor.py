"""Advisor context-builder tests. The advisor is read-only; these lock in that
it marshals positions + heartbeat marks + funding opportunities correctly and
never blows up on missing snapshot files."""
import json
from decimal import Decimal

import pytest

import advisor
import config
import database
import position_manager as pm


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Isolated DB + snapshot files under tmp_path."""
    monkeypatch.setattr(config, "HEARTBEAT_FILE", tmp_path / "heartbeat.json")
    monkeypatch.setattr(config, "FUNDING_SNAPSHOT_FILE", tmp_path / "funding.json")
    conn = database.init_db(tmp_path / "test.db")
    yield conn, tmp_path
    conn.close()


def _open_position(conn):
    mgr = pm.PositionManager(conn)
    pos = mgr.create("CASHCATUSDT", Decimal(1000), paper=False)
    mgr.set_state(pos.id, pm.ENTERING)
    mgr.record_fill(pos.id, "aster", "entry", "SELL",
                    Decimal("100"), Decimal("1.01"), Decimal("0"))
    mgr.record_fill(pos.id, "mexc", "entry", "BUY",
                    Decimal("100"), Decimal("1.00"), Decimal("0.1"))
    mgr.set_state(pos.id, pm.OPEN)
    return pos.id


def test_context_marshals_position_and_marks(wired):
    conn, tmp = wired
    pid = _open_position(conn)
    # fresh heartbeat with a live mark for this position
    import time
    tmp.joinpath("heartbeat.json").write_text(json.dumps({
        "ts_ms": int(time.time() * 1000),
        "marks": {str(pid): {"close_bps": 8.0, "upnl_usd": 4.65,
                             "notional_usd": 1005.0, "liq_dist_pct": 24.0}},
    }))
    tmp.joinpath("funding.json").write_text(json.dumps({
        "ts_ms": 1, "rows": [
            {"symbol": "BTWUSDT", "current_8h_bps": 34.0, "avg_24h_8h_bps": 28.0,
             "net_edge_bps": 12.0, "max_notional_usd": 1200.0, "next_funding_h": 1.5},
        ],
    }))

    ctx = advisor.build_context(conn)
    assert ctx["marks_fresh"] is True
    assert ctx["mode"] in ("paper", "LIVE")
    assert len(ctx["open_positions"]) == 1
    p = ctx["open_positions"][0]
    assert p["symbol"] == "CASHCATUSDT"
    assert p["current_basis_bps"] == 8.0
    assert p["liq_distance_pct"] == 24.0
    assert p["upnl_usd"] == 4.65
    assert ctx["top_opportunities"][0]["symbol"] == "BTWUSDT"
    assert ctx["top_opportunities"][0]["funding_now_8h_bps"] == 34.0


def test_context_survives_missing_snapshots(wired):
    conn, _ = wired
    _open_position(conn)
    # no heartbeat / funding files written
    ctx = advisor.build_context(conn)
    assert ctx["marks_fresh"] is False
    assert ctx["top_opportunities"] == []
    # position still present, just without live marks
    assert ctx["open_positions"][0]["current_basis_bps"] is None


def test_stale_heartbeat_marked_not_fresh(wired):
    conn, tmp = wired
    pid = _open_position(conn)
    tmp.joinpath("heartbeat.json").write_text(json.dumps({
        "ts_ms": 1_000,  # ancient
        "marks": {str(pid): {"close_bps": 8.0}},
    }))
    ctx = advisor.build_context(conn)
    assert ctx["marks_fresh"] is False
    assert ctx["open_positions"][0]["current_basis_bps"] is None
