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
    os.environ.get("SCREEN_FILL_MIN_VOLUME_USD", "50000")
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
# A quoted basis is only believable if the two books are close enough together
# to transact against. spread_cost_bps = entry_bps - close_bps is exactly what
# crossing both books costs right now, so a huge value means the quotes are far
# apart and NEITHER side is a real price: ARGUSUSDT printed a 250bps entry on a
# $4 book. This is the "clear error" gate — a magnitude cap on the basis itself
# would also throw away the genuinely rich names, which is the whole edge.
SCREEN_MAX_SPREAD_COST_BPS = float(
    os.environ.get("SCREEN_MAX_SPREAD_COST_BPS", "100")
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
LIQ_ALERT_THROTTLE_SECONDS = float(os.environ.get("LIQ_ALERT_THROTTLE_SECONDS", "1800"))
# /stops places protective orders this % below the perp liquidation price: a
# reduce-only buy STOP_MARKET on Aster (triggers on the mark, closing the short
# before liquidation) and a resting sell LIMIT on MEXC at the same level.
STOP_LIQ_BUFFER_PCT = Decimal(os.environ.get("STOP_LIQ_BUFFER_PCT", "1"))
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
# When the perp deficit is explained by OUR OWN /stops STOP_MARKET having fired
# (not an ADL), the MEXC sell LIMIT twin is already resting at the chosen stop
# price — leave it working for this long before cancelling it and falling back
# to tranche market sells. Aster's stop triggers on the Aster mark; MEXC often
# reaches the level seconds later, and market-dumping immediately realises the
# venue price gap at the worst possible moment.
STOP_SPOT_GRACE_SECONDS = float(os.environ.get("STOP_SPOT_GRACE_SECONDS", "120"))
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
ADVISOR_MODEL = os.environ.get("ADVISOR_MODEL", "claude-opus-4-8")
ADVISOR_MAX_TOKENS = int(os.environ.get("ADVISOR_MAX_TOKENS", "1200"))
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
