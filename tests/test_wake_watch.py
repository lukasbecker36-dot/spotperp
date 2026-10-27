"""wake_watch.py: the AIN-pattern filter, history loading, and the 24h/72h follow-up."""
import csv
import gzip

import pytest

import config
import wake_watch as ww

H = ww.HOUR_MS
NOW = 1_793_000_000_000 - (1_793_000_000_000 % H)
COLS = ["ts_ms", "symbol", "mark", "funding_apr", "mexc_ask_5pct", "max_day_gain_30d", "age_days", "score"]


def ain_like(sym="AINUSDT", fund_now=0.6, ask_now=3.0, mark_now=0.040, gain=1.5, age=120):
    """40 hourly sweeps: flat price, funding 0.11 then rising to fund_now in the last
    4h, MEXC ask ~5k then ask_now (in $k) at the last sweep."""
    rows = []
    for i in range(40, -1, -1):
        t = NOW - i * H
        fund = fund_now if i <= 3 else 0.11
        ask = ask_now * 1000 if i == 0 else 5000
        mark = mark_now if i == 0 else 0.038
        rows.append({"ts_ms": t, "symbol": sym, "mark": mark, "funding_apr": fund,
                     "mexc_ask_5pct": ask, "max_day_gain_30d": gain, "age_days": age, "score": 5})
    return rows


def write_log(tmp_path, rows):
    by_day = {}
    for r in rows:
        day = __import__("time").strftime("%Y%m%d", __import__("time").gmtime(r["ts_ms"] / 1000))
        by_day.setdefault(day, []).append(r)
    for i, (day, rs) in enumerate(sorted(by_day.items())):
        path = tmp_path / (f"scam_log_{day}.csv" + (".gz" if i == 0 else ""))   # older day rotated
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "wt", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            w.writerows(rs)


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(ww, "STATE_FILE", tmp_path / "wake_watch.json")
    monkeypatch.setattr(ww, "RESULTS_FILE", tmp_path / "wake_watch_results.csv")
    return tmp_path


def test_ain_pattern_matches_and_variants_do_not(out):
    write_log(out, ain_like() + ain_like("MOVEDUSDT", mark_now=0.050)          # already +30%
              + ain_like("YOUNGUSDT", age=20) + ain_like("NOSPIKEUSDT", gain=0.2)
              + ain_like("THICKUSDT", ask_now=5.0) + ain_like("COLDUSDT", fund_now=0.2))
    hist = ww.load_history(NOW, out_dir=out)
    assert set(hist) == {"AINUSDT", "MOVEDUSDT", "YOUNGUSDT", "NOSPIKEUSDT", "THICKUSDT", "COLDUSDT"}
    hits = {s for s, rows in hist.items() if ww.matches(ww.features(rows, NOW))}
    assert hits == {"AINUSDT"}


def test_not_enough_history_never_matches(out):
    write_log(out, ain_like()[-6:])
    assert not ww.matches(ww.features(ww.load_history(NOW, out_dir=out)["AINUSDT"], NOW))


def test_alert_once_then_24h_and_72h_followups(out):
    write_log(out, ain_like())
    hist = ww.load_history(NOW, out_dir=out)
    msgs = ww.step(NOW, {"AINUSDT": 0.040}, hist)
    assert len(msgs) == 1 and "AINUSDT" in msgs[0] and "WATCH ONLY" in msgs[0]
    assert ww.step(NOW + H, {"AINUSDT": 0.12}, hist) == []                     # no repeat alert
    m24 = ww.step(NOW + 24 * H, {"AINUSDT": 0.10}, hist)
    assert len(m24) == 1 and "after 24h: +150%" in m24[0] and "max +200%" in m24[0]
    m72 = ww.step(NOW + 72 * H, {"AINUSDT": 0.06}, hist)
    assert any("after 72h: +50%" in m for m in m72) and any("1 completed: 1 reached +30%, 1 reached +100%" in m for m in m72)
    r = ww.results()[0]
    assert r["symbol"] == "AINUSDT" and float(r["max_72h"]) == pytest.approx(2.0)
    assert "AINUSDT" not in ww.load_state()["active"]


def test_rearm_after_72h_only(out):
    write_log(out, ain_like())
    hist = ww.load_history(NOW, out_dir=out)
    ww.step(NOW, {"AINUSDT": 0.040}, hist)
    st = ww.load_state()
    st["active"].clear()
    ww.save_state(st)
    assert ww.step(NOW + 2 * H, {}, hist) == []                                  # inside the 72h re-arm
