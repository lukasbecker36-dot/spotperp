"""MEXC client: server-time offset for signed-request clock alignment."""
import exchange_client
from auth import MexcCredentials
from exchange_client import MexcClient


async def test_mexc_time_offset_applied(monkeypatch):
    c = MexcClient(session=None, creds=MexcCredentials(api_key="k", api_secret="s"))
    monkeypatch.setattr(exchange_client, "now_ms", lambda: 1000)

    async def fake_request(method, path, **kw):
        assert path == "/api/v3/time"
        return {"serverTime": 6000}
    c._request = fake_request

    off = await c.sync_time()
    assert off == 5000 and c._time_offset_ms == 5000
    # A signed query now carries the server-aligned timestamp (1000 + 5000).
    assert "timestamp=6000" in c._signed_query({"symbol": "BTCUSDT"})


async def test_mexc_time_sync_failure_keeps_prior_offset(monkeypatch):
    c = MexcClient(session=None, creds=MexcCredentials(api_key="k", api_secret="s"))
    c._time_offset_ms = 42

    async def boom(method, path, **kw):
        raise exchange_client.ExchangeError("mexc", "timeout")
    c._request = boom

    assert await c.sync_time() == 42          # unchanged on failure


async def test_mexc_my_trades_since_pages_forward_without_gaps_or_repeats():
    from exchange_client import MexcClient
    fills = [{"id": i, "time": 1000 + i // 2, "qty": "1", "price": "1",
              "isBuyer": True} for i in range(250)]   # pairs share a millisecond
    client = MexcClient.__new__(MexcClient)

    async def my_trades(symbol, limit=100, start_ms=None, end_ms=None):
        rows = [t for t in fills if start_ms <= t["time"] <= end_ms]
        return rows[:limit]
    client.my_trades = my_trades
    got, complete = await client.my_trades_since("X", 1000, 2000, page=100)
    assert complete and [t["id"] for t in got] == list(range(250))
    got, complete = await client.my_trades_since("X", 1000, 2000, page=100,
                                                 max_pages=1)
    assert not complete
