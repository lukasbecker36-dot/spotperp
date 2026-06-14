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
/enter SYMBOL NOTIONAL [min_bps] [carry] — start maker entry (floor defaults to fee breakeven; 'carry' = funding trade, no auto-close)
/cancel ID|SYMBOL — abort a working entry
/exit ID|SYMBOL now [qty] — aggressive close (taker both legs); qty=coins, omit=full
/exit ID|SYMBOL passive [target_bps] [qty] — work maker close; qty=coins, omit=full
/exit ID|SYMBOL cancel — stop a working exit, back to OPEN
/trades [n] — last closed trades
/pnl — realised P&L summary
/log [n] — last journal lines
/mode — show paper/live
/paper — switch to paper (restarts engine)
/live YES — switch to LIVE (restarts engine)
/start /stop YES /restart — engine service control
/flatten YES — emergency close everything
"""


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
        commands = [
            {"command": "screen", "description": "Top basis opportunities"},
            {"command": "funding", "description": "Top funding carry (24h avg)"},
            {"command": "enter", "description": "Enter: SYMBOL NOTIONAL [min_bps] [carry]"},
            {"command": "exit", "description": "Exit: ID|SYMBOL now|passive [bps]"},
            {"command": "cancel", "description": "Cancel working entry: ID|SYMBOL"},
            {"command": "positions", "description": "Show open positions"},
            {"command": "balance", "description": "USDT balance on each venue"},
            {"command": "recon", "description": "Live exchange P&L (round-trip cost)"},
            {"command": "adopt", "description": "Import venue carry trade: SYMBOL"},
            {"command": "book", "description": "Order book both venues: SYMBOL"},
            {"command": "status", "description": "Engine status and heartbeat"},
            {"command": "pnl", "description": "Realised P&L summary"},
            {"command": "trades", "description": "Recent closed trades"},
            {"command": "log", "description": "Recent journal entries"},
            {"command": "mode", "description": "Show paper/live mode"},
            {"command": "paper", "description": "Switch to paper mode"},
            {"command": "live", "description": "Switch to live mode (YES)"},
            {"command": "flatten", "description": "Close all positions (YES)"},
        ]
        url = f"https://api.telegram.org/bot{self._token}/setMyCommands"
        async with self._session.post(url, json={"commands": commands}) as resp:
            if resp.status == 200:
                log.info("registered %d bot commands", len(commands))
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
        message = update.get("message") or update.get("edited_message")
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
            return self._cmd_status()
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
            return await self._queue_and_wait("cancel", {"position_id": args[0]})
        if command == "exit":
            return await self._cmd_exit(args)
        if command == "flatten":
            if not confirmed:
                return "this closes ALL positions — repeat as: /flatten YES"
            return await self._queue_and_wait("flatten", {})
        if command == "paper":
            config.set_mode(live=False)
            return self._systemctl("restart") + "\nmode set to paper, engine restarting"
        if command == "live":
            if not confirmed:
                return "this enables REAL trading — repeat as: /live YES"
            config.set_mode(live=True)
            return self._systemctl("restart") + "\nmode set to LIVE, engine restarting"
        if command == "start":
            return self._systemctl("start")
        if command == "stop":
            if not confirmed:
                return "this stops the engine — repeat as: /stop YES"
            return self._systemctl("stop")
        if command == "restart":
            return self._systemctl("restart")
        return f"unknown command /{command}\n\n{HELP}"

    # ── read commands ──

    def _cmd_screen(self, args: list[str]) -> str:
        n = int(args[0]) if args else 10
        snap = screener.read_snapshot()
        age_s = (time.time() * 1000 - snap["ts_ms"]) / 1000 if snap["ts_ms"] else -1
        rows = snap["rows"][:n]
        if not rows:
            return "no screener data (engine running?)"
        hdr = f"{'symbol':<16}{'entry':>6}{'net':>6}{'fund':>6}{'depth$':>8}"
        sep = "-" * len(hdr)
        lines = [f"screener ({age_s:.0f}s old, {len(snap['rows'])} pairs)", hdr, sep]
        for r in rows:
            sym = r["symbol"][:15]
            lines.append(
                f"{sym:<16}{r['entry_bps']:>6.1f}{r['net_edge_bps']:>6.1f}"
                f"{r['funding_8h_bps']:>6.2f}{r['max_notional_usd']:>8,.0f}"
            )
        lines.append(sep)
        lines.append("bps: entry=raw basis, net=after fees, fund=8h rate")
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
        hdr = f"{'symbol':<14}{'iv':>3}{'24h':>7}{'now':>7}{'entry':>7}{'net':>7}{'depth$':>8}"
        sep = "-" * len(hdr)
        lines = [f"funding carry ({age_s:.0f}s old)", hdr, sep]
        for r in rows:
            entry = f"{r['entry_bps']:>7.1f}" if r["entry_bps"] is not None else f"{'-':>7}"
            net = f"{r['net_edge_bps']:>7.1f}" if r["net_edge_bps"] is not None else f"{'-':>7}"
            depth = f"{r['max_notional_usd']:>8,.0f}" if r.get("max_notional_usd") else f"{'-':>8}"
            lines.append(
                f"{r['symbol'][:13]:<14}{r['interval_hours']:>2}h"
                f"{r['avg_24h_8h_bps']:>7.1f}{r['current_8h_bps']:>7.1f}{entry}{net}{depth}"
            )
        lines.append(sep)
        lines.append("iv=funding interval; 24h=avg carry/8h; now=latest/8h")
        lines.append("all bps; short perp receives positive funding")
        return "\n".join(lines)

    def _cmd_status(self) -> str:
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
        service = subprocess.run(
            ["systemctl", "is-active", ENGINE_SERVICE],
            capture_output=True, text=True,
        ).stdout.strip() or "unknown"
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
            m = marks.get(str(p.id)) if marks_fresh else None
            if m:
                lines.append(
                    f"   basis {entry} -> {m['close_bps']:.1f}bps"
                    f" | uPnL ${m['upnl_usd']:+.2f}"
                    f" (funding ${m['funding_usd']:+.2f},"
                    f" fees ${float(p.fees_usd):.2f})"
                )
            else:
                lines.append(f"   basis {entry} -> ? (no live mark)")
        return "\n".join(lines)

    def _cmd_trades(self, args: list[str]) -> str:
        n = int(args[0]) if args else 10
        closed = self._positions.closed(n)
        if not closed:
            return "no closed trades"
        lines = ["last trades:"]
        for p in closed:
            pnl = f"${float(p.realized_pnl_usd):.2f}" if p.realized_pnl_usd is not None else "-"
            lines.append(
                f"#{p.id} {p.symbol} {p.state} pnl={pnl}"
                f" fees=${float(p.fees_usd):.2f} funding=${float(p.funding_usd):.2f}"
                f"{' (paper)' if p.paper else ''}"
            )
        return "\n".join(lines)

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
                    " (default is a convergence trade that auto-closes)")
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
            if row and row["status"] != "pending":
                return row["response"] or row["status"]
            await asyncio.sleep(0.5)
        return (
            f"command queued (#{command_id}) but engine has not responded in"
            f" {wait}s — is it running? check /status"
        )

    # ── service control ──

    def _systemctl(self, action: str) -> str:
        result = subprocess.run(
            ["sudo", "systemctl", action, ENGINE_SERVICE],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return f"systemctl {action} failed: {result.stderr.strip()}"
        return f"systemctl {action} {ENGINE_SERVICE}: ok"


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
