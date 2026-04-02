"""Sniper bridge — forwards buy signals for low-risk token launches.

When the forensic pipeline produces a low-risk score (below the
configured threshold), the SniperBridge optionally sends a buy signal
to an external sniper bot via webhook and/or a Telegram chat.

Webhook requests are signed with HMAC-SHA256 (using the ADMIN_API_KEY
as the shared secret) in the ``X-Signature`` header so the receiving
sniper bot can verify authenticity and reject forged signals.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import aiohttp
from loguru import logger
from telegram import Bot

from src.config import settings


class SniperBridge:
    """Sends buy signals for promising (low-risk) launches."""

    def __init__(
        self,
        webhook_url: str = "",
        signal_chat_id: str = "",
    ) -> None:
        self._webhook_url = webhook_url
        self._signal_chat_id = signal_chat_id
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def process(
        self,
        result: Any,
        bot: Bot | None = None,
    ) -> dict[str, Any] | None:
        """Check if a pipeline result qualifies as a buy signal.

        Returns the signal dict if sent, None otherwise.
        """
        score = getattr(result, "total_score", 100.0)
        if score > settings.sniper_max_risk_score:
            return None

        mint = getattr(result, "mint", "")
        if not mint:
            return None

        signal = {
            "action": "buy",
            "mint": mint,
            "name": getattr(result, "name", ""),
            "symbol": getattr(result, "symbol", ""),
            "risk_score": score,
            "source": getattr(result, "source", ""),
            "deployer": getattr(result, "deployer", ""),
        }

        # Send via webhook (HMAC-signed)
        if self._webhook_url:
            try:
                session = await self._ensure_session()
                body = json.dumps(signal, sort_keys=True)
                headers: dict[str, str] = {"Content-Type": "application/json"}
                # Sign with ADMIN_API_KEY if available
                if settings.admin_api_key:
                    sig = hmac.new(
                        settings.admin_api_key.encode(),
                        body.encode(),
                        hashlib.sha256,
                    ).hexdigest()
                    headers["X-Signature"] = f"sha256={sig}"
                async with session.post(
                    self._webhook_url, data=body, headers=headers,
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"Sniper webhook returned {resp.status}"
                        )
            except Exception as e:
                logger.error(f"Sniper webhook failed: {e}")

        # Send via Telegram
        if self._signal_chat_id and bot:
            try:
                msg = (
                    f"🎯 <b>BUY SIGNAL</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🪙 {signal['name']} (${signal['symbol']})\n"
                    f"🔑 <code>{mint}</code>\n"
                    f"⚠️ Risk: {score:.0f}/100\n"
                    f"📍 {signal['source']}\n\n"
                    f"🔗 <a href='https://dexscreener.com/solana/{mint}'>Chart</a>"
                    f" · <a href='https://pump.fun/{mint}'>Pump</a>"
                )
                await bot.send_message(
                    chat_id=self._signal_chat_id,
                    text=msg,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Sniper Telegram alert failed: {e}")

        logger.info(f"🎯 Buy signal: {mint[:16]}... (score={score})")
        return signal

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
