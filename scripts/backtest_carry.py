"""Backtest the FUNDING-CARRY strategy from the engine's own basis logs.

Unlike divergence capture (monetise the basis), carry monetises FUNDING: enter
a premium hedge (short Aster perp / long MEXC spot) while the perp pays a rich
funding rate, hold to collect it, and give back only the basis drift + costs.
This replays output/basis_log_*.csv to measure whether the funding actually
collected exceeds those costs, per name, at various funding thresholds — the
evidence needed before wiring up carry auto-entry.

Execution model (the live engine's): maker perp + taker spot, both legs, in
and out. The basis P&L over a hold is:

    basis_pnl_bps = entry_bps[entry]  -  close_bps[exit]

(entry short at the ask/ask basis, exit at the bid/bid basis). This bakes in
the maker perp legs EARNING their spread — an OPTIMISTIC assumption, since real
resting orders are adverse-selected; treat basis_pnl as an upper bound. Funding
dominates a multi-day carry, so this matters less than for divergence capture.

    net_bps = funding_collected_bps + basis_pnl_bps - fees_bps
    annualised = net_bps * 8760 / hold_hours     (per unit notional)

Simulation (no look-ahead):
  - entry: funding_8h_bps >= --threshold for --confirm samples, depth >=
    --min-depth; executes on the NEXT sample;
  - funding accrues each step as funding_8h_bps/8 * hours (short receives
    positive funding; per-step gap capped at 1h);
  - exit when funding_8h_bps <= --exit-funding (carry decayed), or the basis
    has converged to <= --exit-basis (optional, default off = +inf so it never
    triggers), or --max-hold-hours elapses — executing on the next sample;
  - one carry per symbol at a time, sequential re-entry.

Usage (on the server, no network):
    python scripts/backtest_carry.py --every 5 output/basis_log_*.csv
    python scripts/backtest_carry.py --thresholds 5,10,20 --min-depth 1000 \
        --max-hold-hours 72 --per-symbol-threshold 10 output/basis_log_*.csv
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from backtest_divergence import Series, load_logs, pctile  # noqa: E402

FUNDING_STEP_CAP_HOURS = 1.0
HOURS_PER_YEAR = 8760.0


@dataclass
class Carry:
    symbol: str
    entry_ts: int
    exit_ts: int
    basis_in: float          # entry_bps at entry (short established here)
    basis_out: float         # close_bps at exit
    funding_bps: float       # collected over the hold
    fees_bps: float
    depth_usd: float
    reason: str              # funding_decay | basis_converged | max_hold | data_end

    @property
    def basis_pnl(self) -> float:
        return self.basis_in - self.basis_out

    @property
    def net_bps(self) -> float:
        return self.funding_bps + self.basis_pnl - self.fees_bps

    @property
    def hold_hours(self) -> float:
        return max((self.exit_ts - self.entry_ts) / 3_600_000, 1e-9)

    @property
    def annualised_bps(self) -> float:
        return self.net_bps * HOURS_PER_YEAR / self.hold_hours


def simulate_carry(
    symbol: str, series: Series, *,
    threshold: float, exit_funding: float, exit_basis: float,
    confirm: int, min_depth: float, max_hold_hours: float, fees_bps: float,
    gap_reset_ms: int, maker_fill_frac: float = 0.0,
) -> list[Carry]:
    trades: list[Carry] = []
    ts, ent, cls, fnd, dep = (
        series.ts, series.entry, series.close, series.funding, series.depth,
    )
    n = len(ts)
    streak = 0
    i = 0
    while i < n - 1:
        if i > 0 and ts[i] - ts[i - 1] > gap_reset_ms:
            streak = 0
        if fnd[i] >= threshold and dep[i] >= min_depth:
            streak += 1
        else:
            streak = 0
        if streak < confirm:
            i += 1
            continue
        entry_i = i + 1
        streak = 0
        if fnd[entry_i] < threshold:
            i = entry_i               # funding decayed before we entered
            continue
        # maker perp entry: frac=0 fills at the ask (earns full spread,
        # optimistic); frac=1 fills at the bid (gives up the whole perp
        # spread to adverse selection, pessimistic). The spread proxy is
        # (entry_bps - close_bps) at entry: ask/ask minus bid/bid basis.
        basis_in = ent[entry_i] - maker_fill_frac * (ent[entry_i] - cls[entry_i])
        funding = 0.0
        deadline = ts[entry_i] + max_hold_hours * 3_600_000
        exit_i = None
        reason = "data_end"
        j = entry_i
        while j + 1 < n:
            j += 1
            step_h = min((ts[j] - ts[j - 1]) / 3_600_000, FUNDING_STEP_CAP_HOURS)
            funding += fnd[j] / 8.0 * step_h
            if ts[j] > deadline:
                exit_i, reason = j, "max_hold"
                break
            if fnd[j] <= exit_funding:
                exit_i, reason = (j + 1 if j + 1 < n else j), "funding_decay"
                break
            if cls[j] <= exit_basis:
                exit_i, reason = (j + 1 if j + 1 < n else j), "basis_converged"
                break
        if exit_i is None:            # ran off the end still open
            i = n
            continue
        trades.append(Carry(
            symbol=symbol,
            entry_ts=ts[entry_i],
            exit_ts=ts[exit_i],
            basis_in=basis_in,
            basis_out=cls[exit_i],
            funding_bps=funding,
            fees_bps=fees_bps,
            depth_usd=dep[entry_i],
            reason=reason,
        ))
        i = exit_i + 1
    return trades


def summarize(trades: list[Carry]) -> dict:
    if not trades:
        return {"trades": 0}
    nets = [t.net_bps for t in trades]
    ann = [t.annualised_bps for t in trades]
    return {
        "trades": len(trades),
        "win_rate": sum(1 for x in nets if x > 0) / len(nets),
        "med_net": pctile(nets, 50),
        "mean_net": sum(nets) / len(nets),
        "p25_net": pctile(nets, 25),
        "total_net": sum(nets),
        "med_fund": pctile([t.funding_bps for t in trades], 50),
        "med_basis": pctile([t.basis_pnl for t in trades], 50),
        "med_hold_h": pctile([t.hold_hours for t in trades], 50),
        "med_ann": pctile(ann, 50),
        "med_depth": pctile([t.depth_usd for t in trades], 50),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Backtest funding-carry P&L from output/basis_log_*.csv")
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--thresholds", default="2,5,10,20",
                    help="entry funding_8h_bps thresholds (comma-separated)")
    ap.add_argument("--exit-funding", type=float, default=0.0,
                    help="exit when funding_8h_bps <= this (default 0)")
    ap.add_argument("--exit-basis", type=float, default=float("-inf"),
                    help="also exit if the closeable basis <= this bps (premium"
                         " converged); default -inf = off")
    ap.add_argument("--confirm", type=int, default=3)
    ap.add_argument("--min-depth", type=float,
                    default=float(config.MIN_DEPTH_NOTIONAL_USD))
    ap.add_argument("--max-hold-hours", type=float, default=168.0)
    ap.add_argument("--slippage-bps", type=float, default=4.0,
                    help="extra round-trip cost beyond the touch (default 4)")
    ap.add_argument("--maker-fill-frac", type=float, default=0.0,
                    help="share of the perp bid-ask the maker leg GIVES UP at"
                         " entry (0=earns full spread/optimistic, 1=fills at"
                         " the bid/pessimistic). Sensitivity knob: the default"
                         " basis P&L assumes the maker earns its spread; raise"
                         " this to see if funding alone still carries the edge.")
    ap.add_argument("--symbols", default=None, help="comma-separated filter")
    ap.add_argument("--per-symbol-threshold", type=float, default=None)
    ap.add_argument("--every", type=int, default=1,
                    help="keep 1-in-N samples per symbol (downsample)")
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

    # maker perp (0 fee) + taker spot, both legs
    fees = 2 * float(config.MEXC_TAKER_FEE * 10000) + args.slippage_bps
    thresholds = [float(x) for x in args.thresholds.split(",")]

    lines = []
    lines.append(f"funding-carry backtest — {len(data)} symbols, {n_samples:,}"
                 f" samples, ~{span_h:.1f}h span")
    lines.append(f"maker-perp/taker-spot | fees {fees:.1f}bps round-trip |"
                 f" confirm {args.confirm} | depth>= ${args.min_depth:,.0f} |"
                 f" exit fund<= {args.exit_funding} basis<= {args.exit_basis} |"
                 f" max hold {args.max_hold_hours:.0f}h")
    mf = args.maker_fill_frac
    mf_note = ("maker EARNS full spread = OPTIMISTIC" if mf <= 0
               else "maker FILLS AT BID = PESSIMISTIC" if mf >= 1
               else f"maker gives up {mf:.0%} of the perp spread")
    lines.append("NET = funding collected + basis drift (entry - exit basis) - fees."
                 f" maker-fill-frac {mf:.2f} ({mf_note}).")
    lines.append("annBps = net annualised per unit notional (net x 8760 / hold_h).")
    hdr = (f"{'fund_thr':>8}{'trades':>7}{'win%':>6}{'medNet':>8}{'meanNet':>8}"
           f"{'p25':>7}{'medFund':>8}{'medBasis':>9}{'medHold_h':>10}"
           f"{'medAnn%':>8}{'total':>10}{'medDep$':>8}")
    lines.append(hdr)
    lines.append("-" * len(hdr))

    per_symbol: list[tuple] = []
    for thr in thresholds:
        allt: list[Carry] = []
        for sym, series in data.items():
            t = simulate_carry(
                sym, series, threshold=thr, exit_funding=args.exit_funding,
                exit_basis=args.exit_basis, confirm=args.confirm,
                min_depth=args.min_depth, max_hold_hours=args.max_hold_hours,
                fees_bps=fees, gap_reset_ms=gap_reset_ms,
                maker_fill_frac=args.maker_fill_frac,
            )
            allt.extend(t)
            if args.per_symbol_threshold == thr and t:
                per_symbol.append((sym, summarize(t)))
        r = summarize(allt)
        if r["trades"] == 0:
            lines.append(f"{thr:>8.1f}{0:>7}   (no qualifying carries)")
            continue
        lines.append(
            f"{thr:>8.1f}{r['trades']:>7}{r['win_rate'] * 100:>5.0f}%"
            f"{r['med_net']:>8.1f}{r['mean_net']:>8.1f}{r['p25_net']:>7.1f}"
            f"{r['med_fund']:>8.1f}{r['med_basis']:>9.1f}{r['med_hold_h']:>10.1f}"
            f"{r['med_ann'] / 100:>8.1f}{r['total_net']:>10.0f}{r['med_depth']:>8.0f}"
        )

    if per_symbol:
        lines.append("")
        lines.append(f"per-symbol at fund_thr {args.per_symbol_threshold}"
                     f" (sorted by median annualised):")
        per_symbol.sort(key=lambda x: x[1].get("med_ann", 0), reverse=True)
        for sym, r in per_symbol[:40]:
            lines.append(
                f"  {sym:<16}n={r['trades']:<4} win={r['win_rate'] * 100:>3.0f}%"
                f" medNet={r['med_net']:>7.1f} medFund={r['med_fund']:>6.1f}"
                f" medBasis={r['med_basis']:>7.1f} hold={r['med_hold_h']:>5.1f}h"
                f" ann={r['med_ann'] / 100:>6.1f}% dep=${r['med_depth']:,.0f}"
            )

    lines.append("")
    lines.append("READ: a real carry edge shows POSITIVE mean (not just median)"
                 " net, on names with tradeable depth, where funding CLEARLY"
                 " exceeds the (often negative) basis drift. Watch medBasis: if"
                 " it's a big negative, the premium widened against you while you"
                 " held — funding has to cover that too.")
    report = "\n".join(lines)
    print(report)
    out = config.OUTPUT_DIR / "carry_backtest.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(f"\nreport written to {out}")


if __name__ == "__main__":
    main()
