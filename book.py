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


def _where_in(live: Decimal, lo: float, hi: float) -> str:
    """Where a live figure sits in a band, or that it has left it."""
    if live > hi:
        return "ABOVE"
    if live < lo:
        return "BELOW"
    if hi <= lo:
        return ""
    return f"{(float(live) - lo) / (hi - lo) * 100:.0f}%"


def _side_block(
    label: str, live: Decimal, r24: tuple | None, rbase: tuple | None, *,
    thin: bool, base_ok: bool, base_label: int, desc: str, side: str,
) -> list[str]:
    """One basis side: the live figure, its 24h and longer bands, and what the
    two together say.

    Each side gets its own bands because they answer different questions. On
    the entry side the range says whether the basis is rich enough to sell
    into. On the exit side it is where an /exit can actually fill — and the
    longer band's low is the level a patient exit could reach, which the 24h
    low alone will understate.
    """
    out = [f"{label} {float(live):+7.1f}bps"]
    lo, hi = r24 or (None, None)
    if lo is None:
        out.append(f"  {desc}")
        return out
    mark = "?" if thin else ""
    where = "" if thin else _where_in(live, lo, hi)
    suffix = ("  ABOVE 24h range" if where == "ABOVE" else
              "  BELOW 24h range" if where == "BELOW" else
              f"  {where} of range" if where else "")
    out.append(f"  24h  {lo:+.1f}{mark} to {hi:+.1f}{mark}{suffix}")

    b_lo, b_hi = rbase or (None, None)
    if b_lo is None or not base_ok:
        out.append(f"  {desc}")
        return out
    bw = _where_in(live, b_lo, b_hi)
    btail = ("  ABOVE the period" if bw == "ABOVE" else
             "  BELOW the period" if bw == "BELOW" else
             f"  now {bw} up the period" if bw else "")
    out.append(f"  {base_label}h  {b_lo:+.1f} to {b_hi:+.1f}{btail}")

    if not thin and b_hi > b_lo:
        if lo > b_hi:
            out.append(f"  ⚠ today's whole range is ABOVE the {base_label}h"
                       " band — the pair has repriced, not dislocated")
        elif hi < b_lo:
            out.append(f"  ⚠ today's whole range is BELOW the {base_label}h"
                       f" band — the {base_label}h figures describe a level"
                       " the pair has left")
        elif side == "exit" and lo - b_lo >= 5.0:
            # The one number a passive exit most wants: today's low is not the
            # floor, the period's is. Waiting is worth this much more.
            out.append(f"  a patient exit has {lo - b_lo:.0f}bps more room than"
                       f" today's low — {base_label}h reached {b_lo:+.1f}")
    out.append(f"  {desc}")
    return out


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
        thin = range_hours < range_min_hours
        base_ok = base_hours >= range_min_hours * 2
        lines.append("")
        lines += _side_block(
            "entry basis ", entry, entry_range, base_entry_range,
            thin=thin, base_ok=base_ok, base_label=base_label,
            desc="short perp ask / buy spot ask", side="entry",
        )
        lines += _side_block(
            "exit passive", exit_passive, exit_range, base_exit_range,
            thin=thin, base_ok=base_ok, base_label=base_label,
            desc="perp maker bid / sell spot bid", side="exit",
        )
        lines.append(f"exit taker   {float(exit_taker):+7.1f}bps")
        lines.append(
            "  perp taker ask / sell spot bid — crosses the perp spread:"
            f" {float(exit_taker - exit_passive):.1f}bps worse"
        )
        if (entry_range or (None,))[0] is not None and not thin:
            shown = (f"24h/{base_label}h" if base_ok
                     and (base_entry_range or (None,))[0] is not None else "24h")
            lines.append(
                f"  {shown} = p10/p90 of the HOURLY mean. Entry and exit"
                " ranges are tracked separately, not one shifted from the"
                " other — the gap between them is both books' live spread."
            )
        elif (entry_range or (None,))[0] is not None:
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
