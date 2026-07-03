"""Multi-channel alerting: Telegram (primary), optional Discord webhook.

Fire-and-forget — alert failures are logged, never raised into trading code.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

log = logging.getLogger(__name__)


def _chunks(text: str, size: int) -> list[str]:
    """Split on line boundaries where possible, hard-splitting only lines that
    are themselves longer than `size`."""
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


class Notifier:
    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._tg_token = os.environ.get("ALERT_TELEGRAM_BOT_TOKEN", "")
        self._tg_chat = os.environ.get("ALERT_TELEGRAM_CHAT_ID", "")
        self._discord = os.environ.get("ALERT_DISCORD_WEBHOOK_URL", "")

    async def alert(self, message: str) -> None:
        await asyncio.gather(
            self._send_telegram(message),
            self._send_discord(message),
            return_exceptions=True,
        )

    async def _send_telegram(self, message: str) -> None:
        if not self._tg_token or not self._tg_chat:
            return
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        # Telegram rejects messages over 4096 chars with HTTP 400 and drops the
        # whole thing — an emergency alert enumerating many positions could be
        # lost. Chunk to stay under the limit.
        for chunk in _chunks(message, 4000):
            try:
                async with self._session.post(
                    url,
                    json={"chat_id": self._tg_chat, "text": chunk},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        # Never log the URL (it carries the bot token).
                        log.warning("telegram alert failed: HTTP %s", resp.status)
            except Exception:
                log.exception("telegram alert failed")

    async def _send_discord(self, message: str) -> None:
        if not self._discord:
            return
        try:
            async with self._session.post(
                self._discord,
                json={"content": message},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    log.warning("discord alert failed: HTTP %s", resp.status)
        except Exception:
            log.exception("discord alert failed")
