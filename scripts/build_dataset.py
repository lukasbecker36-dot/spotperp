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
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from backtest_divergence import Series, load_logs, pctile  # noqa: E402

HOUR_MS = 3_600_000
FIVE_MIN_MS = 300_000
FUNDING_STEP_CAP_HOURS = 1.0


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


def features_at(s: Series, hourly: Hourly, i: int, floor: float) -> dict | None:
    """What the screens would have shown at sample i, using only data <= ts[i]."""
    ts = s.ts[i]
    lo_i = bisect.bisect_left(s.ts, ts - FIVE_MIN_MS)
    recent_entry = s.entry[lo_i:i + 1]
    if len(recent_entry) < 3:
        return None                     # too few samples for jitter to mean anything
    ent24, cls24 = hourly.window(ts)
    if len(ent24) < config.SCREEN_DIFF_MIN_HOURS:
        return None                     # no usable 24h norm yet
    entry_avg = _mean(recent_entry)
    close_avg = _mean(s.close[lo_i:i + 1])
    lo_entry, hi_entry = pctile(list(ent24), 10), pctile(list(ent24), 90)
    lo_close, hi_close = pctile(list(cls24), 10), pctile(list(cls24), 90)
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


def outcomes(s: Series, i: int, feat: dict, horizons: list[int], floor: float) -> dict:
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
    hit_zero_ts = hit_target_ts = hit_profit_ts = None
    dwell_after = 0.0
    accrued = 0.0
    ts0 = s.ts[i]
    for k in range(i + 1, j_end):
        # Snapshot every boundary this sample has passed, before folding it in.
        while b < len(bounds) and s.ts[k] > ts0 + bounds[b] * HOUR_MS:
            snaps[bounds[b]] = (min_close, max_close, s.close[k - 1],
                                accrued, funding_at_min, dwell_after)
            b += 1
        c = s.close[k]
        dt_h = min((s.ts[k] - s.ts[k - 1]) / HOUR_MS, FUNDING_STEP_CAP_HOURS)
        accrued += s.funding[k - 1] / 8.0 * dt_h
        if min_close is None or c < min_close:
            min_close, funding_at_min = c, accrued
        if max_close is None or c > max_close:
            max_close = c
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
        snaps[bounds[b]] = (min_close, max_close, s.close[j_end - 1],
                            accrued, funding_at_min, dwell_after)
        b += 1

    def _hours(t, limit_h):
        """Crossing times are monotonic, so one first-crossing serves every
        horizon — it just doesn't count for the ones that ended before it."""
        if not t or (t - ts0) / HOUR_MS > limit_h:
            return ""
        return round((t - ts0) / HOUR_MS, 2)

    out: dict = {}
    for h in horizons:
        mn, mx, end_close, fund, fund_at_min, dwell = snaps[h]
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
            if s.depth[i] < min_depth or s.entry[i] < min_entry:
                continue
            feat = features_at(s, hourly, i, floor)
            if feat is None:
                continue
            last_emit = s.ts[i]
            out = outcomes(s, i, feat, horizons, floor)
            if out:
                rows.append({"symbol": symbol, **feat, **out})
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
    rows = build(
        series, stride_h=args.stride_hours, horizons=horizons,
        floor=args.floor, min_depth=args.min_depth, min_entry=args.min_entry,
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
        else:
            msg.append(
                "There is enough history, so a filter excluded everything:"
                f" --min-depth {args.min_depth:g}, --min-entry"
                f" {args.min_entry:g}, or --symbols."
            )
        print(" ".join(msg), file=sys.stderr)
        return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows):,} rows to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
