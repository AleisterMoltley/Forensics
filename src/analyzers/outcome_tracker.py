"""Outcome tracker — labels token launches as rug or survived.

Periodically checks recently scanned tokens at 1h, 6h, and 24h
intervals to determine if they rugged (liquidity pulled, price
collapsed to near zero) or survived.

Also provides a ``TrainingDataExporter`` utility for bulk CSV export
of labeled data for ML training.
"""
from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.analyzers.rpc import rpc
from src.models import TokenLaunch, Deployer


# Check intervals in seconds
CHECK_INTERVALS = [
    3600,   # 1 hour
    21600,  # 6 hours
    86400,  # 24 hours
]

# Price collapse threshold — if current price is <X% of peak, it's a rug
RUG_PRICE_THRESHOLD = 0.05  # 5% of peak = rug


class OutcomeTracker:
    """Periodically checks unresolved token launches for rug outcomes."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
        self._running = False

    async def start(self) -> None:
        self._running = True
        logger.info("Outcome tracker started (1h/6h/24h checks)")

        while self._running:
            try:
                await self._check_outcomes()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Outcome tracker error: {e}")

            # Check every 15 minutes
            await asyncio.sleep(900)

    async def _check_outcomes(self) -> None:
        """Check pending tokens that are old enough for outcome labeling."""
        now = datetime.now(timezone.utc)

        async with self._sf() as session:
            # Find tokens that haven't been labeled yet
            rows = (
                await session.execute(
                    select(TokenLaunch)
                    .where(
                        and_(
                            TokenLaunch.is_rug.is_(None),
                            TokenLaunch.launched_at <= now - timedelta(hours=1),
                        )
                    )
                    .limit(50)
                )
            ).scalars().all()

            for token in rows:
                try:
                    outcome = await self._check_single(token)
                    if outcome is not None:
                        token.is_rug = outcome
                        if outcome:
                            token.rug_detected_at = now
                            # Atomic increment — same pattern as pipeline.py
                            await session.execute(
                                update(Deployer)
                                .where(Deployer.address == token.deployer)
                                .values(rug_count=Deployer.rug_count + 1)
                            )

                except Exception as e:
                    logger.debug(f"Outcome check failed for {token.mint[:12]}: {e}")

            await session.commit()

    async def _check_single(self, token: TokenLaunch) -> bool | None:
        """Check a single token's outcome. Returns True=rug, False=survived, None=undecided.

        IMPORTANT: Outcome labels must be based ONLY on external on-chain
        signals (deployer balance, activity, liquidity) and NEVER on the
        bot's own risk_score_total.  Using the risk score here would
        create label leakage — the ML model would learn to confirm its
        own predictions instead of learning from ground truth.
        """
        try:
            age = (datetime.now(timezone.utc) - token.launched_at).total_seconds()

            # Minimum 6h age before any rug labelling to reduce
            # false positives from slow-starting legitimate tokens.
            MIN_RUG_AGE = 21600  # 6 hours

            # Check if there's still liquidity / activity
            sigs = await rpc.get_signatures_for_address(token.mint, limit=5)
            if not sigs:
                # No transactions at all — likely dead
                if age > 86400:  # older than 24h with no activity
                    return True
                return None  # too early to tell

            # Check latest TX for token balance information
            latest_sig = sigs[0].get("signature", "")
            tx = await rpc.get_transaction(latest_sig)
            if not tx:
                return None

            meta = tx.get("meta", {})
            post_token = meta.get("postTokenBalances", [])

            # Check if deployer still holds tokens
            deployer_balance = 0
            for ptb in post_token:
                if ptb.get("mint") == token.mint and ptb.get("owner") == token.deployer:
                    deployer_balance = int(
                        ptb.get("uiTokenAmount", {}).get("amount", "0")
                    )

            # Rug signals — based purely on on-chain state, NOT risk score:
            # 1. Deployer dumped all tokens AND token is inactive — require 6h min age
            if deployer_balance == 0 and len(sigs) <= 2 and age > MIN_RUG_AGE:
                return True
            # 2. Token completely dead after 24h (no activity at all)
            if age > 86400 and len(sigs) <= 1:
                return True

            # If token survived 24h+ with activity → survived
            age = (datetime.now(timezone.utc) - token.launched_at).total_seconds()
            if age > 86400 and len(sigs) >= 3:
                return False

            return None  # still undecided

        except Exception:
            return None

    async def stop(self) -> None:
        self._running = False
        logger.info("Outcome tracker stopped")


class TrainingDataExporter:
    """Exports labeled data for ML training."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def export_csv(self, limit: int = 50_000) -> str:
        """Export labeled token data as CSV string."""
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(TokenLaunch)
                    .where(TokenLaunch.is_rug.isnot(None))
                    .limit(limit)
                )
            ).scalars().all()

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "mint", "deployer", "source",
            "score_deployer", "score_holders", "score_lp",
            "score_bundled", "score_contract", "score_social",
            "risk_score_total", "is_rug",
        ])
        for r in rows:
            writer.writerow([
                r.mint, r.deployer, r.source,
                r.score_deployer, r.score_holders, r.score_lp,
                r.score_bundled, r.score_contract, r.score_social,
                r.risk_score_total, 1 if r.is_rug else 0,
            ])

        return buf.getvalue()
