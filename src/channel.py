"""Channel publisher — sends alerts to a public Telegram channel.

Publishes two types of messages:
  - Warning alerts for high-risk launches (score ≥ threshold)
  - Gem alerts for unusually low-risk launches (score ≤ threshold)
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from telegram import Bot


class ChannelPublisher:
    """Publishes forensic alerts to a public Telegram channel."""

    def __init__(
        self,
        channel_id: str = "",
        bot: Bot | None = None,
        min_score_for_warning: int = 70,
        max_score_for_gem: int = 25,
    ) -> None:
        self._channel_id = channel_id
        self._bot = bot
        self._min_warning = min_score_for_warning
        self._max_gem = max_score_for_gem

    async def maybe_publish(self, result: Any) -> bool:
        """Publish to channel if the result meets thresholds.

        Returns True if a message was sent.
        """
        if not self._channel_id or not self._bot:
            return False

        score = getattr(result, "total_score", 50.0)
        mint = getattr(result, "mint", "")
        name = getattr(result, "name", "")
        symbol = getattr(result, "symbol", "")
        token = f"{name} (${symbol})" if name else mint[:16]

        msg = None

        if score >= self._min_warning:
            msg = (
                f"🔴 <b>RUG WARNING — {score:.0f}/100</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 {token}\n"
                f"🔑 <code>{mint}</code>\n\n"
                f"🔗 <a href='https://dexscreener.com/solana/{mint}'>Chart</a>"
                f" · <a href='https://solscan.io/token/{mint}'>Solscan</a>"
            )
        elif score <= self._max_gem:
            msg = (
                f"💎 <b>POTENTIAL GEM — {score:.0f}/100</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 {token}\n"
                f"🔑 <code>{mint}</code>\n\n"
                f"🔗 <a href='https://dexscreener.com/solana/{mint}'>Chart</a>"
                f" · <a href='https://pump.fun/{mint}'>Pump</a>"
            )

        if not msg:
            return False

        try:
            await self._bot.send_message(
                chat_id=self._channel_id,
                text=msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True
        except Exception as e:
            logger.error(f"Channel publish failed: {e}")
            return False
