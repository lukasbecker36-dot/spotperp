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
             "entry_bps": 45.0, "close_bps": -5.0, "spread_cost_bps": 50.0,
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
    opp = ctx["top_opportunities"][0]
    assert opp["symbol"] == "BTWUSDT"
    assert opp["funding_now_8h_bps"] == 34.0
    assert opp["entry_basis_bps"] == 45.0
    assert opp["exit_basis_bps"] == -5.0        # exit basis surfaced for vetting
    assert opp["depth_usd"] == 1200.0


def test_context_survives_missing_snapshots(wired):
    conn, _ = wired
    _open_position(conn)
    # no heartbeat / funding files written
    ctx = advisor.build_context(conn)
    assert ctx["marks_fresh"] is False
    assert ctx["top_opportunities"] == []
    # position still present, just without live marks
    assert ctx["open_positions"][0]["current_basis_bps"] is None


def test_run_window_gate(monkeypatch):
    monkeypatch.setattr(config, "ADVISOR_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "ADVISOR_RUN_HOURS", [7, 11, 15, 19, 23])
    from datetime import datetime
    from zoneinfo import ZoneInfo
    hour = datetime.now(ZoneInfo("Europe/London")).hour
    assert advisor._in_run_window() == (hour in [7, 11, 15, 19, 23])
    # empty hours => never in window
    monkeypatch.setattr(config, "ADVISOR_RUN_HOURS", [])
    assert advisor._in_run_window() is False


def test_run_window_fails_open_on_bad_tz(monkeypatch):
    monkeypatch.setattr(config, "ADVISOR_TIMEZONE", "Not/AZone")
    monkeypatch.setattr(config, "ADVISOR_RUN_HOURS", [7])
    assert advisor._in_run_window() is True  # unknown tz -> run rather than go silent


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
