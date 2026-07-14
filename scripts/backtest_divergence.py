"""Backtest short-term divergence capture from the engine's OWN basis logs,
comparing two execution models:

  maker-taker (the live engine's model):
      enter  = rest GTX at the Aster ask + taker-buy spot at the MEXC ask
               -> executable edge  = entry_bps (ask/ask, as logged)
      exit   = rest GTX at the Aster bid + taker-sell spot at the MEXC bid
               -> executable close = close_bps (bid/bid, as logged)
      fees   = 2 x MEXC taker (Aster maker is 0)
      CAVEAT: assumes the maker fills at the touch the moment the signal
      appears — real resting orders fill adverse-selected, so treat these
      results as an UPPER bound.

  taker-taker (cross both books, in and out):
      enter  = sell perp at the Aster BID + buy spot at the MEXC ASK
      exit   = buy perp at the Aster ASK + sell spot at the MEXC BID
      fees   = 2 x (Aster taker + MEXC taker)
      The log stores entry_bps (ask/ask) and close_bps (bid/bid) but not the
      per-venue spreads, so the taker measures are reconstructed to first
      order from the total spread s = entry - close with a configurable
      Aster/MEXC split (--mexc-spread-frac, default 0.5):
          taker_entry(t) = close(t) - f * s(t)      (f = MEXC share)
          taker_exit(t)  = entry(t) + f * s(t)
      f=0 is the optimistic bound (all spread on Aster), f=1 pessimistic.

Simulation rules (both models, no look-ahead):
  - signal: the model's executable ENTRY edge >= threshold for --confirm
    consecutive samples (gaps > 3x the 60s cadence reset the streak), with
    top-of-book depth >= --min-depth at signal time;
  - the trade executes on the NEXT sample at that sample's prices;
  - exit: the model's executable EXIT measure <= --exit-bps, executing on
    the next sample; or --max-hold-hours elapses (exit at prevailing);
    trades still open when the data ends are counted but excluded from P&L;
  - funding accrues from the logged funding_8h_bps integrated over the hold
    (short perp receives positive funding); per-step gaps capped at 1h;
  - one open trade per symbol, sequential re-entry allowed.

Usage (on the server, no network needed):
    python scripts/backtest_divergence.py output/basis_log_*.csv
    python scripts/backtest_divergence.py --thresholds 50,100,150 \
        --min-depth 500 --symbols PLAY,BTW output/basis_log_*.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import array  # noqa: E402
import config  # noqa: E402  (fees / output dir defaults)

BPS = 10000.0
SAMPLE_GAP_RESET_MS = 3 * int(config.BASIS_LOG_SECONDS) * 1000
FUNDING_STEP_CAP_HOURS = 1.0


class Series:
    """Per-symbol time series as packed columnar arrays. Millions of rows fit
    in ~40 bytes each (5 x 8-byte columns) vs ~250 for a Python object — the
    difference between fitting in the 4GB box and OOM-thrashing on 11M rows."""

    __slots__ = ("ts", "entry", "close", "funding", "depth")

    def __init__(self):
        self.ts = array.array("q")       # int64 ms
        self.entry = array.array("d")    # ask/ask  (maker perp + taker spot buy)
        self.close = array.array("d")    # bid/bid  (maker perp + taker spot sell)
        self.funding = array.array("d")  # funding_8h_bps
        self.depth = array.array("d")    # top-of-book USD

    def __len__(self):
        return len(self.ts)

    def append(self, ts, e, c, f, d):
        self.ts.append(ts)
        self.entry.append(e)
        self.close.append(c)
        self.funding.append(f)
        self.depth.append(d)


@dataclass
class Trade:
    symbol: str
    entry_ts: int
    exit_ts: int | None
    entry_edge: float       # model-executable edge at entry
    exit_edge: float | None  # model-executable measure paid at exit
    funding_bps: float
    fees_bps: float
    depth_usd: float
    outcome: str            # converged | timeout | unresolved

    @property
    def gross_bps(self) -> float | None:
        if self.exit_edge is None:
            return None
        return self.entry_edge - self.exit_edge

    @property
    def net_bps(self) -> float | None:
        g = self.gross_bps
        if g is None:
            return None
        return g + self.funding_bps - self.fees_bps

    @property
    def hold_minutes(self) -> float | None:
        if self.exit_ts is None:
            return None
        return (self.exit_ts - self.entry_ts) / 60_000


class Model:
    """Maps logged (entry_bps, close_bps) to a model's executable measures."""

    def __init__(self, name: str, fees_bps: float, mexc_frac: float | None):
        self.name = name
        self.fees_bps = fees_bps
        self.mexc_frac = mexc_frac  # None = maker-taker (uses logged directly)

    def entry_edge(self, entry: float, close: float) -> float:
        if self.mexc_frac is None:
            return entry
        return close - self.mexc_frac * max(0.0, entry - close)

    def exit_measure(self, entry: float, close: float) -> float:
        if self.mexc_frac is None:
            return close
        return entry + self.mexc_frac * max(0.0, entry - close)


def load_logs(
    paths: list[str], symbol_filter: set[str] | None = None, every: int = 1,
) -> dict[str, Series]:
    by_symbol: dict[str, Series] = {}
    seen: dict[str, int] = {}   # per-symbol row counter for downsampling
    total_rows = 0
    for pi, p in enumerate(sorted(paths), 1):
        rows_here = 0
        # Fixed column order (positional) avoids DictReader's per-row dict build,
        # which is the bottleneck on multi-million-row logs. Header decides layout.
        with open(p, newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                continue
            idx = {name: k for k, name in enumerate(header)}
            try:
                ts_i, sym_i, e_i, c_i, fnd_i, dep_i = (
                    idx["ts_ms"], idx["symbol"], idx["entry_bps"],
                    idx["close_bps"], idx["funding_8h_bps"], idx["max_notional_usd"],
                )
            except KeyError:
                continue
            for row in reader:
                try:
                    sym = row[sym_i]
                    if symbol_filter is not None and sym not in symbol_filter:
                        continue
                    if every > 1:
                        k = seen.get(sym, 0)
                        seen[sym] = k + 1
                        if k % every != 0:
                            continue
                    s = by_symbol.get(sym)
                    if s is None:
                        s = by_symbol[sym] = Series()
                    s.append(int(row[ts_i]), float(row[e_i]), float(row[c_i]),
                             float(row[fnd_i]), float(row[dep_i]))
                    rows_here += 1
                except (IndexError, ValueError):
                    continue  # malformed line (partial write) — skip
        total_rows += rows_here
        print(f"  [{pi}/{len(paths)}] {Path(p).name}: {rows_here:,} rows"
              f" ({total_rows:,} total)", file=sys.stderr, flush=True)
    # No sort: the engine writes ticks chronologically and each symbol appears
    # once per tick, so appending across name-sorted (=chronological) daily
    # files yields per-symbol series already in ts order. The gap logic below
    # tolerates the occasional out-of-order row without corrupting results.
    return by_symbol


def simulate(
    symbol: str, series: Series, model: Model, *,
    threshold: float, exit_bps: float, confirm: int,
    min_depth: float, max_hold_hours: float,
    gap_reset_ms: int = SAMPLE_GAP_RESET_MS,
) -> list[Trade]:
    trades: list[Trade] = []
    ts, ent, cls, fnd, dep = (
        series.ts, series.entry, series.close, series.funding, series.depth,
    )
    n = len(ts)
    streak = 0
    i = 0
    while i < n - 1:
        if i > 0 and ts[i] - ts[i - 1] > gap_reset_ms:
            streak = 0
        if model.entry_edge(ent[i], cls[i]) >= threshold and dep[i] >= min_depth:
            streak += 1
        else:
            streak = 0
        if streak < confirm:
            i += 1
            continue
        # Confirmed: execute on the NEXT sample at its prices (no look-ahead).
        entry_i = i + 1
        streak = 0
        entry_edge = model.entry_edge(ent[entry_i], cls[entry_i])
        if entry_edge < threshold:
            i = entry_i        # signal decayed before we could trade — miss
            continue
        funding = 0.0
        exit_i = None
        outcome = "unresolved"
        deadline = ts[entry_i] + max_hold_hours * 3_600_000
        j = entry_i
        while j + 1 < n:
            j += 1
            step_h = min((ts[j] - ts[j - 1]) / 3_600_000, FUNDING_STEP_CAP_HOURS)
            funding += fnd[j] / 8.0 * step_h
            if ts[j] > deadline:
                exit_i, outcome = j, "timeout"
                break
            if model.exit_measure(ent[j], cls[j]) <= exit_bps:
                # Exit executes on the next sample after the signal print.
                if j + 1 < n:
                    exit_i, outcome = j + 1, "converged"
                break
        trades.append(Trade(
            symbol=symbol,
            entry_ts=ts[entry_i],
            exit_ts=ts[exit_i] if exit_i is not None else None,
            entry_edge=entry_edge,
            exit_edge=model.exit_measure(ent[exit_i], cls[exit_i]) if exit_i is not None else None,
            funding_bps=funding,
            fees_bps=model.fees_bps,
            depth_usd=dep[entry_i],
            outcome=outcome,
        ))
        i = (exit_i if exit_i is not None else n) + 1
    return trades


def pctile(vals: list[float], pct: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = (len(vals) - 1) * pct / 100
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    return vals[f] + (k - f) * (vals[c] - vals[f])


def summarize(trades: list[Trade]) -> dict:
    resolved = [t for t in trades if t.net_bps is not None]
    nets = [t.net_bps for t in resolved]
    holds = [t.hold_minutes for t in resolved]
    return {
        "trades": len(trades),
        "resolved": len(resolved),
        "timeouts": sum(1 for t in trades if t.outcome == "timeout"),
        "unresolved": sum(1 for t in trades if t.outcome == "unresolved"),
        "win_rate": (sum(1 for x in nets if x > 0) / len(nets)) if nets else 0.0,
        "mean_net": (sum(nets) / len(nets)) if nets else 0.0,
        "med_net": pctile(nets, 50),
        "p25_net": pctile(nets, 25),
        "p75_net": pctile(nets, 75),
        "total_net": sum(nets),
        "med_hold_min": pctile(holds, 50),
        "med_depth": pctile([t.depth_usd for t in resolved], 50),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Backtest maker-taker vs taker-taker divergence capture"
                    " from output/basis_log_*.csv")
    ap.add_argument("logs", nargs="+", help="basis_log CSV file(s)")
    ap.add_argument("--thresholds", default="30,50,100,150",
                    help="entry-edge thresholds in bps (comma-separated)")
    ap.add_argument("--exit-bps", type=float, default=0.0,
                    help="exit when the model's exit measure <= this (default 0)")
    ap.add_argument("--confirm", type=int, default=3,
                    help="consecutive samples >= threshold before entry (default 3)")
    ap.add_argument("--min-depth", type=float,
                    default=float(config.MIN_DEPTH_NOTIONAL_USD),
                    help="min top-of-book depth USD at signal (default from config)")
    ap.add_argument("--max-hold-hours", type=float, default=48.0)
    ap.add_argument("--mexc-spread-frac", type=float, default=0.5,
                    help="share of the total spread attributed to MEXC for the"
                         " taker-taker reconstruction (0=optimistic, 1=pessimistic)")
    ap.add_argument("--slippage-bps", type=float, default=4.0,
                    help="extra cost per round trip beyond the touch (default 4)")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated filter, e.g. PLAY,BTW")
    ap.add_argument("--per-symbol-threshold", type=float, default=None,
                    help="also print a per-symbol table at this threshold")
    ap.add_argument("--every", type=int, default=1,
                    help="keep only 1-in-N samples per symbol (downsample) to cut"
                         " memory/time on huge logs; e.g. 5 = ~5min resolution")
    args = ap.parse_args()
    every = max(1, args.every)
    gap_reset_ms = 3 * every * int(config.BASIS_LOG_SECONDS) * 1000

    want = None
    if args.symbols:
        want = {
            s.strip().upper() + ("" if s.strip().upper().endswith("USDT") else "USDT")
            for s in args.symbols.split(",")
        }
    print(f"loading {len(args.logs)} log file(s)"
          f"{f' for {sorted(want)}' if want else ''}...", file=sys.stderr, flush=True)
    data = load_logs(args.logs, symbol_filter=want, every=every)
    if not data:
        print("no data — check the log paths / symbol filter")
        return
    n_samples = sum(len(v) for v in data.values())
    span_h = (
        max(v.ts[-1] for v in data.values() if len(v))
        - min(v.ts[0] for v in data.values() if len(v))
    ) / 3_600_000

    mexc_taker = float(config.MEXC_TAKER_FEE * 10000)
    aster_taker = float(config.ASTER_TAKER_FEE * 10000)
    models = [
        Model("maker-taker", 2 * mexc_taker + args.slippage_bps, None),
        Model("taker-taker", 2 * (aster_taker + mexc_taker) + args.slippage_bps,
              args.mexc_spread_frac),
    ]
    thresholds = [float(x) for x in args.thresholds.split(",")]

    lines = []
    lines.append(f"divergence backtest — {len(data)} symbols, {n_samples:,} samples,"
                 f" ~{span_h:.1f}h span")
    lines.append(f"exit<= {args.exit_bps}bps | confirm {args.confirm} samples |"
                 f" depth>= ${args.min_depth:,.0f} | max hold {args.max_hold_hours:.0f}h |"
                 f" slippage {args.slippage_bps}bps/round-trip |"
                 f" taker-taker MEXC spread share {args.mexc_spread_frac}")
    lines.append("NOTE maker-taker assumes touch fills for the maker leg = UPPER bound"
                 " (real resting orders fill adverse-selected).")
    hdr = (f"{'model':<12}{'thr':>5}{'trades':>7}{'res':>5}{'t/o':>5}{'win%':>6}"
           f"{'medNet':>8}{'meanNet':>8}{'p25':>7}{'p75':>7}{'total':>9}"
           f"{'medHold':>8}{'medDep$':>8}")
    lines.append(hdr)
    lines.append("-" * len(hdr))

    per_symbol_rows: list[tuple] = []
    for model in models:
        for thr in thresholds:
            all_trades: list[Trade] = []
            for sym, series in data.items():
                t = simulate(
                    sym, series, model, threshold=thr, exit_bps=args.exit_bps,
                    confirm=args.confirm, min_depth=args.min_depth,
                    max_hold_hours=args.max_hold_hours, gap_reset_ms=gap_reset_ms,
                )
                all_trades.extend(t)
                if args.per_symbol_threshold == thr:
                    r = summarize(t)
                    if r["trades"]:
                        per_symbol_rows.append((model.name, sym, r))
            r = summarize(all_trades)
            lines.append(
                f"{model.name:<12}{thr:>5.0f}{r['trades']:>7}{r['resolved']:>5}"
                f"{r['timeouts']:>5}{r['win_rate'] * 100:>5.0f}%"
                f"{r['med_net']:>8.1f}{r['mean_net']:>8.1f}{r['p25_net']:>7.1f}"
                f"{r['p75_net']:>7.1f}{r['total_net']:>9.1f}"
                f"{r['med_hold_min']:>8.0f}{r['med_depth']:>8.0f}"
            )
        lines.append("-" * len(hdr))

    if per_symbol_rows:
        lines.append("")
        lines.append(f"per-symbol at {args.per_symbol_threshold:.0f}bps"
                     f" (sorted by total net):")
        per_symbol_rows.sort(key=lambda x: x[2]["total_net"], reverse=True)
        for name, sym, r in per_symbol_rows[:40]:
            lines.append(
                f"  {name:<12}{sym:<16}trades={r['trades']:<4}"
                f" win={r['win_rate'] * 100:>3.0f}% medNet={r['med_net']:>7.1f}"
                f" total={r['total_net']:>8.1f} medHold={r['med_hold_min']:>5.0f}m"
                f" dep=${r['med_depth']:,.0f}"
            )

    lines.append("")
    lines.append("bps figures are per-unit-notional; $P&L ~= net_bps/1e4 x size,"
                 " capped by depth. timeouts exit at the prevailing basis;"
                 " unresolved trades are excluded from P&L.")
    lines.append("READ WITH CARE: maker-taker rows with big nets on SHORT median"
                 " holds are usually the spread mirage (the model books the full"
                 " bid-ask as profit; live maker fills are adverse-selected)."
                 " Trust names where maker-taker and taker-taker BOTH clear zero.")
    report = "\n".join(lines)
    print(report)
    out = config.OUTPUT_DIR / "divergence_backtest.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(f"\nreport written to {out}")


if __name__ == "__main__":
    main()
