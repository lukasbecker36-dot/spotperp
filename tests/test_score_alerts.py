"""Score alerts: one message per name per episode, and a history replay that
behaves exactly like the live alert."""
import time

import config
import control_bot
import database
import score_alerts

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
