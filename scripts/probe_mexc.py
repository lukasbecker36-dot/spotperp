"""MEXC API discovery/testing. Run on a box with API access:

    python scripts/probe_mexc.py            # read-only checks
    python scripts/probe_mexc.py --order SYMBOL QTY PRICE   # place+cancel a far
                                            # -from-market limit order (tiny size)
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from auth import load_env, load_mexc_credentials
from exchange_client import MexcClient


async def main() -> None:
    load_env()
    creds = None
    try:
        creds = load_mexc_credentials()
        print("credentials: loaded")
    except RuntimeError as exc:
        print(f"credentials: NOT loaded ({exc}) — market data only")

    async with aiohttp.ClientSession() as session:
        client = MexcClient(session, creds)

        info = await client.exchange_info()
        usdt = [s for s in info.values() if s.quote_asset == "USDT"]
        print(f"exchangeInfo: {len(info)} tradeable symbols, {len(usdt)} vs USDT")
        btc = info.get("BTCUSDT")
        print(f"BTCUSDT filters: tick={btc.tick_size} step={btc.step_size}"
              f" min_notional={btc.min_notional}" if btc else "BTCUSDT missing!")

        books = await client.book_tickers()
        b = books.get("BTCUSDT")
        print(f"bookTicker: {len(books)} symbols; BTCUSDT {b.bid}/{b.ask}" if b else "no BTCUSDT book")

        depth = await client.depth("BTCUSDT", 5)
        print(f"depth: top bid {depth['bids'][0]}, top ask {depth['asks'][0]}")

        if creds:
            account = await client.account()
            balances = [
                f"{a['asset']}={a['free']}"
                for a in account.get("balances", [])
                if Decimal(str(a.get("free", 0))) > 0
            ]
            print(f"account balances: {', '.join(balances) or 'all zero'}")

        if creds and len(sys.argv) >= 5 and sys.argv[1] == "--order":
            symbol, qty, price = sys.argv[2], Decimal(sys.argv[3]), Decimal(sys.argv[4])
            print(f"placing LIMIT BUY {qty} {symbol} @ {price} ...")
            order = await client.place_order(
                symbol, "BUY", "LIMIT", quantity=qty, price=price
            )
            print(f"placed: id={order.order_id} status={order.status}")
            fetched = await client.get_order(symbol, order.order_id)
            print(f"queried: status={fetched.status} executed={fetched.executed_qty}")
            cancelled = await client.cancel_order(symbol, order.order_id)
            print(f"cancelled: status={cancelled.status}")


if __name__ == "__main__":
    asyncio.run(main())
