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
    # Mean absolute change between consecutive samples in the window. A basis
    # that jumps wildly every tick can't be worked by a resting order: the fill
    # price is a lottery (BULLA printed -0.3/+158/-160/+27 inside a minute).
    entry_bps_jitter: float = 0.0
    # 24h mean of the entry basis (filled by DailyBasis). Tells a DISLOCATION
    # (entry_bps >> this -> likely to revert) apart from a pair that simply
    # always trades rich (entry_bps ~ this -> no convergence to capture).
    entry_bps_avg_24h: float = 0.0
    hours_24h: float = 0.0    # hours of history behind that mean
    # Robust 24h range of the HOURLY MEAN basis (p10/p90). A pair that swings
    # wide and then reaches flat/negative is round-trippable; one whose quote
    # only flickers has a tight range because the noise averages out per hour.
    basis_p10_24h: float = 0.0
    basis_p90_24h: float = 0.0
    # Can this actually be TRADED? Depth is resting size; these are flow.
    perp_volume_24h: float = 0.0    # Aster perp 24h quote volume (USDT)
    perp_trades_24h: float = 0.0    # ...and how many trades made it up
    hours_tradeable_24h: float = 0.0  # hours the basis sat at/above the floor
    # Aster's OWN index price vs the MEXC mid, in bps. Aster builds its index
    # from real spot venues, so it should agree with MEXC to within a spread.
    # A large gap means the two symbols are not the same asset, or the contract
    # multiplier is wrong — in which case the basis is arithmetic on two
    # unrelated prices. None when premiumIndex has not supplied one.
    index_divergence_bps: float | None = None


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
        # Mean absolute change BETWEEN CONSECUTIVE samples, not a standard
        # deviation: a basis drifting smoothly +10 -> +80 across the window is
        # perfectly tradeable yet has a big std, while one bouncing +150/-150
        # each sample is not. Only successive change separates them.
        if n >= 2:
            row.entry_bps_jitter = sum(
                abs(dq[i][1] - dq[i - 1][1]) for i in range(1, n)
            ) / (n - 1)
        return row


def compute_row(
    pair: PairMap,
    aster: BookTicker,
    mexc: BookTicker,
    funding_rate: Decimal | None,
    *,
    now_ms: int,
    funding_interval_hours: int = 8,
    index_price: Decimal | None = None,
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
    index_divergence_bps = None
    if index_price is not None and index_price > 0:
        mexc_mid = (mexc.ask + mexc.bid) / 2
        if mexc_mid > 0:
            index_divergence_bps = float(
                (index_price / mult - mexc_mid) / mexc_mid * BPS
            )
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
        index_divergence_bps=index_divergence_bps,
    )


def rank_rows(rows: list[ScreenerRow]) -> list[ScreenerRow]:
    # Rank by the windowed-average net edge so a persistent opportunity outranks
    # a one-tick spike. Depth eligibility stays on the current top-of-book.
    eligible = [
        r
        for r in rows
        if r.max_notional_usd >= config.SCREEN_MIN_DEPTH_USD
        and not _bad_index(r)
    ]
    eligible.sort(key=lambda r: r.net_edge_bps_avg, reverse=True)
    return eligible[: config.SCREENER_TOP_N]


def _bad_index(row: ScreenerRow) -> bool:
    """True when Aster's own index disagrees with MEXC spot by so much that the
    two symbols cannot be the same asset at the same scale.

    This is the one error the other gates cannot see. depth, spread and jitter
    all test the RELATIONSHIP between the two quotes, so a pair that is
    mis-mapped or on the wrong contract multiplier passes every one of them: a
    consistently wrong price is still a tight, deep, steady quote. ONEUSDT
    printed a +2674bps entry that had sat above +1500 all day, on a $6 book
    with a -101bps carry, and looked perfectly workable.

    Aster builds its index from real spot venues, so index vs MEXC mid is an
    independent check that the two sides are the same thing. Fails OPEN when
    premiumIndex has not supplied an index.
    """
    d = row.index_divergence_bps
    return d is not None and abs(d) > config.SCREEN_MAX_INDEX_DIVERGENCE_BPS


def _too_jittery(row: ScreenerRow) -> bool:
    """True when the basis flickers too hard to work a resting order against.

    Needs at least 3 samples to mean anything; with fewer we cannot judge, so
    fail OPEN rather than hiding a name for lack of data.
    """
    return (
        row.samples >= 3
        and row.entry_bps_jitter > config.SCREEN_MAX_BASIS_JITTER_BPS
    )


def quote_reject_reason(row: ScreenerRow) -> str | None:
    """Why this row's quoted basis should not be believed or worked, or None.

    /screen fill earns its trust by gating hard on flow and steadiness. Any
    other screen that hangs a live basis off a different ranking — /funding
    ranks by carry — inherits the same failure modes without those gates, and
    the errors are spectacular: ARGUSUSDT showing a 250bps entry on a $4 book,
    AINUSDT -114.6 on $9. Those are not opportunities; they are one stale or
    one-sided quote.

    Deliberately NOT a magnitude cap on the basis. The richest names are the
    edge (哈基米 quotes +123 and is real), so capping the number throws away
    exactly what the screen is for. Gate on the things that make a quote
    UNREAL instead: an untransactable gap between the books, a book too thin
    to have a price at all, a basis that flickers, and no flow to fill against.
    """
    if _bad_index(row):
        return "index"
    if row.max_notional_usd < config.SCREEN_MIN_DEPTH_USD:
        return "depth"
    # Entry (ask/ask) minus close (bid/bid) is what crossing both books costs
    # now. 170bps of it means the quotes are nowhere near each other.
    if row.spread_cost_bps > config.SCREEN_MAX_SPREAD_COST_BPS:
        return "spread"
    if _too_jittery(row):
        return "jitter"
    # perp_volume_24h is 0 when the ticker sweep has not landed yet; fail OPEN
    # there rather than blanking the whole screen on a slow start.
    if row.perp_volume_24h and row.perp_volume_24h < config.SCREEN_FILL_MIN_VOLUME_USD:
        return "volume"
    return None


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
        if r.max_notional_usd >= config.SCREEN_MIN_DEPTH_USD
        and r.hours_24h >= config.SCREEN_DIFF_MIN_HOURS
    ]
    eligible.sort(
        key=lambda r: r.entry_bps_avg - r.entry_bps_avg_24h, reverse=True
    )
    return eligible[: config.SCREENER_TOP_N]


def rank_rows_by_swing(rows: list[ScreenerRow]) -> list[ScreenerRow]:
    """Rank by the round trip available from here: entry basis now minus the
    pair's own 24h LOW (p10 of hourly means).

    This is the STONK profile — a basis that goes wide, pays funding while you
    hold, then returns to flat/negative so the position can actually be closed
    at a profit. Two filters make it a round trip rather than wishful thinking:

      - the 24h low must actually REACH SCREEN_SWING_EXIT_BPS. A pair pinned at
        +80..+120 has a 40bps range but never becomes closeable, so it is carry,
        not a swing.
      - funding must be >= SCREEN_SWING_MIN_FUNDING_BPS, so waiting is paid for.

    Because the range is built from hourly MEANS, a pair whose quote merely
    flickers intra-hour scores near zero — the noise averages out.
    """
    eligible = [
        r
        for r in rows
        if r.max_notional_usd >= config.SCREEN_MIN_DEPTH_USD
        and r.hours_24h >= config.SCREEN_DIFF_MIN_HOURS
        and r.basis_p10_24h <= config.SCREEN_SWING_EXIT_BPS
        and r.funding_8h_bps >= config.SCREEN_SWING_MIN_FUNDING_BPS
        and not _too_jittery(r)
        and not _bad_index(r)
    ]
    eligible.sort(key=lambda r: r.entry_bps_avg - r.basis_p10_24h, reverse=True)
    return eligible[: config.SCREENER_TOP_N]


def rank_rows_by_fillability(rows: list[ScreenerRow]) -> list[ScreenerRow]:
    """Rank by how many CHANCES a name gives you to get filled.

    /screen ranks by the size of the edge, and depth only proves the book is
    not empty. But an entry rests as a maker: it fills when a taker lifts it.
    A wide basis on a symbol nobody trades never fills however good it looks —
    which is why the richest /screen rows can be the hardest to enter.

    So gate on FLOW, not size: real 24h perp volume, and a basis that sat at a
    workable level for hours rather than spiking for one tick. Also require the
    basis to clear the entry floor RIGHT NOW, or the screen lists names that
    fill well but cannot be entered today. Ranked by dwell time, because that is
    literally the number of chances to be lifted.
    """
    eligible = [
        r
        for r in rows
        if r.max_notional_usd >= config.SCREEN_MIN_DEPTH_USD
        and r.perp_volume_24h >= config.SCREEN_FILL_MIN_VOLUME_USD
        and r.hours_tradeable_24h >= config.SCREEN_FILL_MIN_HOURS
        # Funding is the carry you collect while working the round trip. A
        # negative rate means the short PAYS to wait, so time stops being on
        # your side — the opposite of what this screen is selecting for.
        and r.funding_8h_bps >= config.SCREEN_FILL_MIN_FUNDING_BPS
        and not _too_jittery(r)
        and not _bad_index(r)
        # Enterable RIGHT NOW. Dwell says a name is reliably workable, but a
        # row you cannot act on today is a watchlist entry, not a candidate.
        and r.entry_bps_avg >= float(config.ENTRY_MIN_EDGE_FLOOR_BPS)
        # Worth doing, not merely possible: the round trip down to the pair's
        # own 24h low must clear the cost floor. Without this the screen ranks
        # a name you can fill easily but whose basis never comes back far
        # enough to close for a profit ABOVE one that does.
        and (r.entry_bps_avg - r.basis_p10_24h)
        - float(config.ENTRY_MIN_EDGE_FLOOR_BPS)
        >= config.SCREEN_FILL_MIN_NET_SWING_BPS
    ]
    # Rank by the composite: risk-adjusted edge, discounted by how reliably it
    # will fill. Neither half works alone — fill frequency put a flickering book
    # with 4,104 chances above a 2x better, far steadier edge, and raw edge put
    # names that never fill on top. See fill_score.
    eligible.sort(key=fill_score, reverse=True)
    return eligible[: config.SCREENER_TOP_N]


def _fill_chances(row: ScreenerRow) -> float:
    """Expected number of taker events while the basis is workable."""
    return row.hours_tradeable_24h * (row.perp_trades_24h / 24.0)


def fill_score(row: ScreenerRow) -> float:
    """One number for "how good is this trade", in bps.

        net x min(1, chances / SCREEN_FILL_TARGET_CHANCES)

    net      what the round trip is worth after round-trip costs. Those costs
             now carry the measured slippage (SLIPPAGE_BUFFER_BPS), so the
             execution haircut is charged once, in the floor, for every screen.
    xfactor  saturating fill probability. Below the target a resting order may
             never be lifted; above it, extra flow adds nothing to a single
             round trip.

    No jitter term. This used to subtract the full 5m jitter, on the theory
    that a resting order fills on the bad side of a moving basis — inferred
    from one position (STONK #186: quoted +121 across its clips, locked
    +58.8). Measured on 612 entry and 481 exit clips it does not hold:

      * entry slippage is flat in jitter (5.0 / 7.5 / 7.4 / 1.4 bps across the
        quartiles) and scales with CLIP SIZE instead;
      * exit slippage inverts — the jitteriest quartile locked 6.3bps BETTER
        than quoted, because a resting exit catches more dips on a basis that
        moves;
      * holding net edge fixed, jitter carries no information about what the
        basis does next (med hold 17.1 -> 13.3 across the jitter range in the
        10-20 band, flat in the 20-40 band).

    So the haircut under-charged steady names and docked jittery ones up to
    25bps for a cost they were not paying. _too_jittery still drops the
    extremes: that gate is about whether a position can be WORKED, which is a
    different claim and untested either way.

    Deliberately NOT multiplied by depth. Top-of-book depth understates exactly
    the names worth trading here (STONK shows ~$8 at the touch yet fills $42-99
    clips), so weighting by it would re-bury them — the very bug the screen
    depth floor was lowered to fix. Size is a separate question: read $clip.
    """
    net = (
        row.entry_bps_avg
        - row.basis_p10_24h
        - float(config.ENTRY_MIN_EDGE_FLOOR_BPS)
    )
    factor = min(
        1.0, _fill_chances(row) / max(config.SCREEN_FILL_TARGET_CHANCES, 1.0)
    )
    return net * factor


def carry_score(
    row: ScreenerRow, avg_8h_bps: float, current_8h_bps: float
) -> float:
    """One number for a /funding row, in bps: what entering NOW and holding for
    FUNDING_SCORE_HOLD_HOURS is worth.

        (entry - lo24 - floor) + carry x hold/8

    The two halves of a carry trade are in different units and the board made
    you convert between them by eye. The basis is a ONE-OFF — you capture
    (entry - exit) once, and lo24 is where the exit realistically fills — while
    funding is a STREAM. They only become comparable over a stated horizon.

    carry is min(24h average, latest settlement), the conservative read of the
    stream. Taking the average alone lets a collapsed carry keep flattering a
    row (CATEUSDT averaged 61.7bps/8h while its latest settle was 22.2);
    taking the latest alone lets one spiky settlement do the same. The lower of
    the two is wrong only when the carry is genuinely recovering, which costs
    you a missed entry rather than a bad one.

    No jitter term, for the reasons in fill_score: measured against real fills
    it does not predict slippage, and the execution cost it stood for is now
    charged in the floor via SLIPPAGE_BUFFER_BPS.

    As with fill_score, not weighted by depth — that is a sizing question, read
    depth$ — and scaled by a saturating fill factor, since an edge on a book
    with no takers is not an edge.
    """
    floor = float(config.ENTRY_MIN_EDGE_FLOOR_BPS)
    one_off = row.entry_bps_avg - row.basis_p10_24h - floor
    carry = min(avg_8h_bps, current_8h_bps)
    stream = carry * config.FUNDING_SCORE_HOLD_HOURS / 8.0
    factor = min(
        1.0,
        row.perp_trades_24h / max(config.SCREEN_FILL_TARGET_CHANCES, 1.0),
    ) if row.perp_trades_24h else 1.0
    return (one_off + stream) * factor


def write_snapshot(
    rows: list[ScreenerRow],
    diff_rows: list[ScreenerRow] | None = None,
    swing_rows: list[ScreenerRow] | None = None,
    fill_rows: list[ScreenerRow] | None = None,
) -> None:
    config.SCREENER_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts_ms": int(time.time() * 1000),
        "rows": [asdict(r) for r in rows],
        "diff_rows": [asdict(r) for r in (diff_rows or [])],
        "swing_rows": [asdict(r) for r in (swing_rows or [])],
        "fill_rows": [asdict(r) for r in (fill_rows or [])],
    }
    tmp = config.SCREENER_SNAPSHOT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(config.SCREENER_SNAPSHOT_FILE)


def read_snapshot() -> dict:
    try:
        return json.loads(config.SCREENER_SNAPSHOT_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ts_ms": 0, "rows": []}


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile of a small unsorted list."""
    s = sorted(values)
    if not s:
        return 0.0
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


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

    def add(
        self, symbol: str, ts_ms: int, entry_bps: float,
        close_bps: float | None = None,
    ) -> None:
        """Bucket one sample. close_bps is optional so a caller that only has
        the entry basis still works; those hours simply have no close range.
        Tracked separately because the two are NOT a fixed offset apart — the
        gap is the live spread of both books, which moves on its own."""
        hour = int(ts_ms) // 3_600_000
        b = self._buckets[symbol]
        slot = b.get(hour)
        c = float(close_bps) if close_bps is not None else 0.0
        n_c = 1 if close_bps is not None else 0
        if slot is None:
            b[hour] = [float(entry_bps), 1, c, n_c]
        else:
            slot[0] += float(entry_bps)
            slot[1] += 1
            slot[2] += c
            slot[3] += n_c
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

    def hourly_means(self, symbol: str, close: bool = False) -> list[float]:
        """Mean entry basis for each hour held. Averaging within the hour is
        what makes the range usable: a pair whose quote merely FLICKERS (BULLA
        printed +-160bps inside a minute) collapses to near-identical hourly
        means, while a pair that genuinely swings over a day keeps a wide
        spread. So a range built from these is a real oscillation, not noise."""
        b = self._buckets.get(symbol)
        if not b:
            return []
        if close:
            return [v[2] / v[3] for v in b.values() if v[3] > 0]
        return [v[0] / v[1] for v in b.values() if v[1] > 0]

    def hours_above(self, symbol: str, threshold: float) -> float:
        """How many of the last 24 hours had a mean basis at or above
        `threshold` — the DWELL time at a workable level.

        A resting maker entry needs TIME to be lifted. A basis that spikes for
        one tick can't be worked; one sitting wide for 18 of 24 hours gives
        repeated chances to be filled (the STONK pattern). This is the
        difference between an opportunity and a screenshot.
        """
        return float(sum(1 for m in self.hourly_means(symbol) if m >= threshold))

    def percentiles(
        self, symbol: str, lo: float = 10.0, hi: float = 90.0,
        close: bool = False,
    ) -> tuple[float | None, float | None]:
        """(lo, hi) percentile of the hourly means — a robust 24h range. Uses
        percentiles rather than min/max so one odd hour can't define it."""
        means = self.hourly_means(symbol, close=close)
        if not means:
            return None, None
        return _percentile(means, lo), _percentile(means, hi)

    def annotate(self, row: "ScreenerRow | None") -> "ScreenerRow | None":
        if row is None:
            return None
        mean, hours = self.stats(row.symbol)
        row.entry_bps_avg_24h = row.entry_bps if mean is None else mean
        row.hours_24h = hours
        lo, hi = self.percentiles(row.symbol)
        row.basis_p10_24h = row.entry_bps if lo is None else lo
        row.basis_p90_24h = row.entry_bps if hi is None else hi
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
                close_i = idx.get("close_bps")
                for row in reader:
                    try:
                        ts = int(row[ts_i])
                        if ts < cutoff:
                            continue
                        close = (
                            float(row[close_i]) if close_i is not None else None
                        )
                        daily.add(row[sym_i], ts, float(row[entry_i]), close)
                        seeded += 1
                    except (IndexError, ValueError):
                        continue   # malformed / partially-written line
        except OSError:
            log.warning("could not seed 24h basis from %s", path, exc_info=True)
    return seeded
