"""Order-book snapshot formatting for the /book command.

Shows the top N levels of both venues for a cross-listed symbol. Aster
perp prices are per contract-unit; they're normalised to MEXC base units
(divided by the pair's qty_multiplier) so the two books line up level for
level. Per-level notional ($) is shown for sizing and is multiplier-
invariant. A touch-basis line gives the executable entry/close basis.
"""
from __future__ import annotations

from decimal import Decimal

BPS = Decimal(10000)


def _levels(rows, n: int) -> list[tuple[Decimal, Decimal]]:
    out: list[tuple[Decimal, Decimal]] = []
    for row in (rows or [])[:n]:
        out.append((Decimal(str(row[0])), Decimal(str(row[1]))))
    return out


def _fmt_price(p: Decimal) -> str:
    s = f"{p:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def _fmt_notional(usd: Decimal) -> str:
    u = float(usd)
    if u >= 1e6:
        return f"${u / 1e6:.1f}M"
    if u >= 1e3:
        return f"${u / 1e3:.1f}k"
    return f"${u:.0f}"


def _block(title: str, asks, bids, div: Decimal) -> list[str]:
    lines = [title]
    for p, q in reversed(asks):  # worst ask first, best ask adjacent to spread
        lines.append(f"  ask {_fmt_price(p / div):>12} {_fmt_notional(p * q):>7}")
    lines.append("      " + "·" * 12)
    for p, q in bids:            # best bid first
        lines.append(f"  bid {_fmt_price(p / div):>12} {_fmt_notional(p * q):>7}")
    return lines


def _range_line(label: str, live: Decimal, lo, hi, thin: bool) -> str:
    """One basis line with the pair's own 24h range beside the live figure, and
    a word on where in that range the live figure sits.

    The live number alone cannot say whether it is a good level: +32 means one
    thing on a pair that ranged -48 to +1 today and another on one that ranged
    +30 to +60. Outside the range is the interesting case and reads opposite
    ways by side — above is a dislocation, below means the range is describing
    a regime that has ended.
    """
    if lo is None or hi is None:
        return f"{label} {float(live):+7.1f}bps"
    mark = "?" if thin else ""
    where = ""
    if not thin:
        if live > hi:
            where = "  ABOVE 24h range"
        elif live < lo:
            where = "  BELOW 24h range"
        elif hi > lo:
            pct = (float(live) - lo) / (hi - lo) * 100
            where = f"  {pct:.0f}% of range"
    return (
        f"{label} {float(live):+7.1f}bps   24h {lo:+.1f}{mark} to"
        f" {hi:+.1f}{mark}{where}"
    )


def format_book(
    symbol: str, mult: Decimal, aster_depth: dict, mexc_depth: dict,
    levels: int = 5, funding: dict | None = None, volume: dict | None = None,
    entry_range: tuple | None = None, exit_range: tuple | None = None,
    range_hours: float = 0.0, range_min_hours: float = 6.0,
    base_entry_range: tuple | None = None, base_exit_range: tuple | None = None,
    base_hours: float = 0.0, base_label: int = 72,
) -> str:
    a_asks = _levels(aster_depth.get("asks"), levels)
    a_bids = _levels(aster_depth.get("bids"), levels)
    m_asks = _levels(mexc_depth.get("asks"), levels)
    m_bids = _levels(mexc_depth.get("bids"), levels)

    lines = [f"{symbol} order book — top {levels}"]
    if mult != 1:
        lines.append(f"(Aster price shown ÷{mult} = base units)")
    lines.append("")
    if a_asks or a_bids:
        lines += _block("ASTER perp", a_asks, a_bids, mult)
    else:
        lines.append("ASTER perp: no levels")
    lines.append("")
    if m_asks or m_bids:
        lines += _block("MEXC spot", m_asks, m_bids, Decimal(1))
    else:
        lines.append("MEXC spot: no levels")

    if a_asks and a_bids and m_asks and m_bids:
        a_ask, a_bid = a_asks[0][0] / mult, a_bids[0][0] / mult
        m_ask, m_bid = m_asks[0][0], m_bids[0][0]
        # Premium trade: short perp + long spot; exit = buy back perp + sell spot.
        entry = (a_ask - m_ask) / m_ask * BPS          # short perp ask / buy spot ask
        exit_passive = (a_bid - m_bid) / m_bid * BPS   # perp maker bid / sell spot bid
        exit_taker = (a_ask - m_bid) / m_bid * BPS     # perp taker ask / sell spot bid
        lines.append("")
        e_lo, e_hi = entry_range or (None, None)
        x_lo, x_hi = exit_range or (None, None)
        thin = range_hours < range_min_hours
        lines.append(_range_line("entry basis ", entry, e_lo, e_hi, thin))
        lines.append("  short perp ask / buy spot ask")
        lines.append(_range_line("exit passive", exit_passive, x_lo, x_hi, thin))
        lines.append("  perp maker bid / sell spot bid")
        lines.append(f"exit taker   {float(exit_taker):+7.1f}bps")
        lines.append(
            "  perp taker ask / sell spot bid — crosses the perp spread:"
            f" {float(exit_taker - exit_passive):.1f}bps worse"
        )
        b_lo, b_hi = base_entry_range or (None, None)
        bx_lo, bx_hi = base_exit_range or (None, None)
        if b_lo is not None and base_hours >= range_min_hours * 2:
            lines.append(
                f"  {base_label}h   entry {b_lo:+.1f} to {b_hi:+.1f}"
                + (f"   exit {bx_lo:+.1f} to {bx_hi:+.1f}"
                   if bx_lo is not None else "")
            )
            # Where today's band sits inside the longer one. The useful case is
            # the 24h band having left the baseline entirely — that is the pair
            # moving to a new level, not a dislocation within its usual range,
            # and the two read completely differently.
            if e_lo is not None and not thin and b_hi > b_lo:
                if e_lo > b_hi:
                    lines.append(
                        f"  ⚠ today's whole range is ABOVE the {base_label}h"
                        " band — the pair has repriced, not dislocated"
                    )
                elif e_hi < b_lo:
                    lines.append(
                        f"  ⚠ today's whole range is BELOW the {base_label}h"
                        f" band — the {base_label}h figures describe a level"
                        " the pair has left"
                    )
                else:
                    pos = (e_lo - b_lo) / (b_hi - b_lo) * 100
                    lines.append(
                        f"  today sits {pos:.0f}% up the {base_label}h band"
                        + ("  (the cheap end of the period)" if pos < 25 else
                           "  (the rich end of the period)" if pos > 75 else "")
                    )
        if e_lo is not None and not thin:
            lines.append(
                f"  24h = p10/p90 of the HOURLY mean over {range_hours:.0f}h."
                " The exit range is tracked separately, not the entry range"
                " shifted — the gap between them is both books' live spread."
            )
        elif e_lo is not None:
            lines.append(
                f"  ? = only {range_hours:.0f}h of history"
                f" (need {range_min_hours:.0f}h), so those bounds are the live"
                " basis, not a range."
            )

    if funding:
        # Funding is stored 8h-equivalent (contracts settle on different
        # intervals, so normalising is the only way to compare them at all),
        # but a rate per HOUR is what you can weigh against a holding period.
        cur = funding.get("current_8h_bps")
        avg = funding.get("avg_24h_8h_bps")
        cur = None if cur is None else float(cur) / 8.0
        avg = None if avg is None else float(avg) / 8.0
        nxt = funding.get("next_funding_h")
        iv = funding.get("interval_hours")
        lines.append("")
        parts = []
        if cur is not None:
            parts.append(f"now {cur:+.2f}")
        if avg is not None:
            parts.append(f"24h avg {avg:+.2f}")
        lines.append(
            f"funding (bps per hour): {'  '.join(parts) if parts else 'n/a'}"
        )
        tail = []
        if iv:
            tail.append(f"settles every {iv}h")
        if nxt is not None and nxt >= 0:
            tail.append(f"next in {float(nxt):.1f}h")
        tail.append("+ = short receives")
        lines.append("  " + " · ".join(tail))

    if volume:
        qv = volume.get("quote_volume")
        tr = volume.get("trades")
        def _v(x):
            x = float(x or 0)
            if x >= 1e9:
                return f"${x / 1e9:.1f}B"
            if x >= 1e6:
                return f"${x / 1e6:.1f}M"
            if x >= 1e3:
                return f"${x / 1e3:.0f}k"
            return f"${x:.0f}"
        lines.append("")
        lines.append(
            f"perp 24h volume {_v(qv)} over {float(tr or 0):,.0f} trades"
        )
        lines.append(
            "  flow, not resting depth — this is what lifts a maker entry"
        )
    return "\n".join(lines)
