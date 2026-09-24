"""Main engine process: market data loop, screener, command poller, executor
tasks and safety stops. Controlled via the commands table written by the
Telegram bot (control_bot.py).
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
from decimal import Decimal, InvalidOperation

import aiohttp

import config
import database
import equity
import funding
import intents
import position_manager as pm
import book
import recon
import recovery
import screener
from auth import (
    load_aster_credentials,
    load_env,
    load_mexc_credentials,
)
from database import (
    abandon_running_commands,
    claim_command,
    journal,
    pending_commands,
    prune_old_rows,
    resolve_command,
)
from exchange_client import AsterClient, ExchangeError, MexcClient
from executor import Executor, LiveTrader, MarketData, PaperTrader
from notify import Notifier

log = logging.getLogger("live_monitor")


def _dec_or_zero(value) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


class Engine:
    def __init__(self, session: aiohttp.ClientSession):
        self.paper = config.paper_mode()
        self.session = session
        self.conn = database.init_db()
        self.md = MarketData()
        self._last_basis_log = 0.0
        # -inf so the first slow scan samples immediately: time.monotonic() is
        # small early in a process's life, so a 0.0 sentinel would suppress the
        # first sample for a whole interval after every restart.
        self._last_equity = float("-inf")
        self.notifier = Notifier(session)
        self.positions = pm.PositionManager(self.conn)

        aster_creds = mexc_creds = None
        try:
            aster_creds = load_aster_credentials()
        except RuntimeError as exc:
            if not self.paper:
                raise
            log.warning("paper mode without Aster creds: %s", exc)
        try:
            mexc_creds = load_mexc_credentials()
        except RuntimeError as exc:
            if not self.paper:
                raise
            log.warning("paper mode without MEXC creds: %s", exc)

        self.aster = AsterClient(session, aster_creds)
        self.mexc = MexcClient(session, mexc_creds)
        trader = (
            PaperTrader(self.md)
            if self.paper
            else LiveTrader(self.aster, self.mexc, self.conn)
        )
        self.executor = Executor(
            self.md, trader, self.positions, self.notifier, self.conn,
            paper=self.paper,
        )
        # Position ids whose passive exit was started by the convergence auto-
        # close (not the operator): these may escalate to a taker-taker close
        # or stand back down to OPEN. Operator /exit passive is never in here,
        # so manual passive exits keep their no-auto-escalation guarantee.
        self._auto_passive: set[int] = set()
        # Rolling per-symbol basis history for the /screen 5-minute averages.
        self._basis_avg = screener.RollingBasis(config.SCREEN_AVG_WINDOW_SECONDS)
        # 24h mean entry basis per symbol, so /screen can show whether the
        # current level is elevated or just this pair's normal richness.
        self._basis_24h = screener.DailyBasis()
        # A second, longer window. Hourly aggregates only, so a few hundred
        # floats per symbol — the cost is nil and it answers a question the
        # 24h band cannot: whether TODAY is the anomaly.
        self._basis_base = screener.DailyBasis(hours=config.BASELINE_HOURS)
        # Aster positionRisk cached by symbol (mark + liquidation price) for the
        # /positions liq readout. Refreshed each slow scan in live mode.
        self._position_risk: dict[str, dict] = {}
        # Throttle for the near-liquidation alert: position id -> last-alert
        # monotonic time. Popped when the position recovers above the threshold.
        self._liq_alerted: dict[int, float] = {}
        # Perp qty each position's /stops orders were placed for, so the safety
        # loop can auto-refresh them after a resize. In-memory: lost on restart
        # (stops themselves survive; re-run /stops to re-arm auto-refresh).
        self._stops_qty: dict[int, Decimal] = {}
        # Last auto-stop placement attempt per position (throttles retries).
        self._auto_stops_attempt: dict[int, float] = {}
        # Consecutive sweeps the taker-close-is-profitable condition held,
        # per position: a single flickering quote must not cross both legs.
        self._tp_confirm: dict[int, int] = {}
        # Positions whose working order was stood down for liquidation
        # proximity. Latched so a manual /exit afterwards is honoured.
        self._liq_protect: set[int] = set()
        # Last DB-vs-MEXC spot check per position (throttle), and the first
        # time a deficit was seen (confirmation window).
        self._spot_check_at: dict[int, float] = {}
        self._spot_deficit_since: dict[int, float] = {}
        # Venue order ids of each position's /stops legs, so the hedge guard can
        # recognise its OWN stop firing (query the real fill) vs an ADL.
        self._stops_orders: dict[int, dict] = {}
        # Hedge-integrity guard: position id -> monotonic time the perp-leg
        # deficit was first observed (with fresh venue data). Cleared when the
        # legs match again; acted on after HEDGE_BREAK_CONFIRM_SECONDS.
        self._hedge_break: dict[int, float] = {}
        # Stop-fired grace: position id -> {"until", "mexc_id", "recorded",
        # "floor"} while the resting MEXC sell LIMIT is given time to fill at
        # the stop price before falling back to tranche market sells.
        self._stop_grace: dict[int, dict] = {}
        self._position_risk_ts = 0.0        # last SUCCESSFUL refresh (monotonic)
        self._position_risk_wall_ms = 0     # ...and its wall-clock time

    # ── startup ──

    async def start(self) -> None:
        mode = "paper" if self.paper else "LIVE"
        journal(self.conn, f"engine starting in {mode} mode")
        orphaned = abandon_running_commands(self.conn)
        if orphaned:
            journal(self.conn, f"abandoned {orphaned} command(s) left running by"
                    f" a prior crash", "WARN")
        if not self.paper:
            await self.mexc.sync_time()   # align the signing clock before trading
        await self._load_symbol_maps(initial=True)
        # Warm the 24h basis window from our own logs: the engine restarts on
        # every /update, and a reset window would make the figure useless.
        try:
            now_ms = int(time.time() * 1000)
            seeded = await asyncio.to_thread(
                screener.seed_daily_from_logs, self._basis_24h, now_ms,
            )
            base_seeded = await asyncio.to_thread(
                screener.seed_daily_from_logs, self._basis_base, now_ms,
                config.BASELINE_HOURS,
            )
            log.info("seeded basis windows: 24h=%d samples, %dh=%d samples",
                     seeded, config.BASELINE_HOURS, base_seeded)
        except Exception:
            log.exception("24h basis seed failed (continuing without history)")
        # Stops outlive the process: without their ids the hedge guard cannot
        # tell our own stop firing from an ADL, and dumps spot at market while
        # the sell LIMIT rests at the stop price (position 191).
        stops = database.load_stop_orders(self.conn)
        for pid, row in stops.items():
            self._stops_orders[pid] = {
                "aster_id": row["aster_id"], "mexc_id": row["mexc_id"],
            }
            self._stops_qty[pid] = Decimal(row["perp_qty"] or "0")
        if stops:
            log.info("recovered /stops orders for %d position(s)", len(stops))

        await recovery.reconcile(
            self.conn, self.positions, self.aster, self.mexc, self.notifier,
            paper=self.paper,
        )
        await self._refresh_books()
        await self._refresh_funding()
        await self._refresh_funding_stats()
        await self._resume_positions()
        await self._startup_hedge_check()
        try:
            await self._refresh_position_funding()
        except Exception:
            log.exception("startup position funding refresh failed")
        await self.notifier.alert(f"basis-trade engine started ({mode})")
        await asyncio.gather(
            self._market_loop(),
            self._command_loop(),
            self._safety_loop(),
            self._funding_loop(),
        )

    async def _load_symbol_maps(self, *, initial: bool = False) -> tuple[list[str], list[str]]:
        """(Re)build the cross-listed universe from both venues' exchangeInfo.
        Returns (added, removed) canonical symbols vs the previous universe. On
        a periodic refresh a fetch failure leaves the existing maps untouched;
        at startup it raises (the engine must have a universe to run)."""
        aster_info, mexc_info = await asyncio.gather(
            self.aster.exchange_info(), self.mexc.exchange_info(),
            return_exceptions=True,
        )
        if not isinstance(aster_info, dict) or not isinstance(mexc_info, dict):
            msg = (f"symbol map refresh failed (aster={aster_info!r:.80},"
                   f" mexc={mexc_info!r:.80})")
            if initial:
                raise RuntimeError(msg)
            log.warning(msg)
            return [], []
        new_maps = screener.build_pair_maps(set(aster_info), set(mexc_info))
        before = set(self.md.pair_maps)
        added = sorted(set(new_maps) - before)
        removed = sorted(before - set(new_maps))
        self.md.aster_info = aster_info
        self.md.mexc_info = mexc_info
        self.md.pair_maps = new_maps
        log.info(
            "symbol maps: %d aster, %d mexc, %d tradeable pairs",
            len(aster_info), len(mexc_info), len(new_maps),
        )
        if initial:
            journal(self.conn, f"{len(new_maps)} cross-listed USDT pairs")
        elif added or removed:
            journal(
                self.conn,
                f"symbol universe changed: +{len(added)} -{len(removed)}"
                f" (now {len(new_maps)})",
            )
        return added, removed

    async def _resume_positions(self) -> None:
        for pos in self.positions.active():
            if pos.state == pm.EXITING:
                log.info("resuming exit for position %s", pos.id)
                # A leftover /stops (or stop-grace) MEXC sell LIMIT would lock
                # the spot balance the resumed exit needs to sell.
                await self._cancel_stops_for(pos)
                await self.executor.start_exit(pos)
            elif pos.state == pm.UNWINDING:
                journal(
                    self.conn,
                    f"position {pos.id} stuck UNWINDING at startup — manual check",
                    "ERROR",
                )

    async def _startup_hedge_check(self) -> None:
        """Catch a hedge that broke while the engine was DOWN (e.g. an
        overnight ADL): fetch venue risk now and run the integrity check once,
        so the warning fires within seconds of boot and the sell-down follows
        one confirmation window later — instead of waiting for the loops to
        notice. The 30s confirmation still applies (the safety loop re-checks
        against further fresh snapshots before acting)."""
        if self.paper:
            return
        await self._refresh_position_risk()
        for pos in self.positions.active():
            try:
                await self._check_hedge_integrity(pos)
            except Exception:
                log.exception("startup hedge check failed for position %s", pos.id)

    # ── loops ──

    async def _market_loop(self) -> None:
        last_slow = 0.0
        last_symbol_refresh = time.monotonic()  # startup already loaded the universe
        while True:
            try:
                await self._refresh_books()
                now_mono = time.monotonic()
                if now_mono - last_slow >= config.SLOW_SCAN_SECONDS:
                    last_slow = now_mono
                    await self._slow_scan()
                if now_mono - last_symbol_refresh >= config.SYMBOL_REFRESH_SECONDS:
                    last_symbol_refresh = now_mono
                    added, _removed = await self._load_symbol_maps()
                    if added:
                        names = ", ".join(s[:-4] for s in added[:20])
                        more = " …" if len(added) > 20 else ""
                        await self.notifier.alert(
                            f"🆕 {len(added)} new cross-listed pair(s) now"
                            f" tradeable: {names}{more}"
                        )
            except Exception:
                log.exception("market loop error")
            await asyncio.sleep(config.POLL_INTERVAL_SECONDS)

    async def _slow_scan(self) -> None:
        """Refresh funding + write the three snapshot files. Each step is
        isolated so a failure in one (e.g. a data-dependent throw in the
        funding snapshot) can't cascade and silently freeze the others — the
        heartbeat in particular must keep writing so /positions marks and
        /status stay live. Failures are logged individually to pinpoint them.
        (A shared try once let a broken funding write freeze the heartbeat and
        funding snapshot for days while /screen stayed fresh.)"""
        try:
            await self._refresh_funding()
        except Exception:
            log.exception("slow scan: funding rate refresh failed")
        try:
            await self._refresh_position_risk()
        except Exception:
            log.exception("slow scan: position risk refresh failed")
        try:
            await self._sample_equity()
        except Exception:
            log.exception("slow scan: equity snapshot failed")
        for label, fn in (
            ("screener snapshot", self._write_screener_snapshot),
            ("funding snapshot", self._write_funding_snapshot),
            ("heartbeat", self._write_heartbeat),
        ):
            try:
                fn()
            except Exception:
                log.exception("slow scan: %s write failed", label)

    async def _sample_equity(self) -> None:
        """Record total account value on its own cadence inside the slow scan.

        Live only: paper has no venue balances, and a zero row would poison the
        history with a cliff the day the mode was switched.
        """
        if self.paper:
            return
        now = time.monotonic()
        if now - self._last_equity < config.EQUITY_SNAPSHOT_MINUTES * 60:
            return
        self._last_equity = now
        eq = await equity.snapshot(self.aster, self.mexc, self.md.mexc_books)
        if eq.errors:
            # A venue that failed contributes 0, which would read as a crash in
            # account value. Skip the sample rather than record a lie.
            log.warning("equity snapshot incomplete, not stored: %s", eq.errors)
            return
        database.record_equity(
            self.conn, int(time.time() * 1000), eq.aster_usd,
            eq.spot_coins_usd, eq.spot_usdt_usd, eq.total_usd,
        )

    async def _refresh_books(self) -> None:
        aster_books, mexc_books = await asyncio.gather(
            self.aster.book_tickers(), self.mexc.book_tickers(),
            return_exceptions=True,
        )
        if isinstance(aster_books, dict):
            self.md.aster_books = aster_books
        else:
            log.warning("aster book refresh failed: %r", aster_books)
        if isinstance(mexc_books, dict):
            self.md.mexc_books = mexc_books
        else:
            log.warning("mexc book refresh failed: %r", mexc_books)

    async def _refresh_funding(self) -> None:
        try:
            self.md.funding = await self.aster.premium_index()
        except ExchangeError:
            log.exception("funding refresh failed")

    async def _refresh_position_risk(self) -> None:
        """Cache Aster positionRisk by symbol (mark + liquidation price) so
        /positions can show how close each perp short is to liquidation. Live
        mode only — paper has no venue positions."""
        if self.paper:
            return
        try:
            risk = await self.aster.position_risk()
        except ExchangeError:
            log.exception("position risk refresh failed")
            return
        self._position_risk = {
            r["symbol"]: r for r in risk if r.get("symbol")
        }
        # Freshness markers: the hedge-integrity guard only trusts (and counts
        # confirmation time against) a recently-SUCCESSFUL snapshot. The wall
        # clock is used to check the snapshot postdates a position's opening —
        # a freshly-entered perp lags the ~15s risk poll and would otherwise
        # look like an ADL (the CASHCAT false positive).
        self._position_risk_ts = time.monotonic()
        self._position_risk_wall_ms = int(time.time() * 1000)

    async def _funding_loop(self) -> None:
        while True:
            await asyncio.sleep(config.FUNDING_REFRESH_SECONDS)
            if not self.paper:
                try:
                    await self.mexc.sync_time()   # keep the signing clock aligned
                except Exception:
                    log.exception("mexc time re-sync failed")
            try:
                await self._refresh_funding_stats()
            except Exception:
                log.exception("funding stats sweep error")
            try:
                await self._refresh_position_funding()
            except Exception:
                log.exception("position funding refresh error")
            try:
                prune_old_rows(self.conn)
            except Exception:
                log.exception("db prune error")

    async def _refresh_position_funding(self) -> None:
        """Set each open LIVE position's funding_usd to the actual FUNDING_FEE
        income Aster has paid over the trade's life. Without this the live mark
        falls back to extrapolating today's rate across the whole hold, which
        badly distorts long-held / adopted positions."""
        if self.paper:
            return
        now = int(time.time() * 1000)
        for pos in self.positions.active():
            if pos.state != pm.OPEN or pos.paper or pos.opened_ms is None:
                continue
            pair = self.md.pair_maps.get(pos.symbol)
            if pair is None:
                continue
            try:
                rows = await self.aster.income_history(
                    pair.aster_symbol, "FUNDING_FEE", pos.opened_ms, now
                )
            except ExchangeError:
                log.exception("funding income fetch failed for %s", pos.symbol)
                continue
            total = sum(
                (Decimal(str(r.get("income", "0"))) for r in rows), Decimal(0)
            )
            self.positions.set_funding(pos.id, total)

    async def _refresh_funding_stats(self) -> None:
        """Sweep funding-rate history for every pair in rate-limited batches,
        derive each symbol's interval and 24h-average carry, write a snapshot."""
        symbols = list(self.md.pair_maps)
        now = int(time.time() * 1000)
        batch = max(1, config.FUNDING_FETCH_BATCH)
        for i in range(0, len(symbols), batch):
            chunk = symbols[i : i + batch]
            histories = await asyncio.gather(
                *(self.aster.funding_rate_history(s, config.FUNDING_HISTORY_LIMIT)
                  for s in chunk),
                return_exceptions=True,
            )
            for sym, hist in zip(chunk, histories):
                if isinstance(hist, Exception):
                    continue
                current = (self.md.funding.get(sym) or {}).get("funding_rate")
                self.md.funding_stats[sym] = funding.summarize(
                    sym, hist, current, now_ms=now
                )
            await asyncio.sleep(0.25)
        log.info("funding stats refreshed for %d symbols", len(self.md.funding_stats))
        # 24h perp volume in the same sweep: one call for the whole universe,
        # and it moves slowly enough that the funding cadence is plenty.
        try:
            self.md.perp_volume = await self.aster.ticker_24hr()
            log.info("perp 24h volume for %d symbols", len(self.md.perp_volume))
        except Exception:
            log.exception("perp 24h volume refresh failed (keeping previous)")

    def _write_screener_snapshot(self) -> None:
        now = int(time.time() * 1000)
        rows = []
        for sym, pair in self.md.pair_maps.items():
            aster = self.md.aster_books.get(pair.aster_symbol)
            mexc = self.md.mexc_books.get(pair.mexc_symbol)
            if aster is None or mexc is None:
                continue
            fund_row = self.md.funding.get(pair.aster_symbol) or {}
            funding_rate = fund_row.get("funding_rate")
            stat = self.md.funding_stats.get(pair.aster_symbol)
            interval = stat.interval_hours if stat else 8
            row = screener.compute_row(
                pair, aster, mexc, funding_rate, now_ms=now,
                funding_interval_hours=interval,
                index_price=fund_row.get("index_price"),
            )
            if row is not None:
                self._basis_avg.add(sym, now, row.entry_bps, row.net_edge_bps)
                self._basis_avg.annotate(row)
                self._basis_24h.add(sym, now, row.entry_bps, row.close_bps)
                self._basis_base.add(sym, now, row.entry_bps, row.close_bps)
                self._basis_24h.annotate(row)
                vol = self.md.perp_volume.get(pair.aster_symbol) or {}
                row.perp_volume_24h = float(vol.get("quote_volume", 0) or 0)
                row.perp_trades_24h = float(vol.get("trades", 0) or 0)
                row.hours_tradeable_24h = self._basis_24h.hours_above(
                    sym, float(config.ENTRY_MIN_EDGE_FLOOR_BPS)
                )
                rows.append(row)
        screener.write_snapshot(
            screener.rank_rows(rows),
            screener.rank_rows_by_dislocation(rows),
            screener.rank_rows_by_swing(rows),
            screener.rank_rows_by_fillability(rows),
        )
        self._log_basis_rows(rows)

    def _log_basis_rows(self, rows: list[screener.ScreenerRow]) -> None:
        """Append executable touch-basis rows for ALL pairs to a daily CSV so
        convergence can be analysed on real quotes (scripts/analyze_basis_log.py),
        not last-trade candle prints."""
        now = time.time()
        if now - self._last_basis_log < config.BASIS_LOG_SECONDS:
            return
        self._last_basis_log = now
        day = time.strftime("%Y%m%d", time.gmtime(now))
        path = config.OUTPUT_DIR / f"basis_log_{day}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        # perp_trades_24h was appended to this format later: depth says the
        # book is not empty, but what LIFTS a resting maker is trades, and
        # without it a replay can rebuild every screen except the fill factor.
        # A file started before the upgrade keeps its own header for the rest
        # of the day — appending a wider row under a narrower header would
        # leave a ragged CSV. Readers index by header name, so both widths
        # load; the new column simply begins with tomorrow's file.
        wide = True
        if path.exists():
            try:
                with open(path, newline="") as f:
                    header = next(csv.reader(f), [])
                wide = "perp_trades_24h" in header
            except OSError:
                log.exception("basis log header read failed")
                return
        try:
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if not path.stat().st_size:
                    w.writerow([
                        "ts_ms", "symbol", "entry_bps", "close_bps",
                        "funding_8h_bps", "max_notional_usd", "perp_trades_24h",
                    ])
                for r in rows:
                    row = [
                        r.ts_ms, r.symbol, f"{r.entry_bps:.2f}",
                        f"{r.close_bps:.2f}", f"{r.funding_8h_bps:.2f}",
                        f"{r.max_notional_usd:.0f}",
                    ]
                    if wide:
                        row.append(f"{r.perp_trades_24h:.0f}")
                    w.writerow(row)
        except OSError:
            log.exception("basis log write failed")

    def _write_funding_snapshot(self) -> None:
        """Persist funding stats joined with live basis/depth, ranked by 24h
        average carry, for the bot's /funding command."""
        now = int(time.time() * 1000)
        rows = []
        hidden: dict[str, int] = {}
        for sym, stat in self.md.funding_stats.items():
            pair = self.md.pair_maps.get(sym)
            if pair is None:
                continue
            aster = self.md.aster_books.get(pair.aster_symbol)
            mexc = self.md.mexc_books.get(pair.mexc_symbol)
            fund_row = self.md.funding.get(sym) or {}
            live_rate = fund_row.get("funding_rate")
            screen = None
            if aster is not None and mexc is not None:
                screen = screener.compute_row(
                    pair, aster, mexc, live_rate,
                    now_ms=now, funding_interval_hours=stat.interval_hours,
                    index_price=fund_row.get("index_price"),
                )
                # compute_row returns None on a stale / zero-priced book (common
                # for thin microcaps); only annotate a real row. The screener
                # snapshot (run just before this) already added this scan's
                # sample, so annotate is read-only here for the 5m means.
                if screen is not None:
                    self._basis_avg.annotate(screen)
                    self._basis_24h.annotate(screen)
                    # The flow figures are set on the /screen path only; the
                    # quality gate below needs them here too.
                    vol = self.md.perp_volume.get(pair.aster_symbol) or {}
                    screen.perp_volume_24h = float(vol.get("quote_volume", 0) or 0)
                    screen.perp_trades_24h = float(vol.get("trades", 0) or 0)
            # Recompute the current rate from the 15s-fresh premiumIndex rather
            # than the 15-min stats sweep, so a new funding settlement shows up
            # promptly. (lastFundingRate only changes at each settlement, so
            # this holds steady between them by design — not a stale snapshot.)
            current_8h = stat.current_8h_bps
            if live_rate is not None and stat.interval_hours:
                current_8h = float(
                    live_rate * Decimal(10000) * Decimal(8) / Decimal(stat.interval_hours)
                )
            next_ms = fund_row.get("next_funding_time")
            next_funding_h = (
                float((next_ms - now) / Decimal(3_600_000))
                if next_ms and next_ms > 0 else None
            )
            # Ranked by carry, but a carry you cannot actually enter is not a
            # candidate — and the thin books this screen reaches produce the
            # worst quote errors on the whole system. Hide them, and count
            # them so /funding can say the screen is filtering rather than
            # leaving the user wondering where a row went.
            reject = screener.quote_reject_reason(screen) if screen else "book"
            if not reject:
                entry_now = (
                    screen.entry_bps_avg if screen.samples
                    else screen.entry_bps
                )
                # Gate on the figure the board SHOWS, so a row cannot be
                # hidden for a number the reader cannot see.
                if entry_now < config.FUNDING_MIN_ENTRY_BPS:
                    reject = "discount"
            if reject:
                hidden[reject] = hidden.get(reject, 0) + 1
                continue
            rows.append({
                "symbol": sym,
                "interval_hours": stat.interval_hours,
                "current_8h_bps": current_8h,
                "avg_24h_8h_bps": stat.avg_24h_8h_bps,
                "realized_24h_bps": stat.realized_24h_bps,
                "next_funding_h": next_funding_h,
                # `screen` is non-None past the gate above, so these no
                # longer need the None guards they carried when an empty book
                # still made the board.
                "entry_bps": screen.entry_bps,
                "close_bps": screen.close_bps,
                "spread_cost_bps": screen.spread_cost_bps,
                "net_edge_bps": screen.net_edge_bps,
                "entry_bps_avg": screen.entry_bps_avg,
                "net_edge_bps_avg": screen.net_edge_bps_avg,
                "samples": screen.samples,
                "max_notional_usd": screen.max_notional_usd,
                "entry_bps_jitter": screen.entry_bps_jitter,
                "perp_trades_24h": screen.perp_trades_24h,
                # 24h range of the hourly mean basis, so /funding can say
                # whether the live entry sits at the rich or the cheap end of
                # where this pair has actually traded today.
                "basis_p10_24h": screen.basis_p10_24h,
                "basis_p90_24h": screen.basis_p90_24h,
                "hours_24h": screen.hours_24h,
                # The board's one number: basis one-off + carry stream over a
                # fixed horizon. Sorting on this replaces ranking by raw carry,
                # which put a collapsed 61bps average on a $5 book on top.
                "score": screener.carry_score(screen, stat.avg_24h_8h_bps, current_8h),
            })
        rows.sort(key=lambda r: r["score"], reverse=True)
        config.FUNDING_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts_ms": now,
            "rows": rows[: config.SCREENER_TOP_N],
            "hidden": hidden,
        }
        tmp = config.FUNDING_SNAPSHOT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        tmp.replace(config.FUNDING_SNAPSHOT_FILE)

    def _position_marks(self) -> dict[str, dict]:
        """Live marks for open positions: closeable basis now, accrued-funding
        estimate and unrealized P&L at passive-exit touch prices."""
        now_ms = int(time.time() * 1000)
        marks: dict[str, dict] = {}
        # Every active position gets an entry: a full mark, or a {"skip": reason}
        # so /positions can explain WHY a live mark is missing instead of a bare
        # "no live mark".
        for pos in self.positions.active():
            pid = str(pos.id)
            if pos.perp_qty <= 0 or pos.perp_entry_avg is None:
                marks[pid] = {"skip": "no recorded entry price"}
                continue
            pair = self.md.pair_maps.get(pos.symbol)
            if pair is None:
                marks[pid] = {"skip": "symbol not cross-listed now (try /refresh)"}
                continue
            aster = self.md.aster_books.get(pair.aster_symbol)
            mexc = self.md.mexc_books.get(pair.mexc_symbol)
            if aster is None or mexc is None:
                venue = "Aster" if aster is None else "MEXC"
                marks[pid] = {"skip": f"no live {venue} quote"}
                continue
            if aster.bid <= 0 or mexc.bid <= 0:
                venue = "Aster perp" if aster.bid <= 0 else "MEXC spot"
                marks[pid] = {"skip": f"no {venue} bid (thin book)"}
                continue
            # Basis closeable right now: maker perp buy-back at the bid vs
            # spot sell at the bid (same definition as the screener's close).
            close_bps = (
                (aster.bid / pair.qty_multiplier - mexc.bid) / mexc.bid
                * Decimal(10000)
            )
            # Short perp marked at the bid, long spot at the bid.
            perp_pnl = (pos.perp_entry_avg - aster.bid) * pos.perp_qty
            spot_pnl = (
                (mexc.bid - pos.spot_entry_avg) * pos.spot_qty
                if pos.spot_entry_avg is not None else Decimal(0)
            )
            # Funding accrued so far. LIVE: the real FUNDING_FEE income, kept
            # current by _refresh_position_funding (never extrapolate today's
            # rate across a multi-day hold — that distorts the P&L). PAPER:
            # estimate from the current rate at the symbol's interval.
            funding_est = pos.funding_usd
            if (pos.paper and funding_est == 0 and pos.opened_ms
                    and pos.spot_entry_avg is not None):
                rate = (self.md.funding.get(pair.aster_symbol) or {}).get("funding_rate")
                if rate is not None:
                    stat = self.md.funding_stats.get(pair.aster_symbol)
                    interval = (
                        Decimal(stat.interval_hours) if stat is not None
                        else Decimal(config.FUNDING_INTERVAL_HOURS)
                    )
                    periods = (
                        Decimal(now_ms - pos.opened_ms) / Decimal(3_600_000) / interval
                    )
                    notional = pos.perp_qty * pos.spot_entry_avg * pair.qty_multiplier
                    funding_est = rate * notional * periods
            upnl = perp_pnl + spot_pnl + funding_est - pos.fees_usd
            mark = {
                "close_bps": float(close_bps),
                "funding_usd": float(funding_est),
                "upnl_usd": float(upnl),
                # Current USD size of the perp leg (closeable value at the bid).
                "notional_usd": float(pos.perp_qty * aster.bid),
            }
            # Liquidation proximity for the short perp (live mode only).
            liq_stats = self._liq_stats(pos)
            if liq_stats is not None:
                mark["liq_price"], mark["liq_dist_pct"] = liq_stats
            marks[str(pos.id)] = mark
        return marks

    def _liq_stats(self, pos: pm.Position) -> tuple[float, float] | None:
        """(liquidation_price, distance_pct) for a position's short perp, or
        None if there's no cached risk/mark. Distance is the % the mark must
        rise to hit liquidation (liq is above the mark for a short)."""
        pair = self.md.pair_maps.get(pos.symbol)
        if pair is None:
            return None
        risk = self._position_risk.get(pair.aster_symbol)
        if not risk:
            return None
        liq = _dec_or_zero(risk.get("liquidationPrice"))
        mark_px = _dec_or_zero(risk.get("markPrice"))
        if mark_px <= 0:
            book = self.md.aster_books.get(pair.aster_symbol)
            if book is not None and book.bid > 0:
                mark_px = book.bid
        if liq <= 0 or mark_px <= 0:
            return None
        return float(liq), float((liq - mark_px) / mark_px * Decimal(100))

    async def _check_hedge_integrity(self, pos: pm.Position) -> bool:
        """Detect the perp leg being closed/reduced ON THE VENUE with no order
        of ours (ADL, liquidation, manual close) — which leaves the spot leg
        naked — and rebalance. Returns True when it took over the position (the
        caller must then skip the basis safety checks this sweep).

        Guards against acting on bad data: requires a recent SUCCESSFUL
        positionRisk refresh, and the deficit must persist continuously for
        HEDGE_BREAK_CONFIRM_SECONDS before anything is traded."""
        if self.paper or pos.paper or pos.state != pm.OPEN:
            return False
        if pos.perp_qty <= 0 or pos.spot_qty <= 0:
            return False
        if self.executor.has_task(pos.id):
            # An entry/add is working — fills are still being recorded, so a
            # transient DB-vs-venue gap is expected. Don't accumulate.
            self._hedge_break.pop(pos.id, None)
            return False
        pair = self.md.pair_maps.get(pos.symbol)
        info = self.md.aster_info.get(pair.aster_symbol) if pair else None
        if pair is None or info is None:
            return False
        now = time.monotonic()
        if now - self._position_risk_ts > config.HEDGE_BREAK_RISK_FRESH_SECONDS:
            return False   # can't trust the venue snapshot; never act on stale
        # The snapshot must postdate the position opening: a just-entered perp
        # lags the ~15s risk poll, so an older snapshot showing 0 is cache lag,
        # not an ADL (the CASHCAT false positive — enter then "perp at 0").
        if pos.opened_ms is None or self._position_risk_wall_ms < pos.opened_ms:
            self._hedge_break.pop(pos.id, None)
            return False
        row = self._position_risk.get(pair.aster_symbol)
        venue_perp = abs(_dec_or_zero(row.get("positionAmt"))) if row else Decimal(0)
        tolerance = max(
            info.step_size,
            pos.perp_qty * config.HEDGE_BREAK_TOLERANCE_PCT / Decimal(100),
        )
        deficit = pos.perp_qty - venue_perp
        if deficit <= tolerance:
            if self._hedge_break.pop(pos.id, None) is not None:
                journal(self.conn, f"position {pos.id}: perp leg matches venue"
                        f" again — hedge-break timer reset")
            return False

        first = self._hedge_break.get(pos.id)
        if first is None:
            self._hedge_break[pos.id] = now
            journal(self.conn, f"position {pos.id}: VENUE PERP DEFICIT"
                    f" db={pos.perp_qty} venue={venue_perp}", "ERROR")
            await self.notifier.alert(
                f"⚠️ position {pos.id} {pos.symbol}: Aster shows the perp short"
                f" at {venue_perp} but we hold {pos.perp_qty} — possible ADL /"
                f" liquidation / manual close. Confirming for"
                f" {config.HEDGE_BREAK_CONFIRM_SECONDS:.0f}s before rebalancing"
                f" the spot leg."
            )
            # Take over the position for the whole confirmation window: return
            # True so the caller SKIPS the basis auto-closes. Otherwise the
            # convergence TP / adverse stop can fire on the (now nonsensical)
            # basis and close the position out from under the guard — pricing a
            # perp buy-back that can't happen because the perp is already gone.
            return True
        if now - first < config.HEDGE_BREAK_CONFIRM_SECONDS:
            return True

        # Confirmed on fresh data for the full window: act.
        self._hedge_break.pop(pos.id, None)
        mark = _dec_or_zero(row.get("markPrice")) if row else Decimal(0)
        if mark <= 0:
            book = self.md.aster_books.get(pair.aster_symbol)
            mark = book.bid if book and book.bid > 0 else pos.perp_entry_avg or Decimal(0)

        # Was this OUR OWN /stops STOP_MARKET firing rather than an ADL? If the
        # stop order executed, reconcile the perp at its REAL fill price and
        # treat the resting MEXC sell LIMIT as the preferred spot exit.
        stop_ids = dict(self._stops_orders.get(pos.id) or {})
        if not stop_ids.get("mexc_id"):
            # Last-ditch recovery: the spot twin of a stop is a resting sell
            # LIMIT, so it is still OPEN and findable by its client-id prefix
            # even when we have lost the id. Worth the extra call — without it
            # this path cancels a good limit at the stop price and dumps the
            # spot at market instead.
            try:
                for o in await self.mexc.open_orders(pair.mexc_symbol):
                    if o.client_order_id.startswith(f"sp_stop_{pos.id}_"):
                        stop_ids["mexc_id"] = o.order_id
                        journal(self.conn, f"position {pos.id}: recovered spot"
                                f" stop-limit {o.order_id} by client id")
                        break
            except ExchangeError:
                log.exception("spot stop recovery failed for position %s", pos.id)
        stop_order = None
        if stop_ids.get("aster_id"):
            try:
                o = await self.aster.get_order(
                    pair.aster_symbol, stop_ids["aster_id"]
                )
                if o.executed_qty > 0:
                    stop_order = o
            except ExchangeError:
                log.exception("stop-order lookup failed for position %s", pos.id)

        if stop_order is not None:
            fill_qty = min(deficit, stop_order.executed_qty)
            price = stop_order.avg_price if stop_order.avg_price > 0 else mark
            self.positions.record_fill(
                pos.id, "aster", "exit", "BUY", fill_qty, price, Decimal(0),
                order_id=stop_ids["aster_id"],
            )
            if deficit > fill_qty:   # stop covered part; rest was external
                self.positions.record_fill(
                    pos.id, "aster", "exit", "BUY", deficit - fill_qty, mark,
                    Decimal(0), order_id="ADL",
                )
            journal(self.conn, f"position {pos.id}: liq-protection STOP FIRED,"
                    f" {fill_qty} @ {price} (venue {venue_perp} remains)", "ERROR")
        else:
            # Genuine ADL/liquidation/manual: book at mark — the true close
            # price lives on the venue (check trade history / /recon).
            self.positions.record_fill(
                pos.id, "aster", "exit", "BUY", deficit, mark, Decimal(0),
                order_id="ADL",
            )
            journal(self.conn, f"position {pos.id}: HEDGE BROKEN — reconciled"
                    f" {deficit} perp @ ~{mark} (venue {venue_perp} remains)",
                    "ERROR")

        # Record whatever the MEXC stop-limit already sold (its fills were
        # otherwise invisible to the DB — the sell-down would then try to sell
        # spot we no longer hold).
        mexc_order = None
        if stop_ids.get("mexc_id"):
            try:
                mexc_order = await self.mexc.get_order(
                    pair.mexc_symbol, stop_ids["mexc_id"]
                )
            except ExchangeError:
                log.exception("stop-limit lookup failed for position %s", pos.id)
            if mexc_order is not None and mexc_order.executed_qty > 0:
                self.positions.record_fill(
                    pos.id, "mexc", "exit", "SELL", mexc_order.executed_qty,
                    mexc_order.avg_price, Decimal(0),
                    order_id=stop_ids["mexc_id"],
                )

        floor = venue_perp if venue_perp > tolerance else Decimal(0)
        self.positions.set_exit_request(
            pos.id, "now", None, floor if floor > 0 else None
        )
        self._stops_qty.pop(pos.id, None)
        self._stops_orders.pop(pos.id, None)
        database.clear_stop_orders(self.conn, pos.id)

        mexc_info = self.md.mexc_info.get(pair.mexc_symbol)
        fresh = self.positions.get(pos.id)
        floor_spot = (
            mexc_info.round_qty(floor * pair.qty_multiplier)
            if mexc_info else floor * pair.qty_multiplier
        )
        excess = fresh.spot_qty - floor_spot
        if mexc_info and mexc_info.round_qty(excess) <= 0:
            # The stop-limit already sold everything needed: just finish.
            await self.notifier.alert(
                f"✅ position {pos.id} {pos.symbol}: perp stop fired and the"
                f" MEXC stop-limit already covered the spot — closing out"
            )
            await self.executor._complete_exit(pos.id, floor)
            return True

        # The spot decision turns ONLY on whether a sell LIMIT is resting at
        # the stop price — not on whether we managed to identify the Aster
        # stop, which affects the P&L booking above and nothing else. An ADL
        # that happens to leave our limit resting is served just as well by
        # trying it for the grace window before dumping at market.
        if mexc_order is not None and mexc_order.is_open:
            # Our stop fired and its spot twin is still resting AT THE CHOSEN
            # STOP PRICE. Don't cancel it and market-dump — give it a grace
            # window to fill at that price (MEXC often lags Aster's mark by
            # seconds). Tracked by _check_stop_grace in the safety loop.
            self._stop_grace[pos.id] = {
                "until": time.monotonic() + config.STOP_SPOT_GRACE_SECONDS,
                "mexc_id": stop_ids["mexc_id"],
                "recorded": mexc_order.executed_qty,
                "floor": floor,
            }
            self.positions.set_state(pos.id, pm.EXITING)
            how = (
                f"liq-protection STOP FIRED on Aster (perp closed @"
                f" ~{stop_order.avg_price})" if stop_order is not None
                else f"perp leg reduced on Aster by {deficit}"
            )
            await self.notifier.alert(
                f"🛑 position {pos.id} {pos.symbol}: {how}. The MEXC sell LIMIT"
                f" is still resting at the stop price — giving it"
                f" {config.STOP_SPOT_GRACE_SECONDS:.0f}s to fill there before"
                f" falling back to tranche market sells."
            )
            return True

        # ADL path (or no usable resting spot order): cancel any remnants and
        # tranche-sell the excess at market.
        await self._cancel_stops_for(pos)   # the spot sell LIMIT locks balance
        await self.notifier.alert(
            f"🚨 position {pos.id} {pos.symbol}: perp leg reduced on venue by"
            f" {deficit} — now selling the unhedged spot in"
            f" {float(config.ADL_SELL_TRANCHE_PCT):.0f}% tranches every"
            f" {config.ADL_SELL_INTERVAL_SECONDS:.0f}s. Verify the real close"
            f" price on Aster; /recon is venue-truth."
        )
        await self.executor.start_spot_rebalance(
            self.positions.get(pos.id), floor
        )
        return True

    async def _check_stop_grace(self, pos: pm.Position) -> None:
        """Manage a stop-fired position while its MEXC sell LIMIT rests at the
        stop price: record fill increments, finish when it completes, and fall
        back to tranche market sells if the grace window expires unfilled."""
        info = self._stop_grace.get(pos.id)
        if info is None:
            return
        pair = self.md.pair_maps.get(pos.symbol)
        mexc_info = self.md.mexc_info.get(pair.mexc_symbol) if pair else None
        if pair is None or mexc_info is None:
            return
        try:
            order = await self.mexc.get_order(pair.mexc_symbol, info["mexc_id"])
        except ExchangeError:
            log.exception("stop-grace lookup failed for position %s", pos.id)
            return   # transient; try next sweep (grace clock keeps running)
        delta = order.executed_qty - info["recorded"]
        if delta > 0:
            self.positions.record_fill(
                pos.id, "mexc", "exit", "SELL", delta, order.avg_price,
                Decimal(0), order_id=info["mexc_id"],
            )
            info["recorded"] = order.executed_qty
        fresh = self.positions.get(pos.id)
        floor = info["floor"]
        floor_spot = mexc_info.round_qty(floor * pair.qty_multiplier)
        if mexc_info.round_qty(fresh.spot_qty - floor_spot) <= 0:
            self._stop_grace.pop(pos.id, None)
            await self.notifier.alert(
                f"✅ position {pos.id} {pos.symbol}: spot stop-limit filled at"
                f" the stop price — closing out"
            )
            await self.executor._complete_exit(pos.id, floor)
            return
        if time.monotonic() < info["until"] and order.is_open:
            return   # still resting at the stop price; keep waiting
        # Grace over (or the order is gone): cancel any remnant and fall back
        # to tranche market sells for what's left.
        self._stop_grace.pop(pos.id, None)
        if order.is_open:
            try:
                await self.mexc.cancel_order(pair.mexc_symbol, info["mexc_id"])
            except ExchangeError:
                log.exception("stop-grace cancel failed for position %s", pos.id)
        await self.notifier.alert(
            f"⏳ position {pos.id} {pos.symbol}: spot stop-limit didn't fill"
            f" within {config.STOP_SPOT_GRACE_SECONDS:.0f}s — selling the"
            f" remainder in tranches at market"
        )
        await self.executor.start_spot_rebalance(
            self.positions.get(pos.id), floor
        )

    async def _ensure_stops(self, pos: pm.Position) -> None:
        """Keep liquidation-protection orders in place for a live OPEN position.

        Idempotent, runs every safety sweep, and covers three cases:
          - no stops at all (a brand-new position, or one whose stops were
            cancelled to free the spot balance for a partial exit and never
            re-armed) -> place them;
          - size changed (a /enter size-up, or a completed part-reduce)
            -> re-place at the current size;
          - already correct -> no-op.

        Skipped while an entry/exit task is working (size still in flux), while
        an exit has been REQUESTED but not yet started, and while the stop-fire
        / ADL handlers own the position — so it never places a spot sell LIMIT
        that would lock balance those paths need."""
        if self.paper or not config.AUTO_STOPS:
            return
        if not self._stops_wanted(pos):
            return
        async with self._stop_lock(pos.id):
            # Re-read under the lock. The snapshot this sweep was handed can be
            # stale by the time the lock is free: /exit records its request and
            # cancels the stops while we wait, and placing from the old snapshot
            # is exactly the race that re-locked position 226's spot balance
            # straight after its exit began.
            pos = self.positions.get(pos.id)
            if not self._stops_wanted(pos):
                return
            await self._ensure_stops_locked(pos)

    def _stops_wanted(self, pos: pm.Position) -> bool:
        if pos.state != pm.OPEN or pos.perp_qty <= 0 or pos.spot_qty <= 0:
            return False
        # An exit that has been asked for but whose task has not started yet
        # is still an exit: the stops are about to be cancelled to free the
        # spot, and re-placing them would lock it for the whole close.
        if pos.exit_mode is not None:
            return False
        if self.executor.has_task(pos.id):
            return False
        if pos.id in self._stop_grace or pos.id in self._hedge_break:
            return False
        return True

    async def _ensure_stops_locked(self, pos: pm.Position) -> None:
        pair = self.md.pair_maps.get(pos.symbol)
        info = self.md.aster_info.get(pair.aster_symbol) if pair else None
        if info is None:
            return
        current = info.round_qty(pos.perp_qty)
        prior = self._stops_qty.get(pos.id)
        if prior == current:
            return
        # _place_stops can fail without recording (no liquidation price yet on a
        # just-opened position, venue error). Throttle so a persistent failure
        # doesn't retry — and alert — every sweep.
        now = time.monotonic()
        last = self._auto_stops_attempt.get(pos.id)
        # `is not None`, not a 0.0 default: monotonic() is small early in the
        # process's life, so a 0.0 default throttles the very FIRST attempt —
        # exactly when a position needs stops after an /update restart.
        if last is not None and now - last < config.AUTO_STOPS_RETRY_SECONDS:
            return
        self._auto_stops_attempt[pos.id] = now
        result = await self._place_stops(pos)   # updates self._stops_qty
        if prior is None:
            await self.notifier.alert(
                f"🛡 position {pos.id} {pos.symbol}: stops auto-placed\n{result}"
            )
        else:
            await self.notifier.alert(
                f"🔁 position {pos.id} {pos.symbol}: size changed {prior} -> {current}"
                f" — /stops auto-refreshed to cover the new size\n{result}"
            )

    async def _check_spot_integrity(self, pos: pm.Position) -> None:
        """Reconcile the DB's spot leg against the MEXC balance.

        The perp leg is checked against Aster positionRisk, but nothing ever
        checked the spot leg — so the DB could believe it holds coins that are
        not there. The main way that happens: an AMBIGUOUS spot sale, where the
        order may have filled but we deliberately do NOT record a fill (to avoid
        double-counting a sale that might not have happened). The alert asks for
        a manual reconcile; nothing did it. STONK #189 ended up with 366 in the
        DB against 4 on MEXC, and the exit then wedged trying to sell coins that
        did not exist.

        Only a venue balance BELOW the DB is acted on. A balance above it is
        expected and fine — the operator may hold the same coin outside the
        strategy, and that is none of our business.
        """
        if self.paper or pos.paper or pos.spot_qty <= 0:
            return
        if pos.state in (pm.PENDING_ENTRY, pm.ENTERING):
            return                      # spot still in flux mid-entry
        now = time.monotonic()
        if now - self._spot_check_at.get(pos.id, 0.0) < config.SPOT_CHECK_INTERVAL_SECONDS:
            return
        self._spot_check_at[pos.id] = now
        pair = self.md.pair_maps.get(pos.symbol)
        info = self.md.mexc_info.get(pair.mexc_symbol) if pair else None
        if pair is None or info is None:
            return
        try:
            account = await self.mexc.account()
        except ExchangeError:
            log.exception("spot integrity: MEXC account fetch failed")
            return
        venue = Decimal(0)
        for b in account.get("balances", []):
            if b.get("asset") == info.base_asset:
                venue = _dec_or_zero(b.get("free")) + _dec_or_zero(b.get("locked"))
                break
        deficit = pos.spot_qty - venue
        tolerance = max(
            info.step_size,
            pos.spot_qty * config.HEDGE_BREAK_TOLERANCE_PCT / Decimal(100),
        )
        if deficit <= tolerance:
            self._spot_deficit_since.pop(pos.id, None)
            return
        first = self._spot_deficit_since.get(pos.id)
        if first is None:
            self._spot_deficit_since[pos.id] = now
            journal(self.conn, f"position {pos.id}: SPOT DEFICIT db={pos.spot_qty}"
                    f" venue={venue} — confirming", "ERROR")
            return
        if now - first < config.HEDGE_BREAK_CONFIRM_SECONDS:
            return
        self._spot_deficit_since.pop(pos.id, None)
        # Confirmed: book the missing spot as sold at the current bid so the DB
        # matches reality and the position can finish closing.
        book = self.md.mexc_books.get(pair.mexc_symbol)
        price = book.bid if book and book.bid > 0 else (pos.spot_entry_avg or Decimal(0))
        qty = info.round_qty(deficit)
        if qty <= 0 or price <= 0:
            return
        self.positions.record_fill(
            pos.id, "mexc", "exit", "SELL", qty, price,
            Decimal(0),            # fee already taken on the venue, if it filled
        )
        journal(self.conn, f"position {pos.id}: spot reconciled to venue"
                f" (-{qty} @ {price})", "ERROR")
        await self.notifier.alert(
            f"🔧 position {pos.id} {pos.symbol}: DB held {pos.spot_qty} spot but"
            f" MEXC shows {venue} — booking the missing {qty} as sold @ {price}"
            f" so the position can close. Likely an AMBIGUOUS sale that did fill;"
            f" verify the real price in your MEXC trade history."
        )

    async def _stand_down_for_liq(self, pos: pm.Position, dist: float) -> None:
        """Near liquidation, a WAITING order is a liability.

        A passive exit has NO deadline, and while one runs the position's stops
        are cancelled (their spot sell LIMIT would lock the balance the exit
        needs to sell). A move overnight could liquidate the perp with nothing
        armed. So stand the waiting order down and let the auto-stops reconciler
        arm protection in its place.

        Deliberate choices:
          - an AGGRESSIVE exit is left running: it is actively closing the
            position, which removes the risk faster than stops would;
          - latched per excursion, so a manual /exit afterwards is honoured
            rather than cancelled again on the next sweep;
          - skipped when AUTO_STOPS is off, since cancelling the exit would
            then leave NO protection at all — strictly worse than waiting.
        """
        if self.paper or not config.AUTO_STOPS:
            return
        if pos.id in self._liq_protect:
            return                      # already stood down this excursion
        if not self.executor.has_task(pos.id):
            return                      # nothing waiting
        if pos.state == pm.EXITING and pos.exit_mode == "now":
            return                      # taker close in flight; let it finish
        self._liq_protect.add(pos.id)
        # Don't let the convergence TP immediately restart a passive close.
        self._auto_passive.discard(pos.id)
        what = "exit" if pos.state == pm.EXITING else "entry"
        if pos.state == pm.EXITING:
            await self._cancel_exit(pos)      # awaits task cleanup
        else:
            self.executor.request_cancel(pos.id)
        journal(
            self.conn,
            f"position {pos.id}: {dist:.1f}% from liq — working {what} stood"
            f" down, arming stops",
            "ERROR",
        )
        await self.notifier.alert(
            f"🛑 position {pos.id} {pos.symbol}: {dist:.1f}% from LIQUIDATION —"
            f" working {what} cancelled and stops armed. Add margin, or send"
            f" /exit {pos.id} now to close (a manual exit WILL be honoured)."
        )
        await self._ensure_stops(self.positions.get(pos.id))

    async def _check_liquidation(self, pos: pm.Position) -> None:
        """Alert (throttled) when a live perp short's mark is within
        LIQ_ALERT_PCT of its liquidation price. Re-arms once it recovers."""
        if pos.paper or pos.perp_qty <= 0:
            return
        stats = self._liq_stats(pos)
        if stats is None:
            return
        _liq, dist = stats
        if dist >= float(config.LIQ_ALERT_PCT):
            self._liq_alerted.pop(pos.id, None)  # recovered -> re-arm
            self._liq_protect.discard(pos.id)    # ...and the stand-down latch
            return
        # Protection must not wait on the ALERT throttle window, so stand any
        # waiting order down first.
        await self._stand_down_for_liq(pos, dist)
        now = time.monotonic()
        last = self._liq_alerted.get(pos.id)
        if last is not None and now - last < config.LIQ_ALERT_THROTTLE_SECONDS:
            return
        self._liq_alerted[pos.id] = now
        journal(
            self.conn,
            f"position {pos.id}: LIQ WARNING {dist:.1f}% from liquidation", "ERROR",
        )
        await self.notifier.alert(
            f"🚨 position {pos.id} {pos.symbol}: perp short is {dist:.1f}% from"
            f" LIQUIDATION (threshold {float(config.LIQ_ALERT_PCT):.0f}%) —"
            f" add margin or reduce the position"
        )

    def _write_heartbeat(self) -> None:
        config.HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        active = self.positions.active()
        payload = {
            "ts_ms": int(time.time() * 1000),
            "mode": "paper" if self.paper else "live",
            "pairs": len(self.md.pair_maps),
            "active_positions": len(active),
            "states": {str(p.id): p.state for p in active},
            "marks": self._position_marks(),
        }
        tmp = config.HEARTBEAT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(config.HEARTBEAT_FILE)

    async def _command_loop(self) -> None:
        while True:
            try:
                await self._drain_commands()
            except Exception:
                log.exception("command loop error")
            await asyncio.sleep(config.COMMAND_POLL_SECONDS)

    async def _drain_commands(self) -> None:
        now_ms = int(time.time() * 1000)
        for row in pending_commands(self.conn):
            age_s = (now_ms - row["created_ms"]) / 1000
            if age_s > config.COMMAND_TTL_SECONDS:
                # The engine was down when this was queued; firing a stale /enter
                # or /flatten now would trade at the wrong market. Skip it.
                resolve_command(
                    self.conn, row["id"], "expired",
                    f"skipped: {age_s:.0f}s old (engine was down when queued)",
                )
                journal(self.conn, f"command {row['id']} {row['command']}"
                        f" expired ({age_s:.0f}s old)", "WARN")
                continue
            # Claim before executing so a crash mid-command can't replay it on
            # restart (pending_commands never returns 'running').
            if not claim_command(self.conn, row["id"]):
                continue
            response = await self._handle_command(
                row["command"], json.loads(row["args"])
            )
            resolve_command(self.conn, row["id"], "done", response)

    async def _handle_command(self, command: str, args: dict) -> str:
        try:
            if command == "enter":
                return self._cmd_enter(args)
            if command == "exit":
                return await self._cmd_exit(args)
            if command == "cancel":
                return await self._cmd_cancel(args)
            if command == "flatten":
                return await self._cmd_flatten()
            if command == "recon":
                return await self._cmd_recon()
            if command == "book":
                return await self._cmd_book(args)
            if command == "adopt":
                return await self._cmd_adopt(args)
            if command == "balance":
                return await self._cmd_balance()
            if command == "refresh":
                return await self._cmd_refresh()
            if command == "equity":
                return await self._cmd_equity(args)
            if command == "recompute":
                return await self._cmd_recompute(args)
            if command == "truefill":
                return await self._cmd_truefill(args)
            if command == "orders":
                return await self._cmd_orders(args)
            if command == "stops":
                return await self._cmd_stops(args)
            if command == "remove":
                return await self._cmd_remove(args)
            return f"unknown command: {command}"
        except Exception as exc:
            log.exception("command %s failed", command)
            return f"error: {exc}"

    def _cmd_enter(self, args: dict) -> str:
        symbol = args["symbol"].upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        notional = Decimal(str(args["notional"]))
        if symbol not in self.md.pair_maps:
            return f"{symbol} is not cross-listed (no Aster perp + MEXC spot pair)"
        if notional <= 0 or notional > config.MAX_NOTIONAL_PER_LEG_USD:
            return f"notional must be in (0, {config.MAX_NOTIONAL_PER_LEG_USD}]"
        min_bps = (
            Decimal(str(args["min_bps"])) if args.get("min_bps") is not None else None
        )
        kind = "carry" if str(args.get("kind", "")).lower() == "carry" else "convergence"

        # Size up an existing position rather than rejecting it.
        existing = [
            p for p in self.positions.active()
            if p.symbol == symbol and p.state != pm.UNWINDING
        ]
        if existing:
            if len(existing) > 1:
                ids = ", ".join(str(p.id) for p in existing)
                return (f"multiple active positions in {symbol} (ids {ids})"
                        f" — can't auto-add, manage them by ID")
            pos = existing[0]
            if pos.state != pm.OPEN:
                return (f"{symbol} position #{pos.id} is {pos.state} (still working)"
                        f" — wait for it to settle or /cancel {pos.id} first")
            # Cap against the ACTUAL current filled size (perp qty x live price),
            # not target_notional — target_notional must never be pre-bumped by a
            # requested add, or cancelled adds inflate it and later adds size
            # against phantom exposure.
            pair = self.md.pair_maps.get(symbol)
            aster = self.md.aster_books.get(pair.aster_symbol) if pair else None
            cur_notional = (
                pos.perp_qty * aster.bid if aster and aster.bid > 0
                else pos.target_notional
            )
            if cur_notional + notional > config.MAX_NOTIONAL_PER_LEG_USD:
                return (f"add would take {symbol} to ~${float(cur_notional + notional):,.0f}"
                        f" notional, over the ${config.MAX_NOTIONAL_PER_LEG_USD} per-leg cap")
            if min_bps is not None:
                self.positions.set_min_entry_bps(pos.id, min_bps)
            self.executor.start_add(self.positions.get(pos.id), notional)
            floor = (
                min_bps if min_bps is not None
                else (pos.min_entry_bps if pos.min_entry_bps is not None
                      else config.ENTRY_MIN_EDGE_FLOOR_BPS)
            )
            return (
                f"sizing up #{pos.id} {symbol} [{pos.trade_kind}]: adding ${notional}"
                f" to ~${float(cur_notional):,.0f} current — SELL perp (maker) / BUY"
                f" spot on fill, basis floor {float(floor):.1f}bps"
            )

        pos = self.positions.create(
            symbol, notional, paper=self.paper, min_entry_bps=min_bps,
            trade_kind=kind,
        )
        self.executor.start_entry(pos)
        floor = min_bps if min_bps is not None else config.ENTRY_MIN_EDGE_FLOOR_BPS
        adverse = (
            "" if config.ADVERSE_WIDEN_STOP_BPS is None
            else f"; adverse-widen stop at +{float(config.ADVERSE_WIDEN_STOP_BPS):.0f}bps"
        )
        auto = (
            f"auto-closes: passive maker at"
            f" {float(config.CONVERGED_PASSIVE_BPS):.0f}bps, crosses if taker"
            f" turns profitable, max-hold {config.MAX_HOLD_HOURS}h{adverse}"
            if kind == "convergence"
            else f"CARRY: manual /exit only{adverse}"
        )
        return (
            f"entry #{pos.id} started [{kind}]: SELL {symbol} perp (maker) /"
            f" BUY spot on fill, notional ${notional},"
            f" basis floor {float(floor):.1f}bps — {auto}"
        )

    def _resolve_position(self, ref: str) -> pm.Position | str:
        """Resolve a position reference: numeric ID or symbol (BEAT, beatusdt).
        Returns the Position, or an error string for the operator."""
        ref = str(ref).strip()
        if ref.isdigit():
            try:
                return self.positions.get(int(ref))
            except KeyError:
                return f"no position with id {ref}"
        symbol = ref.upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        matches = [p for p in self.positions.active() if p.symbol == symbol]
        if not matches:
            return f"no active position in {symbol}"
        if len(matches) > 1:
            ids = ", ".join(str(p.id) for p in matches)
            return f"multiple active positions in {symbol} (ids {ids}) — use the ID"
        return matches[0]

    def _stop_lock(self, position_id: int) -> asyncio.Lock:
        """One lock per position around placing and cancelling its stops, so
        the two can never interleave. Placing and cancelling both wait on the
        venue; without this, a cancel could run while a placement was mid-
        flight and miss the order it was about to create."""
        locks = self.__dict__.setdefault("_stop_locks", {})
        lock = locks.get(position_id)
        if lock is None:
            lock = locks[position_id] = asyncio.Lock()
        return lock

    async def _cancel_stops_for(self, pos: pm.Position) -> int:
        async with self._stop_lock(pos.id):
            return await self._cancel_stops_locked(pos)

    async def _cancel_stops_locked(self, pos: pm.Position) -> int:
        """Cancel a position's resting protective /stops on both venues. Called
        when a close begins so the spot LIMIT stops locking the balance (which
        makes the exit's spot sell fail 'insufficient balance') and the perp
        STOP doesn't fire mid-close. No-op in paper / when no pair.

        Cancels by the venue order ids recorded when the stops were placed
        FIRST, and only then sweeps by client-id prefix for any it did not
        know about. This used to delete those recorded ids and then rely on
        the prefix sweep alone, so a sweep that missed — a venue that does not
        echo client ids, an open-orders call that failed — left the MEXC
        stop-limit resting, locking the whole spot balance, with nothing
        reporting it. That is how position 226's exit sat unable to sell for
        35 minutes while its perp was already closed.
        """
        known = dict(self._stops_orders.get(pos.id) or {})
        stored = database.load_stop_orders(self.conn).get(pos.id) or {}
        for k in ("aster_id", "mexc_id"):
            known.setdefault(k, stored.get(k))
        self._stops_qty.pop(pos.id, None)   # no longer managing stops for it
        self._stop_grace.pop(pos.id, None)
        if self.paper:
            self._stops_orders.pop(pos.id, None)
            database.clear_stop_orders(self.conn, pos.id)
            return 0
        pair = self.md.pair_maps.get(pos.symbol)
        if pair is None:
            return 0
        cancelled = 0
        failed: list[str] = []
        for client, symbol, key in (
            (self.aster, pair.aster_symbol, "aster_id"),
            (self.mexc, pair.mexc_symbol, "mexc_id"),
        ):
            oid = known.get(key)
            if not oid:
                continue
            try:
                await client.cancel_order(symbol, str(oid))
                cancelled += 1
            except ExchangeError as exc:
                # Filled or already cancelled is the usual reason, and fine.
                # Anything else is recorded so the sweep below gets a second go.
                failed.append(f"{key} {oid}: {exc}")
        swept = await self._cancel_stop_orders(pair)
        # The record goes only once the orders it points at have been dealt
        # with — clearing it first is what lost them.
        self._stops_orders.pop(pos.id, None)
        database.clear_stop_orders(self.conn, pos.id)
        if failed and not swept:
            journal(self.conn, f"position {pos.id}: stop cancel incomplete — "
                    + "; ".join(failed), "WARN")
        return cancelled + swept

    async def _cmd_exit(self, args: dict) -> str:
        pos = self._resolve_position(args["position_id"])
        if isinstance(pos, str):
            return pos
        if pos.state not in (pm.OPEN, pm.EXITING):
            return f"position {pos.id} is {pos.state}, cannot exit"
        mode = args.get("mode", "now")
        if mode == "cancel":
            return await self._cancel_exit(pos)
        target = args.get("target_bps")
        target_dec = Decimal(str(target)) if target is not None else (
            config.EXIT_BASIS_BPS if mode == "passive" else None
        )
        # Optional partial-close size, in coins (perp contracts, as shown in
        # /positions). Stored as the floor to stop at. >= current size = full.
        target_qty = None
        size_desc = "full"
        qty_arg = args.get("qty")
        if qty_arg is not None:
            raw = str(qty_arg).strip()
            # "$500" = USD notional, converted to perp contracts at the live
            # perp mid. A bare number stays coins/contracts (as in /positions).
            usd_mode = raw.startswith("$")
            try:
                q = Decimal(raw[1:].replace(",", "") if usd_mode else raw)
            except (InvalidOperation, ValueError):
                return f"bad qty: {qty_arg}"
            if q <= 0:
                return "qty must be > 0"
            usd = q
            if usd_mode:
                pair = self.md.pair_maps.get(pos.symbol)
                info = self.md.aster_info.get(pair.aster_symbol) if pair else None
                abook = self.md.aster_books.get(pair.aster_symbol) if pair else None
                px = (
                    (abook.bid + abook.ask) / 2
                    if abook and abook.bid > 0 and abook.ask > 0 else Decimal(0)
                )
                if info is None or px <= 0:
                    return (f"{pos.symbol}: no live perp quote to size ${usd}"
                            f" — pass a coin quantity instead")
                q = info.round_qty(usd / px)
                if q <= 0:
                    return (f"${usd} is below one lot of {pos.symbol}"
                            f" (step {info.step_size} @ ~{px}) — increase the size")
            if q < pos.perp_qty:
                target_qty = pos.perp_qty - q
                size_desc = (
                    f"~${usd:,.0f} ({q}) of {pos.perp_qty}" if usd_mode
                    else f"{q} of {pos.perp_qty}"
                )
        # Record the exit request BEFORE cancelling the stops. The cancel waits
        # on the venue, and while it did, the safety sweep saw an OPEN position
        # with no exit task and no stops on record — and placed fresh ones,
        # including a spot sell LIMIT that locked the whole balance for the
        # entire exit. That is what left position 226 naked long for 35
        # minutes. _ensure_stops now refuses any position with an exit
        # requested, so recording it first closes the window.
        self.positions.set_exit_request(pos.id, mode, target_dec, target_qty)
        await self._cancel_stops_for(pos)
        await self.executor.start_exit(self.positions.get(pos.id))
        desc = "aggressive (taker both legs)" if mode == "now" else (
            f"passive maker, target {target_dec}bps"
        )
        return f"position {pos.id}: exit started — {desc}, size {size_desc}"

    async def _cancel_exit(self, pos: pm.Position) -> str:
        """Stop a working exit: cancel the task and AWAIT its cleanup (which
        pulls any resting maker order and records late fills) BEFORE flipping
        the position back to OPEN, so the state can't say OPEN while the dying
        task is still trading."""
        if pos.state != pm.EXITING and pos.exit_mode is None:
            return f"position {pos.id} has no working exit"
        await self.executor._cancel_task(pos.id)
        self.positions.set_exit_request(pos.id, None, None)
        self.positions.set_state(pos.id, pm.OPEN, "exit cancelled by operator")
        return f"position {pos.id}: exit cancelled, back to OPEN"

    async def _cmd_cancel(self, args: dict) -> str:
        pos = self._resolve_position(args["position_id"])
        if isinstance(pos, str):
            return pos
        # /cancel handles whatever is working: an exit if EXITING, else an entry.
        if pos.state == pm.EXITING:
            return await self._cancel_exit(pos)
        if self.executor.request_cancel(pos.id):
            return f"position {pos.id}: entry cancel requested"
        return f"position {pos.id}: no working entry or exit to cancel"

    async def _cmd_remove(self, args: dict) -> str:
        """Stop tracking a position that was closed manually on the exchange:
        mark it CLOSED in the DB and place NO closing trades (unlike /flatten).
        Cancels any working entry/exit task, clears its auto-exit / liq-alert
        state, and cancels any resting orders it left on the venues (entry,
        exit and /stops) so they can't fire on an untracked position.
        Reversible via /adopt if done by mistake."""
        pos = self._resolve_position(str(args.get("position_id", "")))
        if isinstance(pos, str):
            return pos
        if pos.state in (pm.CLOSED, pm.CANCELLED):
            return f"position {pos.id} {pos.symbol} is already {pos.state}"
        # Await cleanup so a dying exit task can't record fills into the position
        # after we mark it CLOSED below.
        await self.executor._cancel_task(pos.id)
        self._auto_passive.discard(pos.id)
        self._liq_alerted.pop(pos.id, None)
        self._stops_qty.pop(pos.id, None)
        self._stops_orders.pop(pos.id, None)
        database.clear_stop_orders(self.conn, pos.id)
        self._stop_grace.pop(pos.id, None)
        cancelled = 0
        pair = self.md.pair_maps.get(pos.symbol)
        if pair is not None and not self.paper:
            cancelled = await self._cancel_stop_orders(
                pair, ("sp_stop_", "sp_pent_", "sp_pext_")
            )
        prior = pos.state
        self.positions.set_state(pos.id, pm.CLOSED, "removed: closed manually on venue")
        journal(self.conn, f"position {pos.id} {pos.symbol}: /remove -> CLOSED"
                f" (was {prior}, cancelled {cancelled} resting orders)")
        extra = f", cancelled {cancelled} resting order(s)" if cancelled else ""
        return (
            f"removed #{pos.id} {pos.symbol} from active positions — marked CLOSED,"
            f" no closing trades placed{extra} (was {prior}, perp={pos.perp_qty}"
            f" spot={pos.spot_qty}). If that was a mistake, /adopt {pos.symbol}"
            f" to restore tracking."
        )

    async def _cmd_flatten(self) -> str:
        count = 0
        for pos in self.positions.active():
            if pos.state in (pm.PENDING_ENTRY, pm.ENTERING):
                self.executor.request_cancel(pos.id)
                count += 1
            elif pos.state == pm.OPEN:
                # Request the exit FIRST: _ensure_stops skips a position with
                # an exit requested, so stops cannot be re-placed while the
                # cancel below is waiting on the venue.
                self.positions.set_exit_request(pos.id, "now", None)
                await self._cancel_stops_for(pos)
                await self.executor.start_exit(self.positions.get(pos.id))
                count += 1
        return f"flatten: {count} positions being closed/cancelled"

    async def _cmd_balance(self) -> str:
        """USDT balance on each venue (Aster perp margin + MEXC spot)."""
        lines = ["USDT balances"]
        total = Decimal(0)
        try:
            bals = await self.aster.balances()
            u = next((b for b in bals if b.get("asset") == "USDT"), None)
            if u is not None:
                bal = _dec_or_zero(u.get("balance"))
                avail = _dec_or_zero(u.get("availableBalance"))
                total += bal
                lines.append(
                    f"Aster perp  {float(bal):>10,.2f}  (avail {float(avail):,.2f})"
                )
            else:
                lines.append("Aster perp  no USDT")
        except ExchangeError as exc:
            lines.append(f"Aster perp  error: {exc}")
        try:
            acct = await self.mexc.account()
            u = next(
                (b for b in acct.get("balances", []) if b.get("asset") == "USDT"), None
            )
            if u is not None:
                free = _dec_or_zero(u.get("free"))
                locked = _dec_or_zero(u.get("locked"))
                tot = free + locked
                total += tot
                lines.append(
                    f"MEXC spot   {float(tot):>10,.2f}  (free {float(free):,.2f})"
                )
            else:
                lines.append("MEXC spot   no USDT")
        except ExchangeError as exc:
            lines.append(f"MEXC spot   error: {exc}")
        lines.append(f"combined    {float(total):>10,.2f}")
        return "\n".join(lines)

    async def _cmd_refresh(self) -> str:
        """Re-fetch both venues' exchangeInfo and rebuild the tradeable universe
        on demand, so a freshly-listed coin becomes available without waiting
        for the periodic refresh or restarting the engine."""
        added, removed = await self._load_symbol_maps()
        lines = [f"universe refreshed: {len(self.md.pair_maps)} cross-listed pairs"]
        if added:
            lines.append(f"+{len(added)}: " + ", ".join(s[:-4] for s in added[:30]))
        if removed:
            lines.append(f"-{len(removed)}: " + ", ".join(s[:-4] for s in removed[:30]))
        if not added and not removed:
            lines.append("(no change — both venues already known, or fetch failed)")
        return "\n".join(lines)

    def _touch_basis(self, pair) -> tuple[float | None, float | None]:
        """(entry, exit-passive) basis at the touch, in bps, or (None, None).

        Entry is ask/ask (rest the perp short at the ask, buy spot at the ask);
        the passive exit is bid/bid (rest the perp buy-back at the bid, sell
        spot at the bid). They are different numbers and each working order has
        to be judged against its own one.
        """
        aster = self.md.aster_books.get(pair.aster_symbol)
        mexc = self.md.mexc_books.get(pair.mexc_symbol)
        if aster is None or mexc is None or mexc.ask <= 0 or mexc.bid <= 0:
            return None, None
        mult = pair.qty_multiplier
        entry = float((aster.ask / mult - mexc.ask) / mexc.ask * Decimal(10000))
        exit_p = float((aster.bid / mult - mexc.bid) / mexc.bid * Decimal(10000))
        return entry, exit_p

    async def _cmd_orders(self, args: dict) -> str:
        """Every working entry and exit with the level it is waiting for, the
        level the market is at, and the pair's own 24h range for that side.

        Stops are excluded: they are protection, not an attempt to trade a
        level, and they would swamp the list. Note they cannot be filtered by
        ORDER TYPE — the MEXC half of a stop is a plain sell LIMIT, identical
        in kind to a passive exit — so they are matched on the sp_stop_ client
        id prefix instead.
        """
        working = [
            p for p in self.positions.active()
            if p.state in (pm.ENTERING, pm.EXITING)
        ]
        if not working:
            n_open = len(self.positions.active())
            return (
                "no working orders"
                + (f" ({n_open} position(s) OPEN, nothing resting)" if n_open
                   else "")
            )
        out: list[str] = [f"working orders ({len(working)})"]
        stops_seen = 0
        for pos in working:
            pair = self.md.pair_maps.get(pos.symbol)
            if pair is None:
                out.append(f"\n#{pos.id} {pos.symbol}: not cross-listed")
                continue
            entering = pos.state == pm.ENTERING
            entry_now, exit_now = self._touch_basis(pair)
            now = entry_now if entering else exit_now
            lo, hi = self._basis_24h.percentiles(pos.symbol, close=not entering)
            _, hours = self._basis_24h.stats(pos.symbol)
            thin = hours < config.SCREEN_DIFF_MIN_HOURS

            if entering:
                target = pos.min_entry_bps
                head = f"#{pos.id} {pos.symbol}  ENTRY"
                # An entry rests until the basis is at or ABOVE its floor; an
                # exit fires at or BELOW its target. Spelling out the direction
                # stops the reader guessing which side of the level they need.
                want = f"fires at >= {float(target):+.1f}" if target is not None else "fires at the default floor"
            else:
                target = pos.exit_target_bps
                head = f"#{pos.id} {pos.symbol}  EXIT {pos.exit_mode or ''}".rstrip()
                want = (f"fires at <= {float(target):+.1f}" if target is not None
                        else "taker — no level to wait for")
            out.append("")
            out.append(f"{head}  {want}bps" if target is not None else f"{head}  {want}")
            if now is None:
                out.append("  now      -     (no live book)")
            elif lo is None or hi is None or thin:
                out.append(f"  now {now:+7.1f}   24h range: not enough history")
            else:
                where = ""
                if now > hi:
                    where = "  ABOVE 24h range"
                elif now < lo:
                    where = "  BELOW 24h range"
                out.append(
                    f"  now {now:+7.1f}   24h {lo:+.1f} to {hi:+.1f}{where}"
                )
                # The gap that decides whether waiting is realistic: an exit
                # target under the 24h low, or an entry floor over the 24h
                # high, is a level this pair has not reached all day.
                # ...but only when the level is not already satisfied: an
                # entry whose floor the basis ALREADY clears fills on the next
                # tick, so warning that it "may never fill" would be nonsense.
                if target is not None:
                    t = float(target)
                    satisfied = now >= t if entering else now <= t
                    if satisfied:
                        out.append("  ready — the level is met right now")
                    elif entering and t > hi:
                        out.append(
                            f"  ⚠ floor {t:+.1f} is above the 24h high"
                            f" {hi:+.1f} — may never fill"
                        )
                    elif (not entering) and t < lo:
                        out.append(
                            f"  ⚠ target {t:+.1f} is below the 24h low"
                            f" {lo:+.1f} — may never fill"
                        )
            out.append(
                f"  filled {pos.perp_qty} perp / {pos.spot_qty} spot"
            )
            if self.paper:
                out.append("  (paper — no venue orders)")
                continue
            for client, sym, leg in (
                (self.aster, pair.aster_symbol, "perp"),
                (self.mexc, pair.mexc_symbol, "spot"),
            ):
                try:
                    orders = await client.open_orders(sym)
                except ExchangeError as exc:
                    out.append(f"  {leg}: open-orders fetch failed ({exc})")
                    continue
                for o in orders:
                    if o.client_order_id.startswith("sp_stop_"):
                        stops_seen += 1
                        continue
                    mine = o.client_order_id.startswith(("sp_pent_", "sp_pext_"))
                    tag = "" if mine else "  (not placed by the bot)"
                    done = (f" filled {o.executed_qty}/{o.orig_qty}"
                            if o.executed_qty > 0 else f" {o.orig_qty}")
                    out.append(
                        f"  {leg} {o.side}{done} @ {recon._p(o.price)}{tag}"
                    )
        out.append("")
        out.append(
            "ENTRY rests until the basis rises TO its floor; EXIT fires when"
            " the basis falls TO its target. 'now' is that side's live touch"
            " basis — entry is ask/ask, exit is bid/bid, so they differ."
        )
        out.append(
            f"24h = p10/p90 of the hourly mean for that side."
            + (f" {stops_seen} stop order(s) hidden." if stops_seen else "")
        )
        return "\n".join(out)

    async def _cmd_equity(self, args: dict) -> str:
        """Total account value now, the daily change table, and a chart."""
        if self.paper:
            return "equity needs LIVE mode — paper has no venue balances"
        days = int(args.get("days") or 14)
        eq = await equity.snapshot(self.aster, self.mexc, self.md.mexc_books)
        lines = ["account value"]
        lines.append(
            f"  Aster perp  {float(eq.aster_usd):>12,.2f}"
            f"   (margin {float(eq.aster_margin_usd):,.2f},"
            f" upnl {float(eq.aster_upnl_usd):+,.2f})"
        )
        lines.append(f"  MEXC coins  {float(eq.spot_coins_usd):>12,.2f}")
        lines.append(f"  MEXC USDT   {float(eq.spot_usdt_usd):>12,.2f}")
        lines.append(f"  TOTAL       {float(eq.total_usd):>12,.2f}")
        if eq.coins:
            top = "  ".join(
                f"{a} {float(v):,.0f}" for a, _q, v in eq.coins[:6]
            )
            lines.append(f"  holdings: {top}")
        if eq.unpriced:
            lines.append(
                "  not priced (no MEXC USDT book): "
                + ", ".join(sorted(set(eq.unpriced))[:10])
            )
        for e in eq.errors:
            lines.append(f"  ⚠️ {e}")

        # Store this reading too, so asking always extends the history.
        if not eq.errors:
            database.record_equity(
                self.conn, int(time.time() * 1000), eq.aster_usd,
                eq.spot_coins_usd, eq.spot_usdt_usd, eq.total_usd,
            )
        since = int(time.time() * 1000) - days * 86_400_000
        series = equity.daily_series(database.equity_history(self.conn, since))
        if len(series) < 2:
            lines.append("")
            lines.append(
                f"no daily history yet — sampled every"
                f" {config.EQUITY_SNAPSHOT_MINUTES:.0f}min, so the table fills"
                " in from tomorrow."
            )
            return "\n".join(lines)

        lines.append("")
        # Components, not just the total: a hedged book can show a daily change
        # purely because the two legs are marked differently (Aster mark price
        # vs MEXC bid), and seeing perp and coins move in OPPOSITE directions
        # is what identifies that as a marking artefact rather than profit.
        hdr = (f"{'date (UTC)':<11}{'value':>11}{'change':>9}{'%':>7}"
               f"{'perp':>9}{'coins':>9}{'usdt':>9}")
        lines.append(hdr)
        lines.append("-" * len(hdr))
        prev = None
        for m in series[-days:]:
            if prev is None:
                cells = f"{'-':>9}{'-':>7}{'-':>9}{'-':>9}{'-':>9}"
            else:
                d = m.total - prev.total
                pct = f"{float(d / prev.total * 100):+.2f}" if prev.total else "-"
                cells = (
                    f"{float(d):>+9,.2f}{pct:>7}"
                    f"{float(m.aster - prev.aster):>+9,.2f}"
                    f"{float(m.spot_coins - prev.spot_coins):>+9,.2f}"
                    f"{float(m.spot_usdt - prev.spot_usdt):>+9,.2f}"
                )
            lines.append(f"{m.day:<11}{float(m.total):>11,.2f}{cells}")
            prev = m
        first, last = series[0].total, series[-1].total
        move = last - first
        lines.append("-" * len(hdr))
        lines.append(
            f"{len(series)}d change {float(move):+,.2f}"
            + (f" ({float(move / first * 100):+.2f}%)" if first else "")
        )
        if series[0].samples < 2:
            lines.append(
                f"⚠️ {series[0].day} has only {series[0].samples} sample, so it"
                " is the moment sampling started, not a full day's close — the"
                " first change figure spans less than a day."
            )
        # What the closed trades actually earned, to sit beside the marks. A
        # gap between the two is mark-to-market on open positions, funding, or
        # a transfer — not an error in either number.
        realised = self.positions.pnl_summary().get("live_today", Decimal(0))
        if len(series) >= 2:
            lines.append(
                f"today: marks {float(series[-1].total - series[-2].total):+,.2f}"
                f" vs closed trades {float(realised):+,.2f}"
            )

        chart = equity.sparkline([float(m.total) for m in series[-days:]])
        if chart:
            lines.append("")
            lines += chart
        lines.append("")
        lines.append(
            "A day's change is the difference between two MARKS, not a sum of"
            " trades. It moves when nothing is traded: the perp leg is valued"
            " at Aster's mark price and the spot leg at the MEXC bid, so a"
            " change in the basis — or a wide spot book — shifts the total on"
            " its own. perp and coins moving opposite ways by similar amounts"
            " is that, and it reverses when the basis does."
        )
        lines.append(
            "Real money in the number: funding received, fees paid, realised"
            " trades. Not P&L at all: deposits and withdrawals, which look"
            " exactly like profit here."
        )
        return "\n".join(lines)

    async def _cmd_recompute(self, args: dict) -> str:
        """Re-derive a position's legs from its fills.

        The stored averages are folded in as fills arrive, so a position that
        was written by an older, wrong derivation keeps that answer until the
        next fill lands. This replays the fills and rewrites qty, averages,
        fees and realised P&L. Touches the book only; places no orders.
        """
        pos = self._resolve_position(str(args.get("position_id", "")))
        if isinstance(pos, str):
            return pos
        pair = self.md.pair_maps.get(pos.symbol)
        mult = pair.qty_multiplier if pair else Decimal(1)

        def basis(p) -> str:
            if not (p.perp_entry_avg and p.spot_entry_avg):
                return "-"
            v = (p.perp_entry_avg / mult - p.spot_entry_avg) / p.spot_entry_avg
            return f"{float(v * 10000):+.1f}bps"

        before = pos
        self.positions.recompute_from_fills(pos.id)
        after = self.positions.get(pos.id)
        lines = [f"position {pos.id} {pos.symbol}: re-derived from fills"]
        lines.append(f"  perp qty   {before.perp_qty} -> {after.perp_qty}")
        lines.append(
            f"  perp entry {recon._p(before.perp_entry_avg or Decimal(0))}"
            f" -> {recon._p(after.perp_entry_avg or Decimal(0))}"
        )
        lines.append(f"  entry basis {basis(before)} -> {basis(after)}")
        if after.unwind_pnl_usd:
            lines.append(f"  unwind P&L ${float(after.unwind_pnl_usd):+.2f}")
        if before.state in (pm.CLOSED, pm.CANCELLED):
            pnl = self.positions.finalize_pnl(pos.id)
            lines.append(f"  realised P&L -> ${float(pnl):+.2f}")
        else:
            # An open position has no realised P&L yet; recomputing one would
            # book a number for a trade that has not finished.
            self.conn.execute(
                "UPDATE positions SET entry_basis_bps=? WHERE id=?",
                (str((after.perp_entry_avg / mult - after.spot_entry_avg)
                     / after.spot_entry_avg * Decimal(10000))
                 if after.perp_entry_avg and after.spot_entry_avg else None,
                 pos.id),
            )
            self.conn.commit()
        journal(self.conn, f"position {pos.id}: recomputed from fills")
        return "\n".join(lines)

    async def _cmd_truefill(self, args: dict) -> str:
        """Replace an exit price booked at MARK with the venue's real fills.

        When the hedge guard cannot identify the order behind a perp close it
        books the close at the mark price — a guess, flagged in the alert as
        "verify the real close price on Aster". Those rows carry order_id
        'ADL'. This looks the window up in Aster's own trade record, replaces
        the guessed price and commission with the executed ones, rebuilds the
        leg averages from the fills and recomputes realised P&L.

        Read-only on the venue: it moves no money, it only corrects the book.
        """
        if self.paper:
            return "truefill needs LIVE mode — it reads real venue trades"
        pos = self._resolve_position(str(args.get("position_id", "")))
        if isinstance(pos, str):
            return pos
        pair = self.md.pair_maps.get(pos.symbol)
        if pair is None:
            return f"{pos.symbol}: not cross-listed"
        synthetic = self.positions.synthetic_fills(pos.id)
        if not synthetic:
            return (f"position {pos.id} {pos.symbol}: no mark-priced fills —"
                    " every close is already booked from a real order")

        before = self.positions.get(pos.id)
        lines = [f"position {pos.id} {pos.symbol}: {len(synthetic)} mark-priced"
                 f" fill(s) to correct"]
        fixed = 0
        for f in synthetic:
            if f["venue"] != "aster":
                continue
            # The guard acts only after HEDGE_BREAK_CONFIRM_SECONDS of
            # confirmation on a ~15s poll, so the real trade predates the
            # recorded row by up to a couple of minutes. Look back generously
            # and forward a little; a wrong window finds nothing rather than
            # the wrong trade.
            start = int(f["ts_ms"]) - 30 * 60_000
            end = int(f["ts_ms"]) + 5 * 60_000
            try:
                trades = await self.aster.user_trades(
                    pair.aster_symbol, start, end
                )
            except ExchangeError as exc:
                return f"{lines[0]}\nAster userTrades failed: {exc}"
            # Closing a short is a BUY. Newest first: the stop fill is the most
            # recent buy before the guard noticed.
            buys = sorted(
                (t for t in trades if str(t.get("side", "")).upper() == "BUY"),
                key=lambda t: int(t.get("time") or 0), reverse=True,
            )
            want = Decimal(f["qty"])
            took = Decimal(0)
            cost = Decimal(0)
            fee = Decimal(0)
            used: list[str] = []
            for t in buys:
                if took >= want:
                    break
                q = min(_dec_or_zero(t.get("qty")), want - took)
                if q <= 0:
                    continue
                took += q
                cost += q * _dec_or_zero(t.get("price"))
                fee += _dec_or_zero(t.get("commission"))
                used.append(str(t.get("id") or t.get("orderId") or "?"))
            if took <= 0:
                lines.append(f"  fill #{f['id']}: no Aster BUY trades in the"
                             f" window — left as booked")
                continue
            vwap = cost / took
            if took < want:
                lines.append(f"  fill #{f['id']}: only found {took} of {want}"
                             f" — corrected the part that matched")
            self.positions.update_fill(
                int(f["id"]), vwap, fee, f"venue:{','.join(used[:3])}"
            )
            lines.append(
                f"  fill #{f['id']}: {recon._p(Decimal(f['price']))} (mark)"
                f" -> {recon._p(vwap)} (venue), fee {float(fee):.4f}"
            )
            fixed += 1

        if not fixed:
            return "\n".join(lines)
        self.positions.recompute_from_fills(pos.id)
        pnl = self.positions.finalize_pnl(pos.id)
        after = self.positions.get(pos.id)
        lines.append("")
        lines.append(
            f"perp exit avg {recon._p(before.perp_exit_avg or Decimal(0))}"
            f" -> {recon._p(after.perp_exit_avg or Decimal(0))}"
        )
        old_pnl = before.realized_pnl_usd
        lines.append(
            f"realised P&L {'-' if old_pnl is None else f'${float(old_pnl):+.2f}'}"
            f" -> ${float(pnl):+.2f}"
        )
        journal(self.conn, f"position {pos.id}: truefill corrected {fixed}"
                f" mark-priced fill(s), pnl -> {pnl}")
        return "\n".join(lines)

    async def _cmd_stops(self, args: dict) -> str:
        """Place liquidation-protection orders for one position: a reduce-only
        buy STOP_MARKET on the Aster perp STOP_LIQ_BUFFER_PCT below the liq
        price (triggers on the mark, closing the short before liquidation), and
        a resting sell LIMIT on MEXC spot at the same level (full size on both).
        Re-running refreshes: prior /stops orders are cancelled first. Once
        placed, the safety loop auto-refreshes them if the position is resized."""
        if self.paper:
            return "stops need LIVE mode — they place real protective orders"
        pos = self._resolve_position(str(args.get("symbol", "")))
        if isinstance(pos, str):
            return pos
        if pos.state not in (pm.OPEN, pm.EXITING):
            return f"position {pos.id} is {pos.state}, no stops placed"
        if pos.state == pm.EXITING or pos.exit_mode is not None:
            # A stop's spot half is a resting sell LIMIT for the full size, and
            # a resting sell LOCKS the coins it is selling — the same coins the
            # exit needs. Placing it mid-exit is how an exit stalls on
            # 'Oversold' with the perp already closed. The liquidation guard is
            # what protects a working exit: at LIQ_ALERT_PCT it cancels the
            # exit and arms stops itself.
            return (
                f"position {pos.id} {pos.symbol} has an exit working — stops"
                " would lock the spot the exit is trying to sell. It is still"
                f" protected: at {config.LIQ_ALERT_PCT}% to liquidation the"
                " exit is cancelled and stops are armed automatically. To"
                f" place them now, /exit {pos.id} cancel first."
            )
        async with self._stop_lock(pos.id):
            return await self._place_stops(pos)

    async def _place_stops(self, pos: pm.Position) -> str:
        """(Re)place the protective orders at the position's CURRENT size and
        record that size so a later resize (e.g. /enter size-up) auto-refreshes."""
        pair = self.md.pair_maps.get(pos.symbol)
        if pair is None:
            return f"{pos.symbol}: not cross-listed"
        aster_info = self.md.aster_info.get(pair.aster_symbol)
        mexc_info = self.md.mexc_info.get(pair.mexc_symbol)
        if aster_info is None or mexc_info is None:
            return f"{pos.symbol}: missing symbol info (try /refresh)"

        risk = self._position_risk.get(pair.aster_symbol)
        if not risk:
            try:
                rows = await self.aster.position_risk()
            except ExchangeError as exc:
                return f"{pos.symbol}: couldn't fetch positionRisk ({exc})"
            risk = next(
                (r for r in rows if r.get("symbol") == pair.aster_symbol), None
            )
        liq = _dec_or_zero(risk.get("liquidationPrice")) if risk else Decimal(0)
        if liq <= 0:
            return (f"{pos.symbol}: no liquidation price on Aster"
                    f" (cross-margin / no leverage?) — can't size the stop")

        buf = config.STOP_LIQ_BUFFER_PCT / Decimal(100)
        stop_ref = liq * (Decimal(1) - buf)          # perp-contract price terms
        perp_stop = aster_info.round_price(stop_ref, up=False)
        spot_price = mexc_info.round_price(stop_ref / pair.qty_multiplier, up=False)
        perp_qty = aster_info.round_qty(pos.perp_qty)
        spot_qty = mexc_info.round_qty(pos.spot_qty)
        if perp_qty <= 0 or spot_qty <= 0:
            return f"{pos.symbol}: position too small to place stops"

        cancelled = await self._cancel_stop_orders(pair)
        lines: list[str] = []
        aster_id = mexc_id = None
        try:
            r = await self.aster.place_order(
                pair.aster_symbol, "BUY", "STOP_MARKET",
                quantity=perp_qty, stop_price=perp_stop, reduce_only=True,
                working_type="MARK_PRICE",
                client_order_id=intents.make_client_order_id(pos.id, "stop"),
            )
            aster_id = r.order_id
            lines.append(f"perp STOP buy {perp_qty} trigger {recon._p(perp_stop)} (id {r.order_id})")
        except ExchangeError as exc:
            lines.append(f"perp stop FAILED: {exc}")
        try:
            r = await self.mexc.place_order(
                pair.mexc_symbol, "SELL", "LIMIT",
                quantity=spot_qty, price=spot_price,
                client_order_id=intents.make_client_order_id(pos.id, "stop"),
            )
            mexc_id = r.order_id
            lines.append(f"spot SELL limit {spot_qty} @ {recon._p(spot_price)} (id {r.order_id})")
        except ExchangeError as exc:
            lines.append(f"spot limit FAILED: {exc}")

        # Track the size these stops cover so the safety loop re-places them if
        # the position is later resized (a size-up otherwise leaves the added
        # portion unprotected until the operator remembers to re-run /stops).
        self._stops_qty[pos.id] = perp_qty
        database.save_stop_orders(self.conn, pos.id, aster_id, mexc_id, perp_qty)
        # ...and the venue order ids, so the hedge guard can tell OUR OWN stop
        # firing apart from an ADL and read the real fill prices. In-memory:
        # after a restart the guard degrades to the mark-price ADL path.
        self._stops_orders[pos.id] = {"aster_id": aster_id, "mexc_id": mexc_id}
        journal(self.conn, f"position {pos.id}: /stops liq={liq} stop={perp_stop}"
                f" qty={perp_qty} (cancelled {cancelled} prior)")
        head = (f"stops for #{pos.id} {pos.symbol}: {float(config.STOP_LIQ_BUFFER_PCT):.0f}%"
                f" below liq {recon._p(liq)}")
        if cancelled:
            head += f" (replaced {cancelled} prior)"
        return head + "\n  " + "\n  ".join(lines)

    async def _cancel_stop_orders(
        self, pair: screener.PairMap, prefixes: tuple[str, ...] = ("sp_stop_",)
    ) -> int:
        """Cancel our resting orders on both venues for a symbol, matched by
        client-id prefix. Default is just /stops (sp_stop_) so re-running /stops
        refreshes rather than stacks; /remove passes all sp_ prefixes to clear
        every order it left behind."""
        cancelled = 0
        for client, symbol in (
            (self.aster, pair.aster_symbol), (self.mexc, pair.mexc_symbol)
        ):
            try:
                for o in await client.open_orders(symbol):
                    if o.client_order_id.startswith(prefixes):
                        await client.cancel_order(symbol, o.order_id)
                        cancelled += 1
            except ExchangeError:
                log.exception("cancel resting orders on %s failed", symbol)
        return cancelled

    async def _cmd_book(self, args: dict) -> str:
        """Top-5 order book levels on both venues for a cross-listed symbol."""
        symbol = str(args.get("symbol", "")).upper()
        if not symbol:
            return "usage: /book SYMBOL"
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        pair = self.md.pair_maps.get(symbol)
        if pair is None:
            return f"{symbol} is not cross-listed (no Aster perp + MEXC spot pair)"
        try:
            aster_depth, mexc_depth = await asyncio.gather(
                self.aster.depth(pair.aster_symbol, limit=10),
                self.mexc.depth(pair.mexc_symbol, limit=10),
            )
        except ExchangeError as exc:
            return f"{symbol}: book fetch failed ({exc})"

        # Funding: current (live rate projected to 8h) + realised 24h average.
        stat = self.md.funding_stats.get(pair.aster_symbol)
        fund_row = self.md.funding.get(pair.aster_symbol) or {}
        live_rate = fund_row.get("funding_rate")
        funding = None
        if stat is not None:
            current_8h = stat.current_8h_bps
            if live_rate is not None and stat.interval_hours:
                current_8h = float(
                    live_rate * Decimal(10000) * Decimal(8) / Decimal(stat.interval_hours)
                )
            next_ms = fund_row.get("next_funding_time")
            next_h = (
                float((next_ms - int(time.time() * 1000)) / 3_600_000)
                if next_ms and next_ms > 0 else None
            )
            funding = {
                "current_8h_bps": current_8h,
                "avg_24h_8h_bps": stat.avg_24h_8h_bps,
                "interval_hours": stat.interval_hours,
                "next_funding_h": next_h,
            }
        # The pair's own 24h range for BOTH sides. Entry and close are tracked
        # separately rather than one derived from the other: the gap between
        # them is the live spread of both books, which moves on its own.
        _, hours_24h = self._basis_24h.stats(symbol)
        return book.format_book(
            symbol, pair.qty_multiplier, aster_depth, mexc_depth,
            funding=funding,
            volume=self.md.perp_volume.get(pair.aster_symbol),
            entry_range=self._basis_24h.percentiles(symbol),
            exit_range=self._basis_24h.percentiles(symbol, close=True),
            range_hours=hours_24h,
            range_min_hours=config.SCREEN_DIFF_MIN_HOURS,
            base_entry_range=self._basis_base.percentiles(symbol),
            base_exit_range=self._basis_base.percentiles(symbol, close=True),
            base_hours=self._basis_base.stats(symbol)[1],
            base_label=config.BASELINE_HOURS,
        )

    async def _cmd_adopt(self, args: dict) -> str:
        """Import an existing on-venue short-perp / long-spot carry trade into
        the bot's DB as an OPEN carry position, so it can be closed via /exit.
        Reconstructs perp leg from Aster positionRisk and spot leg from MEXC
        balance + myTrades. Live mode only."""
        if self.paper:
            return "adopt needs LIVE mode (engine is in paper) — these are real positions"
        symbol = str(args.get("symbol", "")).upper()
        if not symbol:
            return "usage: /adopt SYMBOL"
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        pair = self.md.pair_maps.get(symbol)
        if pair is None:
            return f"{symbol} is not cross-listed (no Aster perp + MEXC spot pair)"
        if any(p.symbol == symbol for p in self.positions.active()):
            return f"{symbol} is already an active bot position"
        info = self.md.mexc_info.get(pair.mexc_symbol)
        if info is None:
            return f"{symbol}: no MEXC symbol info"

        try:
            risk = await self.aster.position_risk()
            account = await self.mexc.account()
        except ExchangeError as exc:
            return f"adopt needs live API access: {exc}"

        perp_row = next(
            (r for r in risk
             if r.get("symbol") == pair.aster_symbol
             and Decimal(str(r.get("positionAmt", "0"))) < 0),
            None,
        )
        if perp_row is None:
            return f"{symbol}: no short perp position on Aster to adopt"
        perp_qty = abs(Decimal(str(perp_row.get("positionAmt", "0"))))
        perp_entry = Decimal(str(perp_row.get("entryPrice", "0")))
        perp_base = perp_qty * pair.qty_multiplier

        base = info.base_asset
        balance = Decimal(0)
        for b in account.get("balances", []):
            if b.get("asset") == base:
                balance = _dec_or_zero(b.get("free")) + _dec_or_zero(b.get("locked"))
                break
        spot_qty = info.round_qty(min(balance, perp_base))
        if spot_qty <= 0:
            return (f"{symbol}: short perp but no {base} spot held on MEXC —"
                    f" can't adopt as a hedged position (the LYN case)")

        try:
            trades = await self.mexc.my_trades(pair.mexc_symbol, limit=200)
        except ExchangeError:
            trades = []
        rec = recon.reconstruct_spot_entry(trades, spot_qty)
        if rec is not None:
            spot_entry, earliest_ms, covered = rec
        else:
            spot_entry, earliest_ms, covered = perp_entry / pair.qty_multiplier, None, False

        notional = spot_qty * spot_entry
        pos = self.positions.create(
            symbol, notional, paper=False, trade_kind="carry",
        )
        # Reconstruct the entry legs (fees unknown/already paid -> 0).
        self.positions.record_fill(
            pos.id, "aster", "entry", "SELL", perp_qty, perp_entry, Decimal(0)
        )
        self.positions.record_fill(
            pos.id, "mexc", "entry", "BUY", spot_qty, spot_entry, Decimal(0)
        )
        entry_basis = (
            (perp_entry / pair.qty_multiplier - spot_entry) / spot_entry * Decimal(10000)
        )
        self.positions.set_state(pos.id, pm.OPEN, "adopted from venue")
        opened = earliest_ms or int(time.time() * 1000)
        self.conn.execute(
            "UPDATE positions SET opened_ms=?, entry_basis_bps=? WHERE id=?",
            (opened, str(entry_basis), pos.id),
        )
        self.conn.commit()
        journal(
            self.conn,
            f"position {pos.id}: ADOPTED {symbol} perp={perp_qty} spot={spot_qty}"
            f" entry_basis={entry_basis:.1f}",
        )
        imbalance = spot_qty - perp_base
        warn = ""
        if abs(imbalance) > perp_base * Decimal("0.02"):
            warn = f"\n⚠️ hedge imbalance: {float(imbalance):+.4f} {base} (perp vs spot)"
        est = " (entry price estimated — short trade history)" if not covered else ""
        return (
            f"adopted #{pos.id} {symbol} [carry]: perp -{perp_qty} @ {perp_entry}"
            f" / spot {spot_qty} @ {spot_entry}{est}\n"
            f"entry basis {float(entry_basis):.1f}bps — close via /exit {pos.id}"
            f"{warn}"
        )

    async def _cmd_recon(self) -> str:
        """Value the short-perp / long-spot pairs actually open on the venues
        right now, with the full maker-Aster/taker-MEXC round-trip cost model.
        Works for manually-opened positions too — it reads the exchanges, not
        the bot's position DB."""
        try:
            risk = await self.aster.position_risk()
            account = await self.mexc.account()
        except ExchangeError as exc:
            return f"recon needs live API access (Aster + MEXC): {exc}"

        balances: dict[str, Decimal] = {}
        for b in account.get("balances", []):
            asset = b.get("asset")
            if asset:
                balances[asset] = _dec_or_zero(b.get("free")) + _dec_or_zero(
                    b.get("locked")
                )

        now_ms = int(time.time() * 1000)
        shorts = [
            r for r in risk
            if Decimal(str(r.get("positionAmt", "0"))) < 0
        ]
        if not shorts:
            return "no short perp positions on Aster"

        results = await asyncio.gather(
            *(self._recon_pair(r, balances, now_ms) for r in shorts),
            return_exceptions=True,
        )
        pairs: list[recon.PairRecon] = []
        notes: list[str] = []
        for r, res in zip(shorts, results):
            sym = r.get("symbol", "?")
            if isinstance(res, BaseException):
                log.warning("recon %s failed: %r", sym, res)
                notes.append(f"{sym}: recon failed ({res})")
            elif isinstance(res, str):
                notes.append(res)
            elif res is not None:
                pairs.append(res)
        pairs.sort(key=lambda p: p.net_pnl, reverse=True)
        return recon.format_report(pairs, notes)

    async def _recon_pair(
        self, risk_row: dict, balances: dict[str, Decimal], now_ms: int
    ) -> recon.PairRecon | str | None:
        symbol = risk_row.get("symbol", "")
        pair = self.md.pair_maps.get(symbol)
        if pair is None:
            return f"{symbol}: perp only (no MEXC spot pair)"
        info = self.md.mexc_info.get(pair.mexc_symbol)
        if info is None:
            return f"{symbol}: no MEXC symbol info"
        base = info.base_asset
        aster_book = self.md.aster_books.get(pair.aster_symbol)
        mexc_book = self.md.mexc_books.get(pair.mexc_symbol)
        if aster_book is None or mexc_book is None:
            return f"{symbol}: no live quotes yet"

        perp_qty = abs(Decimal(str(risk_row.get("positionAmt", "0"))))
        perp_entry = Decimal(str(risk_row.get("entryPrice", "0")))
        perp_exit = aster_book.bid
        perp_base = perp_qty * pair.qty_multiplier
        # Mark price drives liquidation; fall back to the book mid if absent.
        perp_mark = _dec_or_zero(risk_row.get("markPrice"))
        if perp_mark <= 0:
            perp_mark = (aster_book.bid + aster_book.ask) / Decimal(2)
        perp_liq = _dec_or_zero(risk_row.get("liquidationPrice"))

        # Funding rate: current (live premiumIndex, projected to 8h) and the
        # realised 24h average re-expressed per 8h — same figures as /funding.
        stat = self.md.funding_stats.get(pair.aster_symbol)
        interval = stat.interval_hours if stat else config.FUNDING_INTERVAL_HOURS
        live_rate = (self.md.funding.get(pair.aster_symbol) or {}).get("funding_rate")
        if live_rate is not None and interval:
            funding_now = float(live_rate * Decimal(10000) * Decimal(8) / Decimal(interval))
        else:
            funding_now = stat.current_8h_bps if stat else 0.0
        funding_avg = stat.avg_24h_8h_bps if stat else 0.0

        spot_balance = balances.get(base, Decimal(0))
        spot_qty = min(spot_balance, perp_base)
        if spot_qty <= 0:
            return f"{symbol}: short perp but no {base} spot held"

        try:
            trades = await self.mexc.my_trades(pair.mexc_symbol, limit=200)
        except ExchangeError:
            log.exception("recon: myTrades(%s) failed", pair.mexc_symbol)
            trades = []
        recon_entry = recon.reconstruct_spot_entry(trades, spot_qty)
        if recon_entry is not None:
            spot_entry, earliest_ms, covered = recon_entry
            spot_entry_est = not covered
        else:
            # No visible buys: assume spot entered near the perp entry price
            # (zero-basis proxy), normalised to MEXC base units.
            spot_entry = perp_entry / pair.qty_multiplier
            earliest_ms = None
            spot_entry_est = True

        start_ms = earliest_ms or (now_ms - config.MAX_HOLD_HOURS * 3_600_000)
        held_hours = (
            Decimal(now_ms - earliest_ms) / Decimal(3_600_000)
            if earliest_ms else None
        )
        funding_usd = Decimal(0)
        try:
            income = await self.aster.income_history(
                pair.aster_symbol, "FUNDING_FEE", start_ms, now_ms
            )
            funding_usd = sum(
                (Decimal(str(i.get("income", "0"))) for i in income), Decimal(0)
            )
        except ExchangeError:
            log.exception("recon: income_history(%s) failed", pair.aster_symbol)

        return recon.PairRecon(
            symbol=symbol,
            base_asset=base,
            perp_qty=perp_qty,
            perp_entry=perp_entry,
            perp_exit=perp_exit,
            spot_qty=spot_qty,
            spot_entry=spot_entry,
            spot_exit=mexc_book.bid,
            spot_entry_est=spot_entry_est,
            funding_usd=funding_usd,
            held_hours=held_hours,
            spot_balance=spot_balance,
            perp_base=perp_base,
            perp_mark=perp_mark,
            perp_liq=perp_liq,
            funding_now_8h_bps=funding_now,
            funding_avg_8h_bps=funding_avg,
        )

    async def _safety_loop(self) -> None:
        while True:
            try:
                active = self.positions.active()
                active_ids = {p.id for p in active}
                for pos in active:
                    # One broken symbol (e.g. dropped from the universe -> KeyError
                    # in _close_basis_bps) must not disable checks for every other
                    # position, so isolate each one.
                    try:
                        if pos.id in self._stop_grace:
                            # Stop fired; the spot stop-limit is working at the
                            # chosen price — this owns the position until done.
                            await self._check_stop_grace(pos)
                            continue
                        # Liquidation risk exists in any state while the perp short
                        # is open, so check it independently of the basis logic.
                        await self._check_liquidation(pos)
                        await self._check_spot_integrity(pos)
                        if pos.state == pm.OPEN:
                            if await self._check_hedge_integrity(pos):
                                continue   # guard took over; skip basis checks
                            await self._check_safety(pos)
                            await self._ensure_stops(pos)
                        elif pos.state == pm.EXITING and pos.id in self._auto_passive:
                            await self._check_auto_passive(pos)
                    except Exception:
                        log.exception("safety check failed for position %s", pos.id)
                # Drop auto-passive ids whose position is no longer active (closed,
                # cancelled). Intersecting with active_ids — NOT a "seen this sweep"
                # set — keeps ids that _check_safety just added this same sweep.
                self._auto_passive &= active_ids
                for gone in set(self._auto_stops_attempt) - active_ids:
                    self._auto_stops_attempt.pop(gone, None)
                for gone in set(self._tp_confirm) - active_ids:
                    self._tp_confirm.pop(gone, None)
                self._liq_protect &= active_ids
                for gone in set(self._spot_check_at) - active_ids:
                    self._spot_check_at.pop(gone, None)
                    self._spot_deficit_since.pop(gone, None)
            except Exception:
                log.exception("safety loop error")
            await asyncio.sleep(config.POLL_INTERVAL_SECONDS * 5)

    async def _check_safety(self, pos: pm.Position) -> None:
        # Never auto-close on a frozen book: a stale quote can show a phantom
        # converged/inverted basis and trigger a taker close into prices that no
        # longer exist. Max-hold waits too; it'll fire once quotes are fresh.
        if not self.executor._books_fresh(pos.symbol):
            return
        close = self.executor._close_basis_bps(pos.symbol)
        if (config.ADVERSE_WIDEN_STOP_BPS is not None
                and close is not None and pos.entry_basis_bps is not None):
            widened = close - pos.entry_basis_bps
            if widened >= config.ADVERSE_WIDEN_STOP_BPS:
                journal(
                    self.conn,
                    f"position {pos.id}: ADVERSE STOP close={close:.1f}bps"
                    f" entry={pos.entry_basis_bps:.1f}bps", "ERROR",
                )
                await self.notifier.alert(
                    f"🛑 position {pos.id} {pos.symbol}: adverse stop — basis"
                    f" widened {float(widened):.1f}bps past entry"
                    f" ({float(pos.entry_basis_bps):.1f} ->"
                    f" {float(close):.1f}bps) — force closing"
                )
                # Request the exit FIRST: _ensure_stops skips a position with
                # an exit requested, so stops cannot be re-placed while the
                # cancel below is waiting on the venue.
                self.positions.set_exit_request(pos.id, "now", None)
                await self._cancel_stops_for(pos)
                await self.executor.start_exit(self.positions.get(pos.id))
                return
        # Carry trades are held for funding and only the operator closes them:
        # skip the convergence auto-close and the max-hold timeout. The
        # adverse-widen stop above still applies (perp-liquidation protection).
        if pos.trade_kind == "carry":
            return
        # Don't auto-close within the min-hold: right after entry the bid/bid
        # closeable basis is mostly the bid-ask spread, not convergence, so the
        # TP would round-trip both spreads at a loss on a wide name. (Adverse
        # stop above and max-hold below still apply from the start.)
        held_min = (
            (time.time() * 1000 - pos.opened_ms) / 60_000
            if pos.opened_ms else 1e9
        )
        if held_min < config.CONVERGENCE_MIN_HOLD_MINUTES:
            return
        # Two-tier convergence auto-close (see config.CONVERGED_PASSIVE_BPS).
        # While still in premium (close above the passive trigger) just hold and
        # collect funding; max-hold below still bounds the carry.
        if (close is not None and close <= config.CONVERGED_PASSIVE_BPS
                and pos.id not in self._liq_protect):
            pnl = self._aggressive_close_pnl(pos)
            if pnl is not None and pnl > 0:
                # Taker-taker close is profitable now — but only cross once the
                # condition has held for several sweeps, so a flickering quote
                # can't open a real trade at a fictional price.
                if not self._tp_confirmed(pos.id):
                    journal(
                        self.conn,
                        f"position {pos.id}: CONVERGED TP confirming"
                        f" {self._tp_confirm[pos.id]}/"
                        f"{config.CONVERGED_TP_CONFIRM_TICKS}"
                        f" basis={close:.1f}bps est_pnl={pnl:.2f}",
                    )
                    return
                self._tp_confirm.pop(pos.id, None)
                journal(
                    self.conn,
                    f"position {pos.id}: CONVERGED TP basis={close:.1f}bps"
                    f" est_pnl={pnl:.2f}",
                )
                await self.notifier.alert(
                    f"🎯 position {pos.id} {pos.symbol}: basis {float(close):.1f}bps,"
                    f" taker close nets ${float(pnl):+.2f} — taking profit"
                )
                # Request the exit FIRST: _ensure_stops skips a position with
                # an exit requested, so stops cannot be re-placed while the
                # cancel below is waiting on the venue.
                self.positions.set_exit_request(pos.id, "now", None)
                await self._cancel_stops_for(pos)
                await self.executor.start_exit(self.positions.get(pos.id))
                return
            # Converged but a taker close isn't worth it yet: work it passively
            # at the convergence target (maker perp buy-back, 0 perp fee).
            self._tp_confirm.pop(pos.id, None)
            journal(
                self.conn,
                f"position {pos.id}: CONVERGED basis={close:.1f}bps -> passive"
                f" exit at {config.CONVERGED_PASSIVE_BPS}bps",
            )
            await self.notifier.alert(
                f"🎯 position {pos.id} {pos.symbol}: basis converged to"
                f" {float(close):.1f}bps — working a passive maker close at"
                f" {float(config.CONVERGED_PASSIVE_BPS):.0f}bps (will cross if a"
                f" taker close turns profitable)"
            )
            self._auto_passive.add(pos.id)
            # Exit requested before the cancel — see _ensure_stops.
            self.positions.set_exit_request(
                pos.id, "passive", config.CONVERGED_PASSIVE_BPS
            )
            await self._cancel_stops_for(pos)
            await self.executor.start_exit(self.positions.get(pos.id))
            return
        self._tp_confirm.pop(pos.id, None)   # still in premium: streak broken
        if pos.opened_ms is not None:
            hold_hours = (time.time() * 1000 - pos.opened_ms) / 3_600_000
            if hold_hours > config.MAX_HOLD_HOURS:
                await self._force_close_timeout(pos)

    async def _check_auto_passive(self, pos: pm.Position) -> None:
        """Manage a convergence-auto passive exit while it works: escalate to a
        taker-taker close if that turns profitable, or stand it back down to
        OPEN if the basis recovers into premium. Operator passive exits are
        never routed here, so they keep their no-auto-escalation guarantee."""
        if pos.exit_mode != "passive":
            # Operator switched it (e.g. /exit now) — release ownership.
            self._auto_passive.discard(pos.id)
            return
        if not self.executor._books_fresh(pos.symbol):
            return  # don't escalate/stand-down on a frozen book
        close = self.executor._close_basis_bps(pos.symbol)
        if close is None:
            return
        # Basis recovered into premium: stop working the close, hand back to
        # OPEN so it keeps collecting funding and max-hold is re-armed.
        if close > config.CONVERGED_PASSIVE_BPS + config.CONVERGED_PASSIVE_RESET_BPS:
            self._auto_passive.discard(pos.id)
            self._tp_confirm.pop(pos.id, None)
            await self._cancel_exit(pos)
            journal(
                self.conn,
                f"position {pos.id}: basis recovered to {close:.1f}bps -> back"
                f" to OPEN (passive close stood down)",
            )
            await self.notifier.alert(
                f"↩️ position {pos.id} {pos.symbol}: basis recovered to"
                f" {float(close):.1f}bps — passive close stood down, holding"
            )
            return
        pnl = self._aggressive_close_pnl(pos)
        if pnl is not None and pnl > 0:
            if not self._tp_confirmed(pos.id):
                return          # same anti-flicker gate as the converged TP
            self._tp_confirm.pop(pos.id, None)
            self._auto_passive.discard(pos.id)
            journal(
                self.conn,
                f"position {pos.id}: ESCALATE passive->taker basis={close:.1f}bps"
                f" est_pnl={pnl:.2f}",
            )
            await self.notifier.alert(
                f"🎯 position {pos.id} {pos.symbol}: basis {float(close):.1f}bps,"
                f" taker close now nets ${float(pnl):+.2f} — crossing to lock it"
            )
            # Request the exit FIRST: _ensure_stops skips a position with
            # an exit requested, so stops cannot be re-placed while the
            # cancel below is waiting on the venue.
            self.positions.set_exit_request(pos.id, "now", None)
            await self._cancel_stops_for(pos)
            await self.executor.start_exit(self.positions.get(pos.id))

    def _tp_confirmed(self, pos_id: int) -> bool:
        """Count consecutive sweeps where a taker close looked profitable.

        A thin book can print a basis hundreds of bps from where a taker order
        actually fills, so one tick must never cross both legs for real. Returns
        True only once the condition has held CONVERGED_TP_CONFIRM_TICKS times.
        """
        n = self._tp_confirm.get(pos_id, 0) + 1
        self._tp_confirm[pos_id] = n
        return n >= config.CONVERGED_TP_CONFIRM_TICKS

    def _aggressive_close_pnl(self, pos: pm.Position) -> Decimal | None:
        """Estimated net PnL of closing taker on both legs right now:
        perp buy-back at the Aster ask, spot sell at the MEXC bid, taker
        fees on both, plus funding accrued, minus fees already paid."""
        pair = self.md.pair_maps.get(pos.symbol)
        if pair is None or pos.perp_entry_avg is None or pos.spot_entry_avg is None:
            return None
        aster = self.md.aster_books.get(pair.aster_symbol)
        mexc = self.md.mexc_books.get(pair.mexc_symbol)
        if aster is None or mexc is None or aster.ask <= 0 or mexc.bid <= 0:
            return None
        perp_pnl = (pos.perp_entry_avg - aster.ask) * pos.perp_qty
        spot_pnl = (mexc.bid - pos.spot_entry_avg) * pos.spot_qty
        exit_fees = (
            pos.perp_qty * aster.ask * config.ASTER_TAKER_FEE
            + pos.spot_qty * mexc.bid * config.MEXC_TAKER_FEE
        )
        return perp_pnl + spot_pnl + pos.funding_usd - pos.fees_usd - exit_fees

    async def _force_close_timeout(self, pos: pm.Position) -> None:
        await self.notifier.alert(
            f"⏰ position {pos.id} {pos.symbol}: max hold"
            f" ({config.MAX_HOLD_HOURS}h) reached — force closing"
        )
        # Request the exit FIRST: _ensure_stops skips a position with
        # an exit requested, so stops cannot be re-placed while the
        # cancel below is waiting on the venue.
        self.positions.set_exit_request(pos.id, "now", None)
        await self._cancel_stops_for(pos)
        await self.executor.start_exit(self.positions.get(pos.id))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    load_env()
    async with aiohttp.ClientSession() as session:
        engine = Engine(session)
        await engine.start()


if __name__ == "__main__":
    asyncio.run(main())
