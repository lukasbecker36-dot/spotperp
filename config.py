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
SCAM_SNAPSHOT_FILE = DATA_DIR / "scam_snapshot.json"
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
FUNDING_ALL_TOP_N = 20                   # /funding all: names by 24h funding
SCREENER_MIN_NET_EDGE_BPS = Decimal("0") # show rows above this net edge
# Slippage allowance per ROUND TRIP, on top of fees, in the cost floor.
#
# 15, not the 2 this carried for months. Measured on real fills against the
# quotes showing while each order rested (scripts/adverse_selection.py, 612
# entry and 481 exit clips): entry gives up a median 6.0bps versus the board,
# exit 9.7, so a complete trade pays 15.6.
#
# The labelled dataset agrees independently, from prices rather than fills:
# holding 72h from a candidate whose net edge was 0-10bps returned a median
# +8.0 against a floor that budgeted 2 — i.e. it was losing money once the
# real 15.6 is charged. Break-even sits near net 10-15, not near 0.
#
# This roughly doubles ENTRY_MIN_EDGE_FLOOR_BPS (12 -> 25) and will visibly
# thin the screens. That is the intent: the rows it removes were not paying.
# Overstating costs misses trades, understating them takes losing ones.
SLIPPAGE_BUFFER_BPS = Decimal(os.environ.get("SLIPPAGE_BUFFER_BPS", "15.0"))
QUOTE_STALE_SECONDS = 10                 # ignore quotes older than this
MIN_DEPTH_NOTIONAL_USD = Decimal("200")  # min top-of-book notional on both sides
# Depth floor for the SCREENS only (nothing in execution reads this). Kept far
# below MIN_DEPTH_NOTIONAL_USD on purpose: top-of-book depth is the wrong
# measure for how these names actually trade. STONK shows ~$8 at the touch yet
# fills $42-99 clips by working an order over time, so a $200 floor silently
# excluded it — and every name like it — from all four screens while admitting
# deep books with no edge. The dwell, net-swing, jitter and $clip columns now
# do the discriminating; this only drops books that are outright empty.
SCREEN_MIN_DEPTH_USD = float(os.environ.get("SCREEN_MIN_DEPTH_USD", "5"))
# The screens' depth check once the 5-level figure is known: the first
# SCREEN_DEPTH_LEVELS MEXC asks summed must reach this. An entry hedges each
# perp fill with a spot buy that can sweep several levels while the blended
# basis holds, so the touch alone understates what is fillable. Default = one
# entry clip (ENTRY_MAX_CLIP_NOTIONAL_USD's $50): five levels that cannot
# hedge one clip stall the entry. Until a name's 5-level depth has been
# fetched, the touch check above (SCREEN_MIN_DEPTH_USD) stands in.
SCREEN_MIN_DEPTH5_USD = float(os.environ.get("SCREEN_MIN_DEPTH5_USD", "50"))
# Most names fetched for 5-level depth per scan (one REST call each): the
# boards' rows plus every name that passes the other checks, by volume.
SCREEN_DEPTH_WATCH_MAX = int(os.environ.get("SCREEN_DEPTH_WATCH_MAX", "150"))
# Pacing for that fetch, which runs in its own loop (never inside the 15s
# scan). MEXC's CDN (Akamai) bans an IP for BURSTS well inside the documented
# API limits, and a ban takes the whole MEXC leg down — balances, hedges,
# exit sales. Board names refresh every SCREEN_DEPTH_BOARD_SECONDS, near-miss
# candidates every SCREEN_DEPTH_CANDIDATE_SECONDS; requests go out
# SCREEN_DEPTH_BATCH at a time with SCREEN_DEPTH_PAUSE_SECONDS between. A 403
# (CDN block) stops the sweep for SCREEN_DEPTH_BACKOFF_SECONDS.
SCREEN_DEPTH_BOARD_SECONDS = float(os.environ.get("SCREEN_DEPTH_BOARD_SECONDS", "15"))
SCREEN_DEPTH_CANDIDATE_SECONDS = float(
    os.environ.get("SCREEN_DEPTH_CANDIDATE_SECONDS", "90")
)
SCREEN_DEPTH_BATCH = int(os.environ.get("SCREEN_DEPTH_BATCH", "3"))
SCREEN_DEPTH_PAUSE_SECONDS = float(os.environ.get("SCREEN_DEPTH_PAUSE_SECONDS", "0.5"))
SCREEN_DEPTH_BACKOFF_SECONDS = float(
    os.environ.get("SCREEN_DEPTH_BACKOFF_SECONDS", "900")
)
BASIS_LOG_SECONDS = 60.0                 # touch-basis CSV sampling cadence
# /screen reports a time-windowed mean of entry/net basis (sampled once per
# slow scan) so a persistent edge is distinguishable from a one-tick blip, and
# rows are ranked by the AVERAGE net edge rather than the instantaneous touch.
SCREEN_AVG_WINDOW_SECONDS = float(os.environ.get("SCREEN_AVG_WINDOW_SECONDS", "300"))
# /screen diff ranks by (5m avg entry basis - 24h avg entry basis): how far a
# pair sits ABOVE its own recent norm. Require at least this many hours of 24h
# history before a pair qualifies, or a thin window makes the gap meaningless.
SCREEN_DIFF_MIN_HOURS = float(os.environ.get("SCREEN_DIFF_MIN_HOURS", "6"))
# /screen swing looks for the STONK profile: a basis that goes WIDE, pays
# funding while you wait, then comes back to flat/negative so the position can
# actually be closed at a profit. A pair only qualifies if its 24h low (p10 of
# hourly means) actually reaches this level — otherwise it is permanently rich
# and there is no round trip, just carry. Funding must also be non-negative, so
# holding is paid rather than paid for.
SCREEN_SWING_EXIT_BPS = float(os.environ.get("SCREEN_SWING_EXIT_BPS", "5"))
SCREEN_SWING_MIN_FUNDING_BPS = float(
    os.environ.get("SCREEN_SWING_MIN_FUNDING_BPS", "0")
)
# /screen fill answers "can I actually get filled here?". Depth is RESTING
# size; a maker entry only fills when a taker lifts it, so these gate on FLOW:
# 24h perp volume, and how many of the last 24h the basis sat at a workable
# level (a basis that spikes for one tick cannot be worked; one wide for hours
# gives repeated chances — the STONK pattern).
# A floor for "someone is trading this at all", NOT a liquidity requirement:
# the names that actually fill a $100 clip can be far thinner than they look
# (STONK fills repeatedly on modest volume). Set too high this excludes exactly
# the profile worth trading. Check a known-good symbol's vol24 in /book and
# calibrate from that rather than trusting this default.
SCREEN_FILL_MIN_VOLUME_USD = float(
    os.environ.get("SCREEN_FILL_MIN_VOLUME_USD", "500000")
)
# /funding's own floor. Lower than /screen fill's: its score already scales
# down a name with little flow, and a $500k floor hid 281 of ~412 names.
FUNDING_MIN_VOLUME_USD = float(os.environ.get("FUNDING_MIN_VOLUME_USD", "150000"))
# Band edges for /funding's near-miss line: names hidden ONLY for volume,
# counted by how far under the floor they sit.
VOLUME_BAND_EDGES = (25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000)
# depth$ on /funding and /screen fill: the summed value of this many MEXC
# ask levels (the spot BUY side of an entry), not just the touch.
SCREEN_DEPTH_LEVELS = int(os.environ.get("SCREEN_DEPTH_LEVELS", "5"))
# /funding and /screen fill drop a name whose day's best round trip is too
# small: 24h ENTRY-basis high minus 24h EXIT-basis low must be at least
# SCREEN_MIN_EXIT_ROOM_BPS (default: the round-trip cost floor), or
# SCREEN_MIN_EXIT_ROOM_PCT of the entry high when that is larger. A % alone
# fails near zero: UPUSDT's best entry (-6.7) and best exit (-6.0) were the
# same level, which no percentage of -6.7 can see.
_room_bps = os.environ.get("SCREEN_MIN_EXIT_ROOM_BPS")
SCREEN_MIN_EXIT_ROOM_BPS = float(_room_bps) if _room_bps else None
SCREEN_MIN_EXIT_ROOM_PCT = float(os.environ.get("SCREEN_MIN_EXIT_ROOM_PCT", "10"))
# Score history + /alert. Every row on /screen fill and /funding is stored
# with its score this often, and kept this long, so /scores can say how often
# a level is reached. An alert re-arms for a name once it has been below the
# level for SCORE_ALERT_REARM_MINUTES; `/alert <board> auto` picks the lowest
# level that would have fired at most SCORE_ALERT_AUTO_PER_DAY times a day
# over the last SCORE_ALERT_LOOKBACK_DAYS.
# /study: longest a background study (build_dataset + report) may run.
STUDY_TIMEOUT_SECONDS = float(os.environ.get("STUDY_TIMEOUT_SECONDS", "1800"))
SCORE_HISTORY_SECONDS = float(os.environ.get("SCORE_HISTORY_SECONDS", "120"))
SCORE_HISTORY_DAYS = int(os.environ.get("SCORE_HISTORY_DAYS", "30"))
SCORE_ALERT_REARM_MINUTES = float(os.environ.get("SCORE_ALERT_REARM_MINUTES", "30"))
SCORE_ALERT_AUTO_PER_DAY = float(os.environ.get("SCORE_ALERT_AUTO_PER_DAY", "3"))
SCORE_ALERT_LOOKBACK_DAYS = float(os.environ.get("SCORE_ALERT_LOOKBACK_DAYS", "7"))
# /autoenter: when a board's score alert fires for a name, start a normal
# (non-carry) entry at the operator's notional. Hard limits, whatever the
# alerts say: open auto-entered positions, auto entries per rolling 24h, and
# a per-symbol cooldown since its last auto entry or add. A symbol already
# held is ADDED to when that position is OPEN and idle (it keeps its own kind
# and exit rules); adds count toward the daily cap and cooldown. One coin is
# acted on at most once per scan, so /funding and /screen spike alerting on it
# together cannot double up.
# Auto-entry only fires on a basis steady enough to work a resting order:
# 5m jitter at or under this (the boards' own cap, SCREEN_MAX_BASIS_JITTER_BPS,
# is looser so a human can still look at a flickery name). USUSDT auto-fired
# at jit 24.5 — under the board's 25 but untradeable; names that fill well sit
# in single digits. A name with too few samples to measure jit is skipped.
# Empty/"off" disables the check.
_aj = os.environ.get("AUTO_ENTER_MAX_JITTER_BPS", "10").strip().lower()
AUTO_ENTER_MAX_JITTER_BPS = None if _aj in ("", "off", "none") else float(_aj)
AUTO_ENTER_MAX_OPEN = int(os.environ.get("AUTO_ENTER_MAX_OPEN", "3"))
AUTO_ENTER_MAX_PER_DAY = int(os.environ.get("AUTO_ENTER_MAX_PER_DAY", "6"))
AUTO_ENTER_SYMBOL_COOLDOWN_HOURS = float(
    os.environ.get("AUTO_ENTER_SYMBOL_COOLDOWN_HOURS", "24")
)
# ── /screen spike: short-term mean reversion ──
# A name qualifies when its 5m entry basis sits well above its own 24h norm
# (median of hourly means): at least SPIKE_MIN_BPS above it AND SPIKE_MIN_Z
# robust deviations (MAD-scaled, the scale floored at SPIKE_MIN_SCALE_BPS),
# and reverting to the norm EXIT basis is worth at least SPIKE_MIN_EDGE_BPS
# after the cost floor. No dwell or funding requirement — a spike is brief by
# nature; funding over the hold is charged as a cost in the score instead.
# Calibrate from scripts/dataset_report.py --by spike_z --label norm.
# Calibrated on /study spike v2 (normal books, 6h): paid-within-6h and the
# worked-exit P&L rise with z — 79% / +1.6 at z<2, 86% / +18.7 at 3-5, 100% /
# +26.8 at >=5 — while spike SIZE in bps predicted nothing. So z gates and
# the bps floor is only a token.
SPIKE_MIN_BPS = float(os.environ.get("SPIKE_MIN_BPS", "10"))
SPIKE_MIN_Z = float(os.environ.get("SPIKE_MIN_Z", "3"))
SPIKE_MIN_SCALE_BPS = float(os.environ.get("SPIKE_MIN_SCALE_BPS", "5"))
SPIKE_MIN_EDGE_BPS = float(os.environ.get("SPIKE_MIN_EDGE_BPS", "10"))
SPIKE_MIN_VOLUME_USD = float(os.environ.get("SPIKE_MIN_VOLUME_USD", "150000"))
# Spikes only on NORMAL books: instant round trip (entry - exit basis) at or
# under this. Above it the study's apparent profits came from assuming both
# perp maker fills at once (ask in, bid out) — 'paid' at hour 0 — not from
# the basis reverting.
SPIKE_MAX_ROUNDTRIP_BPS = float(os.environ.get("SPIKE_MAX_ROUNDTRIP_BPS", "10"))
# Hours a spike trade is expected to take to revert — funding over it is
# charged in the score. (Reversions that paid did so in ~0.2-0.6h.)
SPIKE_HOLD_HOURS = float(os.environ.get("SPIKE_HOLD_HOURS", "1"))
# Optional time stop: after this many hours an unreverted spike works a
# passive close at ANY basis. OFF by default (operator decision) — a spike
# trade holds until its norm-exit auto-close, or you close it. Set hours to
# enable.
_smh = os.environ.get("SPIKE_MAX_HOLD_HOURS", "").strip().lower()
SPIKE_MAX_HOLD_HOURS: float | None = (
    None if _smh in ("", "off", "none") else float(_smh)
)
SCREEN_FILL_MIN_HOURS = float(os.environ.get("SCREEN_FILL_MIN_HOURS", "4"))
# Reject a basis that jumps wildly tick to tick: a resting order can't be
# worked against it, because the price you get is a lottery (BULLA printed
# -0.3/+158/-160/+27 inside a minute and a taker close fired on -160 then
# filled at +27). Measured as the mean absolute change between consecutive 5m
# samples, so a smooth drift is NOT penalised — only flicker. Check a symbol
# you trade happily in /screen fill and calibrate from its jit column.
SCREEN_MAX_BASIS_JITTER_BPS = float(
    os.environ.get("SCREEN_MAX_BASIS_JITTER_BPS", "25")
)
# A fillable name is not automatically a trade worth doing. The round trip
# (entry basis now - the pair's own 24h low) must clear the round-trip cost
# floor by at least this much, or you are working an order for nothing — or
# worse, entering something whose basis never comes back far enough to close
# (ZEREBRO: 44.9 entry, 24h low +32.3, so 12.6 gross and +0.6 after costs).
SCREEN_FILL_MIN_NET_SWING_BPS = float(
    os.environ.get("SCREEN_FILL_MIN_NET_SWING_BPS", "5")
)
# Expected taker events at which a resting order is reliably lifted. The fill
# factor in fill_score saturates here: below it a name may not fill at all;
# above it more flow adds nothing to a SINGLE round trip, so a hyperactive book
# must not outrank a much fatter edge on a quieter one.
SCREEN_FILL_TARGET_CHANCES = float(
    os.environ.get("SCREEN_FILL_TARGET_CHANCES", "500")
)
# Flag a 24h low above this as "never comes near zero": the net is real only if
# the exit target is set up there, and a basis that has never approached zero
# may be structurally rich rather than mean-reverting (BASECAT: 149 entry, 24h
# low +100.6).
SCREEN_FILL_FLAG_LO_BPS = float(os.environ.get("SCREEN_FILL_FLAG_LO_BPS", "25"))
# Shorting the perp is the premium trade, and a positive rate means the short
# RECEIVES. A negative rate turns the wait into a cost: you are paying to hold
# the position while the basis converges, which is the opposite of the setup
# /screen fill is looking for. Drop those rows. Raise this to demand the carry
# actually pays for the hold rather than merely not costing.
SCREEN_FILL_MIN_FUNDING_BPS = float(
    os.environ.get("SCREEN_FILL_MIN_FUNDING_BPS", "0")
)
# spread_cost_bps = entry_bps - close_bps is the INSTANT ROUND TRIP: enter
# (perp ask vs spot ask) and exit (perp bid vs spot bid) on today's books,
# before fees. It works out to perp spread - spot spread: the perp spread is
# earned (maker on both sides), the spot spread paid (taker on both sides).
#
# Above SCREEN_MAX_SPREAD_COST_BPS the perp book is so wide the quote is
# probably not real (ARGUSUSDT printed a 250bps entry on a $4 book) — though
# some blowouts are tradeable, so this stays loose. Hidden as 'spread'.
SCREEN_MAX_SPREAD_COST_BPS = float(
    os.environ.get("SCREEN_MAX_SPREAD_COST_BPS", "100")
)
# Below SCREEN_MIN_ROUNDTRIP_BPS crossing the books costs more than a typical
# edge: the basis must converge that far just to break even. UPUSDT: entry
# -24.0, exit +74.3, round trip -98 — a 108bps spot spread the +100 check can
# never see, since a wide SPOT book makes the number negative. 'roundtrip'.
SCREEN_MIN_ROUNDTRIP_BPS = float(
    os.environ.get("SCREEN_MIN_ROUNDTRIP_BPS", "-50")
)
# Holding period the /funding score assumes. The carry is a stream and the
# basis is a one-off, so they are only comparable over a stated horizon; 24h
# is one day of funding (3 settlements at the 8h-normalised rate) and roughly
# how long these positions are actually held.
FUNDING_SCORE_HOLD_HOURS = float(
    os.environ.get("FUNDING_SCORE_HOLD_HOURS", "24")
)
# How often total account value is sampled into equity_snapshots. The daily
# table reads the LAST sample of each UTC day, so this also sets how close the
# daily mark lands to midnight.
EQUITY_SNAPSHOT_MINUTES = float(os.environ.get("EQUITY_SNAPSHOT_MINUTES", "30"))
# Aster's own index (built from real spot venues) vs the MEXC mid. They should
# agree to within a spread; 5% apart means the two symbols are not the same
# asset or the contract multiplier is wrong, and the "basis" is arithmetic on
# two unrelated prices. This is the only gate that tests the quotes against an
# outside reference — depth/spread/jitter all test the two against EACH OTHER,
# so a consistently wrong price sails through them.
SCREEN_MAX_INDEX_DIVERGENCE_BPS = float(
    os.environ.get("SCREEN_MAX_INDEX_DIVERGENCE_BPS", "500")
)
# A second, longer basis window shown beside the 24h one in /book. The 24h
# range says whether the basis is high FOR THIS PAIR TODAY; this says whether
# today itself is unusual. The distinction is the CATE trap: it quoted +104
# against a 24h low of +120, because the 24h band had gone stale and was
# describing a regime that had already ended.
BASELINE_HOURS = int(os.environ.get("BASELINE_HOURS", "72"))
# /funding lists premium candidates: short the perp, buy the spot. A negative
# entry basis means the perp trades at a DISCOUNT, so shorting it starts the
# trade underwater and the carry has to pay that back before anything else.
# BANKUSDT scored +147 at an entry of -82 purely because its 24h low was -312
# and the score read that as convergence to come.
#
# 0 keeps discount rows off the board. /enter still accepts a negative target
# by hand — this is about what gets SURFACED, not what is permitted.
FUNDING_MIN_ENTRY_BPS = float(os.environ.get("FUNDING_MIN_ENTRY_BPS", "0"))

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
# Where the convergence auto-close starts, per position: 10% of the way back
# from the pair's 24h EXIT-basis low (p10 of the hourly maker-taker close
# basis) towards the entry basis — i.e. bank 90% of the move the day has
# shown is reachable, rather than waiting for zero. Entry +60, exit low +10
# -> close at +15. Capped at entry - ENTRY_MIN_EDGE_FLOOR_BPS so it never
# works an exit that can't cover costs. Falls back to CONVERGED_PASSIVE_BPS
# with too little history (SCREEN_DIFF_MIN_HOURS). Empty/"off" = fixed
# CONVERGED_PASSIVE_BPS as before.
_low_pct = os.environ.get("CONVERGED_EXIT_LOW_PCT", "10").strip().lower()
CONVERGED_EXIT_LOW_PCT: Decimal | None = (
    None if _low_pct in ("", "off", "none") else Decimal(_low_pct)
)
# Let the convergence auto-close CROSS both legs as taker when a taker close
# looks profitable. OFF by default: the estimate prices the WHOLE position at
# the touch with no margin, then sweeps both books in one go — USUSDT #274
# estimated +$1.26, sold $1,108 at once, filled +7.7bps against -5.8 quoted
# and realised -$3.34. Off, the auto-close always works the clipped passive
# maker close at its target. "1" restores the old behaviour.
CONVERGED_TAKER_TP = os.environ.get("CONVERGED_TAKER_TP", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
CONVERGED_PASSIVE_RESET_BPS = Decimal(
    os.environ.get("CONVERGED_PASSIVE_RESET_BPS", "5.0")
)
# Consecutive safety sweeps the "taker close is profitable" condition must
# hold before the engine actually crosses both legs. A thin book can print a
# basis hundreds of bps away from where a taker order really fills, and a
# single bad tick would otherwise open a real trade at a fictional price
# (BULLA #170 fired on a -160bps quote and filled at +27bps, for -$2.07 on an
# estimated +$0.38). Entries already require confirmation; this matches them.
CONVERGED_TP_CONFIRM_TICKS = int(
    os.environ.get("CONVERGED_TP_CONFIRM_TICKS", "3")
)
# Don't let the convergence auto-close fire until a position has been held this
# long. Right after entry the closeable (bid/bid) basis is dominated by the
# bid-ask SPREAD, not real convergence — on a wide microcap it's already <= 0
# the instant you enter, so the TP would round-trip both spreads for a loss
# (CASHCAT: entered +243bps, "converged" -91.7bps and closed within a minute).
# Adverse-widen stop and max-hold still apply from the start. 0 disables.
CONVERGENCE_MIN_HOLD_MINUTES = float(
    os.environ.get("CONVERGENCE_MIN_HOLD_MINUTES", "15")
)
# Alert (and flag in /positions & /recon) when a perp short's mark is within
# this % of its liquidation price. Re-alerts at most every throttle window
# while still in danger; re-arms once it recovers back above the threshold.
LIQ_ALERT_PCT = Decimal(os.environ.get("LIQ_ALERT_PCT", "30"))

# ── Exit-opportunity alerts (advisory only — never closes anything) ──
# A carry position's closeable basis can plunge while nobody is watching, and
# the chance to bank it is gone by the time it is noticed. Alert when BOTH hold,
# sustained:
#   - the close basis is below this pair's own 72h low (the p10 of hourly means
#     on the CLOSE series) — an unusual level for this name, not its normal one;
#   - closing now, after exit costs, beats EXIT_OPP_CARRY_DAYS days of the
#     carry you would give up — worth acting on, not merely unusual.
# Each gate alone fires wrongly: the range alone ignores whether exiting pays,
# the carry multiple alone fires on slow drift on anything entered rich.
EXIT_OPP_ALERTS = os.environ.get("EXIT_OPP_ALERTS", "1") not in ("0", "false", "")
EXIT_OPP_CARRY_DAYS = float(os.environ.get("EXIT_OPP_CARRY_DAYS", "3"))
# Must hold this long on every sweep: thin books print one-tick spikes all the
# time, and a level that holds is one that can actually be exited into.
EXIT_OPP_SUSTAIN_SECONDS = float(os.environ.get("EXIT_OPP_SUSTAIN_SECONDS", "120"))
# How far under the 72h low the close basis has to be (0 = anywhere below it).
EXIT_OPP_BELOW_LO_BPS = float(os.environ.get("EXIT_OPP_BELOW_LO_BPS", "0"))
# One alert per event; it re-arms only once the basis is back this far ABOVE
# the 72h low, so a level sitting on the boundary cannot alert repeatedly.
EXIT_OPP_REARM_BPS = float(os.environ.get("EXIT_OPP_REARM_BPS", "10"))
# The 72h low is meaningless on a pair with a day's history or less.
EXIT_OPP_MIN_HOURS = float(os.environ.get("EXIT_OPP_MIN_HOURS", "24"))
# Exit-side slippage, measured over 481 exit clips against the quotes showing
# while each rested (scripts/adverse_selection.py): median 9.7bps.
EXIT_SLIPPAGE_BPS = Decimal(os.environ.get("EXIT_SLIPPAGE_BPS", "10"))
LIQ_ALERT_THROTTLE_SECONDS = float(os.environ.get("LIQ_ALERT_THROTTLE_SECONDS", "1800"))
# /stops places protective orders this % below the perp liquidation price: a
# reduce-only buy STOP (stop-limit) on Aster that triggers on the mark, and a
# resting sell LIMIT on MEXC at the same level.
# Why 10% and a limit, not 1% and STOP_MARKET: in a squeeze Aster's thin book
# spikes far above the mark for seconds, and a market stop buys that wick
# (IRYS #313 triggered at mark 0.0643 and filled at 0.0709, -$666 on the perp —
# more than liquidation would have cost). A limit refuses the wick, and 10% of
# room lets it wait for the pullback before the mark reaches liquidation. On a
# replay of 275 pumps (spot >= 1.5x its 72h low, Aster 1m mark/last + MEXC 5m)
# this beat the 1% market stop by ~0.8% of notional per pump; tighter buffers
# got liquidated waiting (5%: 12 liquidations, 1%: 52).
STOP_LIQ_BUFFER_PCT = Decimal(os.environ.get("STOP_LIQ_BUFFER_PCT", "10"))
# The stop-limit's buy price, this % above its trigger: room for normal
# slippage in an ordinary fast market, while refusing to chase a wick. +1%
# filled too rarely before liquidation in the replay; above +3% added nothing.
# Never set above liquidation.
STOP_LIMIT_HEADROOM_PCT = Decimal(os.environ.get("STOP_LIMIT_HEADROOM_PCT", "3"))
# Re-place the stops when the liquidation price moves by more than this (%)
# from the level they were set against. Half the buffer: past that, a stop
# either fires well short of liquidation (margin added — position 249 was
# 64% from liq when its stale stop fired) or sits beyond it (margin removed).
# Small enough to catch a margin change, large enough not to churn on the slow
# drift funding causes on an isolated position.
STOP_LIQ_REFRESH_PCT = Decimal(os.environ.get("STOP_LIQ_REFRESH_PCT", "0.5"))
# Automatically keep /stops in place on a live OPEN position: place them when
# it has none (a brand-new position, or one whose stops were cancelled to run a
# partial exit and never re-armed) and re-place them whenever its size changes
# (a /enter size-up or a completed part-reduce). Without this, protection
# depends on remembering to run /stops by hand. Set AUTO_STOPS=0 to disable.
AUTO_STOPS = os.environ.get("AUTO_STOPS", "1").strip().lower() not in (
    "0", "false", "no", "off", ""
)
# Don't retry a failed auto-placement more often than this (a fresh position
# may have no liquidation price yet; retrying every sweep would spam alerts).
AUTO_STOPS_RETRY_SECONDS = float(os.environ.get("AUTO_STOPS_RETRY_SECONDS", "300"))

# ── Hedge-integrity guard (ADL protection) ──
# The venue can close/reduce the perp leg WITHOUT any order of ours: auto-
# deleveraging (a profitable short gets force-closed against liquidated longs),
# venue liquidation, or a manual close on the exchange UI. That leaves the spot
# leg naked long. The safety loop compares each live position's DB perp qty to
# Aster positionRisk and, when the venue shows less:
#   1. alerts immediately, then confirms the deficit continuously for
#      HEDGE_BREAK_CONFIRM_SECONDS using only FRESH positionRisk data (a stale
#      cache or API outage never triggers action);
#   2. reconciles the DB perp to venue reality (synthetic exit fill at mark);
#   3. sells the now-unhedged spot down to the surviving perp size in tranches
#      of ADL_SELL_TRANCHE_PCT every ADL_SELL_INTERVAL_SECONDS (market-selling
#      a plunged microcap in one clip would eat the book).
HEDGE_BREAK_CONFIRM_SECONDS = float(os.environ.get("HEDGE_BREAK_CONFIRM_SECONDS", "30"))
HEDGE_BREAK_RISK_FRESH_SECONDS = float(
    os.environ.get("HEDGE_BREAK_RISK_FRESH_SECONDS", "45")
)
HEDGE_BREAK_TOLERANCE_PCT = Decimal(os.environ.get("HEDGE_BREAK_TOLERANCE_PCT", "1"))
# How often to compare a position's DB spot leg against the MEXC balance.
# Cheap (one account call) but not needed every sweep; the divergence it
# catches (an ambiguous sale that actually filled) is rare and static.
SPOT_CHECK_INTERVAL_SECONDS = float(
    os.environ.get("SPOT_CHECK_INTERVAL_SECONDS", "60")
)
ADL_SELL_TRANCHE_PCT = Decimal(os.environ.get("ADL_SELL_TRANCHE_PCT", "10"))
ADL_SELL_INTERVAL_SECONDS = float(os.environ.get("ADL_SELL_INTERVAL_SECONDS", "10"))
# When the perp deficit is explained by OUR OWN /stops stop order having fired
# (not an ADL), the MEXC sell LIMIT twin is already resting at the chosen stop
# price — leave it working for this long before cancelling it and falling back
# to tranche market sells. Aster's stop triggers on the Aster mark; MEXC often
# reaches the level seconds later, and market-dumping immediately realises the
# venue price gap at the worst possible moment.
STOP_SPOT_GRACE_SECONDS = float(os.environ.get("STOP_SPOT_GRACE_SECONDS", "120"))
# The reverse case: how often to poll each position's MEXC stop-limit for having
# filled BEFORE the Aster stop (a MEXC-only spike). A filled spot stop leaves
# the perp short naked, so it is closed at once (_check_spot_stop_fill).
SPOT_STOP_POLL_SECONDS = float(os.environ.get("SPOT_STOP_POLL_SECONDS", "10"))
# ── Scam-candidate screen (scam_screen.py; advisory, never trades) ──
# Scores every Aster coin perp on the insider-squeeze warning signs and alerts
# on Telegram when a name first reaches SCAM_ALERT_SCORE, or its funding falls
# to SCAM_NEG_FUNDING_APR (crowded shorts). Set SCAM_SCREEN=0 to switch off.
SCAM_SCREEN = os.environ.get("SCAM_SCREEN", "1").strip().lower() not in (
    "0", "false", "no", "off", ""
)
SCAM_SCREEN_INTERVAL_SECONDS = float(os.environ.get("SCAM_SCREEN_INTERVAL_SECONDS", "3600"))
# Market caps (CoinGecko) and daily candles: slow-moving, and CoinGecko's free
# tier rate-limits hard (~10 calls/min). Optional demo key: COINGECKO_API_KEY.
SCAM_SLOW_REFRESH_SECONDS = float(os.environ.get("SCAM_SLOW_REFRESH_SECONDS", "21600"))
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
SCAM_COINGECKO_PAGES = int(os.environ.get("SCAM_COINGECKO_PAGES", "8"))   # x250 = top 2000
SCAM_COINGECKO_PAGE_PAUSE_SECONDS = float(os.environ.get("SCAM_COINGECKO_PAGE_PAUSE_SECONDS", "8"))
# Alert at this score (max 15). On 2026-10-24 the live universe scored: 14 of
# 569 coin perps at >=5, 134 at >=4. Raised to 9 (operator: alert only on
# scores above 8) — the full list stays on /scam.
SCAM_ALERT_SCORE = int(os.environ.get("SCAM_ALERT_SCORE", "9"))
# Funding at or below this (annualised, 1.0 = 100%) marks crowded shorts that
# can squeeze (MYX ran -2%/4h before its squeeze). -2.0 = about -18bps per 8h;
# 11 names were there on 2026-10-24. It ALERTS only alongside a score at or
# above SCAM_ALERT_SCORE — or for a coin you hold, where a squeeze hits your
# own short.
SCAM_NEG_FUNDING_APR = float(os.environ.get("SCAM_NEG_FUNDING_APR", "-2.0"))
SCAM_ALERT_REARM_HOURS = float(os.environ.get("SCAM_ALERT_REARM_HOURS", "24"))
# Each sweep also appends output/scam_log_YYYYMMDD.csv: open interest, funding,
# volumes and order-book shape (USD within +-2/5/10% of mid on Aster and MEXC)
# for every coin perp — history neither venue serves later, logged so whether
# OI build-up or a thin ask side leads a spike can be tested in a few weeks.
SCAM_LOG_DEPTH = os.environ.get("SCAM_LOG_DEPTH", "1").strip().lower() not in (
    "0", "false", "no", "off", ""
)
SCAM_DEPTH_LEVELS = int(os.environ.get("SCAM_DEPTH_LEVELS", "100"))
SCAM_FETCH_CONCURRENCY = int(os.environ.get("SCAM_FETCH_CONCURRENCY", "3"))
SCAM_ALERT_MAX_LINES = int(os.environ.get("SCAM_ALERT_MAX_LINES", "20"))
# Thresholds (HackerNoon screening heuristics; float + recent-pump added here).
SCAM_OI_MCAP_WARN, SCAM_OI_MCAP_DANGER = 0.5, 1.0
SCAM_PERP_SPOT_WARN, SCAM_PERP_SPOT_DANGER = 15.0, 40.0
SCAM_FUNDING_APR_WARN, SCAM_FUNDING_APR_DANGER = 1.0, 3.0      # negative side
# Positive funding (longs paying) scores heavier: on the replay a 24h average
# above +10bp/8h (~+110%/yr) preceded a +50% week 4.7x as often as normal
# funding, against 2.8x for crowded shorts below -20bp/8h.
SCAM_FUNDING_POS_WARN, SCAM_FUNDING_POS_DANGER = 1.0, 3.0
# 'Already ran': price >= 3x its 45-day low and >= 30% below its 45-day high.
SCAM_RAN_OFF_LOW, SCAM_RAN_BELOW_HIGH = 3.0, 0.30
SCAM_AGE_WARN_DAYS, SCAM_AGE_DANGER_DAYS = 180.0, 60.0
SCAM_MCAP_WARN_USD, SCAM_MCAP_DANGER_USD = 100e6, 30e6
SCAM_FLOAT_WARN = 0.20
SCAM_PUMP_DAY_GAIN = 0.50
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
# Re-fetch both venues' exchangeInfo and rebuild the cross-listed universe on
# this cadence so newly-listed coins become tradeable without an engine
# restart (also exposed on demand via the /refresh command).
SYMBOL_REFRESH_SECONDS = float(os.environ.get("SYMBOL_REFRESH_SECONDS", "1800"))
FUNDING_REFRESH_SECONDS = 900.0          # full funding-history sweep cadence
FUNDING_FETCH_BATCH = 8                  # concurrent funding-history fetches
FUNDING_HISTORY_LIMIT = 30               # prints per symbol (>= 24h on 1h funding)
COMMAND_POLL_SECONDS = 1.0
# Drop queued Telegram commands older than this before executing them: if the
# engine was down when you sent /enter or /flatten, it must NOT fire minutes
# later at a different market. Also the claim-before-execute window that stops
# a crash mid-command from replaying it on restart.
COMMAND_TTL_SECONDS = float(os.environ.get("COMMAND_TTL_SECONDS", "60"))
ORDER_STATUS_POLL_SECONDS = 2.0
# Cancel/replace throttle for a RESTING MAKER order. This is not the hedge
# lag (that is POLL_INTERVAL_SECONDS): it bounds how long an order may sit at a
# price the market has moved away from. A stale resting perp SELL in a rising
# market is picked off below the live ask — STONK #186 locked +58.8bps while
# the market showed +121 — and a resting order sized to spot depth that has
# since vanished can be hit with nothing to hedge against. Both shrink as this
# falls; the cost is more cancel/replace traffic to the venue.
REPRICE_MIN_INTERVAL_SECONDS = float(os.environ.get(
    "REPRICE_MIN_INTERVAL_SECONDS", "1.0"))
# Exits reprice faster than entries: a resting buy-back sized to spot bid
# depth must be pulled quickly when that depth vanishes, or it can be hit
# with no spot to hedge against (leg desync). Lower = tighter, more
# cancel/replace traffic.
EXIT_REPRICE_MIN_INTERVAL_SECONDS = float(os.environ.get(
    "EXIT_REPRICE_MIN_INTERVAL_SECONDS", "1.0"))
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
# hedge locks that worse spot. So the realized entry basis can land below the
# resting floor. Once the perp has FILLED, the entry-floor decision is sunk —
# the only choice left is hedge-and-hold vs unwind (a guaranteed taker-spread
# loss for zero position). So before hedging each perp fill we re-price the
# basis the hedge would ACTUALLY pay against fresh spot depth, and:
#   - SALVAGE (hedge and keep the position) as long as that basis is >=
#     ENTRY_HEDGE_MIN_BPS — holding a thin-but-positive entry beats paying to
#     unwind. You may end up entered below the floor you wanted.
#   - UNWIND (don't enter) only when it is below ENTRY_HEDGE_MIN_BPS, i.e. so
#     low that holding would lock a loss. Set higher to be pickier (more
#     unwinds), lower/negative to salvage even more.
#   - ALERT (but still enter) if the final realized basis lands more than
#     ENTRY_REALIZED_ALERT_BPS below the floor, so it is never a silent miss.
ENTRY_HEDGE_MIN_BPS = Decimal(os.environ.get("ENTRY_HEDGE_MIN_BPS", "0"))
# ...but an absolute floor assumes every entry is a premium trade. Entering at
# a DELIBERATELY negative basis is a real trade — short perp + long spot pays
# (entry - exit), so entering at -15 to exit at -50 earns 35bps, and a positive
# carry pays you to wait. Unwinding such a fill for being negative refuses the
# position the user actually asked for. So the salvage floor is the LOWER of
# ENTRY_HEDGE_MIN_BPS and (the entry target you set - this slack): never
# unwind merely for filling at the level you asked for. Premium entries are
# unaffected, since for a +30 target the absolute 0 floor is already lower.
ENTRY_HEDGE_SLIP_BPS = Decimal(os.environ.get("ENTRY_HEDGE_SLIP_BPS", "15"))
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
# Cap each resting maker clip so a single taker sweep catches at most this
# notional before the next tick re-checks the (possibly collapsed) basis and
# stops resting — bounding the adverse-selection blast radius. Set to a large
# value to rest the full size at once. Lower = safer on thin names, more clips.
#
# $50 rather than $100, measured over 612 real entry clips against the quotes
# that were showing while the order rested (scripts/adverse_selection.py):
#
#   clip <$18   slippage  2.3 bps      clip $50-99   slippage  4.4
#   clip $18-50 slippage  3.8 bps      clip >=$99    slippage 12.6
#
# The jump at the cap is walking the book, not thin books: the executor
# already caps a clip to the depth that supports it, so the LARGE clips are
# the ones on deep books and they still slip three times as much. Halving the
# cap is worth ~8bps an entry against a median quoted edge of 40.
ENTRY_MAX_CLIP_NOTIONAL_USD: Decimal | None = (
    Decimal(os.environ["ENTRY_MAX_CLIP_NOTIONAL_USD"])
    if os.environ.get("ENTRY_MAX_CLIP_NOTIONAL_USD") else Decimal("50")
)
# Same idea for a PASSIVE exit: cap each resting perp buy-back clip. A resting
# maker is sized to the spot bid depth AT PLACEMENT, but it fills later — by
# which time that bid may be gone, so a big resting clip can be swept while the
# reactive spot sell walks a thinned book (leg desync). Small clips bound how
# much perp a single sweep can close before the next tick re-reads spot depth.
#
# Left at $100 deliberately: unlike entries, measured exit slippage is FLAT
# across clip size (9.9 / 9.7 / 12.2 / 8.3 bps from the smallest quartile to
# the largest), so there is nothing to buy by halving it here.
EXIT_MAX_CLIP_NOTIONAL_USD: Decimal | None = (
    Decimal(os.environ["EXIT_MAX_CLIP_NOTIONAL_USD"])
    if os.environ.get("EXIT_MAX_CLIP_NOTIONAL_USD") else Decimal("100")
)

# ── Funding ──
FUNDING_INTERVAL_HOURS = 8

# ── AI advisor (advisory only — never trades) ──
# A periodic Claude review of open positions (basis / funding / liq proximity)
# and top funding opportunities, delivered to Telegram. Reads the same snapshot
# files the bot's /funding, /positions and /screen commands use; it only ever
# SENDS MESSAGES — it cannot place, size or close orders. Trigger on demand with
# /review, or on a schedule via deploy/basis-trade-advisor.timer.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ADVISOR_MODEL = os.environ.get("ADVISOR_MODEL", "claude-sonnet-5-5")
# Thinking depth for the review (low|medium|high|xhigh|max).
ADVISOR_EFFORT = os.environ.get("ADVISOR_EFFORT", "medium")
ADVISOR_MAX_TOKENS = int(os.environ.get("ADVISOR_MAX_TOKENS", "16000"))
# How many top funding-carry opportunities to show the advisor as alternatives.
ADVISOR_TOP_OPPORTUNITIES = int(os.environ.get("ADVISOR_TOP_OPPORTUNITIES", "12"))
# Scheduled runs only fire when the local hour (ADVISOR_TIMEZONE) is one of
# these — 07:30 then every 4h to 23:30 UK, nothing overnight. The timer wakes
# hourly and the advisor self-gates, so this works on any systemd version and
# whatever the box's own clock is set to. /review ignores the gate entirely.
ADVISOR_TIMEZONE = os.environ.get("ADVISOR_TIMEZONE", "Europe/London")
ADVISOR_RUN_HOURS = [
    int(h) for h in os.environ.get("ADVISOR_RUN_HOURS", "7,11,15,19,23").split(",")
    if h.strip()
]

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
