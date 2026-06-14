"""Aster DEX API discovery/testing. Run on a box with API access:

    python scripts/probe_aster.py           # read-only checks
    python scripts/probe_aster.py --order SYMBOL QTY PRICE   # place+cancel a
                                            # far-from-market GTX order
    python scripts/probe_aster.py --margin SYMBOL   # set 1x ISOLATED, verify
                                            # which endpoint version works
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from auth import load_aster_credentials, load_env
from exchange_client import AsterClient


async def main() -> None:
    load_env()
    creds = None
    try:
        creds = load_aster_credentials()
        print(f"credentials: loaded (signer {creds.signer[:10]}…)")
    except RuntimeError as exc:
        print(f"credentials: NOT loaded ({exc}) — market data only")

    async with aiohttp.ClientSession() as session:
        client = AsterClient(session, creds)

        info = await client.exchange_info()
        print(f"exchangeInfo: {len(info)} trading symbols")
        btc = info.get("BTCUSDT")
        print(f"BTCUSDT filters: tick={btc.tick_size} step={btc.step_size}"
              f" min_notional={btc.min_notional}" if btc else "BTCUSDT missing!")

        books = await client.book_tickers()
        b = books.get("BTCUSDT")
        print(f"bookTicker: {len(books)} symbols; BTCUSDT {b.bid}/{b.ask}" if b else "no BTCUSDT book")

        premium = await client.premium_index()
        p = premium.get("BTCUSDT")
        if p:
            print(f"premiumIndex BTCUSDT: mark={p['mark_price']}"
                  f" funding={p['funding_rate']}")

        if creds:
            balances = await client.balances()
            nonzero = [
                f"{b['asset']}={b['balance']}"
                for b in balances
                if Decimal(str(b.get("balance", 0))) != 0
            ]
            print(f"balances: {', '.join(nonzero) or 'all zero'}")
            risk = await client.position_risk()
            open_pos = [
                f"{r['symbol']}={r['positionAmt']}"
                for r in risk
                if Decimal(str(r.get("positionAmt", 0))) != 0
            ]
            print(f"open positions: {', '.join(open_pos) or 'none'}")

        if creds and len(sys.argv) >= 3 and sys.argv[1] == "--margin":
            symbol = sys.argv[2]
            print(f"setting ISOLATED on {symbol} ...")
            r1 = await client.set_margin_type(symbol, "ISOLATED")
            print(f"  marginType -> {r1}")
            print(f"setting leverage 1 on {symbol} ...")
            r2 = await client.set_leverage(symbol, 1)
            print(f"  leverage -> {r2}")
            risk = await client.position_risk()
            for r in risk:
                if r.get("symbol") == symbol:
                    print(f"  positionRisk: marginType={r.get('marginType')}"
                          f" leverage={r.get('leverage')}")

        if creds and len(sys.argv) >= 5 and sys.argv[1] == "--order":
            symbol, qty, price = sys.argv[2], Decimal(sys.argv[3]), Decimal(sys.argv[4])
            print(f"placing GTX BUY {qty} {symbol} @ {price} ...")
            order = await client.place_order(
                symbol, "BUY", "LIMIT", quantity=qty, price=price,
                time_in_force="GTX",
            )
            print(f"placed: id={order.order_id} status={order.status}")
            fetched = await client.get_order(symbol, order.order_id)
            print(f"queried: status={fetched.status} executed={fetched.executed_qty}")
            cancelled = await client.cancel_order(symbol, order.order_id)
            print(f"cancelled: status={cancelled.status}")


if __name__ == "__main__":
    asyncio.run(main())
