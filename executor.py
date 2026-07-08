"""Entry/exit execution: maker leg on Aster with reprice loop, taker hedge on
MEXC, leg-risk unwind, paper/live parity.

Entry (premium trade):
    1. Rest a GTX (post-only) SELL on the Aster perp joined to the best ask,
       cancel/replacing as the ask moves (rate-limited).
    2. Every fill increment is immediately hedged with a MEXC spot IOC-limit
       BUY capped HEDGE_SLIPPAGE_BPS past the ask (exact base-qty control —
       MEXC market BUY only accepts quoteOrderQty).
    3. If the hedge cannot fill, the naked perp fill is unwound with an IOC
       reduce-only buy-back and the operator is alerted.

Exit modes:
    "now"     — taker close on both legs concurrently.
    "passive" — mirror of entry: GTX BUY on Aster at the bid (optionally only
                while closeable basis <= target), spot sold IOC on each fill.
                Passive exits do NOT auto-escalate; safety stops still apply.

Paper mode uses the same state machines with simulated fills: maker orders
fill when the opposite touch crosses our price, takers fill at the touch.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import config
import intents
import position_manager as pm
from database import journal
from exchange_client import (
    AmbiguousOrderError,
    AsterClient,
    BookTicker,
    ExchangeError,
    MexcClient,
    OrderResult,
    SymbolInfo,
)
from notify import Notifier
from screener import PairMap, BPS

log = logging.getLogger(__name__)


@dataclass
class MarketData:
    """Shared quote cache, refreshed by the live monitor's fast tick."""

    aster_books: dict[str, BookTicker] = field(default_factory=dict)
    mexc_books: dict[str, BookTicker] = field(default_factory=dict)
    funding: dict[str, dict] = field(default_factory=dict)
    funding_stats: dict[str, object] = field(default_factory=dict)  # sym -> FundingStat
    pair_maps: dict[str, PairMap] = field(default_factory=dict)
    aster_info: dict[str, SymbolInfo] = field(default_factory=dict)
    mexc_info: dict[str, SymbolInfo] = field(default_factory=dict)


@dataclass
class TakerFill:
    qty: Decimal
    avg_price: Decimal


class Trader:
    """Order operations shared by paper and live implementations."""

    async def place_perp_maker(
        self, symbol: str, side: str, qty: Decimal, price: Decimal, client_id: str
    ) -> str:
        raise NotImplementedError

    async def poll_perp_order(self, symbol: str, order_id: str) -> OrderResult:
        raise NotImplementedError

    async def cancel_perp_order(self, symbol: str, order_id: str) -> OrderResult:
        raise NotImplementedError

    async def perp_taker(
        self, symbol: str, side: str, qty: Decimal, cap_price: Decimal, reduce_only: bool
    ) -> TakerFill:
        raise NotImplementedError

    async def spot_taker(
        self, symbol: str, side: str, qty: Decimal, cap_price: Decimal
    ) -> TakerFill:
        raise NotImplementedError

    async def spot_depth(
        self, symbol: str, side: str, limit: int = 20
    ) -> list[tuple[Decimal, Decimal]]:
        """Resting (price, qty) levels on the side a taker would hit:
        BUY hits asks (ascending), SELL hits bids (descending)."""
        raise NotImplementedError

    async def ensure_perp_margin(
        self, symbol: str, leverage: int, margin_type: str
    ) -> str | None:
        """Set the Aster perp's margin type and leverage. Returns None on
        success (incl. already-set), or an error message."""
        raise NotImplementedError


def max_hedgeable_qty(
    perp_norm_price: Decimal,
    asks: list[tuple[Decimal, Decimal]],
    floor_bps: Decimal,
) -> Decimal:
    """Largest base quantity buyable across `asks` (ascending price) whose
    volume-weighted fill price keeps the entry basis at or above floor_bps:

        (perp_norm_price - vwap) / vwap * 1e4 >= floor_bps

    The perp leg fills at one resting price (perp_norm_price, already
    normalised to base units); the spot taker walks the book, so the VWAP
    rises and the basis falls monotonically as size grows. Returns the base
    quantity at the exact point the floor binds (partial of the breaking
    level included). 0 if even the best ask fails the floor.
    """
    if perp_norm_price <= 0 or not asks:
        return Decimal(0)
    vwap_max = perp_norm_price / (1 + floor_bps / BPS)
    if vwap_max <= 0:
        return Decimal(0)
    cum_qty = Decimal(0)
    cum_cost = Decimal(0)
    for price, qty in asks:
        if qty <= 0:
            continue
        if price <= vwap_max:
            cum_qty += qty
            cum_cost += price * qty
            continue
        # This level breaks the floor: take the partial that lifts the VWAP
        # to exactly vwap_max, then stop.
        num = vwap_max * cum_qty - cum_cost
        denom = price - vwap_max
        if denom > 0 and num > 0:
            cum_qty += min(num / denom, qty)
        break
    return cum_qty


def max_closeable_qty(
    perp_norm_price: Decimal,
    bids: list[tuple[Decimal, Decimal]],
    target_bps: Decimal,
) -> Decimal:
    """Mirror of max_hedgeable_qty for a passive exit: largest base quantity
    sellable across `bids` (descending price) whose volume-weighted fill price
    keeps the CLOSE basis at or below target_bps:

        (perp_norm_price - vwap) / vwap * 1e4 <= target_bps

    The perp buy-back fills at one resting price (perp_norm_price, the bid
    normalised to base units); selling spot deeper lowers the VWAP and raises
    the close basis, so there is a single max size. Returns the base quantity
    where the target binds (partial of the breaking level included). 0 if even
    the best bid would close above target.
    """
    if perp_norm_price <= 0 or not bids:
        return Decimal(0)
    vwap_min = perp_norm_price / (1 + target_bps / BPS)
    if vwap_min <= 0:
        return Decimal(0)
    cum_qty = Decimal(0)
    cum_cost = Decimal(0)
    for price, qty in bids:
        if qty <= 0:
            continue
        if price >= vwap_min:
            cum_qty += qty
            cum_cost += price * qty
            continue
        # This level would pull the VWAP below vwap_min (basis above target):
        # take the partial that lands the VWAP exactly at vwap_min, then stop.
        num = vwap_min * cum_qty - cum_cost  # <= 0 (prior prices >= vwap_min)
        denom = price - vwap_min             # < 0
        if denom < 0 and num <= 0:
            cum_qty += min(num / denom, qty)
        break
    return cum_qty



class LiveTrader(Trader):
    def __init__(self, aster: AsterClient, mexc: MexcClient, conn):
        self._aster = aster
        self._mexc = mexc
        self._conn = conn

    async def place_perp_maker(self, symbol, side, qty, price, client_id) -> str:
        intent_id = intents.record_intent(
            self._conn,
            None,
            "aster",
            "place",
            {"symbol": symbol, "side": side, "qty": qty, "price": price,
             "client_order_id": client_id, "tif": "GTX"},
        )
        try:
            result = await self._aster.place_order(
                symbol,
                side,
                "LIMIT",
                quantity=qty,
                price=price,
                time_in_force="GTX",
                client_order_id=client_id,
            )
        except AmbiguousOrderError:
            intents.resolve_intent(self._conn, intent_id, "ambiguous")
            raise
        except ExchangeError as exc:
            intents.resolve_intent(self._conn, intent_id, "failed", str(exc))
            raise
        intents.resolve_intent(self._conn, intent_id, "done", result.raw)
        return result.order_id

    async def poll_perp_order(self, symbol, order_id) -> OrderResult:
        return await self._aster.get_order(symbol, order_id)

    async def cancel_perp_order(self, symbol, order_id) -> OrderResult:
        try:
            await self._aster.cancel_order(symbol, order_id)
        except ExchangeError as exc:
            # Already filled/cancelled is fine; final status comes from get_order.
            log.info("cancel %s %s: %s", symbol, order_id, exc)
        return await self._aster.get_order(symbol, order_id)

    async def perp_taker(self, symbol, side, qty, cap_price, reduce_only) -> TakerFill:
        intent_id = intents.record_intent(
            self._conn,
            None,
            "aster",
            "place",
            {"symbol": symbol, "side": side, "qty": qty, "price": cap_price,
             "tif": "IOC", "reduce_only": reduce_only},
        )
        try:
            result = await self._aster.place_order(
                symbol,
                side,
                "LIMIT",
                quantity=qty,
                price=cap_price,
                time_in_force="IOC",
                reduce_only=reduce_only,
            )
        except AmbiguousOrderError:
            intents.resolve_intent(self._conn, intent_id, "ambiguous")
            raise
        except ExchangeError as exc:
            intents.resolve_intent(self._conn, intent_id, "failed", str(exc))
            raise
        intents.resolve_intent(self._conn, intent_id, "done", result.raw)
        return TakerFill(qty=result.executed_qty, avg_price=result.avg_price)

    async def spot_taker(self, symbol, side, qty, cap_price) -> TakerFill:
        intent_id = intents.record_intent(
            self._conn,
            None,
            "mexc",
            "place",
            {"symbol": symbol, "side": side, "qty": qty, "price": cap_price,
             "type": "IMMEDIATE_OR_CANCEL"},
        )
        try:
            result = await self._mexc.place_order(
                symbol,
                side,
                "IMMEDIATE_OR_CANCEL",
                quantity=qty,
                price=cap_price,
            )
        except AmbiguousOrderError:
            intents.resolve_intent(self._conn, intent_id, "ambiguous")
            raise
        except ExchangeError as exc:
            intents.resolve_intent(self._conn, intent_id, "failed", str(exc))
            raise
        # MEXC place responses can omit executed qty; query for the final state.
        # If that re-query fails we CANNOT tell a filled IOC from an empty one,
        # so surface it as ambiguous rather than reporting qty=0 (which would
        # make the hedge loop re-buy a possibly-filled order = double size).
        if result.executed_qty == 0 and result.order_id:
            try:
                result = await self._mexc.get_order(symbol, result.order_id)
            except ExchangeError as exc:
                intents.resolve_intent(self._conn, intent_id, "ambiguous")
                raise AmbiguousOrderError(
                    "mexc",
                    f"order {result.order_id} placed but fill state unknown ({exc})",
                )
        intents.resolve_intent(self._conn, intent_id, "done", result.raw)
        return TakerFill(qty=result.executed_qty, avg_price=result.avg_price)

    async def spot_depth(self, symbol, side, limit=20):
        data = await self._mexc.depth(symbol, limit)
        key = "asks" if side == "BUY" else "bids"
        return [
            (Decimal(str(p)), Decimal(str(q))) for p, q in data.get(key, [])
        ]

    async def ensure_perp_margin(self, symbol, leverage, margin_type):
        try:
            await self._aster.set_margin_type(symbol, margin_type)
        except ExchangeError as exc:
            return f"marginType: {exc}"
        try:
            await self._aster.set_leverage(symbol, leverage)
        except ExchangeError as exc:
            return f"leverage: {exc}"
        return None


@dataclass
class _PaperOrder:
    symbol: str
    side: str
    price: Decimal
    qty: Decimal
    executed: Decimal = Decimal(0)
    status: str = "NEW"


class PaperTrader(Trader):
    """Simulated fills against the live quote cache.

    Maker orders fill immediately at their resting price (we assume the
    market trades through our level). This lets paper mode test the full
    entry/exit/hedge pipeline without waiting for a real book cross.
    """

    def __init__(self, md: MarketData):
        self._md = md
        self._orders: dict[str, _PaperOrder] = {}

    def _to_result(self, order_id: str, order: _PaperOrder) -> OrderResult:
        return OrderResult(
            venue="paper",
            symbol=order.symbol,
            order_id=order_id,
            client_order_id=order_id,
            side=order.side,
            status=order.status,
            price=order.price,
            orig_qty=order.qty,
            executed_qty=order.executed,
            avg_price=order.price if order.executed else Decimal(0),
        )

    async def place_perp_maker(self, symbol, side, qty, price, client_id) -> str:
        order_id = f"paper-{uuid.uuid4().hex[:10]}"
        order = _PaperOrder(symbol, side, price, qty)
        order.executed = qty
        order.status = "FILLED"
        self._orders[order_id] = order
        return order_id

    async def poll_perp_order(self, symbol, order_id) -> OrderResult:
        return self._to_result(order_id, self._orders[order_id])

    async def cancel_perp_order(self, symbol, order_id) -> OrderResult:
        return self._to_result(order_id, self._orders[order_id])

    async def perp_taker(self, symbol, side, qty, cap_price, reduce_only) -> TakerFill:
        book = self._md.aster_books.get(symbol)
        if book is None:
            return TakerFill(Decimal(0), Decimal(0))
        price = book.ask if side == "BUY" else book.bid
        return TakerFill(qty=qty, avg_price=price)

    async def spot_taker(self, symbol, side, qty, cap_price) -> TakerFill:
        book = self._md.mexc_books.get(symbol)
        if book is None:
            return TakerFill(Decimal(0), Decimal(0))
        price = book.ask if side == "BUY" else book.bid
        return TakerFill(qty=qty, avg_price=price)

    async def spot_depth(self, symbol, side, limit=20):
        # Paper has only top-of-book; expose it as a single level so the
        # hedgeable-size cap behaves the same way live does at the touch.
        book = self._md.mexc_books.get(symbol)
        if book is None:
            return []
        return [(book.ask, book.ask_qty)] if side == "BUY" else [
            (book.bid, book.bid_qty)
        ]

    async def ensure_perp_margin(self, symbol, leverage, margin_type):
        return None  # no real account in paper mode


class Executor:
    def __init__(
        self,
        md: MarketData,
        trader: Trader,
        positions: pm.PositionManager,
        notifier: Notifier,
        conn,
        *,
        paper: bool,
    ):
        self._md = md
        self._trader = trader
        self._positions = positions
        self._notifier = notifier
        self._conn = conn
        self._paper = paper
        self._tasks: dict[int, asyncio.Task] = {}
        self._cancel_requested: set[int] = set()
        self._margin_configured: set[str] = set()  # Aster symbols set to 1x isolated

    # ── public API (called by the live monitor's command poller) ──

    def start_entry(self, position: pm.Position) -> None:
        self._spawn(position.id, self._run_entry(position))

    def start_add(self, position: pm.Position, add_notional: Decimal) -> None:
        """Size up an already-OPEN position: work another maker entry for the
        incremental notional, folding the fills into the same position."""
        self._spawn(position.id, self._run_entry(position, add_notional=add_notional))

    async def start_exit(self, position: pm.Position) -> None:
        await self._cancel_task(position.id)
        self._spawn(position.id, self._run_exit(position))

    async def start_spot_rebalance(
        self, position: pm.Position, floor_perp: Decimal
    ) -> None:
        """Sell the position's UNHEDGED spot down to floor_perp-worth (the perp
        still alive on the venue after an ADL/liquidation/manual reduction) in
        small tranches. floor_perp=0 closes the spot leg entirely."""
        await self._cancel_task(position.id)
        self._spawn(position.id, self._run_spot_selldown(position, floor_perp))

    async def _run_spot_selldown(
        self, position: pm.Position, floor_perp: Decimal
    ) -> None:
        """Tranche seller for a broken hedge: ADL_SELL_TRANCHE_PCT of the excess
        every ADL_SELL_INTERVAL_SECONDS (one market clip into a just-plunged
        microcap book would eat it). The DB perp side has already been
        reconciled to the venue by the caller; on restart mid-way, the position
        is EXITING with exit_mode 'now' + floor, so _resume_positions resumes a
        normal aggressive exit that finishes the job in one clip."""
        symbol = position.symbol
        pair = self._pair(symbol)
        mexc_info = self._md.mexc_info[pair.mexc_symbol]
        self._positions.set_state(position.id, pm.EXITING)
        floor_spot = mexc_info.round_qty(floor_perp * pair.qty_multiplier)
        pos = self._positions.get(position.id)
        initial_excess = pos.spot_qty - floor_spot
        tranche = mexc_info.round_qty(
            initial_excess * config.ADL_SELL_TRANCHE_PCT / Decimal(100)
        )
        journal(self._conn, f"position {position.id}: spot sell-down of"
                f" {initial_excess} to floor {floor_spot} (tranche {tranche})")
        deadline = time.monotonic() + config.EXIT_TIMEOUT_MINUTES * 60
        while time.monotonic() < deadline:
            pos = self._positions.get(position.id)
            remaining = pos.spot_qty - floor_spot
            if mexc_info.round_qty(remaining) <= 0:
                break
            qty = min(tranche, remaining) if tranche > 0 else remaining
            # A sub-min-notional tranche (or tail) can't be sold on its own —
            # fold it into the remainder in one final clip.
            book = self._md.mexc_books.get(pair.mexc_symbol)
            ref = book.bid if book and book.bid > 0 else Decimal(0)
            if ref > 0 and remaining * ref <= mexc_info.min_notional * 2:
                qty = remaining
            try:
                shortfall, err = await self._hedge_spot(
                    position, "SELL", qty, "exit"
                )
            except AmbiguousOrderError as exc:
                await self._notifier.alert(
                    f"🚨 position {position.id} {symbol}: spot sell-down tranche"
                    f" AMBIGUOUS ({exc}) — stopping; reconcile the spot balance"
                    f" manually, then /exit or /remove"
                )
                return   # leave EXITING; operator decides
            if shortfall > 0 and err:
                log.warning("sell-down tranche short %s: %s", shortfall, err)
            await asyncio.sleep(config.ADL_SELL_INTERVAL_SECONDS)
        await self._complete_exit(position.id, floor_perp)

    async def _cancel_task(self, position_id: int) -> None:
        """Cancel a running task for this position and AWAIT its cleanup before
        returning, so a replacement can't run concurrently with the old task's
        CancelledError handler (which cancels resting orders and sells pending
        spot) — that race could double-sell spot or drive perp_qty negative."""
        existing = self._tasks.get(position_id)
        if existing and not existing.done():
            existing.cancel()
            try:
                await existing
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("cancelled task for position %s ended in error",
                              position_id)

    def request_cancel(self, position_id: int) -> bool:
        """Abort a working ENTRY (no new fills; what's hedged stays open)."""
        task = self._tasks.get(position_id)
        if task and not task.done():
            self._cancel_requested.add(position_id)
            return True
        return False

    def has_task(self, position_id: int) -> bool:
        task = self._tasks.get(position_id)
        return bool(task and not task.done())

    def _spawn(self, position_id: int, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks[position_id] = task
        task.add_done_callback(lambda t: self._on_done(position_id, t))

    def _on_done(self, position_id: int, task: asyncio.Task) -> None:
        self._cancel_requested.discard(position_id)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.exception("executor task for position %s failed", position_id,
                          exc_info=exc)
            journal(self._conn, f"position {position_id}: executor error {exc!r}", "ERROR")

    # ── helpers ──

    def _pair(self, symbol: str) -> PairMap:
        return self._md.pair_maps[symbol]

    def _books(self, symbol: str) -> tuple[BookTicker | None, BookTicker | None]:
        # A symbol can drop out of the universe (delist/halt) while a position is
        # still open; degrade to (None, None) rather than KeyError so the safety
        # loop and marks skip it instead of crashing.
        pair = self._md.pair_maps.get(symbol)
        if pair is None:
            return (None, None)
        return (
            self._md.aster_books.get(pair.aster_symbol),
            self._md.mexc_books.get(pair.mexc_symbol),
        )

    def _books_fresh(self, symbol: str) -> bool:
        """Both venues quoted within QUOTE_STALE_SECONDS. Guards safety auto-
        closes from firing on a frozen book (e.g. a MEXC REST outage leaving the
        last snapshot in place while the market moves)."""
        aster, mexc = self._books(symbol)
        if aster is None or mexc is None:
            return False
        stale_ms = config.QUOTE_STALE_SECONDS * 1000
        now_ms = int(time.time() * 1000)
        return (now_ms - aster.ts_ms) <= stale_ms and (now_ms - mexc.ts_ms) <= stale_ms

    def _entry_basis_bps(self, symbol: str) -> Decimal | None:
        aster, mexc = self._books(symbol)
        if aster is None or mexc is None:
            return None
        mult = self._pair(symbol).qty_multiplier
        return (aster.ask / mult - mexc.ask) / mexc.ask * BPS

    async def _live_hedge_basis_bps(
        self, perp_price: Decimal, mexc_symbol: str, base_qty: Decimal,
        mult: Decimal,
    ) -> Decimal | None:
        """Executable entry basis the spot hedge would ACTUALLY realise: the
        perp fill price vs the VWAP of buying base_qty by walking fresh MEXC
        asks. None if depth is unavailable. Unlike the placement-time gate this
        re-prices against the book at the moment of hedging, catching the spot
        drift / depth-walk that adverse-selects a resting maker entry."""
        try:
            asks = await self._trader.spot_depth(mexc_symbol, "BUY")
        except ExchangeError:
            asks = []
        if not asks or base_qty <= 0:
            return None
        cum = Decimal(0)
        cost = Decimal(0)
        for px, qty in asks:
            take = min(qty, base_qty - cum)
            cost += take * px
            cum += take
            if cum >= base_qty:
                break
        if cum <= 0:
            return None
        vwap = cost / cum
        return (perp_price / mult - vwap) / vwap * BPS

    def _close_basis_bps(self, symbol: str) -> Decimal | None:
        aster, mexc = self._books(symbol)
        if aster is None or mexc is None:
            return None
        mult = self._pair(symbol).qty_multiplier
        return (aster.bid / mult - mexc.bid) / mexc.bid * BPS

    def _fee_usd(self, venue: str, maker: bool, qty: Decimal, price: Decimal) -> Decimal:
        if venue == "aster":
            rate = config.ASTER_MAKER_FEE if maker else config.ASTER_TAKER_FEE
        else:
            rate = config.MEXC_MAKER_FEE if maker else config.MEXC_TAKER_FEE
        return qty * price * rate

    async def _hedge_cap(
        self, mexc_symbol: str, side: str, base_qty: Decimal, info: SymbolInfo
    ) -> Decimal | None:
        """IOC limit price that crosses enough *live* depth to fill base_qty,
        plus a buffer. The hedge must fill to keep the position neutral, so we
        price it to cross the real book up to the needed size rather than the
        cached touch + a thin buffer — a fast microcap blows straight through
        the latter (PLAY: cached ask + 10bps sat below the real ask, so the
        IOC crossed nothing). Falls back to touch + buffer if depth is gone.
        """
        buf = config.HEDGE_SLIPPAGE_BPS / BPS
        try:
            levels = await self._trader.spot_depth(mexc_symbol, side)
        except ExchangeError:
            levels = []
        ref: Decimal | None = None
        cum = Decimal(0)
        for price, qty in levels:
            cum += qty
            ref = price
            if cum >= base_qty:
                break  # this level completes the fill
        if ref is None:
            book = self._md.mexc_books.get(mexc_symbol)
            if book is None:
                return None
            ref = book.ask if side == "BUY" else book.bid
        if side == "BUY":
            return info.round_price(ref * (1 + buf), up=True)
        return info.round_price(ref * (1 - buf), up=False)

    async def _hedge_spot(
        self, position: pm.Position, side: str, base_qty: Decimal, phase: str
    ) -> tuple[Decimal, str | None]:
        """Buy (entry) or sell (exit) spot for a perp fill increment.

        Returns (unfilled_remainder_in_base_units, last_error). The error is
        the most recent MEXC rejection message (None if the only problem was
        thin liquidity / partial fills), so callers can surface the real
        reason instead of a generic "hedge failed".
        """
        pair = self._pair(position.symbol)
        info = self._md.mexc_info[pair.mexc_symbol]
        remaining = info.round_qty(base_qty)
        if remaining <= 0:
            return Decimal(0), None
        last_error: str | None = None
        for attempt in range(config.HEDGE_RETRY_ATTEMPTS):
            cap = await self._hedge_cap(pair.mexc_symbol, side, remaining, info)
            if cap is None:
                last_error = "no MEXC book/depth to price the hedge"
                await asyncio.sleep(1)
                continue
            try:
                fill = await self._trader.spot_taker(
                    pair.mexc_symbol, side, remaining, cap
                )
            except AmbiguousOrderError:
                # The order may have filled — retrying would double it. Propagate
                # so the caller alerts and leaves reconciliation to recovery /
                # the operator rather than blindly unwinding or re-sending.
                raise
            except ExchangeError as exc:
                last_error = str(exc)
                log.warning("spot hedge attempt %s failed: %s", attempt + 1, exc)
                await asyncio.sleep(1)
                continue
            if fill.qty > 0:
                self._positions.record_fill(
                    position.id,
                    "mexc",
                    phase,
                    side,
                    fill.qty,
                    fill.avg_price,
                    self._fee_usd("mexc", False, fill.qty, fill.avg_price),
                )
                remaining = info.round_qty(remaining - fill.qty)
                last_error = None  # progress made; not an outright rejection
            if remaining <= 0:
                return Decimal(0), None
            await asyncio.sleep(0.5)
        return remaining, last_error

    async def _unwind_perp(self, position: pm.Position, qty: Decimal) -> None:
        """Buy back a naked perp fill (leg risk). qty in Aster contract units."""
        pair = self._pair(position.symbol)
        info = self._md.aster_info[pair.aster_symbol]
        qty = info.round_qty(qty)
        if qty <= 0:
            return
        deadline = time.monotonic() + config.UNWIND_TIMEOUT_SECONDS
        remaining = qty
        while remaining > 0 and time.monotonic() < deadline:
            book = self._md.aster_books.get(pair.aster_symbol)
            if book is None:
                await asyncio.sleep(1)
                continue
            cap = info.round_price(
                book.ask * (1 + config.HEDGE_SLIPPAGE_BPS / BPS), up=True
            )
            try:
                fill = await self._trader.perp_taker(
                    pair.aster_symbol, "BUY", remaining, cap, True
                )
            except ExchangeError as exc:
                log.warning("unwind attempt failed: %s", exc)
                await asyncio.sleep(1)
                continue
            if fill.qty > 0:
                self._positions.record_fill(
                    position.id,
                    "aster",
                    "unwind",
                    "BUY",
                    fill.qty,
                    fill.avg_price,
                    self._fee_usd("aster", False, fill.qty, fill.avg_price),
                )
                remaining = info.round_qty(remaining - fill.qty)
        if remaining > 0:
            await self._notifier.alert(
                f"⚠️ position {position.id} {position.symbol}: unwind INCOMPLETE,"
                f" {remaining} contracts still naked — manual intervention needed"
            )

    # ── entry ──

    async def _ensure_margin(self, position: pm.Position, aster_symbol: str) -> None:
        """Set 1x isolated on the Aster perp before the first live order on a
        symbol. Best-effort: alert and proceed on failure (a small entry is
        not worth blocking, but the operator must know it isn't at 1x)."""
        if self._paper or aster_symbol in self._margin_configured:
            return
        err = await self._trader.ensure_perp_margin(
            aster_symbol, config.ASTER_LEVERAGE, config.ASTER_MARGIN_TYPE
        )
        if err is None:
            self._margin_configured.add(aster_symbol)
            journal(
                self._conn,
                f"position {position.id}: Aster {aster_symbol} set"
                f" {config.ASTER_MARGIN_TYPE} {config.ASTER_LEVERAGE}x",
            )
        else:
            await self._notifier.alert(
                f"⚠️ position {position.id} {position.symbol}: could NOT set"
                f" {config.ASTER_MARGIN_TYPE} {config.ASTER_LEVERAGE}x on Aster"
                f" ({err}) — check margin/leverage manually before relying on it"
            )

    async def _run_entry(
        self, position: pm.Position, *, add_notional: Decimal | None = None
    ) -> None:
        symbol = position.symbol
        pair = self._pair(symbol)
        aster_info = self._md.aster_info[pair.aster_symbol]
        # An add works on a position that already holds exposure: target only
        # the incremental notional, and on a no-fill outcome leave the prior
        # exposure OPEN rather than CANCELling it.
        is_add = add_notional is not None
        leg_notional = add_notional if is_add else position.target_notional
        had_exposure = position.perp_qty > 0 or position.spot_qty > 0
        self._positions.set_state(position.id, pm.ENTERING)
        journal(self._conn, f"position {position.id}:"
                f" {'adding to' if is_add else 'entering'} {symbol}"
                f" notional={leg_notional}")
        await self._ensure_margin(position, pair.aster_symbol)

        aster_book, _ = self._books(symbol)
        if aster_book is None:
            if had_exposure:
                self._positions.set_state(position.id, pm.OPEN, "add aborted: no quotes")
            else:
                self._positions.set_state(position.id, pm.CANCELLED, "no quotes")
            return
        total_qty = aster_info.round_qty(leg_notional / aster_book.ask)
        if total_qty <= 0:
            if had_exposure:
                self._positions.set_state(
                    position.id, pm.OPEN, "add below lot size"
                )
            else:
                self._positions.set_state(
                    position.id, pm.CANCELLED, "notional below lot size"
                )
            return

        entry_floor = (
            position.min_entry_bps if position.min_entry_bps is not None
            else config.ENTRY_MIN_EDGE_FLOOR_BPS
        )
        deadline = time.monotonic() + config.ENTRY_TIMEOUT_MINUTES * 60
        remaining = total_qty
        run_filled = Decimal(0)        # perp contracts filled by THIS run (add-aware)
        unhedged = Decimal(0)          # perp filled, spot not yet bought
        order_id: str | None = None
        order_price = Decimal(0)
        order_qty = Decimal(0)         # size the resting maker was placed with
        order_seen_executed = Decimal(0)
        last_reprice = 0.0
        aborted = False                # hedge-time basis collapse -> stop entry

        async def absorb_fills(result: OrderResult) -> None:
            nonlocal remaining, unhedged, order_seen_executed, run_filled
            delta = result.executed_qty - order_seen_executed
            if delta <= 0:
                return
            order_seen_executed = result.executed_qty
            remaining -= delta
            run_filled += delta
            price = result.avg_price if result.avg_price > 0 else result.price
            self._positions.record_fill(
                position.id, "aster", "entry", "SELL", delta, price,
                self._fee_usd("aster", True, delta, price), result.order_id,
            )
            unhedged += delta

        async def hedge_unhedged(force: bool = False) -> None:
            nonlocal unhedged, aborted
            if unhedged <= 0:
                return
            book = self._md.mexc_books.get(pair.mexc_symbol)
            ref_price = book.ask if book else Decimal(1)
            notional = unhedged * pair.qty_multiplier * ref_price
            if not force and notional < config.MIN_HEDGE_NOTIONAL_USD:
                return  # accumulate dust

            # Re-price the basis the hedge will actually pay against fresh spot
            # depth. A resting maker fills adverse-selected (spot has rallied,
            # basis compressed); if it has collapsed below the floor by more
            # than the abort band, don't lock a bad entry — unwind this perp
            # increment instead. order_price is where the perp leg filled.
            perp_ref = order_price if order_price > 0 else ref_price
            live_basis = await self._live_hedge_basis_bps(
                perp_ref, pair.mexc_symbol,
                unhedged * pair.qty_multiplier, pair.qty_multiplier,
            )
            if (
                live_basis is not None
                and live_basis < entry_floor - config.ENTRY_HEDGE_ABORT_BPS
            ):
                naked = unhedged
                unhedged = Decimal(0)
                aborted = True
                await self._notifier.alert(
                    f"🛑 position {position.id} {symbol}: entry basis collapsed to"
                    f" {live_basis:.1f}bps (floor {entry_floor}bps) by hedge time"
                    f" — unwinding {naked} perp units instead of entering"
                )
                await self._unwind_perp(position, naked)
                return

            try:
                shortfall, err = await self._hedge_spot(
                    position, "BUY", unhedged * pair.qty_multiplier, "entry"
                )
            except AmbiguousOrderError as exc:
                # Spot buy may or may not have filled. Do NOT unwind (could leave
                # a naked spot long) and do NOT retry (could double). Leave the
                # perp fill recorded and let recovery / the operator reconcile.
                unhedged = Decimal(0)
                await self._notifier.alert(
                    f"🚨 position {position.id} {symbol}: spot hedge AMBIGUOUS"
                    f" ({exc}) — perp leg is filled but spot fill is UNKNOWN."
                    f" NOT unwinding/retrying; reconcile the hedge manually."
                )
                return
            naked = shortfall / pair.qty_multiplier
            unhedged = Decimal(0)
            if naked > 0:
                reason = f" — MEXC: {err}" if err else " (no fill / thin book)"
                await self._notifier.alert(
                    f"⚠️ position {position.id} {symbol}: spot hedge failed for"
                    f" {shortfall} base units{reason}, unwinding perp leg"
                )
                await self._unwind_perp(position, naked)

        try:
            while True:
                cancelled = position.id in self._cancel_requested
                timed_out = time.monotonic() > deadline
                done = remaining < aster_info.step_size
                if cancelled or timed_out or done or aborted:
                    if order_id is not None:
                        result = await self._trader.cancel_perp_order(
                            pair.aster_symbol, order_id
                        )
                        await absorb_fills(result)
                        order_id = None
                    await hedge_unhedged(force=True)
                    break

                book = self._md.aster_books.get(pair.aster_symbol)
                if book is None:
                    await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                    continue

                edge = self._entry_basis_bps(symbol)
                edge_ok = edge is not None and edge >= entry_floor
                price = aster_info.round_price(book.ask, up=True)  # join best ask

                # Cap the working maker to what MEXC can hedge (taker BUY,
                # walking the ask book) at a VWAP that still clears the floor.
                # Without this a fast/thin spot book leaves an over-filled
                # perp leg to unwind. Depth unavailable -> fall back to the
                # full remaining size (the per-fill hedge cap still guards).
                target_qty = aster_info.round_qty(remaining)
                if edge_ok or order_id is not None:
                    try:
                        asks = await self._trader.spot_depth(
                            pair.mexc_symbol, "BUY", config.ENTRY_DEPTH_LEVELS
                        )
                    except ExchangeError:
                        asks = []
                    if asks:
                        hedgeable = max_hedgeable_qty(
                            price / pair.qty_multiplier, asks, entry_floor
                        )
                        cap = aster_info.round_qty(hedgeable / pair.qty_multiplier)
                        target_qty = min(target_qty, cap)

                # Cap each resting clip to bound adverse-selection blast radius:
                # a single taker sweep can only catch one clip before the next
                # tick re-checks the (now-collapsed) basis and stops resting.
                if config.ENTRY_MAX_CLIP_NOTIONAL_USD is not None and price > 0:
                    clip = aster_info.round_qty(
                        config.ENTRY_MAX_CLIP_NOTIONAL_USD / price
                    )
                    if clip >= aster_info.step_size:
                        target_qty = min(target_qty, clip)

                if order_id is None:
                    if edge_ok and target_qty >= aster_info.step_size:
                        client_id = intents.make_client_order_id(position.id, "pent")
                        try:
                            order_id = await self._trader.place_perp_maker(
                                pair.aster_symbol, "SELL", target_qty, price, client_id
                            )
                            order_price = price
                            order_qty = target_qty
                            order_seen_executed = Decimal(0)
                            last_reprice = time.monotonic()
                        except ExchangeError as exc:
                            # GTX that would cross is rejected; retry next tick.
                            log.info("maker place rejected: %s", exc)
                else:
                    result = await self._trader.poll_perp_order(
                        pair.aster_symbol, order_id
                    )
                    await absorb_fills(result)
                    if not result.is_open:
                        order_id = None
                    else:
                        open_qty = order_qty - order_seen_executed
                        moved = order_price != price
                        stale_edge = not edge_ok
                        # Resize if the hedgeable cap drifted away from the
                        # resting size by a lot (shrank -> over-exposed;
                        # grew -> leaving fillable size on the table).
                        size_drift = abs(target_qty - open_qty) >= aster_info.step_size
                        can_reprice = (
                            time.monotonic() - last_reprice
                            >= config.REPRICE_MIN_INTERVAL_SECONDS
                        )
                        if (moved or stale_edge or size_drift) and can_reprice:
                            result = await self._trader.cancel_perp_order(
                                pair.aster_symbol, order_id
                            )
                            await absorb_fills(result)
                            order_id = None

                await hedge_unhedged()
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            # Shutdown: leave any resting order for recovery to reconcile (it
            # cancels sp_pent_ on active symbols at startup). Operator /remove
            # cancels the resting order itself before marking CLOSED, and /exit
            # can't reach an ENTERING position, so no other path orphans it.
            raise

        final = self._positions.get(position.id)
        if final.spot_qty > 0 or final.perp_qty > 0:
            entry_basis = None
            if final.perp_entry_avg and final.spot_entry_avg:
                entry_basis = (
                    (final.perp_entry_avg / pair.qty_multiplier - final.spot_entry_avg)
                    / final.spot_entry_avg * BPS
                )
            # Snap target_notional to the ACTUAL filled size (perp qty x entry
            # price), so a partially-filled or cancelled add can't leave it
            # inflated — it always reflects real exposure for the cap/display.
            actual_notional = (
                final.perp_qty * final.perp_entry_avg
                if final.perp_entry_avg else final.target_notional
            )
            self._conn.execute(
                "UPDATE positions SET entry_basis_bps=?, target_notional=? WHERE id=?",
                (str(entry_basis) if entry_basis is not None else None,
                 str(actual_notional), position.id),
            )
            self._conn.commit()
            self._positions.set_state(position.id, pm.OPEN)
            basis_str = (
                "n/a" if entry_basis is None else f"{round(float(entry_basis), 2)}bps"
            )
            if is_add and run_filled > 0:
                journal(self._conn, f"position {position.id}: ADD +{run_filled} ->"
                        f" perp={final.perp_qty} spot={final.spot_qty}"
                        f" blended_basis={entry_basis}")
                await self._notifier.alert(
                    f"➕ position {position.id} {symbol}: added {run_filled} perp"
                    f" units (total qty={final.perp_qty}), blended entry"
                    f" basis={basis_str} ({'paper' if self._paper else 'LIVE'})"
                )
            elif is_add:
                # Add worked but nothing filled (floor never met / timed out):
                # the position is unchanged, still OPEN.
                journal(self._conn, f"position {position.id}: add ended with no"
                        f" additional fills (perp={final.perp_qty})")
                await self._notifier.alert(
                    f"position {position.id} {symbol}: add filled nothing"
                    f" (basis stayed below floor / timed out) — position unchanged"
                )
            else:
                journal(self._conn, f"position {position.id}: OPEN perp={final.perp_qty}"
                        f" spot={final.spot_qty} entry_basis={entry_basis}")
                await self._notifier.alert(
                    f"✅ position {position.id} {symbol} OPEN: qty={final.perp_qty},"
                    f" entry basis={basis_str}"
                    f" ({'paper' if self._paper else 'LIVE'})"
                )
            if (
                not is_add
                and run_filled > 0
                and entry_basis is not None
                and entry_basis < entry_floor - config.ENTRY_REALIZED_ALERT_BPS
            ):
                await self._notifier.alert(
                    f"⚠️ position {position.id} {symbol}: realized entry basis"
                    f" {round(float(entry_basis), 2)}bps is well below your"
                    f" {entry_floor}bps floor — the resting perp leg was"
                    f" adverse-selected as spot rallied. Consider a higher floor"
                    f" or a more liquid name."
                )
        elif run_filled > 0 or final.fees_usd > 0:
            # Perp filled then was fully unwound (hedge abort / hedge failure):
            # no net exposure, but there IS a realized loss (unwind slippage +
            # fees). Book it so /pnl reflects every outcome, not just clean exits.
            pnl = self._positions.finalize_pnl(position.id)
            self._positions.set_state(position.id, pm.CANCELLED, "unwound after fill")
            journal(self._conn, f"position {position.id}: entry unwound after fill,"
                    f" realized={pnl}")
            await self._notifier.alert(
                f"position {position.id} {symbol}: entry unwound — realized"
                f" ${float(pnl):+.2f} (unwind slippage + fees)"
            )
        else:
            self._positions.set_state(position.id, pm.CANCELLED, "no fills")
            journal(self._conn, f"position {position.id}: entry ended with no fills")
            await self._notifier.alert(
                f"position {position.id} {symbol}: entry cancelled, no exposure"
            )

    # ── exit ──

    async def _run_exit(self, position: pm.Position) -> None:
        position = self._positions.get(position.id)
        if position.exit_mode == "passive":
            await self._run_passive_exit(position)
        else:
            await self._run_aggressive_exit(position)

    async def _run_aggressive_exit(self, position: pm.Position) -> None:
        symbol = position.symbol
        pair = self._pair(symbol)
        aster_info = self._md.aster_info[pair.aster_symbol]
        mexc_info = self._md.mexc_info[pair.mexc_symbol]
        self._positions.set_state(position.id, pm.EXITING)
        # Partial exit: stop closing once the perp falls to this floor (and the
        # spot to the matching floor). None / 0 -> full close.
        floor_perp = position.exit_target_qty or Decimal(0)
        floor_spot = floor_perp * pair.qty_multiplier
        journal(self._conn, f"position {position.id}: aggressive exit"
                f" floor_perp={floor_perp}")

        deadline = time.monotonic() + config.EXIT_TIMEOUT_MINUTES * 60
        while time.monotonic() < deadline:
            pos = self._positions.get(position.id)
            perp_left = max(
                Decimal(0), aster_info.round_qty(pos.perp_qty) - aster_info.round_qty(floor_perp)
            )
            spot_left = max(
                Decimal(0), mexc_info.round_qty(pos.spot_qty) - mexc_info.round_qty(floor_spot)
            )
            if perp_left <= 0 and spot_left <= 0:
                break
            aster_book, mexc_book = self._books(symbol)
            jobs = []
            if perp_left > 0 and aster_book:
                cap = aster_info.round_price(
                    aster_book.ask * (1 + config.HEDGE_SLIPPAGE_BPS / BPS), up=True
                )
                jobs.append(("aster", self._trader.perp_taker(
                    pair.aster_symbol, "BUY", perp_left, cap, True
                )))
            if spot_left > 0 and mexc_book:
                cap = mexc_info.round_price(
                    mexc_book.bid * (1 - config.HEDGE_SLIPPAGE_BPS / BPS), up=False
                )
                jobs.append(("mexc", self._trader.spot_taker(
                    pair.mexc_symbol, "SELL", spot_left, cap
                )))
            if not jobs:
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                continue
            results = await asyncio.gather(*(j[1] for j in jobs), return_exceptions=True)
            for (venue, _), result in zip(jobs, results):
                if isinstance(result, AmbiguousOrderError):
                    # Maybe-filled taker leg: stop this close and alert rather
                    # than looping and re-sending (perp is reduce-only so it's
                    # safe there, but a re-sent spot SELL would oversell).
                    await self._notifier.alert(
                        f"🚨 position {position.id} {symbol}: {venue} exit leg"
                        f" AMBIGUOUS ({result}) — stopping close, reconcile"
                        f" manually before re-exiting"
                    )
                    await self._complete_exit(position.id, floor_perp)
                    return
                if isinstance(result, BaseException):
                    log.warning("exit leg %s failed: %r", venue, result)
                    continue
                if result.qty > 0:
                    side = "BUY" if venue == "aster" else "SELL"
                    self._positions.record_fill(
                        position.id, venue, "exit", side, result.qty,
                        result.avg_price,
                        self._fee_usd(venue, False, result.qty, result.avg_price),
                    )
            await asyncio.sleep(config.POLL_INTERVAL_SECONDS)

        await self._complete_exit(position.id, floor_perp)

    async def _run_passive_exit(self, position: pm.Position) -> None:
        symbol = position.symbol
        pair = self._pair(symbol)
        aster_info = self._md.aster_info[pair.aster_symbol]
        self._positions.set_state(position.id, pm.EXITING)
        target = position.exit_target_bps
        floor_perp = position.exit_target_qty or Decimal(0)  # partial-exit floor
        journal(self._conn, f"position {position.id}: passive exit target={target}"
                f" floor_perp={floor_perp}")

        order_id: str | None = None
        order_price = Decimal(0)
        order_qty = Decimal(0)         # size the resting buy-back was placed with
        order_seen_executed = Decimal(0)
        last_reprice = 0.0
        # -inf (not 0.0) so the FIRST alert always fires: time.monotonic() is
        # seconds since boot, so a 0.0 sentinel suppresses the first alert for
        # the first PASSIVE_UNREACHABLE_ALERT_SECONDS of machine uptime (now-0
        # < threshold right after a reboot/restart).
        last_unreachable_alert = float("-inf")  # throttle "target unreachable" notices
        to_sell = Decimal(0)   # spot base units pending sale after perp buy-backs

        async def absorb_fills(result: OrderResult) -> None:
            nonlocal to_sell, order_seen_executed
            delta = result.executed_qty - order_seen_executed
            if delta <= 0:
                return
            order_seen_executed = result.executed_qty
            price = result.avg_price if result.avg_price > 0 else result.price
            self._positions.record_fill(
                position.id, "aster", "exit", "BUY", delta, price,
                self._fee_usd("aster", True, delta, price), result.order_id,
            )
            to_sell += delta * pair.qty_multiplier

        async def sell_pending(force: bool = False) -> None:
            nonlocal to_sell
            if to_sell <= 0:
                return
            book = self._md.mexc_books.get(pair.mexc_symbol)
            ref = book.bid if book else Decimal(1)
            if not force and to_sell * ref < config.MIN_HEDGE_NOTIONAL_USD:
                return
            qty = min(to_sell, self._positions.get(position.id).spot_qty)
            try:
                shortfall, err = await self._hedge_spot(position, "SELL", qty, "exit")
            except AmbiguousOrderError as exc:
                # Spot sell may have filled; don't retry (could oversell). Stop
                # this increment and alert for manual reconciliation.
                to_sell = Decimal(0)
                await self._notifier.alert(
                    f"🚨 position {position.id} {symbol}: spot exit sale AMBIGUOUS"
                    f" ({exc}) — perp bought back but spot sale UNKNOWN. Reconcile"
                    f" the spot balance manually."
                )
                return
            to_sell = shortfall
            if shortfall > 0:
                reason = f" — MEXC: {err}" if err else " (no fill / thin book)"
                await self._notifier.alert(
                    f"⚠️ position {position.id} {symbol}: spot exit sale incomplete,"
                    f" {shortfall} base units pending{reason}"
                )

        done = False
        try:
            while True:
                pos = self._positions.get(position.id)
                if pos.exit_mode != "passive":
                    # Mode changed (e.g. /exit ID now replaces this task): the
                    # replacement owns completion, so don't finalize here.
                    break
                remaining = max(
                    Decimal(0),
                    aster_info.round_qty(pos.perp_qty) - aster_info.round_qty(floor_perp),
                )
                if remaining <= 0:
                    if order_id is not None:
                        await self._trader.cancel_perp_order(pair.aster_symbol, order_id)
                        order_id = None
                    await sell_pending(force=True)
                    if to_sell <= 0:  # closed-portion spot fully sold
                        done = True
                        break
                    await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                    continue

                book = self._md.aster_books.get(pair.aster_symbol)
                close = self._close_basis_bps(symbol)
                gated = target is not None and (close is None or close > target)
                price = (
                    aster_info.round_price(book.bid, up=False) if book else order_price
                )

                # Cap the buy-back to what spot can sell (taker SELL, walking
                # the bid book) without the close basis exceeding the target —
                # the mirror of the entry sizing. Stops a thin spot bid from
                # forcing the spot leg to sell through worse bids than the
                # target implied.
                #
                # A PROTECTED exit (target set) must NEVER fall through to full
                # size when depth is missing: an empty/failed spot_depth reply
                # once dumped a whole position at market (BANK, realised +134bps
                # vs a target of 0). So with a target we size to ZERO unless we
                # have live depth proving the spot leg can sell at the target.
                # Only an untargeted "work it at any basis" exit closes full.
                if target is None:
                    place_qty = remaining
                else:
                    place_qty = Decimal(0)
                    if book is not None:
                        try:
                            bids = await self._trader.spot_depth(pair.mexc_symbol, "SELL")
                        except ExchangeError:
                            bids = []
                        if bids:
                            closeable = max_closeable_qty(
                                price / pair.qty_multiplier, bids, target
                            )
                            place_qty = min(
                                remaining,
                                aster_info.round_qty(closeable / pair.qty_multiplier),
                            )

                if order_id is None:
                    placeable = (
                        book is not None and not gated
                        and place_qty >= aster_info.step_size
                    )
                    if placeable and place_qty * price < aster_info.min_notional:
                        # Spot depth at the target only supports a sub-minimum
                        # buy-back; Aster would reject it (-4164). Don't spam
                        # attempts — surface that the target can't be met on the
                        # current spot bid depth so the operator can loosen it
                        # or close aggressively instead.
                        placeable = False
                        now = time.monotonic()
                        if now - last_unreachable_alert > config.PASSIVE_UNREACHABLE_ALERT_SECONDS:
                            last_unreachable_alert = now
                            await self._notifier.alert(
                                f"⏳ position {position.id} {symbol}: passive exit"
                                f" target {target}bps not reachable — spot bid"
                                f" depth at the target is below the"
                                f" {aster_info.min_notional} min order. Loosen the"
                                f" target or use /exit {position.id} now."
                            )
                    if placeable:
                        client_id = intents.make_client_order_id(position.id, "pext")
                        try:
                            order_id = await self._trader.place_perp_maker(
                                pair.aster_symbol, "BUY", place_qty, price, client_id
                            )
                            order_price = price
                            order_qty = place_qty
                            order_seen_executed = Decimal(0)
                            last_reprice = time.monotonic()
                        except ExchangeError as exc:
                            log.info("passive exit place rejected: %s", exc)
                else:
                    result = await self._trader.poll_perp_order(
                        pair.aster_symbol, order_id
                    )
                    await absorb_fills(result)
                    if not result.is_open:
                        order_id = None
                    else:
                        open_qty = order_qty - order_seen_executed
                        size_drift = abs(place_qty - open_qty) >= aster_info.step_size
                        can_reprice = (
                            time.monotonic() - last_reprice
                            >= config.REPRICE_MIN_INTERVAL_SECONDS
                        )
                        if (gated or price != order_price or size_drift) and can_reprice:
                            result = await self._trader.cancel_perp_order(
                                pair.aster_symbol, order_id
                            )
                            await absorb_fills(result)
                            order_id = None

                await sell_pending()
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            # Replaced by an aggressive exit or shutdown: cancel the resting
            # order so the replacement doesn't double-close.
            if order_id is not None:
                try:
                    result = await self._trader.cancel_perp_order(
                        pair.aster_symbol, order_id
                    )
                    await absorb_fills(result)
                    await sell_pending(force=True)
                except ExchangeError:
                    log.exception("cleanup cancel failed for position %s", position.id)
            raise

        if done:
            await self._complete_exit(position.id, floor_perp)

    async def _complete_exit(self, position_id: int, floor_perp: Decimal) -> None:
        """Finish an exit run. A partial close (floor > 0 with residual left)
        returns the position to OPEN at the reduced size — the exit fills
        already shrank perp_qty/spot_qty, and realised P&L is booked only at
        the final full close. A full close finalises P&L and marks CLOSED."""
        pos = self._positions.get(position_id)
        if floor_perp > 0 and (pos.perp_qty > 0 or pos.spot_qty > 0):
            self._positions.set_exit_request(position_id, None, None, None)
            self._positions.set_state(position_id, pm.OPEN, "partial exit complete")
            journal(
                self._conn,
                f"position {position_id}: PARTIAL EXIT done, perp={pos.perp_qty}"
                f" spot={pos.spot_qty} remain",
            )
            await self._notifier.alert(
                f"✂️ position {position_id} {pos.symbol}: partial exit done —"
                f" {pos.perp_qty} perp / {pos.spot_qty} spot remain (OPEN)"
            )
        else:
            await self._finalize_close(position_id)

    async def _finalize_close(self, position_id: int) -> None:
        pos = self._positions.get(position_id)
        # "Flat" means both legs are below one exchange step — sub-step dust can
        # never be traded away, so testing raw qty > 0 wedges the position in
        # EXITING forever (and re-alerts on every restart). Write the dust off.
        pair = self._md.pair_maps.get(pos.symbol)
        aster_info = self._md.aster_info.get(pair.aster_symbol) if pair else None
        mexc_info = self._md.mexc_info.get(pair.mexc_symbol) if pair else None
        perp_left = aster_info.round_qty(pos.perp_qty) if aster_info else pos.perp_qty
        spot_left = mexc_info.round_qty(pos.spot_qty) if mexc_info else pos.spot_qty
        if perp_left > 0 or spot_left > 0:
            journal(
                self._conn,
                f"position {position_id}: exit incomplete perp={pos.perp_qty}"
                f" spot={pos.spot_qty}",
                "WARN",
            )
            await self._notifier.alert(
                f"⚠️ position {position_id} {pos.symbol}: exit incomplete"
                f" (perp={pos.perp_qty}, spot={pos.spot_qty}) — still EXITING"
            )
            return
        if pos.perp_qty > 0 or pos.spot_qty > 0:
            journal(
                self._conn,
                f"position {position_id}: closing with sub-step dust written off"
                f" (perp={pos.perp_qty}, spot={pos.spot_qty})",
                "WARN",
            )
        await self._accrue_funding(pos)
        pnl = self._positions.finalize_pnl(position_id)
        self._positions.set_state(position_id, pm.CLOSED)
        journal(self._conn, f"position {position_id}: CLOSED pnl={pnl}")
        await self._notifier.alert(
            f"🏁 position {position_id} {pos.symbol} CLOSED, realised PnL"
            f" ${float(pnl):.2f} ({'paper' if self._paper else 'LIVE'})"
        )

    async def _accrue_funding(self, pos: pm.Position) -> None:
        """Funding collected by the short perp leg over the holding period.

        Live: exact amounts from Aster income history. Paper: estimated from
        the last known funding rate.
        """
        if pos.opened_ms is None:
            return
        pair = self._pair(pos.symbol)
        end_ms = int(time.time() * 1000)
        if not self._paper and isinstance(self._trader, LiveTrader):
            try:
                rows = await self._trader._aster.income_history(
                    pair.aster_symbol, "FUNDING_FEE", pos.opened_ms, end_ms
                )
                total = sum((Decimal(str(r.get("income", "0"))) for r in rows), Decimal(0))
                self._positions.add_funding(pos.id, total - pos.funding_usd)
                return
            except ExchangeError:
                log.exception("funding income fetch failed; falling back to estimate")
        rate = (self._md.funding.get(pair.aster_symbol) or {}).get("funding_rate")
        if rate is None or pos.spot_entry_avg is None:
            return
        stat = self._md.funding_stats.get(pair.aster_symbol)
        interval_hours = (
            Decimal(stat.interval_hours) if stat is not None
            else Decimal(config.FUNDING_INTERVAL_HOURS)
        )
        hold_hours = Decimal(end_ms - pos.opened_ms) / Decimal(3_600_000)
        periods = hold_hours / interval_hours
        entry_qty = self._positions._phase_qty(pos.id, "aster", "entry")
        notional = entry_qty * pos.spot_entry_avg * pair.qty_multiplier
        # Short perp receives funding when the rate is positive.
        estimate = rate * notional * periods
        self._positions.add_funding(pos.id, estimate - pos.funding_usd)
