"""Total account value across both venues, and its history.

Realised trade P&L (/pnl) only counts positions the bot opened and closed. It
cannot see funding that accrued on a position still open, a coin bought
outside the strategy, or margin sitting idle. Account value can: it is what
the two venues say you are worth right now, so its change over time is the
only number that captures everything.

    total = Aster perp account equity (margin + unrealised P&L)
          + MEXC spot coins marked to market
          + MEXC USDT

One caveat, stated wherever this is displayed: a change in account value is
P&L only if no money moved in or out. A deposit looks exactly like a profit
from the outside, so transfers have to be read alongside it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from exchange_client import ExchangeError


def _dec(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(default)
    except Exception:
        return Decimal(default)


@dataclass
class Equity:
    aster_usd: Decimal = Decimal(0)        # margin balance + unrealised P&L
    aster_margin_usd: Decimal = Decimal(0)  # the wallet part alone
    aster_upnl_usd: Decimal = Decimal(0)   # ...and the mark-to-market part
    spot_coins_usd: Decimal = Decimal(0)   # MEXC holdings ex-USDT, at the bid
    spot_usdt_usd: Decimal = Decimal(0)
    coins: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_usd(self) -> Decimal:
        return self.aster_usd + self.spot_coins_usd + self.spot_usdt_usd


async def snapshot(aster, mexc, mexc_books: dict) -> Equity:
    """Value both venues now. Never raises: a venue that fails to answer is
    reported in `errors` and contributes 0, so a partial snapshot is visibly
    partial rather than silently wrong."""
    eq = Equity()
    try:
        for b in await aster.balances():
            if b.get("asset") != "USDT":
                continue
            wallet = _dec(b.get("balance"))
            # Aster mirrors Binance: crossUnPnl is the open positions' mark-to
            # -market. marginBalance already includes it where present.
            upnl = _dec(b.get("crossUnPnl"))
            margin = _dec(b.get("marginBalance")) if b.get("marginBalance") else None
            eq.aster_margin_usd = wallet
            eq.aster_upnl_usd = upnl
            eq.aster_usd = margin if margin is not None else wallet + upnl
    except ExchangeError as exc:
        eq.errors.append(f"Aster balance: {exc}")

    try:
        acct = await mexc.account()
    except ExchangeError as exc:
        eq.errors.append(f"MEXC account: {exc}")
        return eq

    for b in acct.get("balances", []):
        asset = str(b.get("asset", ""))
        qty = _dec(b.get("free")) + _dec(b.get("locked"))
        if qty <= 0:
            continue
        if asset == "USDT":
            eq.spot_usdt_usd += qty
            continue
        book = mexc_books.get(f"{asset}USDT")
        # Mark at the BID: it is what the holding would fetch if sold, which is
        # the honest valuation for a position you intend to exit.
        price = book.bid if book is not None and book.bid > 0 else None
        if price is None:
            eq.unpriced.append(asset)
            continue
        value = qty * price
        eq.spot_coins_usd += value
        eq.coins.append((asset, qty, value))
    eq.coins.sort(key=lambda c: c[2], reverse=True)
    return eq


def sparkline(values: list[float], height: int = 7, width: int = 48) -> list[str]:
    """A small ASCII line chart. Deliberately dependency-free: a PNG would mean
    a plotting library on the server and an image upload path, for something
    that reads fine in the monospace block everything else already uses."""
    if len(values) < 2:
        return []
    # Downsample by averaging into `width` buckets so a long history still fits.
    if len(values) > width:
        step = len(values) / width
        buckets = []
        for i in range(width):
            lo, hi = int(i * step), max(int((i + 1) * step), int(i * step) + 1)
            chunk = values[lo:hi]
            buckets.append(sum(chunk) / len(chunk))
        values = buckets
    lo, hi = min(values), max(values)
    span = hi - lo
    if span <= 0:
        span = abs(hi) or 1.0
        lo = hi - span / 2
    rows = []
    for r in range(height, 0, -1):
        # Each row is one band of the value range: full block where the series
        # is above the band, half block where it sits inside it, blank above.
        top = lo + span * r / height
        bottom = lo + span * (r - 1) / height
        line = "".join(
            "█" if v >= top else ("▄" if v >= bottom else " ") for v in values
        )
        rows.append(f"{top:>10,.0f} " + line)
    return rows


def daily_series(rows: list) -> list[tuple[str, Decimal]]:
    """(UTC date, last total of that day) for each day present.

    The last snapshot of a day, not the average: a daily P&L table is a
    comparison of closing marks, and averaging would blur a day that moved.
    """
    by_day: dict[str, tuple[int, Decimal]] = {}
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.gmtime(r["ts_ms"] / 1000))
        prev = by_day.get(day)
        if prev is None or r["ts_ms"] >= prev[0]:
            by_day[day] = (r["ts_ms"], _dec(r["total_usd"]))
    return [(d, v) for d, (_, v) in sorted(by_day.items())]
