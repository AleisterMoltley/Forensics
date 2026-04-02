"""Forensic analysis pipeline.

Orchestrates all analyzers (heuristic + bundler) against a token launch
and produces a scored result that feeds into alerts, the dashboard, and
the ML training loop.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.analyzers.bundler_orchestrator import analyze_bundler
from src.analyzers.rpc import rpc
from src.config import settings
from src.models import TokenLaunch, Deployer


@dataclass
class PipelineResult:
    """Result of the full forensic analysis pipeline."""

    mint: str = ""
    name: str = ""
    symbol: str = ""
    deployer: str = ""
    source: str = ""

    # Dimension scores (0-100 each)
    total_score: float = 0.0
    score_deployer: float = 0.0
    score_holders: float = 0.0
    score_lp: float = 0.0
    score_bundled: float = 0.0
    score_contract: float = 0.0
    score_social: float = 0.0

    # Raw analysis data
    deployer_data: dict[str, Any] = field(default_factory=dict)
    holder_data: dict[str, Any] = field(default_factory=dict)
    lp_data: dict[str, Any] = field(default_factory=dict)
    bundle_data: dict[str, Any] = field(default_factory=dict)
    contract_data: dict[str, Any] = field(default_factory=dict)
    social_data: dict[str, Any] = field(default_factory=dict)

    # Market data
    mcap: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "deployer": self.deployer,
            "source": self.source,
            "total_score": self.total_score,
            "score_deployer": self.score_deployer,
            "score_holders": self.score_holders,
            "score_lp": self.score_lp,
            "score_bundled": self.score_bundled,
            "score_contract": self.score_contract,
            "score_social": self.score_social,
            "bundle_data": self.bundle_data,
            "deployer_data": self.deployer_data,
            "mcap": self.mcap,
        }


# Dimension weights for the total risk score
DIMENSION_WEIGHTS = {
    "deployer": 0.25,
    "holders": 0.15,
    "lp": 0.15,
    "bundled": 0.20,
    "contract": 0.10,
    "social": 0.15,
}


class ForensicPipeline:
    """Runs the full forensic analysis suite against a token launch."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
        self.predictor: Any = None  # Attached by main.py (ML model)

    async def analyze(self, launch: dict[str, Any]) -> PipelineResult | None:
        """Run all analyzers against a launch event.

        Parameters
        ----------
        launch : dict
            Must contain at least ``mint`` and ideally ``deployer``,
            ``source``, ``name``, ``symbol``.
        """
        mint = launch.get("mint", "")
        if not mint:
            return None

        result = PipelineResult(
            mint=mint,
            name=launch.get("name", ""),
            symbol=launch.get("symbol", ""),
            source=launch.get("source", "unknown"),
        )

        start = time.time()

        try:
            # Resolve deployer if not provided
            deployer = launch.get("deployer", "")
            if not deployer:
                deployer = await self._resolve_deployer(mint)
            result.deployer = deployer

            # --- Deployer history analysis ---
            if deployer:
                result.score_deployer, result.deployer_data = (
                    await self._analyze_deployer(deployer)
                )

            # --- Bundler analysis (6 detectors) ---
            if deployer:
                try:
                    bundler = await asyncio.wait_for(
                        analyze_bundler(
                            mint,
                            deployer,
                            skip_post_launch=(launch.get("source") == "pump_fun"),
                        ),
                        timeout=90.0,
                    )
                    result.score_bundled = bundler.combined_score
                    result.bundle_data = bundler.to_dict()
                except asyncio.TimeoutError:
                    logger.warning(f"Bundler analysis timed out (90s) for {mint[:16]}")
                except Exception as e:
                    logger.warning(f"Bundler analysis failed: {e}")

            # --- Token extension / contract checks ---
            try:
                result.score_contract, result.contract_data = (
                    await self._analyze_token_extensions(mint)
                )
            except Exception as e:
                logger.debug(f"Token extension check failed: {e}")

            # --- Compute total weighted score ---
            # Only include dimensions that have an active analyzer
            # (i.e. produced a non-zero score).  This prevents inactive
            # dimensions from diluting the total to near-zero.
            scores = {
                "deployer": result.score_deployer,
                "holders": result.score_holders,
                "lp": result.score_lp,
                "bundled": result.score_bundled,
                "contract": result.score_contract,
                "social": result.score_social,
            }
            active = {k: v for k, v in scores.items() if v > 0}
            if active:
                active_weight = sum(DIMENSION_WEIGHTS[k] for k in active)
                total = sum(
                    active[k] * DIMENSION_WEIGHTS[k] for k in active
                ) / active_weight * 1.0  # normalize to 0-100 range
            else:
                total = 0.0

            # ML model overlay (if available)
            if self.predictor and hasattr(self.predictor, "predict"):
                try:
                    ml_score = self.predictor.predict(scores)
                    if ml_score is not None:
                        # Blend: 70% heuristic + 30% ML
                        total = total * 0.7 + ml_score * 0.3
                except Exception as e:
                    logger.debug(f"ML prediction failed, heuristic-only: {e}")

            result.total_score = min(100.0, round(total, 1))

            # --- Extract market cap from launch data ---
            # Pump.fun WS events often include market_cap or marketCapSol
            raw = launch.get("raw", {})
            mcap = (
                raw.get("usd_market_cap")
                or raw.get("marketCapSol")  # SOL-denominated, rough proxy
                or raw.get("market_cap")
                or launch.get("mcap")
            )
            if mcap is not None:
                try:
                    result.mcap = float(mcap)
                except (ValueError, TypeError):
                    pass

            # --- Persist to database ---
            await self._persist(result)

            duration = (time.time() - start) * 1000
            mcap_str = f" mcap=${result.mcap:,.0f}" if result.mcap else ""
            logger.info(
                f"Pipeline: {mint[:16]}... → score={result.total_score}"
                f"{mcap_str} ({duration:.0f}ms)"
            )

            return result

        except Exception as e:
            logger.error(f"Pipeline analysis failed for {mint[:16]}: {e}")
            return None

    async def _resolve_deployer(self, mint: str) -> str:
        """Find the deployer of a token from its earliest transaction."""
        try:
            sigs = await rpc.get_signatures_for_address(mint, limit=5)
            if not sigs:
                return ""
            # Earliest TX is last in the list (newest-first order)
            tx = await rpc.get_transaction(sigs[-1].get("signature", ""))
            if not tx:
                return ""
            keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
            if keys:
                first = keys[0]
                return first if isinstance(first, str) else first.get("pubkey", "")
        except Exception as e:
            logger.debug(f"Deployer resolution failed for {mint[:16]}: {e}")
        return ""

    async def _analyze_deployer(
        self, deployer: str
    ) -> tuple[float, dict[str, Any]]:
        """Score the deployer based on historical behavior."""
        score = 0.0
        data: dict[str, Any] = {}

        try:
            async with self._sf() as session:
                dep = (
                    await session.execute(
                        select(Deployer).where(Deployer.address == deployer)
                    )
                ).scalar_one_or_none()

                if dep:
                    data = {
                        "total_launches": dep.total_launches,
                        "rug_count": dep.rug_count,
                        "watchlisted": dep.watchlisted,
                    }

                    if dep.total_launches > 0:
                        rug_rate = dep.rug_count / dep.total_launches
                        score = min(100, rug_rate * 120)

                    if dep.watchlisted:
                        score = min(100, score + 30)

                    # Serial deployer penalty
                    if dep.total_launches >= 10:
                        score = min(100, score + 15)

                else:
                    # Unknown deployer — slight risk
                    data = {"total_launches": 0, "rug_count": 0, "new_deployer": True}
                    score = 10.0

        except Exception as e:
            logger.debug(f"Deployer analysis error: {e}")

        return round(score, 1), data

    async def _analyze_token_extensions(
        self, mint: str
    ) -> tuple[float, dict[str, Any]]:
        """Check token account for dangerous authorities and Token-2022 extensions.

        Detects:
        - Mint authority retained (can inflate supply)
        - Freeze authority active (can freeze holder accounts)
        - Permanent Delegate extension (can burn/transfer any holder's tokens)
        - Transfer Fee extension (can skim from every transfer)
        - Non-Transferable extension (tokens locked in wallets)
        """
        score = 0.0
        data: dict[str, Any] = {}
        flags: list[str] = []

        try:
            account_info = await rpc.get_account_info(mint)
            if not account_info:
                return 0.0, data

            parsed = account_info.get("value", {})
            if not parsed:
                return 0.0, data

            account_data = parsed.get("data", {})
            parsed_data = account_data.get("parsed", {}) if isinstance(account_data, dict) else {}
            info = parsed_data.get("info", {})

            # --- Standard SPL Token checks ---

            # Mint authority retained → deployer can inflate supply at will
            mint_authority = info.get("mintAuthority")
            if mint_authority:
                score += 25
                flags.append(f"Mint authority retained: {mint_authority[:12]}...")
                data["mint_authority"] = mint_authority

            # Freeze authority active → can freeze any holder's token account
            freeze_authority = info.get("freezeAuthority")
            if freeze_authority:
                score += 20
                flags.append(f"Freeze authority active: {freeze_authority[:12]}...")
                data["freeze_authority"] = freeze_authority

            # --- Token-2022 Extension checks ---
            extensions = info.get("extensions", [])
            if isinstance(extensions, list):
                ext_names = []
                for ext in extensions:
                    ext_type = ext.get("extension", "") if isinstance(ext, dict) else str(ext)
                    ext_names.append(ext_type)

                    # Permanent Delegate — can burn or transfer ANY holder's tokens
                    if ext_type == "permanentDelegate":
                        delegate = ext.get("state", {}).get("delegate", "")
                        score += 35
                        flags.append(f"PERMANENT DELEGATE: {delegate[:12]}... can seize any holder's tokens")
                        data["permanent_delegate"] = delegate

                    # Transfer Fee — skims a % from every transfer
                    elif ext_type == "transferFeeConfig":
                        fee_bps = ext.get("state", {}).get("newerTransferFee", {}).get("transferFeeBasisPoints", 0)
                        if fee_bps and int(fee_bps) > 0:
                            fee_pct = int(fee_bps) / 100
                            score += 10 if fee_pct <= 5 else 20
                            flags.append(f"Transfer fee: {fee_pct:.1f}%")
                            data["transfer_fee_bps"] = fee_bps

                    # Non-Transferable — tokens cannot be transferred (soulbound)
                    elif ext_type == "nonTransferable":
                        score += 15
                        flags.append("Non-transferable token (cannot be sold)")

                    # Interest-bearing — can manipulate displayed balances
                    elif ext_type == "interestBearingConfig":
                        score += 5
                        flags.append("Interest-bearing token extension")

                if ext_names:
                    data["extensions"] = ext_names

            data["flags"] = flags
            data["is_token_2022"] = parsed_data.get("type") == "mint" and bool(extensions)

        except Exception as e:
            logger.debug(f"Token extension analysis error for {mint[:16]}: {e}")

        return min(100.0, round(score, 1)), data

    async def _persist(self, result: PipelineResult) -> None:
        """Save or update the analysis result in the database."""
        try:
            async with self._sf() as session:
                existing = (
                    await session.execute(
                        select(TokenLaunch).where(
                            TokenLaunch.mint == result.mint
                        )
                    )
                ).scalar_one_or_none()

                if existing:
                    existing.risk_score_total = result.total_score
                    existing.score_deployer = result.score_deployer
                    existing.score_holders = result.score_holders
                    existing.score_lp = result.score_lp
                    existing.score_bundled = result.score_bundled
                    existing.score_contract = result.score_contract
                    existing.score_social = result.score_social
                    existing.deployer_data = result.deployer_data
                    existing.bundle_data = result.bundle_data
                    if result.mcap is not None:
                        existing.current_mcap = result.mcap
                        if existing.peak_mcap is None or result.mcap > existing.peak_mcap:
                            existing.peak_mcap = result.mcap
                else:
                    session.add(
                        TokenLaunch(
                            mint=result.mint,
                            name=result.name,
                            symbol=result.symbol,
                            deployer=result.deployer,
                            source=result.source,
                            risk_score_total=result.total_score,
                            score_deployer=result.score_deployer,
                            score_holders=result.score_holders,
                            score_lp=result.score_lp,
                            score_bundled=result.score_bundled,
                            score_contract=result.score_contract,
                            score_social=result.score_social,
                            deployer_data=result.deployer_data,
                            bundle_data=result.bundle_data,
                            current_mcap=result.mcap,
                            peak_mcap=result.mcap,
                        )
                    )

                # Update deployer record — atomic increment to prevent
                # race conditions when multiple queue workers process
                # launches from the same deployer concurrently.
                if result.deployer:
                    rows_updated = (
                        await session.execute(
                            sa_update(Deployer)
                            .where(Deployer.address == result.deployer)
                            .values(
                                total_launches=Deployer.total_launches + 1,
                                last_seen=datetime.now(timezone.utc),
                            )
                        )
                    ).rowcount
                    if not rows_updated:
                        session.add(
                            Deployer(
                                address=result.deployer,
                                total_launches=1,
                            )
                        )

                await session.commit()

        except Exception as e:
            logger.error(f"Pipeline persist failed: {e}")
