"""KuCoin spot client: signing, symbol mapping, and translation into the MEXC/Binance shapes callers use."""
import base64
import hashlib
import hmac
import json
from decimal import Decimal

import pytest

import config
import exchange_client
from auth import KucoinCredentials, kucoin_headers
from exchange_client import ExchangeError, KucoinClient

CREDS = KucoinCredentials(api_key="key", api_secret="secret", passphrase="pass", key_version="2")


def ok(data):
    return {"code": "200000", "data": data}


class FakeVenue:
    """Routes (method, path-without-query) to canned replies and records every request."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def __call__(self, method, endpoint, *, data=None, headers=None, venue=None,
                       order_endpoint=False, timeout=10.0, params=None):
        path, _, query = endpoint.partition("?")
        self.calls.append({"method": method, "path": path, "query": query, "body": data, "headers": headers})
        reply = self.routes[(method, path)]
        return reply(query, data) if callable(reply) else reply


def client(routes, hf=None, monkeypatch=None):
    c = KucoinClient(session=None, creds=CREDS)
    c._hf = hf
    c._request = FakeVenue(routes)
    return c


def test_signature_matches_kucoin_spec():
    h = kucoin_headers(CREDS, 1700000000000, "post", "/api/v1/orders", '{"a":1}')
    want = base64.b64encode(hmac.new(b"secret", b'1700000000000POST/api/v1/orders{"a":1}',
                                     hashlib.sha256).digest()).decode()
    assert h["KC-API-SIGN"] == want and h["KC-API-TIMESTAMP"] == "1700000000000"
    assert h["KC-API-PASSPHRASE"] == base64.b64encode(hmac.new(b"secret", b"pass", hashlib.sha256).digest()).decode()
    assert h["KC-API-KEY"] == "key" and h["KC-API-KEY-VERSION"] == "2"


def test_signed_request_signs_the_exact_query_and_body(monkeypatch):
    monkeypatch.setattr(exchange_client, "now_ms", lambda: 1000)
    c = client({("GET", "/api/v1/orders"): ok({"items": []})}, hf=False)
    c._time_offset_ms = 500
    import asyncio
    asyncio.run(c.open_orders("BTCUSDT"))
    call = c._request.calls[0]
    assert call["query"] == "status=active&symbol=BTC-USDT&pageSize=500"
    assert call["headers"] == kucoin_headers(CREDS, 1500, "GET", "/api/v1/orders?" + call["query"], "")


async def test_symbols_tickers_depth_klines_translate():
    c = client({
        ("GET", "/api/v2/symbols"): ok([
            {"symbol": "CARDS-USDT", "baseCurrency": "CARDS", "quoteCurrency": "USDT", "priceIncrement": "0.0001",
             "baseIncrement": "0.01", "minFunds": "0.1", "enableTrading": True},
            {"symbol": "DEAD-USDT", "baseCurrency": "DEAD", "quoteCurrency": "USDT", "enableTrading": False}]),
        ("GET", "/api/v1/market/allTickers"): ok({"time": 123, "ticker": [
            {"symbol": "CARDS-USDT", "buy": "0.26", "bestBidSize": "100", "sell": "0.2601", "bestAskSize": "50"},
            {"symbol": "ZERO-USDT", "buy": None, "sell": "1"}]}),
        ("GET", "/api/v1/market/orderbook/level2_20"): ok({"sequence": "9", "bids": [["0.26", "100"]] * 25,
                                                          "asks": [["0.2601", "50"]] * 25}),
        ("GET", "/api/v1/market/candles"): ok([["120", "1", "2", "3", "0.5", "10", "20"],
                                               ["60", "1.1", "1.2", "1.3", "1.0", "11", "21"]]),
    })
    info = await c.exchange_info()
    assert list(info) == ["CARDSUSDT"] and info["CARDSUSDT"].tick_size == Decimal("0.0001")
    assert info["CARDSUSDT"].step_size == Decimal("0.01") and c.kc_symbol("CARDSUSDT") == "CARDS-USDT"
    t = (await c.book_tickers())["CARDSUSDT"]
    assert (t.bid, t.bid_qty, t.ask, t.ask_qty, t.ts_ms) == (Decimal("0.26"), Decimal("100"), Decimal("0.2601"),
                                                             Decimal("50"), 123)
    d = await c.depth("CARDSUSDT", limit=5)
    assert len(d["bids"]) == 5 and d["asks"][0] == ["0.2601", "50"]
    k = await c.klines("CARDSUSDT", "1m", start_ms=0)
    # oldest first; Binance column order open, high, low, close (KuCoin sends open, close, high, low)
    assert k[0] == [60000, "1.1", "1.3", "1.0", "1.2", "11", 119999, "21"] and k[1][0] == 120000


def test_unknown_symbol_falls_back_to_dash_form():
    c = KucoinClient(session=None, creds=None)
    assert c.kc_symbol("PEPEUSDT") == "PEPE-USDT" and c.kc_symbol("ETHBTC") == "ETH-BTC"


@pytest.mark.parametrize("accounts, path", [([{"type": "trade_hf", "currency": "USDT"}], "/api/v1/hf/orders"),
                                            ([{"type": "trade", "currency": "USDT"}], "/api/v1/orders")])
async def test_ioc_order_body_and_account_kind(accounts, path):
    c = client({("GET", "/api/v1/accounts"): ok(accounts), ("POST", path): ok({"orderId": "abc"})})
    r = await c.place_order("CARDSUSDT", "BUY", "IMMEDIATE_OR_CANCEL", quantity=Decimal("431.00"),
                            price=Decimal("0.2655"), client_order_id="sp_pent_331_1")
    body = json.loads(c._request.calls[-1]["body"])
    assert body == {"clientOid": "sp_pent_331_1", "side": "buy", "symbol": "CARDS-USDT", "type": "limit",
                    "timeInForce": "IOC", "price": "0.2655", "size": "431.00"}
    assert r.order_id == "abc" and r.executed_qty == 0 and r.status == "NEW"


async def test_order_states():
    p = KucoinClient._parse_order
    f = p({"id": "1", "symbol": "X-USDT", "side": "buy", "size": "10", "dealSize": "10", "dealFunds": "25",
           "isActive": False})
    assert f.status == "FILLED" and f.is_filled and f.avg_price == Decimal("2.5") and f.symbol == "XUSDT"
    ioc = p({"id": "2", "size": "10", "dealSize": "4", "dealFunds": "10", "active": False, "cancelExist": True})
    assert ioc.status == "CANCELED" and ioc.executed_qty == Decimal("4") and ioc.avg_price == Decimal("2.5")
    rest = p({"id": "3", "size": "10", "dealSize": "1", "dealFunds": "2", "isActive": True})
    assert rest.status == "PARTIALLY_FILLED" and rest.is_open


async def test_client_id_lookup_missing_is_none():
    def missing(q, d):
        raise ExchangeError("kucoin", "order not exist.", "400100")
    c = client({("GET", "/api/v1/order/client-order/sp_x"): missing}, hf=False)
    assert await c.get_order_by_client_id("XUSDT", "sp_x") is None


async def test_error_code_raises():
    c = client({("GET", "/api/v1/market/allTickers"): {"code": "400100", "msg": "bad"}})
    with pytest.raises(ExchangeError):
        await c.book_tickers()


async def test_account_sums_trading_accounts_only():
    c = client({("GET", "/api/v1/accounts"): ok([
        {"currency": "USDT", "type": "trade", "available": "100", "holds": "5"},
        {"currency": "USDT", "type": "trade_hf", "available": "20", "holds": "0"},
        {"currency": "USDT", "type": "main", "available": "999", "holds": "0"},
        {"currency": "CARDS", "type": "trade", "available": "431", "holds": "0"}])})
    bal = {b["asset"]: b for b in (await c.account())["balances"]}
    assert bal["USDT"] == {"asset": "USDT", "free": "120", "locked": "5"} and bal["CARDS"]["free"] == "431"


async def test_fills_classic_pages_and_seven_day_windows():
    day = 86_400_000
    fills = [{"symbol": "X-USDT", "tradeId": str(i), "orderId": f"o{i}", "side": "buy" if i % 2 else "sell",
              "price": "1", "size": "2", "funds": "2", "fee": "0.002", "feeCurrency": "USDT",
              "createdAt": i * day // 4} for i in range(60)]          # 15 days of fills

    def page(query, _):
        q = dict(kv.split("=") for kv in query.split("&"))
        rows = [f for f in fills if int(q["startAt"]) <= f["createdAt"] <= int(q["endAt"])]
        size, cur = int(q["pageSize"]), int(q["currentPage"])
        total = max(1, -(-len(rows) // size))
        return ok({"items": rows[(cur - 1) * size: cur * size], "totalPage": total})

    c = client({("GET", "/api/v1/fills"): page}, hf=False)
    got, complete = await c.my_trades_since("XUSDT", 0, 15 * day, page=10)
    assert complete and [g["id"] for g in got] == [str(i) for i in range(60)]
    assert got[1]["isBuyer"] and not got[0]["isBuyer"] and got[0]["qty"] == "2" and got[0]["commission"] == "0.002"
    windows = {c_["query"].split("&")[1] for c_ in c._request.calls}
    assert len(windows) == 3                                           # 0-7d, 7-14d, 14-15d
    got, complete = await c.my_trades_since("XUSDT", 0, 15 * day, page=10, max_pages=2)
    assert not complete


async def test_fills_hf_cursor():
    fills = [{"symbol": "X-USDT", "id": i, "orderId": "o", "side": "buy", "price": "1", "size": "1",
              "funds": "1", "fee": "0", "feeCurrency": "USDT", "createdAt": 1000 + i} for i in range(150)]

    def page(query, _):
        q = dict(kv.split("=") for kv in query.split("&"))
        rows = [f for f in fills if f["id"] > int(q.get("lastId", -1))][: int(q["limit"])]
        return ok({"items": rows, "lastId": rows[-1]["id"] if rows else None})

    c = client({("GET", "/api/v1/hf/fills"): page}, hf=True)
    got, complete = await c.my_trades_since("XUSDT", 0, 10_000, page=100)
    assert complete and len(got) == 150


def test_kucoin_is_the_default_venue_with_its_fees(monkeypatch):
    import importlib
    assert config.SPOT_VENUE == "kucoin" and config.SPOT_NAME == "KuCoin"
    for k in ("SPOT_MAKER_FEE", "SPOT_TAKER_FEE", "MEXC_MAKER_FEE", "MEXC_TAKER_FEE"):
        monkeypatch.delenv(k, raising=False)          # conftest pins MEXC's fees for the old fixtures
    try:
        c = importlib.reload(config)
        assert c.MEXC_TAKER_FEE == Decimal("0.001") and c.MEXC_MAKER_FEE == Decimal("0.001")
        assert c.ENTRY_FEE == c.ASTER_MAKER_FEE + Decimal("0.001")
    finally:
        monkeypatch.undo()
        importlib.reload(config)
