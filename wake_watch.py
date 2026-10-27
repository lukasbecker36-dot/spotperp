"""Wake-up watch: alert (never trade) when an old, previously-spiked coin starts to
look like AIN did hours before its 26 Oct 2026 run, and record what happens next.

AIN, 26 Oct: price flat, but funding climbed from the +11%/yr default to +48-69%/yr
and MEXC spot offers within 5% thinned to ~2/3 of normal, hours before a 3.6x day.
Open interest and the scam score only moved after the price did. Over the first
three days of scam logs, requiring all of

    funding now > +30%/yr AND its last-4h average >= 25 points above its own
      baseline (median of the prints 12-36h earlier)       longs paying more
    MEXC ask depth within 5% <= 70% of its own 24h median   offers thinning
    |price change over 6h| < 10%                            not moved yet
    a +50% day within the last 30 days                      a spiker
    listed >= 60 days                                       old, "dead" coin

matched 4 coins: AIN (+262% within 24h), ARIA (+21%), SIREN and BR (flat). $100 each
made +$279. That filter was drawn while looking at AIN, so it proves nothing yet;
this module exists to collect the out-of-sample cases: each match is alerted once,
followed for 72h, and appended to output/wake_watch_results.csv with running stats.

History comes from the scam screen's own hourly logs (output/scam_log_*.csv[.gz]),
so a restart loses nothing. Highs and lows are the hourly marks the sweep sees.
"""
from __future__ import annotations

import csv
import gzip
import json
import statistics
import time
from pathlib import Path

import config

HOUR_MS = 3_600_000
STATE_FILE = config.OUTPUT_DIR / "wake_watch.json"
RESULTS_FILE = config.OUTPUT_DIR / "wake_watch_results.csv"
RESULT_COLS = ["signal_ms", "symbol", "entry", "max_24h", "min_24h", "ret_24h",
               "max_72h", "min_72h", "ret_72h"]


def _num(v):
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def load_history(now_ms: int, hours: float = 40.0, out_dir: Path | None = None) -> dict[str, list[dict]]:
    """symbol -> sweep rows from the last `hours`, oldest first, from today's and
    yesterday's scam logs (plain or rotated .gz)."""
    out_dir = out_dir or config.OUTPUT_DIR
    since = now_ms - hours * HOUR_MS
    days = {time.strftime("%Y%m%d", time.gmtime(t / 1000))
            for t in range(int(since), int(now_ms) + 86_400_000, 86_400_000) if t <= now_ms}
    days.add(time.strftime("%Y%m%d", time.gmtime(now_ms / 1000)))
    hist: dict[str, list[dict]] = {}
    for day in sorted(days):
        for path in (out_dir / f"scam_log_{day}.csv", out_dir / f"scam_log_{day}.csv.gz"):
            if not path.exists():
                continue
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", newline="", encoding="utf-8", errors="replace") as f:   # CJK tickers
                for r in csv.DictReader(f):
                    ts = _num(r.get("ts_ms"))
                    if ts is None or ts < since or ts > now_ms:
                        continue
                    hist.setdefault(r["symbol"], []).append({
                        "ts": ts, "mark": _num(r.get("mark")), "fund": _num(r.get("funding_apr")),
                        "ask5": _num(r.get("mexc_ask_5pct")), "gain30": _num(r.get("max_day_gain_30d")),
                        "age": _num(r.get("age_days")), "score": _num(r.get("score"))})
    for rows in hist.values():
        rows.sort(key=lambda x: x["ts"])
    return hist


def features(rows: list[dict], now_ms: int) -> dict | None:
    """The filter's inputs at the latest sweep, or None without enough history."""
    if not rows:
        return None
    cur = rows[-1]
    if now_ms - cur["ts"] > 2 * HOUR_MS:
        return None

    def between(lo_h, hi_h, key):
        return [r[key] for r in rows
                if r[key] is not None and now_ms - hi_h * HOUR_MS <= r["ts"] <= now_ms - lo_h * HOUR_MS]

    recent = between(0, 3.5, "fund")
    base = between(12, 36, "fund")
    asks = between(4, 28, "ask5")
    then = [r for r in rows if r["mark"] and abs(r["ts"] - (now_ms - 6 * HOUR_MS)) <= 0.6 * HOUR_MS]
    if len(base) < 12 or len(asks) < 12 or not recent or not then or not cur["mark"]:
        return None
    med_ask = statistics.median(asks)
    return {
        "fund": cur["fund"], "d_fund": statistics.mean(recent) - statistics.median(base),
        "fund_base": statistics.median(base),
        "ask_ratio": (cur["ask5"] / med_ask) if cur["ask5"] is not None and med_ask > 0 else None,
        "px_chg6": cur["mark"] / then[0]["mark"] - 1, "gain30": cur["gain30"], "age": cur["age"],
        "mark": cur["mark"], "score": cur["score"]}


def matches(f: dict | None) -> bool:
    return bool(
        f and f["fund"] is not None and f["ask_ratio"] is not None
        and f["gain30"] is not None and f["age"] is not None
        and f["fund"] > config.WAKE_FUND_MIN
        and f["d_fund"] >= config.WAKE_FUND_RISE
        and f["ask_ratio"] <= config.WAKE_ASK_RATIO
        and abs(f["px_chg6"]) < config.WAKE_MAX_MOVE
        and f["gain30"] >= config.WAKE_PRIOR_SPIKE
        and f["age"] >= config.WAKE_MIN_AGE_DAYS)


def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("active", {})
    st.setdefault("last_alert", {})
    return st


def save_state(st: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE_FILE)


def results() -> list[dict]:
    try:
        with open(RESULTS_FILE, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def stats_line(rows: list[dict] | None = None) -> str:
    rows = results() if rows is None else rows
    if not rows:
        return "no completed watches yet"
    mx = [float(r["max_72h"]) for r in rows]
    r72 = [float(r["ret_72h"]) for r in rows]
    r24 = [float(r["ret_24h"]) for r in rows]
    n = len(rows)
    return (f"{n} completed: {sum(m >= 0.3 for m in mx)} reached +30%, {sum(m >= 1.0 for m in mx)} "
            f"reached +100% within 72h; median 72h {statistics.median(r72):+.0%}. $100 each, "
            f"held 24h: ${100 * sum(r24):+,.0f}, held 72h: ${100 * sum(r72):+,.0f} (before costs)")


def step(now_ms: int, marks: dict[str, float], hist: dict[str, list[dict]] | None = None) -> list[str]:
    """One sweep: new matches, 24h updates and 72h results. Returns the messages to
    send. `marks` = this sweep's mark per symbol."""
    st = load_state()
    msgs: list[str] = []
    hist = load_history(now_ms) if hist is None else hist

    for sym, w in list(st["active"].items()):
        px = marks.get(sym)
        if px:
            w["hi"], w["lo"], w["last"] = max(w["hi"], px), min(w["lo"], px), px
        age_h = (now_ms - w["ts"]) / HOUR_MS
        if age_h >= 24 and "r24" not in w:
            w.update(r24=w["last"] / w["entry"] - 1, hi24=w["hi"] / w["entry"] - 1, lo24=w["lo"] / w["entry"] - 1)
            msgs.append(f"⏱ wake-watch {sym} after 24h: {w['r24']:+.0%} (max {w['hi24']:+.0%}, "
                        f"min {w['lo24']:+.0%}) from {w['entry']:.6g}")
        if age_h >= 72:
            row = {"signal_ms": int(w["ts"]), "symbol": sym, "entry": w["entry"],
                   "max_24h": w.get("hi24", w["hi"] / w["entry"] - 1), "min_24h": w.get("lo24", w["lo"] / w["entry"] - 1),
                   "ret_24h": w.get("r24", w["last"] / w["entry"] - 1),
                   "max_72h": w["hi"] / w["entry"] - 1, "min_72h": w["lo"] / w["entry"] - 1,
                   "ret_72h": w["last"] / w["entry"] - 1}
            new = not RESULTS_FILE.exists()
            RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(RESULTS_FILE, "a", newline="") as f:
                wr = csv.DictWriter(f, fieldnames=RESULT_COLS)
                if new:
                    wr.writeheader()
                wr.writerow({k: (f"{v:.6g}" if isinstance(v, float) else v) for k, v in row.items()})
            del st["active"][sym]
            msgs.append(f"🏁 wake-watch {sym} after 72h: {row['ret_72h']:+.0%} (max {row['max_72h']:+.0%}, "
                        f"min {row['min_72h']:+.0%})\nrunning tally — {stats_line()}")

    for sym, rows in hist.items():
        if sym in st["active"]:
            continue
        last = st["last_alert"].get(sym, 0)
        if now_ms - last < config.WAKE_REARM_HOURS * HOUR_MS:
            continue
        f = features(rows, now_ms)
        if not matches(f):
            continue
        st["last_alert"][sym] = now_ms
        st["active"][sym] = {"ts": now_ms, "entry": f["mark"], "hi": f["mark"], "lo": f["mark"], "last": f["mark"]}
        msgs.append(
            f"👀 wake-up watch: {sym} @ {f['mark']:.6g} — looks like AIN before its run\n"
            f"  funding {f['fund']:+.0%}/yr, up {f['d_fund']:+.0%} on its baseline {f['fund_base']:+.0%}\n"
            f"  MEXC offers within 5% at {f['ask_ratio']:.0%} of normal · price {f['px_chg6']:+.0%} over 6h\n"
            f"  +{f['gain30']:.0%} day in the last 30d · listed {f['age']:.0f}d · score {f['score']:.0f}\n"
            f"  WATCH ONLY: of the first 4 such cases 1 ran (AIN), 1 rose 21%, 2 went nowhere. "
            f"I'll report it at 24h and 72h. {stats_line()}")
    save_state(st)
    return msgs
