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


def clip_basis(conn, phase: str = "entry", paper: bool = False) -> list[dict]:
    """Every hedged clip of one phase: a spot leg, and the perp fills it paired.

    Entry and exit are mirror images — entry rests a perp SELL and takes spot
    at the ask, exit rests a perp BUY and takes spot at the bid — so the same
    pairing serves both. 'unwind' fills are excluded: they reverse an entry
    that was never hedged, so there is no spot leg and nothing was locked.
    """
    rows = conn.execute(
        "SELECT f.position_id, f.venue, f.phase, f.side, f.qty, f.price,"
        " f.ts_ms, p.symbol FROM fills f JOIN positions p ON p.id=f.position_id"
        " WHERE f.phase=? AND p.paper=? ORDER BY f.position_id, f.id",
        (phase, int(paper)),
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


def quoted_before(
    s: Series, ts_ms: int, window_min: float, phase: str = "entry"
) -> tuple | None:
    """(mean basis, jitter, samples, min depth) over the window BEFORE ts_ms.

    Before, not at: by the moment a resting order fills the basis has already
    moved, so pricing the comparison at the fill would bake in the very effect
    being measured. This is what the board would have shown you while the
    order sat there.
    """
    hi = bisect.bisect_left(s.ts, ts_ms)
    lo = bisect.bisect_left(s.ts, ts_ms - int(window_min * 60_000))
    # An entry is quoted ask/ask and an exit bid/bid — comparing a realised
    # exit against the entry series would charge it the whole spread.
    vals = (s.entry if phase == "entry" else s.close)[lo:hi]
    if len(vals) < 3:
        return None
    jit = sum(abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))) / (len(vals) - 1)
    # Depth while the order rested, so a clip can be expressed as a FRACTION
    # of the book rather than an absolute size. The executor already sizes a
    # clip to the book at placement (max_hedgeable_qty) but the order fills
    # later, against a book that has moved — so the question this answers is
    # whether the cap should scale with depth instead of being flat.
    depth = min(s.depth[lo:hi]) if hi > lo else 0.0
    return sum(vals) / len(vals), jit, len(vals), depth


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
    ap.add_argument("--phase", choices=("entry", "exit", "both"), default="both",
                    help="which side to score (default both). The round trip"
                         " pays slippage twice, so the cost floor needs the"
                         " sum, not the entry alone")
    ap.add_argument("--regress", action="store_true",
                    help="also fit slippage against clip size and book"
                         " fraction, to decide whether the clip cap should be"
                         " flat or scale with depth")
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
    phases = ["entry", "exit"] if args.phase == "both" else [args.phase]
    all_clips: list[dict] = []
    for ph in phases:
        for c in clip_basis(conn, phase=ph, paper=args.paper):
            if c["notional_usd"] >= args.min_notional:
                all_clips.append({**c, "phase": ph})
    if not all_clips:
        print("no hedged clips found in the DB", file=sys.stderr)
        return
    symbols = {c["symbol"] for c in all_clips}
    print(f"{len(all_clips)} clips across {len(symbols)} symbols", file=sys.stderr)

    series = load_logs(paths, symbols)
    scored = []
    no_quote = 0
    for c in all_clips:
        s = series.get(c["symbol"])
        q = quoted_before(s, c["ts_ms"], args.window_min, c["phase"]) if s else None
        if q is None:
            no_quote += 1
            continue
        quoted, jit, n, depth = q
        # Positive always means WORSE than the board showed. An entry wants a
        # high basis so locking lower is the loss; an exit wants a low one, so
        # the sign flips.
        slip = (quoted - c["locked_bps"] if c["phase"] == "entry"
                else c["locked_bps"] - quoted)
        scored.append({
            **c,
            "quoted_bps": round(quoted, 2),
            "jit_bps": round(jit, 2),
            "quote_samples": n,
            "depth_usd": round(depth, 0),
            # How much of the visible book this clip was. A flat cap is right
            # if slippage tracks notional; a depth-scaled cap is right if it
            # tracks this instead.
            "size_frac": round(c["notional_usd"] / depth, 3) if depth > 0 else "",
            "slippage_bps": round(slip, 2),
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

    medians = {}
    for ph in phases:
        rows_ph = [r for r in scored if r["phase"] == ph]
        if not rows_ph:
            print(f"\nno {ph} clips scored", file=sys.stderr)
            continue
        medians[ph] = _report(rows_ph, ph, args.by)
        if args.regress:
            _regress(rows_ph, ph)

    if len(medians) > 1:
        total = sum(medians.values())
        print(f"\nround trip: {medians.get('entry', 0):+.1f} entry"
              f" {medians.get('exit', 0):+.1f} exit = {total:+.1f} bps of"
              f" slippage per complete trade.")
        print(
            f"SLIPPAGE_BUFFER_BPS is {float(config.SLIPPAGE_BUFFER_BPS):g}, so"
            f" the cost floor budgets that against {total:.1f} measured."
        )


def _ols(rows: list[dict], y_key: str, x_keys: list[str]) -> tuple | None:
    """Least squares with an intercept, solved by Gaussian elimination.

    Pure Python on purpose: this runs on the trading box, which has aiohttp
    and not much else, and a handful of features over a few hundred clips does
    not need a linear algebra dependency.
    """
    data = []
    for r in rows:
        xs = [r.get(k) for k in x_keys]
        y = r.get(y_key)
        if any(not isinstance(v, (int, float)) for v in xs + [y]):
            continue
        data.append(([1.0] + [float(v) for v in xs], float(y)))
    n, k = len(data), len(x_keys) + 1
    if n <= k + 2:
        return None
    # Normal equations (X'X)b = X'y.
    xtx = [[sum(row[i] * row[j] for row, _ in data) for j in range(k)]
           for i in range(k)]
    xty = [sum(row[i] * y for row, y in data) for i in range(k)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r2: abs(xtx[r2][col]))
        if abs(xtx[piv][col]) < 1e-12:
            return None
        xtx[col], xtx[piv] = xtx[piv], xtx[col]
        xty[col], xty[piv] = xty[piv], xty[col]
        d = xtx[col][col]
        xtx[col] = [v / d for v in xtx[col]]
        xty[col] /= d
        for r2 in range(k):
            if r2 == col:
                continue
            f = xtx[r2][col]
            xtx[r2] = [a - f * b for a, b in zip(xtx[r2], xtx[col])]
            xty[r2] -= f * xty[col]
    beta = xty
    ybar = sum(y for _, y in data) / n
    ss_tot = sum((y - ybar) ** 2 for _, y in data)
    ss_res = sum((y - sum(b * v for b, v in zip(beta, row))) ** 2
                 for row, y in data)
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return beta, r2, n


def _regress(scored: list[dict], phase: str) -> None:
    """Is slippage a function of absolute clip size, or of the FRACTION of the
    book a clip takes? The answer decides whether the cap should be flat or
    scale with depth."""
    print(f"\n{phase}: slippage_bps regressed on ...")
    for name, keys in (
        ("notional only", ["notional_usd"]),
        ("book fraction only", ["size_frac"]),
        ("both", ["notional_usd", "size_frac"]),
        ("both + jitter", ["notional_usd", "size_frac", "jit_bps"]),
    ):
        fit = _ols(scored, "slippage_bps", keys)
        if fit is None:
            print(f"  {name:<20} (not enough usable rows)")
            continue
        beta, r2, n = fit
        terms = "  ".join(f"{k}={b:+.4g}" for k, b in zip(keys, beta[1:]))
        print(f"  {name:<20} R2={r2:5.3f}  n={n:<5} const={beta[0]:+.2f}  {terms}")
    print(
        "  Compare the R2 values: whichever of notional and book fraction"
        " explains more is the one the clip cap should key on. A low R2 on"
        " both means clip sizing is not what drives slippage."
    )


def _report(scored: list[dict], phase: str, by: str) -> float:
    """Print one phase's summary and bucket table; return its median slippage."""
    slip = sorted(r["slippage_bps"] for r in scored)
    quoted_all = sorted(r["quoted_bps"] for r in scored)
    locked_all = sorted(r["locked_bps"] for r in scored)
    med = pctile(slip, 50)
    print(f"\n{len(scored)} hedged {phase} clips")
    print(f"  median quoted   {pctile(quoted_all, 50):+8.1f} bps")
    print(f"  median locked   {pctile(locked_all, 50):+8.1f} bps")
    print(f"  median slippage {med:+8.1f} bps"
          f"   (p25 {pctile(slip, 25):+.1f}, p75 {pctile(slip, 75):+.1f})")

    if by not in scored[0]:
        print(f"no column {by!r}; try one of: "
              + ", ".join(k for k in scored[0] if k not in ("symbol", "phase")),
              file=sys.stderr)
        return med
    vals = [r[by] for r in scored if isinstance(r[by], (int, float))]
    if not vals:
        groups: dict[str, list] = {}
        for r in scored:
            groups.setdefault(str(r[by]), []).append(r)
        order = sorted(groups, key=lambda k: -len(groups[k]))[:20]
    else:
        edges = [round(pctile(sorted(vals), p), 2) for p in (25, 50, 75)]
        groups = {}
        skipped = 0
        for r in scored:
            j = r[by]
            # size_frac is blank when the logs carried no depth for that
            # minute. Those rows are absent from this cut, not zero.
            if not isinstance(j, (int, float)):
                skipped += 1
                continue
            name = (f"<{edges[0]:g}" if j < edges[0] else
                    f">={edges[-1]:g}" if j >= edges[-1] else
                    next(f"{edges[i-1]:g}..{edges[i]:g}"
                         for i in range(1, len(edges)) if j < edges[i]))
            groups.setdefault(name, []).append(r)
        order = ([f"<{edges[0]:g}"]
                 + [f"{edges[i-1]:g}..{edges[i]:g}" for i in range(1, len(edges))]
                 + [f">={edges[-1]:g}"])
        if skipped:
            print(f"  ({skipped} clips have no {by} and are left out of this"
                  f" cut)", file=sys.stderr)
    ratio_hdr = "slip/jit" if by == "jit_bps" else "med " + by
    hdr = (f"{by:<14}{'clips':>7}{'med quoted':>12}"
           f"{'med locked':>12}{'med slip':>10}{ratio_hdr[:10]:>11}")
    print(hdr)
    print("-" * len(hdr))
    for name in order:
        g = groups.get(name, [])
        if not g:
            continue
        gv = [r[by] for r in g if isinstance(r[by], (int, float))]
        med_j = pctile(sorted(gv), 50) if gv else 0.0
        med_s = pctile(sorted(r["slippage_bps"] for r in g), 50)
        # Against jitter the useful number is the implied haircut; against
        # anything else it is just the bucket's own median.
        tail = (med_s / med_j if med_j else 0.0) if by == "jit_bps" else med_j
        print(
            f"{name:<14}{len(g):>7}"
            f"{pctile(sorted(r['quoted_bps'] for r in g), 50):>12.1f}"
            f"{pctile(sorted(r['locked_bps'] for r in g), 50):>12.1f}"
            f"{med_s:>10.1f}{tail:>11.2f}"
        )
    print("-" * len(hdr))
    print(
        "slippage = how much WORSE than the board the clip came out. An entry"
        " wants a high basis so locking lower is the loss; an exit wants a low"
        " one, so its sign is flipped to match."
    )
    if by == "jit_bps":
        print(
            "slip/jit is the haircut the data implies against the 1.00x"
            " fill_score applies. A FALLING ratio with flat slippage means the"
            " cost is a constant, not a multiple of jitter — read med slip."
        )
    return med


if __name__ == "__main__":
    main()
