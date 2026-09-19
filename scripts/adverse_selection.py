"""Measure adverse selection on real entry fills: quoted basis vs locked basis.

fill_score subtracts the full 5-minute jitter from a candidate's edge, on the
theory that a resting maker order fills on the bad side of a moving basis so
the quoted number overstates what you lock. The evidence for that was a single
position (STONK #186: quoted +121bps across its clips, locked +58.8), and the
labelled dataset cannot test it — that has prices in it, not fills.

This does. It joins the fills the engine actually got against the basis logs
that recorded what was quoted at the time, and reports, per clip:

    slippage = quoted entry basis (5m mean before the fill) - basis locked

then buckets it by the jitter over that same window. If slippage grows with
jitter the haircut is right in kind, and the slope says whether subtracting
the FULL jitter is right in size. If it is flat, the haircut is demoting rows
for nothing.

Clips are paired the way the executor hedges them: perp entry fills queue up
unhedged, and each spot buy consumes them oldest-first, so a clip's locked
basis uses the VWAP of exactly the perp fills that spot leg hedged.

Contract multipliers are not in the DB, so they are inferred per position by
rounding perp_price/spot_price to the nearest power of ten. A position whose
ratio is nowhere near one is reported and skipped rather than guessed at.

Usage:
    .venv/bin/python scripts/adverse_selection.py output/basis_log_*.csv*
    .venv/bin/python scripts/adverse_selection.py --db data/positions.db \
        --csv output/adverse.csv output/basis_log_*.csv*
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import sqlite3
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from backtest_divergence import Series, load_logs, pctile  # noqa: E402

HOUR_MS = 3_600_000
MULTS = (1, 10, 100, 1000, 10_000, 100_000)


def infer_multiplier(perp_px: float, spot_px: float) -> int | None:
    """Nearest power of ten to perp/spot, or None if nothing is close.

    A 1000PEPE-style contract quotes ~1000x the spot price. Getting this wrong
    moves the basis by ~(mult-1)*1e4 bps, so a ratio that matches nothing is
    reported rather than forced into the closest bucket.
    """
    if perp_px <= 0 or spot_px <= 0:
        return None
    ratio = perp_px / spot_px
    best = min(MULTS, key=lambda m: abs(ratio / m - 1))
    return best if abs(ratio / best - 1) < 0.25 else None


def clip_basis(conn, paper: bool = False) -> list[dict]:
    """Every hedged entry clip: the spot buy, and the perp fills it hedged."""
    rows = conn.execute(
        "SELECT f.position_id, f.venue, f.phase, f.side, f.qty, f.price,"
        " f.ts_ms, p.symbol FROM fills f JOIN positions p ON p.id=f.position_id"
        " WHERE f.phase='entry' AND p.paper=? ORDER BY f.position_id, f.id",
        (int(paper),),
    ).fetchall()
    by_pos: dict[int, list] = {}
    for r in rows:
        by_pos.setdefault(r["position_id"], []).append(r)

    clips: list[dict] = []
    for pid, fills in by_pos.items():
        # Infer the multiplier from the position's own first perp/spot pair.
        perp_px = next((float(f["price"]) for f in fills if f["venue"] == "aster"), 0.0)
        spot_px = next((float(f["price"]) for f in fills if f["venue"] == "mexc"), 0.0)
        mult = infer_multiplier(perp_px, spot_px)
        if mult is None:
            continue
        pending: deque[list] = deque()      # [qty_contracts, price, ts_ms]
        for f in fills:
            qty, price, ts = float(f["qty"]), float(f["price"]), int(f["ts_ms"])
            if f["venue"] == "aster":
                pending.append([qty, price, ts])
                continue
            # A spot buy: consume the perp fills it hedges, oldest first.
            need = qty / mult                # spot base units -> contracts
            took = cost = 0.0
            first_ts = None
            while need > 1e-12 and pending:
                take = min(pending[0][0], need)
                if first_ts is None:
                    first_ts = pending[0][2]
                took += take
                cost += take * pending[0][1]
                pending[0][0] -= take
                need -= take
                if pending[0][0] <= 1e-12:
                    pending.popleft()
            if took <= 0 or price <= 0:
                continue
            perp_vwap = cost / took
            clips.append({
                "position_id": pid,
                "symbol": f["symbol"],
                "ts_ms": first_ts or ts,
                "hedge_ts_ms": ts,
                "qty_base": round(qty, 6),
                "notional_usd": round(qty * price, 2),
                "perp_vwap": perp_vwap / mult,
                "spot_price": price,
                "locked_bps": round(
                    (perp_vwap / mult - price) / price * 10_000, 2
                ),
            })
    return clips


def quoted_before(s: Series, ts_ms: int, window_min: float) -> tuple | None:
    """(mean entry basis, jitter, samples) over the window BEFORE ts_ms.

    Before, not at: by the moment a resting order fills the basis has already
    moved, so pricing the comparison at the fill would bake in the very effect
    being measured. This is what the board would have shown you while the
    order sat there.
    """
    hi = bisect.bisect_left(s.ts, ts_ms)
    lo = bisect.bisect_left(s.ts, ts_ms - int(window_min * 60_000))
    vals = s.entry[lo:hi]
    if len(vals) < 3:
        return None
    jit = sum(abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))) / (len(vals) - 1)
    return sum(vals) / len(vals), jit, len(vals)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+", help="output/basis_log_*.csv[.gz]")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--csv", default="", help="also write the per-clip rows here")
    ap.add_argument("--window-min", type=float, default=5.0,
                    help="minutes of quotes before the fill to average (default 5)")
    ap.add_argument("--min-notional", type=float, default=0.0,
                    help="ignore clips smaller than this (dust distorts medians)")
    ap.add_argument("--by", default="jit_bps",
                    help="column to bucket by (default jit_bps). notional_usd"
                         " separates a constant spread-crossing cost from one"
                         " that grows as a clip walks the book")
    ap.add_argument("--paper", action="store_true", help="score paper fills instead")
    args = ap.parse_args()

    paths: list[str] = []
    for pattern in args.logs:
        hits = sorted(glob.glob(pattern))
        paths.extend(hits or ([pattern] if Path(pattern).exists() else []))
    if not paths:
        print("no log files matched", file=sys.stderr)
        return

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    clips = [c for c in clip_basis(conn, paper=args.paper)
             if c["notional_usd"] >= args.min_notional]
    if not clips:
        print("no hedged entry clips found in the DB", file=sys.stderr)
        return
    symbols = {c["symbol"] for c in clips}
    print(f"{len(clips)} clips across {len(symbols)} symbols", file=sys.stderr)

    series = load_logs(paths, symbols)
    scored = []
    no_quote = 0
    for c in clips:
        s = series.get(c["symbol"])
        q = quoted_before(s, c["ts_ms"], args.window_min) if s else None
        if q is None:
            no_quote += 1
            continue
        quoted, jit, n = q
        scored.append({
            **c,
            "quoted_bps": round(quoted, 2),
            "jit_bps": round(jit, 2),
            "quote_samples": n,
            # Positive = the board promised more than the clip locked.
            "slippage_bps": round(quoted - c["locked_bps"], 2),
        })
    if no_quote:
        print(f"{no_quote} clips had no quotes in the logs for that window",
              file=sys.stderr)
    if not scored:
        print("nothing to score — do the logs cover when these trades ran?",
              file=sys.stderr)
        return

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(scored[0].keys()))
            w.writeheader()
            w.writerows(scored)
        print(f"wrote {len(scored)} rows to {out}", file=sys.stderr)

    slip = sorted(r["slippage_bps"] for r in scored)
    quoted_all = sorted(r["quoted_bps"] for r in scored)
    locked_all = sorted(r["locked_bps"] for r in scored)
    print(f"\n{len(scored)} hedged entry clips")
    print(f"  median quoted   {pctile(quoted_all, 50):+8.1f} bps")
    print(f"  median locked   {pctile(locked_all, 50):+8.1f} bps")
    print(f"  median slippage {pctile(slip, 50):+8.1f} bps"
          f"   (p25 {pctile(slip, 25):+.1f}, p75 {pctile(slip, 75):+.1f})")

    by = args.by
    if scored and by not in scored[0]:
        print(f"no column {by!r}; try one of: "
              + ", ".join(k for k in scored[0] if k != "symbol"), file=sys.stderr)
        return
    edges = [round(pctile(sorted(r[by] for r in scored), p), 2)
             for p in (25, 50, 75)]
    ratio_hdr = "slip/jit" if by == "jit_bps" else "med " + by
    hdr = (f"\n{by:<14}{'clips':>7}{'med quoted':>12}"
           f"{'med locked':>12}{'med slip':>10}{ratio_hdr:>10}")
    print(hdr)
    print("-" * (len(hdr) - 1))
    groups: dict[str, list] = {}
    for r in scored:
        j = r[by]
        name = (f"<{edges[0]:g}" if j < edges[0] else
                f">={edges[-1]:g}" if j >= edges[-1] else
                next(f"{edges[i-1]:g}..{edges[i]:g}"
                     for i in range(1, len(edges)) if j < edges[i]))
        groups.setdefault(name, []).append(r)
    order = ([f"<{edges[0]:g}"]
             + [f"{edges[i-1]:g}..{edges[i]:g}" for i in range(1, len(edges))]
             + [f">={edges[-1]:g}"])
    for name in order:
        g = groups.get(name, [])
        if not g:
            continue
        med_j = pctile(sorted(r[by] for r in g), 50)
        med_s = pctile(sorted(r["slippage_bps"] for r in g), 50)
        # Against jitter the useful number is the implied haircut; against
        # anything else it is just the bucket's own median.
        tail = (med_s / med_j if med_j else 0.0) if by == "jit_bps" else med_j
        print(
            f"{name:<14}{len(g):>7}"
            f"{pctile(sorted(r['quoted_bps'] for r in g), 50):>12.1f}"
            f"{pctile(sorted(r['locked_bps'] for r in g), 50):>12.1f}"
            f"{med_s:>10.1f}{tail:>10.2f}"
        )
    print("-" * (len(hdr) - 1))
    print(
        "slippage = quoted - locked, so POSITIVE means the board promised more"
        " than the clip got."
    )
    if by == "jit_bps":
        print(
            "slip/jit is the haircut the data implies against the 1.00x"
            " fill_score applies. Flat near zero and the haircut demotes rows"
            " for nothing; flat near one and it is about right; rising and the"
            " shape is right with the size in the last column. A FALLING ratio"
            " with flat slippage means the cost is a constant, not a multiple"
            " of jitter — read the med slip column, not this one."
        )


if __name__ == "__main__":
    main()
