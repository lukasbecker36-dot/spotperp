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
You are a risk-and-opportunity advisor for a perp-spot basis trading desk. The \
strategy: SHORT an Aster perpetual and LONG the same coin's MEXC spot to capture \
the premium (basis) and collect Aster funding while the short is on. Positive \
basis = perp above spot (the edge you enter on); it decays toward zero as the \
trade works. Funding_8h_bps > 0 means the short RECEIVES funding (good carry). \
Liq-distance % is how far the perp mark must rise to liquidate the short — small \
is dangerous.

You ADVISE ONLY. You cannot trade. Recommend actions the operator would take by \
hand: hold, exit (now/passive), add margin, reduce, or rotate capital into a \
better funding opportunity. Be concise and specific — reference position IDs and \
real numbers from the data. Lead with anything urgent (liquidation proximity, \
funding that has flipped negative, basis gone adverse past the stop). If nothing \
needs doing, say so plainly rather than inventing action. Do not fabricate \
numbers; reason only from what you are given. Keep the whole reply under ~250 \
words, plain text for Telegram (no markdown tables)."""


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def build_context(conn) -> dict:
    """Marshal open positions + live marks + top opportunities + recent P&L into
    a JSON-able dict. Pure reads; safe to call from the bot or a timer."""
    now_ms = int(time.time() * 1000)
    positions = PositionManager(conn)

    # Live per-position marks (basis now, uPnL, liq distance) from the heartbeat.
    marks, marks_fresh = {}, False
    try:
        hb = json.loads(config.HEARTBEAT_FILE.read_text())
        marks = hb.get("marks", {})
        marks_fresh = (now_ms - hb["ts_ms"]) < 120_000
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    open_positions = []
    for p in positions.active():
        m = marks.get(str(p.id), {}) if marks_fresh else {}
        held_h = (now_ms - p.opened_ms) / 3_600_000 if p.opened_ms else None
        open_positions.append({
            "id": p.id,
            "symbol": p.symbol,
            "kind": p.trade_kind,
            "paper": p.paper,
            "entry_basis_bps": _f(p.entry_basis_bps),
            "current_basis_bps": _f(m.get("close_bps")),
            "funding_collected_usd": _f(p.funding_usd, 0.0),
            "fees_usd": _f(p.fees_usd, 0.0),
            "upnl_usd": _f(m.get("upnl_usd")),
            "notional_usd": _f(m.get("notional_usd")),
            "liq_distance_pct": _f(m.get("liq_dist_pct")),
            "held_hours": round(held_h, 1) if held_h is not None else None,
        })

    # Top funding-carry opportunities (what /funding shows), for rotation ideas.
    opportunities = []
    try:
        fsnap = json.loads(config.FUNDING_SNAPSHOT_FILE.read_text())
        for r in fsnap.get("rows", [])[: config.ADVISOR_TOP_OPPORTUNITIES]:
            opportunities.append({
                "symbol": r.get("symbol"),
                "funding_now_8h_bps": _f(r.get("current_8h_bps")),
                "funding_24h_avg_8h_bps": _f(r.get("avg_24h_8h_bps")),
                "net_edge_bps": _f(r.get("net_edge_bps")),
                "depth_usd": _f(r.get("max_notional_usd")),
                "next_funding_h": _f(r.get("next_funding_h")),
            })
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    recent = [{
        "symbol": t.symbol,
        "realized_pnl_usd": _f(t.realized_pnl_usd),
    } for t in positions.closed(5)]

    return {
        "generated_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now_ms / 1000)),
        "mode": "paper" if config.paper_mode() else "LIVE",
        "marks_fresh": marks_fresh,
        "thresholds": {
            "exit_basis_bps": _f(config.EXIT_BASIS_BPS),
            "adverse_widen_stop_bps": _f(config.ADVERSE_WIDEN_STOP_BPS),
            "max_hold_hours": _f(config.MAX_HOLD_HOURS),
            "liq_alert_pct": _f(config.LIQ_ALERT_PCT),
        },
        "open_positions": open_positions,
        "top_opportunities": opportunities,
        "recent_closed": recent,
    }


async def _call_claude(session: aiohttp.ClientSession, context: dict) -> str:
    if not config.ANTHROPIC_API_KEY:
        return ("advisor not configured: set ANTHROPIC_API_KEY in .env to enable"
                " the AI review.")
    user_msg = (
        "Here is the current book state as JSON. Review it and advise.\n\n"
        + json.dumps(context, indent=1)
    )
    payload = {
        "model": config.ADVISOR_MODEL,
        "max_tokens": config.ADVISOR_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    headers = {
        "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    try:
        async with session.post(
            ANTHROPIC_URL, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            body = await resp.json()
            if resp.status != 200:
                err = body.get("error", {}).get("message", str(body))
                log.warning("advisor API error %s: %s", resp.status, err)
                return f"advisor API error (HTTP {resp.status}): {err}"
            parts = [b.get("text", "") for b in body.get("content", [])
                     if b.get("type") == "text"]
            return "\n".join(parts).strip() or "advisor returned no text."
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
    if n == 0 and not ctx["top_opportunities"]:
        return "🧠 review: no open positions and no opportunity data yet."
    advice = await _call_claude(session, ctx)
    stale = "" if ctx["marks_fresh"] else " ⚠️ live marks stale"
    header = f"🧠 {ctx['mode']} review · {n} open · {ctx['generated_utc']} UTC{stale}"
    return f"{header}\n\n{advice}"


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
    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":
    main()
