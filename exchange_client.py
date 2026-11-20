"""Async REST wrappers for Aster DEX perps (V3) and the spot leg: MEXC spot (V3) or KuCoin spot.

Aster and MEXC are Binance-compatible in shape; KucoinClient translates KuCoin's API into the same
shapes (Binance-style symbols like "BTCUSDT", balances, fills, order states), so callers can't tell
the spot venues apart. All prices/quantities are Decimal.
Order placement raises ExchangeError on a definitive rejection; ambiguous
outcomes (timeouts, 5xx) raise AmbiguousOrderError so callers can reconcile.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

import aiohttp

import config
from auth import (AsterCredentials, KucoinCredentials, MexcCredentials, aster_sign, kucoin_headers,
                  mexc_sign, now_ms)

log = logging.getLogger(__name__)


class ExchangeError(Exception):
    def __init__(self, venue: str, message: str, code: int | None = None):
        super().__init__(f"{venue}: {message} (code={code})")
        self.venue = venue
        self.code = code


class AmbiguousOrderError(ExchangeError):
    """The order may or may not have reached the engine (timeout / 5xx)."""


@dataclass(frozen=True)
class BookTicker:
    symbol: str
    bid: Decimal
    bid_qty: Decimal
    ask: Decimal
    ask_qty: Decimal
    ts_ms: int

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal

    def round_price(self, price: Decimal, *, up: bool) -> Decimal:
        q = (price / self.tick_size).to_integral_value(
            rounding=ROUND_UP if up else ROUND_DOWN
        )
        return q * self.tick_size

    def round_qty(self, qty: Decimal) -> Decimal:
        q = (qty / self.step_size).to_integral_value(rounding=ROUND_DOWN)
        return q * self.step_size


@dataclass
class OrderResult:
    venue: str
    symbol: str
    order_id: str
    client_order_id: str
    side: str
    status: str               # NEW / PARTIALLY_FILLED / FILLED / CANCELED / EXPIRED ...
    price: Decimal
    orig_qty: Decimal
    executed_qty: Decimal
    avg_price: Decimal
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_open(self) -> bool:
        return self.status in ("NEW", "PARTIALLY_FILLED")

    @property
    def is_filled(self) -> bool:
        return self.status == "FILLED"


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


class _BaseClient:
    def __init__(self, base_url: str, session: aiohttp.ClientSession):
        self._base = base_url.rstrip("/")
        self._session = session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: str | None = None,
        headers: dict[str, str] | None = None,
        venue: str,
        order_endpoint: bool = False,
        timeout: float = 10.0,
    ) -> Any:
        url = self._base + path
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
                if resp.status in (429, 418):
                    # Rate limited / IP-banned: the request was rejected before
                    # processing (order NOT placed), so it's a definitive error —
                    # but back off first so the caller's retry doesn't hammer us
                    # straight into a longer 418 ban.
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else 2.0
                    log.warning(
                        "%s rate limited (HTTP %s), backing off %.1fs (weight=%s)",
                        venue, resp.status, delay,
                        resp.headers.get("X-MBX-USED-WEIGHT-1M"),
                    )
                    await asyncio.sleep(min(delay, 30.0))
                    raise ExchangeError(venue, f"rate limited HTTP {resp.status}")
                if resp.status >= 500:
                    err = AmbiguousOrderError if order_endpoint else ExchangeError
                    raise err(venue, f"HTTP {resp.status}: {text[:300]}")
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    # A 2xx with an unparseable body on an order endpoint means
                    # the order MAY have been accepted — treat as ambiguous so
                    # the caller reconciles instead of assuming it failed.
                    err = AmbiguousOrderError if order_endpoint else ExchangeError
                    raise err(
                        venue,
                        f"non-JSON response (HTTP {resp.status}): {text[:300]}",
                    )
                if resp.status >= 400:
                    code = payload.get("code") if isinstance(payload, dict) else None
                    msg = payload.get("msg") if isinstance(payload, dict) else text
                    raise ExchangeError(venue, str(msg), code)
                if isinstance(payload, dict) and "code" in payload and "msg" in payload \
                        and payload.get("code") not in (0, 200, "0", "200", "200000", None):
                    raise ExchangeError(venue, str(payload["msg"]), payload["code"])
                return payload
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            err = AmbiguousOrderError if order_endpoint else ExchangeError
            raise err(venue, f"transport error: {exc!r}") from exc


# ─────────────────────────────── Aster perps ───────────────────────────────

class AsterClient(_BaseClient):
    VENUE = "aster"

    def __init__(self, session: aiohttp.ClientSession, creds: AsterCredentials | None):
        super().__init__(config.ASTER_BASE, session)
        self._creds = creds

    def _signed_body(self, params: dict[str, str]) -> str:
        if self._creds is None:
            raise ExchangeError(self.VENUE, "no credentials configured")
        import urllib.parse

        return urllib.parse.urlencode(aster_sign(params, self._creds))

    # ── market data (unsigned) ──

    async def exchange_info(self) -> dict[str, SymbolInfo]:
        payload = await self._request(
            "GET", "/fapi/v3/exchangeInfo", venue=self.VENUE, timeout=20
        )
        out: dict[str, SymbolInfo] = {}
        for sym in payload.get("symbols", []):
            if sym.get("status") not in (None, "TRADING"):
                continue
            tick = step = min_notional = Decimal("0")
            for f in sym.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    tick = _dec(f.get("tickSize"))
                elif f.get("filterType") == "LOT_SIZE":
                    step = _dec(f.get("stepSize"))
                elif f.get("filterType") == "MIN_NOTIONAL":
                    min_notional = _dec(f.get("notional"))
            if not tick or not step:
                continue
            out[sym["symbol"]] = SymbolInfo(
                symbol=sym["symbol"],
                base_asset=sym.get("baseAsset", ""),
                quote_asset=sym.get("quoteAsset", ""),
                tick_size=tick,
                step_size=step,
                min_notional=min_notional,
            )
        return out

    async def book_tickers(self) -> dict[str, BookTicker]:
        payload = await self._request(
            "GET", "/fapi/v3/ticker/bookTicker", venue=self.VENUE
        )
        ts = now_ms()
        out = {}
        for t in payload if isinstance(payload, list) else [payload]:
            bid, ask = _dec(t.get("bidPrice")), _dec(t.get("askPrice"))
            if bid <= 0 or ask <= 0:
                continue
            out[t["symbol"]] = BookTicker(
                symbol=t["symbol"],
                bid=bid,
                bid_qty=_dec(t.get("bidQty")),
                ask=ask,
                ask_qty=_dec(t.get("askQty")),
                ts_ms=int(t.get("time") or ts),
            )
        return out

    async def ticker_24hr(self) -> dict[str, dict[str, Decimal]]:
        """24h traded volume and trade count for every perp, in one call.

        Depth says the book is not empty; VOLUME says someone is actually
        trading. A resting maker entry only fills when a taker lifts it, so a
        wide basis on a symbol nobody trades is untradeable however good the
        number looks.
        """
        # Aster serves some market data on v3 (bookTicker, premiumIndex, depth)
        # and some only on v1 (klines, fundingRate). Try both rather than guess:
        # a silent failure here leaves every row at volume 0 and empties
        # /screen fill with no obvious cause.
        payload = None
        last_err: ExchangeError | None = None
        for path in ("/fapi/v3/ticker/24hr", "/fapi/v1/ticker/24hr"):
            try:
                payload = await self._request("GET", path, venue=self.VENUE)
                break
            except ExchangeError as exc:
                last_err = exc
        if payload is None:
            raise last_err or ExchangeError(
                self.VENUE, "ticker/24hr unavailable on v3 or v1"
            )
        out: dict[str, dict[str, Decimal]] = {}
        for row in payload if isinstance(payload, list) else [payload]:
            sym = row.get("symbol")
            if not sym:
                continue
            out[sym] = {
                "quote_volume": _dec(row.get("quoteVolume")),
                "trades": Decimal(int(row.get("count") or 0)),
            }
        return out

    async def premium_index(self) -> dict[str, dict[str, Decimal]]:
        """mark price, index price and current funding rate for all symbols."""
        payload = await self._request(
            "GET", "/fapi/v3/premiumIndex", venue=self.VENUE
        )
        out = {}
        for row in payload if isinstance(payload, list) else [payload]:
            out[row["symbol"]] = {
                "mark_price": _dec(row.get("markPrice")),
                "index_price": _dec(row.get("indexPrice")),
                "funding_rate": _dec(row.get("lastFundingRate")),
                "next_funding_time": Decimal(int(row.get("nextFundingTime") or 0)),
            }
        return out

    async def depth(self, symbol: str, limit: int = 20) -> dict[str, list]:
        return await self._request(
            "GET",
            "/fapi/v3/depth",
            params={"symbol": symbol, "limit": str(limit)},
            venue=self.VENUE,
        )

    async def klines(
        self, symbol: str, interval: str = "1m",
        start_ms: int | None = None, end_ms: int | None = None,
        limit: int = 1500,
    ) -> list[list]:
        params: dict[str, str] = {
            "symbol": symbol, "interval": interval, "limit": str(limit),
        }
        if start_ms is not None:
            params["startTime"] = str(start_ms)
        if end_ms is not None:
            params["endTime"] = str(end_ms)
        return await self._request(
            "GET", "/fapi/v1/klines", params=params, venue=self.VENUE,
        )

    async def funding_rate_history(
        self, symbol: str, limit: int = 10
    ) -> list[tuple[int, Decimal]]:
        """Recent funding prints as (funding_time_ms, funding_rate) ascending.

        The spacing of the timestamps reveals the symbol's funding interval and
        the rates give the realised funding for 24h averaging.
        """
        payload = await self._request(
            "GET",
            "/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": str(limit)},
            venue=self.VENUE,
        )
        rows = [
            (int(r.get("fundingTime") or 0), _dec(r.get("fundingRate")))
            for r in (payload if isinstance(payload, list) else [])
        ]
        rows.sort(key=lambda r: r[0])
        return rows

    # ── trading (signed) ──

    @staticmethod
    def _parse_order(payload: dict[str, Any]) -> OrderResult:
        return OrderResult(
            venue=AsterClient.VENUE,
            symbol=payload.get("symbol", ""),
            order_id=str(payload.get("orderId", "")),
            client_order_id=str(payload.get("clientOrderId", "")),
            side=payload.get("side", ""),
            status=payload.get("status", "NEW"),
            price=_dec(payload.get("price")),
            orig_qty=_dec(payload.get("origQty")),
            executed_qty=_dec(payload.get("executedQty") or payload.get("cumQty")),
            avg_price=_dec(payload.get("avgPrice")),
            raw=payload,
        )

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        *,
        quantity: Decimal | None = None,
        price: Decimal | None = None,
        time_in_force: str | None = None,
        reduce_only: bool = False,
        stop_price: Decimal | None = None,
        working_type: str | None = None,
        client_order_id: str | None = None,
    ) -> OrderResult:
        params: dict[str, str] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "newOrderRespType": "RESULT",
        }
        if quantity is not None:
            params["quantity"] = format(quantity, "f")
        if price is not None:
            params["price"] = format(price, "f")
        if time_in_force:
            params["timeInForce"] = time_in_force
        if reduce_only:
            params["reduceOnly"] = "true"
        if stop_price is not None:
            params["stopPrice"] = format(stop_price, "f")
        if working_type:
            params["workingType"] = working_type
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        body = self._signed_body(params)
        payload = await self._request(
            "POST",
            "/fapi/v3/order",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            venue=self.VENUE,
            order_endpoint=True,
        )
        return self._parse_order(payload)

    async def get_order(self, symbol: str, order_id: str) -> OrderResult:
        body = self._signed_body({"symbol": symbol, "orderId": order_id})
        payload = await self._request(
            "GET",
            f"/fapi/v3/order?{body}",
            venue=self.VENUE,
        )
        return self._parse_order(payload)

    async def get_order_by_client_id(
        self, symbol: str, client_order_id: str
    ) -> OrderResult | None:
        """Look an order up by our client id (for crash recovery). Returns None
        if the venue has no such order (it never landed)."""
        body = self._signed_body(
            {"symbol": symbol, "origClientOrderId": client_order_id}
        )
        try:
            payload = await self._request(
                "GET", f"/fapi/v3/order?{body}", venue=self.VENUE
            )
        except ExchangeError as exc:
            if exc.code in (-2013, "-2013"):   # "Order does not exist"
                return None
            raise
        return self._parse_order(payload)

    async def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        body = self._signed_body({"symbol": symbol, "orderId": order_id})
        payload = await self._request(
            "DELETE",
            "/fapi/v3/order",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            venue=self.VENUE,
            order_endpoint=True,
        )
        return self._parse_order(payload)

    async def open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        params: dict[str, str] = {}
        if symbol:
            params["symbol"] = symbol
        body = self._signed_body(params)
        payload = await self._request(
            "GET", f"/fapi/v3/openOrders?{body}", venue=self.VENUE
        )
        return [self._parse_order(o) for o in payload]

    async def position_risk(self) -> list[dict[str, Any]]:
        body = self._signed_body({})
        return await self._request(
            "GET", f"/fapi/v3/positionRisk?{body}", venue=self.VENUE
        )

    async def balances(self) -> list[dict[str, Any]]:
        body = self._signed_body({})
        return await self._request(
            "GET", f"/fapi/v3/balance?{body}", venue=self.VENUE
        )

    async def income_history(
        self, symbol: str, income_type: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        body = self._signed_body(
            {
                "symbol": symbol,
                "incomeType": income_type,
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": "1000",
            }
        )
        return await self._request(
            "GET", f"/fapi/v3/income?{body}", venue=self.VENUE
        )

    async def user_trades(
        self, symbol: str, start_ms: int, end_ms: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Executed trades for one symbol in a time window — the venue's own
        record of what price a fill actually got. The only authority when an
        order id has been lost (a stop that fired while the engine was
        restarting) or when a close was booked synthetically at mark."""
        body = self._signed_body(
            {
                "symbol": symbol,
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": str(limit),
            }
        )
        return await self._request(
            "GET", f"/fapi/v3/userTrades?{body}", venue=self.VENUE
        )

    async def _post_margin(self, paths: list[str], params: dict[str, str]) -> Any:
        """POST a signed margin/leverage change, trying each path in turn.
        Aster mirrors the Binance endpoints but the version prefix is not
        certain, so we try v3 then v1. 'No need to change' (already at the
        requested value) is treated as success."""
        last_exc: ExchangeError | None = None
        for path in paths:
            try:
                body = self._signed_body(params)  # fresh nonce per attempt
                return await self._request(
                    "POST", path, data=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    venue=self.VENUE,
                )
            except ExchangeError as exc:
                if exc.code == -4046 or "no need to change" in str(exc).lower():
                    return {"already_set": True}
                last_exc = exc
        raise last_exc if last_exc else ExchangeError(self.VENUE, "no margin path")

    async def set_leverage(self, symbol: str, leverage: int) -> Any:
        return await self._post_margin(
            ["/fapi/v3/leverage", "/fapi/v1/leverage"],
            {"symbol": symbol, "leverage": str(leverage)},
        )

    async def set_margin_type(self, symbol: str, margin_type: str) -> Any:
        return await self._post_margin(
            ["/fapi/v3/marginType", "/fapi/v1/marginType"],
            {"symbol": symbol, "marginType": margin_type.upper()},
        )


# ─────────────────────────────── MEXC spot ────────────────────────────────

class MexcClient(_BaseClient):
    VENUE = "mexc"

    def __init__(self, session: aiohttp.ClientSession, creds: MexcCredentials | None):
        super().__init__(config.MEXC_BASE, session)
        self._creds = creds
        self._time_offset_ms = 0   # server_time - local_time, kept by sync_time()

    async def sync_time(self) -> int:
        """Measure the MEXC server-clock offset so signed requests carry a
        timestamp the server accepts. Without this, >5s of local clock skew
        (VPS drift / ntpd restart) makes EVERY signed call fail (code 700003),
        including the hedge and exit. Returns the offset in ms; best-effort."""
        try:
            payload = await self._request("GET", "/api/v3/time", venue=self.VENUE)
            server_ms = int(payload["serverTime"])
        except (ExchangeError, KeyError, ValueError, TypeError):
            log.exception("mexc time sync failed; keeping offset %s", self._time_offset_ms)
            return self._time_offset_ms
        self._time_offset_ms = server_ms - now_ms()
        if abs(self._time_offset_ms) > 1000:
            log.warning("mexc clock skew %sms — applying offset", self._time_offset_ms)
        return self._time_offset_ms

    def _signed_query(self, params: dict[str, str]) -> str:
        if self._creds is None:
            raise ExchangeError(self.VENUE, "no credentials configured")
        signed = dict(params)
        signed["timestamp"] = str(now_ms() + self._time_offset_ms)
        signed["recvWindow"] = "5000"
        return mexc_sign(signed, self._creds)

    def _auth_headers(self) -> dict[str, str]:
        # MEXC rejects any other content type on signed endpoints (code 700013);
        # all parameters go in the query string, never in a form body.
        assert self._creds is not None
        return {
            "X-MEXC-APIKEY": self._creds.api_key,
            "Content-Type": "application/json",
        }

    # ── market data (unsigned) ──

    async def exchange_info(self) -> dict[str, SymbolInfo]:
        payload = await self._request(
            "GET", "/api/v3/exchangeInfo", venue=self.VENUE, timeout=20
        )
        out: dict[str, SymbolInfo] = {}
        for sym in payload.get("symbols", []):
            # status "1" / "ENABLED" = trading; also require API spot trading
            if not sym.get("isSpotTradingAllowed", True):
                continue
            if str(sym.get("status")) not in ("1", "ENABLED", "TRADING"):
                continue
            # MEXC expresses precision rather than Binance-style filters.
            price_scale = int(sym.get("quotePrecision", sym.get("quoteAssetPrecision", 8)))
            qty_scale = int(sym.get("baseAssetPrecision", 8))
            out[sym["symbol"]] = SymbolInfo(
                symbol=sym["symbol"],
                base_asset=sym.get("baseAsset", ""),
                quote_asset=sym.get("quoteAsset", ""),
                tick_size=Decimal(1).scaleb(-price_scale),
                step_size=Decimal(1).scaleb(-qty_scale),
                min_notional=_dec(sym.get("quoteAmountPrecision"), "1"),
            )
        return out

    async def book_tickers(self) -> dict[str, BookTicker]:
        payload = await self._request(
            "GET", "/api/v3/ticker/bookTicker", venue=self.VENUE
        )
        ts = now_ms()
        out = {}
        for t in payload if isinstance(payload, list) else [payload]:
            bid, ask = _dec(t.get("bidPrice")), _dec(t.get("askPrice"))
            if bid <= 0 or ask <= 0:
                continue
            out[t["symbol"]] = BookTicker(
                symbol=t["symbol"],
                bid=bid,
                bid_qty=_dec(t.get("bidQty")),
                ask=ask,
                ask_qty=_dec(t.get("askQty")),
                ts_ms=ts,
            )
        return out

    async def depth(self, symbol: str, limit: int = 20) -> dict[str, list]:
        return await self._request(
            "GET",
            "/api/v3/depth",
            params={"symbol": symbol, "limit": str(limit)},
            venue=self.VENUE,
        )

    async def klines(
        self, symbol: str, interval: str = "1m",
        start_ms: int | None = None, end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[list]:
        params: dict[str, str] = {
            "symbol": symbol, "interval": interval, "limit": str(limit),
        }
        if start_ms is not None:
            params["startTime"] = str(start_ms)
        if end_ms is not None:
            params["endTime"] = str(end_ms)
        return await self._request(
            "GET", "/api/v3/klines", params=params, venue=self.VENUE,
        )

    # ── trading (signed) ──

    @staticmethod
    def _parse_order(payload: dict[str, Any]) -> OrderResult:
        executed = _dec(payload.get("executedQty"))
        quote_executed = _dec(payload.get("cummulativeQuoteQty"))
        avg = quote_executed / executed if executed > 0 else Decimal("0")
        return OrderResult(
            venue=MexcClient.VENUE,
            symbol=payload.get("symbol", ""),
            order_id=str(payload.get("orderId", "")),
            client_order_id=str(payload.get("clientOrderId", "")),
            side=payload.get("side", ""),
            status=payload.get("status", "NEW"),
            price=_dec(payload.get("price")),
            orig_qty=_dec(payload.get("origQty")),
            executed_qty=executed,
            avg_price=avg,
            raw=payload,
        )

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        *,
        quantity: Decimal | None = None,
        quote_order_qty: Decimal | None = None,
        price: Decimal | None = None,
        client_order_id: str | None = None,
    ) -> OrderResult:
        params: dict[str, str] = {"symbol": symbol, "side": side, "type": order_type}
        if quantity is not None:
            params["quantity"] = format(quantity, "f")
        if quote_order_qty is not None:
            params["quoteOrderQty"] = format(quote_order_qty, "f")
        if price is not None:
            params["price"] = format(price, "f")
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        query = self._signed_query(params)
        payload = await self._request(
            "POST",
            f"/api/v3/order?{query}",
            headers=self._auth_headers(),
            venue=self.VENUE,
            order_endpoint=True,
        )
        return self._parse_order(payload)

    async def get_order(self, symbol: str, order_id: str) -> OrderResult:
        query = self._signed_query({"symbol": symbol, "orderId": order_id})
        payload = await self._request(
            "GET",
            f"/api/v3/order?{query}",
            headers=self._auth_headers(),
            venue=self.VENUE,
        )
        return self._parse_order(payload)

    async def get_order_by_client_id(
        self, symbol: str, client_order_id: str
    ) -> OrderResult | None:
        """Look an order up by our client id (crash recovery). None if the venue
        has no such order (it never landed)."""
        query = self._signed_query(
            {"symbol": symbol, "origClientOrderId": client_order_id}
        )
        try:
            payload = await self._request(
                "GET", f"/api/v3/order?{query}",
                headers=self._auth_headers(), venue=self.VENUE,
            )
        except ExchangeError as exc:
            if exc.code in (-2013, "-2013") or "does not exist" in str(exc).lower():
                return None
            raise
        return self._parse_order(payload)

    async def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        query = self._signed_query({"symbol": symbol, "orderId": order_id})
        payload = await self._request(
            "DELETE",
            f"/api/v3/order?{query}",
            headers=self._auth_headers(),
            venue=self.VENUE,
            order_endpoint=True,
        )
        return self._parse_order(payload)

    async def open_orders(self, symbol: str) -> list[OrderResult]:
        query = self._signed_query({"symbol": symbol})
        payload = await self._request(
            "GET",
            f"/api/v3/openOrders?{query}",
            headers=self._auth_headers(),
            venue=self.VENUE,
        )
        return [self._parse_order(o) for o in payload]

    async def account(self) -> dict[str, Any]:
        query = self._signed_query({})
        return await self._request(
            "GET",
            f"/api/v3/account?{query}",
            headers=self._auth_headers(),
            venue=self.VENUE,
        )

    async def my_trades(
        self, symbol: str, limit: int = 100,
        start_ms: int | None = None, end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        params = {"symbol": symbol, "limit": str(limit)}
        if start_ms is not None:
            params["startTime"] = str(int(start_ms))
        if end_ms is not None:
            params["endTime"] = str(int(end_ms))
        query = self._signed_query(params)
        return await self._request(
            "GET",
            f"/api/v3/myTrades?{query}",
            headers=self._auth_headers(),
            venue=self.VENUE,
        )

    async def my_trades_since(
        self, symbol: str, start_ms: int, end_ms: int, page: int = 100,
        max_pages: int = 20,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Every fill in [start_ms, end_ms], paging forward by time (a single
        call returns at most `page` rows). Returns (trades oldest-first,
        complete) — complete is False if max_pages ran out first."""
        out: list[dict[str, Any]] = []
        seen: set = set()
        cursor = int(start_ms)
        for _ in range(max_pages):
            rows = await self.my_trades(symbol, limit=page, start_ms=cursor,
                                        end_ms=end_ms)
            rows = sorted(rows or [], key=lambda t: int(t.get("time", 0)))
            fresh = [t for t in rows if (t.get("id"), t.get("time")) not in seen]
            for t in fresh:
                seen.add((t.get("id"), t.get("time")))
            out.extend(fresh)
            if len(rows) < page:
                return sorted(out, key=lambda t: int(t.get("time", 0))), True
            last = int(rows[-1].get("time", cursor))
            # Re-ask from the last fill's own millisecond: fills sharing it
            # would otherwise be skipped; `seen` drops the repeats.
            cursor = last if last > cursor else cursor + 1
        return sorted(out, key=lambda t: int(t.get("time", 0))), False


# ─────────────────────────────── KuCoin spot ───────────────────────────────

_KC_INTERVALS = {"1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
                 "1h": "1hour", "2h": "2hour", "4h": "4hour", "6h": "6hour", "8h": "8hour",
                 "12h": "12hour", "1d": "1day", "1w": "1week"}
_KC_INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
                   "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
                   "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000, "1w": 604_800_000}
_KC_FILL_WINDOW_MS = 7 * 86_400_000        # KuCoin fill queries span at most 7 days


class KucoinClient(_BaseClient):
    """KuCoin spot behind MexcClient's interface: Binance-style symbols ("BTCUSDT" <-> "BTC-USDT"),
    {"balances": [{"asset","free","locked"}]}, fills with qty/price/commission/time/isBuyer, OrderResult.

    Accounts come in two kinds with different trading endpoints: classic ("trade") and high-frequency
    ("trade_hf", which KuCoin has migrated many accounts to). The kind is read from the account list on
    first use, or forced with KUCOIN_HF=1/0."""
    VENUE = "kucoin"

    def __init__(self, session: aiohttp.ClientSession, creds: KucoinCredentials | None):
        super().__init__(config.KUCOIN_BASE, session)
        self._creds = creds
        self._time_offset_ms = 0
        self._kc: dict[str, str] = {}             # "BTCUSDT" -> "BTC-USDT", filled by exchange_info()
        forced = (os.environ.get("KUCOIN_HF") or "").strip().lower()
        self._hf: bool | None = None if forced == "" else forced in ("1", "true", "yes")

    # ── plumbing ──

    def kc_symbol(self, symbol: str) -> str:
        if symbol in self._kc:
            return self._kc[symbol]
        if "-" in symbol:
            return symbol
        for quote in ("USDT", "USDC", "BTC", "ETH"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return f"{symbol[:-len(quote)]}-{quote}"
        return symbol

    @staticmethod
    def int_symbol(kc_symbol: str) -> str:
        return kc_symbol.replace("-", "")

    async def _call(self, method: str, path: str, *, params: dict[str, str] | None = None,
                    body: dict[str, Any] | None = None, signed: bool = False,
                    order_endpoint: bool = False, timeout: float = 10.0) -> Any:
        """One request; returns KuCoin's `data`. A code other than 200000 raises ExchangeError."""
        endpoint = path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = None
        if signed:
            if self._creds is None:
                raise ExchangeError(self.VENUE, "no credentials configured")
            headers = kucoin_headers(self._creds, now_ms() + self._time_offset_ms, method, endpoint, data)
        payload = await self._request(method, endpoint, data=data or None, headers=headers,
                                      venue=self.VENUE, order_endpoint=order_endpoint, timeout=timeout)
        if not isinstance(payload, dict):
            raise ExchangeError(self.VENUE, f"unexpected reply: {str(payload)[:200]}")
        if str(payload.get("code")) != "200000":
            raise ExchangeError(self.VENUE, str(payload.get("msg") or payload)[:300], payload.get("code"))
        return payload.get("data")

    async def is_hf(self) -> bool:
        """True if the account trades through KuCoin's high-frequency ('trade_hf') account."""
        if self._hf is None:
            accounts = await self._call("GET", "/api/v1/accounts", signed=True)
            self._hf = any(a.get("type") == "trade_hf" for a in accounts or [])
            log.info("kucoin account kind: %s", "high-frequency" if self._hf else "classic")
        return self._hf

    async def sync_time(self) -> int:
        """Server-clock offset for signed requests (KuCoin rejects timestamps more than 5s off)."""
        try:
            server_ms = int(await self._call("GET", "/api/v1/timestamp"))
        except (ExchangeError, ValueError, TypeError):
            log.exception("kucoin time sync failed; keeping offset %s", self._time_offset_ms)
            return self._time_offset_ms
        self._time_offset_ms = server_ms - now_ms()
        if abs(self._time_offset_ms) > 1000:
            log.warning("kucoin clock skew %sms — applying offset", self._time_offset_ms)
        return self._time_offset_ms

    # ── market data (unsigned) ──

    async def exchange_info(self) -> dict[str, SymbolInfo]:
        rows = await self._call("GET", "/api/v2/symbols", timeout=20)
        out: dict[str, SymbolInfo] = {}
        for s in rows or []:
            if not s.get("enableTrading"):
                continue
            sym = self.int_symbol(s["symbol"])
            self._kc[sym] = s["symbol"]
            out[sym] = SymbolInfo(
                symbol=sym,
                base_asset=s.get("baseCurrency", ""),
                quote_asset=s.get("quoteCurrency", ""),
                tick_size=_dec(s.get("priceIncrement"), "0.00000001"),
                step_size=_dec(s.get("baseIncrement"), "0.00000001"),
                min_notional=_dec(s.get("minFunds"), "1"),
            )
        return out

    async def book_tickers(self) -> dict[str, BookTicker]:
        data = await self._call("GET", "/api/v1/market/allTickers", timeout=20)
        ts = int(data.get("time") or now_ms())
        out = {}
        for t in data.get("ticker", []):
            bid, ask = _dec(t.get("buy")), _dec(t.get("sell"))
            if bid <= 0 or ask <= 0:
                continue
            sym = self.int_symbol(t["symbol"])
            self._kc.setdefault(sym, t["symbol"])
            out[sym] = BookTicker(symbol=sym, bid=bid, bid_qty=_dec(t.get("bestBidSize")),
                                  ask=ask, ask_qty=_dec(t.get("bestAskSize")), ts_ms=ts)
        return out

    async def depth(self, symbol: str, limit: int = 20) -> dict[str, list]:
        path = "/api/v1/market/orderbook/level2_20" if limit <= 20 else "/api/v1/market/orderbook/level2_100"
        d = await self._call("GET", path, params={"symbol": self.kc_symbol(symbol)})
        return {"lastUpdateId": d.get("sequence"), "bids": (d.get("bids") or [])[:limit],
                "asks": (d.get("asks") or [])[:limit]}

    async def klines(self, symbol: str, interval: str = "1m", start_ms: int | None = None,
                     end_ms: int | None = None, limit: int = 1000) -> list[list]:
        """Binance-shaped rows, oldest first: [open_ms, open, high, low, close, volume, close_ms, quote_vol].
        KuCoin returns [time_s, open, close, high, low, volume, turnover], newest first."""
        params = {"type": _KC_INTERVALS.get(interval, interval), "symbol": self.kc_symbol(symbol)}
        if start_ms is not None:
            params["startAt"] = str(int(start_ms) // 1000)
        if end_ms is not None:
            params["endAt"] = str(int(end_ms) // 1000)
        rows = await self._call("GET", "/api/v1/market/candles", params=params)
        step = _KC_INTERVAL_MS.get(interval, 60_000)
        out = sorted(([int(r[0]) * 1000, r[1], r[3], r[4], r[2], r[5], int(r[0]) * 1000 + step - 1, r[6]]
                      for r in rows or []), key=lambda r: r[0])
        return out[:limit] if start_ms is not None else out[-limit:]

    # ── trading (signed) ──

    @staticmethod
    def _parse_order(d: dict[str, Any], symbol: str = "") -> OrderResult:
        size, executed = _dec(d.get("size")), _dec(d.get("dealSize"))
        funds = _dec(d.get("dealFunds"))
        active = d.get("isActive", d.get("active"))
        if active:
            status = "PARTIALLY_FILLED" if executed > 0 else "NEW"
        elif size > 0 and executed >= size:
            status = "FILLED"
        else:
            status = "CANCELED"          # cancelled, or an IOC's unfilled rest (executed_qty keeps what filled)
        return OrderResult(
            venue=KucoinClient.VENUE,
            symbol=KucoinClient.int_symbol(d.get("symbol", "")) or symbol,
            order_id=str(d.get("id") or d.get("orderId") or ""),
            client_order_id=str(d.get("clientOid") or ""),
            side=str(d.get("side", "")).upper(),
            status=status,
            price=_dec(d.get("price")),
            orig_qty=size,
            executed_qty=executed,
            avg_price=funds / executed if executed > 0 else Decimal("0"),
            raw=d,
        )

    async def place_order(self, symbol: str, side: str, order_type: str, *,
                          quantity: Decimal | None = None, quote_order_qty: Decimal | None = None,
                          price: Decimal | None = None, client_order_id: str | None = None) -> OrderResult:
        """order_type: LIMIT (GTC), IMMEDIATE_OR_CANCEL (limit IOC), LIMIT_MAKER (post-only), MARKET."""
        body: dict[str, Any] = {"clientOid": client_order_id or uuid.uuid4().hex, "side": side.lower(),
                                "symbol": self.kc_symbol(symbol)}
        t = order_type.upper()
        if t == "MARKET":
            body["type"] = "market"
        else:
            body.update(type="limit", timeInForce="IOC" if t == "IMMEDIATE_OR_CANCEL" else "GTC")
            if t == "LIMIT_MAKER":
                body["postOnly"] = True
        if price is not None and t != "MARKET":
            body["price"] = format(price, "f")
        if quantity is not None:
            body["size"] = format(quantity, "f")
        if quote_order_qty is not None:
            body["funds"] = format(quote_order_qty, "f")
        path = "/api/v1/hf/orders" if await self.is_hf() else "/api/v1/orders"
        d = await self._call("POST", path, body=body, signed=True, order_endpoint=True)
        # The place reply carries only the ids, so report it as NEW: callers re-query for the fill
        # (the executor already does, as MEXC's IOC replies could omit executed qty too).
        return OrderResult(venue=self.VENUE, symbol=symbol, order_id=str((d or {}).get("orderId", "")),
                           client_order_id=body["clientOid"], side=side.upper(), status="NEW",
                           price=price or Decimal("0"), orig_qty=quantity or Decimal("0"),
                           executed_qty=Decimal("0"), avg_price=Decimal("0"), raw=d or {})

    async def get_order(self, symbol: str, order_id: str) -> OrderResult:
        if await self.is_hf():
            d = await self._call("GET", f"/api/v1/hf/orders/{order_id}",
                                 params={"symbol": self.kc_symbol(symbol)}, signed=True)
        else:
            d = await self._call("GET", f"/api/v1/orders/{order_id}", signed=True)
        return self._parse_order(d or {}, symbol)

    async def get_order_by_client_id(self, symbol: str, client_order_id: str) -> OrderResult | None:
        """Look an order up by our client id (crash recovery). None if KuCoin has no such order."""
        try:
            if await self.is_hf():
                d = await self._call("GET", f"/api/v1/hf/orders/client-order/{client_order_id}",
                                     params={"symbol": self.kc_symbol(symbol)}, signed=True)
            else:
                d = await self._call("GET", f"/api/v1/order/client-order/{client_order_id}", signed=True)
        except ExchangeError as exc:
            if "not exist" in str(exc).lower() or "not found" in str(exc).lower():
                return None
            raise
        return self._parse_order(d, symbol) if d else None

    async def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        if await self.is_hf():
            await self._call("DELETE", f"/api/v1/hf/orders/{order_id}",
                             params={"symbol": self.kc_symbol(symbol)}, signed=True, order_endpoint=True)
        else:
            await self._call("DELETE", f"/api/v1/orders/{order_id}", signed=True, order_endpoint=True)
        return await self.get_order(symbol, order_id)      # cancel replies carry only ids

    async def open_orders(self, symbol: str) -> list[OrderResult]:
        kc = self.kc_symbol(symbol)
        if await self.is_hf():
            rows = await self._call("GET", "/api/v1/hf/orders/active", params={"symbol": kc}, signed=True)
        else:
            d = await self._call("GET", "/api/v1/orders",
                                 params={"status": "active", "symbol": kc, "pageSize": "500"}, signed=True)
            rows = (d or {}).get("items")
        return [self._parse_order(o, symbol) for o in rows or []]

    async def account(self) -> dict[str, Any]:
        """{"balances": [{"asset", "free", "locked"}]} summed over the spot trading accounts ("trade" and
        "trade_hf"). Funds in the main/funding account can't be traded and aren't counted."""
        rows = await self._call("GET", "/api/v1/accounts", signed=True)
        bal: dict[str, list[Decimal]] = {}
        for a in rows or []:
            if a.get("type") not in ("trade", "trade_hf"):
                continue
            b = bal.setdefault(a.get("currency", ""), [Decimal(0), Decimal(0)])
            b[0] += _dec(a.get("available"))
            b[1] += _dec(a.get("holds"))
        return {"balances": [{"asset": c, "free": format(v[0], "f"), "locked": format(v[1], "f")}
                             for c, v in bal.items()]}

    @staticmethod
    def _fill(t: dict[str, Any]) -> dict[str, Any]:
        return {"symbol": KucoinClient.int_symbol(t.get("symbol", "")), "id": t.get("tradeId") or t.get("id"),
                "orderId": t.get("orderId"), "price": t.get("price"), "qty": t.get("size"),
                "quoteQty": t.get("funds"), "commission": t.get("fee"), "commissionAsset": t.get("feeCurrency"),
                "time": int(t.get("createdAt") or 0), "isBuyer": str(t.get("side", "")).lower() == "buy"}

    async def _fills_page(self, symbol: str, start_ms: int, end_ms: int, limit: int,
                          cursor: Any = None) -> tuple[list[dict[str, Any]], Any]:
        """One page of fills in [start_ms, end_ms] (at most 7 days). Returns (fills, next cursor or None)."""
        params = {"symbol": self.kc_symbol(symbol), "startAt": str(int(start_ms)), "endAt": str(int(end_ms))}
        if await self.is_hf():
            n = min(limit, 100)
            params["limit"] = str(n)
            if cursor:
                params["lastId"] = str(cursor)
            d = await self._call("GET", "/api/v1/hf/fills", params=params, signed=True) or {}
            items = d.get("items") or []
            nxt = d.get("lastId") if len(items) >= n and d.get("lastId") else None
        else:
            page = int(cursor or 1)
            params.update(pageSize=str(min(max(limit, 10), 500)), currentPage=str(page))
            d = await self._call("GET", "/api/v1/fills", params=params, signed=True) or {}
            items = d.get("items") or []
            nxt = page + 1 if page < int(d.get("totalPage") or 1) else None
        return [self._fill(t) for t in items], nxt

    async def my_trades(self, symbol: str, limit: int = 100, start_ms: int | None = None,
                        end_ms: int | None = None) -> list[dict[str, Any]]:
        """Up to `limit` fills, MEXC/Binance-shaped. With no start: the last 7 days."""
        end = int(end_ms) if end_ms is not None else now_ms()
        start = int(start_ms) if start_ms is not None else end - _KC_FILL_WINDOW_MS + 1
        start = max(start, end - _KC_FILL_WINDOW_MS + 1)
        fills, _ = await self._fills_page(symbol, start, end, limit)
        return sorted(fills, key=lambda t: t["time"])[-limit:]

    async def my_trades_since(self, symbol: str, start_ms: int, end_ms: int, page: int = 100,
                              max_pages: int = 20) -> tuple[list[dict[str, Any]], bool]:
        """Every fill in [start_ms, end_ms], oldest first, walking 7-day windows and their pages.
        Returns (fills, complete) — complete is False if max_pages ran out first."""
        out: dict[Any, dict[str, Any]] = {}
        pages = 0
        lo = int(start_ms)
        while lo <= int(end_ms):
            hi = min(int(end_ms), lo + _KC_FILL_WINDOW_MS - 1)
            cursor = None
            while True:
                if pages >= max_pages:
                    return sorted(out.values(), key=lambda t: t["time"]), False
                fills, cursor = await self._fills_page(symbol, lo, hi, page, cursor)
                pages += 1
                for f in fills:
                    out[(f["id"], f["orderId"])] = f
                if not cursor:
                    break
            lo = hi + 1
        return sorted(out.values(), key=lambda t: t["time"]), True


def spot_client(session: aiohttp.ClientSession, creds) -> "MexcClient | KucoinClient":
    """The spot-leg client for config.SPOT_VENUE."""
    return KucoinClient(session, creds) if config.SPOT_VENUE == "kucoin" else MexcClient(session, creds)
