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


def format_book(
    symbol: str, mult: Decimal, aster_depth: dict, mexc_depth: dict,
    levels: int = 5, funding: dict | None = None, volume: dict | None = None,
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
        lines.append(f"entry basis  {float(entry):+7.1f}bps  (short perp ask / buy spot ask)")
        lines.append(f"exit passive {float(exit_passive):+7.1f}bps  (perp maker bid / sell spot bid)")
        lines.append(f"exit taker   {float(exit_taker):+7.1f}bps  (perp taker ask / sell spot bid)")
        lines.append(f"  taker exit crosses the perp spread: {float(exit_taker - exit_passive):.1f}bps worse")

    if funding:
        cur = funding.get("current_8h_bps")
        avg = funding.get("avg_24h_8h_bps")
        nxt = funding.get("next_funding_h")
        iv = funding.get("interval_hours")
        lines.append("")
        parts = []
        if cur is not None:
            parts.append(f"now {float(cur):+.1f}")
        if avg is not None:
            parts.append(f"24h avg {float(avg):+.1f}")
        lines.append(f"funding (8h-equiv, bps): {'  '.join(parts) if parts else 'n/a'}")
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
