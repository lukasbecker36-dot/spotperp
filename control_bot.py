"""Telegram control bot: long-polls getUpdates, no third-party bot framework.

Auth: only chat IDs in CONTROL_TELEGRAM_CHAT_IDS may issue commands.
Destructive commands (/stop, /flatten, /live) require the literal word YES
within the same message (e.g. "/flatten YES").

The bot never trades directly: trading commands are queued in the commands
table and executed by the engine process; service control shells out to
systemctl; read commands hit the DB / snapshot files.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import subprocess
import time
from decimal import Decimal, InvalidOperation

import aiohttp

import config
import database
import screener
from auth import load_env, load_telegram_credentials
from database import enqueue_command
from position_manager import PositionManager

log = logging.getLogger("control_bot")

ENGINE_SERVICE = "basis-trade.service"
CONTROL_SERVICE = "basis-trade-control.service"
COMMAND_WAIT_SECONDS = 10

HELP = """Commands:
/screen [n] — top basis opportunities
/funding [n] — top funding carry (24h avg, 8h-equiv)
/status — engine heartbeat + open positions
/positions — active positions detail (bot DB)
/balance — USDT balance on each venue (Aster perp + MEXC spot)
/recon — live exchange P&L: pairs open on the venues now, full round-trip cost
/adopt SYMBOL — import an existing venue carry trade as a managed position
/book SYMBOL — top 5 order book levels on both venues
/refresh — rebuild the cross-listed coin universe (pick up new listings)
/enter SYMBOL NOTIONAL [min_bps] [carry] — start maker entry, or size up an already-OPEN position by NOTIONAL (floor defaults to fee breakeven; 'carry' = funding trade, no auto-close)
/cancel ID|SYMBOL — stop a working entry OR exit, back to OPEN
/exit ID|SYMBOL now [qty] — aggressive close (taker both legs); qty=coins, omit=full
/exit ID|SYMBOL passive [target_bps] [qty] — work maker close; qty=coins, omit=full
/exit ID|SYMBOL cancel — stop a working exit, back to OPEN
/stops SYMBOL — place liq-protection stop (perp) + sell limit (spot) ~1% below liq price (auto-refreshes on size-up)
/remove ID|SYMBOL YES — stop tracking a position closed manually on the exchange (DB only)
/trades [n] — last closed trades (avg venue prices, open/close basis, funding, commission, P&L; default 5)
/pnl — realised P&L summary
/log [n] — last journal lines
/mode — show paper/live
/paper — switch to paper (restarts engine)
/live YES — switch to LIVE (restarts engine)
/start /stop YES /restart — engine service control
/update — git pull + restart engine and control bot (deploy latest code)
/flatten YES — emergency close everything
"""

# Telegram command menu / autocomplete (setMyCommands). Keep in sync with the
# dispatcher in _dispatch(); grouped monitoring -> trading -> position mgmt ->
# mode/service. Telegram limits: name 1-32 chars [a-z0-9_], description 1-256.
BOT_COMMANDS = [
    # monitoring
    {"command": "screen", "description": "Top basis opportunities (5m avg)"},
    {"command": "funding", "description": "Top funding carry (now + 24h avg)"},
    {"command": "positions", "description": "Open positions, P&L + liq proximity"},
    {"command": "recon", "description": "Live venue P&L, funding rate + liq"},
    {"command": "balance", "description": "USDT balance on each venue"},
    {"command": "book", "description": "Order book both venues: SYMBOL"},
    {"command": "status", "description": "Engine status and heartbeat"},
    {"command": "pnl", "description": "Realised P&L summary"},
    {"command": "trades", "description": "Recent closed trades"},
    {"command": "log", "description": "Recent journal entries"},
    # trading
    {"command": "enter", "description": "Enter / size up: SYMBOL NOTIONAL [min_bps] [carry]"},
    {"command": "exit", "description": "Exit: ID|SYMBOL now|passive [bps]"},
    {"command": "cancel", "description": "Cancel working entry or exit: ID|SYMBOL"},
    {"command": "stops", "description": "Liq-protection orders 1% below liq: SYMBOL"},
    {"command": "flatten", "description": "Close all positions (YES)"},
    # position management
    {"command": "adopt", "description": "Import a venue carry trade: SYMBOL"},
    {"command": "remove", "description": "Stop tracking a manually-closed pos: ID|SYMBOL YES"},
    {"command": "refresh", "description": "Rebuild the cross-listed coin universe"},
    # mode + service
    {"command": "mode", "description": "Show paper/live mode"},
    {"command": "paper", "description": "Switch to paper mode"},
    {"command": "live", "description": "Switch to live mode (YES)"},
    {"command": "start", "description": "Start the engine service"},
    {"command": "stop", "description": "Stop the engine service (YES)"},
    {"command": "restart", "description": "Restart the engine service"},
    {"command": "update", "description": "Git pull + restart (deploy latest code)"},
]


class ControlBot:
    def __init__(self, session: aiohttp.ClientSession):
        creds = load_telegram_credentials()
        if not creds.bot_token:
            raise RuntimeError("ALERT_TELEGRAM_BOT_TOKEN not set")
        if not creds.control_chat_ids:
            raise RuntimeError("CONTROL_TELEGRAM_CHAT_IDS not set")
        self._token = creds.bot_token
        self._allowed = set(creds.control_chat_ids)
        self._session = session
        self._conn = database.init_db()
        self._positions = PositionManager(self._conn)
        self._offset = 0

    # ── telegram plumbing ──

    async def _register_commands(self) -> None:
        url = f"https://api.telegram.org/bot{self._token}/setMyCommands"
        async with self._session.post(url, json={"commands": BOT_COMMANDS}) as resp:
            if resp.status == 200:
                log.info("registered %d bot commands", len(BOT_COMMANDS))
            else:
                log.warning("setMyCommands failed: %s", await resp.text())

    async def run(self) -> None:
        log.info("control bot started, allowed chats: %s", self._allowed)
        await self._register_commands()
        while True:
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self._handle_update(update)
            except Exception:
                log.exception("update loop error")
                await asyncio.sleep(5)

    async def _get_updates(self) -> list[dict]:
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        async with self._session.get(
            url,
            params={"timeout": 50, "offset": self._offset + 1},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            payload = await resp.json()
        if not payload.get("ok"):
            log.warning("getUpdates failed: %s", payload)
            await asyncio.sleep(5)
            return []
        updates = payload.get("result", [])
        if updates:
            self._offset = max(u["update_id"] for u in updates)
        return updates

    async def _send(self, chat_id: str, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        for chunk_start in range(0, len(text), 3800):
            chunk = text[chunk_start : chunk_start + 3800]
            async with self._session.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": f"<pre>{html.escape(chunk)}</pre>",
                    "parse_mode": "HTML",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning("sendMessage failed: %s", await resp.text())

    async def _handle_update(self, update: dict) -> None:
        # Only act on NEW messages. Editing an old message (e.g. a historic
        # "/flatten YES") would otherwise re-execute the command.
        message = update.get("message")
        if not message:
            return
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        if chat_id not in self._allowed:
            log.warning("unauthorised chat %s: %s", chat_id, text)
            return
        parts = text.split()
        command = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1:]
        try:
            reply = await self._dispatch(command, args)
        except Exception as exc:
            log.exception("command %s failed", command)
            reply = f"error: {exc}"
        if reply:
            await self._send(chat_id, reply)

    # ── command dispatch ──

    async def _dispatch(self, command: str, args: list[str]) -> str:
        confirmed = bool(args) and args[-1] == "YES"
        if command in ("help", "start_help"):
            return HELP
        if command == "screen":
            return self._cmd_screen(args)
        if command == "funding":
            return self._cmd_funding(args)
        if command == "status":
            return await self._cmd_status()
        if command == "positions":
            return self._cmd_positions()
        if command == "recon":
            return await self._queue_and_wait("recon", {}, wait=25)
        if command == "balance":
            return await self._queue_and_wait("balance", {}, wait=15)
        if command == "book":
            if not args:
                return "usage: /book SYMBOL"
            return await self._queue_and_wait("book", {"symbol": args[0]}, wait=15)
        if command == "adopt":
            if not args:
                return "usage: /adopt SYMBOL (import an existing venue carry trade)"
            return await self._queue_and_wait("adopt", {"symbol": args[0]}, wait=25)
        if command == "refresh":
            return await self._queue_and_wait("refresh", {}, wait=25)
        if command == "stops":
            if not args:
                return ("usage: /stops SYMBOL — place liquidation-protection"
                        " orders (reduce-only buy STOP on Aster + sell LIMIT on"
                        " MEXC) 1% below the perp liq price, full size")
            return await self._queue_and_wait("stops", {"symbol": args[0]}, wait=25)
        if command == "remove":
            if not args:
                return ("usage: /remove ID|SYMBOL YES — stop tracking a position"
                        " you closed manually on the exchange (no venue orders)")
            if not confirmed:
                return ("this stops tracking the position (marks it CLOSED, no"
                        " venue orders) — repeat as: /remove {} YES".format(args[0]))
            return await self._queue_and_wait("remove", {"position_id": args[0]})
        if command == "trades":
            return self._cmd_trades(args)
        if command == "pnl":
            return self._cmd_pnl()
        if command == "log":
            return self._cmd_log(args)
        if command == "mode":
            return f"mode: {'paper' if config.paper_mode() else 'LIVE'}"
        if command == "enter":
            return await self._cmd_enter(args)
        if command == "cancel":
            if not args:
                return "usage: /cancel ID|SYMBOL — stop a working entry or exit"
            return await self._queue_and_wait("cancel", {"position_id": args[0]})
        if command == "exit":
            return await self._cmd_exit(args)
        if command == "flatten":
            if not confirmed:
                return "this closes ALL positions — repeat as: /flatten YES"
            return await self._queue_and_wait("flatten", {})
        if command == "paper":
            config.set_mode(live=False)
            return (await self._systemctl("restart")) + "\nmode set to paper, engine restarting"
        if command == "live":
            if not confirmed:
                return "this enables REAL trading — repeat as: /live YES"
            config.set_mode(live=True)
            return (await self._systemctl("restart")) + "\nmode set to LIVE, engine restarting"
        if command == "start":
            return await self._systemctl("start")
        if command == "stop":
            if not confirmed:
                return "this stops the engine — repeat as: /stop YES"
            return await self._systemctl("stop")
        if command == "restart":
            return await self._systemctl("restart")
        if command == "update":
            return await self._update()
        return f"unknown command /{command}\n\n{HELP}"

    # ── read commands ──

    def _cmd_screen(self, args: list[str]) -> str:
        n = int(args[0]) if args else 10
        snap = screener.read_snapshot()
        age_s = (time.time() * 1000 - snap["ts_ms"]) / 1000 if snap["ts_ms"] else -1
        rows = snap["rows"][:n]
        if not rows:
            return "no screener data (engine running?)"
        win_m = config.SCREEN_AVG_WINDOW_SECONDS / 60.0
        # entry/net are the windowed averages; 'now' is the live net edge so a
        # persistent edge (now ~ net) is distinguishable from a one-tick spike.
        hdr = f"{'symbol':<14}{'entry':>6}{'net':>6}{'now':>6}{'fund':>6}{'depth$':>8}"
        sep = "-" * len(hdr)
        lines = [
            f"screener ({age_s:.0f}s old, {len(snap['rows'])} pairs, {win_m:.0f}m avg)",
            hdr, sep,
        ]
        max_n = 0
        for r in rows:
            sym = r["symbol"][:13]
            entry_avg = r.get("entry_bps_avg", r["entry_bps"])
            net_avg = r.get("net_edge_bps_avg", r["net_edge_bps"])
            max_n = max(max_n, r.get("samples", 0))
            lines.append(
                f"{sym:<14}{entry_avg:>6.1f}{net_avg:>6.1f}{r['net_edge_bps']:>6.1f}"
                f"{r['funding_8h_bps']:>6.2f}{r['max_notional_usd']:>8,.0f}"
            )
        lines.append(sep)
        lines.append(
            f"bps: entry/net = {win_m:.0f}m avg (<={max_n} samples), ranked by net;"
            " now = live net edge, fund = 8h rate"
        )
        return "\n".join(lines)

    def _cmd_funding(self, args: list[str]) -> str:
        n = int(args[0]) if args else 10
        try:
            snap = json.loads(config.FUNDING_SNAPSHOT_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return "no funding data yet (first sweep runs at startup, ~1min)"
        age_s = (time.time() * 1000 - snap["ts_ms"]) / 1000 if snap["ts_ms"] else -1
        rows = snap["rows"][:n]
        if not rows:
            return "no funding data yet"
        win_m = config.SCREEN_AVG_WINDOW_SECONDS / 60.0
        hdr = (f"{'symbol':<12}{'iv':>3}{'24h':>6}{'fund':>6}{'next':>6}"
               f"{'entry':>6}{'net':>6}{'depth$':>8}")
        sep = "-" * len(hdr)
        lines = [f"funding carry ({age_s:.0f}s old)", hdr, sep]

        def avg(r, key_avg, key_live):
            # 5m windowed mean; fall back to live (old snapshot / no samples).
            v = r.get(key_avg)
            return v if v is not None else r.get(key_live)

        for r in rows:
            ev, nv = avg(r, "entry_bps_avg", "entry_bps"), avg(r, "net_edge_bps_avg", "net_edge_bps")
            entry = f"{ev:>6.1f}" if ev is not None else f"{'-':>6}"
            net = f"{nv:>6.1f}" if nv is not None else f"{'-':>6}"
            depth = f"{r['max_notional_usd']:>8,.0f}" if r.get("max_notional_usd") else f"{'-':>8}"
            nh = r.get("next_funding_h")
            nxt = f"{nh:>5.1f}h" if nh is not None and nh >= 0 else f"{'-':>6}"
            lines.append(
                f"{r['symbol'][:11]:<12}{r['interval_hours']:>2}h"
                f"{r['avg_24h_8h_bps']:>6.1f}{r['current_8h_bps']:>6.1f}{nxt}{entry}{net}{depth}"
            )
        lines.append(sep)
        lines.append("iv=interval; 24h=avg carry/8h; fund=settled/8h; next=to settle")
        lines.append(f"entry/net = {win_m:.0f}m avg basis; short perp gets +funding")
        return "\n".join(lines)

    async def _cmd_status(self) -> str:
        try:
            hb = json.loads(config.HEARTBEAT_FILE.read_text())
            age = (time.time() * 1000 - hb["ts_ms"]) / 1000
            engine = (
                f"engine: {'OK' if age < 60 else 'STALE'} (heartbeat {age:.0f}s ago)\n"
                f"mode: {hb['mode']}, pairs: {hb['pairs']},"
                f" active positions: {hb['active_positions']}"
            )
        except (FileNotFoundError, json.JSONDecodeError):
            engine = "engine: NO HEARTBEAT (not running?)"
        result = await self._run(["systemctl", "is-active", ENGINE_SERVICE])
        service = result.stdout.strip() or "unknown"
        return f"{engine}\nservice: {service}\n\n{self._cmd_positions()}"

    def _cmd_positions(self) -> str:
        active = self._positions.active()
        if not active:
            return "no active positions"
        try:
            hb = json.loads(config.HEARTBEAT_FILE.read_text())
            marks = hb.get("marks", {})
            marks_fresh = (time.time() * 1000 - hb["ts_ms"]) < 120_000
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            marks, marks_fresh = {}, False
        lines = ["active positions:"]
        total_notional = 0.0
        total_upnl = 0.0
        for p in active:
            entry = (
                f"{float(p.entry_basis_bps):.1f}" if p.entry_basis_bps else "-"
            )
            held = (
                f"{(time.time() * 1000 - p.opened_ms) / 3600000:.1f}h"
                if p.opened_ms
                else "-"
            )
            kind_tag = " ⚓carry" if p.trade_kind == "carry" else ""
            lines.append(
                f"#{p.id} {p.symbol} [{p.state}]{kind_tag}"
                f" perp={p.perp_qty} spot={p.spot_qty}"
                f" held={held}"
                f"{' exit=' + p.exit_mode if p.exit_mode else ''}"
                f"{' (paper)' if p.paper else ''}"
            )
            if not marks_fresh:
                lines.append(f"   basis {entry} -> ? (engine heartbeat stale)")
                continue
            m = marks.get(str(p.id))
            if m and "upnl_usd" in m:
                liq = ""
                if m.get("liq_dist_pct") is not None:
                    d = m["liq_dist_pct"]
                    warn = " ⚠️" if d < float(config.LIQ_ALERT_PCT) else ""
                    liq = f" | liq +{d:.0f}%{warn}"
                notional = m.get("notional_usd")
                size = f" | ~${notional:,.0f}" if notional else ""
                total_notional += notional or 0.0
                total_upnl += m["upnl_usd"]
                lines.append(
                    f"   basis {entry} -> {m['close_bps']:.1f}bps{size}"
                    f" | uPnL ${m['upnl_usd']:+.2f}"
                    f" (funding ${m['funding_usd']:+.2f},"
                    f" fees ${float(p.fees_usd):.2f}){liq}"
                )
            elif m and "skip" in m:
                lines.append(f"   basis {entry} -> ? ({m['skip']})")
            else:
                lines.append(f"   basis {entry} -> ? (no live mark)")
        if total_notional:
            lines.append(
                f"total: ~${total_notional:,.0f} notional | uPnL ${total_upnl:+.2f}"
            )
        return "\n".join(lines)

    @staticmethod
    def _fmt_px(px: Decimal | None) -> str:
        """Price with enough significant figures for both $60k coins and
        sub-cent microcaps."""
        if px is None:
            return "-"
        f = float(px)
        if f == 0:
            return "0"
        if f >= 100:
            return f"{f:,.2f}"
        if f >= 1:
            return f"{f:.4f}"
        return f"{f:.6g}"

    @staticmethod
    def _basis_bps(perp: Decimal | None, spot: Decimal | None) -> float | None:
        """Executed basis = (perp - spot) / spot in bps."""
        if perp is None or spot is None or spot == 0:
            return None
        return float((perp - spot) / spot) * 10000

    def _cmd_trades(self, args: list[str]) -> str:
        n = int(args[0]) if args else 5
        closed = self._positions.closed(n)
        if not closed:
            return "no closed trades"
        blocks = []
        for p in closed:
            # Premium trade: SHORT perp on Aster (sell to open / buy to close),
            # LONG spot on MEXC (buy to open / sell to close).
            open_basis = self._basis_bps(p.perp_entry_avg, p.spot_entry_avg)
            if open_basis is None and p.entry_basis_bps is not None:
                open_basis = float(p.entry_basis_bps)
            close_basis = self._basis_bps(p.perp_exit_avg, p.spot_exit_avg)
            pnl = (f"${float(p.realized_pnl_usd):+.2f}"
                   if p.realized_pnl_usd is not None else "-")
            when = (time.strftime("%m-%d %H:%M", time.localtime(p.closed_ms / 1000))
                    if p.closed_ms else "-")
            held = (f"{(p.closed_ms - p.opened_ms) / 3_600_000:.1f}h"
                    if p.closed_ms and p.opened_ms else "-")
            ob = f"{open_basis:+.1f}" if open_basis is not None else "-"
            cb = f"{close_basis:+.1f}" if close_basis is not None else "-"
            drift = (f"{open_basis - close_basis:+.1f}"
                     if open_basis is not None and close_basis is not None else "-")
            blocks.append("\n".join([
                f"#{p.id} {p.symbol} {p.state}"
                f"{' (paper)' if p.paper else ''}  {when} · held {held}",
                f"  Aster perp  sell {self._fmt_px(p.perp_entry_avg)}"
                f"  buy {self._fmt_px(p.perp_exit_avg)}",
                f"  MEXC spot   buy  {self._fmt_px(p.spot_entry_avg)}"
                f"  sell {self._fmt_px(p.spot_exit_avg)}",
                f"  basis  open {ob}  close {cb}  captured {drift} bps",
                f"  funding ${float(p.funding_usd):+.2f}"
                f"  commission ${float(p.fees_usd):.2f}"
                f"  →  P&L {pnl}",
            ]))
        return "last trades:\n\n" + "\n\n".join(blocks)

    def _cmd_pnl(self) -> str:
        s = self._positions.pnl_summary()
        return (
            f"realised P&L (USD)\n"
            f"live:  today {float(s['live_today']):+.2f} | all-time {float(s['live_all_time']):+.2f}\n"
            f"paper: today {float(s['paper_today']):+.2f} | all-time {float(s['paper_all_time']):+.2f}"
        )

    def _cmd_log(self, args: list[str]) -> str:
        n = int(args[0]) if args else 20
        rows = self._conn.execute(
            "SELECT ts_ms, level, message FROM journal ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        if not rows:
            return "journal empty"
        lines = []
        for r in reversed(rows):
            ts = time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts_ms"] / 1000))
            lines.append(f"{ts} {r['level']:<5} {r['message']}")
        return "\n".join(lines)

    # ── trading commands (queued to the engine) ──

    async def _cmd_enter(self, args: list[str]) -> str:
        if len(args) < 2:
            return ("usage: /enter SYMBOL NOTIONAL [min_bps] [carry]\n"
                    "min_bps = basis floor while the entry works"
                    " (default: fee breakeven)\n"
                    "carry = funding-carry trade: no auto-close, /exit only"
                    " (default is a convergence trade that auto-closes)\n"
                    "if a position in SYMBOL is already OPEN, this sizes it up"
                    " by NOTIONAL instead of opening a new one")
        try:
            notional = Decimal(args[1])
        except InvalidOperation:
            return f"bad notional: {args[1]}"
        payload: dict = {"symbol": args[0], "notional": str(notional)}
        # Trailing args, any order: a number -> min_bps, 'carry'/'conv' -> kind.
        for tok in args[2:]:
            low = tok.lower()
            if low in ("carry", "funding"):
                payload["kind"] = "carry"
            elif low in ("conv", "converge", "convergence"):
                payload["kind"] = "convergence"
            else:
                try:
                    payload["min_bps"] = str(Decimal(tok))
                except InvalidOperation:
                    return f"unrecognised arg: {tok} (expected min_bps or carry)"
        return await self._queue_and_wait("enter", payload)

    async def _cmd_exit(self, args: list[str]) -> str:
        if len(args) < 2 or args[1] not in ("now", "passive", "cancel"):
            return ("usage: /exit ID|SYMBOL now [qty] |"
                    " passive [target_bps] [qty] | cancel\n"
                    "qty = coins to close (as in /positions); omit = full")
        payload: dict = {"position_id": args[0], "mode": args[1]}
        if args[1] == "now" and len(args) > 2:
            payload["qty"] = args[2]
        elif args[1] == "passive":
            if len(args) > 2:
                payload["target_bps"] = args[2]
            if len(args) > 3:
                payload["qty"] = args[3]
        return await self._queue_and_wait("exit", payload)

    async def _queue_and_wait(
        self, command: str, args: dict, wait: int = COMMAND_WAIT_SECONDS
    ) -> str:
        command_id = enqueue_command(self._conn, command, args)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            row = self._conn.execute(
                "SELECT status, response FROM commands WHERE id=?", (command_id,)
            ).fetchone()
            # 'pending' = not yet picked up, 'running' = engine is executing it;
            # keep waiting until it reaches a terminal state.
            if row and row["status"] not in ("pending", "running"):
                return row["response"] or row["status"]
            await asyncio.sleep(0.5)
        return (
            f"command queued (#{command_id}) but engine has not responded in"
            f" {wait}s — is it running? check /status"
        )

    # ── service control ──

    @staticmethod
    async def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        # Off the event loop: systemctl restart / git pull can take seconds and
        # would otherwise stall getUpdates and every other command.
        return await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True
        )

    async def _systemctl(self, action: str, service: str = ENGINE_SERVICE) -> str:
        result = await self._run(["sudo", "systemctl", action, service])
        if result.returncode != 0:
            return f"systemctl {action} failed: {result.stderr.strip()}"
        return f"systemctl {action} {service}: ok"

    async def _git_pull(self) -> tuple[bool, str]:
        """Fast-forward the deploy checkout to its tracked branch. Returns
        (ok, summary). Pins origin/<current-branch> so it works regardless of
        how tracking is configured on the server."""
        repo = str(config.PROJECT_ROOT)
        head = await self._run(
            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"]
        )
        if head.returncode != 0:
            return False, f"git branch lookup failed: {head.stderr.strip()}"
        branch = head.stdout.strip()
        pull = await self._run(
            ["git", "-C", repo, "pull", "--ff-only", "origin", branch]
        )
        out = (pull.stdout + pull.stderr).strip()
        if pull.returncode != 0:
            return False, f"git pull failed ({branch}):\n{out}"
        return True, f"git pull {branch}: {out.splitlines()[-1] if out else 'ok'}"

    async def _update(self) -> str:
        """Pull the latest code and restart both services. The engine restarts
        synchronously; the control bot (this process) restarts detached after a
        short delay so this reply is delivered before systemd kills us."""
        ok, pull_msg = await self._git_pull()
        if not ok:
            return f"❌ update aborted — {pull_msg}\n(no restart)"
        engine_msg = await self._systemctl("restart", ENGINE_SERVICE)
        # Restart our own service out-of-band: a detached child survives this
        # process being killed, and the sleep lets the Telegram reply flush.
        subprocess.Popen(
            ["bash", "-c", f"sleep 3 && sudo systemctl restart {CONTROL_SERVICE}"],
            start_new_session=True,
        )
        return (
            f"🔄 {pull_msg}\n{engine_msg}\n"
            f"control bot restarting in ~3s (this is the last message from the"
            f" old process) — send /status in a few seconds to confirm both are up"
        )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    load_env()
    async with aiohttp.ClientSession() as session:
        bot = ControlBot(session)
        await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
