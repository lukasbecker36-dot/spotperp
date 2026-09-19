"""Score the screens against what actually happened.

Reads the labelled rows from build_dataset.py and answers the question the
live boards cannot: of everything that looked good by some measure, what
fraction went on to work?

Group by any feature column and it prints, per bucket, how often the basis
reached the exit level you would have set, how long it took, and what the
round trip paid. Comparing buckets is the whole point — a feature that matters
separates them, a feature that does not shows the same hit rate all the way
down.

Usage:
    python scripts/dataset_report.py output/dataset.csv
    python scripts/dataset_report.py --by jit_bps --horizon 72 output/dataset.csv
    python scripts/dataset_report.py --by funding_8h_bps --buckets 0,2,5,10 \
        --min-rows 50 output/dataset.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_divergence import pctile  # noqa: E402


def _f(row: dict, key: str):
    v = row.get(key, "")
    if v in ("", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def bucket_of(value: float, edges: list[float]) -> str:
    for i, e in enumerate(edges):
        if value < e:
            return f"<{e:g}" if i == 0 else f"{edges[i-1]:g}..{e:g}"
    return f">={edges[-1]:g}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", help="output/dataset.csv from build_dataset.py")
    ap.add_argument("--by", default="net_vs_close_lo",
                    help="feature column to bucket by (default net_vs_close_lo)")
    ap.add_argument("--buckets", default="",
                    help="comma-separated edges; default = quintiles of the data")
    ap.add_argument("--horizon", type=int, default=72,
                    help="which outcome window to score (default 72)")
    ap.add_argument("--min-rows", type=int, default=20,
                    help="hide buckets thinner than this (default 20)")
    ap.add_argument("--symbols", default="", help="comma-separated filter")
    ap.add_argument("--where", action="append", default=[], metavar="COL=LO:HI",
                    help="keep only rows where COL is in [LO,HI); repeatable."
                         " Either bound may be blank. Use this to CONTROL for"
                         " a confounder — bucketing by jit across the whole"
                         " set mixes in the fact that jittery names also have"
                         " bigger edges, so hold net_vs_close_lo fixed and"
                         " look at jit within the band")
    ap.add_argument("--metric", choices=("sustained", "best"),
                    default="sustained",
                    help="which exit figure to report (default sustained: the"
                         " lowest level the basis actually HELD, rather than"
                         " its single lowest print)")
    ap.add_argument("--label", choices=("profit", "lo24"), default="profit",
                    help="what counts as working: the trip became profitable"
                         " after costs (default), or the close basis reached"
                         " the lo24 target")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.dataset)))
    filt = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    if filt:
        rows = [r for r in rows if r["symbol"] in filt]
    for clause in args.where:
        try:
            col, rng = clause.split("=", 1)
            lo_s, hi_s = rng.split(":", 1)
        except ValueError:
            print(f"bad --where {clause!r}; expected COL=LO:HI", file=sys.stderr)
            return
        lo = float(lo_s) if lo_s.strip() else float("-inf")
        hi = float(hi_s) if hi_s.strip() else float("inf")
        before = len(rows)
        rows = [r for r in rows
                if (_f(r, col) is not None and lo <= _f(r, col) < hi)]
        print(f"where {col} in [{lo:g},{hi:g}): {before:,} -> {len(rows):,} rows",
              file=sys.stderr)
    if not rows:
        print("no rows left after filtering", file=sys.stderr)
        return
    h = args.horizon
    reached_k = f"h{h}_reached_{args.label}"
    hours_k = f"h{h}_hours_to_{args.label}"
    best_k, hold_k, fund_k = (
        f"h{h}_{args.metric}_pnl_bps", f"h{h}_hold_pnl_bps", f"h{h}_funding_bps",
    )
    if rows and reached_k not in rows[0]:
        print(f"no {h}h outcomes in this file; available horizons: "
              + ", ".join(sorted({
                  k.split('_')[0][1:] for k in rows[0] if k.startswith("h")
                  and k.endswith("_reached_lo24")
              })), file=sys.stderr)
        return
    vals = [v for v in (_f(r, args.by) for r in rows) if v is not None]
    groups: dict[str, list[dict]] = {}
    if vals:
        if args.buckets:
            edges = [float(x) for x in args.buckets.split(",")]
        else:
            edges = [round(pctile(vals, p), 2) for p in (20, 40, 60, 80)]
        for r in rows:
            v = _f(r, args.by)
            if v is None:
                continue
            groups.setdefault(bucket_of(v, edges), []).append(r)
        order = ([f"<{edges[0]:g}"]
                 + [f"{edges[i-1]:g}..{edges[i]:g}" for i in range(1, len(edges))]
                 + [f">={edges[-1]:g}"])
    else:
        # A text column (symbol, say) groups by value — "which names actually
        # work" is as reasonable a question as "which levels do".
        for r in rows:
            groups.setdefault(r.get(args.by, ""), []).append(r)
        if not any(groups):
            print(f"column {args.by!r} not found", file=sys.stderr)
            return
        order = sorted(groups, key=lambda k: -len(groups[k]))
    label = "med exit" if args.metric == "sustained" else "med best"
    hdr = (f"{args.by:<18}{'rows':>7}{'reached':>9}{'med hrs':>9}"
           f"{label:>10}{'med hold':>10}{'med fund':>10}")
    print(f"scoring {len(rows):,} rows over a {h}h horizon")
    print(hdr)
    print("-" * len(hdr))
    for name in order:
        g = groups.get(name, [])
        if len(g) < args.min_rows:
            continue
        reached = [r for r in g if _f(r, reached_k) == 1]
        hours = sorted(x for x in (_f(r, hours_k) for r in reached) if x is not None)
        best = sorted(x for x in (_f(r, best_k) for r in g) if x is not None)
        hold = sorted(x for x in (_f(r, hold_k) for r in g) if x is not None)
        fund = sorted(x for x in (_f(r, fund_k) for r in g) if x is not None)
        print(
            f"{name:<18}{len(g):>7,}{len(reached) / len(g) * 100:>8.0f}%"
            f"{(pctile(hours, 50) if hours else float('nan')):>9.1f}"
            f"{pctile(best, 50):>10.1f}{pctile(hold, 50):>10.1f}"
            f"{pctile(fund, 50):>10.1f}"
        )
    print("-" * len(hdr))
    if args.label == "profit":
        print(
            "reached = closing out actually paid, basis plus funding, after"
            " costs. med hrs = how long that took, over the rows that got"
            " there. Use --label lo24 to score against the exit target"
            " instead — but note a basis pinned at its own 24h low satisfies"
            " that trivially while never being worth trading."
        )
    else:
        print(
            "reached = the close basis got down to the lo24 you would have set"
            " as the exit target. med hrs = how long that took, over the rows"
            " that got there."
        )
    if args.metric == "sustained":
        print(
            "med exit = round trip at the lowest level the basis actually HELD"
            " for several samples — still a well-timed exit, but not one set"
            " by a single flickering print (--metric best uses the raw"
            " minimum, which one bad tick can move by thousands of bps)."
        )
    else:
        print(
            "med best = round trip at the horizon's single lowest print. One"
            " flickering quote sets it, so it is not a level you could have"
            " traded — prefer the default --metric sustained."
        )
    print(
        "med hold = holding the whole horizon and taking whatever the basis"
        " was at the end. Both are net of the cost floor and include funding."
    )
    print(
        "A feature that matters separates the buckets. One that does not shows"
        " the same hit rate all the way down — which is the finding, not a"
        " failure."
    )


if __name__ == "__main__":
    # Piping into head is the natural way to read this; don't traceback on it.
    try:
        main()
    except BrokenPipeError:
        sys.stderr.close()
