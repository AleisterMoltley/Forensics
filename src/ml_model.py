"""ML model for rug-pull prediction.

Provides a simple gradient-boosted classifier that learns from historical
forensic analysis outcomes.  The ``AutoRetrainer`` periodically rebuilds
the model as new labeled data accumulates.

Until enough labeled samples exist (MIN_SAMPLES), the model returns None
and the pipeline falls back to heuristic-only scoring.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import struct
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import settings

# Minimum labeled samples before the model will train
MIN_SAMPLES = 50
MODEL_PATH = Path("data/rug_model.pkl")
RETRAIN_INTERVAL_HOURS = 6


class RugPredictor:
    """Lightweight rug-pull probability predictor.

    Uses scikit-learn's GradientBoostingClassifier when available,
    otherwise provides a no-op fallback.
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._serializer: str = "none"  # "skops" | "joblib" | "none"
        self._feature_names = [
            "score_deployer",
            "score_holders",
            "score_lp",
            "score_bundled",
            "score_contract",
            "score_social",
        ]

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def predict(self, scores: dict[str, float]) -> float | None:
        """Predict rug probability (0-100) from dimension scores.

        Returns None if the model is not trained yet.
        """
        if not self._model:
            return None

        try:
            features = [scores.get(f.replace("score_", ""), 0.0) for f in self._feature_names]
            proba = self._model.predict_proba([features])[0][1]  # P(rug=True)
            return round(proba * 100, 1)
        except Exception as e:
            logger.debug(f"Prediction error: {e}")
            return None

    def retrain(self, X: list[list[float]], y: list[int]) -> bool:
        """Retrain the model on labeled data.

        Parameters
        ----------
        X : list of feature vectors
        y : list of labels (1 = rug, 0 = survived)

        Returns True if training succeeded.
        """
        if len(X) < MIN_SAMPLES:
            logger.info(f"ML: need {MIN_SAMPLES} samples, have {len(X)} — skipping")
            return False

        try:
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.model_selection import cross_val_score

            clf = GradientBoostingClassifier(
                n_estimators=100,
                max_depth=4,
                learning_rate=0.1,
                min_samples_leaf=5,
                random_state=42,
            )
            clf.fit(X, y)

            # Quick cross-validation
            cv_scores = cross_val_score(clf, X, y, cv=min(5, len(X) // 10 or 2))
            logger.info(
                f"ML: retrained on {len(X)} samples — "
                f"CV accuracy: {cv_scores.mean():.3f} ± {cv_scores.std():.3f}"
            )

            self._model = clf

            # Persist model securely.
            # Priority 1: skops (zero-pickle serialization, no RCE risk)
            # Priority 2: joblib + HMAC signature (pickle-based but signed)
            try:
                MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
                try:
                    from skops.io import dump as skops_dump
                    skops_dump(clf, MODEL_PATH)
                    self._serializer = "skops"
                    logger.info("ML: model saved via skops (pickle-free)")
                except ImportError:
                    import joblib
                    joblib.dump(clf, MODEL_PATH)
                    self._serializer = "joblib"
                    logger.info("ML: model saved via joblib (⚠️ install skops for pickle-free serialization)")
                # Write HMAC signature alongside the model file
                self._write_signature(MODEL_PATH)
            except Exception as e:
                logger.warning(f"ML: failed to save model: {e}")

            return True

        except ImportError:
            logger.warning("ML: scikit-learn not installed — model disabled")
            return False
        except Exception as e:
            logger.error(f"ML retrain failed: {e}")
            return False

    def load(self) -> bool:
        """Try to load a previously saved model with integrity verification.

        Security layers:
        1. HMAC-SHA256 signature verification (detects file tampering)
        2. Type check — loaded object must be a scikit-learn classifier
        3. Prefers skops (pickle-free) over joblib when available
        """
        try:
            if MODEL_PATH.exists():
                if not self._verify_signature(MODEL_PATH):
                    logger.warning(
                        "ML: model file HMAC signature mismatch — "
                        "refusing to load (possible tampering)"
                    )
                    return False

                loaded = None

                # Try skops first (pickle-free, no RCE risk)
                try:
                    from skops.io import load as skops_load
                    from skops.io import get_untrusted_types
                    # Only allow sklearn types — reject anything else
                    untrusted = get_untrusted_types(file=MODEL_PATH)
                    safe_prefixes = ("sklearn.", "numpy.")
                    for t in untrusted:
                        if not any(t.startswith(p) for p in safe_prefixes):
                            logger.warning(
                                f"ML: model contains untrusted type '{t}' — "
                                f"refusing to load"
                            )
                            return False
                    loaded = skops_load(MODEL_PATH, trusted=untrusted)
                    logger.info("ML: loaded via skops (pickle-free)")
                except ImportError:
                    pass
                except Exception as e:
                    logger.debug(f"ML: skops load failed, trying joblib: {e}")

                # Fall back to joblib (pickle-based)
                if loaded is None:
                    import joblib
                    loaded = joblib.load(MODEL_PATH)
                    logger.info("ML: loaded via joblib (signature verified)")

                # Type validation — the loaded object MUST be a sklearn
                # classifier, not arbitrary code smuggled via pickle.
                type_name = type(loaded).__module__ + "." + type(loaded).__qualname__
                if not type_name.startswith("sklearn."):
                    logger.warning(
                        f"ML: loaded object is {type_name}, not a sklearn model — "
                        f"refusing to use (possible pickle RCE attempt)"
                    )
                    return False

                if not hasattr(loaded, "predict_proba"):
                    logger.warning(
                        "ML: loaded object has no predict_proba — "
                        "not a valid classifier"
                    )
                    return False

                self._model = loaded
                return True
        except Exception as e:
            logger.warning(f"ML: failed to load model: {e}")
        return False

    # ------------------------------------------------------------------
    # Model file integrity (HMAC-SHA256)
    # ------------------------------------------------------------------
    # The signing key is derived from the Helius API key so that it is
    # unique per deployment but does not require an extra env variable.
    # This protects against accidental or malicious model-file replacement
    # on shared volumes.

    @staticmethod
    def _signing_key() -> bytes:
        """Derive a signing key from the deployment's Helius API key."""
        from src.config import settings
        seed = (settings.helius_api_key or "forensics-dev-key").encode()
        return hashlib.sha256(b"model-signing:" + seed).digest()

    @classmethod
    def _write_signature(cls, model_path: Path) -> None:
        """Write an HMAC-SHA256 signature file alongside the model."""
        data = model_path.read_bytes()
        sig = hmac.new(cls._signing_key(), data, hashlib.sha256).hexdigest()
        model_path.with_suffix(".sig").write_text(sig)

    @classmethod
    def _verify_signature(cls, model_path: Path) -> bool:
        """Verify the HMAC-SHA256 signature of a model file."""
        sig_path = model_path.with_suffix(".sig")
        if not sig_path.exists():
            logger.warning("ML: no signature file found for model")
            return False
        expected_sig = sig_path.read_text().strip()
        data = model_path.read_bytes()
        actual_sig = hmac.new(cls._signing_key(), data, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected_sig, actual_sig)


class AutoRetrainer:
    """Periodically retrains the ML model from the database."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
        self.predictor = RugPredictor()
        self.predictor.load()
        self._running = False

    async def start(self) -> None:
        """Run the periodic retrain loop."""
        self._running = True
        logger.info(f"AutoRetrainer started (interval: {RETRAIN_INTERVAL_HOURS}h)")

        while self._running:
            try:
                await self._retrain_from_db()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AutoRetrainer error: {e}")

            await asyncio.sleep(RETRAIN_INTERVAL_HOURS * 3600)

    async def _retrain_from_db(self) -> None:
        """Fetch labeled data from the database and retrain."""
        from src.models import TokenLaunch

        try:
            async with self._sf() as session:
                rows = (
                    await session.execute(
                        select(TokenLaunch).where(
                            TokenLaunch.is_rug.isnot(None)
                        )
                    )
                ).scalars().all()

            if len(rows) < MIN_SAMPLES:
                logger.info(
                    f"AutoRetrainer: {len(rows)} labeled samples "
                    f"(need {MIN_SAMPLES})"
                )
                return

            X = [
                [
                    r.score_deployer or 0,
                    r.score_holders or 0,
                    r.score_lp or 0,
                    r.score_bundled or 0,
                    r.score_contract or 0,
                    r.score_social or 0,
                ]
                for r in rows
            ]
            y = [1 if r.is_rug else 0 for r in rows]

            # Run training in executor to not block event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.predictor.retrain, X, y)

        except Exception as e:
            logger.error(f"AutoRetrainer: DB fetch failed: {e}")

    async def stop(self) -> None:
        self._running = False
        logger.info("AutoRetrainer stopped")
