"""Deployer alert network — in-memory reputation cache for instant alerts.

Maintains a fast lookup cache of deployer wallet addresses and their
risk profiles (rug counts, watchlist status) so that incoming launches
can be scored in <1ms without a DB round-trip.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import Deployer, TokenLaunch


class DeployerAlertNetwork:
    """Fast in-memory deployer reputation network."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
        self._cache: dict[str, dict[str, Any]] = {}
        self._watchlist: set[str] = set()

    async def load(self) -> None:
        """Load deployer data from the database into memory."""
        try:
            async with self._sf() as session:
                rows = (
                    await session.execute(select(Deployer))
                ).scalars().all()

                for dep in rows:
                    self._cache[dep.address] = {
                        "total_launches": dep.total_launches,
                        "rug_count": dep.rug_count,
                        "watchlisted": dep.watchlisted,
                        "rug_rate": (
                            dep.rug_count / dep.total_launches
                            if dep.total_launches > 0
                            else 0
                        ),
                    }
                    if dep.watchlisted:
                        self._watchlist.add(dep.address)

            logger.info(
                f"Deployer network loaded: {len(self._cache)} deployers, "
                f"{len(self._watchlist)} watchlisted"
            )

        except Exception as e:
            logger.error(f"Deployer network load failed: {e}")

    async def auto_watchlist_from_rugs(self, min_rugs: int = 2) -> None:
        """Auto-watchlist deployers with N+ confirmed rugs."""
        added = 0
        try:
            async with self._sf() as session:
                rows = (
                    await session.execute(
                        select(Deployer).where(
                            and_(
                                Deployer.rug_count >= min_rugs,
                                Deployer.watchlisted.is_(False),
                            )
                        )
                    )
                ).scalars().all()

                for dep in rows:
                    dep.watchlisted = True
                    self._watchlist.add(dep.address)
                    if dep.address in self._cache:
                        self._cache[dep.address]["watchlisted"] = True
                    added += 1

                await session.commit()

            if added > 0:
                logger.info(f"Auto-watchlisted {added} deployers (≥{min_rugs} rugs)")

        except Exception as e:
            logger.error(f"Auto-watchlist failed: {e}")

    def check_fast(self, deployer: str) -> dict[str, Any] | None:
        """Fast (<1ms) deployer reputation check from in-memory cache.

        Returns an alert dict if the deployer is known and risky,
        None otherwise.
        """
        info = self._cache.get(deployer)
        if not info:
            return None

        alerts: list[str] = []
        severity = "info"

        if info.get("watchlisted"):
            alerts.append("WATCHLISTED deployer")
            severity = "critical"

        rug_count = info.get("rug_count", 0)
        if rug_count >= 5:
            alerts.append(f"Serial rugger: {rug_count} confirmed rugs")
            severity = "critical"
        elif rug_count >= 2:
            alerts.append(f"Repeat rugger: {rug_count} confirmed rugs")
            severity = "warning"

        rug_rate = info.get("rug_rate", 0)
        if rug_rate >= 0.8 and info.get("total_launches", 0) >= 3:
            alerts.append(f"High rug rate: {rug_rate:.0%}")
            if severity != "critical":
                severity = "warning"

        if not alerts:
            return None

        return {
            "deployer": deployer,
            "severity": severity,
            "alerts": alerts,
            "info": info,
        }

    def format_alert(self, alert: dict[str, Any], launch: dict[str, Any]) -> str:
        """Format a deployer alert for Telegram."""
        severity = alert.get("severity", "info")
        emoji = "🚨" if severity == "critical" else "⚠️"
        deployer = alert.get("deployer", "")
        alerts = alert.get("alerts", [])
        mint = launch.get("mint", "?")
        name = launch.get("name", "")
        symbol = launch.get("symbol", "")
        token = f"{name} (${symbol})" if name else mint[:16]

        lines = [
            f"{emoji} <b>DEPLOYER ALERT — {severity.upper()}</b>",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"🪙 <b>{token}</b>",
            f"👤 <code>{deployer[:6]}…{deployer[-4:]}</code>",
            "",
        ]
        for a in alerts:
            lines.append(f"  ⚡ {a}")

        lines.append(
            f"\n🔗 <a href='https://dexscreener.com/solana/{mint}'>Chart</a>"
            f" · <a href='https://solscan.io/account/{deployer}'>Deployer</a>"
        )

        return "\n".join(lines)

    def update_cache(self, deployer: str, data: dict[str, Any]) -> None:
        """Update the in-memory cache for a deployer."""
        self._cache[deployer] = data
        if data.get("watchlisted"):
            self._watchlist.add(deployer)
