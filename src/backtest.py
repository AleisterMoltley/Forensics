"""Backtest engine — evaluates the ML model against historical data.

Runs the rug predictor against all labeled tokens in the database and
computes precision, recall, F1, and accuracy metrics.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import TokenLaunch


@dataclass
class BacktestResult:
    """Aggregate backtest metrics."""

    total_samples: int = 0
    rug_count: int = 0
    survived_count: int = 0
    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    accuracy: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_samples": self.total_samples,
            "rug_count": self.rug_count,
            "survived_count": self.survived_count,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "true_negatives": self.true_negatives,
            "false_negatives": self.false_negatives,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "accuracy": round(self.accuracy, 4),
        }


# Risk score threshold for binary rug prediction
RUG_THRESHOLD = 60.0


class BacktestEngine:
    """Evaluates forensic scoring accuracy against known outcomes."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def run(self, threshold: float = RUG_THRESHOLD) -> BacktestResult:
        """Run backtest on all labeled tokens.

        A token with risk_score_total ≥ threshold is predicted as rug.
        Compares against the actual ``is_rug`` label.
        """
        result = BacktestResult()

        try:
            async with self._sf() as session:
                rows = (
                    await session.execute(
                        select(TokenLaunch).where(
                            TokenLaunch.is_rug.isnot(None)
                        )
                    )
                ).scalars().all()

            result.total_samples = len(rows)
            result.rug_count = sum(1 for r in rows if r.is_rug)
            result.survived_count = result.total_samples - result.rug_count

            for row in rows:
                predicted_rug = row.risk_score_total >= threshold
                actual_rug = row.is_rug

                if predicted_rug and actual_rug:
                    result.true_positives += 1
                elif predicted_rug and not actual_rug:
                    result.false_positives += 1
                elif not predicted_rug and not actual_rug:
                    result.true_negatives += 1
                elif not predicted_rug and actual_rug:
                    result.false_negatives += 1

            # Compute metrics
            tp, fp, fn = (
                result.true_positives,
                result.false_positives,
                result.false_negatives,
            )
            tn = result.true_negatives

            result.precision = tp / (tp + fp) if (tp + fp) > 0 else 0
            result.recall = tp / (tp + fn) if (tp + fn) > 0 else 0
            result.f1 = (
                2 * result.precision * result.recall
                / (result.precision + result.recall)
                if (result.precision + result.recall) > 0
                else 0
            )
            result.accuracy = (
                (tp + tn) / result.total_samples
                if result.total_samples > 0
                else 0
            )

            logger.info(
                f"Backtest: {result.total_samples} samples — "
                f"P={result.precision:.3f} R={result.recall:.3f} "
                f"F1={result.f1:.3f} Acc={result.accuracy:.3f}"
            )

        except Exception as e:
            logger.error(f"Backtest failed: {e}")

        return result
