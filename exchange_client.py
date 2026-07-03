"""Async REST wrappers for Aster DEX perps (V3) and MEXC spot (V3).

Both APIs are Binance-compatible in shape. All prices/quantities are Decimal.
Order placement raises ExchangeError on a definitive rejection; ambiguous
outcomes (timeouts, 5xx) raise AmbiguousOrderError so callers can reconcile.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

import aiohttp

import config
from auth import AsterCredentials, MexcCredentials, aster_sign, mexc_sign, now_ms

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
                        and payload.get("code") not in (0, 200, "0", "200", None):
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

    def _signed_query(self, params: dict[str, str]) -> str:
        if self._creds is None:
            raise ExchangeError(self.VENUE, "no credentials configured")
        signed = dict(params)
        signed["timestamp"] = str(now_ms())
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

    async def my_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        query = self._signed_query({"symbol": symbol, "limit": str(limit)})
        return await self._request(
            "GET",
            f"/api/v3/myTrades?{query}",
            headers=self._auth_headers(),
            venue=self.VENUE,
        )
