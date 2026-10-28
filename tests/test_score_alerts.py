"""Score alerts: one message per name per episode, and a history replay that
behaves exactly like the live alert."""
import time

import config
import control_bot
import database
import pytest

import score_alerts


@pytest.fixture(autouse=True)
def _no_sustain(monkeypatch):
    """Most tests here are about episodes and re-arming; the sustain rule has
    its own tests below."""
    monkeypatch.setattr(config, "SCORE_ALERT_SUSTAIN_SECONDS", 0.0)

MIN = 60_000


def test_alerts_once_per_episode_and_rearms_after_quiet():
    st = score_alerts.AlertState(rearm_ms=30 * MIN)
    assert st.observe(0, "fill", "A", 31.0, 30.0)            # first crossing
    assert not st.observe(1 * MIN, "fill", "A", 35.0, 30.0)   # still above
    assert not st.observe(2 * MIN, "fill", "A", 20.0, 30.0)   # dips below
    assert not st.observe(10 * MIN, "fill", "A", 32.0, 30.0)  # back within 30m
    assert st.observe(50 * MIN, "fill", "A", 32.0, 30.0)      # >30m below: new
    assert st.observe(50 * MIN, "fill", "B", 40.0, 30.0)      # names independent
    assert st.observe(50 * MIN, "funding", "A", 40.0, 30.0)   # boards independent


def test_reset_lets_names_already_above_alert_again():
    st = score_alerts.AlertState(rearm_ms=30 * MIN)
    assert st.observe(0, "fill", "A", 31.0, 30.0)
    st.reset("fill")
    assert st.observe(1 * MIN, "fill", "A", 31.0, 30.0)


def _history(days=2):
    """A every 2 minutes, crossing 50 once a day for an hour; B steady at 20."""
    rows = []
    for t in range(0, int(days * 24 * 60), 2):
        ts = t * MIN
        a = 60.0 if (t % (24 * 60)) < 60 else 10.0
        rows.append((ts, "A", a))
        rows.append((ts, "B", 20.0))
    return rows


def test_simulate_matches_the_live_rule():
    hist = _history()
    hits = score_alerts.simulate(hist, 50.0, rearm_ms=30 * MIN)
    assert [h[1] for h in hits] == ["A", "A"]                # once a day
    assert len(score_alerts.simulate(hist, 15.0, rearm_ms=30 * MIN)) == 2 + 1


def test_ladder_and_auto_level():
    hist = _history()
    rows = score_alerts.ladder(hist, [15.0, 50.0])
    by = {r["level"]: r for r in rows}
    assert by[50.0]["names"] == 1
    assert by[15.0]["names"] == 2
    # Walking down: 60 alerts ~1/day (A); 20 adds B -> 1.5/day, too noisy, so
    # the walk stops at 60 and never slides on into 10, where both names sit
    # above the line all day and it LOOKS quiet again (1/day).
    assert score_alerts.auto_level(hist, per_day=1.05) == 60.0
    assert len(score_alerts.simulate(hist, 10.0, rearm_ms=30 * MIN)) == 2
    assert score_alerts.auto_level(hist, per_day=0.5) == 61.0   # none quiet enough


def test_prune_drops_old_score_history(tmp_path):
    conn = database.init_db(tmp_path / "t.db")
    now = int(time.time() * 1000)
    database.record_scores(conn, now - 40 * 86_400_000, "fill",
                           [{"symbol": "OLD", "score": 1.0}])
    database.record_scores(conn, now, "fill", [{"symbol": "NEW", "score": 2.0}])
    database.prune_old_rows(conn, score_days=30)
    assert [r["symbol"] for r in database.score_history(conn, "fill")] == ["NEW"]


def _bot_with_db(tmp_path):
    bot = control_bot.ControlBot.__new__(control_bot.ControlBot)
    bot._conn = database.init_db(tmp_path / "t.db")
    return bot


def test_alert_command_sets_and_clears_levels(tmp_path):
    bot = _bot_with_db(tmp_path)
    assert "ON at score ≥ 30" in bot._cmd_alert(["fill", "30"])
    assert database.get_setting(bot._conn, "score_alert") == {"fill": 30.0}
    bot._cmd_alert(["funding", "45"])
    status = bot._cmd_alert([])
    assert "ON at ≥ 30" in status and "ON at ≥ 45" in status
    bot._cmd_alert(["fill", "off"])
    assert database.get_setting(bot._conn, "score_alert") == {"funding": 45.0}
    bot._cmd_alert(["off"])
    assert database.get_setting(bot._conn, "score_alert") == {}


def test_alert_auto_without_history_says_so(tmp_path):
    bot = _bot_with_db(tmp_path)
    assert "no score history yet" in bot._cmd_alert(["fill", "auto"])
    assert database.get_setting(bot._conn, "score_alert", {}) == {}


def test_alert_auto_and_scores_use_recorded_history(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCORE_ALERT_AUTO_PER_DAY", 1.05)
    bot = _bot_with_db(tmp_path)
    now = int(time.time() * 1000)
    base = now - 2 * 86_400_000
    for ts, sym, score in _history():
        database.record_scores(bot._conn, base + ts, "fill",
                               [{"symbol": sym, "score": score}])
    out = bot._cmd_alert(["fill", "auto"])
    assert "auto: would have fired" in out
    assert database.get_setting(bot._conn, "score_alert")["fill"] == 60.0
    scores = bot._cmd_scores(["fill"])
    assert "alerts/day" in scores and "← current" in scores
    assert "no score history yet" in bot._cmd_scores(["funding"])


def test_autoenter_command(tmp_path):
    bot = _bot_with_db(tmp_path)
    out = bot._cmd_autoenter(["fill", "200"])
    assert "auto-entry ON at $200" in out and "alert is OFF" in out
    bot._cmd_alert(["fill", "30"])
    assert "$200 when score ≥ 30" in bot._cmd_autoenter([])
    assert "must be in" in bot._cmd_autoenter(["fill", "999999"])
    bot._cmd_autoenter(["funding", "150"])
    bot._cmd_autoenter(["fill", "off"])
    assert database.get_setting(bot._conn, "auto_enter") == {"funding": 150.0}
    bot._cmd_autoenter(["off"])
    assert database.get_setting(bot._conn, "auto_enter") == {}


async def test_study_spike_runs_and_posts_a_report(tmp_path, monkeypatch):
    import csv as _csv, math as _math, random as _random
    _random.seed(3)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    now = time.time()
    for back in range(3, -1, -1):
        day = time.strftime("%Y%m%d", time.gmtime(now - back * 86400))
        t0 = int((now - back * 86400) // 86400 * 86400 * 1000)
        with open(tmp_path / f"basis_log_{day}.csv", "w", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["ts_ms", "symbol", "entry_bps", "close_bps",
                        "funding_8h_bps", "max_notional_usd", "perp_trades_24h"])
            for m in range(0, 1440):
                e = 15 + _random.gauss(0, 3)
                if m % 300 < 30:
                    e += 60 * (1 - (m % 300) / 30)
                w.writerow([t0 + m * 60000, "AAAUSDT", f"{e:.2f}",
                            f"{e - 8:.2f}", "1.0", "500", "20000"])
    bot = _bot_with_db(tmp_path)
    sent = []

    async def fake_send(chat, text):
        sent.append((chat, text))
    bot._send = fake_send
    bot._reply_chat = "42"
    reply = bot._cmd_study(["spike", "3"])
    assert "spike study started" in reply
    assert "already running" in bot._cmd_study(["spike"])
    await bot._study_task
    assert sent and sent[0][0] == "42"
    report = sent[0][1]
    assert "does a spike PAY" in report and "spike_z" in report
    assert "roundtrip" not in report.split("\n")[0]
    assert "FAILED" not in report


def test_spike_alert_shows_each_side_against_its_own_norm():
    msg = score_alerts.format_alert("spike", {
        "symbol": "IOUSDT", "score": 33.4, "entry_bps": 40.7, "lo24_bps": -11.7,
        "hi24_bps": 35.0, "funding_8h_bps": 0.56, "volume_24h": 155_000,
        "depth_usd": 4561, "jitter_bps": 8.3, "spike_bps": 45.5, "spike_z": 6.4,
        "norm_close_bps": -22.0, "norm_entry_bps": -4.8,
        "exit_lo24_bps": -30.1, "exit_hi24_bps": -8.4}, 20)
    assert "vs norm entry -4.8 → spike +45.5 (z 6.4)" in msg
    assert "norm exit -22.0 (24h exit range -30.1 to -8.4)" in msg



def test_a_score_must_hold_before_it_alerts():
    """A one-scan flicker must not alert (or auto-enter)."""
    st = score_alerts.AlertState(rearm_ms=30 * MIN, sustain_ms=30_000)
    S = 1000
    assert not st.observe(0, "fill", "A", 50.0, 40.0)            # starts the run
    assert not st.observe(15 * S, "fill", "A", 50.0, 40.0)       # 15s held
    assert st.observe(30 * S, "fill", "A", 50.0, 40.0)           # 30s held: alert
    # A flicker: up for one scan, then down — never alerts.
    assert not st.observe(0, "fill", "B", 50.0, 40.0)
    assert not st.observe(15 * S, "fill", "B", 10.0, 40.0)       # breaks the run
    assert not st.observe(30 * S, "fill", "B", 50.0, 40.0)       # run restarts
    assert not st.observe(45 * S, "fill", "B", 50.0, 40.0)
    assert st.observe(60 * S, "fill", "B", 50.0, 40.0)
