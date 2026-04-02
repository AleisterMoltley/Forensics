"""Post-rug fund tracker.

After a rug is confirmed, traces where the extracted SOL goes.
Identifies CEX deposit addresses, mixer usage, and new wallets
that may be future deployers.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.analyzers.rpc import rpc
from src.config import settings
from src.models import TokenLaunch, Deployer


# Known CEX hot wallet addresses (Solana mainnet)
# Sources: Arkham Intelligence, Solscan labels, public documentation
# Last updated: 2026-04-02
KNOWN_CEX_ADDRESSES = frozenset({
    # Binance
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",  # Coinbase
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE",  # Coinbase 2
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",  # FTX (historical, still receives)
    # Kraken
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiWB7t",
    "CuieVDEDtLo7FypA9SbLM9saXFdb1dsshEkyErMqkRQq",
    # OKX
    "5VCwKtCXgCDuQosQgadj4KTfMn8MiPrGtrPiRPTGzRMf",
    "JA5cjQJpGp2oxRPC7gnLYUABpao5Go2V4C7REzSoe1Lr",
    # Bybit
    "AC5RDfQFmDS1deWZos921JfqscXdByf6BKHBKqejFgSk",
    # KuCoin
    "BmFdpraQhkiDQE6SnfG5PVddVeGzCycPGtivGqmJCD8d",
    # Gate.io
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w",
    # MEXC
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ",
    # Crypto.com
    "6FEVkH17P9y8Q9aCkDdPcMDjvj7SVxrTETaYEm8f51S2",
    # Backpack
    "BPk4RGqyjwPTi9XFNbwJMN35Uv4QTe2vvZhb5E3WKbou",
})


class PostRugTracker:
    """Tracks fund movements after confirmed rug-pulls."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        deployer_network: Any = None,
    ) -> None:
        self._sf = session_factory
        self._deployer_network = deployer_network
        self._running = False

    async def start(self) -> None:
        self._running = True
        interval = settings.post_rug_check_interval
        logger.info(f"Post-rug tracker started (interval: {interval}s)")

        while self._running:
            try:
                await self._trace_recent_rugs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Post-rug tracker error: {e}")

            await asyncio.sleep(interval)

    async def _trace_recent_rugs(self) -> None:
        """Find recently confirmed rugs and trace their fund flows."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        async with self._sf() as session:
            rugs = (
                await session.execute(
                    select(TokenLaunch).where(
                        and_(
                            TokenLaunch.is_rug.is_(True),
                            TokenLaunch.rug_detected_at >= cutoff,
                        )
                    ).limit(20)
                )
            ).scalars().all()

        for rug in rugs:
            try:
                await self._trace_deployer(rug.deployer, rug.mint)
            except Exception as e:
                logger.debug(f"Post-rug trace failed for {rug.mint[:12]}: {e}")

    async def _trace_deployer(self, deployer: str, mint: str) -> None:
        """Trace outbound SOL transfers from a rug deployer."""
        sigs = await rpc.get_signatures_for_address(deployer, limit=20)

        for sig_info in sigs:
            sig = sig_info.get("signature", "")
            try:
                tx = await rpc.get_transaction(sig)
                if not tx:
                    continue

                meta = tx.get("meta", {})
                if meta.get("err"):
                    continue

                msg = tx.get("transaction", {}).get("message", {})
                account_keys = msg.get("accountKeys", [])
                keys = [
                    (ak if isinstance(ak, str) else ak.get("pubkey", ""))
                    for ak in account_keys
                ]
                pre_balances = meta.get("preBalances", [])
                post_balances = meta.get("postBalances", [])

                # Find outbound transfers
                deployer_idx = None
                for i, k in enumerate(keys):
                    if k == deployer:
                        deployer_idx = i
                        break

                if deployer_idx is None:
                    continue

                for i, k in enumerate(keys):
                    if i == deployer_idx:
                        continue
                    pre = pre_balances[i] if i < len(pre_balances) else 0
                    post = post_balances[i] if i < len(post_balances) else 0
                    received = post - pre

                    if received > 100_000_000:  # > 0.1 SOL
                        if k in KNOWN_CEX_ADDRESSES:
                            logger.info(
                                f"📍 Post-rug: deployer sent {received/1e9:.2f} SOL "
                                f"to CEX ({k[:12]}...)"
                            )

                        # Track new destination wallet as potential future deployer
                        if self._deployer_network:
                            self._deployer_network.update_cache(k, {
                                "total_launches": 0,
                                "rug_count": 0,
                                "watchlisted": False,
                                "linked_to_rugger": deployer[:12],
                            })

            except Exception as e:
                logger.debug(f"Post-rug TX trace failed for {sig[:16]}: {e}")
                continue

    async def stop(self) -> None:
        self._running = False
        logger.info("Post-rug tracker stopped")
