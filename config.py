"""All tunables: endpoints, fees, thresholds, sizing, execution timing.

Values can be overridden via environment variables of the same name where
noted. Monetary values and rates are Decimal throughout.
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
DB_PATH = DATA_DIR / "positions.db"
MODE_FILE = DATA_DIR / "mode.env"
SCREENER_SNAPSHOT_FILE = DATA_DIR / "screener_snapshot.json"
FUNDING_SNAPSHOT_FILE = DATA_DIR / "funding_snapshot.json"
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"

# ── Exchange endpoints ──
ASTER_BASE = os.environ.get("ASTER_BASE", "https://fapi.asterdex.com")
ASTER_WS_BASE = os.environ.get("ASTER_WS_BASE", "wss://fstream.asterdex.com")
MEXC_BASE = os.environ.get("MEXC_BASE", "https://api.mexc.com")

# ── Fee schedule (fractions, not bps; verify against live commission endpoints) ──
ASTER_MAKER_FEE = Decimal(os.environ.get("ASTER_MAKER_FEE", "0.0"))
ASTER_TAKER_FEE = Decimal(os.environ.get("ASTER_TAKER_FEE", "0.0009"))
MEXC_MAKER_FEE = Decimal(os.environ.get("MEXC_MAKER_FEE", "0.0"))
MEXC_TAKER_FEE = Decimal(os.environ.get("MEXC_TAKER_FEE", "0.0005"))

# Entry: Aster maker + MEXC taker. Exit (aggressive): Aster taker + MEXC taker.
# Exit (passive): Aster maker + MEXC taker.
ENTRY_FEE = ASTER_MAKER_FEE + MEXC_TAKER_FEE
EXIT_FEE_PASSIVE = ASTER_MAKER_FEE + MEXC_TAKER_FEE
EXIT_FEE_AGGRESSIVE = ASTER_TAKER_FEE + MEXC_TAKER_FEE

# ── Screener ──
SCREENER_TOP_N = 15                      # rows kept in the snapshot
SCREENER_MIN_NET_EDGE_BPS = Decimal("0") # show rows above this net edge
SLIPPAGE_BUFFER_BPS = Decimal("2.0")     # haircut for taker slippage per round trip
QUOTE_STALE_SECONDS = 10                 # ignore quotes older than this
MIN_DEPTH_NOTIONAL_USD = Decimal("200")  # min top-of-book notional on both sides
BASIS_LOG_SECONDS = 60.0                 # touch-basis CSV sampling cadence
# /screen reports a time-windowed mean of entry/net basis (sampled once per
# slow scan) so a persistent edge is distinguishable from a one-tick blip, and
# rows are ranked by the AVERAGE net edge rather than the instantaneous touch.
SCREEN_AVG_WINDOW_SECONDS = float(os.environ.get("SCREEN_AVG_WINDOW_SECONDS", "300"))

# ── Strategy parameters (safety stops apply even to manual positions) ──
EXIT_BASIS_BPS = Decimal("5.0")          # default passive-exit target basis
# Adverse stop: force-close when the closeable basis has WIDENED this far
# above the entry basis. DISABLED by default (None): a perp and its own spot
# are tied together by funding and won't diverge enough to threaten the perp
# margin, so we run without it (operator decision). Set a bps value here or
# via the ADVERSE_WIDEN_STOP_BPS env var to re-enable.
ADVERSE_WIDEN_STOP_BPS: Decimal | None = (
    Decimal(os.environ["ADVERSE_WIDEN_STOP_BPS"])
    if os.environ.get("ADVERSE_WIDEN_STOP_BPS") else None
)
# Convergence auto-close (two-tier). The closeable (maker-taker) basis is the
# convergence measure: perp maker buy-back at the Aster bid vs spot taker sell
# at the MEXC bid.
#   1. Once it reaches CONVERGED_PASSIVE_BPS the premium has converged, so the
#      engine starts WORKING A PASSIVE maker buy-back (0 perp fee) at that
#      target — booking the convergence without paying to cross the perp.
#   2. If the basis runs negative far enough that an AGGRESSIVE taker-both-legs
#      close is also net positive (funding + price, after taker fees), it
#      crosses immediately and locks it (the passive fill might never come).
#   3. If the basis recovers back above CONVERGED_PASSIVE_BPS +
#      CONVERGED_PASSIVE_RESET_BPS the passive work is stood down and the
#      position returns to OPEN (keeps collecting funding, max-hold re-armed).
# Carry trades ignore all of this — operator /exit only.
CONVERGED_PASSIVE_BPS = Decimal(os.environ.get("CONVERGED_PASSIVE_BPS", "0.0"))
CONVERGED_PASSIVE_RESET_BPS = Decimal(
    os.environ.get("CONVERGED_PASSIVE_RESET_BPS", "5.0")
)
MAX_HOLD_HOURS = 168                     # 1 week max hold
# Aster perp margin applied to each symbol before its first live entry: 1x
# isolated keeps the short fully margined (liquidation only on a ~100% move),
# matching the fully-funded-perp capital model and the no-adverse-stop choice.
ASTER_LEVERAGE = int(os.environ.get("ASTER_LEVERAGE", "1"))
ASTER_MARGIN_TYPE = os.environ.get("ASTER_MARGIN_TYPE", "ISOLATED")
MAX_NOTIONAL_PER_LEG_USD = Decimal("5000")

# ── Execution ──
POLL_INTERVAL_SECONDS = 1.0              # fast tick: book refresh + executor step
SLOW_SCAN_SECONDS = 15.0                 # screener snapshot + funding refresh
FUNDING_REFRESH_SECONDS = 900.0          # full funding-history sweep cadence
FUNDING_FETCH_BATCH = 8                  # concurrent funding-history fetches
FUNDING_HISTORY_LIMIT = 30               # prints per symbol (>= 24h on 1h funding)
COMMAND_POLL_SECONDS = 1.0
ORDER_STATUS_POLL_SECONDS = 2.0
REPRICE_MIN_INTERVAL_SECONDS = 3.0       # don't cancel/replace faster than this
PASSIVE_UNREACHABLE_ALERT_SECONDS = 600  # throttle "passive target unreachable" alerts
# Default basis floor while an entry works: stop resting/repricing when the
# executable basis decays below cost breakeven (fees + slippage buffer), so a
# falling perp ask can't walk the order down into an unprofitable entry.
# Overridable per entry: /enter SYMBOL NOTIONAL [min_bps].
ENTRY_MIN_EDGE_FLOOR_BPS = Decimal(os.environ.get(
    "ENTRY_MIN_EDGE_FLOOR_BPS",
    str((ENTRY_FEE + EXIT_FEE_PASSIVE) * 10000 + SLIPPAGE_BUFFER_BPS),
))
ENTRY_TIMEOUT_MINUTES = 60
EXIT_TIMEOUT_MINUTES = 30
UNWIND_TIMEOUT_SECONDS = 60              # hard limit to flatten a naked leg
HEDGE_RETRY_ATTEMPTS = 3
# A resting maker perp leg is adverse-selected: it fills preferentially when
# the spot has rallied and the basis has compressed, then the reactive taker
# hedge locks that worse spot. So the realized entry basis can land well below
# the resting floor. Before hedging each perp fill we re-price the basis the
# hedge would ACTUALLY pay against fresh spot depth, and:
#   - abort (unwind the perp increment, don't enter) if it is below
#     floor - ENTRY_HEDGE_ABORT_BPS, so a severe collapse never enters;
#   - alert (but still enter) if the final realized basis lands more than
#     ENTRY_REALIZED_ALERT_BPS below the floor, so it is never a silent miss.
ENTRY_HEDGE_ABORT_BPS = Decimal(os.environ.get("ENTRY_HEDGE_ABORT_BPS", "20"))
ENTRY_REALIZED_ALERT_BPS = Decimal(os.environ.get("ENTRY_REALIZED_ALERT_BPS", "10"))
# Buffer added past the live depth level that completes the hedge fill (the
# IOC is priced to cross real resting depth up to the needed size, then this
# buffer on top). Wider = surer fill on fast/thin names, at most this much
# extra slippage. 10bps off the cached touch was too tight for microcaps.
HEDGE_SLIPPAGE_BPS = Decimal("25.0")
MIN_HEDGE_NOTIONAL_USD = Decimal("5")    # accumulate partial fills below this
# Spot book levels fetched when sizing an entry/hedge. The taker leg walks
# DOWN the ask book past top-of-book as long as the volume-weighted basis
# still clears the floor (see max_hedgeable_qty), so a deeper fetch lets a
# single placement absorb more size on a thin top-of-book name. 20 was the
# old default; 50 reaches well past the touch without a heavy depth payload.
ENTRY_DEPTH_LEVELS = int(os.environ.get("ENTRY_DEPTH_LEVELS", "50"))
# Largest notional rested in a SINGLE perp maker clip. The hedgeable-size cap
# can legitimately rest a big order when spot is deep, but a resting maker is
# adverse-selected: a violent taker sweep fills the whole clip in one tick at
# the exact moment the basis collapses, and the hedge-abort guard then unwinds
# all of it as a taker at a loss (ASTEROID: one 902k clip swept -> -40.7bps ->
# -3.70 unwind). Capping the clip limits that blast radius — a sweep catches at
# most one clip, then edge_ok goes false and the remainder never rests. None
# (default) keeps the old single-order behaviour; set e.g. 100 to chunk entries.
ENTRY_MAX_CLIP_NOTIONAL_USD: Decimal | None = (
    Decimal(os.environ["ENTRY_MAX_CLIP_NOTIONAL_USD"])
    if os.environ.get("ENTRY_MAX_CLIP_NOTIONAL_USD") else None
)

# ── Funding ──
FUNDING_INTERVAL_HOURS = 8

# ── Mode ──
def paper_mode() -> bool:
    """Read paper/live flag. data/mode.env wins over the environment."""
    if MODE_FILE.exists():
        for line in MODE_FILE.read_text().splitlines():
            if line.startswith("TRADING_MODE="):
                return line.split("=", 1)[1].strip().lower() != "live"
    return os.environ.get("TRADING_MODE", "paper").lower() != "live"


def set_mode(live: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODE_FILE.write_text(f"TRADING_MODE={'live' if live else 'paper'}\n")
