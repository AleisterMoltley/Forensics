"""Bundler detection orchestrator.

Runs all six bundler-derived analyzers against a token launch and
produces a combined ``bundler_score`` (0-100) that feeds into the
forensic pipeline's ``score_bundled`` dimension.

The six detectors, each derived from Anon-Bundler source code:

  1. Funding fan-out   (funding.ts)     → master→N same-amount transfers
  2. Same-slot bundle  (jito.ts)        → create+buys+tip in one slot
  3. Reserve-aware buy (pumpfun.ts)     → bonding curve precision entry
  4. Wash trades       (volumeBot.ts)   → 70/30 buy/sell, tight cadence
  5. Coordinated exit  (autoSell.ts)    → staggered multi-wallet dump
  6. Recovery sweep    (recover.ts)     → N→1 SOL drain post-rug

Usage from the forensic pipeline::

    from src.analyzers.bundler_orchestrator import analyze_bundler

    result = await analyze_bundler(mint, deployer)
    score = result.combined_score
    flags = result.all_flags
    raw   = result.to_dict()
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.analyzers.funding_fanout import analyze_funding_fanout, FanOutResult
from src.analyzers.same_slot_bundle import analyze_same_slot_bundle, BundleResult
from src.analyzers.reserve_buys import analyze_reserve_buys, ReserveAnalysisResult
from src.analyzers.wash_trades import analyze_wash_trading, WashTradeResult
from src.analyzers.coordinated_exit import analyze_coordinated_exit, CoordinatedExitResult
from src.analyzers.recovery_sweep import analyze_recovery_sweep, SweepResult


# Weight each detector's score in the combined result.
# Same-slot bundle and funding fan-out are the strongest pre-launch signals.
# Wash trades and coordinated exit are post-launch confirmations.
# Recovery sweep is a post-rug confirmation (may not be available at scan time).
WEIGHTS = {
    "funding_fanout": 0.20,
    "same_slot_bundle": 0.25,
    "reserve_buys": 0.15,
    "wash_trades": 0.15,
    "coordinated_exit": 0.15,
    "recovery_sweep": 0.10,
}


@dataclass
class BundlerAnalysis:
    """Combined result from all six bundler detectors."""

    funding: FanOutResult | None = None
    bundle: BundleResult | None = None
    reserves: ReserveAnalysisResult | None = None
    wash: WashTradeResult | None = None
    exit: CoordinatedExitResult | None = None
    sweep: SweepResult | None = None

    combined_score: float = 0.0
    detectors_triggered: int = 0
    all_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "combined_score": self.combined_score,
            "detectors_triggered": self.detectors_triggered,
            "flags": self.all_flags,
            "funding_fanout": self.funding.to_dict() if self.funding else None,
            "same_slot_bundle": self.bundle.to_dict() if self.bundle else None,
            "reserve_buys": self.reserves.to_dict() if self.reserves else None,
            "wash_trades": self.wash.to_dict() if self.wash else None,
            "coordinated_exit": self.exit.to_dict() if self.exit else None,
            "recovery_sweep": self.sweep.to_dict() if self.sweep else None,
        }


async def analyze_bundler(
    mint: str,
    deployer: str,
    funded_wallets: list[str] | None = None,
    skip_post_launch: bool = False,
) -> BundlerAnalysis:
    """Run the full bundler detection suite against a token launch.

    Parameters
    ----------
    mint : str
        Token mint address.
    deployer : str
        Deployer / creator wallet address.
    funded_wallets : list[str], optional
        Pre-identified bundle wallets (from the funding fan-out detector
        or from other analyzers).  If not provided, the fan-out detector
        will identify them.
    skip_post_launch : bool
        If True, skip wash-trade and coordinated-exit analysis (useful
        for real-time scanning where the token just launched and there's
        no post-launch activity yet).
    """
    result = BundlerAnalysis()

    try:
        # Phase 1: Pre-launch detectors (run in parallel)
        logger.info(f"Bundler analysis: starting for {mint[:16]}...")

        phase1 = await asyncio.gather(
            analyze_funding_fanout(deployer, mint=mint),
            analyze_same_slot_bundle(mint, deployer),
            analyze_reserve_buys(mint),
            return_exceptions=True,
        )

        # Unpack results
        if not isinstance(phase1[0], Exception):
            result.funding = phase1[0]
        else:
            logger.warning(f"Funding fan-out failed: {phase1[0]}")

        if not isinstance(phase1[1], Exception):
            result.bundle = phase1[1]
        else:
            logger.warning(f"Same-slot bundle failed: {phase1[1]}")

        if not isinstance(phase1[2], Exception):
            result.reserves = phase1[2]
        else:
            logger.warning(f"Reserve buys failed: {phase1[2]}")

        # Collect funded wallets for the sweep detector
        if funded_wallets is None and result.funding and result.funding.funded_wallets:
            funded_wallets = result.funding.funded_wallets

        # Also add same-slot bundle wallets
        if result.bundle and result.bundle.same_slot_wallets:
            extra = result.bundle.same_slot_wallets
            if funded_wallets:
                funded_wallets = list(set(funded_wallets + extra))
            else:
                funded_wallets = extra

        # Phase 2: Post-launch detectors (if not skipped)
        if not skip_post_launch:
            phase2 = await asyncio.gather(
                analyze_wash_trading(mint),
                analyze_coordinated_exit(mint, deployer),
                return_exceptions=True,
            )

            if not isinstance(phase2[0], Exception):
                result.wash = phase2[0]
            else:
                logger.warning(f"Wash trade analysis failed: {phase2[0]}")

            if not isinstance(phase2[1], Exception):
                result.exit = phase2[1]
            else:
                logger.warning(f"Coordinated exit failed: {phase2[1]}")

        # Phase 3: Recovery sweep (only if we have wallet list)
        if funded_wallets and len(funded_wallets) >= 2:
            try:
                result.sweep = await analyze_recovery_sweep(funded_wallets, deployer)
            except Exception as e:
                logger.warning(f"Recovery sweep failed: {e}")

        # Aggregate scores
        scores: dict[str, float] = {}

        if result.funding:
            scores["funding_fanout"] = result.funding.score
        if result.bundle:
            scores["same_slot_bundle"] = result.bundle.score
        if result.reserves:
            scores["reserve_buys"] = result.reserves.score
        if result.wash:
            scores["wash_trades"] = result.wash.score
        if result.exit:
            scores["coordinated_exit"] = result.exit.score
        if result.sweep:
            scores["recovery_sweep"] = result.sweep.score

        # Weighted average (only across detectors that ran)
        if scores:
            total_weight = sum(WEIGHTS[k] for k in scores)
            weighted_sum = sum(scores[k] * WEIGHTS[k] for k in scores)
            base_score = weighted_sum / total_weight if total_weight > 0 else 0

            # Bonus: multiple detectors triggering is a strong confirmation
            triggered = sum(1 for s in scores.values() if s >= 25)
            result.detectors_triggered = triggered

            multi_detector_bonus = 0
            if triggered >= 4:
                multi_detector_bonus = 15
            elif triggered >= 3:
                multi_detector_bonus = 10
            elif triggered >= 2:
                multi_detector_bonus = 5

            result.combined_score = min(100.0, round(base_score + multi_detector_bonus, 1))

        # Collect all flags
        for det in [result.funding, result.bundle, result.reserves,
                     result.wash, result.exit, result.sweep]:
            if det and hasattr(det, "flags"):
                result.all_flags.extend(det.flags)

        if result.detectors_triggered >= 3:
            result.all_flags.insert(
                0,
                f"⚡ {result.detectors_triggered}/6 bundler detectors triggered "
                f"(combined score: {result.combined_score})"
            )

        logger.info(
            f"Bundler analysis complete: score={result.combined_score}, "
            f"detectors={result.detectors_triggered}/6, "
            f"flags={len(result.all_flags)}"
        )

        return result

    except Exception as e:
        logger.error(f"Bundler orchestrator failed: {e}")
        return result
