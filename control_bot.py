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
import unicodedata
from decimal import Decimal, InvalidOperation

import aiohttp

import advisor
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
/screen diff [n] — pairs furthest ABOVE their own 24h avg basis (reversion candidates)
/screen swing [n] — pairs that go wide then return to closeable (round-trip candidates)
/screen fill [n] — pairs with real perp volume + a persistently workable basis (you can actually get filled)
/funding [n] — top funding carry (bps per hour)
/status — engine heartbeat + open positions
/review — AI review of positions + opportunities (advisory only, never trades)
/positions — active positions detail (bot DB)
/orders — working entries/exits: level waited for, level now, 24h range (stops excluded)
/balance — USDT balance on each venue (Aster perp + MEXC spot)
/recon — live exchange P&L: pairs open on the venues now, full round-trip cost
/adopt SYMBOL — import an existing venue carry trade as a managed position
/book SYMBOL — top 5 order book levels on both venues
/refresh — rebuild the cross-listed coin universe (pick up new listings)
/enter SYMBOL NOTIONAL [min_bps] [carry] — start maker entry, or size up an already-OPEN position by NOTIONAL (floor defaults to fee breakeven; 'carry' = funding trade, no auto-close)
/cancel ID|SYMBOL — stop a working entry OR exit, back to OPEN
/exit ID|SYMBOL now [qty] — aggressive close (taker both legs); qty=coins or $500, omit=full
/exit ID|SYMBOL passive [target_bps] [qty] — work maker close; qty=coins or $500, omit=full
/exit ID|SYMBOL cancel — stop a working exit, back to OPEN
/stops SYMBOL — place liq-protection stop (perp) + sell limit (spot) ~1% below liq price (auto-placed on new positions and re-armed after a size change / part-reduce; AUTO_STOPS=0 to disable)
/remove ID|SYMBOL YES — stop tracking a position closed manually on the exchange (DB only)
/truefill ID|SYMBOL — re-price a mark-booked exit (ADL / lost stop) from Aster's real trades and recompute P&L
/recompute ID|SYMBOL — re-derive quantities, averages and entry basis from the fill history (DB only)
/trades [n] — last closed trades (avg venue prices, open/close basis, funding, commission, P&L; default 5)
/pnl — realised P&L summary (closed trades only)
/equity [days] — total account value (perp margin + upnl, spot coins, USDT), daily change table and chart
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
    {"command": "screen", "description": "Basis opportunities; 'diff' vs 24h avg, 'swing' round-trip"},
    {"command": "funding", "description": "Top funding carry (now + 24h avg)"},
    {"command": "positions", "description": "Open positions, P&L + liq proximity"},
    {"command": "recon", "description": "Live venue P&L, funding rate + liq"},
    {"command": "balance", "description": "USDT balance on each venue"},
    {"command": "book", "description": "Order book both venues: SYMBOL"},
    {"command": "status", "description": "Engine status and heartbeat"},
    {"command": "review", "description": "AI review of book + opportunities (advisory)"},
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



def _hourly(bps_8h: float | None) -> float:
    """Funding is computed and stored 8h-equivalent (contracts settle on
    different intervals, so an 8h normalisation is the only way to add them
    up). Displaying it that way makes rates hard to compare against a holding
    period measured in hours, so every board shows bps PER HOUR."""
    return (bps_8h or 0.0) / 8.0


def _pad(text: str, width: int) -> str:
    """Left-pad to WIDTH *display* columns, not code points.

    Telegram renders <pre> in a monospace font where CJK glyphs occupy two
    cells. "哈基米USDT" is 9 characters but 14 columns wide, so a plain
    f"{sym:<11}" shunts the rest of that row right and the board stops lining
    up — and the CJK-named pairs are exactly the ones topping this screen.
    """
    cells = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    while cells > width and text:
        text = text[:-1]
        cells = sum(
            2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text
        )
    return text + " " * (width - cells)


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
        if command == "review":
            return await advisor.review_text(self._session, self._conn)
        if command == "positions":
            return self._cmd_positions()
        if command == "equity":
            days = args[0] if args else None
            return await self._queue_and_wait(
                "equity", {"days": days}, wait=30
            )
        if command == "recompute":
            if not args:
                return ("usage: /recompute ID|SYMBOL — re-derive a position's"
                        " quantities, averages and entry basis from its fills."
                        " Corrects the book only; places no orders.")
            return await self._queue_and_wait(
                "recompute", {"position_id": args[0]}, wait=25
            )
        if command == "truefill":
            if not args:
                return ("usage: /truefill ID|SYMBOL — re-price an exit that was"
                        " booked at mark (ADL / lost stop) from Aster's real"
                        " trade record, then recompute P&L. Corrects the book"
                        " only; places no orders.")
            return await self._queue_and_wait(
                "truefill", {"position_id": args[0]}, wait=30
            )
        if command == "orders":
            return await self._queue_and_wait("orders", {}, wait=25)
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
        if args and args[0].lower() in ("diff", "d"):
            return self._cmd_screen_diff(args[1:])
        if args and args[0].lower() in ("swing", "s"):
            return self._cmd_screen_swing(args[1:])
        if args and args[0].lower() in ("fill", "f"):
            return self._cmd_screen_fill(args[1:])
        n = int(args[0]) if args else 10
        snap = screener.read_snapshot()
        age_s = (time.time() * 1000 - snap["ts_ms"]) / 1000 if snap["ts_ms"] else -1
        rows = snap["rows"][:n]
        if not rows:
            return "no screener data (engine running?)"
        win_m = config.SCREEN_AVG_WINDOW_SECONDS / 60.0
        # entry/net are the windowed averages; 'now' is the live net edge so a
        # persistent edge (now ~ net) is distinguishable from a one-tick spike.
        hdr = (f"{'symbol':<14}{'entry':>6}{'24h':>7}{'net':>6}{'now':>6}"
               f"{'fund':>6}{'depth$':>8}")
        sep = "-" * len(hdr)
        lines = [
            f"screener ({age_s:.0f}s old, {len(snap['rows'])} pairs, {win_m:.0f}m avg)",
            hdr, sep,
        ]
        max_n = 0
        max_h = 0.0
        for r in rows:
            sym = r["symbol"][:13]
            entry_avg = r.get("entry_bps_avg", r["entry_bps"])
            net_avg = r.get("net_edge_bps_avg", r["net_edge_bps"])
            # 24h mean entry basis: entry >> 24h = dislocated (room to revert);
            # entry ~ 24h = this pair's normal level, so nothing to converge.
            # A '?' marks too little history to be a real norm — DailyBasis
            # falls back to the live basis there, which would otherwise look
            # like a genuine 24h average (and is why such a pair is absent
            # from /screen diff).
            d24 = r.get("entry_bps_avg_24h")
            hrs = r.get("hours_24h", 0.0) or 0.0
            if d24 is None:
                d24_s = f"{'-':>7}"
            elif hrs < config.SCREEN_DIFF_MIN_HOURS:
                d24_s = f"{f'{d24:.1f}?':>7}"
            else:
                d24_s = f"{d24:>7.1f}"
            max_n = max(max_n, r.get("samples", 0))
            max_h = max(max_h, r.get("hours_24h", 0.0) or 0.0)
            lines.append(
                f"{sym:<14}{entry_avg:>6.1f}{d24_s}{net_avg:>6.1f}"
                f"{r['net_edge_bps']:>6.1f}"
                f"{_hourly(r['funding_8h_bps']):>6.2f}"
                f"{r['max_notional_usd']:>8,.0f}"
            )
        lines.append(sep)
        lines.append(
            f"bps: entry/net = {win_m:.0f}m avg (<={max_n} samples), ranked by net;"
            " now = live net edge, fund = funding in bps per HOUR"
        )
        lines.append(
            f"24h = 24h avg entry basis (<={max_h:.0f}h history)."
            " entry >> 24h = elevated, likely to revert;"
            " entry ~ 24h = this pair's normal level, won't converge"
        )
        lines.append(
            f"? = under {config.SCREEN_DIFF_MIN_HOURS:.0f}h of history, so that"
            " figure is the live basis, not a norm — excluded from /screen diff"
        )
        return "\n".join(lines)

    def _cmd_screen_diff(self, args: list[str]) -> str:
        """Pairs whose 5m entry basis sits furthest ABOVE their own 24h mean.

        /screen ranks by absolute net edge, which hides a pair that is wildly
        dislocated but not especially rich. This is the reversion view: a +5
        basis on a pair that normally sits at -50 is a 55bps gap to capture IF
        it reverts.
        """
        n = int(args[0]) if args else 10
        snap = screener.read_snapshot()
        age_s = (time.time() * 1000 - snap["ts_ms"]) / 1000 if snap["ts_ms"] else -1
        rows = (snap.get("diff_rows") or [])[:n]
        if not rows:
            return (
                "no dislocation data yet — needs"
                f" {config.SCREEN_DIFF_MIN_HOURS:.0f}h of 24h basis history"
                " (the engine seeds it from the basis logs at startup)."
                " If you just deployed, give it a slow scan."
            )
        win_m = config.SCREEN_AVG_WINDOW_SECONDS / 60.0
        hdr = (f"{'symbol':<14}{'entry':>6}{'24h':>7}{'diff':>7}{'net':>6}"
               f"{'depth$':>8}{'hrs':>5}")
        sep = "-" * len(hdr)
        lines = [f"dislocation screen ({age_s:.0f}s old)", hdr, sep]
        for r in rows:
            entry_avg = r.get("entry_bps_avg", r["entry_bps"])
            d24 = r.get("entry_bps_avg_24h", entry_avg)
            lines.append(
                f"{r['symbol'][:13]:<14}{entry_avg:>6.1f}{d24:>7.1f}"
                f"{entry_avg - d24:>+7.1f}{r['net_edge_bps_avg']:>6.1f}"
                f"{r['max_notional_usd']:>8,.0f}{r.get('hours_24h', 0):>5.0f}"
            )
        lines.append(sep)
        lines.append(
            f"diff = {win_m:.0f}m avg entry basis - 24h avg, ranked biggest first."
        )
        lines.append(
            "A positive diff means the basis is ABOVE its own norm: short perp /"
            " long spot profits if it reverts. Reversion is a HYPOTHESIS — the"
            " divergence backtest found this hard to capture net of spreads, so"
            " check /book depth and the exit basis before entering."
        )
        return "\n".join(lines)


    def _cmd_screen_swing(self, args: list[str]) -> str:
        """Pairs that go WIDE and then come back — the round-trip profile.

        /screen ranks by absolute edge and /screen diff by distance from the
        mean; neither asks whether a pair ever becomes closeable. This ranks by
        (entry now - the pair's own 24h low), and only lists pairs whose low
        actually reaches an exitable level with funding paying you to wait.
        """
        n = int(args[0]) if args else 10
        snap = screener.read_snapshot()
        age_s = (time.time() * 1000 - snap["ts_ms"]) / 1000 if snap["ts_ms"] else -1
        rows = (snap.get("swing_rows") or [])[:n]
        if not rows:
            return (
                "no swing candidates — needs"
                f" {config.SCREEN_DIFF_MIN_HOURS:.0f}h of basis history, a 24h low"
                f" reaching {config.SCREEN_SWING_EXIT_BPS:.0f}bps or below, and"
                " non-negative funding. If you just deployed, give it a slow scan."
            )
        hdr = (f"{'symbol':<13}{'entry':>7}{'lo24':>7}{'hi24':>7}{'capt':>7}"
               f"{'fund':>6}{'dep$':>7}")
        sep = "-" * len(hdr)
        lines = [f"swing screen ({age_s:.0f}s old)", hdr, sep]
        for r in rows:
            entry_avg = r.get("entry_bps_avg", r["entry_bps"])
            lo = r.get("basis_p10_24h", entry_avg)
            hi = r.get("basis_p90_24h", entry_avg)
            lines.append(
                f"{r['symbol'][:12]:<13}{entry_avg:>7.1f}{lo:>7.1f}{hi:>7.1f}"
                f"{entry_avg - lo:>+7.1f}{_hourly(r['funding_8h_bps']):>6.2f}"
                f"{r['max_notional_usd']:>7,.0f}"
            )
        lines.append(sep)
        lines.append(
            "lo24/hi24 = p10/p90 of HOURLY MEAN basis (intra-hour flicker averages"
            " out, so this is a real swing not a noisy quote)."
        )
        lines.append(
            f"capt = entry now - lo24: the round trip if it returns to its own low."
            f" Listed only if lo24 <= {config.SCREEN_SWING_EXIT_BPS:.0f}bps (it"
            " actually becomes closeable) and funding >= 0 (paid to wait)."
        )
        lines.append(
            "Past oscillation is not a promise it repeats — check /book depth and"
            " the exit basis before entering."
        )
        return "\n".join(lines)


    def _cmd_screen_fill(self, args: list[str]) -> str:
        """Names you can realistically get FILLED on.

        /screen ranks by the size of the edge, and its depth filter only proves
        the book isn't empty. But an entry rests as a maker — it fills when a
        taker lifts it. A wide basis on a symbol nobody trades never fills,
        which is why the richest rows are often the hardest to enter.
        """
        n = int(args[0]) if args else 10
        snap = screener.read_snapshot()
        age_s = (time.time() * 1000 - snap["ts_ms"]) / 1000 if snap["ts_ms"] else -1
        rows = (snap.get("fill_rows") or [])[:n]
        if not rows:
            # Say WHICH gate is empty rather than listing both: no volume at all
            # means the 24h ticker call is failing, which is a bug, not a market.
            main = snap.get("rows") or []
            if main and not any(r.get("perp_volume_24h") for r in main):
                return (
                    "no fillable candidates — and NO perp volume data at all, so"
                    " the Aster 24h ticker call is failing. Check:"
                    " journalctl -u basis-trade | grep -i 'perp 24h volume'"
                )
            best_hrs = max((r.get("hours_tradeable_24h", 0) or 0) for r in main) if main else 0
            return (
                "no fillable candidates — needs 24h perp volume >= "
                f"${config.SCREEN_FILL_MIN_VOLUME_USD:,.0f} and the basis workable"
                f" for >= {config.SCREEN_FILL_MIN_HOURS:.0f}h of the last 24"
                f" (best right now: {best_hrs:.0f}h), and funding at or above"
            f" {_hourly(config.SCREEN_FILL_MIN_FUNDING_BPS):+.2f}bps/h funding."
                " If you just deployed, give it a slow scan — the dwell hours"
                " build from the seeded basis history."
            )

        def vol(v: float) -> str:
            if v >= 1e9:
                return f"{v / 1e9:.1f}B"
            if v >= 1e6:
                return f"{v / 1e6:.1f}M"
            if v >= 1e3:
                return f"{v / 1e3:.0f}k"
            return f"{v:.0f}"

        hdr = (f"{'symbol':<11}{'score':>6}{'entry':>6}{'lo24':>7}{'net':>7}"
               f"{'fund':>6}{'hrs':>4}{'tr/h':>6}{'jit':>5}{'$clip':>6}")
        sep = "-" * len(hdr)
        lines = [f"fillability screen ({age_s:.0f}s old)", hdr, sep]
        for r in rows:
            entry_avg = r.get("entry_bps_avg", r["entry_bps"])
            lo = r.get("basis_p10_24h", entry_avg)
            jit = r.get("entry_bps_jitter", 0.0)
            jit_s = f"{jit:>5.1f}" if r.get("samples", 0) >= 3 else f"{'-':>5}"
            net = entry_avg - lo - float(config.ENTRY_MIN_EDGE_FLOOR_BPS)
            chances = (r.get("hours_tradeable_24h", 0.0) or 0.0) * (
                (r.get("perp_trades_24h", 0.0) or 0.0) / 24.0
            )
            factor = min(
                1.0, chances / max(config.SCREEN_FILL_TARGET_CHANCES, 1.0)
            )
            score = (net - jit) * factor
            # A lo24 well above 0 means the basis never comes back to a level
            # you can exit at — the trip is a carry, not a round trip.
            lo_s = (f"{f'{lo:.1f}!':>7}"
                    if lo > config.SCREEN_FILL_FLAG_LO_BPS else f"{lo:>7.1f}")
            lines.append(
                f"{_pad(r['symbol'], 11)}{score:>+6.1f}{entry_avg:>6.1f}{lo_s}"
                f"{net:>+7.1f}{_hourly(r.get('funding_8h_bps')):>+6.2f}"
                f"{r.get('hours_tradeable_24h', 0):>4.0f}"
                f"{r.get('perp_trades_24h', 0) / 24.0:>6.1f}{jit_s}"
                f"{net * r['max_notional_usd'] / 10000.0:>6.1f}"
            )
        lines.append(sep)
        lines.append(
            "Ranked by score = (net - jit) x min(1, hrs x tr/h /"
            f" {config.SCREEN_FILL_TARGET_CHANCES:.0f}): what the trip is worth"
            " after costs, haircut by how much the basis moves against a"
            " resting order, scaled down if there is too little taker flow to"
            " lift it. It is the whole board in one number — read the columns"
            " only to see WHY a row scores what it does."
        )
        lines.append(
            "Score is NOT weighted by depth, on purpose: top-of-book"
            " understates exactly the names worth trading here. Size is a"
            " separate question — read $clip."
        )
        lines.append(
            "hrs = hours of the last 24 the basis cleared the entry floor."
            " tr/h = Aster perp trades per hour (flow, not resting depth — this"
            " is what lifts a maker). Full 24h volume is in /book."
        )
        lines.append(
            f"net = (entry - lo24) - {float(config.ENTRY_MIN_EDGE_FLOOR_BPS):.0f}bps"
            " round-trip cost: what the trip is actually WORTH. lo24 is where"
            " to set your exit target — a lo24 of +32 will never fill an /exit"
            f" at 0; '!' marks lo24 above"
            f" +{config.SCREEN_FILL_FLAG_LO_BPS:.0f}, i.e. a basis that never"
            " comes back to flat. Rows under"
            f" +{config.SCREEN_FILL_MIN_NET_SWING_BPS:.0f} net are dropped as"
            " not worth working."
        )
        lines.append(
            "$clip = net x TOP-OF-BOOK depth: what one clip at the touch is"
            " worth. It UNDERSTATES a name you work over time — STONK shows ~$8"
            " at the touch yet fills $42-99 clips — so read it with hrs, which"
            " is what such a name actually trades on."
        )
        lines.append(
            "fund = funding in bps per HOUR; + means the SHORT receives, so"
            " it pays you to wait while the round trip works. Rows below"
            f" {_hourly(config.SCREEN_FILL_MIN_FUNDING_BPS):+.2f} are dropped:"
            " a negative rate makes the wait a cost and takes time off your"
            " side."
        )
        lines.append(
            "score no longer subtracts jit. Measured over 612 entry and 481"
            " exit clips, slippage is flat in jitter and scales with CLIP SIZE"
            " instead — and on exits the jitteriest quartile locked BETTER"
            " than quoted. The cost is charged in the floor now."
        )
        lines.append(
            f"jit = mean bps the basis moves BETWEEN 5m samples. A smooth drift"
            f" scores low; flicker scores high. Rows above"
            f" {config.SCREEN_MAX_BASIS_JITTER_BPS:.0f} are dropped as"
            " untradeable — calibrate off a name you trade happily."
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
        # No interval column: normalising to a per-hour rate is precisely what
        # makes the settlement interval stop mattering for comparison. 'next'
        # still says when the cash lands, and /book has the interval.
        hdr = (f"{'symbol':<11}{'score':>6}{'24h':>7}{'fund':>6}"
               f"{'next':>5}{'entry':>6}{'lo24':>7}{'hi24':>7}{'jit':>5}"
               f"{'depth$':>7}")
        sep = "-" * len(hdr)
        lines = [f"funding carry ({age_s:.0f}s old)", hdr, sep]

        def avg(r, key_avg, key_live):
            # 5m windowed mean; fall back to live (old snapshot / no samples).
            v = r.get(key_avg)
            return v if v is not None else r.get(key_live)

        floor = float(config.ENTRY_MIN_EDGE_FLOOR_BPS)
        for r in rows:
            ev = avg(r, "entry_bps_avg", "entry_bps")
            entry = f"{ev:>6.1f}" if ev is not None else f"{'-':>6}"
            jv = r.get("entry_bps_jitter")
            jit = (f"{jv:>5.1f}" if jv is not None and r.get("samples", 0) >= 3
                   else f"{'-':>5}")
            depth = f"{r['max_notional_usd']:>6,.0f}" if r.get("max_notional_usd") else f"{'-':>6}"
            nh = r.get("next_funding_h")
            nxt = f"{nh:>4.1f}h" if nh is not None and nh >= 0 else f"{'-':>5}"
            # Where the live basis sits inside the pair's own 24h range. A '?'
            # marks too little history for the range to be a norm — DailyBasis
            # falls back to the live basis there, which would otherwise read as
            # a genuine high and low.
            hrs24 = r.get("hours_24h") or 0.0
            thin = hrs24 < config.SCREEN_DIFF_MIN_HOURS

            def rng(v, w):
                if v is None:
                    return f"{'-':>{w}}"
                return f"{f'{v:.0f}?':>{w}}" if thin else f"{v:>{w}.1f}"

            lo24 = rng(r.get("basis_p10_24h"), 7)
            hi24 = rng(r.get("basis_p90_24h"), 7)
            sc = r.get("score")
            # The score subtracts jit, but jit needs 3 samples to exist. After
            # a restart every row scores with a zero haircut, which flatters
            # the flickery ones most — mark it rather than hide it.
            if sc is None:
                score = f"{'-':>6}"
            elif r.get("samples", 0) < 3:
                score = f"{f'{sc:+.0f}*':>6}"
            else:
                score = f"{sc:>+6.0f}"
            lines.append(
                f"{_pad(r['symbol'], 11)}{score}"
                f"{_hourly(r['avg_24h_8h_bps']):>7.2f}"
                f"{_hourly(r['current_8h_bps']):>7.2f}{nxt}"
                f"{entry}{lo24}{hi24}{jit}{depth}"
            )
        lines.append(sep)
        lines.append(
            f"score = bps from entering now and holding"
            f" {config.FUNDING_SCORE_HOLD_HOURS:.0f}h:"
            f" (entry - lo24 - {floor:.0f}bps cost) + carry/h x"
            f" {config.FUNDING_SCORE_HOLD_HOURS:.0f}. The basis is a"
            " ONE-OFF you capture once; funding is a STREAM. This is the whole"
            " board in one number — read the columns only to see WHY."
        )
        lines.append(
            "carry is the LOWER of 24h and fund, so a collapsed carry cannot"
            " flatter a row on its average. The cost figure carries the"
            " measured slippage, so execution is charged once, in the floor."
            " Not weighted by depth — that is sizing, read depth$."
            " '*' = under 3 samples, so jit is not yet measurable (normal for"
            " a few minutes after a restart)."
        )
        lines.append(
            "all funding in bps per HOUR — comparable across contracts"
            " whatever interval they settle on, and directly against a holding"
            " period. 24h = average over the last 24h; fund = the latest"
            " settlement; next = hours until the next one (/book has the"
            " settlement interval)."
        )
        lines.append(f"entry = {win_m:.0f}m avg basis (short perp gets +funding).")
        lines.append(
            "lo24/hi24 = p10/p90 of the HOURLY mean basis over 24h — where this"
            " pair has actually traded today. entry near hi24 = rich end, a"
            " good moment to sell the perp; entry near lo24 = you are entering"
            " at the cheap end and paying for the carry. hi24-lo24 is also the"
            " round trip on offer, and lo24 is where an /exit will fill."
            f" '?' = under {config.SCREEN_DIFF_MIN_HOURS:.0f}h of history, so"
            " those two are the live basis, not a range."
        )
        lines.append(
            "jit = mean bps the basis moves between 5m samples. Shown because"
            " an extreme is hard to work, NOT as a cost: measured on real"
            " fills it does not predict slippage either way."
        )
        hid = snap.get("hidden") or {}
        if hid:
            lines.append(
                "hidden as unworkable: "
                + ", ".join(f"{v} {k}" for k, v in sorted(hid.items()))
                + f" (index = Aster's own index disagrees with MEXC spot by"
                f" >{config.SCREEN_MAX_INDEX_DIVERGENCE_BPS / 100:.0f}%, so the"
                " two symbols are not the same asset at the same scale and the"
                " basis is fiction; spread = books"
                f" >{config.SCREEN_MAX_SPREAD_COST_BPS:.0f}bps apart, so the"
                " quote is not a real price; depth/volume = too thin to fill;"
                " jitter = flickers too hard to work)"
            )
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
        blocks = []
        total_notional = 0.0
        total_upnl = 0.0
        for p in active:
            open_basis = self._basis_bps(p.perp_entry_avg, p.spot_entry_avg)
            if open_basis is None and p.entry_basis_bps is not None:
                open_basis = float(p.entry_basis_bps)
            ob = f"{open_basis:+.1f}" if open_basis is not None else "-"
            held = (
                f"{(time.time() * 1000 - p.opened_ms) / 3_600_000:.1f}h"
                if p.opened_ms else "-"
            )
            kind_tag = " ⚓carry" if p.trade_kind == "carry" else ""
            base = p.symbol[:-4] if p.symbol.endswith("USDT") else p.symbol
            head = (f"#{p.id} {p.symbol} [{p.state}]{kind_tag}  held {held}"
                    f"{' · exit ' + p.exit_mode if p.exit_mode else ''}"
                    f"{' (paper)' if p.paper else ''}")
            venue = [
                f"  Aster perp  short {self._fmt_qty(p.perp_qty)} {base}"
                f"  sell {self._fmt_px(p.perp_entry_avg)} / buy —(open)",
                f"  MEXC spot   long  {self._fmt_qty(p.spot_qty)} {base}"
                f"  buy {self._fmt_px(p.spot_entry_avg)} / sell —(open)",
            ]
            m = marks.get(str(p.id)) if marks_fresh else None
            if m and "upnl_usd" in m:
                now_basis = m["close_bps"]
                drift = (f"{open_basis - now_basis:+.1f}"
                         if open_basis is not None else "-")
                liq = ""
                if m.get("liq_dist_pct") is not None:
                    d = m["liq_dist_pct"]
                    warn = " ⚠️" if d < float(config.LIQ_ALERT_PCT) else ""
                    liq = f" | liq +{d:.0f}%{warn}"
                notional = m.get("notional_usd")
                size = f"~${notional:,.0f}" if notional else "~$?"
                total_notional += notional or 0.0
                total_upnl += m["upnl_usd"]
                tail = [
                    f"  basis  entry {ob}  now {now_basis:+.1f}"
                    f"  captured {drift} bps",
                    f"  size {size}  funding ${m['funding_usd']:+.2f}"
                    f"  commission ${float(p.fees_usd):.2f}"
                    f"  →  uPnL ${m['upnl_usd']:+.2f}{liq}",
                ]
            else:
                if not marks_fresh:
                    why = "engine heartbeat stale"
                elif m and "skip" in m:
                    why = m["skip"]
                else:
                    why = "no live mark"
                tail = [
                    f"  basis  entry {ob}  now ? ({why})",
                    f"  commission ${float(p.fees_usd):.2f}",
                ]
            blocks.append("\n".join([head, *venue, *tail]))
        out = "active positions:\n\n" + "\n\n".join(blocks)
        if total_notional:
            out += (f"\n\ntotal: ~${total_notional:,.0f} notional"
                    f" | uPnL ${total_upnl:+.2f}")
        return out

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
    def _fmt_qty(qty: Decimal | None) -> str:
        """Coin units without trailing-zero noise (e.g. 3377, 9.95, 0.0125)."""
        if qty is None:
            return "-"
        q = qty.normalize()
        # normalize() renders small integers in exponent form (3E+3); expand.
        if q == q.to_integral_value():
            return str(int(q))
        return f"{q:f}"

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
            # Hedge-balance check: captured basis is a PRICE relationship that
            # only turns into the shown P&L if both legs are the same size. If
            # the fraction of the perp closed differs from the fraction of the
            # spot closed, the position carried naked delta and the coin's raw
            # move (not the basis) drove P&L — flag it so a "converged but lost"
            # trade is legible.
            q = self._positions.leg_qtys(p.id)
            imbalance = ""
            fp = (q["perp_exit"] / q["perp_entry"]) if q["perp_entry"] else None
            fs = (q["spot_exit"] / q["spot_entry"]) if q["spot_entry"] else None
            if fp is not None and fs is not None and abs(float(fp - fs)) > 0.02:
                imbalance = (f"  ⚠️ hedge imbalance: closed {float(fp) * 100:.0f}%"
                             f" of perp vs {float(fs) * 100:.0f}% of spot"
                             f" — residual delta drove P&L, not basis")
            blocks.append("\n".join([
                f"#{p.id} {p.symbol} {p.state}"
                f"{' (paper)' if p.paper else ''}  {when} · held {held}",
                f"  Aster perp  sell {self._fmt_px(p.perp_entry_avg)}"
                f"  buy {self._fmt_px(p.perp_exit_avg)}"
                f"  ({self._fmt_qty(q['perp_entry'])}/{self._fmt_qty(q['perp_exit'])})",
                f"  MEXC spot   buy  {self._fmt_px(p.spot_entry_avg)}"
                f"  sell {self._fmt_px(p.spot_exit_avg)}"
                f"  ({self._fmt_qty(q['spot_entry'])}/{self._fmt_qty(q['spot_exit'])})",
                f"  basis  open {ob}  close {cb}  captured {drift} bps",
                f"  funding ${float(p.funding_usd):+.2f}"
                f"  commission ${float(p.fees_usd):.2f}"
                + (f"  unwind ${float(p.unwind_pnl_usd):+.2f}"
                   if p.unwind_pnl_usd else "")
                + f"  →  P&L {pnl}{imbalance}",
            ]))
        return "last trades:\n\n" + "\n\n".join(blocks)

    def _cmd_pnl(self) -> str:
        s = self._positions.pnl_summary()
        # Say what this number is NOT: it counts closed positions only, so it
        # misses funding still accruing, coins held outside the strategy and
        # idle margin. /equity is the complete picture.
        head = (
            f"realised P&L (USD)\n"
            f"live:  today {float(s['live_today']):+.2f} | all-time {float(s['live_all_time']):+.2f}\n"
            f"paper: today {float(s['paper_today']):+.2f} | all-time {float(s['paper_all_time']):+.2f}"
        )
        # Itemise today. A daily total is unverifiable on its own, and the
        # lines that make it puzzling are the ones nobody placed: a CANCELLED
        # position is an entry that filled and was unwound, which books a real
        # cost without ever being a trade.
        today = self._positions.closed_today()
        if today:
            lines = [head, "", f"today's {len(today)} closed position(s):"]
            for p in today:
                when = (time.strftime("%H:%M", time.localtime(p.closed_ms / 1000))
                        if p.closed_ms else "--:--")
                tag = "  (entry unwound, never a trade)" if p.state == "CANCELLED" else ""
                lines.append(
                    f"  {when}  #{p.id} {p.symbol:<12}"
                    f" {float(p.realized_pnl_usd or 0):>+9.2f}{tag}"
                )
            lines.append("")
            lines.append(
                "closed positions only — funding still accruing on an open one,"
                " and any coin held outside the strategy, are not in here."
                " /equity is the whole account; /trades breaks a line down."
            )
            return "\n".join(lines)
        return (
            head + "\n\nnothing closed today."
            "\nclosed positions only — /equity for total account value"
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
                    "qty = coins to close (as in /positions), or $NOTIONAL"
                    " e.g. $500; omit = full")
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
