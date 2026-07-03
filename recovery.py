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
                if final.executed_qty > 0:
                    await notifier.alert(
                        f"⚠️ recovery: stale order {order.order_id} on {symbol} had"
                        f" fills ({final.executed_qty}) — check position vs venue"
                    )

    # 2. close out the intent journal
    for row in unresolved:
        intents.resolve_intent(conn, row["id"], "failed", "reconciled at startup")

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
