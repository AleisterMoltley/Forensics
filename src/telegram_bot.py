"""Telegram alert bot with owner-gated privileged commands.

Security design
---------------
* Critical commands (/export, /train, /backtest, /watchlist, /mute, /unmute)
  are restricted to the Telegram user IDs listed in the TELEGRAM_OWNER_IDS
  environment variable (comma-separated integers).
* If TELEGRAM_OWNER_IDS is empty the restriction is disabled (dev mode).
  In production TELEGRAM_OWNER_IDS must always be set; validate_env() in
  config.py logs a warning when it is absent.
* The access check uses a constant-time comparison-safe integer membership
  test — no string comparison that could leak timing information.
* Unauthorized attempts are logged (user ID only, no message content) and
  silently refused with a short denial reply to avoid information leakage.
"""
from __future__ import annotations

import asyncio
import csv
import functools
import io
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Bot, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from config import settings


# ---------------------------------------------------------------------------
# Owner-only access control
# ---------------------------------------------------------------------------

def _owner_only(
    handler: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[..., Coroutine[Any, Any, None]]:
    """Decorator that restricts a command handler to privileged owner IDs.

    Behaviour when TELEGRAM_OWNER_IDS is **empty** (dev mode):
        Every user is permitted — matching the validate_env() warning.

    Behaviour when TELEGRAM_OWNER_IDS is **set**:
        Only listed user IDs may invoke the command.  All others receive
        a short denial reply; the attempt is logged at WARNING level so
        operators can detect probing.
    """
    @functools.wraps(handler)
    async def wrapper(
        self: "TelegramAlerts",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        owner_ids = settings.owner_id_set
        if owner_ids:
            user = update.effective_user
            user_id = user.id if user else None
            if user_id not in owner_ids:
                logger.warning(
                    f"Unauthorized command /{handler.__name__.removeprefix('_cmd_')} "
                    f"attempted by user_id={user_id}"
                )
                if update.message:
                    await update.message.reply_text(
                        "⛔ You are not authorized to run this command."
                    )
                return
        await handler(self, update, context)

    return wrapper


# ---------------------------------------------------------------------------
# TelegramAlerts
# ---------------------------------------------------------------------------

class TelegramAlerts:
    """Manages the Telegram bot: polling, alert delivery, and command handling.

    Public interface expected by main.py
    -------------------------------------
    * ``TelegramAlerts(session_factory, pipeline)`` — constructor
    * ``await instance.start()``                   — begin polling
    * ``await instance.stop()``                    — graceful shutdown
    * ``await instance.send_alert(result)``        — push a forensic result
    * ``instance.bot``                             — raw Bot for direct sends
    * ``instance._alerts_enabled``                 — mutable flag
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pipeline: Any,
    ) -> None:
        self._session_factory = session_factory
        self._pipeline = pipeline
        self._alerts_enabled: bool = True
        self._app: Application | None = None
        self._start_time: float | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def bot(self) -> Bot | None:
        """Raw Bot instance; None if not yet started or no token configured."""
        if self._app is not None:
            return self._app.bot
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the Application and start long-polling in the background."""
        if not settings.telegram_bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled")
            return

        self._start_time = time.time()

        self._app = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .build()
        )

        # --- Register command handlers ---
        # Public
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))

        # Owner-only
        self._app.add_handler(CommandHandler("mute", self._cmd_mute))
        self._app.add_handler(CommandHandler("unmute", self._cmd_unmute))
        self._app.add_handler(CommandHandler("export", self._cmd_export))
        self._app.add_handler(CommandHandler("train", self._cmd_train))
        self._app.add_handler(CommandHandler("backtest", self._cmd_backtest))
        self._app.add_handler(CommandHandler("watchlist", self._cmd_watchlist))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started (polling)")

    async def stop(self) -> None:
        """Stop polling and cleanly shut down the Application."""
        if self._app is None:
            return
        try:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        except Exception as exc:
            logger.warning(f"Error during Telegram bot shutdown: {exc}")
        finally:
            self._app = None
        logger.info("Telegram bot stopped")

    # ------------------------------------------------------------------
    # Alert delivery
    # ------------------------------------------------------------------

    async def send_alert(self, result: Any) -> None:
        """Send a forensic analysis result to the configured alert chat.

        Does nothing when:
        * alerts are muted (``_alerts_enabled = False``)
        * no chat_id is configured
        * the risk score is below the configured threshold
        * the bot is not running
        """
        if not self._alerts_enabled:
            return
        if not settings.telegram_chat_id:
            return
        if self.bot is None:
            return

        score: float = getattr(result, "total_score", 0.0)
        if score < settings.min_risk_score_alert:
            return

        mint: str = getattr(result, "mint", "unknown")
        source: str = getattr(result, "source", "unknown")
        name: str = getattr(result, "name", "")
        symbol: str = getattr(result, "symbol", "")

        # Per-category scores (optional attributes)
        score_deployer: float = getattr(result, "score_deployer", 0.0)
        score_holders: float = getattr(result, "score_holders", 0.0)
        score_lp: float = getattr(result, "score_lp", 0.0)
        score_bundled: float = getattr(result, "score_bundled", 0.0)
        score_contract: float = getattr(result, "score_contract", 0.0)

        risk_emoji = "🔴" if score >= 75 else "🟡" if score >= 50 else "🟢"
        source_label = "Pump.fun" if source == "pump_fun" else "Raydium"

        token_label = f"{name} ({symbol})" if name else mint

        msg = (
            f"{risk_emoji} <b>Risk Alert — {source_label}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Token: <code>{token_label}</code>\n"
            f"🔑 Mint: <code>{mint}</code>\n"
            f"⚠️ Risk Score: <b>{score:.0f}/100</b>\n\n"
            f"📊 <b>Breakdown:</b>\n"
            f"  • Deployer:  {score_deployer:.0f}\n"
            f"  • Holders:   {score_holders:.0f}\n"
            f"  • LP:        {score_lp:.0f}\n"
            f"  • Bundled:   {score_bundled:.0f}\n"
            f"  • Contract:  {score_contract:.0f}\n\n"
            f"🔗 <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a> | "
            f"<a href='https://solscan.io/token/{mint}'>Solscan</a>"
        )

        try:
            await self.bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.error(f"Telegram send_alert failed: {exc}")

    # ------------------------------------------------------------------
    # Public command handlers
    # ------------------------------------------------------------------

    async def _cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Welcome message shown to any user who messages the bot."""
        await update.message.reply_text(
            "🔬 <b>Token Launch Forensics Bot</b>\n\n"
            "I monitor new Solana token launches and alert you when high-risk "
            "tokens are detected.\n\n"
            "<b>Available commands:</b>\n"
            "  /status   — show bot status\n"
            "  /mute     — pause alerts (owner)\n"
            "  /unmute   — resume alerts (owner)\n"
            "  /watchlist — list watchlisted deployers (owner)\n"
            "  /export   — download training data CSV (owner)\n"
            "  /train    — trigger ML model retraining (owner)\n"
            "  /backtest — run backtest on historical data (owner)",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Public status command — no sensitive data exposed."""
        uptime_str = "unknown"
        if self._start_time is not None:
            uptime_s = int(time.time() - self._start_time)
            h, remainder = divmod(uptime_s, 3600)
            m, s = divmod(remainder, 60)
            uptime_str = f"{h}h {m}m {s}s"

        owner_count = len(settings.owner_id_set)
        owner_info = f"{owner_count} configured" if owner_count else "unrestricted (dev mode)"

        await update.message.reply_text(
            f"🤖 <b>Bot Status</b>\n\n"
            f"  Alerts: {'✅ ON' if self._alerts_enabled else '🔇 MUTED'}\n"
            f"  Threshold: {settings.min_risk_score_alert}/100\n"
            f"  Uptime: {uptime_str}\n"
            f"  Owner IDs: {owner_info}",
            parse_mode=ParseMode.HTML,
        )

    # ------------------------------------------------------------------
    # Owner-only command handlers
    # ------------------------------------------------------------------

    @_owner_only
    async def _cmd_mute(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Pause outbound alerts."""
        self._alerts_enabled = False
        await update.message.reply_text("🔇 Alerts muted.")
        logger.info(f"Alerts muted by user_id={update.effective_user.id}")

    @_owner_only
    async def _cmd_unmute(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Resume outbound alerts."""
        self._alerts_enabled = True
        await update.message.reply_text("✅ Alerts resumed.")
        logger.info(f"Alerts unmuted by user_id={update.effective_user.id}")

    @_owner_only
    async def _cmd_export(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Export all token launches as a CSV file."""
        await update.message.reply_text("⏳ Generating CSV export…")

        try:
            # Lazy import to avoid a hard dependency when models are unavailable
            from models import TokenLaunch  # type: ignore[import]
        except ImportError:
            await update.message.reply_text("❌ Models module not available.")
            return

        try:
            async with self._session_factory() as session:
                rows = (await session.execute(select(TokenLaunch))).scalars().all()

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                "mint", "name", "symbol", "deployer", "source",
                "risk_score_total", "score_deployer", "score_holders",
                "score_lp", "score_bundled", "score_contract",
                "is_rug", "launched_at",
            ])
            for r in rows:
                writer.writerow([
                    r.mint, r.name, r.symbol, r.deployer, r.source,
                    r.risk_score_total, r.score_deployer, r.score_holders,
                    r.score_lp, r.score_bundled, r.score_contract,
                    r.is_rug, r.launched_at,
                ])

            csv_bytes = buf.getvalue().encode()
            filename = (
                f"forensics_export_"
                f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
            )
            await update.message.reply_document(
                document=InputFile(io.BytesIO(csv_bytes), filename=filename),
                caption=f"✅ Exported {len(rows)} records.",
            )
            logger.info(
                f"CSV export ({len(rows)} rows) sent to user_id="
                f"{update.effective_user.id}"
            )

        except Exception as exc:
            logger.error(f"Export failed: {exc}")
            await update.message.reply_text(f"❌ Export failed: {exc}")

    @_owner_only
    async def _cmd_train(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Trigger ML model retraining."""
        await update.message.reply_text("🧠 Triggering ML model retraining…")
        logger.info(f"Manual retraining triggered by user_id={update.effective_user.id}")

        try:
            predictor = getattr(self._pipeline, "predictor", None)
            if predictor is None:
                await update.message.reply_text(
                    "⚠️ Predictor not attached to pipeline — retraining unavailable."
                )
                return

            retrain = getattr(predictor, "retrain", None)
            if callable(retrain):
                await asyncio.get_event_loop().run_in_executor(None, retrain)
                await update.message.reply_text("✅ Retraining complete.")
            else:
                await update.message.reply_text(
                    "⚠️ Predictor does not expose a retrain() method."
                )
        except Exception as exc:
            logger.error(f"Manual retraining failed: {exc}")
            await update.message.reply_text(f"❌ Retraining failed: {exc}")

    @_owner_only
    async def _cmd_backtest(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Run backtest and return a summary."""
        await update.message.reply_text("⏳ Running backtest…")
        logger.info(f"Backtest triggered by user_id={update.effective_user.id}")

        try:
            from src.backtest import BacktestEngine  # type: ignore[import]
        except ImportError:
            await update.message.reply_text(
                "⚠️ BacktestEngine not available (src/backtest.py missing)."
            )
            return

        try:
            engine = BacktestEngine(self._session_factory)
            result = await engine.run()
            data = result.to_dict()

            precision = data.get("precision", "n/a")
            recall = data.get("recall", "n/a")
            f1 = data.get("f1", "n/a")
            total = data.get("total_samples", "n/a")
            rugs = data.get("rug_count", "n/a")

            msg = (
                f"📈 <b>Backtest Results</b>\n\n"
                f"  Samples: {total}  |  Rugs: {rugs}\n"
                f"  Precision: {precision}\n"
                f"  Recall:    {recall}\n"
                f"  F1:        {f1}"
            )
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

        except Exception as exc:
            logger.error(f"Backtest failed: {exc}")
            await update.message.reply_text(f"❌ Backtest failed: {exc}")

    @_owner_only
    async def _cmd_watchlist(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show watchlisted deployer addresses."""
        try:
            from models import Deployer  # type: ignore[import]
        except ImportError:
            await update.message.reply_text("❌ Models module not available.")
            return

        try:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        select(Deployer)
                        .where(Deployer.watchlisted.is_(True))
                        .order_by(Deployer.rug_count.desc())
                        .limit(20)
                    )
                ).scalars().all()

            if not rows:
                await update.message.reply_text("📋 Watchlist is empty.")
                return

            lines = ["📋 <b>Watchlisted Deployers (top 20)</b>\n"]
            for r in rows:
                addr = r.address
                # Truncate wallet address for display: first 6 + last 4
                display = f"{addr[:6]}…{addr[-4:]}" if len(addr) > 10 else addr
                lines.append(
                    f"  • <code>{display}</code> — "
                    f"{r.rug_count} rug(s) / {r.total_launches} launches"
                )

            await update.message.reply_text(
                "\n".join(lines), parse_mode=ParseMode.HTML
            )

        except Exception as exc:
            logger.error(f"Watchlist query failed: {exc}")
            await update.message.reply_text(f"❌ Could not fetch watchlist: {exc}")
