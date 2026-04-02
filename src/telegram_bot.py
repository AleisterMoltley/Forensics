"""Telegram Forensics Bot — primary operator interface.

Security design
---------------
* Critical commands are restricted to TELEGRAM_OWNER_IDS.
* If TELEGRAM_OWNER_IDS is empty, restriction is disabled (dev mode).
* Unauthorized attempts are logged (user ID only) and silently refused.

All output is in English.
"""
from __future__ import annotations

import asyncio
import csv
import functools
import io
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Coroutine

from loguru import logger
from sqlalchemy import select, func, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Bot, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from src.config import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Solana base58 address validation (same pattern as dashboard.py)
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _validate_solana_address(v: str) -> bool:
    """Return True if v is a valid Solana base58 address."""
    return bool(_BASE58_RE.match(v))

def _ta(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 10 else addr

def _uptime(start: float | None) -> str:
    if not start:
        return "unknown"
    s = int(time.time() - start)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{d}d " if d else "") + f"{h}h {m}m {s}s"

def _bar(score: float, w: int = 10) -> str:
    f = round(score / 100 * w)
    return "█" * f + "░" * (w - f) + f" {score:.0f}"

def _remoji(s: float) -> str:
    if s >= 80: return "🔴"
    if s >= 60: return "🟠"
    if s >= 40: return "🟡"
    return "🟢"

def _rlabel(s: float) -> str:
    if s >= 80: return "CRITICAL"
    if s >= 60: return "HIGH"
    if s >= 40: return "MEDIUM"
    if s >= 20: return "LOW"
    return "CLEAN"

SEP = "━" * 28


# ---------------------------------------------------------------------------
# Owner-only decorator
# ---------------------------------------------------------------------------

def _owner_only(handler: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., Coroutine[Any, Any, None]]:
    @functools.wraps(handler)
    async def wrapper(self: "TelegramAlerts", update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ids = settings.owner_id_set
        # Block privileged commands when no owner IDs configured in production.
        # "Production" = Railway OR any network-exposed deployment (0.0.0.0).
        is_production = settings.is_railway or settings.dashboard_host == "0.0.0.0"
        if not ids and is_production:
            logger.warning(f"Blocked /{handler.__name__.removeprefix('_cmd_')}: TELEGRAM_OWNER_IDS not set in production")
            if update.message:
                await update.message.reply_text(
                    "⛔ Privileged commands are disabled.\n"
                    "Set TELEGRAM_OWNER_IDS to enable them."
                )
            return
        if ids:
            u = update.effective_user
            uid = u.id if u else None
            if uid not in ids:
                logger.warning(f"Unauthorized /{handler.__name__.removeprefix('_cmd_')} by uid={uid}")
                if update.message:
                    await update.message.reply_text("⛔ You are not authorized to run this command.")
                return
        await handler(self, update, context)
    return wrapper


# ---------------------------------------------------------------------------
# TelegramAlerts
# ---------------------------------------------------------------------------

class TelegramAlerts:

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], pipeline: Any) -> None:
        self._sf = session_factory
        self._pipeline = pipeline
        self._alerts_enabled: bool = True
        self._app: Application | None = None
        self._start_time: float | None = None
        self._scans_total: int = 0
        self._alerts_sent: int = 0

    @property
    def bot(self) -> Bot | None:
        return self._app.bot if self._app else None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if not settings.telegram_bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN not set — bot disabled")
            return
        self._start_time = time.time()

        # Restore persisted threshold from DB (M5 fix)
        try:
            from src.models import AlertConfig
            async with self._sf() as session:
                cfg = (await session.execute(
                    select(AlertConfig).where(AlertConfig.id == 1)
                )).scalar_one_or_none()
                if cfg and cfg.min_risk_threshold is not None:
                    settings.min_risk_score_alert = cfg.min_risk_threshold
                    logger.info(f"Restored alert threshold from DB: {cfg.min_risk_threshold}")
        except Exception as e:
            logger.debug(f"Could not load persisted threshold: {e}")

        self._app = Application.builder().token(settings.telegram_bot_token).build()

        public = [("start", self._cmd_start), ("help", self._cmd_help), ("status", self._cmd_status)]
        owner = [
            ("scan", self._cmd_scan), ("deployer", self._cmd_deployer),
            ("bundler", self._cmd_bundler), ("lookup", self._cmd_lookup),
            ("stats", self._cmd_stats), ("top", self._cmd_top),
            ("watchlist", self._cmd_watchlist), ("threshold", self._cmd_threshold),
            ("mute", self._cmd_mute), ("unmute", self._cmd_unmute),
            ("export", self._cmd_export), ("train", self._cmd_train),
            ("backtest", self._cmd_backtest),
        ]
        for name, h in public + owner:
            self._app.add_handler(CommandHandler(name, h))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started (polling)")

    async def stop(self) -> None:
        if not self._app:
            return
        try:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        except Exception as e:
            logger.warning(f"Telegram shutdown: {e}")
        finally:
            self._app = None
        logger.info("Telegram bot stopped")

    # ── Alert delivery ────────────────────────────────────────────────

    async def send_alert(self, result: Any) -> None:
        if not self._alerts_enabled or not settings.telegram_chat_id or not self.bot:
            return
        score: float = getattr(result, "total_score", 0.0)
        if score < settings.min_risk_score_alert:
            return

        mint = getattr(result, "mint", "?")
        source = getattr(result, "source", "?")
        name = getattr(result, "name", "")
        symbol = getattr(result, "symbol", "")
        deployer = getattr(result, "deployer", "")

        dims = []
        for lbl, attr in [("Deployer","score_deployer"),("Holders","score_holders"),
                           ("LP","score_lp"),("Bundle","score_bundled"),
                           ("Contract","score_contract"),("Social","score_social")]:
            v = getattr(result, attr, 0.0)
            if v > 0:
                dims.append(f"  {lbl:<10} {_bar(v, 8)}")

        token = f"{name} (${symbol})" if name else f"{mint[:16]}…"
        src = "Pump.fun" if source == "pump_fun" else "Raydium"

        # Bundler flags from bundle_data
        bd = getattr(result, "bundle_data", {}) or {}
        bf = bd.get("flags", [])
        flags_sec = ""
        if bf:
            flags_sec = "\n🔍 <b>Bundler Intel:</b>\n" + "\n".join(f"  ⚡ {f}" for f in bf[:6]) + "\n"

        # Deployer context
        dd = getattr(result, "deployer_data", {}) or {}
        dep_sec = ""
        if dd.get("total_launches", 0) > 0:
            dep_sec = (
                f"\n👤 Deployer: <code>{_ta(deployer)}</code>"
                f" — {dd['total_launches']} launches, {dd.get('rug_count',0)} rugs\n"
            )

        dims_text = "\n".join(dims)

        msg = (
            f"{_remoji(score)} <b>RISK ALERT — {_rlabel(score)}</b>\n{SEP}\n"
            f"🪙 <b>{token}</b>\n"
            f"📍 {src}  ·  ⚠️ <b>{score:.0f}/100</b>\n"
            f"🔑 <code>{mint}</code>\n\n"
            f"<pre>{dims_text}</pre>"
            f"{flags_sec}{dep_sec}\n"
            f"🔗 <a href='https://dexscreener.com/solana/{mint}'>Chart</a>"
            f" · <a href='https://solscan.io/token/{mint}'>Solscan</a>"
            f" · <a href='https://pump.fun/{mint}'>Pump</a>\n"
            f"💡 /scan {mint}"
        )

        try:
            await self.bot.send_message(
                chat_id=settings.telegram_chat_id, text=msg,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
            self._alerts_sent += 1
        except TelegramError as e:
            logger.error(f"Alert failed: {e}")

    # ── /start ────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🔬 <b>Token Launch Forensics Bot</b>\n\n"
            "Real-time forensic analysis of every Solana token launch.\n"
            "7 heuristic analyzers + ML scoring + 6 bundler detectors.\n\n"
            "Type /help for the full command reference.",
            parse_mode=ParseMode.HTML,
        )

    # ── /help ─────────────────────────────────────────────────────────

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            f"📖 <b>Command Reference</b>\n{SEP}\n\n"
            "<b>🔍 Intelligence</b>\n"
            "  /scan &lt;mint&gt; — Full forensic scan (7 analyzers + 6 bundler detectors)\n"
            "  /lookup &lt;mint&gt; — Quick database lookup with score breakdown\n"
            "  /deployer &lt;addr&gt; — Deployer wallet deep-dive (history, funding, tokens)\n"
            "  /bundler &lt;mint&gt; — Standalone bundler detection (all 6 engines)\n\n"
            "<b>📊 Analytics</b>\n"
            "  /stats — 24h statistics with score distribution\n"
            "  /top — Top 10 highest-risk tokens today\n"
            "  /backtest — ML model backtest on historical data\n\n"
            "<b>🛡 Watchlist</b>\n"
            "  /watchlist — View all watchlisted deployers\n"
            "  /watchlist add &lt;addr&gt; — Add to watchlist\n"
            "  /watchlist remove &lt;addr&gt; — Remove from watchlist\n\n"
            "<b>⚙️ Control</b>\n"
            "  /status — System health, uptime, counters\n"
            "  /threshold &lt;0-100&gt; — Set minimum alert score\n"
            "  /mute / /unmute — Pause or resume alerts\n"
            "  /train — Force ML model retrain\n"
            "  /export — Download full CSV dataset\n",
            parse_mode=ParseMode.HTML,
        )

    # ── /status ───────────────────────────────────────────────────────

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        oc = len(settings.owner_id_set)
        await update.message.reply_text(
            f"🤖 <b>System Status</b>\n{SEP}\n\n"
            f"  Status:    {'🟢 ONLINE' if self._alerts_enabled else '🔇 MUTED'}\n"
            f"  Uptime:    {_uptime(self._start_time)}\n"
            f"  Threshold: {settings.min_risk_score_alert}/100\n"
            f"  Owners:    {oc} configured\n\n"
            f"  Scans:     {self._scans_total}\n"
            f"  Alerts:    {self._alerts_sent}\n",
            parse_mode=ParseMode.HTML,
        )

    # ── /scan <mint> ──────────────────────────────────────────────────

    @_owner_only
    async def _cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: /scan &lt;mint_address&gt;\n\nRuns 7 analyzers + 6 bundler detectors (15-30s).",
                parse_mode=ParseMode.HTML)
            return

        mint = context.args[0].strip()
        if not _validate_solana_address(mint):
            await update.message.reply_text("❌ Invalid address format (expected base58, 32–44 chars).")
            return

        await update.message.reply_text(
            f"🔬 Deep scan: <code>{mint[:20]}…</code>\nRunning 7+6 analyzers…",
            parse_mode=ParseMode.HTML)

        try:
            # Pipeline scan
            pr = None
            if self._pipeline and hasattr(self._pipeline, "analyze"):
                pr = await self._pipeline.analyze({"mint": mint, "source": "manual_scan"})

            # Find deployer
            deployer = getattr(pr, "deployer", "") if pr else ""
            if not deployer:
                try:
                    from src.analyzers.rpc import rpc
                    sigs = await rpc.get_signatures_for_address(mint, limit=5)
                    if sigs:
                        tx = await rpc.get_transaction(sigs[-1].get("signature", ""))
                        if tx:
                            ks = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                            if ks:
                                deployer = ks[0] if isinstance(ks[0], str) else ks[0].get("pubkey", "")
                except Exception as e:
                    logger.debug(f"Deployer resolve in /scan failed: {e}")
            br = None
            if deployer:
                try:
                    from src.analyzers.bundler_orchestrator import analyze_bundler
                    br = await analyze_bundler(mint, deployer)
                except Exception as e:
                    logger.warning(f"Bundler scan error: {e}")

            L = [f"🔬 <b>Forensic Scan Complete</b>\n{SEP}", f"🔑 <code>{mint}</code>"]

            if deployer:
                L.append(f"👤 <code>{_ta(deployer)}</code>")
            L.append("")

            if pr:
                ts = getattr(pr, "total_score", 0.0)
                L.append(f"{_remoji(ts)} <b>Risk: {ts:.0f}/100 — {_rlabel(ts)}</b>\n")
                L.append("<b>Heuristics:</b>")
                for lbl, attr in [("Deployer","score_deployer"),("Holders","score_holders"),
                                   ("LP Lock","score_lp"),("Bundle","score_bundled"),
                                   ("Contract","score_contract"),("Social","score_social")]:
                    L.append(f"  {lbl:<10} {_bar(getattr(pr, attr, 0.0), 8)}")
                L.append("")

            if br and br.combined_score > 0:
                L.append(f"🎯 <b>Bundler: {br.combined_score:.0f}/100</b> ({br.detectors_triggered}/6 detectors)\n")

                dets = [
                    ("📤 Fan-out", br.funding), ("⚡ Jito bundle", br.bundle),
                    ("📐 Curve precision", br.reserves), ("🔄 Wash trading", br.wash),
                    ("🚪 Coord. exit", br.exit), ("🧹 SOL sweep", br.sweep),
                ]
                for lbl, d in dets:
                    if d and d.score > 0:
                        L.append(f"  {lbl}: {_bar(d.score, 6)}")
                        for fl in d.flags[:2]:
                            L.append(f"    → {fl}")
                L.append("")

                if br.all_flags:
                    L.append("🚩 <b>Key Flags:</b>")
                    for fl in br.all_flags[:8]:
                        L.append(f"  • {fl}")
            else:
                L.append("🎯 Bundler: 0/100 — no patterns detected")

            L.append(
                f"\n🔗 <a href='https://dexscreener.com/solana/{mint}'>Chart</a>"
                f" · <a href='https://solscan.io/token/{mint}'>Solscan</a>"
            )

            self._scans_total += 1
            msg = "\n".join(L)
            await self._send_long(update, msg)

        except Exception as e:
            logger.error(f"/scan failed: {e}")
            await update.message.reply_text(f"❌ Scan failed: {e}")

    # ── /deployer <addr> ──────────────────────────────────────────────

    @_owner_only
    async def _cmd_deployer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: /deployer &lt;wallet_address&gt;\n\nFull deployer history, funding patterns, tokens.",
                parse_mode=ParseMode.HTML)
            return

        addr = context.args[0].strip()
        if not _validate_solana_address(addr):
            await update.message.reply_text("❌ Invalid address format (expected base58, 32–44 chars).")
            return
        await update.message.reply_text(f"🔍 Analyzing <code>{_ta(addr)}</code>…", parse_mode=ParseMode.HTML)

        try:
            from src.models import TokenLaunch, Deployer

            L = [f"👤 <b>Deployer Deep-Dive</b>\n{SEP}", f"🔑 <code>{addr}</code>\n"]

            async with self._sf() as session:
                dep = (await session.execute(select(Deployer).where(Deployer.address == addr))).scalar_one_or_none()

                if dep:
                    rr = (dep.rug_count / dep.total_launches * 100) if dep.total_launches else 0
                    L += [
                        "<b>Profile:</b>",
                        f"  Launches:     {dep.total_launches}",
                        f"  Confirmed rugs: {dep.rug_count} ({rr:.0f}%)",
                        f"  Watchlisted:  {'✅' if dep.watchlisted else '❌'}",
                        f"  First seen:   {dep.first_seen.strftime('%Y-%m-%d %H:%M') if dep.first_seen else '?'}",
                        f"  Last seen:    {dep.last_seen.strftime('%Y-%m-%d %H:%M') if dep.last_seen else '?'}",
                    ]
                    if dep.notes:
                        L.append(f"  Notes:        {dep.notes[:200]}")
                    L.append("")
                else:
                    L.append("⚠️ Not in database — likely new deployer.\n")

                tks = (await session.execute(
                    select(TokenLaunch).where(TokenLaunch.deployer == addr)
                    .order_by(desc(TokenLaunch.launched_at)).limit(10)
                )).scalars().all()

                if tks:
                    L.append(f"<b>Recent Tokens ({len(tks)}):</b>")
                    for t in tks:
                        e = _remoji(t.risk_score_total)
                        rug = " 🚩RUG" if t.is_rug else ""
                        nm = f"{t.name} (${t.symbol})" if t.name else t.mint[:16]
                        dt = t.launched_at.strftime("%m/%d %H:%M") if t.launched_at else "?"
                        L.append(f"  {e} {nm} — {t.risk_score_total:.0f}/100{rug}")
                        L.append(f"     <code>{t.mint[:28]}…</code>  {dt}")

                    scores = [t.risk_score_total for t in tks]
                    rugs = sum(1 for t in tks if t.is_rug)
                    L += [
                        "",
                        f"<b>Aggregate:</b>",
                        f"  Avg score: {sum(scores)/len(scores):.1f}  |  Max: {max(scores):.1f}  |  Rugs: {rugs}/{len(tks)}",
                    ]

            # Funding analysis
            try:
                from src.analyzers.funding_fanout import analyze_funding_fanout
                fan = await analyze_funding_fanout(addr, lookback=50)
                if fan.fan_out_count > 0:
                    L += [
                        f"\n📤 <b>Funding Pattern (score: {fan.score:.0f}/100):</b>",
                        f"  Fan-out: {fan.fan_out_count} wallets × {fan.fan_out_amount_sol} SOL",
                        f"  Batch size: {fan.fan_out_batch_size}",
                    ]
                    for fl in fan.flags[:4]:
                        L.append(f"  • {fl}")
            except Exception as e:
                logger.debug(f"Fan-out analysis in /deployer failed: {e}")

            L.append(f"\n🔗 <a href='https://solscan.io/account/{addr}'>Solscan</a>")
            await self._send_long(update, "\n".join(L))

        except Exception as e:
            logger.error(f"/deployer failed: {e}")
            await update.message.reply_text(f"❌ Failed: {e}")

    # ── /bundler <mint> ───────────────────────────────────────────────

    @_owner_only
    async def _cmd_bundler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(
                "Usage: /bundler &lt;mint_address&gt;\n\n"
                "Runs all 6 bundler detectors independently:\n"
                "  1. Funding fan-out (funding.ts)\n"
                "  2. Same-slot Jito bundle (jito.ts)\n"
                "  3. Bonding curve precision (pumpfun.ts)\n"
                "  4. Wash trade fingerprint (volumeBot.ts)\n"
                "  5. Coordinated exit (autoSell.ts)\n"
                "  6. Recovery sweep (recover.ts)\n\n"
                "Takes 30-60 seconds.", parse_mode=ParseMode.HTML)
            return

        mint = context.args[0].strip()
        if not _validate_solana_address(mint):
            await update.message.reply_text("❌ Invalid address format (expected base58, 32–44 chars).")
            return
        await update.message.reply_text(
            f"🎯 Running 6 bundler detectors on <code>{mint[:20]}…</code>\nThis takes 30-60s…",
            parse_mode=ParseMode.HTML)

        try:
            from src.analyzers.bundler_orchestrator import analyze_bundler
            from src.analyzers.rpc import rpc

            deployer = ""
            sigs = await rpc.get_signatures_for_address(mint, limit=5)
            if sigs:
                tx = await rpc.get_transaction(sigs[-1].get("signature", ""))
                if tx:
                    ks = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                    if ks:
                        deployer = ks[0] if isinstance(ks[0], str) else ks[0].get("pubkey", "")

            if not deployer:
                await update.message.reply_text("❌ Could not identify deployer.")
                return

            r = await analyze_bundler(mint, deployer)

            L = [
                f"🎯 <b>Bundler Analysis Report</b>\n{SEP}",
                f"🔑 Mint: <code>{mint}</code>",
                f"👤 Deployer: <code>{_ta(deployer)}</code>\n",
                f"<b>Combined Score: {r.combined_score:.0f}/100</b>",
                f"Detectors triggered: {r.detectors_triggered}/6\n",
            ]

            dets = [
                ("📤 Funding Fan-Out", r.funding),
                ("⚡ Same-Slot Bundle", r.bundle),
                ("📐 Reserve Precision", r.reserves),
                ("🔄 Wash Trading", r.wash),
                ("🚪 Coordinated Exit", r.exit),
                ("🧹 Recovery Sweep", r.sweep),
            ]

            for lbl, det in dets:
                if det is None:
                    L.append(f"<b>{lbl}</b>: ⏭ skipped")
                    continue

                L.append(f"<b>{lbl}</b>")
                L.append(f"  Score: {_bar(det.score, 8)}")

                # Show all non-score/non-flags fields
                d = det.to_dict()
                for k, v in d.items():
                    if k in ("score", "flags"):
                        continue
                    if isinstance(v, float):
                        v = f"{v:.4f}" if v < 1 else f"{v:.1f}"
                    elif isinstance(v, list):
                        v = f"{len(v)} items" if len(v) > 3 else str(v)
                    elif isinstance(v, bool):
                        v = "✅ yes" if v else "❌ no"
                    L.append(f"  {k}: {v}")

                for fl in (det.flags or [])[:3]:
                    L.append(f"  🚩 {fl}")
                L.append("")

            # Verdict
            if r.combined_score >= 70:
                L.append("🔴 <b>VERDICT: High-confidence bundled launch</b>")
            elif r.combined_score >= 40:
                L.append("🟠 <b>VERDICT: Probable bundler involvement</b>")
            elif r.combined_score >= 20:
                L.append("🟡 <b>VERDICT: Some automated patterns</b>")
            else:
                L.append("🟢 <b>VERDICT: No significant bundler patterns</b>")

            await self._send_long(update, "\n".join(L))

        except Exception as e:
            logger.error(f"/bundler failed: {e}")
            await update.message.reply_text(f"❌ Bundler analysis failed: {e}")

    # ── /lookup <mint> ────────────────────────────────────────────────

    @_owner_only
    async def _cmd_lookup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text("Usage: /lookup &lt;mint_address&gt;", parse_mode=ParseMode.HTML)
            return

        mint = context.args[0].strip()
        if not _validate_solana_address(mint):
            await update.message.reply_text("❌ Invalid address format (expected base58, 32–44 chars).")
            return
        try:
            from src.models import TokenLaunch
            async with self._sf() as session:
                row = (await session.execute(select(TokenLaunch).where(TokenLaunch.mint == mint))).scalar_one_or_none()

            if not row:
                await update.message.reply_text(f"❓ Not in DB. Use /scan {mint} for live analysis.")
                return

            nm = f"{row.name} (${row.symbol})" if row.name else "unnamed"
            rug = "🚩 CONFIRMED RUG" if row.is_rug else ("✅ Survived" if row.is_rug is False else "⏳ Pending")

            L = [
                f"{_remoji(row.risk_score_total)} <b>{nm}</b> — {_rlabel(row.risk_score_total)}\n{SEP}",
                f"Mint: <code>{row.mint}</code>",
                f"Deployer: <code>{_ta(row.deployer)}</code>",
                f"Source: {row.source}",
                f"Launched: {row.launched_at.strftime('%Y-%m-%d %H:%M UTC') if row.launched_at else '?'}",
                f"Outcome: {rug}\n",
                f"<b>Risk Score: {row.risk_score_total:.0f}/100</b>",
                f"  Deployer  {_bar(row.score_deployer, 8)}",
                f"  Holders   {_bar(row.score_holders, 8)}",
                f"  LP        {_bar(row.score_lp, 8)}",
                f"  Bundle    {_bar(row.score_bundled, 8)}",
                f"  Contract  {_bar(row.score_contract, 8)}",
                f"  Social    {_bar(row.score_social, 8)}",
            ]

            if row.peak_mcap:
                L.append(f"\nPeak MCap: ${row.peak_mcap:,.0f}")
            if row.current_mcap:
                L.append(f"Current MCap: ${row.current_mcap:,.0f}")

            L += [
                f"\n🔗 <a href='https://dexscreener.com/solana/{mint}'>Chart</a>"
                f" · <a href='https://solscan.io/token/{mint}'>Solscan</a>",
                f"💡 /scan {mint}  ·  /deployer {row.deployer}",
            ]

            await update.message.reply_text("\n".join(L), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        except Exception as e:
            logger.error(f"/lookup failed: {e}")
            await update.message.reply_text(f"❌ Lookup failed: {e}")

    # ── /stats ────────────────────────────────────────────────────────

    @_owner_only
    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            from src.models import TokenLaunch
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

            async with self._sf() as session:
                total = (await session.execute(select(func.count(TokenLaunch.id)).where(TokenLaunch.launched_at >= cutoff))).scalar() or 0
                rugs = (await session.execute(select(func.count(TokenLaunch.id)).where(and_(TokenLaunch.launched_at >= cutoff, TokenLaunch.is_rug.is_(True))))).scalar() or 0
                avg = (await session.execute(select(func.avg(TokenLaunch.risk_score_total)).where(TokenLaunch.launched_at >= cutoff))).scalar() or 0
                high = (await session.execute(select(func.count(TokenLaunch.id)).where(and_(TokenLaunch.launched_at >= cutoff, TokenLaunch.risk_score_total >= 70)))).scalar() or 0
                alerted = (await session.execute(select(func.count(TokenLaunch.id)).where(and_(TokenLaunch.launched_at >= cutoff, TokenLaunch.alerted.is_(True))))).scalar() or 0

                dist = {}
                for lo, hi, lbl in [(0,25,"🟢 0-25"),(25,50,"🟡 25-50"),(50,75,"🟠 50-75"),(75,101,"🔴 75-100")]:
                    c = (await session.execute(select(func.count(TokenLaunch.id)).where(and_(
                        TokenLaunch.launched_at >= cutoff, TokenLaunch.risk_score_total >= lo, TokenLaunch.risk_score_total < hi
                    )))).scalar() or 0
                    dist[lbl] = c

            rr = (rugs / total * 100) if total else 0
            dl = []
            for lbl, c in dist.items():
                p = (c / total * 100) if total else 0
                bw = round(p / 5)
                dl.append(f"  {lbl:<10} {'█' * bw}{'░' * (20-bw)} {c} ({p:.0f}%)")

            await update.message.reply_text(
                f"📊 <b>24h Statistics</b>\n{SEP}\n\n"
                f"  Scanned:     {total}\n"
                f"  Rugs:        {rugs} ({rr:.1f}%)\n"
                f"  High risk:   {high}\n"
                f"  Alerts sent: {alerted}\n"
                f"  Avg score:   {avg:.1f}\n\n"
                f"<b>Distribution:</b>\n" + "\n".join(dl),
                parse_mode=ParseMode.HTML)

        except Exception as e:
            await update.message.reply_text(f"❌ Stats failed: {e}")

    # ── /top ──────────────────────────────────────────────────────────

    @_owner_only
    async def _cmd_top(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            from src.models import TokenLaunch
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

            async with self._sf() as session:
                rows = (await session.execute(
                    select(TokenLaunch).where(TokenLaunch.launched_at >= cutoff)
                    .order_by(desc(TokenLaunch.risk_score_total)).limit(10)
                )).scalars().all()

            if not rows:
                await update.message.reply_text("📊 No tokens in last 24h.")
                return

            L = [f"🏆 <b>Top 10 Highest Risk — 24h</b>\n{SEP}\n"]
            for i, t in enumerate(rows, 1):
                nm = f"{t.name} (${t.symbol})" if t.name else t.mint[:16]
                rug = " 🚩" if t.is_rug else ""
                L.append(
                    f"<b>{i}.</b> {_remoji(t.risk_score_total)} {nm} — <b>{t.risk_score_total:.0f}</b>{rug}\n"
                    f"   <code>{t.mint[:32]}…</code>"
                )

            L.append(f"\n💡 /lookup &lt;mint&gt; for details")
            await update.message.reply_text("\n".join(L), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        except Exception as e:
            await update.message.reply_text(f"❌ Failed: {e}")

    # ── /watchlist ────────────────────────────────────────────────────

    @_owner_only
    async def _cmd_watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from src.models import Deployer
        args = context.args or []

        if len(args) >= 2 and args[0].lower() == "add":
            addr = args[1].strip()
            if not _validate_solana_address(addr):
                await update.message.reply_text("❌ Invalid address format (expected base58, 32–44 chars).")
                return
            try:
                async with self._sf() as session:
                    dep = (await session.execute(select(Deployer).where(Deployer.address == addr))).scalar_one_or_none()
                    if dep:
                        dep.watchlisted = True
                    else:
                        session.add(Deployer(address=addr, watchlisted=True, notes="Added via Telegram"))
                    await session.commit()
                await update.message.reply_text(f"✅ <code>{_ta(addr)}</code> watchlisted.", parse_mode=ParseMode.HTML)
            except Exception as e:
                await update.message.reply_text(f"❌ {e}")
            return

        if len(args) >= 2 and args[0].lower() == "remove":
            addr = args[1].strip()
            if not _validate_solana_address(addr):
                await update.message.reply_text("❌ Invalid address format (expected base58, 32–44 chars).")
                return
            try:
                async with self._sf() as session:
                    dep = (await session.execute(select(Deployer).where(Deployer.address == addr))).scalar_one_or_none()
                    if dep:
                        dep.watchlisted = False
                        await session.commit()
                        await update.message.reply_text(f"✅ Removed <code>{_ta(addr)}</code>.", parse_mode=ParseMode.HTML)
                    else:
                        await update.message.reply_text("❓ Not in database.")
            except Exception as e:
                await update.message.reply_text(f"❌ {e}")
            return

        try:
            async with self._sf() as session:
                rows = (await session.execute(
                    select(Deployer).where(Deployer.watchlisted.is_(True))
                    .order_by(Deployer.rug_count.desc()).limit(25)
                )).scalars().all()

            if not rows:
                await update.message.reply_text("📋 Watchlist empty.\nAdd with /watchlist add &lt;addr&gt;", parse_mode=ParseMode.HTML)
                return

            L = [f"📋 <b>Watchlist ({len(rows)})</b>\n{SEP}\n"]
            for r in rows:
                rr = (r.rug_count / r.total_launches * 100) if r.total_launches else 0
                L.append(f"  <code>{_ta(r.address)}</code> — {r.rug_count} rugs / {r.total_launches} ({rr:.0f}%)")

            L.append(f"\n💡 /deployer &lt;addr&gt; for deep-dive")
            await update.message.reply_text("\n".join(L), parse_mode=ParseMode.HTML)

        except Exception as e:
            await update.message.reply_text(f"❌ {e}")

    # ── /threshold ────────────────────────────────────────────────────

    @_owner_only
    async def _cmd_threshold(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(f"Current: <b>{settings.min_risk_score_alert}/100</b>\nUsage: /threshold &lt;0-100&gt;", parse_mode=ParseMode.HTML)
            return
        try:
            v = int(context.args[0])
            assert 0 <= v <= 100
            settings.min_risk_score_alert = v
            # Persist to DB so the threshold survives restarts
            try:
                from src.models import AlertConfig
                async with self._sf() as session:
                    cfg = (await session.execute(
                        select(AlertConfig).where(AlertConfig.id == 1)
                    )).scalar_one_or_none()
                    if cfg:
                        cfg.min_risk_threshold = v
                    else:
                        session.add(AlertConfig(id=1, min_risk_threshold=v))
                    await session.commit()
            except Exception as e:
                logger.warning(f"Failed to persist threshold to DB: {e}")
            await update.message.reply_text(f"✅ Threshold → <b>{v}/100</b> (persisted)", parse_mode=ParseMode.HTML)
        except (ValueError, AssertionError):
            await update.message.reply_text("❌ Must be 0-100.")

    # ── /mute & /unmute ───────────────────────────────────────────────

    @_owner_only
    async def _cmd_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._alerts_enabled = False
        await update.message.reply_text("🔇 Alerts muted.")
        logger.info(f"Muted by uid={update.effective_user.id}")

    @_owner_only
    async def _cmd_unmute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._alerts_enabled = True
        await update.message.reply_text("✅ Alerts resumed.")
        logger.info(f"Unmuted by uid={update.effective_user.id}")

    # ── /export ───────────────────────────────────────────────────────

    @_owner_only
    async def _cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("⏳ Generating CSV (up to 50k rows)…")
        try:
            from src.models import TokenLaunch
            async with self._sf() as session:
                rows = (await session.execute(
                    select(TokenLaunch).order_by(desc(TokenLaunch.launched_at)).limit(50_000)
                )).scalars().all()

            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["mint","name","symbol","deployer","source","risk_score_total",
                         "score_deployer","score_holders","score_lp","score_bundled",
                         "score_contract","score_social","is_rug","peak_mcap","current_mcap","launched_at"])
            for r in rows:
                w.writerow([r.mint,r.name,r.symbol,r.deployer,r.source,r.risk_score_total,
                            r.score_deployer,r.score_holders,r.score_lp,r.score_bundled,
                            r.score_contract,r.score_social,r.is_rug,r.peak_mcap,r.current_mcap,r.launched_at])

            fn = f"forensics_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
            await update.message.reply_document(
                document=InputFile(io.BytesIO(buf.getvalue().encode()), filename=fn),
                caption=f"✅ {len(rows)} records.")
        except Exception as e:
            await update.message.reply_text(f"❌ Export failed: {e}")

    # ── /train ────────────────────────────────────────────────────────

    @_owner_only
    async def _cmd_train(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("🧠 Retraining ML model…")
        try:
            p = getattr(self._pipeline, "predictor", None)
            if not p:
                await update.message.reply_text("⚠️ Predictor not attached.")
                return
            rt = getattr(p, "retrain", None)
            if callable(rt):
                await asyncio.get_event_loop().run_in_executor(None, rt)
                await update.message.reply_text("✅ Retrain complete.")
            else:
                await update.message.reply_text("⚠️ No retrain() method.")
        except Exception as e:
            await update.message.reply_text(f"❌ Retrain failed: {e}")

    # ── /backtest ─────────────────────────────────────────────────────

    @_owner_only
    async def _cmd_backtest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("⏳ Running backtest…")
        try:
            from src.backtest import BacktestEngine
            engine = BacktestEngine(self._sf)
            res = await engine.run()
            d = res.to_dict()
            await update.message.reply_text(
                f"📈 <b>Backtest Results</b>\n{SEP}\n\n"
                f"  Samples:   {d.get('total_samples','?')}\n"
                f"  Rugs:      {d.get('rug_count','?')}\n"
                f"  Precision: {d.get('precision','?')}\n"
                f"  Recall:    {d.get('recall','?')}\n"
                f"  F1:        {d.get('f1','?')}\n"
                f"  Accuracy:  {d.get('accuracy','?')}",
                parse_mode=ParseMode.HTML)
        except ImportError:
            await update.message.reply_text("⚠️ BacktestEngine not available.")
        except Exception as e:
            await update.message.reply_text(f"❌ Backtest failed: {e}")

    # ── Utility: send long messages (split at 4096 Telegram limit) ────

    async def _send_long(self, update: Update, text: str) -> None:
        limit = 4000
        while text:
            if len(text) <= limit:
                try:
                    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                except TelegramError:
                    # Fallback: send without HTML parsing if tags are broken
                    await update.message.reply_text(text, disable_web_page_preview=True)
                break
            cut = text[:limit].rfind("\n")
            if cut < 100:
                cut = limit
            chunk = text[:cut]
            # Ensure we don't split inside an HTML tag — scan back from cut
            # to close any open '<' that hasn't been closed with '>'
            open_bracket = chunk.rfind("<")
            close_bracket = chunk.rfind(">")
            if open_bracket > close_bracket:
                # We're inside a tag — cut before it
                cut = open_bracket
                chunk = text[:cut]
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except TelegramError:
                await update.message.reply_text(chunk, disable_web_page_preview=True)
            text = text[cut:]
