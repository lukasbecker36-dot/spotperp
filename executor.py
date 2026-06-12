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
        if result.executed_qty == 0 and result.order_id:
            try:
                result = await self._mexc.get_order(symbol, result.order_id)
            except ExchangeError:
                pass
        intents.resolve_intent(self._conn, intent_id, "done", result.raw)
        return TakerFill(qty=result.executed_qty, avg_price=result.avg_price)


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

    # ── public API (called by the live monitor's command poller) ──

    def start_entry(self, position: pm.Position) -> None:
        self._spawn(position.id, self._run_entry(position))

    def start_exit(self, position: pm.Position) -> None:
        existing = self._tasks.get(position.id)
        if existing and not existing.done():
            # A passive exit being replaced (e.g. /exit ID now): stop it first.
            existing.cancel()
        self._spawn(position.id, self._run_exit(position))

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
        pair = self._pair(symbol)
        return (
            self._md.aster_books.get(pair.aster_symbol),
            self._md.mexc_books.get(pair.mexc_symbol),
        )

    def _entry_basis_bps(self, symbol: str) -> Decimal | None:
        aster, mexc = self._books(symbol)
        if aster is None or mexc is None:
            return None
        mult = self._pair(symbol).qty_multiplier
        return (aster.ask / mult - mexc.ask) / mexc.ask * BPS

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

    async def _hedge_spot(
        self, position: pm.Position, side: str, base_qty: Decimal, phase: str
    ) -> Decimal:
        """Buy (entry) or sell (exit) spot for a perp fill increment.

        Returns the UNFILLED remainder in MEXC base units (0 on success).
        """
        pair = self._pair(position.symbol)
        info = self._md.mexc_info[pair.mexc_symbol]
        remaining = info.round_qty(base_qty)
        if remaining <= 0:
            return Decimal(0)
        for attempt in range(config.HEDGE_RETRY_ATTEMPTS):
            book = self._md.mexc_books.get(pair.mexc_symbol)
            if book is None:
                await asyncio.sleep(1)
                continue
            slip = config.HEDGE_SLIPPAGE_BPS / BPS
            cap = (
                book.ask * (1 + slip) if side == "BUY" else book.bid * (1 - slip)
            )
            cap = info.round_price(cap, up=(side == "BUY"))
            try:
                fill = await self._trader.spot_taker(
                    pair.mexc_symbol, side, remaining, cap
                )
            except ExchangeError as exc:
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
            if remaining <= 0:
                return Decimal(0)
            await asyncio.sleep(0.5)
        return remaining

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

    async def _run_entry(self, position: pm.Position) -> None:
        symbol = position.symbol
        pair = self._pair(symbol)
        aster_info = self._md.aster_info[pair.aster_symbol]
        self._positions.set_state(position.id, pm.ENTERING)
        journal(self._conn, f"position {position.id}: entering {symbol}"
                f" notional={position.target_notional}")

        aster_book, _ = self._books(symbol)
        if aster_book is None:
            self._positions.set_state(position.id, pm.CANCELLED, "no quotes")
            return
        total_qty = aster_info.round_qty(position.target_notional / aster_book.ask)
        if total_qty <= 0:
            self._positions.set_state(position.id, pm.CANCELLED, "notional below lot size")
            return

        deadline = time.monotonic() + config.ENTRY_TIMEOUT_MINUTES * 60
        remaining = total_qty
        unhedged = Decimal(0)          # perp filled, spot not yet bought
        order_id: str | None = None
        order_price = Decimal(0)
        order_seen_executed = Decimal(0)
        last_reprice = 0.0

        async def absorb_fills(result: OrderResult) -> None:
            nonlocal remaining, unhedged, order_seen_executed
            delta = result.executed_qty - order_seen_executed
            if delta <= 0:
                return
            order_seen_executed = result.executed_qty
            remaining -= delta
            price = result.avg_price if result.avg_price > 0 else result.price
            self._positions.record_fill(
                position.id, "aster", "entry", "SELL", delta, price,
                self._fee_usd("aster", True, delta, price), result.order_id,
            )
            unhedged += delta

        async def hedge_unhedged(force: bool = False) -> None:
            nonlocal unhedged
            if unhedged <= 0:
                return
            book = self._md.mexc_books.get(pair.mexc_symbol)
            ref_price = book.ask if book else Decimal(1)
            notional = unhedged * pair.qty_multiplier * ref_price
            if not force and notional < config.MIN_HEDGE_NOTIONAL_USD:
                return  # accumulate dust
            shortfall = await self._hedge_spot(
                position, "BUY", unhedged * pair.qty_multiplier, "entry"
            )
            naked = shortfall / pair.qty_multiplier
            unhedged = Decimal(0)
            if naked > 0:
                await self._notifier.alert(
                    f"⚠️ position {position.id} {symbol}: spot hedge failed for"
                    f" {shortfall} base units, unwinding perp leg"
                )
                await self._unwind_perp(position, naked)

        try:
            while True:
                cancelled = position.id in self._cancel_requested
                timed_out = time.monotonic() > deadline
                done = remaining < aster_info.step_size
                if cancelled or timed_out or done:
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
                edge_ok = edge is not None and edge >= config.ENTRY_MIN_EDGE_FLOOR_BPS
                desired = book.ask  # join the best ask

                if order_id is None:
                    if edge_ok and remaining >= aster_info.step_size:
                        qty = aster_info.round_qty(remaining)
                        price = aster_info.round_price(desired, up=True)
                        client_id = intents.make_client_order_id(position.id, "pent")
                        try:
                            order_id = await self._trader.place_perp_maker(
                                pair.aster_symbol, "SELL", qty, price, client_id
                            )
                            order_price = price
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
                        moved = order_price != aster_info.round_price(desired, up=True)
                        stale_edge = not edge_ok
                        can_reprice = (
                            time.monotonic() - last_reprice
                            >= config.REPRICE_MIN_INTERVAL_SECONDS
                        )
                        if (moved or stale_edge) and can_reprice:
                            result = await self._trader.cancel_perp_order(
                                pair.aster_symbol, order_id
                            )
                            await absorb_fills(result)
                            order_id = None

                await hedge_unhedged()
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            # Engine shutdown: leave resting orders for recovery to reconcile.
            raise

        final = self._positions.get(position.id)
        if final.spot_qty > 0 or final.perp_qty > 0:
            entry_basis = None
            if final.perp_entry_avg and final.spot_entry_avg:
                entry_basis = (
                    (final.perp_entry_avg / pair.qty_multiplier - final.spot_entry_avg)
                    / final.spot_entry_avg * BPS
                )
            self._conn.execute(
                "UPDATE positions SET entry_basis_bps=? WHERE id=?",
                (str(entry_basis) if entry_basis is not None else None, position.id),
            )
            self._conn.commit()
            self._positions.set_state(position.id, pm.OPEN)
            journal(self._conn, f"position {position.id}: OPEN perp={final.perp_qty}"
                    f" spot={final.spot_qty} entry_basis={entry_basis}")
            await self._notifier.alert(
                f"✅ position {position.id} {symbol} OPEN: qty={final.perp_qty},"
                f" entry basis={entry_basis if entry_basis is None else round(float(entry_basis), 2)}bps"
                f" ({'paper' if self._paper else 'LIVE'})"
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
        journal(self._conn, f"position {position.id}: aggressive exit")

        deadline = time.monotonic() + config.EXIT_TIMEOUT_MINUTES * 60
        while time.monotonic() < deadline:
            pos = self._positions.get(position.id)
            perp_left = aster_info.round_qty(pos.perp_qty)
            spot_left = mexc_info.round_qty(pos.spot_qty)
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

        await self._finalize_close(position.id)

    async def _run_passive_exit(self, position: pm.Position) -> None:
        symbol = position.symbol
        pair = self._pair(symbol)
        aster_info = self._md.aster_info[pair.aster_symbol]
        self._positions.set_state(position.id, pm.EXITING)
        target = position.exit_target_bps
        journal(self._conn, f"position {position.id}: passive exit target={target}")

        order_id: str | None = None
        order_price = Decimal(0)
        order_seen_executed = Decimal(0)
        last_reprice = 0.0
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
            shortfall = await self._hedge_spot(position, "SELL", qty, "exit")
            to_sell = shortfall
            if shortfall > 0:
                await self._notifier.alert(
                    f"⚠️ position {position.id} {symbol}: spot exit sale incomplete,"
                    f" {shortfall} base units pending"
                )

        try:
            while True:
                pos = self._positions.get(position.id)
                if pos.exit_mode != "passive":
                    # Mode changed (e.g. /exit ID now replaces this task).
                    break
                remaining = aster_info.round_qty(pos.perp_qty)
                if remaining <= 0:
                    if order_id is not None:
                        await self._trader.cancel_perp_order(pair.aster_symbol, order_id)
                        order_id = None
                    await sell_pending(force=True)
                    if self._positions.get(position.id).spot_qty <= 0:
                        break
                    await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                    continue

                book = self._md.aster_books.get(pair.aster_symbol)
                close = self._close_basis_bps(symbol)
                gated = target is not None and (close is None or close > target)

                if order_id is None:
                    if book is not None and not gated:
                        price = aster_info.round_price(book.bid, up=False)
                        client_id = intents.make_client_order_id(position.id, "pext")
                        try:
                            order_id = await self._trader.place_perp_maker(
                                pair.aster_symbol, "BUY", remaining, price, client_id
                            )
                            order_price = price
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
                        desired = (
                            aster_info.round_price(book.bid, up=False) if book else order_price
                        )
                        can_reprice = (
                            time.monotonic() - last_reprice
                            >= config.REPRICE_MIN_INTERVAL_SECONDS
                        )
                        if (gated or desired != order_price) and can_reprice:
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

        await self._finalize_close(position.id)

    async def _finalize_close(self, position_id: int) -> None:
        pos = self._positions.get(position_id)
        if pos.perp_qty > 0 or pos.spot_qty > 0:
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
