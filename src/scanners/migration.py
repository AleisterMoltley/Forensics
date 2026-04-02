"""Pump.fun → Raydium migration listener and post-migration analyzer.

Monitors tracked Pump.fun tokens for migration events (bonding curve
completion → Raydium pool creation) and analyzes post-migration
deployer behavior.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import settings
from src.analyzers.rpc import rpc


class MigrationListener:
    """Watches tracked Pump.fun mints for migration to Raydium."""

    def __init__(
        self,
        on_migration: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
        session_factory: async_sessionmaker[AsyncSession],
        check_interval: int = 60,
    ) -> None:
        self._on_migration = on_migration
        self._sf = session_factory
        self._check_interval = check_interval
        self._running = False
        self._tracked_mints: OrderedDict[str, None] = OrderedDict()
        self._migrated: set[str] = set()

    def track_mint(self, mint: str) -> None:
        """Add a Pump.fun mint to the watch list for migration events."""
        self._tracked_mints[mint] = None
        # Cap memory: evict oldest entries (FIFO)
        while len(self._tracked_mints) > 5_000:
            self._tracked_mints.popitem(last=False)

    async def start(self) -> None:
        """Periodically check tracked mints for migration."""
        self._running = True
        logger.info(f"Migration listener started (interval: {self._check_interval}s)")

        while self._running:
            try:
                await self._check_migrations()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Migration check failed: {e}")

            await asyncio.sleep(self._check_interval)

    async def _check_migrations(self) -> None:
        """Check each tracked mint for migration to Raydium, PumpSwap, or other DEXes."""
        for mint in list(self._tracked_mints):
            if mint in self._migrated:
                continue

            try:
                sigs = await rpc.get_signatures_for_address(mint, limit=10)
                for sig_info in sigs:
                    sig = sig_info.get("signature", "")
                    tx = await rpc.get_transaction(sig)
                    if not tx:
                        continue

                    msg = tx.get("transaction", {}).get("message", {})
                    account_keys = msg.get("accountKeys", [])
                    keys = [
                        (ak if isinstance(ak, str) else ak.get("pubkey", ""))
                        for ak in account_keys
                    ]

                    # Known DEX program IDs for migration detection
                    migration_targets = {
                        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium_amm_v4",
                        "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium_cpmm",
                        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium_clmm",
                        "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP": "pumpswap",
                    }

                    matched_dex = None
                    for program_id, dex_name in migration_targets.items():
                        if program_id in keys:
                            matched_dex = dex_name
                            break

                    if matched_dex:
                        self._migrated.add(mint)
                        self._tracked_mints.pop(mint, None)
                        logger.info(f"🔄 Migration detected: {mint[:16]}... → {matched_dex}")
                        await self._on_migration({
                            "mint": mint,
                            "signature": sig,
                            "slot": sig_info.get("slot", 0),
                            "block_time": sig_info.get("blockTime", 0),
                            "dex": matched_dex,
                        })
                        break

            except Exception as e:
                logger.debug(f"Migration check for {mint[:12]}: {e}")

    async def stop(self) -> None:
        self._running = False
        logger.info("Migration listener stopped")


class MigrationAnalyzer:
    """Analyzes deployer behavior after a Pump.fun → Raydium migration."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def analyze_post_migration(
        self, event: dict[str, Any]
    ) -> dict[str, Any]:
        """Check if the deployer sold tokens after migration.

        Returns a dict with flags and a boolean indicating suspicious
        post-migration activity.
        """
        mint = event.get("mint", "")
        migration_time = event.get("block_time", 0)
        flags: list[str] = []
        deployer_sold = False

        try:
            # Find the deployer from DB or from the migration TX
            deployer = ""
            from src.models import TokenLaunch

            async with self._sf() as session:
                row = (
                    await session.execute(
                        select(TokenLaunch).where(TokenLaunch.mint == mint)
                    )
                ).scalar_one_or_none()
                if row:
                    deployer = row.deployer

            if not deployer:
                return {
                    "deployer_sold_post_migration": False,
                    "flags": ["Could not identify deployer"],
                }

            # Check deployer's recent TXs for token sells after migration
            sigs = await rpc.get_signatures_for_address(deployer, limit=20)

            for sig_info in sigs:
                block_time = sig_info.get("blockTime", 0)
                if block_time < migration_time:
                    continue  # only post-migration TXs

                sig = sig_info.get("signature", "")
                tx = await rpc.get_transaction(sig)
                if not tx:
                    continue

                meta = tx.get("meta", {})
                pre_token = meta.get("preTokenBalances", [])
                post_token = meta.get("postTokenBalances", [])

                for ptb in post_token:
                    if ptb.get("mint") != mint:
                        continue
                    owner = ptb.get("owner", "")
                    if owner != deployer:
                        continue

                    post_amount = int(
                        ptb.get("uiTokenAmount", {}).get("amount", "0")
                    )
                    pre_amount = 0
                    for prb in pre_token:
                        if (
                            prb.get("mint") == mint
                            and prb.get("owner") == deployer
                        ):
                            pre_amount = int(
                                prb.get("uiTokenAmount", {}).get("amount", "0")
                            )
                            break

                    if post_amount < pre_amount:
                        sold = pre_amount - post_amount
                        pct = (sold / pre_amount * 100) if pre_amount > 0 else 0
                        deployer_sold = True
                        flags.append(
                            f"Deployer sold {pct:.0f}% of holdings "
                            f"within {block_time - migration_time}s of migration"
                        )

            if deployer_sold:
                flags.insert(0, "⚠️ Deployer sold tokens post-migration")
            else:
                flags.append("Deployer held position after migration")

        except Exception as e:
            logger.error(f"Post-migration analysis failed: {e}")
            flags.append(f"Analysis error: {e}")

        return {
            "deployer_sold_post_migration": deployer_sold,
            "flags": flags,
        }
