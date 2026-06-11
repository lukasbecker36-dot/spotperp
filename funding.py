"""Funding-rate normalisation for Aster perps.

Aster funds symbols on different intervals (1h, 4h, 8h ...). The premiumIndex
``lastFundingRate`` is the rate charged *per that symbol's own interval*, so it
cannot be compared across symbols without normalising. We derive the interval
from the spacing of the funding-rate history and express everything as an
8h-equivalent so the screener can rank like-for-like.

Two metrics:

- ``current_8h_bps`` — the latest instantaneous rate projected to 8h. Reacts
  immediately, but a single print can spike.
- ``avg_24h_8h_bps`` — the realised funding actually paid over the last 24h,
  re-expressed per 8h (sum of the 24h window / 3). Robust to spikes; this is
  what the carry screener ranks on.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

BPS = Decimal("10000")

# Plausible Aster funding intervals, in hours; the derived gap snaps to one.
_CANDIDATE_INTERVALS = (1, 2, 3, 4, 6, 8, 12, 24)
DEFAULT_INTERVAL_HOURS = 8


@dataclass(frozen=True)
class FundingStat:
    symbol: str
    interval_hours: int
    current_8h_bps: float       # latest rate, projected to an 8h window
    avg_24h_8h_bps: float       # realised 24h funding, re-expressed per 8h
    realized_24h_bps: float     # raw sum of funding charged over last 24h
    samples_24h: int            # number of funding prints inside the window


def derive_interval_hours(funding_times_ms: list[int]) -> int:
    """Median gap between consecutive funding timestamps, snapped to a
    plausible interval. Falls back to 8h when history is too thin."""
    times = sorted(t for t in funding_times_ms if t > 0)
    if len(times) < 2:
        return DEFAULT_INTERVAL_HOURS
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not gaps:
        return DEFAULT_INTERVAL_HOURS
    gaps.sort()
    median_ms = gaps[len(gaps) // 2]
    median_hours = median_ms / 3_600_000
    return min(_CANDIDATE_INTERVALS, key=lambda c: abs(c - median_hours))


def summarize(
    symbol: str,
    history: list[tuple[int, Decimal]],
    current_rate: Decimal | None,
    *,
    now_ms: int,
) -> FundingStat:
    """Build a FundingStat from funding history.

    ``history`` is a list of ``(funding_time_ms, funding_rate)`` where the rate
    is the per-interval fraction (e.g. 0.000823 for 0.0823%). ``current_rate``
    is the live premiumIndex rate; falls back to the latest history entry.
    """
    interval = derive_interval_hours([t for t, _ in history])
    to_8h = Decimal(8) / Decimal(interval)

    if current_rate is None and history:
        current_rate = max(history, key=lambda r: r[0])[1]
    current_8h = (current_rate or Decimal(0)) * BPS * to_8h

    window_start = now_ms - 24 * 3_600_000
    window = [rate for t, rate in history if t >= window_start]
    realized_24h = sum(window, Decimal(0)) * BPS
    # 24h spans three 8h windows; realised/3 is the per-8h average paid.
    avg_24h_8h = realized_24h / Decimal(3)

    return FundingStat(
        symbol=symbol,
        interval_hours=interval,
        current_8h_bps=float(current_8h),
        avg_24h_8h_bps=float(avg_24h_8h),
        realized_24h_bps=float(realized_24h),
        samples_24h=len(window),
    )
