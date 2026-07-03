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
