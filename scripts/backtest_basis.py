"""Backtest basis convergence using 1m candles over the past 48 hours.

For every cross-listed pair, fetches 1m Aster perp and MEXC spot candles,
computes the per-minute basis (close-to-close), then simulates entering a
trade whenever the basis exceeds the breakeven threshold (round-trip fees)
and measures:
  - how many times a trade would have been entered
  - median / mean / p75 / p95 / max time to converge below breakeven
  - win rate (converged before the 48h window ends)
  - median / mean basis captured (entry - exit)

Usage:
    python scripts/backtest_basis.py [--symbols BTC,ETH] [--hours 48] [--top N]

Requires network access to Aster and MEXC APIs. No API keys needed (public
klines endpoints). Writes a summary to output/basis_backtest.txt and prints
to stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import aiohttp

# Add project root to path so we can import our modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from exchange_client import AsterClient, MexcClient
from screener import ASTER_TO_MEXC_ALIASES, build_pair_maps, PairMap

log = logging.getLogger("backtest_basis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HOUR_MS = 3_600_000
MINUTE_MS = 60_000


@dataclass
class Candle:
    open_time: int   # ms
    open: float
    high: float
    low: float
    close: float
    volume: float


def parse_candles(raw: list[list]) -> list[Candle]:
    out = []
    for r in raw:
        out.append(Candle(
            open_time=int(r[0]),
            open=float(r[1]),
            high=float(r[2]),
            low=float(r[3]),
            close=float(r[4]),
            volume=float(r[5]),
        ))
    out.sort(key=lambda c: c.open_time)
    return out


async def fetch_klines_paginated(
    client, symbol: str, start_ms: int, end_ms: int, interval: str = "1m"
) -> list[Candle]:
    """Paginate through klines in chunks (Aster max 1500, MEXC max 1000)."""
    all_candles: list[Candle] = []
    cursor = start_ms
    limit = 1500 if isinstance(client, AsterClient) else 1000
    while cursor < end_ms:
        try:
            raw = await client.klines(
                symbol, interval, start_ms=cursor, end_ms=end_ms, limit=limit,
            )
        except Exception as exc:
            log.warning("kline fetch %s from %d failed: %s", symbol, cursor, exc)
            break
        if not raw:
            break
        batch = parse_candles(raw)
        all_candles.extend(batch)
        last_time = batch[-1].open_time
        if last_time <= cursor:
            break
        cursor = last_time + MINUTE_MS
        await asyncio.sleep(0.1)
    return all_candles


def align_candles(
    perp: list[Candle], spot: list[Candle]
) -> list[tuple[Candle, Candle]]:
    """Inner-join on open_time, returning aligned (perp, spot) pairs."""
    spot_by_time = {c.open_time: c for c in spot}
    return [(p, spot_by_time[p.open_time]) for p in perp if p.open_time in spot_by_time]


@dataclass
class Trade:
    entry_minute: int       # index into the aligned series
    entry_basis_bps: float  # basis at entry
    exit_minute: int | None # index when basis first dropped below breakeven
    exit_basis_bps: float | None
    converge_minutes: int | None  # exit_minute - entry_minute
    basis_captured_bps: float | None  # entry - exit basis


@dataclass
class SymbolResult:
    symbol: str
    aster_symbol: str
    mexc_symbol: str
    candles: int
    hours_covered: float
    breakeven_bps: float
    trades: int
    wins: int
    win_rate: float
    median_converge_min: float | None
    mean_converge_min: float | None
    p75_converge_min: float | None
    p95_converge_min: float | None
    max_converge_min: float | None
    median_captured_bps: float | None
    mean_captured_bps: float | None
    mean_basis_bps: float
    max_basis_bps: float
    pct_above_breakeven: float


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * pct / 100
    f = int(k)
    c = min(f + 1, len(values) - 1)
    d = k - f
    return values[f] + d * (values[c] - values[f])


def run_backtest(
    aligned: list[tuple[Candle, Candle]],
    pair: PairMap,
    breakeven_bps: float,
    confirm_minutes: int = 3,
) -> tuple[list[float], list[Trade]]:
    """Compute basis series and simulate trades entering above breakeven.

    Anti-noise rules (1m closes are last-trade prints; on thin names they
    bounce between bid and ask, manufacturing phantom convergence):
    - entry requires the basis to hold above breakeven for ``confirm_minutes``
      consecutive bars, and executes on the NEXT bar at that bar's basis;
    - exit signals when the basis prints below breakeven, but executes on the
      next bar at that bar's basis (you can't trade the signal print).
    """
    mult = float(pair.qty_multiplier)
    basis_series: list[float] = []
    for perp_c, spot_c in aligned:
        if spot_c.close <= 0:
            basis_series.append(0.0)
            continue
        perp_price = perp_c.close / mult
        basis = (perp_price - spot_c.close) / spot_c.close * 10000
        basis_series.append(basis)

    trades: list[Trade] = []
    n = len(basis_series)
    i = confirm_minutes
    while i < n - 1:
        window = basis_series[i - confirm_minutes : i]
        if not all(b >= breakeven_bps for b in window):
            i += 1
            continue
        # confirmed: enter at bar i (the bar after the confirmation window)
        entry_idx = i
        entry_bps = basis_series[entry_idx]
        if entry_bps < breakeven_bps:
            # signal decayed before we could trade it — realistic miss
            i += 1
            continue
        exit_idx = None
        exit_bps = None
        for j in range(entry_idx + 1, n):
            if basis_series[j] <= breakeven_bps:
                # exit executes on the next bar after the signal print
                fill = min(j + 1, n - 1)
                if fill == j:  # signal on the last bar: no bar left to fill
                    break
                exit_idx = fill
                exit_bps = basis_series[fill]
                break
        if exit_idx is not None:
            trades.append(Trade(
                entry_minute=entry_idx,
                entry_basis_bps=entry_bps,
                exit_minute=exit_idx,
                exit_basis_bps=exit_bps,
                converge_minutes=exit_idx - entry_idx,
                basis_captured_bps=entry_bps - exit_bps,
            ))
            i = exit_idx + 1
        else:
            trades.append(Trade(
                entry_minute=entry_idx,
                entry_basis_bps=entry_bps,
                exit_minute=None,
                exit_basis_bps=None,
                converge_minutes=None,
                basis_captured_bps=None,
            ))
            # No more entries once stuck in a non-converging trade
            break

    return basis_series, trades


def summarize_symbol(
    symbol: str, pair: PairMap,
    aligned: list[tuple[Candle, Candle]],
    basis_series: list[float],
    trades: list[Trade],
    breakeven_bps: float,
) -> SymbolResult:
    wins = [t for t in trades if t.converge_minutes is not None]
    converge_times = [t.converge_minutes for t in wins]
    captured = [t.basis_captured_bps for t in wins if t.basis_captured_bps is not None]
    above = sum(1 for b in basis_series if b >= breakeven_bps)
    hours = len(aligned) / 60 if aligned else 0

    return SymbolResult(
        symbol=symbol,
        aster_symbol=pair.aster_symbol,
        mexc_symbol=pair.mexc_symbol,
        candles=len(aligned),
        hours_covered=hours,
        breakeven_bps=breakeven_bps,
        trades=len(trades),
        wins=len(wins),
        win_rate=len(wins) / len(trades) if trades else 0,
        median_converge_min=percentile(converge_times, 50) if converge_times else None,
        mean_converge_min=sum(converge_times) / len(converge_times) if converge_times else None,
        p75_converge_min=percentile(converge_times, 75) if converge_times else None,
        p95_converge_min=percentile(converge_times, 95) if converge_times else None,
        max_converge_min=max(converge_times) if converge_times else None,
        median_captured_bps=percentile(captured, 50) if captured else None,
        mean_captured_bps=sum(captured) / len(captured) if captured else None,
        mean_basis_bps=sum(basis_series) / len(basis_series) if basis_series else 0,
        max_basis_bps=max(basis_series) if basis_series else 0,
        pct_above_breakeven=above / len(basis_series) * 100 if basis_series else 0,
    )


def format_results(results: list[SymbolResult]) -> str:
    # Sort by number of winning trades descending
    results.sort(key=lambda r: (r.wins, r.mean_basis_bps), reverse=True)

    lines = []
    lines.append("=" * 90)
    lines.append("BASIS CONVERGENCE BACKTEST — 1m candles")
    lines.append("=" * 90)
    lines.append("")

    hdr = (
        f"{'symbol':<16}{'candles':>7}{'BE bps':>7}{'trades':>7}{'wins':>6}"
        f"{'win%':>6}{'med min':>8}{'avg min':>8}{'p95 min':>8}"
        f"{'med cap':>8}{'avg bps':>8}{'%above':>7}"
    )
    lines.append(hdr)
    lines.append("-" * len(hdr))

    for r in results:
        def f(v, fmt=".0f"):
            return f"{v:{fmt}}" if v is not None else "-"
        lines.append(
            f"{r.symbol:<16}{r.candles:>7}{r.breakeven_bps:>7.1f}{r.trades:>7}"
            f"{r.wins:>6}{r.win_rate * 100:>5.0f}%"
            f"{f(r.median_converge_min):>8}{f(r.mean_converge_min):>8}"
            f"{f(r.p95_converge_min):>8}{f(r.median_captured_bps, '.1f'):>8}"
            f"{r.mean_basis_bps:>8.1f}{r.pct_above_breakeven:>6.1f}%"
        )

    lines.append("")
    lines.append("Legend:")
    lines.append("  BE bps    = breakeven basis (entry+exit fees)")
    lines.append("  trades    = entries above breakeven (sequential, no overlap)")
    lines.append("  wins      = converged below breakeven within the window")
    lines.append("  med/avg/p95 min = convergence time in minutes")
    lines.append("  med cap   = median basis captured (entry - exit) in bps")
    lines.append("  avg bps   = mean basis over all candles")
    lines.append("  %above    = % of candles where basis > breakeven")
    lines.append("")
    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description="Backtest basis convergence")
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated symbols (e.g. BTC,ETH). Default: all cross-listed")
    parser.add_argument("--hours", type=int, default=48,
                        help="Lookback period in hours (default: 48)")
    parser.add_argument("--top", type=int, default=30,
                        help="Show top N results (default: 30)")
    parser.add_argument("--breakeven-override", type=float, default=None,
                        help="Override breakeven bps (default: computed from fees)")
    parser.add_argument("--confirm", type=int, default=3,
                        help="Consecutive minutes above breakeven required before"
                             " entry; entry/exit execute on the next bar (default: 3)")
    args = parser.parse_args()

    # Compute breakeven: maker entry + passive exit (maker + taker spot both legs)
    breakeven_bps = float(
        (config.ENTRY_FEE + config.EXIT_FEE_PASSIVE) * Decimal(10000)
        + config.SLIPPAGE_BUFFER_BPS
    )
    if args.breakeven_override is not None:
        breakeven_bps = args.breakeven_override
    log.info("breakeven threshold: %.1f bps", breakeven_bps)

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.hours * HOUR_MS

    async with aiohttp.ClientSession() as session:
        aster = AsterClient(session, None)
        mexc = MexcClient(session, None)

        # Build pair maps
        log.info("fetching exchange info...")
        aster_info, mexc_info = await asyncio.gather(
            aster.exchange_info(), mexc.exchange_info()
        )
        pairs = build_pair_maps(set(aster_info), set(mexc_info))
        log.info("%d cross-listed USDT pairs", len(pairs))

        # Filter to requested symbols
        if args.symbols:
            requested = {
                s.strip().upper() + ("USDT" if not s.strip().upper().endswith("USDT") else "")
                for s in args.symbols.split(",")
            }
            pairs = {k: v for k, v in pairs.items() if k in requested}
            if not pairs:
                log.error("none of %s are cross-listed", args.symbols)
                return
            log.info("filtered to %d symbols: %s", len(pairs), list(pairs.keys()))

        results: list[SymbolResult] = []
        total = len(pairs)

        for idx, (sym, pair) in enumerate(sorted(pairs.items()), 1):
            log.info("[%d/%d] %s (aster=%s, mexc=%s)...",
                     idx, total, sym, pair.aster_symbol, pair.mexc_symbol)
            try:
                perp_raw, spot_raw = await asyncio.gather(
                    fetch_klines_paginated(aster, pair.aster_symbol, start_ms, now_ms),
                    fetch_klines_paginated(mexc, pair.mexc_symbol, start_ms, now_ms),
                )
            except Exception as exc:
                log.warning("  skip %s: %s", sym, exc)
                continue

            if len(perp_raw) < 60 or len(spot_raw) < 60:
                log.info("  skip %s: too few candles (perp=%d, spot=%d)",
                         sym, len(perp_raw), len(spot_raw))
                continue

            aligned = align_candles(perp_raw, spot_raw)
            if len(aligned) < 60:
                log.info("  skip %s: only %d aligned candles", sym, len(aligned))
                continue

            basis_series, trades = run_backtest(
                aligned, pair, breakeven_bps, confirm_minutes=args.confirm
            )
            result = summarize_symbol(sym, pair, aligned, basis_series, trades, breakeven_bps)
            results.append(result)
            log.info("  %s: %d candles, %d trades, %d wins, mean basis %.1f bps",
                     sym, result.candles, result.trades, result.wins, result.mean_basis_bps)

            # Rate limit
            await asyncio.sleep(0.2)

    if not results:
        log.error("no results — check network / symbol filters")
        return

    results = sorted(results, key=lambda r: (r.wins, r.mean_basis_bps), reverse=True)[:args.top]
    report = format_results(results)
    print(report)

    # Write to file
    out_dir = config.OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "basis_backtest.txt"
    out_file.write_text(report)
    log.info("report written to %s", out_file)

    # Also write CSV for further analysis
    csv_file = out_dir / "basis_backtest.csv"
    with open(csv_file, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "symbol", "candles", "hours", "breakeven_bps",
            "trades", "wins", "win_rate",
            "median_converge_min", "mean_converge_min",
            "p75_converge_min", "p95_converge_min", "max_converge_min",
            "median_captured_bps", "mean_captured_bps",
            "mean_basis_bps", "max_basis_bps", "pct_above_breakeven",
        ])
        for r in results:
            w.writerow([
                r.symbol, r.candles, f"{r.hours_covered:.1f}", r.breakeven_bps,
                r.trades, r.wins, f"{r.win_rate:.3f}",
                r.median_converge_min, r.mean_converge_min,
                r.p75_converge_min, r.p95_converge_min, r.max_converge_min,
                r.median_captured_bps, r.mean_captured_bps,
                f"{r.mean_basis_bps:.2f}", f"{r.max_basis_bps:.2f}",
                f"{r.pct_above_breakeven:.2f}",
            ])
    log.info("CSV written to %s", csv_file)


if __name__ == "__main__":
    asyncio.run(main())
