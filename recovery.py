"""Startup reconciliation after a crash or restart.

Strategy (conservative):
1. Cancel stale entry/passive-exit maker orders (client ids sp_pent_/sp_pext_)
   on symbols of active positions — those tasks re-place their maker orders, so
   a stale resting one is the only thing that could double-fill. Protective
   /stops orders (sp_stop_) and any manual orders are LEFT IN PLACE: a restart
   must not strip liquidation protection.
2. Mark unresolved intents as reconciled (the cancel sweep plus position
   comparison covers their effects).
3. Re-derive position states: ENTERING with exposure -> OPEN (the monitor
   restarts nothing for entries; the operator decides), ENTERING without
   exposure -> CANCELLED, EXITING/UNWINDING are resumed by the monitor.
4. Compare DB exposure to venue state (Aster positionRisk, MEXC balances) and
   alert on mismatch — never auto-trade to "fix" a discrepancy.
"""
from __future__ import annotations

import logging
from decimal import Decimal

import config
import intents
import position_manager as pm
from database import journal
from exchange_client import AsterClient, ExchangeError, MexcClient
from notify import Notifier

log = logging.getLogger(__name__)


async def reconcile(
    conn,
    positions: pm.PositionManager,
    aster: AsterClient | None,
    mexc: MexcClient | None,
    notifier: Notifier,
    *,
    paper: bool,
) -> None:
    active = positions.active()
    unresolved = intents.unresolved_intents(conn)
    if unresolved:
        journal(conn, f"recovery: {len(unresolved)} unresolved intents", "WARN")

    if not paper and aster is not None:
        # 1. cancel stale resting orders on active symbols
        symbols = sorted({p.symbol for p in active})
        for symbol in symbols:
            try:
                open_orders = await aster.open_orders(symbol)
            except ExchangeError:
                log.exception("recovery: open_orders(%s) failed", symbol)
                continue
            for order in open_orders:
                # Only sweep our own stale entry/exit maker orders. Protective
                # /stops (sp_stop_) and manual orders must survive a restart.
                if not order.client_order_id.startswith(("sp_pent_", "sp_pext_")):
                    continue
                try:
                    final = await aster.cancel_order(symbol, order.order_id)
                except ExchangeError:
                    log.exception("recovery: cancel %s failed", order.order_id)
                    continue
                journal(
                    conn,
                    f"recovery: cancelled stale order {order.order_id} on {symbol},"
                    f" executed={final.executed_qty}",
                    "WARN",
                )
                await _book_late_fill(
                    conn, positions, notifier, "aster", final,
                    order.client_order_id,
                )

    # 2. reconcile the intent journal against the venue by client-order-id
    #    (not just a blind 'failed'), so an order that DID land after a crash is
    #    found and cancelled/surfaced rather than becoming a silent orphan on a
    #    symbol the sweep above didn't cover (e.g. a position recovery cancels).
    for row in unresolved:
        await _reconcile_intent(
            conn, aster, mexc, notifier, row, paper=paper, positions=positions
        )

    # 3. state fix-ups
    for pos in active:
        if pos.state in (pm.PENDING_ENTRY, pm.ENTERING):
            if pos.perp_qty > 0 or pos.spot_qty > 0:
                positions.set_state(pos.id, pm.OPEN, "recovered mid-entry")
                await notifier.alert(
                    f"recovery: position {pos.id} {pos.symbol} recovered as OPEN"
                    f" (perp={pos.perp_qty}, spot={pos.spot_qty}); verify hedge balance"
                )
            else:
                positions.set_state(pos.id, pm.CANCELLED, "recovered, no exposure")
        # EXITING / UNWINDING positions keep their state; live_monitor resumes them.

    # 4. venue comparison
    if not paper and aster is not None and mexc is not None:
        await _compare_with_venues(conn, positions, aster, mexc, notifier)




async def _book_late_fill(
    conn, positions: pm.PositionManager, notifier: Notifier,
    venue: str, order, client_id: str,
) -> bool:
    """Record what a resting order of ours executed while we were not running.

    A restart during a working entry or exit leaves a maker order on the venue.
    It may have filled — wholly or partly — in the gap. Cancelling it and
    merely ALERTING leaves the DB holding a perp leg the venue no longer has,
    and the hedge guard then reads that gap as an ADL and force-sells the spot
    in tranches (position 191).

    Only the DELTA is booked: the executor records fills incrementally as it
    polls, so part of this order may already be in the table under the same
    venue order id.
    """
    parsed = intents.parse_client_order_id(client_id)
    if parsed is None or order.executed_qty <= 0:
        return False
    position_id, phase = parsed
    try:
        positions.get(position_id)
    except KeyError:
        return False
    already = positions.recorded_qty_for_order(position_id, order.order_id)
    delta = order.executed_qty - already
    if delta <= 0:
        return False      # the executor had already booked it all
    price = order.avg_price if order.avg_price > 0 else order.price
    if price <= 0:
        await notifier.alert(
            f"⚠️ recovery: order {client_id} executed {delta} but reported no"
            f" price — book it manually, the position is out of sync"
        )
        return False
    # These are our GTX maker legs, so the maker rate is the right estimate.
    # /truefill can replace it with the venue's own figure if it matters.
    rate = config.ASTER_MAKER_FEE if venue == "aster" else config.MEXC_MAKER_FEE
    positions.record_fill(
        position_id, venue, phase, order.side, delta, price,
        delta * price * rate, order_id=order.order_id,
    )
    journal(
        conn,
        f"recovery: booked late fill {delta} @ {price} on {order.symbol}"
        f" ({client_id}) into position {position_id} {phase}",
        "WARN",
    )
    await notifier.alert(
        f"⚠️ recovery: {client_id} filled {delta} @ {price} while the engine"
        f" was down — booked into position {position_id} so the DB matches the"
        f" venue. The other leg of that increment is NOT hedged; the resumed"
        f" exit (or /exit) will square it."
    )
    return True


async def _reconcile_intent(
    conn, aster, mexc, notifier, row, *, paper: bool,
    positions: pm.PositionManager | None = None,
) -> None:
    """Query the venue for an unresolved intent's order by client id and act:
    cancel it if it's still resting, alert if it filled, mark reconciled either
    way. Falls back to 'failed' when we can't look it up (paper, no client id,
    or lookup error) — same as the old blind behaviour, but only as a fallback."""
    import json
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    client_id = payload.get("client_order_id")
    symbol = payload.get("symbol")
    client = aster if row["venue"] == "aster" else mexc
    if paper or client is None or not client_id or not symbol:
        intents.resolve_intent(conn, row["id"], "failed", "reconciled at startup")
        return
    try:
        order = await client.get_order_by_client_id(symbol, client_id)
    except ExchangeError:
        log.exception("recovery: lookup %s failed", client_id)
        intents.resolve_intent(conn, row["id"], "failed", "lookup failed at startup")
        return
    if order is None:
        intents.resolve_intent(conn, row["id"], "failed", "never landed on venue")
        return
    if order.is_open:
        try:
            await client.cancel_order(symbol, order.order_id)
        except ExchangeError:
            log.exception("recovery: cancel orphan %s failed", order.order_id)
        journal(conn, f"recovery: cancelled orphan order {client_id} on {symbol}"
                f" (executed={order.executed_qty})", "WARN")
    if order.executed_qty > 0:
        booked = positions is not None and await _book_late_fill(
            conn, positions, notifier, row["venue"], order, client_id
        )
        if not booked:
            await notifier.alert(
                f"⚠️ recovery: intent order {client_id} on {symbol} had fills"
                f" ({order.executed_qty} @ {order.avg_price}) that could not be"
                f" attributed — verify the position vs venue and /adopt if needed"
            )
    intents.resolve_intent(conn, row["id"], "reconciled", order.raw)


async def _compare_with_venues(
    conn, positions: pm.PositionManager, aster: AsterClient, mexc: MexcClient,
    notifier: Notifier,
) -> None:
    try:
        risk = await aster.position_risk()
        venue_perp = {
            r["symbol"]: Decimal(str(r.get("positionAmt", "0"))) for r in risk
        }
    except ExchangeError:
        log.exception("recovery: positionRisk failed")
        return
    db_perp: dict[str, Decimal] = {}
    for pos in positions.active():
        if pos.paper:
            continue
        db_perp[pos.symbol] = db_perp.get(pos.symbol, Decimal(0)) - pos.perp_qty
    mismatches = []
    for symbol in sorted(set(db_perp) | {s for s, a in venue_perp.items() if a != 0}):
        db_amt = db_perp.get(symbol, Decimal(0))
        venue_amt = venue_perp.get(symbol, Decimal(0))
        if db_amt != venue_amt:
            mismatches.append(f"{symbol}: db={db_amt} venue={venue_amt}")
    if mismatches:
        msg = "⚠️ recovery: perp exposure mismatch — " + "; ".join(mismatches)
        journal(conn, msg, "ERROR")
        await notifier.alert(msg)
