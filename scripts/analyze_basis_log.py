"""Convergence analysis on logged EXECUTABLE touch quotes.

Reads the daily basis logs the engine writes (output/basis_log_YYYYMMDD.csv,
one row per pair per BASIS_LOG_SECONDS) and runs the convergence test on
real quotes instead of candle prints:

- entry signal: entry_bps (perp ask vs spot ask — what a maker-short +
  taker-buy actually locks in) stays >= threshold for --confirm consecutive
  samples; entry executes on the NEXT sample at its entry_bps;
- exit: first subsequent sample whose close_bps (perp bid vs spot bid — what
  a passive close actually achieves) <= exit target, filled at the next
  sample's close_bps;
- net capture per trade = entry_bps - close_bps_at_fill - fees.

This eliminates both biases of the candle backtest: prices are quotes you
could have traded, and the entry/exit sides each carry their own spread.

Usage:
    python scripts/analyze_basis_log.py [output/basis_log_*.csv ...]
                                        [--confirm 3] [--top 30]
                                        [--entry-bps N] [--exit-bps N]
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config


@dataclass
class Sample:
    ts_ms: int
    entry_bps: float
    close_bps: float
    funding_8h_bps: float
    max_notional_usd: float


@dataclass
class Trade:
    entry_ts: int
    entry_bps: float
    exit_ts: int | None
    exit_close_bps: float | None
    minutes: float | None
    net_captured_bps: float | None  # entry - exit close - fees


def load_logs(paths: list[str]) -> dict[str, list[Sample]]:
    series: dict[str, list[Sample]] = defaultdict(list)
    for path in paths:
        opener = (lambda p: gzip.open(p, "rt", newline="")) if str(path).endswith(
            ".gz") else (lambda p: open(p, newline=""))
        with opener(path) as f:
            for row in csv.DictReader(f):
                try:
                    series[row["symbol"]].append(Sample(
                        ts_ms=int(row["ts_ms"]),
                        entry_bps=float(row["entry_bps"]),
                        close_bps=float(row["close_bps"]),
                        funding_8h_bps=float(row["funding_8h_bps"]),
                        max_notional_usd=float(row["max_notional_usd"]),
                    ))
                except (KeyError, ValueError):
                    continue
    for samples in series.values():
        samples.sort(key=lambda s: s.ts_ms)
    return series


def simulate(
    samples: list[Sample], entry_threshold: float, exit_target: float,
    fees_bps: float, confirm: int,
) -> list[Trade]:
    trades: list[Trade] = []
    n = len(samples)
    i = confirm
    while i < n - 1:
        window = samples[i - confirm : i]
        if not all(s.entry_bps >= entry_threshold for s in window):
            i += 1
            continue
        entry = samples[i]
        if entry.entry_bps < entry_threshold:
            i += 1
            continue
        exit_idx = None
        for j in range(i + 1, n):
            if samples[j].close_bps <= exit_target:
                fill = j + 1
                if fill >= n:
                    break
                exit_idx = fill
                break
        if exit_idx is not None:
            fillet = samples[exit_idx]
            minutes = (fillet.ts_ms - entry.ts_ms) / 60_000
            trades.append(Trade(
                entry_ts=entry.ts_ms,
                entry_bps=entry.entry_bps,
                exit_ts=fillet.ts_ms,
                exit_close_bps=fillet.close_bps,
                minutes=minutes,
                net_captured_bps=entry.entry_bps - fillet.close_bps - fees_bps,
            ))
            i = exit_idx + 1
        else:
            trades.append(Trade(
                entry_ts=entry.ts_ms, entry_bps=entry.entry_bps,
                exit_ts=None, exit_close_bps=None,
                minutes=None, net_captured_bps=None,
            ))
            break
    return trades


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * pct / 100
    f = int(k)
    c = min(f + 1, len(values) - 1)
    return values[f] + (k - f) * (values[c] - values[f])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="*", default=None,
                        help="basis log CSVs (default: output/basis_log_*.csv)")
    parser.add_argument("--confirm", type=int, default=3)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--entry-bps", type=float, default=None,
                        help="entry threshold (default: fees + slippage buffer)")
    parser.add_argument("--exit-bps", type=float, default=None,
                        help="exit target on close basis (default: config EXIT_BASIS_BPS)")
    args = parser.parse_args()

    paths = args.logs or sorted(glob.glob(str(config.OUTPUT_DIR / "basis_log_*.csv")))
    if not paths:
        print("no basis logs found — let the engine run first "
              f"(it samples every {config.BASIS_LOG_SECONDS:.0f}s)")
        return

    fees_bps = float((config.ENTRY_FEE + config.EXIT_FEE_PASSIVE) * Decimal(10000))
    entry_threshold = (
        args.entry_bps if args.entry_bps is not None
        else fees_bps + float(config.SLIPPAGE_BUFFER_BPS)
    )
    exit_target = (
        args.exit_bps if args.exit_bps is not None
        else float(config.EXIT_BASIS_BPS)
    )

    series = load_logs(paths)
    print(f"loaded {sum(len(s) for s in series.values())} samples,"
          f" {len(series)} symbols from {len(paths)} file(s)")
    print(f"entry >= {entry_threshold:.1f} bps (confirm {args.confirm}),"
          f" exit close <= {exit_target:.1f} bps, fees {fees_bps:.1f} bps\n")

    results = []
    for sym, samples in series.items():
        if len(samples) < args.confirm + 10:
            continue
        trades = simulate(samples, entry_threshold, exit_target,
                          fees_bps, args.confirm)
        if not trades:
            continue
        wins = [t for t in trades if t.net_captured_bps is not None]
        nets = [t.net_captured_bps for t in wins]
        mins = [t.minutes for t in wins]
        hours = (samples[-1].ts_ms - samples[0].ts_ms) / 3_600_000
        results.append({
            "symbol": sym,
            "hours": hours,
            "trades": len(trades),
            "wins": len(wins),
            "win_pct": 100 * len(wins) / len(trades),
            "med_net": percentile(nets, 50) if nets else None,
            "tot_net": sum(nets) if nets else 0.0,
            "med_min": percentile(mins, 50) if mins else None,
            "p95_min": percentile(mins, 95) if mins else None,
        })

    results.sort(key=lambda r: r["tot_net"], reverse=True)
    hdr = (f"{'symbol':<16}{'hours':>6}{'trades':>7}{'wins':>6}{'win%':>6}"
           f"{'med net':>8}{'tot net':>8}{'med min':>8}{'p95 min':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in results[: args.top]:
        def fmt(v, spec=".1f"):
            return f"{v:{spec}}" if v is not None else "-"
        print(f"{r['symbol']:<16}{r['hours']:>6.1f}{r['trades']:>7}{r['wins']:>6}"
              f"{r['win_pct']:>5.0f}%{fmt(r['med_net']):>8}{r['tot_net']:>8.1f}"
              f"{fmt(r['med_min'], '.0f'):>8}{fmt(r['p95_min'], '.0f'):>8}")
    print("\nnet bps are fee-adjusted and use executable touch quotes:")
    print("entry at perp-ask/spot-ask, exit at perp-bid/spot-bid (passive close).")


if __name__ == "__main__":
    main()
