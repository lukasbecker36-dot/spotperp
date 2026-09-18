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
