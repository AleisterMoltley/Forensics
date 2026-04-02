"""Market cap milestone tracker.

Periodically checks recently scanned tokens for market cap threshold
crossings.  When a token's mcap crosses a configured milestone (e.g.
$100k, $300k, $1M), the token is re-scanned and a milestone alert
is sent to Telegram.

This enables a "ladder alert" pattern: get an initial alert when a
token launches, then follow-up alerts as it gains traction — each
with an updated forensic analysis that may reveal new bundler
patterns or deployer behavior.

Configuration via environment variables:
    MCAP_MILESTONES=100000,300000,1000000
    MCAP_CHECK_INTERVAL=120
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from typing import Any

import aiohttp
from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import settings
from src.models import TokenLaunch


def _fmt_mcap(v: float) -> str:
    """Format a USD market cap value for display."""
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:.0f}"


class McapMilestoneTracker:
    """Tracks token market caps and fires alerts on milestone crossings.

    Architecture:
    - Maintains an in-memory set of (mint, milestone) pairs already triggered
    - Every MCAP_CHECK_INTERVAL seconds, fetches current mcap for recent tokens
    - Uses DexScreener API (free, no key needed) for mcap data
    - When a new milestone is crossed, triggers a pipeline re-scan + alert
    """

    # DexScreener allows batching up to 30 addresses per request
    _DEXSCREENER_BATCH = 30
    _DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pipeline: Any = None,
        telegram: Any = None,
    ) -> None:
        self._sf = session_factory
        self._pipeline = pipeline
        self._telegram = telegram
        self._running = False
        self._session: aiohttp.ClientSession | None = None

        # Track which milestones have already been triggered per token.
        # Key: (mint, milestone_value) → True
        # Bounded to prevent unbounded memory growth.
        self._triggered: OrderedDict[tuple[str, float], bool] = OrderedDict()
        self._MAX_TRIGGERED = 50_000

    async def start(self) -> None:
        milestones = settings.mcap_milestone_list
        if not milestones:
            logger.info("Milestone tracker disabled (no MCAP_MILESTONES configured)")
            return

        self._running = True
        interval = settings.mcap_check_interval
        logger.info(
            f"Milestone tracker started — thresholds: "
            f"{', '.join(_fmt_mcap(m) for m in milestones)} "
            f"(interval: {interval}s)"
        )

        while self._running:
            try:
                await self._check_milestones()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Milestone tracker error: {e}")

            await asyncio.sleep(interval)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def _check_milestones(self) -> None:
        """Fetch recent tokens from DB and check their current mcap."""
        milestones = settings.mcap_milestone_list
        if not milestones:
            return

        # Get tokens from the last 24h that haven't hit the highest milestone yet
        max_milestone = max(milestones)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(TokenLaunch.mint, TokenLaunch.peak_mcap)
                    .where(
                        and_(
                            TokenLaunch.launched_at >= cutoff,
                            TokenLaunch.is_rug.is_not(True),
                        )
                    )
                    .limit(200)
                )
            ).all()

        if not rows:
            return

        # Filter to tokens that still have milestones to hit
        mints_to_check: list[str] = []
        for mint, peak_mcap in rows:
            for ms in milestones:
                if (mint, ms) not in self._triggered:
                    mints_to_check.append(mint)
                    break

        if not mints_to_check:
            return

        # Fetch current mcap from DexScreener in batches
        for i in range(0, len(mints_to_check), self._DEXSCREENER_BATCH):
            batch = mints_to_check[i : i + self._DEXSCREENER_BATCH]
            try:
                mcap_data = await self._fetch_mcap_batch(batch)
            except Exception as e:
                logger.debug(f"DexScreener fetch failed: {e}")
                continue

            for mint, mcap in mcap_data.items():
                if mcap is None or mcap <= 0:
                    continue

                # Check each milestone
                for ms in milestones:
                    if (mint, ms) in self._triggered:
                        continue
                    if mcap >= ms:
                        # Milestone crossed!
                        self._triggered[(mint, ms)] = True
                        # Evict oldest entries if memory limit reached
                        while len(self._triggered) > self._MAX_TRIGGERED:
                            self._triggered.popitem(last=False)

                        logger.info(
                            f"🎯 Milestone: {mint[:16]}... crossed "
                            f"{_fmt_mcap(ms)} (current: {_fmt_mcap(mcap)})"
                        )

                        # Re-scan and alert
                        asyncio.create_task(
                            self._handle_milestone(mint, ms, mcap),
                            name=f"milestone_{mint[:8]}_{ms}",
                        )

            # Small delay between batches to be polite to DexScreener
            if i + self._DEXSCREENER_BATCH < len(mints_to_check):
                await asyncio.sleep(1.0)

    async def _fetch_mcap_batch(
        self, mints: list[str]
    ) -> dict[str, float | None]:
        """Fetch market caps from DexScreener for a batch of mints."""
        result: dict[str, float | None] = {}
        session = await self._ensure_session()

        # DexScreener accepts comma-separated addresses
        url = self._DEXSCREENER_URL.format(",".join(mints))

        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    logger.debug("DexScreener rate limited, skipping batch")
                    return result
                if resp.status != 200:
                    return result

                data = await resp.json()
                pairs = data.get("pairs", [])

                # Group by base token mint, take highest mcap pair
                for pair in pairs:
                    base_mint = pair.get("baseToken", {}).get("address", "")
                    mcap = pair.get("marketCap") or pair.get("fdv")
                    if base_mint and mcap:
                        try:
                            mcap_f = float(mcap)
                            existing = result.get(base_mint)
                            if existing is None or mcap_f > existing:
                                result[base_mint] = mcap_f
                        except (ValueError, TypeError):
                            continue

        except Exception as e:
            logger.debug(f"DexScreener API error: {e}")

        return result

    async def _handle_milestone(
        self, mint: str, milestone: float, current_mcap: float
    ) -> None:
        """Re-scan a token and send a milestone alert."""
        try:
            # Re-scan through the pipeline
            rescan_result = None
            if self._pipeline:
                try:
                    rescan_result = await self._pipeline.analyze({
                        "mint": mint,
                        "source": "milestone_rescan",
                        "mcap": current_mcap,
                    })
                except Exception as e:
                    logger.warning(f"Milestone re-scan failed for {mint[:16]}: {e}")

            # Get token info from DB for the alert
            name = ""
            symbol = ""
            deployer = ""
            prev_score = 0.0

            async with self._sf() as session:
                row = (
                    await session.execute(
                        select(TokenLaunch).where(TokenLaunch.mint == mint)
                    )
                ).scalar_one_or_none()
                if row:
                    name = row.name or ""
                    symbol = row.symbol or ""
                    deployer = row.deployer or ""
                    prev_score = row.risk_score_total or 0.0
                    # Update mcap in DB
                    row.current_mcap = current_mcap
                    if row.peak_mcap is None or current_mcap > row.peak_mcap:
                        row.peak_mcap = current_mcap
                    await session.commit()

            # Build the milestone alert
            new_score = rescan_result.total_score if rescan_result else prev_score
            token = f"{name} (${symbol})" if name else f"{mint[:16]}…"
            score_change = ""
            if rescan_result and abs(new_score - prev_score) >= 1:
                arrow = "📈" if new_score > prev_score else "📉"
                score_change = f"\n{arrow} Score: {prev_score:.0f} → <b>{new_score:.0f}</b>"

            # Format mcap
            mcap_str = _fmt_mcap(current_mcap)
            milestone_str = _fmt_mcap(milestone)

            # Deployer shorthand
            dep_str = ""
            if deployer:
                dep_str = f"\n👤 <code>{deployer[:6]}…{deployer[-4:]}</code>"

            # New bundler intel from re-scan
            bundle_sec = ""
            if rescan_result:
                bd = getattr(rescan_result, "bundle_data", {}) or {}
                bf = bd.get("flags", [])
                if bf:
                    bundle_sec = (
                        "\n🔍 <b>Updated Intel:</b>\n"
                        + "\n".join(f"  ⚡ {f}" for f in bf[:4])
                    )

            msg = (
                f"🎯 <b>MILESTONE — {milestone_str}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🪙 <b>{token}</b>\n"
                f"💰 MCap: <b>{mcap_str}</b> (crossed {milestone_str})\n"
                f"⚠️ Risk: <b>{new_score:.0f}/100</b>"
                f"{score_change}{dep_str}{bundle_sec}\n\n"
                f"🔗 <a href='https://dexscreener.com/solana/{mint}'>Chart</a>"
                f" · <a href='https://solscan.io/token/{mint}'>Solscan</a>"
                f" · <a href='https://pump.fun/{mint}'>Pump</a>\n"
                f"💡 /scan {mint}"
            )

            # Send via Telegram
            if (
                self._telegram
                and hasattr(self._telegram, "bot")
                and self._telegram.bot
                and settings.telegram_chat_id
            ):
                try:
                    await self._telegram.bot.send_message(
                        chat_id=settings.telegram_chat_id,
                        text=msg,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception as e:
                    logger.error(f"Milestone alert send failed: {e}")

        except Exception as e:
            logger.error(f"Milestone handler failed for {mint[:16]}: {e}")

    async def stop(self) -> None:
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("Milestone tracker stopped")
