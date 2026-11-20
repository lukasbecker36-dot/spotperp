"""KuCoin spot check before the basis bot trades through it. Read-only unless asked:

    python scripts/probe_kucoin.py                              # keys, clock, accounts, balances, overlap
    python scripts/probe_kucoin.py --order SYMBOL QTY PRICE     # also place + cancel a far-from-market
                                                                # post-only BUY (tiny size) to prove trading
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from auth import load_env, load_kucoin_credentials, load_aster_credentials
from exchange_client import AsterClient, ExchangeError, KucoinClient


async def main() -> None:
    load_env()
    try:
        creds = load_kucoin_credentials()
        print(f"credentials: loaded (key version {creds.key_version})")
    except RuntimeError as exc:
        print(f"credentials: NOT loaded ({exc}) — market data only")
        creds = None

    async with aiohttp.ClientSession() as session:
        kc = KucoinClient(session, creds)
        off = await kc.sync_time()
        print(f"clock offset vs KuCoin: {off}ms")

        info = await kc.exchange_info()
        usdt = {s for s, i in info.items() if i.quote_asset == "USDT"}
        print(f"symbols: {len(info)} trading, {len(usdt)} vs USDT")
        books = await kc.book_tickers()
        b = books.get("BTCUSDT")
        print(f"tickers: {len(books)}; BTCUSDT {b.bid} ({b.bid_qty}) / {b.ask} ({b.ask_qty})" if b else "no BTCUSDT")
        d = await kc.depth("BTCUSDT", 5)
        print(f"depth BTCUSDT: {len(d['bids'])} bids, {len(d['asks'])} asks, best bid {d['bids'][0]}")

        try:
            aster = AsterClient(session, None)
            perps = await aster.exchange_info()
            overlap = sorted(s for s in perps if s in usdt)
            print(f"Aster perps with a KuCoin USDT spot pair: {len(overlap)} (e.g. {', '.join(overlap[:12])})")
        except Exception as exc:     # noqa: BLE001 — diagnostics only
            print(f"Aster overlap check skipped: {exc}")

        if creds is None:
            return
        try:
            hf = await kc.is_hf()
        except ExchangeError as exc:
            print(f"SIGNED REQUEST FAILED: {exc}\n  -> check KUCOIN_API_KEY / SECRET / PASSPHRASE / KEY_VERSION and "
                  f"that the key allows General + Spot Trading and this server's IP")
            return
        print(f"account kind: {'high-frequency (trade_hf)' if hf else 'classic (trade)'}")
        rows = await kc._call("GET", "/api/v1/accounts", signed=True)
        for r in sorted(rows or [], key=lambda r: (r.get("type", ""), r.get("currency", ""))):
            if Decimal(r.get("balance") or "0") > 0:
                print(f"  {r.get('type'):9s} {r.get('currency'):8s} balance {r.get('balance')} "
                      f"available {r.get('available')} holds {r.get('holds')}")
        bal = {x["asset"]: x for x in (await kc.account())["balances"]}
        u = bal.get("USDT")
        print(f"tradeable USDT (what the bot sees): {u['free'] if u else '0'} free, {u['locked'] if u else '0'} locked"
              + ("" if u else "  <- move USDT from the Main/Funding account to the Trading account"))
        print(f"open BTC-USDT orders: {len(await kc.open_orders('BTCUSDT'))}")
        fills = await kc.my_trades("BTCUSDT", limit=5)
        print(f"recent BTC-USDT fills (7d): {len(fills)}")

        if len(sys.argv) >= 5 and sys.argv[1] == "--order":
            sym, qty, px = sys.argv[2], Decimal(sys.argv[3]), Decimal(sys.argv[4])
            r = await kc.place_order(sym, "BUY", "LIMIT_MAKER", quantity=qty, price=px)
            print(f"placed post-only BUY {qty} {sym} @ {px}: id {r.order_id}")
            await asyncio.sleep(1)
            print("state:", (await kc.get_order(sym, r.order_id)).status)
            c = await kc.cancel_order(sym, r.order_id)
            print(f"cancelled: {c.status}, executed {c.executed_qty}")


if __name__ == "__main__":
    asyncio.run(main())
