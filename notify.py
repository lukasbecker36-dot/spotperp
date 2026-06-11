"""Multi-channel alerting: Telegram (primary), optional Discord webhook.

Fire-and-forget — alert failures are logged, never raised into trading code.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

log = logging.getLogger(__name__)


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
        try:
            url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
            async with self._session.post(
                url,
                json={"chat_id": self._tg_chat, "text": message},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
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
