"""Opportunity screener: executable basis net of fees, spreads and funding.

All prices come from the touch (best bid/offer), so both bid-offer spreads are
priced in by construction:

- Entry (premium trade): short Aster perp at the ask (maker join) and buy MEXC
  spot at the ask (taker) -> entry_bps = (aster_ask - mexc_ask) / mexc_ask.
- Exit (passive): buy back perp at the bid (maker join), sell spot at the bid
  (taker) -> close_bps = (aster_bid - mexc_bid) / mexc_bid.

Net edge assumes the position is exited when the closeable basis reaches the
passive exit target, minus fees on every leg and a slippage haircut.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from decimal import Decimal

import config
from exchange_client import BookTicker

BPS = Decimal("10000")

# Aster symbols whose MEXC spot equivalent differs. Maps the Aster perp symbol
# to (mexc_symbol, base_qty_multiplier): 1 Aster contract unit of "1000PEPE"
# equals 1000 PEPE on MEXC.
ASTER_TO_MEXC_ALIASES: dict[str, tuple[str, Decimal]] = {
    # "1000PEPEUSDT": ("PEPEUSDT", Decimal(1000)),
}


@dataclass(frozen=True)
class PairMap:
    aster_symbol: str
    mexc_symbol: str
    qty_multiplier: Decimal  # mexc base qty = aster qty * multiplier


def build_pair_maps(
    aster_symbols: set[str], mexc_symbols: set[str]
) -> dict[str, PairMap]:
    """Canonical symbol -> mapping between venues (USDT pairs only)."""
    out: dict[str, PairMap] = {}
    for sym in sorted(aster_symbols):
        if not sym.endswith("USDT"):
            continue
        if sym in ASTER_TO_MEXC_ALIASES:
            mexc_sym, mult = ASTER_TO_MEXC_ALIASES[sym]
        else:
            mexc_sym, mult = sym, Decimal(1)
        if mexc_sym in mexc_symbols:
            out[sym] = PairMap(sym, mexc_sym, mult)
    return out


@dataclass
class ScreenerRow:
    symbol: str
    entry_bps: float          # executable basis at entry, before costs
    close_bps: float          # basis closeable right now (passive exit)
    spread_cost_bps: float    # entry_bps - close_bps: both spreads crossed
    fees_bps: float           # entry + passive-exit fees, both legs
    funding_8h_bps: float     # positive = short perp receives funding
    net_edge_bps: float       # entry - exit target - fees - slippage buffer
    max_notional_usd: float   # top-of-book depth cap, min across venues/sides
    aster_ask: str
    mexc_ask: str
    ts_ms: int


def compute_row(
    pair: PairMap,
    aster: BookTicker,
    mexc: BookTicker,
    funding_rate: Decimal | None,
    *,
    now_ms: int,
    funding_interval_hours: int = 8,
) -> ScreenerRow | None:
    stale_ms = config.QUOTE_STALE_SECONDS * 1000
    if now_ms - aster.ts_ms > stale_ms or now_ms - mexc.ts_ms > stale_ms:
        return None
    if mexc.ask <= 0 or mexc.bid <= 0 or aster.ask <= 0 or aster.bid <= 0:
        return None

    # Aster prices are per contract-unit; normalise to MEXC base units.
    mult = pair.qty_multiplier
    aster_ask = aster.ask / mult
    aster_bid = aster.bid / mult

    entry_bps = (aster_ask - mexc.ask) / mexc.ask * BPS
    close_bps = (aster_bid - mexc.bid) / mexc.bid * BPS
    fees_bps = (config.ENTRY_FEE + config.EXIT_FEE_PASSIVE) * BPS
    net_edge_bps = (
        entry_bps
        - config.EXIT_BASIS_BPS
        - fees_bps
        - config.SLIPPAGE_BUFFER_BPS
    )

    depth_candidates = [
        aster.ask_qty * aster.ask,           # perp short side
        aster.bid_qty * aster.bid,           # perp buy-back side
        mexc.ask_qty * mexc.ask,             # spot buy side
        mexc.bid_qty * mexc.bid,             # spot sell side
    ]
    max_notional = min(depth_candidates)

    return ScreenerRow(
        symbol=pair.aster_symbol,
        entry_bps=float(entry_bps),
        close_bps=float(close_bps),
        spread_cost_bps=float(entry_bps - close_bps),
        fees_bps=float(fees_bps),
        funding_8h_bps=float(
            (funding_rate or Decimal(0)) * BPS
            * Decimal(8) / Decimal(funding_interval_hours)
        ),
        net_edge_bps=float(net_edge_bps),
        max_notional_usd=float(max_notional),
        aster_ask=str(aster_ask),
        mexc_ask=str(mexc.ask),
        ts_ms=now_ms,
    )


def rank_rows(rows: list[ScreenerRow]) -> list[ScreenerRow]:
    eligible = [
        r
        for r in rows
        if r.max_notional_usd >= float(config.MIN_DEPTH_NOTIONAL_USD)
    ]
    eligible.sort(key=lambda r: r.net_edge_bps, reverse=True)
    return eligible[: config.SCREENER_TOP_N]


def write_snapshot(rows: list[ScreenerRow]) -> None:
    config.SCREENER_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts_ms": int(time.time() * 1000), "rows": [asdict(r) for r in rows]}
    tmp = config.SCREENER_SNAPSHOT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(config.SCREENER_SNAPSHOT_FILE)


def read_snapshot() -> dict:
    try:
        return json.loads(config.SCREENER_SNAPSHOT_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ts_ms": 0, "rows": []}
