"""Turn the engine's basis logs into a labelled dataset: what a candidate
looked like at a moment, and what happened next.

Every screen we have ranks candidates by a formula nobody has scored. This
builds the scoring sheet. For each (symbol, moment) it writes the features the
live screens would have shown — computed from data at or before that moment,
never after — alongside what the basis actually went on to do over the
following hours.

With that you can finally ask:

  * of everything above a given net edge, what fraction actually converged?
  * does jit predict anything, or was that reasoning we never checked?
  * is lo24 the right exit target, or lo24 + a margin?
  * would "enter above X, exit at lo24" have made money over the last months?

and you can score a formula and a model against the SAME rows.

Two honest limits, both about fills rather than prices:

  * the log records where the basis WAS, not whether a resting maker order
    would have been lifted there. Fillability stays a proxy (dwell hours, and
    perp trade flow on logs new enough to carry it).
  * the entry-side and close-side 24h lows are both emitted, because the live
    board ranks on the ENTRY series while an /exit actually fills against the
    CLOSE series. Which one predicts convergence better is a question this
    dataset exists to answer, not one to bake in.

Usage:
    python scripts/build_dataset.py output/basis_log_*.csv -o output/dataset.csv
    python scripts/build_dataset.py --stride-hours 2 --horizons 24,72,168 \
        --min-depth 5 output/basis_log_*.csv
"""
from __future__ import annotations

import argparse
import array
import bisect
import csv
import glob
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from backtest_divergence import Series, load_logs, pctile  # noqa: E402

HOUR_MS = 3_600_000
FUNDING_STEP_CAP_HOURS = 1.0

_DROP_HELP = {
    "window_samples": (
        "The short window needs at least 3 samples. The basis log is written"
        " once a minute and --every thins it further, so try --every 1 or a"
        " larger --window."
    ),
    "window_gap": (
        "The short window kept straddling logging gaps — the engine was down"
        " for stretches. A larger --window-max-hours accepts wider windows."
    ),
    "no_24h_norm": (
        "No candidate had 24h of history behind it. Check the logs are"
        " contiguous rather than a few scattered days."
    ),
    "min_depth": "Every candidate was below --min-depth.",
    "min_entry": "Every candidate's entry basis was below --min-entry.",
    "no_outcome_window": (
        "Features computed fine but no row had the longest horizon of data"
        " behind it — use shorter --horizons."
    ),
}


@dataclass
class Hourly:
    """Per-hour means of both basis series, oldest first.

    Hourly means, not raw samples, for the same reason the live DailyBasis uses
    them: a quote that merely FLICKERS averages out within the hour, so a range
    built from these is a real oscillation rather than noise.
    """
    hour: array.array          # int64, hour index (ts // HOUR_MS)
    entry: array.array         # double, mean entry basis that hour
    close: array.array         # double, mean close basis that hour

    @classmethod
    def build(cls, s: Series) -> "Hourly":
        hours, ent, cls_ = array.array("q"), array.array("d"), array.array("d")
        cur = None
        se = sc = 0.0
        n = 0
        for i in range(len(s)):
            h = s.ts[i] // HOUR_MS
            if cur is None:
                cur = h
            elif h != cur:
                hours.append(cur)
                ent.append(se / n)
                cls_.append(sc / n)
                cur, se, sc, n = h, 0.0, 0.0, 0
            se += s.entry[i]
            sc += s.close[i]
            n += 1
        if cur is not None and n:
            hours.append(cur)
            ent.append(se / n)
            cls_.append(sc / n)
        return cls(hours, ent, cls_)

    def window(self, ts_ms: int, hours_back: int = 24):
        """(entry means, close means) for the whole hours strictly before ts."""
        end = bisect.bisect_left(self.hour, ts_ms // HOUR_MS)
        start = max(0, end - hours_back)
        return self.entry[start:end], self.close[start:end]


def _mean(xs) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _jitter(values) -> float | None:
    """Mean absolute change between CONSECUTIVE samples — not a standard
    deviation. A basis drifting smoothly +10 -> +80 is perfectly tradeable yet
    has a big std; one bouncing +150/-150 each sample is not. Only successive
    change separates them. Same definition as the live screener."""
    if len(values) < 2:
        return None
    return sum(
        abs(values[i] - values[i - 1]) for i in range(1, len(values))
    ) / (len(values) - 1)


def _accrue_funding(s: Series, i: int, j: int) -> float:
    """Funding collected between sample i and j, in bps.

    Per-step accrual with the gap capped at an hour, matching backtest_carry:
    a logging outage must not book a week of funding in one step.
    """
    total = 0.0
    for k in range(i, min(j, len(s) - 1)):
        dt_h = min((s.ts[k + 1] - s.ts[k]) / HOUR_MS, FUNDING_STEP_CAP_HOURS)
        total += s.funding[k] / 8.0 * dt_h
    return total


def features_at(
    s: Series, hourly: Hourly, i: int, floor: float, *,
    window: int, window_max_h: float, baseline_h: int, drops: dict,
) -> dict | None:
    """What the screens would have shown at sample i, using only data <= ts[i].

    The short-window average and jitter are taken over the last `window`
    SAMPLES rather than a fixed five minutes. The live screener samples every
    15s but the basis log is written once a minute, and --every thins it
    further, so a fixed time window silently holds too few points to measure
    jitter at all — which is exactly how a 100-day archive produced no rows.
    Counting samples is robust to whatever cadence the log happens to have.
    """
    ts = s.ts[i]
    lo_i = max(0, i - window + 1)
    recent_entry = s.entry[lo_i:i + 1]
    if len(recent_entry) < 3:
        drops["window_samples"] = drops.get("window_samples", 0) + 1
        return None                     # too few samples for jitter to mean anything
    if (ts - s.ts[lo_i]) / HOUR_MS > window_max_h:
        # The samples exist but straddle a logging gap, so their "5 minute
        # mean" would span hours. Better no row than a fabricated one.
        drops["window_gap"] = drops.get("window_gap", 0) + 1
        return None
    ent24, cls24 = hourly.window(ts)
    if len(ent24) < config.SCREEN_DIFF_MIN_HOURS:
        drops["no_24h_norm"] = drops.get("no_24h_norm", 0) + 1
        return None                     # no usable 24h norm yet
    entry_avg = _mean(recent_entry)
    close_avg = _mean(s.close[lo_i:i + 1])
    lo_entry, hi_entry = pctile(list(ent24), 10), pctile(list(ent24), 90)
    lo_close, hi_close = pctile(list(cls24), 10), pctile(list(cls24), 90)
    # A longer baseline answers a different question: not "is the basis high
    # for this pair today" but "is TODAY unusual". CATE quoted +104 against a
    # 24h low of +120 — the 24h band had gone stale and was describing a
    # regime that already ended. Whether it ranks better than the 24h band is
    # what the report is for; both are emitted so they can be compared.
    entB, clsB = hourly.window(ts, baseline_h)
    lo_entry_b, hi_entry_b = pctile(list(entB), 10), pctile(list(entB), 90)
    lo_close_b, hi_close_b = pctile(list(clsB), 10), pctile(list(clsB), 90)
    return {
        "ts_ms": ts,
        "entry_bps": round(entry_avg, 2),
        "close_bps": round(close_avg, 2),
        "jit_bps": round(_jitter(recent_entry) or 0.0, 2),
        "lo24_entry": round(lo_entry, 2),
        "hi24_entry": round(hi_entry, 2),
        "lo24_close": round(lo_close, 2),
        "hi24_close": round(hi_close, 2),
        # Both nets: the live board ranks on the entry-series low, but an exit
        # fills against the close series. Emitting both is the point.
        "net_vs_entry_lo": round(entry_avg - lo_entry - floor, 2),
        "net_vs_close_lo": round(entry_avg - lo_close - floor, 2),
        f"lo{baseline_h}_entry": round(lo_entry_b, 2),
        f"hi{baseline_h}_entry": round(hi_entry_b, 2),
        f"lo{baseline_h}_close": round(lo_close_b, 2),
        f"hi{baseline_h}_close": round(hi_close_b, 2),
        f"net_vs_close_lo{baseline_h}": round(entry_avg - lo_close_b - floor, 2),
        f"net_vs_entry_lo{baseline_h}": round(entry_avg - lo_entry_b - floor, 2),
        # Where the 24h band sits inside the longer one. Near 0 means the pair
        # has spent today at the cheap end of its recent range; near 100 the
        # rich end; outside [0,100] means the 24h band has left the baseline
        # altogether, which is a regime change rather than a dislocation.
        "band_pos_pct": (
            round((lo_entry - lo_entry_b) / (hi_entry_b - lo_entry_b) * 100, 1)
            if hi_entry_b > lo_entry_b else ""
        ),
        f"hours_history_{baseline_h}": len(entB),
        "dwell_h": sum(1 for m in ent24 if m >= floor),
        "hours_history": len(ent24),
        "funding_8h_bps": round(s.funding[i], 3),
        "funding_24h_avg_8h_bps": round(_accrue_funding(
            s, bisect.bisect_left(s.ts, ts - 24 * HOUR_MS), i
        ) / 24.0 * 8.0, 3),
        "depth_usd": round(s.depth[i], 0),
        # Present only on logs written after the column was added; older days
        # leave it blank rather than pretending to a number.
        "trades_24h": (
            round(s.trades[i], 0)
            if s.trades is not None and i < len(s.trades) else ""
        ),
    }


def outcomes(s: Series, i: int, feat: dict, horizons: list[int], floor: float,
             sustain: int = 5) -> dict:
    """What actually happened after sample i, for EVERY horizon at once.

    One forward walk to the longest horizon, snapshotting as it crosses each
    shorter one. Scanning per horizon instead costs the sum of them — 264h of
    samples per row for the 24/72/168 default — and over 100 days of logs and
    400 symbols that is the difference between minutes and hours.
    """
    longest = max(horizons)
    end_ts = s.ts[i] + longest * HOUR_MS
    j_end = bisect.bisect_right(s.ts, end_ts)
    if j_end <= i + 1:
        return {}
    entry_at = feat["entry_bps"]
    target_close = feat["lo24_close"]
    bounds = sorted(horizons)
    b = 0                               # next horizon boundary to snapshot at
    snaps: dict[int, tuple] = {}
    min_close = max_close = None
    funding_at_min = 0.0
    # The plain minimum over a window is set by its single worst print, and a
    # flickering quote routinely prints 100s of bps away for one sample
    # (RECALLUSDT scored a 7,995bps "best exit" on one). So also track the
    # lowest level the close basis SUSTAINED for `sustain` consecutive samples:
    # the smallest L with close <= L across a whole window, which is the min
    # over sliding windows of each window's max. Outlier-proof by construction.
    dq: deque[int] = deque()
    sustained = None
    funding_at_sustained = 0.0
    hit_zero_ts = hit_target_ts = hit_profit_ts = None
    dwell_after = 0.0
    accrued = 0.0
    ts0 = s.ts[i]
    for k in range(i + 1, j_end):
        # Snapshot every boundary this sample has passed, before folding it in.
        while b < len(bounds) and s.ts[k] > ts0 + bounds[b] * HOUR_MS:
            snaps[bounds[b]] = (min_close, max_close, s.close[k - 1], accrued,
                                funding_at_min, dwell_after, sustained,
                                funding_at_sustained)
            b += 1
        c = s.close[k]
        dt_h = min((s.ts[k] - s.ts[k - 1]) / HOUR_MS, FUNDING_STEP_CAP_HOURS)
        accrued += s.funding[k - 1] / 8.0 * dt_h
        if min_close is None or c < min_close:
            min_close, funding_at_min = c, accrued
        if max_close is None or c > max_close:
            max_close = c
        while dq and s.close[dq[-1]] <= c:
            dq.pop()
        dq.append(k)
        while dq[0] <= k - sustain:
            dq.popleft()
        if k - i >= sustain:            # a full window of samples exists
            window_max = s.close[dq[0]]
            if sustained is None or window_max < sustained:
                sustained, funding_at_sustained = window_max, accrued
        if hit_zero_ts is None and c <= 0:
            hit_zero_ts = s.ts[k]
        if hit_target_ts is None and c <= target_close:
            hit_target_ts = s.ts[k]
        # The label that matters: the first moment closing out actually pays,
        # basis plus funding, after costs. Unlike "reached lo24" it cannot be
        # satisfied trivially — a basis pinned at its own 24h low scores 100%
        # on that target while never being worth trading.
        if hit_profit_ts is None and entry_at - c + accrued - floor >= 0:
            hit_profit_ts = s.ts[k]
        # Time the ENTRY basis stayed workable, as a fill-chance proxy: the log
        # says where the price was, never whether a resting order was lifted.
        if s.entry[k] >= entry_at - 5.0:
            dwell_after += dt_h
    while b < len(bounds):              # horizons the data ran out inside
        snaps[bounds[b]] = (min_close, max_close, s.close[j_end - 1], accrued,
                            funding_at_min, dwell_after, sustained,
                            funding_at_sustained)
        b += 1

    def _hours(t, limit_h):
        """Crossing times are monotonic, so one first-crossing serves every
        horizon — it just doesn't count for the ones that ended before it."""
        if not t or (t - ts0) / HOUR_MS > limit_h:
            return ""
        return round((t - ts0) / HOUR_MS, 2)

    out: dict = {}
    for h in horizons:
        mn, mx, end_close, fund, fund_at_min, dwell, sus, fund_at_sus = snaps[h]
        if mn is None:
            continue
        to_lo24, to_profit = _hours(hit_target_ts, h), _hours(hit_profit_ts, h)
        out.update({
            f"h{h}_min_close": round(mn, 2),
            f"h{h}_max_close": round(mx, 2),
            f"h{h}_end_close": round(end_close, 2),
            f"h{h}_hours_to_zero": _hours(hit_zero_ts, h),
            f"h{h}_hours_to_lo24": to_lo24,
            f"h{h}_reached_lo24": int(to_lo24 != ""),
            f"h{h}_hours_to_profit": to_profit,
            f"h{h}_reached_profit": int(to_profit != ""),
            f"h{h}_funding_bps": round(fund, 2),
            # What the round trip was worth if you had exited at the best
            # moment the horizon offered — an upper bound, nobody times that.
            f"h{h}_best_pnl_bps": round(entry_at - mn + fund_at_min - floor, 2),
            # The same trip priced at a level that actually held, rather than
            # at one print. This is the column to reason from.
            f"h{h}_sustained_pnl_bps": (
                round(entry_at - sus + fund_at_sus - floor, 2)
                if sus is not None else ""
            ),
            # ...and what simply holding the whole horizon would have paid.
            f"h{h}_hold_pnl_bps": round(entry_at - end_close + fund - floor, 2),
            f"h{h}_dwell_after_h": round(dwell, 2),
        })
    return out


def log_span_hours(series: dict[str, Series]) -> float:
    """Hours between the first and last sample anywhere in the logs."""
    spans = [(s.ts[-1] - s.ts[0]) / HOUR_MS for s in series.values() if len(s) > 1]
    return max(spans) if spans else 0.0


def build(
    series: dict[str, Series], *, stride_h: float, horizons: list[int],
    floor: float, min_depth: float, min_entry: float,
    window: int, window_max_h: float, sustain: int, baseline_h: int,
    drops: dict,
) -> list[dict]:
    rows: list[dict] = []
    done = 0
    stride_ms = int(stride_h * HOUR_MS)
    longest = max(horizons)
    for symbol, s in sorted(series.items()):
        if len(s) < 100:
            continue
        hourly = Hourly.build(s)
        last_emit = 0
        # Stop early enough that the LONGEST horizon still has data behind it;
        # a row whose outcome window runs past the end of the log would look
        # like a trade that never converged, which is a label, not a fact.
        cutoff = s.ts[-1] - longest * HOUR_MS
        for i in range(len(s)):
            if s.ts[i] > cutoff:
                break
            if s.ts[i] - last_emit < stride_ms:
                continue
            if s.depth[i] < min_depth:
                drops["min_depth"] = drops.get("min_depth", 0) + 1
                continue
            if s.entry[i] < min_entry:
                drops["min_entry"] = drops.get("min_entry", 0) + 1
                continue
            feat = features_at(s, hourly, i, floor, window=window,
                               window_max_h=window_max_h,
                               baseline_h=baseline_h, drops=drops)
            if feat is None:
                continue
            last_emit = s.ts[i]
            out = outcomes(s, i, feat, horizons, floor, sustain)
            if out:
                rows.append({"symbol": symbol, **feat, **out})
            else:
                drops["no_outcome_window"] = drops.get("no_outcome_window", 0) + 1
        done += 1
        if done % 25 == 0:
            print(f"  {done}/{len(series)} symbols, {len(rows):,} rows",
                  file=sys.stderr, flush=True)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+",
                    help="output/basis_log_*.csv[.gz]; globs are expanded here"
                         " too, so a quoted pattern works")
    ap.add_argument("-o", "--out", default="output/dataset.csv")
    ap.add_argument("--stride-hours", type=float, default=1.0,
                    help="how often to emit a candidate row per symbol (default 1)")
    ap.add_argument("--horizons", default="24,72,168",
                    help="outcome windows in hours (default 24,72,168)")
    ap.add_argument("--floor", type=float,
                    default=float(config.ENTRY_MIN_EDGE_FLOOR_BPS),
                    help="round-trip cost floor in bps")
    ap.add_argument("--min-depth", type=float, default=config.SCREEN_MIN_DEPTH_USD)
    ap.add_argument("--min-entry", type=float, default=0.0,
                    help="only emit rows whose entry basis is at least this")
    ap.add_argument("--every", type=int, default=1,
                    help="downsample the log 1-in-N while loading (memory)")
    ap.add_argument("--symbols", default="", help="comma-separated filter")
    ap.add_argument("--window", type=int, default=5,
                    help="samples in the short-window mean/jitter (default 5)."
                         " Counted in SAMPLES, not minutes, so it survives"
                         " whatever cadence the log has and any --every")
    ap.add_argument("--baseline-hours", type=int, default=72,
                    help="longer baseline window alongside the 24h one"
                         " (default 72). Emits lo/hi on both series and"
                         " net_vs_close_lo<N>, so the report can compare"
                         " whether it ranks outcomes better than 24h")
    ap.add_argument("--sustain", type=int, default=5,
                    help="consecutive samples a close level must hold to count"
                         " as reachable (default 5). Guards the best-exit"
                         " figure against a single flickering print")
    ap.add_argument("--window-max-hours", type=float, default=1.0,
                    help="skip a row whose window straddles a logging gap"
                         " wider than this (default 1h)")
    args = ap.parse_args()

    # Expand globs ourselves as well as letting the shell do it: quoting the
    # pattern is a natural instinct, and an unexpanded one otherwise reaches
    # open() as a literal filename and fails with a confusing ENOENT.
    paths: list[str] = []
    for pattern in args.logs:
        hits = sorted(glob.glob(pattern))
        if hits:
            paths.extend(hits)
        elif Path(pattern).exists():
            paths.append(pattern)
        else:
            print(f"no files match {pattern!r}", file=sys.stderr)
    if not paths:
        return
    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    filt = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} or None
    series = load_logs(paths, filt, every=args.every)
    span = log_span_hours(series)
    print(f"loaded {len(series)} symbols spanning {span:.1f}h", file=sys.stderr)
    drops: dict[str, int] = {}
    rows = build(
        series, stride_h=args.stride_hours, horizons=horizons,
        floor=args.floor, min_depth=args.min_depth, min_entry=args.min_entry,
        window=args.window, window_max_h=args.window_max_hours,
        sustain=args.sustain, baseline_h=args.baseline_hours, drops=drops,
    )
    if not rows:
        # Say WHICH constraint bit. "No rows" with a 168h horizon over a 5h log
        # is a glob that missed the gzipped days, not a broken build.
        longest = max(horizons)
        # A row needs 24h of history in front of it and the longest horizon
        # behind it, so the log has to span more than the two combined.
        need = longest + 24
        msg = ["no rows written."]
        if span < need:
            msg.append(
                f"The logs span {span:.1f}h but a {longest}h horizon needs"
                f" {need:.0f}h (24h of history before each row, then the"
                f" horizon after it)."
            )
            msg.append(
                "If older days are gzipped the glob missed them — basis_log_*"
                ".csv does not match .csv.gz. Try output/basis_log_*.csv* ."
            )
            fits = [h for h in (6, 12, 24, 48, 72) if h + 24 < span]
            if fits:
                msg.append(
                    f"With this much data, try --horizons {','.join(map(str, fits))}."
                )
        elif drops:
            # Report what actually rejected the candidates rather than listing
            # the filters and leaving you to guess which one bit.
            why = ", ".join(
                f"{k}={v:,}" for k, v in sorted(drops.items(), key=lambda kv: -kv[1])
            )
            msg.append(f"Every candidate was dropped: {why}.")
            top = max(drops, key=drops.get)
            msg.append(_DROP_HELP.get(top, ""))
        print(" ".join(m for m in msg if m), file=sys.stderr)
        return
    if drops:
        print("  dropped: " + ", ".join(
            f"{k}={v:,}" for k, v in sorted(drops.items(), key=lambda kv: -kv[1])
        ), file=sys.stderr)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows):,} rows to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
