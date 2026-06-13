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
    levels: int = 5,
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
        entry = (a_ask - m_ask) / m_ask * BPS
        close = (a_bid - m_bid) / m_bid * BPS
        lines.append("")
        lines.append(
            f"entry basis {float(entry):+.1f}bps  close {float(close):+.1f}bps"
        )
        lines.append("entry=short perp ask vs buy spot ask")
    return "\n".join(lines)
