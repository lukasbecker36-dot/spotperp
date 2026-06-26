"""Control-bot deploy commands (/update): git pull + service restarts.

The bot is built via __new__ to skip the Telegram-credential __init__; only
the subprocess-shelling helpers are exercised, with subprocess stubbed so no
real git/systemctl runs.
"""
import subprocess
from types import SimpleNamespace

import pytest

import control_bot


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


def test_git_pull_success(shell):
    calls, results = shell
    results["rev-parse"] = _FakeProc(0, "claude/pensive-goldberg-q8221t\n", "")
    results["pull"] = _FakeProc(0, "Updating 0c32211..5313de9\nFast-forward\n", "")
    ok, msg = _bot()._git_pull()
    assert ok is True
    assert "claude/pensive-goldberg-q8221t" in msg
    assert "Fast-forward" in msg


def test_git_pull_failure(shell):
    calls, results = shell
    results["rev-parse"] = _FakeProc(0, "main\n", "")
    results["pull"] = _FakeProc(1, "", "fatal: not possible to fast-forward")
    ok, msg = _bot()._git_pull()
    assert ok is False
    assert "git pull failed" in msg


def test_update_aborts_and_skips_restart_on_pull_failure(shell):
    calls, results = shell
    results["rev-parse"] = _FakeProc(0, "main\n", "")
    results["pull"] = _FakeProc(1, "", "conflict")
    reply = _bot()._update()
    assert "update aborted" in reply
    # No systemctl restart and no detached control restart were issued.
    assert not any("systemctl" in c for c in calls["run"])
    assert calls["popen"] == []


def test_update_restarts_engine_then_control_detached(shell):
    calls, results = shell
    results["rev-parse"] = _FakeProc(0, "main\n", "")
    results["pull"] = _FakeProc(0, "Already up to date.\n", "")
    reply = _bot()._update()

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
