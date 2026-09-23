"""What aborting an entry actually costs, against what salvaging it would have.

When a resting perp clip fills and the basis has collapsed by the time the
spot hedge goes in, the executor buys the perp back rather than "locking a
loss" (ENTRY_HEDGE_MIN_BPS). That buy-back is a perp round trip crossed at
market under a timeout, and it is not free: position 217 gave up $0.97 on a
$100 clip — 97bps — to avoid entering at -15.8.

The two sides are not the same kind of number and the script keeps them apart:

  * the unwind cost is CERTAIN and realised. You paid it.
  * the abort basis is not a loss. Salvaging leaves you holding a hedged
    position entered at that basis, which funding and convergence can still
    repay. It is only a loss if you then close it there.

So the comparison is a certain cost against an opening position, and the
question ENTRY_HEDGE_MIN_BPS answers is where one stops being worth the
other. Unwinds are matched to the entry fills they reversed exactly as
PositionManager does it — walking in order, newest-open-first — so the cost
is priced against the clips that were actually bought back.

Usage:
    .venv/bin/python scripts/unwind_cost.py output/basis_log_*.csv*
    .venv/bin/python scripts/unwind_cost.py --csv output/unwinds.csv \
        --db data/positions.db output/basis_log_*.csv*
"""
from __future__ import annotations

import argparse
import csv
import glob
import sqlite3
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from adverse_selection import quoted_before  # noqa: E402
from backtest_divergence import load_logs, pctile  # noqa: E402


def unwind_events(conn, paper: bool = False) -> list[dict]:
    """Every unwind, priced against the entry fills it actually reversed."""
    rows = conn.execute(
        "SELECT f.position_id, f.venue, f.phase, f.qty, f.price, f.ts_ms,"
        " p.symbol FROM fills f JOIN positions p ON p.id=f.position_id"
        " WHERE p.paper=? AND f.venue='aster' ORDER BY f.position_id, f.id",
        (int(paper),),
    ).fetchall()
    by_pos: dict[int, list] = {}
    for r in rows:
        by_pos.setdefault(r["position_id"], []).append(r)

    out: list[dict] = []
    for pid, fills in by_pos.items():
        open_entries: deque[list] = deque()     # [qty, price], oldest first
        for f in fills:
            qty, price = float(f["qty"]), float(f["price"])
            if f["phase"] == "entry":
                open_entries.append([qty, price])
                continue
            if f["phase"] != "unwind":
                continue
            # Newest open entry first: an unwind reverses the clip that just
            # filled, which is also how PositionManager derives the averages.
            need, took, cost = qty, 0.0, 0.0
            while need > 1e-12 and open_entries:
                take = min(open_entries[-1][0], need)
                took += take
                cost += take * open_entries[-1][1]
                open_entries[-1][0] -= take
                need -= take
                if open_entries[-1][0] <= 1e-12:
                    open_entries.pop()
            if took <= 0:
                continue
            entry_vwap = cost / took
            # Short sold at entry_vwap, bought back at price: the cost is the
            # adverse difference, so a positive number is money lost.
            out.append({
                "position_id": pid,
                "symbol": f["symbol"],
                "ts_ms": int(f["ts_ms"]),
                "qty": round(took, 8),
                "notional_usd": round(took * entry_vwap, 2),
                "entry_vwap": entry_vwap,
                "unwind_price": price,
                "cost_usd": round((price - entry_vwap) * took, 4),
                "cost_bps": round((price - entry_vwap) / entry_vwap * 10_000, 2),
            })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="*", help="output/basis_log_*.csv[.gz]")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--csv", default="", help="also write the per-unwind rows here")
    ap.add_argument("--window-min", type=float, default=5.0,
                    help="minutes of quotes before the unwind to average")
    ap.add_argument("--paper", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    events = unwind_events(conn, paper=args.paper)
    if not events:
        print("no unwinds recorded", file=sys.stderr)
        return

    paths: list[str] = []
    for pattern in args.logs:
        hits = sorted(glob.glob(pattern))
        paths.extend(hits or ([pattern] if Path(pattern).exists() else []))
    if paths:
        series = load_logs(paths, {e["symbol"] for e in events})
        for e in events:
            s = series.get(e["symbol"])
            q = quoted_before(s, e["ts_ms"], args.window_min, "entry") if s else None
            # The board's entry basis while the order rested, as a stand-in for
            # the hedge basis the executor computed at abort time — that number
            # is not stored, and this is what a human would have been looking at.
            e["abort_basis_bps"] = round(q[0], 2) if q else ""
    else:
        for e in events:
            e["abort_basis_bps"] = ""

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(events[0].keys()))
            w.writeheader()
            w.writerows(events)
        print(f"wrote {len(events)} rows to {out}", file=sys.stderr)

    costs = sorted(e["cost_bps"] for e in events)
    total = sum(e["cost_usd"] for e in events)
    print(f"\n{len(events)} unwinds, ${total:,.2f} total")
    print(f"  median cost {pctile(costs, 50):+8.1f} bps"
          f"   (p25 {pctile(costs, 25):+.1f}, p75 {pctile(costs, 75):+.1f})")
    print(f"  median clip "
          f"${pctile(sorted(e['notional_usd'] for e in events), 50):,.0f}")

    known = [e for e in events if e["abort_basis_bps"] != ""]
    if not known:
        print("\nNo basis logs supplied, so the abort basis is unknown — pass"
              " output/basis_log_*.csv* to get the comparison.", file=sys.stderr)
        return

    edges = [round(pctile(sorted(e["abort_basis_bps"] for e in known), p), 1)
             for p in (25, 50, 75)]
    hdr = (f"\n{'abort basis':<16}{'n':>5}{'med basis':>11}{'med cost':>10}"
           f"{'med $':>9}{'cheaper':>10}")
    print(hdr)
    print("-" * (len(hdr) - 1))
    for i in range(len(edges) + 1):
        lo = edges[i - 1] if i else float("-inf")
        hi = edges[i] if i < len(edges) else float("inf")
        g = [e for e in known if lo <= e["abort_basis_bps"] < hi]
        if not g:
            continue
        name = (f"<{edges[0]:g}" if i == 0 else
                f">={edges[-1]:g}" if i == len(edges) else
                f"{lo:g}..{hi:g}")
        med_b = pctile(sorted(e["abort_basis_bps"] for e in g), 50)
        med_c = pctile(sorted(e["cost_bps"] for e in g), 50)
        # Salvaging opens a position at med_b; unwinding realises med_c. The
        # comparison is only meaningful when the abort basis is NEGATIVE — a
        # positive one was never a loss to avoid in the first place.
        cheaper = "salvage" if med_c > max(-med_b, 0) else "unwind"
        print(f"{name:<16}{len(g):>5}{med_b:>11.1f}{med_c:>10.1f}"
              f"{pctile(sorted(e['cost_usd'] for e in g), 50):>9.2f}"
              f"{cheaper:>10}")
    print("-" * (len(hdr) - 1))
    print(
        "cost = what the buy-back gave up, in bps of the clip it reversed."
        " CERTAIN and realised."
    )
    print(
        "abort basis = what the board showed while the order rested, as a"
        " stand-in for the hedge basis the executor computed (not stored)."
        " Salvaging is NOT a loss of that: it leaves you holding a hedged"
        " position entered there, which funding and convergence can repay."
    )
    print(
        f"'cheaper' compares a certain cost against opening at that basis."
        f" ENTRY_HEDGE_MIN_BPS is currently"
        f" {float(config.ENTRY_HEDGE_MIN_BPS):+g} with a slack of"
        f" {float(config.ENTRY_HEDGE_SLIP_BPS):g} below the entry target:"
        " lower it past the band where salvage wins."
    )


if __name__ == "__main__":
    main()
