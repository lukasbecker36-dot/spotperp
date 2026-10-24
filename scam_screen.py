"""Scam-candidate screen: flag Aster coin perps set up like insider squeezes.

The pattern (RAVE, SIREN, ARIA, MYX): insiders hold most of the supply, a perp
lists, traders short it for the funding, and a push on thin spot liquidity
forces the shorts to buy back while the insiders sell into it. The screen
scores each coin perp on the published warning signs, plus the one this
system's own data found most predictive (a recent +50% day):

    check                     warning (+1)        danger (+2)
    Aster OI / market cap     > 0.5               > 1.0
    perp / spot 24h volume    > 15x               > 40x (or no spot volume)
    |funding| annualised      > 100%              > 300%
    perp listing age          < 180 days          < 60 days
    market cap                < $100M             < $30M (+1 if unknown)
    circulating / total       < 20%  (+1)
    a +50% day in last 30d    +1

Read-only and advisory: public endpoints only, and nothing here trades.
Sources for the thresholds: HackerNoon "That 300% Funding APR Is Not Free
Money" (OI/mcap, perp/spot, funding, age, mcap), CryptoSlate on RAVE/SIREN/
ARIA (concentration, turnover), Odaily (low initial float). On a replay of
this universe, a +50% day in the prior 30 days carried 2.8-4.4x the base rate
of another one.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field

import aiohttp

import config

log = logging.getLogger("scam_screen")

ASTER_PUBLIC = "https://fapi.asterdex.com/fapi/v1"
MEXC_PUBLIC = "https://api.mexc.com/api/v3"
COINGECKO = "https://api.coingecko.com/api/v3"
DAY_MS = 86_400_000


@dataclass
class Metrics:
    symbol: str
    base: str
    funding_apr: float | None = None      # 1.0 = +100% a year
    oi_usd: float | None = None
    perp_volume_usd: float | None = None
    spot_volume_usd: float | None = None
    market_cap_usd: float | None = None
    float_ratio: float | None = None      # circulating / total supply
    age_days: float | None = None
    max_day_gain_30d: float | None = None  # 0.5 = a +50% day


@dataclass
class Scored:
    symbol: str
    score: int
    reasons: list[str] = field(default_factory=list)
    funding_apr: float | None = None
    market_cap_usd: float | None = None


def canonical_base(base_asset: str) -> str:
    """Ticker CoinGecko knows: '1000PEPE' -> 'PEPE', '1MBABYDOGE' -> 'BABYDOGE'.
    Without this a scaled contract silently gets no market cap."""
    return re.sub(r"^(1000000|1000|1M)(?=[A-Z])", "", base_asset.upper())


def _usd(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    return f"${v / 1e3:.0f}k"


def score(m: Metrics, have_mcap_data: bool) -> Scored:
    """Points and the human-readable reasons behind them. Each check is skipped
    (no points) when its input is missing, except market cap and spot volume:
    an unknown market cap with CoinGecko data loaded means the coin is outside
    the top ~2000, which is itself the warning."""
    pts = 0
    why: list[str] = []
    mcap = m.market_cap_usd or 0.0
    if mcap > 0 and m.oi_usd is not None:
        r = m.oi_usd / mcap
        if r > config.SCAM_OI_MCAP_DANGER:
            pts += 2; why.append(f"OI/mcap {r:.2f}")
        elif r > config.SCAM_OI_MCAP_WARN:
            pts += 1; why.append(f"OI/mcap {r:.2f}")
    if m.perp_volume_usd and have_mcap_data:
        spot = m.spot_volume_usd or 0.0
        r = m.perp_volume_usd / spot if spot > 0 else float("inf")
        label = "no spot volume" if spot <= 0 else f"perp/spot {r:.0f}x"
        if r > config.SCAM_PERP_SPOT_DANGER:
            pts += 2; why.append(label)
        elif r > config.SCAM_PERP_SPOT_WARN:
            pts += 1; why.append(label)
    if m.funding_apr is not None:
        a = abs(m.funding_apr)
        if a > config.SCAM_FUNDING_APR_DANGER:
            pts += 2; why.append(f"funding {m.funding_apr:+.0%} APR")
        elif a > config.SCAM_FUNDING_APR_WARN:
            pts += 1; why.append(f"funding {m.funding_apr:+.0%} APR")
    if m.age_days is not None:
        if m.age_days < config.SCAM_AGE_DANGER_DAYS:
            pts += 2; why.append(f"listed {m.age_days:.0f}d ago")
        elif m.age_days < config.SCAM_AGE_WARN_DAYS:
            pts += 1; why.append(f"listed {m.age_days:.0f}d ago")
    if have_mcap_data:
        if mcap <= 0:
            pts += 1; why.append("no market cap (outside top 2000)")
        elif mcap < config.SCAM_MCAP_DANGER_USD:
            pts += 2; why.append(f"mcap {_usd(mcap)}")
        elif mcap < config.SCAM_MCAP_WARN_USD:
            pts += 1; why.append(f"mcap {_usd(mcap)}")
    if m.float_ratio is not None and m.float_ratio < config.SCAM_FLOAT_WARN:
        pts += 1; why.append(f"float {m.float_ratio:.0%}")
    if m.max_day_gain_30d is not None and m.max_day_gain_30d >= config.SCAM_PUMP_DAY_GAIN:
        gain = f"+{m.max_day_gain_30d:.0%}" if m.max_day_gain_30d < 10 else ">+1000%"
        pts += 1; why.append(f"{gain} day in last 30d")
    return Scored(m.symbol, pts, why, m.funding_apr, m.market_cap_usd)


def plan_alert(
    rows: list[Scored], alerted: dict[str, int], now_ms: int,
    open_symbols: dict[str, int],
) -> tuple[str | None, dict[str, int]]:
    """The digest to send for this sweep (None if nothing new) and the updated
    alerted map. A name alerts when it first qualifies, then stays quiet for
    SCAM_ALERT_REARM_HOURS while it keeps qualifying, so an hourly sweep pings
    once per episode. Keys are 'score:SYM' and 'neg:SYM' so a name can enter
    each list independently. Names that stop qualifying are dropped from the
    map after the re-arm window, which re-arms them."""
    rearm = config.SCAM_ALERT_REARM_HOURS * 3_600_000
    level = config.SCAM_ALERT_SCORE
    neg_apr = config.SCAM_NEG_FUNDING_APR
    new_scored: list[Scored] = []
    new_neg: list[Scored] = []
    keep: dict[str, int] = {}
    for r in rows:
        for key, hit, bucket in (
            (f"score:{r.symbol}", r.score >= level, new_scored),
            (f"neg:{r.symbol}", r.funding_apr is not None and r.funding_apr <= neg_apr,
             new_neg),
        ):
            last = alerted.get(key)
            if hit:
                if last is None or now_ms - last > rearm:
                    bucket.append(r)
                    keep[key] = now_ms
                else:
                    keep[key] = last
            elif last is not None and now_ms - last <= rearm:
                keep[key] = last       # still inside its window: don't re-arm yet
    if not new_scored and not new_neg:
        return None, keep

    def line(r: Scored) -> str:
        pos = open_symbols.get(r.symbol)
        flag = f"  ⚠️ OPEN POSITION #{pos}" if pos is not None else ""
        return f"{r.symbol} [{r.score}] {', '.join(r.reasons)}{flag}"

    out: list[str] = []
    if new_scored:
        new_scored.sort(key=lambda r: (r.symbol not in open_symbols, -r.score))
        out.append(f"🚩 scam-candidate screen: {len(new_scored)} new at score"
                   f" ≥{level}")
        out += [line(r) for r in new_scored[:config.SCAM_ALERT_MAX_LINES]]
        if len(new_scored) > config.SCAM_ALERT_MAX_LINES:
            out.append(f"…and {len(new_scored) - config.SCAM_ALERT_MAX_LINES} more (/scam)")
    if new_neg:
        new_neg.sort(key=lambda r: r.funding_apr or 0)
        if out:
            out.append("")
        out.append(f"🔻 strongly negative funding (≤{neg_apr:.0%} APR — crowded"
                   " shorts, squeeze risk):")
        out += [line(r) for r in new_neg[:config.SCAM_ALERT_MAX_LINES]]
    out.append("/scam for the full board, /scam neg for negative funding")
    return "\n".join(out), keep


class ScamScreen:
    """Fetches the inputs from public endpoints. The fast inputs (funding,
    volume, open interest) refresh every sweep; market caps and daily candles
    move slowly and CoinGecko's free tier rate-limits hard, so those refresh
    every SCAM_SLOW_REFRESH_SECONDS and the last good copy is kept on failure."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._cg: dict[str, dict] = {}
        self._day_gain: dict[str, float] = {}
        self._slow_at = 0.0

    async def _get(self, url: str, params: dict | None = None,
                   headers: dict | None = None, timeout: float = 20.0):
        async with self._session.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 429:
                wait = float(resp.headers.get("Retry-After") or 60)
                raise _RateLimited(wait)
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _coingecko(self) -> dict[str, dict]:
        """Top SCAM_COINGECKO_PAGES x 250 coins by market cap, keyed by upper-case
        ticker; on a ticker collision the larger market cap wins."""
        headers = {"accept": "application/json"}
        if config.COINGECKO_API_KEY:
            headers["x-cg-demo-api-key"] = config.COINGECKO_API_KEY
        out: dict[str, dict] = {}
        for page in range(1, config.SCAM_COINGECKO_PAGES + 1):
            for attempt in range(3):
                try:
                    rows = await self._get(
                        f"{COINGECKO}/coins/markets",
                        {"vs_currency": "usd", "order": "market_cap_desc",
                         "per_page": "250", "page": str(page)},
                        headers,
                    )
                    break
                except _RateLimited as rl:
                    await asyncio.sleep(min(rl.wait, 120) * (attempt + 1))
            else:
                raise RuntimeError(f"coingecko page {page} rate-limited")
            for c in rows:
                k = str(c.get("symbol", "")).upper()
                if k and (k not in out or (c.get("market_cap") or 0)
                          > (out[k].get("market_cap") or 0)):
                    out[k] = c
            await asyncio.sleep(config.SCAM_COINGECKO_PAGE_PAUSE_SECONDS)
        return out

    async def _day_gains(self, onboard: dict[str, int]) -> dict[str, float]:
        """Largest one-day gain (high over the prior day's close) in the last
        30 daily candles, per symbol. The first two days after listing are
        skipped: the listing candle's 'previous close' is a placeholder price,
        which turned APM's debut into a +656,059% day."""
        out: dict[str, float] = {}
        for sym, listed_ms in onboard.items():
            try:
                k = await self._get(f"{ASTER_PUBLIC}/klines",
                                    {"symbol": sym, "interval": "1d", "limit": "31"})
            except Exception:
                continue
            best = 0.0
            for prev, cur in zip(k, k[1:]):
                if int(prev[0]) < listed_ms + 2 * DAY_MS:
                    continue
                pc, hi = float(prev[4]), float(cur[2])
                if pc > 0:
                    best = max(best, hi / pc - 1)
            out[sym] = best
            await asyncio.sleep(0.05)
        return out

    async def sweep(self) -> list[Metrics]:
        info = await self._get(f"{ASTER_PUBLIC}/exchangeInfo")
        now = time.time() * 1000
        perps = {
            s["symbol"]: s for s in info.get("symbols", [])
            if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
            and s.get("underlyingType", "COIN") == "COIN"
            and s["symbol"].endswith("USDT")
        }
        premium, ticker, finfo, mexc = await asyncio.gather(
            self._get(f"{ASTER_PUBLIC}/premiumIndex"),
            self._get(f"{ASTER_PUBLIC}/ticker/24hr"),
            self._get(f"{ASTER_PUBLIC}/fundingInfo"),
            self._get(f"{MEXC_PUBLIC}/ticker/24hr"),
            return_exceptions=True,
        )
        prem = {r["symbol"]: r for r in premium} if isinstance(premium, list) else {}
        tick = {r["symbol"]: r for r in ticker} if isinstance(ticker, list) else {}
        interval = ({r["symbol"]: int(r.get("fundingIntervalHours") or 8) for r in finfo}
                    if isinstance(finfo, list) else {})
        mexc_vol = ({r["symbol"]: float(r.get("quoteVolume") or 0) for r in mexc}
                    if isinstance(mexc, list) else {})

        if time.monotonic() - self._slow_at > config.SCAM_SLOW_REFRESH_SECONDS or not self._cg:
            try:
                self._cg = await self._coingecko()
            except Exception:
                log.exception("coingecko refresh failed (keeping %d coins)", len(self._cg))
            try:
                self._day_gain = await self._day_gains(
                    {sym: int(s.get("onboardDate") or 0) for sym, s in perps.items()}
                )
            except Exception:
                log.exception("daily candle refresh failed")
            self._slow_at = time.monotonic()

        out: list[Metrics] = []
        for sym, s in perps.items():
            p = prem.get(sym) or {}
            mark = float(p.get("markPrice") or 0)
            rate = p.get("lastFundingRate")
            apr = (float(rate) * (24 / interval.get(sym, 8)) * 365
                   if rate not in (None, "") else None)
            oi = None
            try:
                o = await self._get(f"{ASTER_PUBLIC}/openInterest", {"symbol": sym})
                oi = float(o.get("openInterest") or 0) * mark
            except Exception:
                pass
            await asyncio.sleep(0.05)
            base = canonical_base(s.get("baseAsset", sym[:-4]))
            cg = self._cg.get(base) or {}
            circ = cg.get("circulating_supply") or 0
            total = cg.get("total_supply") or cg.get("max_supply") or 0
            out.append(Metrics(
                symbol=sym, base=base, funding_apr=apr, oi_usd=oi,
                perp_volume_usd=float((tick.get(sym) or {}).get("quoteVolume") or 0),
                spot_volume_usd=(cg.get("total_volume") or mexc_vol.get(f"{base}USDT")),
                market_cap_usd=cg.get("market_cap") or None,
                float_ratio=(circ / total) if circ and total else None,
                age_days=((now - s["onboardDate"]) / DAY_MS) if s.get("onboardDate") else None,
                max_day_gain_30d=self._day_gain.get(sym),
            ))
        return out

    @property
    def have_mcap_data(self) -> bool:
        return bool(self._cg)


class _RateLimited(Exception):
    def __init__(self, wait: float) -> None:
        super().__init__(f"rate limited, retry after {wait}s")
        self.wait = wait


def snapshot(rows: list[Scored], alerted: dict[str, int], now_ms: int) -> dict:
    """What /scam reads, and the alert state that survives a restart."""
    return {
        "ts_ms": now_ms,
        "rows": [asdict(r) for r in sorted(rows, key=lambda r: -r.score)],
        "alerted": alerted,
    }
