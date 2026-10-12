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
    monkeypatch.setattr(config, "SCREENER_SNAPSHOT_FILE", tmp_path / "screener.json")
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
    pm.PositionManager(conn).set_auto_entered(pid)
    # fresh heartbeat with a live mark for this position
    import time
    tmp.joinpath("heartbeat.json").write_text(json.dumps({
        "ts_ms": int(time.time() * 1000),
        "marks": {str(pid): {
            "close_bps": 8.0, "upnl_usd": 4.65, "notional_usd": 1005.0,
            "liq_dist_pct": 24.0, "auto_exit_bps": 12.5, "exit_low_bps": 10.0,
            "exit_ref": "24h exit low", "entry_range_24h": [20.0, 60.0],
            "exit_range_24h": [5.0, 40.0], "funding_now_bps_per_h": 1.2,
            "entry_bps_now": 30.0, "exit_taker_bps_now": 25.0,
        }},
    }))
    tmp.joinpath("funding.json").write_text(json.dumps({
        "ts_ms": 1, "rows": [
            {"symbol": "BTWUSDT", "current_8h_bps": 32.0, "avg_24h_8h_bps": 24.0,
             "entry_bps_avg": 45.0, "basis_p10_24h": 10.0, "basis_p90_24h": 50.0,
             "score": 61.0, "perp_volume_24h": 900_000, "mexc_ask_depth_usd": 400.0},
        ],
    }))
    tmp.joinpath("screener.json").write_text(json.dumps({
        "ts_ms": 1, "rows": [], "fill_rows": [],
        "spike_rows": [{"symbol": "SPKUSDT", "score": 33.0, "spike_z": 6.0,
                        "spike_bps": 50.0, "norm_close_bps": 4.0}],
    }))
    database.set_setting(conn, "auto_enter", {"spike": 50})
    database.save_stop_orders(conn, pid, "a1", "m1", Decimal("100"), Decimal("2"))

    ctx = advisor.build_context(conn)
    assert ctx["marks_fresh"] is True
    assert ctx["mode"] in ("paper", "LIVE")
    p = ctx["open_positions"][0]
    assert p["symbol"] == "CASHCATUSDT" and p["kind"] == "convergence"
    assert p["exit_bps_now"] == 8.0
    assert p["auto_exit_bps"] == 12.5 and p["auto_exit_from"] == "24h exit low +10.0"
    assert p["entry_range_24h"] == [20.0, 60.0]
    assert p["funding_now_bps_per_h"] == 1.2
    assert p["auto_entered"] is True and p["stops"] is True
    f = ctx["boards"]["funding"][0]
    assert f["symbol"] == "BTWUSDT" and f["score"] == 61.0
    assert f["funding_now_bps_per_h"] == 4.0          # per HOUR, not per 8h
    assert ctx["boards"]["spike"][0]["z"] == 6.0
    assert ctx["settings"]["auto_enter_usd"] == {"spike": 50}
    assert ctx["closed_today"]["count"] == 0


def test_context_survives_missing_snapshots(wired):
    conn, _ = wired
    _open_position(conn)
    # no heartbeat / funding files written
    ctx = advisor.build_context(conn)
    assert ctx["marks_fresh"] is False
    assert ctx["boards"] == {}
    # position still present, just without live marks
    assert ctx["open_positions"][0]["exit_bps_now"] is None


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
    assert ctx["open_positions"][0]["exit_bps_now"] is None



async def test_call_uses_sonnet_with_fallbacks_and_handles_refusal(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("ADVISOR_MODEL", raising=False)
    sent = {}

    class Resp:
        def __init__(self, body):
            self.status, self._body = 200, body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return self._body

    class Session:
        def __init__(self, body):
            self.body = body

        def post(self, url, json=None, headers=None, timeout=None):
            sent.update(payload=json, headers=headers)
            return Resp(self.body)

    ok = {"stop_reason": "end_turn",
          "content": [{"type": "thinking", "thinking": ""},
                      {"type": "text", "text": "Nothing urgent."}]}
    out = await advisor._call_claude(Session(ok), {"x": 1})
    assert out == "Nothing urgent."
    assert sent["payload"]["model"] == "claude-sonnet-5-5"
    assert sent["payload"]["fallbacks"] == "default"
    assert sent["headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert sent["payload"]["output_config"] == {"effort": config.ADVISOR_EFFORT}
    assert "temperature" not in sent["payload"] and "thinking" not in sent["payload"]

    refused = {"stop_reason": "refusal", "stop_details": {"category": "cyber"},
               "content": []}
    assert "refusal: cyber" in await advisor._call_claude(Session(refused), {})
