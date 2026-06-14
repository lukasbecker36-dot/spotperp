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
from decimal import Decimal

import aiohttp

import config
import database
import funding
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
from database import journal, pending_commands, resolve_command
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

    # ── startup ──

    async def start(self) -> None:
        mode = "paper" if self.paper else "LIVE"
        journal(self.conn, f"engine starting in {mode} mode")
        await self._load_symbol_maps()
        await recovery.reconcile(
            self.conn, self.positions, self.aster, self.mexc, self.notifier,
            paper=self.paper,
        )
        await self._refresh_books()
        await self._refresh_funding()
        await self._refresh_funding_stats()
        self._resume_positions()
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

    async def _load_symbol_maps(self) -> None:
        aster_info, mexc_info = await asyncio.gather(
            self.aster.exchange_info(), self.mexc.exchange_info()
        )
        self.md.aster_info = aster_info
        self.md.mexc_info = mexc_info
        self.md.pair_maps = screener.build_pair_maps(
            set(aster_info), set(mexc_info)
        )
        log.info(
            "symbol maps: %d aster, %d mexc, %d tradeable pairs",
            len(aster_info), len(mexc_info), len(self.md.pair_maps),
        )
        journal(self.conn, f"{len(self.md.pair_maps)} cross-listed USDT pairs")

    def _resume_positions(self) -> None:
        for pos in self.positions.active():
            if pos.state == pm.EXITING:
                log.info("resuming exit for position %s", pos.id)
                self.executor.start_exit(pos)
            elif pos.state == pm.UNWINDING:
                journal(
                    self.conn,
                    f"position {pos.id} stuck UNWINDING at startup — manual check",
                    "ERROR",
                )

    # ── loops ──

    async def _market_loop(self) -> None:
        last_slow = 0.0
        while True:
            try:
                await self._refresh_books()
                if time.monotonic() - last_slow >= config.SLOW_SCAN_SECONDS:
                    last_slow = time.monotonic()
                    await self._refresh_funding()
                    self._write_screener_snapshot()
                    self._write_funding_snapshot()
                    self._write_heartbeat()
            except Exception:
                log.exception("market loop error")
            await asyncio.sleep(config.POLL_INTERVAL_SECONDS)

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

    async def _funding_loop(self) -> None:
        while True:
            await asyncio.sleep(config.FUNDING_REFRESH_SECONDS)
            try:
                await self._refresh_funding_stats()
            except Exception:
                log.exception("funding stats sweep error")
            try:
                await self._refresh_position_funding()
            except Exception:
                log.exception("position funding refresh error")

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

    def _write_screener_snapshot(self) -> None:
        now = int(time.time() * 1000)
        rows = []
        for sym, pair in self.md.pair_maps.items():
            aster = self.md.aster_books.get(pair.aster_symbol)
            mexc = self.md.mexc_books.get(pair.mexc_symbol)
            if aster is None or mexc is None:
                continue
            funding_rate = (
                self.md.funding.get(pair.aster_symbol) or {}
            ).get("funding_rate")
            stat = self.md.funding_stats.get(pair.aster_symbol)
            interval = stat.interval_hours if stat else 8
            row = screener.compute_row(
                pair, aster, mexc, funding_rate, now_ms=now,
                funding_interval_hours=interval,
            )
            if row is not None:
                rows.append(row)
        screener.write_snapshot(screener.rank_rows(rows))
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
        new_file = not path.exists()
        try:
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow([
                        "ts_ms", "symbol", "entry_bps", "close_bps",
                        "funding_8h_bps", "max_notional_usd",
                    ])
                for r in rows:
                    w.writerow([
                        r.ts_ms, r.symbol, f"{r.entry_bps:.2f}",
                        f"{r.close_bps:.2f}", f"{r.funding_8h_bps:.2f}",
                        f"{r.max_notional_usd:.0f}",
                    ])
        except OSError:
            log.exception("basis log write failed")

    def _write_funding_snapshot(self) -> None:
        """Persist funding stats joined with live basis/depth, ranked by 24h
        average carry, for the bot's /funding command."""
        now = int(time.time() * 1000)
        rows = []
        for sym, stat in self.md.funding_stats.items():
            pair = self.md.pair_maps.get(sym)
            if pair is None:
                continue
            aster = self.md.aster_books.get(pair.aster_symbol)
            mexc = self.md.mexc_books.get(pair.mexc_symbol)
            screen = None
            if aster is not None and mexc is not None:
                screen = screener.compute_row(
                    pair, aster, mexc,
                    (self.md.funding.get(sym) or {}).get("funding_rate"),
                    now_ms=now, funding_interval_hours=stat.interval_hours,
                )
            rows.append({
                "symbol": sym,
                "interval_hours": stat.interval_hours,
                "current_8h_bps": stat.current_8h_bps,
                "avg_24h_8h_bps": stat.avg_24h_8h_bps,
                "realized_24h_bps": stat.realized_24h_bps,
                "entry_bps": screen.entry_bps if screen else None,
                "net_edge_bps": screen.net_edge_bps if screen else None,
                "max_notional_usd": screen.max_notional_usd if screen else 0.0,
            })
        rows.sort(key=lambda r: r["avg_24h_8h_bps"], reverse=True)
        config.FUNDING_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts_ms": now, "rows": rows[: config.SCREENER_TOP_N]}
        tmp = config.FUNDING_SNAPSHOT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        tmp.replace(config.FUNDING_SNAPSHOT_FILE)

    def _position_marks(self) -> dict[str, dict]:
        """Live marks for open positions: closeable basis now, accrued-funding
        estimate and unrealized P&L at passive-exit touch prices."""
        now_ms = int(time.time() * 1000)
        marks: dict[str, dict] = {}
        for pos in self.positions.active():
            if pos.perp_qty <= 0 or pos.perp_entry_avg is None:
                continue
            pair = self.md.pair_maps.get(pos.symbol)
            if pair is None:
                continue
            aster = self.md.aster_books.get(pair.aster_symbol)
            mexc = self.md.mexc_books.get(pair.mexc_symbol)
            if aster is None or mexc is None or aster.bid <= 0 or mexc.bid <= 0:
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
            marks[str(pos.id)] = {
                "close_bps": float(close_bps),
                "funding_usd": float(funding_est),
                "upnl_usd": float(upnl),
            }
        return marks

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
                for row in pending_commands(self.conn):
                    response = await self._handle_command(
                        row["command"], json.loads(row["args"])
                    )
                    resolve_command(self.conn, row["id"], "done", response)
            except Exception:
                log.exception("command loop error")
            await asyncio.sleep(config.COMMAND_POLL_SECONDS)

    async def _handle_command(self, command: str, args: dict) -> str:
        try:
            if command == "enter":
                return self._cmd_enter(args)
            if command == "exit":
                return self._cmd_exit(args)
            if command == "cancel":
                return self._cmd_cancel(args)
            if command == "flatten":
                return self._cmd_flatten()
            if command == "recon":
                return await self._cmd_recon()
            if command == "book":
                return await self._cmd_book(args)
            if command == "adopt":
                return await self._cmd_adopt(args)
            if command == "balance":
                return await self._cmd_balance()
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
        active = [p for p in self.positions.active() if p.state != pm.UNWINDING]
        if any(p.symbol == symbol for p in active):
            return f"already have an active position in {symbol}"
        min_bps = (
            Decimal(str(args["min_bps"])) if args.get("min_bps") is not None else None
        )
        kind = "carry" if str(args.get("kind", "")).lower() == "carry" else "convergence"
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
            f"auto-closes on convergence TP / max-hold{adverse}"
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

    def _cmd_exit(self, args: dict) -> str:
        pos = self._resolve_position(args["position_id"])
        if isinstance(pos, str):
            return pos
        if pos.state not in (pm.OPEN, pm.EXITING):
            return f"position {pos.id} is {pos.state}, cannot exit"
        mode = args.get("mode", "now")
        if mode == "cancel":
            task = self.executor._tasks.get(pos.id)
            if task and not task.done():
                task.cancel()
            self.positions.set_exit_request(pos.id, None, None)
            self.positions.set_state(pos.id, pm.OPEN, "exit cancelled by operator")
            return f"position {pos.id}: exit cancelled, back to OPEN"
        target = args.get("target_bps")
        target_dec = Decimal(str(target)) if target is not None else (
            config.EXIT_BASIS_BPS if mode == "passive" else None
        )
        self.positions.set_exit_request(pos.id, mode, target_dec)
        self.executor.start_exit(self.positions.get(pos.id))
        desc = "aggressive (taker both legs)" if mode == "now" else (
            f"passive maker, target {target_dec}bps"
        )
        return f"position {pos.id}: exit started — {desc}"

    def _cmd_cancel(self, args: dict) -> str:
        pos = self._resolve_position(args["position_id"])
        if isinstance(pos, str):
            return pos
        if self.executor.request_cancel(pos.id):
            return f"position {pos.id}: entry cancel requested"
        return f"position {pos.id}: no working entry task"

    def _cmd_flatten(self) -> str:
        count = 0
        for pos in self.positions.active():
            if pos.state in (pm.PENDING_ENTRY, pm.ENTERING):
                self.executor.request_cancel(pos.id)
                count += 1
            elif pos.state == pm.OPEN:
                self.positions.set_exit_request(pos.id, "now", None)
                self.executor.start_exit(self.positions.get(pos.id))
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
        return book.format_book(symbol, pair.qty_multiplier, aster_depth, mexc_depth)

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
        )

    async def _safety_loop(self) -> None:
        while True:
            try:
                for pos in self.positions.active():
                    if pos.state != pm.OPEN:
                        continue
                    await self._check_safety(pos)
            except Exception:
                log.exception("safety loop error")
            await asyncio.sleep(config.POLL_INTERVAL_SECONDS * 5)

    async def _check_safety(self, pos: pm.Position) -> None:
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
                self.positions.set_exit_request(pos.id, "now", None)
                self.executor.start_exit(self.positions.get(pos.id))
                return
        # Carry trades are held for funding and only the operator closes them:
        # skip the convergence take-profit and the max-hold timeout. The
        # adverse-widen stop above still applies (perp-liquidation protection).
        if pos.trade_kind == "carry":
            return
        if close is not None and close <= config.CONVERGED_TP_BPS:
            pnl = self._aggressive_close_pnl(pos)
            if pnl is not None and pnl > 0:
                journal(
                    self.conn,
                    f"position {pos.id}: CONVERGED TP basis={close:.1f}bps"
                    f" est_pnl={pnl:.2f}",
                )
                await self.notifier.alert(
                    f"🎯 position {pos.id} {pos.symbol}: basis inverted to"
                    f" {float(close):.1f}bps, taker close nets"
                    f" ${float(pnl):+.2f} — taking profit"
                )
                self.positions.set_exit_request(pos.id, "now", None)
                self.executor.start_exit(self.positions.get(pos.id))
                return
        if pos.opened_ms is not None:
            hold_hours = (time.time() * 1000 - pos.opened_ms) / 3_600_000
            if hold_hours > config.MAX_HOLD_HOURS:
                await self._force_close_timeout(pos)

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
        self.positions.set_exit_request(pos.id, "now", None)
        self.executor.start_exit(self.positions.get(pos.id))


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
