"""Scam-candidate screen: scoring, alert episodes, and the /scam board."""
from __future__ import annotations

import json
import time

import config
import control_bot
import scam_screen
from scam_screen import Metrics, Scored

H = 3_600_000


def _m(**kw) -> Metrics:
    return Metrics(symbol=kw.pop("symbol", "XUSDT"), base="X", **kw)


def test_canonical_base_strips_scaled_prefixes():
    assert scam_screen.canonical_base("1000PEPE") == "PEPE"
    assert scam_screen.canonical_base("1MBABYDOGE") == "BABYDOGE"
    assert scam_screen.canonical_base("1INCH") == "1INCH"      # a real ticker
    assert scam_screen.canonical_base("doge") == "DOGE"


def test_clean_large_coin_scores_zero():
    s = scam_screen.score(_m(funding_apr=0.1, oi_usd=10e6, perp_volume_usd=50e6,
                             spot_volume_usd=500e6, market_cap_usd=5e9,
                             float_ratio=0.9, age_days=400, max_day_gain_30d=0.1),
                          have_mcap_data=True)
    assert s.score == 0 and s.reasons == []


def test_squeeze_setup_scores_every_danger():
    s = scam_screen.score(_m(funding_apr=-9.9, oi_usd=60e6, perp_volume_usd=50e6,
                             spot_volume_usd=1e6, market_cap_usd=20e6,
                             float_ratio=0.12, age_days=20, max_day_gain_30d=0.8),
                          have_mcap_data=True)
    # OI/mcap 3.0 (2) + perp/spot 50x (2) + funding (2) + age (2) + mcap (2)
    # + float (1) + a +80% day (1)
    assert s.score == 12
    joined = " ".join(s.reasons)
    for part in ("OI/mcap 3.00", "perp/spot 50x", "funding -990% APR",
                 "listed 20d ago", "mcap $20.0M", "float 12%", "+80% day"):
        assert part in joined


def test_warning_tier_scores_one_each():
    s = scam_screen.score(_m(funding_apr=1.5, oi_usd=35e6, perp_volume_usd=20e6,
                             spot_volume_usd=1e6, market_cap_usd=60e6,
                             age_days=100),
                          have_mcap_data=True)
    assert s.score == 5          # OI/mcap .58, perp/spot 20x, funding, age, mcap


def test_unknown_market_cap_counts_only_when_coingecko_loaded():
    m = _m(perp_volume_usd=5e6, spot_volume_usd=None, market_cap_usd=None)
    loaded = scam_screen.score(m, have_mcap_data=True)
    assert "no market cap (outside top 2000)" in loaded.reasons
    assert "no spot volume" in loaded.reasons
    # CoinGecko down: say nothing rather than flag every coin.
    assert scam_screen.score(m, have_mcap_data=False).score == 0


def test_absurd_day_gain_is_capped_in_the_label():
    s = scam_screen.score(_m(max_day_gain_30d=6560.59), have_mcap_data=False)
    assert s.reasons == [">+1000% day in last 30d"]


async def test_listing_days_are_skipped_in_day_gains():
    """The listing candle's 'previous close' is a placeholder: skip the first
    two days after onboarding, keep real moves after that."""
    D = scam_screen.DAY_MS
    listed = 100 * D
    candles = [[listed, 0, 0, 0, "0.0001"],              # listing day, placeholder close
               [listed + D, 0, "65.6", 0, "1.0"],        # 'gain' off the placeholder
               [listed + 2 * D, 0, "1.1", 0, "1.0"],
               [listed + 3 * D, 0, "1.8", 0, "1.5"]]     # a real +80% day

    class _S(scam_screen.ScamScreen):
        async def _get(self, url, params=None, headers=None, timeout=20.0):
            return candles
    out = await _S(session=None)._day_gains({"XUSDT": listed})
    assert abs(out["XUSDT"] - 0.8) < 1e-9


def test_depth_bands_sum_usd_per_side_and_reach():
    book = {"bids": [["99", "10"], ["97", "10"], ["94", "10"]],     # mid 100
            "asks": [["101", "10"], ["104", "10"], ["109", "1"]]}
    d = scam_screen.depth_bands(book, "mexc")
    assert d["mexc_bid_2pct"] == 990.0                  # 99 only (>= 98)
    assert d["mexc_bid_5pct"] == 990.0 + 970.0          # >= 95
    assert d["mexc_bid_10pct"] == 990.0 + 970.0 + 940.0
    assert d["mexc_ask_2pct"] == 1010.0                 # <= 102
    assert d["mexc_ask_5pct"] == 1010.0 + 1040.0        # <= 105
    assert d["mexc_ask_10pct"] == 1010.0 + 1040.0 + 109.0
    assert abs(d["mexc_ask_reach_pct"] - 9.0) < 1e-9
    assert abs(d["mexc_bid_reach_pct"] - 6.0) < 1e-9
    assert scam_screen.depth_bands({"bids": [], "asks": []}, "x") == {}


def test_log_sweep_writes_one_row_per_coin_under_one_header(tmp_path, monkeypatch):
    import csv as _csv
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    m = _m(symbol="AUSDT", mark=1.5, oi_usd=2e6, funding_apr=-3.0,
           depth={"aster_ask_5pct": 1234.0, "mexc_bid_2pct": 50.0})
    rows = [Scored("AUSDT", 6, [], -3.0, None)]
    t = 1_792_800_000_000
    p = scam_screen.log_sweep([m], rows, t)
    scam_screen.log_sweep([m], rows, t + H)           # same day: append, no 2nd header
    got = list(_csv.DictReader(open(p, newline="")))
    assert p.name.startswith("scam_log_") and len(got) == 2
    r = got[0]
    assert r["symbol"] == "AUSDT" and r["score"] == "6" and r["oi_usd"] == "2e+06"
    assert r["aster_ask_5pct"] == "1234" and r["mexc_bid_2pct"] == "50"
    assert r["mexc_ask_10pct"] == "" and r["market_cap_usd"] == ""


def test_alert_once_per_episode_then_rearm(monkeypatch):
    monkeypatch.setattr(config, "SCAM_ALERT_SCORE", 5)
    monkeypatch.setattr(config, "SCAM_ALERT_REARM_HOURS", 24)
    hot = [Scored("AUSDT", 6, ["listed 3d ago"], 0.1, 5e6)]
    msg, state = scam_screen.plan_alert(hot, {}, 0, {})
    assert msg and "AUSDT [6]" in msg
    # Still qualifying an hour later: quiet.
    msg, state = scam_screen.plan_alert(hot, state, H, {})
    assert msg is None and "score:AUSDT" in state
    # Drops out, then back after the window: alerts again.
    _, state = scam_screen.plan_alert([], state, 30 * H, {})
    assert state == {}
    msg, _ = scam_screen.plan_alert(hot, state, 31 * H, {})
    assert msg and "AUSDT" in msg


def test_negative_funding_alerts_separately_whatever_the_score(monkeypatch):
    monkeypatch.setattr(config, "SCAM_NEG_FUNDING_APR", -2.0)
    rows = [Scored("NUSDT", 1, ["funding -500% APR"], -5.0, 5e8),
            Scored("PUSDT", 1, [], -1.0, 5e8)]
    msg, state = scam_screen.plan_alert(rows, {}, 0, {})
    assert "strongly negative funding" in msg
    assert "NUSDT" in msg and "PUSDT" not in msg
    assert set(state) == {"neg:NUSDT"}


def test_open_positions_are_marked_and_listed_first(monkeypatch):
    monkeypatch.setattr(config, "SCAM_ALERT_SCORE", 5)
    rows = [Scored("AUSDT", 9, ["x"], 0.0, 1e6), Scored("DNAUSDT", 5, ["y"], 0.0, 1e6)]
    msg, _ = scam_screen.plan_alert(rows, {}, 0, {"DNAUSDT": 315})
    lines = msg.splitlines()
    assert "OPEN POSITION #315" in lines[1] and lines[1].startswith("DNAUSDT")


def _snap(tmp_path, monkeypatch, rows):
    p = tmp_path / "scam.json"
    p.write_text(json.dumps({"ts_ms": int(time.time() * 1000),
                             "rows": rows, "alerted": {}}))
    monkeypatch.setattr(config, "SCAM_SNAPSHOT_FILE", p)


ROWS = [
    {"symbol": "AXISUSDT", "score": 6, "reasons": ["funding -3324% APR", "listed 3d ago"],
     "funding_apr": -33.24, "market_cap_usd": 46e6},
    {"symbol": "TREEUSDT", "score": 5, "reasons": ["perp/spot 37x"],
     "funding_apr": -5.36, "market_cap_usd": 13.7e6},
    {"symbol": "BTCUSDT", "score": 0, "reasons": [], "funding_apr": 0.1,
     "market_cap_usd": 2e12},
]


def test_scam_board_lists_flagged_names(tmp_path, monkeypatch):
    _snap(tmp_path, monkeypatch, ROWS)
    out = control_bot.ControlBot._cmd_scam(control_bot.ControlBot.__new__(control_bot.ControlBot), [])
    assert "AXISUSDT [6]" in out and "TREEUSDT [5]" in out
    assert "BTCUSDT" not in out                      # no warning signs


def test_scam_neg_and_symbol_detail(tmp_path, monkeypatch):
    _snap(tmp_path, monkeypatch, ROWS)
    bot = control_bot.ControlBot.__new__(control_bot.ControlBot)
    neg = control_bot.ControlBot._cmd_scam(bot, ["neg"])
    assert neg.index("AXISUSDT") < neg.index("TREEUSDT")   # most negative first
    assert "BTCUSDT" not in neg
    one = control_bot.ControlBot._cmd_scam(bot, ["tree"])
    assert "TREEUSDT: score 5" in one and "perp/spot 37x" in one


def test_scam_before_first_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCAM_SNAPSHOT_FILE", tmp_path / "missing.json")
    out = control_bot.ControlBot._cmd_scam(control_bot.ControlBot.__new__(control_bot.ControlBot), [])
    assert "no scam-screen data yet" in out
