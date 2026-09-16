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

import csv
import gzip
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from decimal import Decimal

import config
from exchange_client import BookTicker

log = logging.getLogger(__name__)

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
    entry_bps: float          # executable basis at entry, before costs (live)
    close_bps: float          # basis closeable right now (passive exit)
    spread_cost_bps: float    # entry_bps - close_bps: both spreads crossed
    fees_bps: float           # entry + passive-exit fees, both legs
    funding_8h_bps: float     # positive = short perp receives funding
    net_edge_bps: float       # entry - exit target - fees - slippage buffer (live)
    max_notional_usd: float   # top-of-book depth cap, min across venues/sides
    aster_ask: str
    mexc_ask: str
    ts_ms: int
    # Time-windowed means (filled by RollingBasis): a persistent edge has
    # entry_bps_avg ~ entry_bps; a one-tick blip has avg well below the live spike.
    entry_bps_avg: float = 0.0
    net_edge_bps_avg: float = 0.0
    samples: int = 0          # samples in the window
    window_s: float = 0.0     # span covered by those samples (seconds)
    # 24h mean of the entry basis (filled by DailyBasis). Tells a DISLOCATION
    # (entry_bps >> this -> likely to revert) apart from a pair that simply
    # always trades rich (entry_bps ~ this -> no convergence to capture).
    entry_bps_avg_24h: float = 0.0
    hours_24h: float = 0.0    # hours of history behind that mean


class RollingBasis:
    """Per-symbol time-windowed history of (entry, net) basis so /screen can
    report a mean over the last window_s rather than a single tick. Owned by
    the engine; sampled once per slow scan."""

    def __init__(self, window_s: float):
        self._window_ms = int(window_s * 1000)
        self._hist: dict[str, deque] = defaultdict(deque)

    def add(self, symbol: str, ts_ms: int, entry_bps: float, net_edge_bps: float):
        dq = self._hist[symbol]
        dq.append((ts_ms, entry_bps, net_edge_bps))
        cutoff = ts_ms - self._window_ms
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def annotate(self, row: "ScreenerRow | None") -> "ScreenerRow | None":
        """Fold the windowed means into a freshly computed row (after add).
        No-op on None so a skipped/stale row can't crash the caller."""
        if row is None:
            return None
        dq = self._hist.get(row.symbol)
        if not dq:
            row.entry_bps_avg = row.entry_bps
            row.net_edge_bps_avg = row.net_edge_bps
            row.samples = 0
            row.window_s = 0.0
            return row
        n = len(dq)
        row.entry_bps_avg = sum(x[1] for x in dq) / n
        row.net_edge_bps_avg = sum(x[2] for x in dq) / n
        row.samples = n
        row.window_s = (dq[-1][0] - dq[0][0]) / 1000.0
        return row


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
    # Rank by the windowed-average net edge so a persistent opportunity outranks
    # a one-tick spike. Depth eligibility stays on the current top-of-book.
    eligible = [
        r
        for r in rows
        if r.max_notional_usd >= float(config.MIN_DEPTH_NOTIONAL_USD)
    ]
    eligible.sort(key=lambda r: r.net_edge_bps_avg, reverse=True)
    return eligible[: config.SCREENER_TOP_N]


def rank_rows_by_dislocation(rows: list[ScreenerRow]) -> list[ScreenerRow]:
    """Rank by how far the 5m entry basis sits ABOVE the pair's own 24h mean.

    rank_rows ranks by net edge, so a pair whose basis is wildly dislocated but
    whose absolute edge is mediocre never reaches the snapshot. This is the
    reversion view: a +5 basis on a pair that normally sits at -50 is a 55bps
    gap, which a premium trade captures IF it reverts to its norm.

    Pairs without enough 24h history are excluded — a gap measured against a
    few minutes of data is noise, not a dislocation.
    """
    eligible = [
        r
        for r in rows
        if r.max_notional_usd >= float(config.MIN_DEPTH_NOTIONAL_USD)
        and r.hours_24h >= config.SCREEN_DIFF_MIN_HOURS
    ]
    eligible.sort(
        key=lambda r: r.entry_bps_avg - r.entry_bps_avg_24h, reverse=True
    )
    return eligible[: config.SCREENER_TOP_N]


def write_snapshot(
    rows: list[ScreenerRow], diff_rows: list[ScreenerRow] | None = None
) -> None:
    config.SCREENER_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts_ms": int(time.time() * 1000),
        "rows": [asdict(r) for r in rows],
        "diff_rows": [asdict(r) for r in (diff_rows or [])],
    }
    tmp = config.SCREENER_SNAPSHOT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(config.SCREENER_SNAPSHOT_FILE)


def read_snapshot() -> dict:
    try:
        return json.loads(config.SCREENER_SNAPSHOT_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ts_ms": 0, "rows": []}


class DailyBasis:
    """24h rolling mean of the entry basis, kept in hourly buckets.

    Storing every sample would be ~2M tuples across the universe (one per slow
    scan per symbol); bucketing to (sum, count) per hour is 24 numbers per
    symbol instead. Purpose: tell an ELEVATED basis apart from a pair's normal
    level. A pair that always trades +50bps offers no convergence to capture —
    only a basis well above its own 24h mean is a dislocation likely to revert.
    """

    def __init__(self, hours: int = 24):
        self._hours = hours
        # symbol -> {hour_epoch: [sum_bps, count]}
        self._buckets: dict[str, dict[int, list]] = defaultdict(dict)

    def add(self, symbol: str, ts_ms: int, entry_bps: float) -> None:
        hour = int(ts_ms) // 3_600_000
        b = self._buckets[symbol]
        slot = b.get(hour)
        if slot is None:
            b[hour] = [float(entry_bps), 1]
        else:
            slot[0] += float(entry_bps)
            slot[1] += 1
        # Prune relative to the NEWEST hour held, not the one just added: the
        # log seed replays historical rows, so an out-of-order add must never
        # widen the window past `hours`.
        cutoff = max(b) - self._hours + 1
        for stale in [h for h in b if h < cutoff]:
            del b[stale]

    def stats(self, symbol: str) -> tuple[float | None, float]:
        """(mean entry bps over the window, hours of history behind it)."""
        b = self._buckets.get(symbol)
        if not b:
            return None, 0.0
        n = sum(v[1] for v in b.values())
        if n <= 0:
            return None, 0.0
        return sum(v[0] for v in b.values()) / n, float(len(b))

    def annotate(self, row: "ScreenerRow | None") -> "ScreenerRow | None":
        if row is None:
            return None
        mean, hours = self.stats(row.symbol)
        row.entry_bps_avg_24h = row.entry_bps if mean is None else mean
        row.hours_24h = hours
        return row


def seed_daily_from_logs(daily: DailyBasis, now_ms: int, hours: int = 24) -> int:
    """Warm the 24h window from the engine's own basis logs.

    The engine restarts on every /update; without this the '24h average' would
    be a few minutes of data and useless exactly when it's consulted. Reads only
    the day files that can overlap the window (gzipped ones included) and only
    rows inside it. Returns the number of samples seeded.
    """
    cutoff = now_ms - hours * 3_600_000
    paths = []
    for day_offset in (1, 0):          # yesterday then today (chronological)
        day = time.strftime(
            "%Y%m%d", time.gmtime((now_ms - day_offset * 86_400_000) / 1000)
        )
        for suffix in (".csv", ".csv.gz"):
            path = config.OUTPUT_DIR / f"basis_log_{day}{suffix}"
            if path.exists():
                paths.append(path)
    seeded = 0
    for path in paths:
        try:
            opener = gzip.open if path.name.endswith(".gz") else open
            with opener(path, "rt", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    continue
                idx = {name: i for i, name in enumerate(header)}
                try:
                    ts_i, sym_i, entry_i = (
                        idx["ts_ms"], idx["symbol"], idx["entry_bps"],
                    )
                except KeyError:
                    continue
                for row in reader:
                    try:
                        ts = int(row[ts_i])
                        if ts < cutoff:
                            continue
                        daily.add(row[sym_i], ts, float(row[entry_i]))
                        seeded += 1
                    except (IndexError, ValueError):
                        continue   # malformed / partially-written line
        except OSError:
            log.warning("could not seed 24h basis from %s", path, exc_info=True)
    return seeded
