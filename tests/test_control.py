"""Control-bot deploy commands (/update): git pull + service restarts.

The bot is built via __new__ to skip the Telegram-credential __init__; only
the subprocess-shelling helpers are exercised, with subprocess stubbed so no
real git/systemctl runs.
"""
import inspect
import re
import subprocess
from types import SimpleNamespace

import pytest

import control_bot


def test_bot_commands_valid_telegram_format():
    """Telegram rejects the whole setMyCommands if any entry is malformed."""
    seen = set()
    for c in control_bot.BOT_COMMANDS:
        name, desc = c["command"], c["description"]
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), f"bad name {name!r}"
        assert 1 <= len(desc) <= 256, f"bad description length for {name}"
        assert name not in seen, f"duplicate command {name}"
        seen.add(name)


def test_every_menu_command_is_dispatched():
    """No menu command may be missing a handler (guards against drift/typos)."""
    src = inspect.getsource(control_bot.ControlBot._dispatch)
    for c in control_bot.BOT_COMMANDS:
        assert f'"{c["command"]}"' in src, f'{c["command"]} not handled in _dispatch'


def _bot() -> control_bot.ControlBot:
    return control_bot.ControlBot.__new__(control_bot.ControlBot)


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def shell(monkeypatch):
    """Capture subprocess.run / Popen; drive run() results by matched args."""
    calls = {"run": [], "popen": []}
    results: dict[str, _FakeProc] = {}

    def fake_run(cmd, capture_output=False, text=False):
        calls["run"].append(cmd)
        for key, proc in results.items():
            if key in " ".join(cmd):
                return proc
        return _FakeProc(0, "ok", "")

    def fake_popen(cmd, start_new_session=False):
        calls["popen"].append((cmd, start_new_session))
        return SimpleNamespace(pid=1234)

    monkeypatch.setattr(control_bot.subprocess, "run", fake_run)
    monkeypatch.setattr(control_bot.subprocess, "Popen", fake_popen)
    return calls, results


async def test_git_pull_success(shell):
    calls, results = shell
    results["rev-parse"] = _FakeProc(0, "claude/pensive-goldberg-q8221t\n", "")
    results["pull"] = _FakeProc(0, "Updating 0c32211..5313de9\nFast-forward\n", "")
    ok, msg = await _bot()._git_pull()
    assert ok is True
    assert "claude/pensive-goldberg-q8221t" in msg
    assert "Fast-forward" in msg


async def test_git_pull_failure(shell):
    calls, results = shell
    results["rev-parse"] = _FakeProc(0, "main\n", "")
    results["pull"] = _FakeProc(1, "", "fatal: not possible to fast-forward")
    ok, msg = await _bot()._git_pull()
    assert ok is False
    assert "git pull failed" in msg


async def test_update_aborts_and_skips_restart_on_pull_failure(shell):
    calls, results = shell
    results["rev-parse"] = _FakeProc(0, "main\n", "")
    results["pull"] = _FakeProc(1, "", "conflict")
    reply = await _bot()._update()
    assert "update aborted" in reply
    # No systemctl restart and no detached control restart were issued.
    assert not any("systemctl" in c for c in calls["run"])
    assert calls["popen"] == []


async def test_update_restarts_engine_then_control_detached(shell):
    calls, results = shell
    results["rev-parse"] = _FakeProc(0, "main\n", "")
    results["pull"] = _FakeProc(0, "Already up to date.\n", "")
    reply = await _bot()._update()

    # Engine restarted synchronously via systemctl run.
    assert any(
        c[:3] == ["sudo", "systemctl", "restart"]
        and control_bot.ENGINE_SERVICE in c
        for c in calls["run"]
    )
    # Control bot restarted out-of-band, detached, after a delay.
    assert len(calls["popen"]) == 1
    popen_cmd, new_session = calls["popen"][0]
    assert new_session is True
    joined = " ".join(popen_cmd)
    assert "sleep" in joined and control_bot.CONTROL_SERVICE in joined
    assert "restarting" in reply


def _screen_snapshot(tmp_path, monkeypatch, rows):
    import json as _json
    import time as _time
    import config as _config
    p = tmp_path / "snap.json"
    p.write_text(_json.dumps({"ts_ms": int(_time.time() * 1000),
                              "rows": rows, "diff_rows": []}))
    monkeypatch.setattr(_config, "SCREENER_SNAPSHOT_FILE", p)


def _srow(symbol, entry, avg24, hours, depth=500.0):
    return {"symbol": symbol, "entry_bps": entry, "entry_bps_avg": entry,
            "entry_bps_avg_24h": avg24, "hours_24h": hours,
            "net_edge_bps": 10.0, "net_edge_bps_avg": 10.0,
            "funding_8h_bps": 1.0, "max_notional_usd": depth, "samples": 19}


def test_screen_marks_thin_24h_history(tmp_path, monkeypatch):
    """With too little history DailyBasis falls back to the LIVE basis, which
    would otherwise read as a genuine 24h norm — and silently explains why the
    pair is absent from /screen diff. It must be marked."""
    _screen_snapshot(tmp_path, monkeypatch, [
        _srow("THINUSDT", 62.2, 56.5, hours=2.0),
        _srow("SEASONEDUSDT", 30.4, 25.4, hours=24.0),
    ])
    out = control_bot.ControlBot._cmd_screen(_bot(), [])
    assert "56.5?" in out            # thin history flagged
    assert "25.4?" not in out        # real norm unflagged
    assert "excluded from /screen diff" in out


def test_screen_diff_reports_when_no_dislocation_rows(tmp_path, monkeypatch):
    _screen_snapshot(tmp_path, monkeypatch, [_srow("AAAUSDT", 10.0, 5.0, 24.0)])
    out = control_bot.ControlBot._cmd_screen(_bot(), ["diff"])
    assert "no dislocation data yet" in out


def test_screen_fill_flags_missing_volume_data(tmp_path, monkeypatch):
    """No volume on ANY row means the Aster 24h ticker call is failing — a bug,
    not an empty market. Say so instead of listing the thresholds."""
    _screen_snapshot(tmp_path, monkeypatch, [
        {**_srow("AAAUSDT", 50.0, 10.0, 24.0), "perp_volume_24h": 0,
         "hours_tradeable_24h": 20},
    ])
    out = control_bot.ControlBot._cmd_screen(_bot(), ["fill"])
    assert "NO perp volume data at all" in out
    assert "journalctl" in out


def test_screen_fill_reports_best_dwell_when_thresholds_unmet(tmp_path, monkeypatch):
    _screen_snapshot(tmp_path, monkeypatch, [
        {**_srow("AAAUSDT", 50.0, 10.0, 24.0), "perp_volume_24h": 5_000_000,
         "hours_tradeable_24h": 2},
    ])
    out = control_bot.ControlBot._cmd_screen(_bot(), ["fill"])
    assert "best right now: 2h" in out


def test_pad_counts_display_columns_not_code_points():
    """The CJK-named pairs top the fillability screen, and a CJK glyph takes
    two monospace cells. Padding on len() would shunt the rest of the row right
    and the board would stop lining up."""
    assert control_bot._pad("哈基米USDT", 11) == "哈基米USDT "   # 10 cells + 1
    assert control_bot._pad("GRVTUSDT", 11) == "GRVTUSDT   "
    assert control_bot._pad("我踏马来了USDT", 11) == "我踏马来了U"   # 14 cells -> trimmed to exactly 11


def _cells(text):
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def test_pad_never_exceeds_its_width():
    for sym in ("哈基米USDT", "我踏马来了USDT", "A", "", "LONGASCIISYMBOLUSDT"):
        assert _cells(control_bot._pad(sym, 11)) <= 11


def test_every_dispatched_command_is_in_the_menu():
    """The reverse of the check above. Six commands — orders, equity,
    recompute, truefill, auto, fills — were added to _dispatch and worked when
    typed, but never reached the Telegram Menu, so they were invisible unless
    you already knew the name."""
    src = inspect.getsource(control_bot.ControlBot._dispatch)
    dispatched = set(re.findall(r'command == "([a-z_]+)"', src))
    menu = {c["command"] for c in control_bot.BOT_COMMANDS}
    # help/menu are entry points rather than tools; everything else must show.
    missing = dispatched - menu - {"help", "menu"}
    assert not missing, f"dispatched but not in the menu: {sorted(missing)}"


# ── /fills ───────────────────────────────────────────────────────────────────

import database  # noqa: E402
import position_manager as pm  # noqa: E402
from decimal import Decimal  # noqa: E402


@pytest.fixture
def fills_bot(tmp_path):
    conn = database.init_db(tmp_path / "t.db")
    bot = _bot()
    bot._positions = pm.PositionManager(conn)
    yield bot
    conn.close()


def _closed(bot, symbol="GUSDT", perp_out=1000, spot_out=1000, mult=1):
    mgr = bot._positions
    pos = mgr.create(symbol, Decimal(100), paper=False, trade_kind="carry")
    mgr.record_fill(pos.id, "aster", "entry", "SELL", Decimal(1000),
                    Decimal("1.0050") * mult, Decimal("0.05"))
    mgr.record_fill(pos.id, "mexc", "entry", "BUY", Decimal(1000) * mult,
                    Decimal("1.0000"), Decimal("0.05"))
    mgr.record_fill(pos.id, "aster", "exit", "BUY", Decimal(perp_out),
                    Decimal("1.0300") * mult, Decimal("0.09"))
    mgr.record_fill(pos.id, "mexc", "exit", "SELL", Decimal(spot_out) * mult,
                    Decimal("1.0280"), Decimal("0.05"))
    mgr.set_state(pos.id, pm.CLOSED)
    mgr.finalize_pnl(pos.id)
    return pos.id


def test_fills_shows_every_fill_and_the_pnl_components(fills_bot):
    pid = _closed(fills_bot)
    out = fills_bot._cmd_fills([str(pid)])
    assert out.count(" perp entry") == 1 and out.count(" spot exit") == 1
    assert "hedge  entry matched · exit matched" in out
    for part in ("perp leg", "spot leg", "funding", "fees", "total"):
        assert part in out
    assert "differs by" not in out          # recorded == recomputed


def test_fills_flags_a_hedge_size_mismatch(fills_bot):
    """The question behind it: were the two legs the same size? A gap means
    part of the position carried naked delta, and the coin's own move drove
    that part of the P&L — position 249's loss was larger than its basis move
    alone could explain."""
    pid = _closed(fills_bot, perp_out=1000, spot_out=800)
    out = fills_bot._cmd_fills([str(pid)])
    assert "exit ⚠ spot -200" in out and "-20.0%" in out


def test_fills_warns_when_the_recorded_pnl_is_stale(fills_bot):
    pid = _closed(fills_bot)
    fills_bot._positions._conn.execute(
        "UPDATE positions SET realized_pnl_usd='-24.93' WHERE id=?", (pid,))
    fills_bot._positions._conn.commit()
    out = fills_bot._cmd_fills([str(pid)])
    assert "recorded -$24.93" in out
    assert f"/recompute {pid}" in out


def test_fills_compares_legs_through_the_contract_multiplier(fills_bot):
    """Perp in contracts, spot in coins: a 1000X contract is matched when the
    spot holds 1000x the perp count, not when the counts are equal."""
    pid = _closed(fills_bot, symbol="1000PEPEUSDT", mult=1000)
    out = fills_bot._cmd_fills([str(pid)])
    assert "x1000" in out and "exit matched" in out


def test_fills_resolves_by_symbol_and_defaults_to_latest(fills_bot):
    first = _closed(fills_bot, symbol="GUSDT")
    second = _closed(fills_bot, symbol="BUSDT")
    assert f"#{first} GUSDT" in fills_bot._cmd_fills(["g"])
    assert f"#{second} BUSDT" in fills_bot._cmd_fills([])
    assert "no position #999" in fills_bot._cmd_fills(["999"])


def test_fills_elides_the_middle_of_a_long_history(fills_bot):
    """Positions worked in $50 clips run to hundreds of fills; the message
    stays readable on a phone."""
    mgr = fills_bot._positions
    pos = mgr.create("GUSDT", Decimal(100), paper=False)
    for _ in range(30):
        mgr.record_fill(pos.id, "aster", "entry", "SELL", Decimal(10),
                        Decimal("1.005"), Decimal(0))
        mgr.record_fill(pos.id, "mexc", "entry", "BUY", Decimal(10),
                        Decimal("1.000"), Decimal(0))
    out = fills_bot._cmd_fills([str(pos.id)])
    assert "30 more fills" in out
    assert "no realised P&L" in out


def test_screen_fill_shows_hi24_volume_and_level_depth(tmp_path, monkeypatch):
    _screen_snapshot(tmp_path, monkeypatch, [])
    import json as _json, time as _time
    import config as _config
    row = {**_srow("AAAUSDT", 60.0, 30.0, 24.0, depth=10.0),
           "basis_p10_24h": 5.0, "basis_p90_24h": 88.8,
           "perp_volume_24h": 1_250_000, "hours_tradeable_24h": 20,
           "perp_trades_24h": 2400, "mexc_ask_depth_usd": 2000.0}
    _config.SCREENER_SNAPSHOT_FILE.write_text(_json.dumps(
        {"ts_ms": int(_time.time() * 1000), "rows": [row], "fill_rows": [row]}))
    out = control_bot.ControlBot._cmd_screen(_bot(), ["fill"])
    assert "hi24" in out and "88.8" in out
    assert "1.2M" in out
    net = 60.0 - 5.0 - float(_config.ENTRY_MIN_EDGE_FLOOR_BPS)
    assert f"{net * 2000.0 / 10000.0:.1f}" in out     # $clip off 5-level depth


def test_funding_shows_volume_and_level_depth(tmp_path, monkeypatch):
    import json as _json, time as _time
    import config as _config
    p = tmp_path / "fnd.json"
    p.write_text(_json.dumps({"ts_ms": int(_time.time() * 1000), "rows": [{
        "symbol": "AAAUSDT", "current_8h_bps": 2.0, "avg_24h_8h_bps": 2.0,
        "entry_bps": 40.0, "entry_bps_avg": 40.0, "samples": 5,
        "max_notional_usd": 12.0, "mexc_ask_depth_usd": 3456.0,
        "perp_volume_24h": 640_000, "score": 30.0, "hours_24h": 24.0,
        "basis_p10_24h": 10.0, "basis_p90_24h": 60.0,
    }]}))
    monkeypatch.setattr(_config, "FUNDING_SNAPSHOT_FILE", p)
    out = control_bot.ControlBot._cmd_funding(_bot(), [])
    assert "640k" in out
    assert "3,456" in out
