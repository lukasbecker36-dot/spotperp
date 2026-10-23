"""Live exchange reconciliation P&L (the /recon command).

Independent of the bot's position DB: it values whatever short-perp /
long-spot pairs are actually open on the venues right now (including
positions opened manually), using the same cost model the strategy
assumes — maker on Aster, taker on MEXC, both legs, entry and exit:

    net = perp_pnl + spot_pnl + funding
          - aster_maker_fees(entry+exit) - mexc_taker_fees(entry+exit)

Perp entry comes from Aster positionRisk (entryPrice); the exit is marked
at the Aster best bid (where a maker buy-back would rest). Aster re-prices
entryPrice when part of a position is bought back (average cost), booking
the difference as realised P&L, so the perp leg also adds the REALIZED_PNL
income on the symbol since the position opened. Spot entry is
reconstructed from MEXC myTrades (qty-weighted average of the buys that
built the current holding); the exit is marked at the MEXC best bid (a
taker sell). Funding is the realised FUNDING_FEE income summed from the
earliest matched spot buy (a proxy for the open time) to now.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import config

BPS = Decimal(10000)
_QTY_TOL = Decimal("0.02")  # 2% shortfall tolerance when matching held qty


def _dec(value, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def reconstruct_spot_entry(
    trades: list[dict], held_qty: Decimal
) -> tuple[Decimal, int | None, bool] | None:
    """Cost basis of the currently-held base quantity from MEXC myTrades.

    Walks BUY fills newest-first, accumulating until they cover held_qty.
    Returns (qty_weighted_avg_price, earliest_used_trade_ms, covered) or
    None if there are no buys. `covered` is False when the visible trade
    history doesn't reach back far enough to cover the whole holding.
    """
    buys = sorted(
        (t for t in trades if t.get("isBuyer")),
        key=lambda t: int(t.get("time", 0)),
        reverse=True,
    )
    if not buys:
        return None
    remaining = held_qty
    cost = Decimal(0)
    used = Decimal(0)
    earliest: int | None = None
    for t in buys:
        qty = _dec(t.get("qty"))
        if qty <= 0:
            continue
        take = min(qty, remaining) if remaining > 0 else Decimal(0)
        if take <= 0:
            break
        cost += take * _dec(t.get("price"))
        used += take
        earliest = int(t.get("time", 0)) or earliest
        remaining -= take
        if remaining <= 0:
            break
    if used <= 0:
        return None
    covered = remaining <= held_qty * _QTY_TOL
    return cost / used, earliest, covered


def average_cost_spot(
    trades: list[dict],
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Replay spot fills oldest-first at AVERAGE cost, as Aster books the perp:
    a sale realises (price - average) and leaves the average unchanged.
    Returns (held_qty, average_cost_of_held, realised_usd), or None if a sale
    exceeds what the window bought — the history does not start at the
    position's open, so its numbers would be invented."""
    held = cost = realised = Decimal(0)
    for t in trades:
        qty = _dec(t.get("qty"))
        price = _dec(t.get("price"))
        if qty <= 0:
            continue
        if t.get("isBuyer"):
            held += qty
            cost += qty * price
            continue
        if qty > held * (1 + _QTY_TOL):
            return None
        qty = min(qty, held)
        avg = cost / held if held > 0 else Decimal(0)
        realised += qty * (price - avg)
        cost -= qty * avg
        held -= qty
    if held <= 0:
        return held, Decimal(0), realised
    return held, cost / held, realised


@dataclass
class PairRecon:
    symbol: str
    base_asset: str
    perp_qty: Decimal          # contracts (magnitude; short)
    perp_entry: Decimal        # quote per contract
    perp_exit: Decimal         # maker buy-back at Aster bid
    spot_qty: Decimal          # matched base units
    spot_entry: Decimal        # quote per base unit
    spot_exit: Decimal         # taker sell at MEXC bid
    spot_entry_est: bool       # True if entry price is a fallback estimate
    funding_usd: Decimal
    held_hours: Decimal | None
    spot_balance: Decimal      # total base held on MEXC (free+locked)
    perp_base: Decimal         # perp_qty * qty_multiplier
    perp_mark: Decimal = Decimal(0)   # Aster mark price (liq triggers on this)
    perp_liq: Decimal = Decimal(0)    # Aster liquidation price (0 = none/unknown)
    funding_now_8h_bps: float = 0.0   # current funding rate, projected to 8h
    funding_avg_8h_bps: float = 0.0   # realised 24h funding, re-expressed per 8h
    # P&L Aster already REALISED on this position — reductions (the bot's
    # unwind buy-backs) close at the AVERAGE entry and re-price the rest, so
    # entryPrice alone misstates the trade. Part of perp P&L, not a cost.
    perp_realized_usd: Decimal = Decimal(0)
    # ...and the same for spot: coins already SOLD since the position opened
    # (scaling out), at average cost. Always set together with
    # perp_realized_usd — counting one leg's closed part without the other's
    # invents P&L (JCTUSDT scaled out: +635 perp realised, spot side ignored,
    # /recon NET +625).
    spot_realized_usd: Decimal = Decimal(0)

    @property
    def liq_distance_pct(self) -> Decimal | None:
        """How far the mark must move to liquidate the SHORT, as a % of mark.
        Liquidation is above the mark for a short, so this is positive; smaller
        = closer to liquidation. None if no liq/mark price is available."""
        if self.perp_mark <= 0 or self.perp_liq <= 0:
            return None
        return (self.perp_liq - self.perp_mark) / self.perp_mark * Decimal(100)

    @property
    def notional_usd(self) -> Decimal:
        """Current USD size of the perp leg (closeable value at the Aster bid)."""
        return self.perp_qty * self.perp_exit

    @property
    def perp_pnl(self) -> Decimal:
        """Open leg at Aster's entryPrice, plus what Aster has already
        realised on the same position."""
        return (
            (self.perp_entry - self.perp_exit) * self.perp_qty
            + self.perp_realized_usd
        )

    @property
    def spot_pnl(self) -> Decimal:
        return (
            (self.spot_exit - self.spot_entry) * self.spot_qty
            + self.spot_realized_usd
        )

    @property
    def perp_fees(self) -> Decimal:
        rate = config.ASTER_MAKER_FEE
        return rate * (self.perp_entry + self.perp_exit) * self.perp_qty

    @property
    def spot_fees(self) -> Decimal:
        rate = config.MEXC_TAKER_FEE
        return rate * (self.spot_entry + self.spot_exit) * self.spot_qty

    @property
    def fees(self) -> Decimal:
        return self.perp_fees + self.spot_fees

    @property
    def net_pnl(self) -> Decimal:
        return self.perp_pnl + self.spot_pnl + self.funding_usd - self.fees

    @property
    def hedge_imbalance(self) -> Decimal:
        """Signed base-unit gap: positive = excess spot, negative = naked perp."""
        return self.spot_balance - self.perp_base


def format_report(pairs: list[PairRecon], notes: list[str]) -> str:
    if not pairs and not notes:
        return "no matched perp/spot pairs open on the venues"
    lines: list[str] = ["live recon P&L (maker Aster / taker MEXC, in+out)"]
    total = Decimal(0)
    total_notional = Decimal(0)
    for p in pairs:
        total += p.net_pnl
        total_notional += p.notional_usd
        held = f"{float(p.held_hours):.1f}h" if p.held_hours is not None else "?"
        est = "~" if p.spot_entry_est else ""
        lines.append("")
        lines.append(f"{p.symbol}  (~${float(p.notional_usd):,.0f}, {held} held)")
        lines.append(
            f"  perp {_p(p.perp_entry)}->{_p(p.perp_exit)}"
            f"  {float(p.perp_pnl):+.2f}"
            + (f"  (incl. {float(p.perp_realized_usd):+.2f} realised on Aster)"
               if p.perp_realized_usd else "")
        )
        lines.append(
            f"  spot {est}{_p(p.spot_entry)}->{_p(p.spot_exit)}"
            f"  {float(p.spot_pnl):+.2f}"
            + (f"  (incl. {float(p.spot_realized_usd):+.2f} realised on MEXC)"
               if p.spot_realized_usd else "")
        )
        # Liquidation proximity for the short perp: mark now vs liq price above.
        dist = p.liq_distance_pct
        if dist is not None:
            warn = " ⚠️" if dist < config.LIQ_ALERT_PCT else ""
            lines.append(
                f"  mark {_p(p.perp_mark)}  liq {_p(p.perp_liq)}"
                f"  (+{float(dist):.1f}% to liq){warn}"
            )
        elif p.perp_mark > 0:
            lines.append(f"  mark {_p(p.perp_mark)}  liq n/a")
        lines.append(
            f"  funding {float(p.funding_usd):+.2f}"
            f"   fees -{float(p.fees):.2f}"
        )
        # Stored 8h-equivalent (the only way to compare 1h/4h/8h contracts),
        # shown per HOUR so it weighs directly against how long this position
        # has been held — same convention as /funding, /book and /screen.
        lines.append(
            f"  fund rate now {p.funding_now_8h_bps / 8:+.2f}"
            f" / 24h {p.funding_avg_8h_bps / 8:+.2f} bps/h"
        )
        lines.append(f"  NET {float(p.net_pnl):+.2f}")
        if abs(p.hedge_imbalance) > p.perp_base * _QTY_TOL:
            lines.append(
                f"  ⚠️ hedge imbalance {float(p.hedge_imbalance):+.4f} {p.base_asset}"
            )
    lines.append("")
    lines.append(
        f"TOTAL  ~${float(total_notional):,.0f} notional |"
        f" NET P&L ${float(total):+.2f}"
    )
    for note in notes:
        lines.append(note)
    return "\n".join(lines)


def _p(value: Decimal) -> str:
    """Compact price: trim trailing zeros, keep meaningful precision."""
    s = f"{value:.6f}".rstrip("0").rstrip(".")
    return s or "0"
