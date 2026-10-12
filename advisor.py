"""AI advisor: a periodic Claude review of the book, delivered to Telegram.

ADVISORY ONLY. This module reads the same snapshot files the bot's /positions,
/funding and /screen commands use, packs them into a prompt, asks Claude for an
assessment, and sends the reply to Telegram. It has NO path to place, size or
close an order — it only produces text. Actions remain manual via /enter and
/exit.

Two entry points:
  - review_text(session): build context + call Claude, return the advice string
    (used by the bot's /review command, which sends it as the reply);
  - main(): standalone run for deploy/basis-trade-advisor.timer — builds the
    review and pushes it to Telegram via notify.Notifier.
"""
from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal

import aiohttp

import config
import database
from auth import load_env
from notify import Notifier
from position_manager import PositionManager

log = logging.getLogger("advisor")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

SYSTEM_PROMPT = """\
You advise a perp-spot basis desk. Every position is SHORT an Aster perpetual \
and LONG the same coin's MEXC spot, delta-neutral. P&L comes from the basis \
(perp premium over spot) falling between entry and exit, plus funding the short \
receives while held.

How the desk trades (assume this; do not suggest otherwise):
- Entries AND exits are maker on the Aster perp (sell at the ask, buy back at \
the bid) with MEXC spot hedged as taker. Taker exits are only for tiny \
residuals or emergencies.
- Basis figures, all in bps: entry = perp ask vs spot ask; exit (maker) = perp \
bid vs spot bid — what a normal close realises; exit_taker = perp ask vs spot \
bid. A trip earns entry - exit. The round-trip cost floor is \
cost_floor_bps (fees + measured slippage).
- ALL funding is in bps PER HOUR; positive = the short receives.
- Position kinds: "carry" = held for funding, NO automatic exit, the operator \
closes it; "convergence" = auto-closes passively at auto_exit_bps (derived \
from the pair's 24h exit low); "spike" = short-term mean reversion, \
auto-closes passively at auto_exit_bps (the 24h norm exit basis) and works a \
passive close at any basis after spike_max_hold_h. Liquidation stops (stops: \
true) sit ~1% below liq and fire on their own.
- entry_range / exit_range are p10..p90 of the pair's hourly basis over 24h \
and 72h: use them to say whether the CURRENT basis is unusual for THIS pair \
or just its normal level.

Opportunity boards (each row carries its own score; higher is better):
- funding: carry trades — score = (entry - lo24 - cost) + carry over 24h.
- fill: round trips with real flow — score = net edge x a fill factor.
- spike: basis jumped well above its own 24h norm — score = reversion edge \
after cost + 1h of funding; z = how unusual the jump is for that pair.
Rows on these boards have already passed depth, volume, jitter, round-trip \
and exit-room checks — don't re-litigate those; judge the trade.

You ADVISE ONLY and cannot trade. Use only the numbers given; never invent \
any. Reference position IDs and symbols. Format, plain text for Telegram, \
under ~250 words, no tables or markdown headers:
1. One line: anything urgent (liq distance under liq_alert_pct, funding \
flipped negative on a carry, a warning in recent_warnings that needs action) \
or "Nothing urgent."
2. One short line per position: hold / watch / act, the reason in numbers, \
and what the bot will do ON ITS OWN (auto-exit level, or "no auto-exit — \
carry").
3. At most two opportunities worth a look, with board and score, or "None \
stand out." Skip anything already held unless adding is clearly better.
Holding time alone is never a reason to exit."""


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _r(x, nd=1):
    v = _f(x)
    return round(v, nd) if v is not None else None


def _board_rows(rows: list, n: int, fields: dict) -> list:
    out = []
    for r in rows[:n]:
        out.append({k: (_r(r.get(src), 2) if isinstance(r.get(src), (int, float))
                        else r.get(src)) for k, src in fields.items()})
    return out


def build_context(conn) -> dict:
    """Marshal open positions + live marks + the three boards + recent P&L
    and warnings into a JSON-able dict. Pure reads; safe from the bot or a
    timer."""
    now_ms = int(time.time() * 1000)
    positions = PositionManager(conn)

    # Live per-position marks (basis now, uPnL, liq distance, ranges, funding)
    # from the heartbeat.
    marks, marks_fresh = {}, False
    try:
        hb = json.loads(config.HEARTBEAT_FILE.read_text())
        marks = hb.get("marks", {})
        marks_fresh = (now_ms - hb["ts_ms"]) < 120_000
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    stops = database.load_stop_orders(conn)

    open_positions = []
    for p in positions.active():
        m = marks.get(str(p.id), {}) if marks_fresh else {}
        held_h = (now_ms - p.opened_ms) / 3_600_000 if p.opened_ms else None
        open_positions.append({
            "id": p.id,
            "symbol": p.symbol,
            "state": p.state,
            "kind": p.trade_kind,
            "auto_entered": p.auto_entered,
            "working_exit": p.exit_mode,
            "working_exit_target_bps": _r(p.exit_target_bps),
            "entry_basis_bps": _r(p.entry_basis_bps),
            "entry_bps_now": _r(m.get("entry_bps_now")),
            "exit_bps_now": _r(m.get("close_bps")),
            "exit_taker_bps_now": _r(m.get("exit_taker_bps_now")),
            "auto_exit_bps": _r(m.get("auto_exit_bps")),
            "auto_exit_from": (
                f"{m.get('exit_ref')} {m.get('exit_low_bps'):+.1f}"
                if m.get("exit_low_bps") is not None else
                ("fixed" if m.get("auto_exit_bps") is not None else None)
            ),
            "entry_range_24h": m.get("entry_range_24h"),
            "exit_range_24h": m.get("exit_range_24h"),
            "exit_range_72h": m.get("exit_range_72h"),
            "funding_now_bps_per_h": _r(m.get("funding_now_bps_per_h"), 2),
            "funding_24h_bps_per_h": _r(m.get("funding_24h_bps_per_h"), 2),
            "funding_collected_usd": _r(p.funding_usd, 2),
            "fees_usd": _r(p.fees_usd, 2),
            "upnl_usd": _r(m.get("upnl_usd"), 2),
            "notional_usd": _r(m.get("notional_usd"), 0),
            "liq_distance_pct": _r(m.get("liq_dist_pct")),
            "stops": p.id in stops,
            "held_hours": round(held_h, 1) if held_h is not None else None,
        })

    boards: dict = {}
    try:
        fsnap = json.loads(config.FUNDING_SNAPSHOT_FILE.read_text())
        boards["funding"] = _board_rows(fsnap.get("rows", []), 6, {
            "symbol": "symbol", "score": "score", "entry_bps": "entry_bps_avg",
            "lo24": "basis_p10_24h", "hi24": "basis_p90_24h",
            "jit": "entry_bps_jitter", "volume_24h": "perp_volume_24h",
            "depth5_usd": "mexc_ask_depth_usd",
        })
        for row, src in zip(boards["funding"], fsnap.get("rows", [])):
            row["funding_now_bps_per_h"] = _r(_f(src.get("current_8h_bps"), 0) / 8, 2)
            row["funding_24h_bps_per_h"] = _r(_f(src.get("avg_24h_8h_bps"), 0) / 8, 2)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    try:
        ssnap = json.loads(config.SCREENER_SNAPSHOT_FILE.read_text())
        boards["fill"] = _board_rows(ssnap.get("fill_rows", []), 5, {
            "symbol": "symbol", "entry_bps": "entry_bps_avg",
            "lo24": "basis_p10_24h", "hi24": "basis_p90_24h",
            "hours_workable": "hours_tradeable_24h", "jit": "entry_bps_jitter",
            "volume_24h": "perp_volume_24h", "depth5_usd": "mexc_ask_depth_usd",
        })
        boards["spike"] = _board_rows(ssnap.get("spike_rows", []), 5, {
            "symbol": "symbol", "score": "score", "entry_bps": "entry_bps_avg",
            "norm_bps": "norm_entry_bps", "spike_bps": "spike_bps", "z": "spike_z",
            "norm_exit_bps": "norm_close_bps", "edge_bps": "spike_edge_bps",
            "jit": "entry_bps_jitter", "volume_24h": "perp_volume_24h",
            "depth5_usd": "mexc_ask_depth_usd",
        })
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    today = positions.closed_today()
    eq = database.equity_history(conn, now_ms - 86_400_000)
    equity = None
    if len(eq) >= 2:
        first, last = _f(eq[0]["total_usd"]), _f(eq[-1]["total_usd"])
        if first is not None and last is not None:
            equity = {"now_usd": round(last, 2), "change_24h_usd": round(last - first, 2)}
    warnings = [
        r["message"][:160] for r in conn.execute(
            "SELECT message FROM journal WHERE ts_ms >= ? AND level IN"
            " ('WARN', 'WARNING', 'ERROR') ORDER BY ts_ms DESC LIMIT 8",
            (now_ms - 12 * 3_600_000,),
        )
    ]

    return {
        "generated_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now_ms / 1000)),
        "mode": "paper" if config.paper_mode() else "LIVE",
        "marks_fresh": marks_fresh,
        "settings": {
            "cost_floor_bps": _f(config.ENTRY_MIN_EDGE_FLOOR_BPS),
            "liq_alert_pct": _f(config.LIQ_ALERT_PCT),
            "spike_max_hold_h": config.SPIKE_MAX_HOLD_HOURS,
            "score_alerts": database.get_setting(conn, "score_alert", {}) or {},
            "auto_enter_usd": database.get_setting(conn, "auto_enter", {}) or {},
        },
        "open_positions": open_positions,
        "boards": boards,
        "closed_today": {
            "count": len(today),
            "realized_pnl_usd": round(sum(_f(t.realized_pnl_usd, 0.0) for t in today), 2),
        },
        "recent_closed": [{
            "symbol": t.symbol, "kind": t.trade_kind,
            "realized_pnl_usd": _r(t.realized_pnl_usd, 2),
        } for t in positions.closed(5)],
        "equity": equity,
        "recent_warnings": warnings,
    }


def _api_key() -> str:
    """Read the key from the live environment (populated by load_env at process
    start), falling back to the import-time config value. config.ANTHROPIC_API_KEY
    is captured when config is first imported — often BEFORE load_env() runs — so
    reading it directly can wrongly report the key as unset even when .env has it."""
    return os.environ.get("ANTHROPIC_API_KEY") or config.ANTHROPIC_API_KEY


async def _call_claude(session: aiohttp.ClientSession, context: dict) -> str:
    api_key = _api_key()
    if not api_key:
        return ("advisor not configured: set ANTHROPIC_API_KEY in .env (and"
                " restart the control bot) to enable the AI review.")
    user_msg = (
        "Here is the current book state as JSON. Review it and advise.\n\n"
        + json.dumps(context, indent=1)
    )
    payload = {
        "model": os.environ.get("ADVISOR_MODEL") or config.ADVISOR_MODEL,
        # Thinking is on by default on current models and counts toward
        # max_tokens, so leave room beyond the ~250-word answer.
        "max_tokens": config.ADVISOR_MAX_TOKENS,
        "output_config": {"effort": config.ADVISOR_EFFORT},
        # If a safety classifier declines, re-run on Anthropic's recommended
        # fallback model server-side rather than returning nothing.
        "fallbacks": "default",
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": "server-side-fallback-2026-07-01",
        "content-type": "application/json",
    }
    try:
        async with session.post(
            ANTHROPIC_URL, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=180),
        ) as resp:
            body = await resp.json()
            if resp.status != 200:
                err = body.get("error", {}).get("message", str(body))
                log.warning("advisor API error %s: %s", resp.status, err)
                return f"advisor API error (HTTP {resp.status}): {err}"
            if body.get("stop_reason") == "refusal":
                cat = (body.get("stop_details") or {}).get("category")
                return f"advisor declined this review (refusal: {cat or 'unspecified'})."
            parts = [b.get("text", "") for b in body.get("content", [])
                     if b.get("type") == "text"]
            text = "\n".join(parts).strip() or "advisor returned no text."
            if body.get("stop_reason") == "max_tokens":
                text += "\n(cut off — raise ADVISOR_MAX_TOKENS)"
            return text
    except Exception as exc:  # network/timeout — never raise into caller
        log.exception("advisor API call failed")
        return f"advisor call failed: {exc}"


async def review_text(session: aiohttp.ClientSession, conn=None) -> str:
    """Build context, call Claude, return the advice string with a header."""
    own_conn = conn is None
    conn = conn or database.init_db()
    try:
        ctx = build_context(conn)
    finally:
        if own_conn:
            conn.close()
    n = len(ctx["open_positions"])
    if n == 0 and not any(ctx["boards"].values()):
        return "🧠 review: no open positions and no opportunity data yet."
    advice = await _call_claude(session, ctx)
    stale = "" if ctx["marks_fresh"] else " ⚠️ live marks stale"
    header = f"🧠 {ctx['mode']} review · {n} open · {ctx['generated_utc']} UTC{stale}"
    return f"{header}\n\n{advice}"


def _in_run_window() -> bool:
    """True when the current time in ADVISOR_TIMEZONE falls on one of the
    configured run hours. The timer wakes hourly; this is the actual schedule
    gate, so it needs no systemd timezone support and follows DST via the tz
    database. On a missing tz db it fails OPEN (runs) rather than going silent."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        hour = datetime.now(ZoneInfo(config.ADVISOR_TIMEZONE)).hour
    except Exception:
        log.warning("timezone %s unavailable — running ungated",
                    config.ADVISOR_TIMEZONE)
        return True
    return hour in config.ADVISOR_RUN_HOURS


async def _run() -> None:
    load_env()
    async with aiohttp.ClientSession() as session:
        text = await review_text(session)
        await Notifier(session).alert(text)
        log.info("advisor review sent (%d chars)", len(text))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    import sys
    forced = "--force" in sys.argv[1:] or "--now" in sys.argv[1:]
    if not forced and not _in_run_window():
        from datetime import datetime
        log.info("outside run window (hours %s %s) — skipping",
                 config.ADVISOR_RUN_HOURS, config.ADVISOR_TIMEZONE)
        return
    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":
    main()
