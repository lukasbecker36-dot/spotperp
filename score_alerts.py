"""Score alerts for /funding and /screen fill: message when a board score
crosses an operator-set level, once per name per episode.

The same crossing rule drives the live alert (AlertState) and the replay of
recorded history (simulate), so "this level would have fired N times a day"
in /scores is exactly what /alert at that level will do.
"""
from __future__ import annotations

from collections.abc import Iterable

import config

BOARDS = ("fill", "funding")
BOARD_LABEL = {"fill": "/screen fill", "funding": "/funding"}
DAY_MS = 86_400_000


class AlertState:
    """One alert per name per episode.

    A name alerts when its score first reaches the level, then stays quiet
    while it remains there. It re-arms only after it has been below the level
    (or off the board) for SCORE_ALERT_REARM_MINUTES, so a score hovering on
    the line cannot ping every scan.
    """

    def __init__(self, rearm_ms: float | None = None) -> None:
        self._rearm_ms = (
            config.SCORE_ALERT_REARM_MINUTES * 60_000 if rearm_ms is None
            else rearm_ms
        )
        self._last_above: dict[tuple[str, str], int] = {}

    def observe(self, ts_ms: int, board: str, symbol: str, score: float,
                level: float) -> bool:
        """True when this sample should alert."""
        if score < level:
            return False
        key = (board, symbol)
        prev = self._last_above.get(key)
        self._last_above[key] = ts_ms
        return prev is None or ts_ms - prev > self._rearm_ms

    def reset(self, board: str) -> None:
        """Forget a board's episodes (alert switched off or level changed), so
        names already above a new level alert straight away."""
        for key in [k for k in self._last_above if k[0] == board]:
            del self._last_above[key]


def simulate(history: Iterable, level: float,
             rearm_ms: float | None = None) -> list[tuple[int, str, float]]:
    """Replay recorded (ts_ms, symbol, score) rows, oldest first; return the
    alerts AlertState would have sent at this level."""
    state = AlertState(rearm_ms)
    out = []
    # Only samples at/above the level can alert, and the rule only compares
    # each with the previous above-level sample of the same name — so the rest
    # can be skipped, which keeps a level scan over weeks of history cheap.
    for ts, sym, score in (r for r in history if float(r[2]) >= level):
        if state.observe(int(ts), "x", sym, float(score), level):
            out.append((int(ts), sym, float(score)))
    return out


def span_days(history: list) -> float:
    if len(history) < 2:
        return 0.0
    return max((history[-1][0] - history[0][0]) / DAY_MS, 1 / 24)


def ladder(history: list, levels: Iterable[float] | None = None) -> list[dict]:
    """Alerts/day and distinct names at a range of levels.

    Default levels are percentiles of every recorded board score, so the
    ladder spans what the board actually shows rather than guessed numbers.
    """
    days = span_days(history)
    if not history or days <= 0:
        return []
    if levels is None:
        scores = sorted(float(r[2]) for r in history)
        picks = []
        for p in (50, 75, 90, 95, 98, 99, 99.5):
            v = scores[min(len(scores) - 1, int(len(scores) * p / 100))]
            picks.append(round(v))
        levels = sorted(set(picks))
    out = []
    for lv in levels:
        hits = simulate(history, lv)
        out.append({
            "level": float(lv),
            "per_day": len(hits) / days,
            "names": len({h[1] for h in hits}),
            "alerts": len(hits),
        })
    return out


def auto_level(history: list, per_day: float | None = None) -> float | None:
    """The lowest level reached by walking DOWN from the top score while the
    alert rate stays at or under `per_day`.

    Walking down, not up, because alerts/day is not monotonic in the level.
    Near the top it rises as the level falls; but far enough down, names sit
    above the line all day and alert only once, so a uselessly low level
    also looks quiet. The first level that gets too noisy marks the edge.
    """
    target = config.SCORE_ALERT_AUTO_PER_DAY if per_day is None else per_day
    days = span_days(history)
    if not history or days <= 0:
        return None
    candidates = sorted({round(float(r[2])) for r in history}, reverse=True)
    best = None
    for lv in candidates:
        if len(simulate(history, lv)) / days > target:
            break
        best = float(lv)
    return best if best is not None else float(candidates[0]) + 1


def fmt_volume(v: float) -> str:
    """Compact USD volume: 1.2B / 3.4M / 560k."""
    if v >= 1e9:
        return f"{v / 1e9:.1f}B"
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}k"
    return f"{v:.0f}"


def format_alert(board: str, row: dict, level: float) -> str:
    def num(key, fmt, scale=1.0):
        v = row.get(key)
        return format(v * scale, fmt) if v is not None else "-"
    vol = row.get("volume_24h")
    return (
        f"🔔 {BOARD_LABEL[board]} {row['symbol']}: score {row['score']:+.1f}"
        f" ≥ {level:g}\n"
        f"entry {num('entry_bps', '.1f')}  lo24 {num('lo24_bps', '.1f')}"
        f"  hi24 {num('hi24_bps', '.1f')}  fund"
        f" {num('funding_8h_bps', '+.2f', 1 / 8)}bps/h\n"
        f"vol {fmt_volume(vol) if vol else '-'}"
        f"  depth$ {num('depth_usd', ',.0f')}  jit {num('jitter_bps', '.1f')}\n"
        f"/book {row['symbol']} before entering"
    )
