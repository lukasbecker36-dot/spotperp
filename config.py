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

# ── Strategy parameters (safety stops apply even to manual positions) ──
EXIT_BASIS_BPS = Decimal("5.0")          # default passive-exit target basis
ADVERSE_STOP_BPS = Decimal("-50.0")      # force-close if basis inverts below this
MAX_HOLD_HOURS = 168                     # 1 week max hold
MAX_CONCURRENT_POSITIONS = 3
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
ENTRY_MIN_EDGE_FLOOR_BPS = Decimal("0.0")  # stop chasing entry below this net edge
ENTRY_TIMEOUT_MINUTES = 60
EXIT_TIMEOUT_MINUTES = 30
UNWIND_TIMEOUT_SECONDS = 60              # hard limit to flatten a naked leg
HEDGE_RETRY_ATTEMPTS = 3
HEDGE_SLIPPAGE_BPS = Decimal("10.0")     # IOC limit price buffer past the touch
MIN_HEDGE_NOTIONAL_USD = Decimal("5")    # accumulate partial fills below this

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
