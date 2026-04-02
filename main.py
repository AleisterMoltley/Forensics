"""Token Launch Forensics Bot - Main Entry Point."""
import asyncio
import signal
import sys
import time
from pathlib import Path
from loguru import logger
import uvicorn

from src.config import settings, validate_env, setup_logging
from src.models import init_db
from src.pipeline import ForensicPipeline
from src.scanners.pump_fun import PumpFunListener
from src.scanners.raydium import RaydiumListener
from src.scanners.migration import MigrationListener, MigrationAnalyzer
from src.analyzers.outcome_tracker import OutcomeTracker, TrainingDataExporter
from src.ml_model import AutoRetrainer, RugPredictor
from src.sniper_bridge import SniperBridge
from src.deployer_network import DeployerAlertNetwork
from src.analyzers.post_rug_tracker import PostRugTracker
from src.channel import ChannelPublisher
from src.queue import AnalysisQueue
from src.metrics import metrics, track_scan, track_alert_sent, set_ws_connected
from src.telegram_bot import TelegramAlerts
from src.dashboard import create_app
from src.mcap_tracker import McapMilestoneTracker
from src.rpc import rpc


# Configure logging with secret-redaction filter (defined in config.py).
# This replaces the raw logger.remove()/logger.add() calls so that the
# Helius API key, Telegram token, and wallet addresses are automatically
# stripped from every log record and file sink.
setup_logging(settings)


class ForensicsBot:
    def __init__(self):
        self.engine = None
        self.session_factory = None
        self.pipeline = None
        self.telegram = None
        self.pump_listener = None
        self.raydium_listener = None
        self.migration_listener = None
        self.migration_analyzer = None
        self.outcome_tracker = None
        self.auto_retrainer = None
        self.sniper = None
        self.deployer_network = None
        self.post_rug_tracker = None
        self.channel = None
        self.queue = None
        self.mcap_tracker = None
        self.dashboard_app = None
        self._shutdown = False
        self._start_time = None
        self._last_scan_time = None
        self._ws_status = {"pump_fun": False, "raydium": False, "migration": False}

    async def start(self):
        logger.info("=" * 50)
        logger.info("🔬 Token Launch Forensics Bot starting...")
        logger.info("=" * 50)

        # 0. Validate environment
        validate_env()

        # Ensure data directory (local dev only)
        if not settings.is_railway:
            Path("data").mkdir(exist_ok=True)

        self._start_time = time.time()

        # 1. Init database
        logger.info("Initializing database...")
        self.engine, self.session_factory = await init_db(settings.database_url)

        # 2. Init forensic pipeline
        self.pipeline = ForensicPipeline(self.session_factory)

        # ─── PRIORITY: Start dashboard FIRST so /health responds ───
        # Railway's healthcheck starts immediately after the container
        # launches.  We must have uvicorn listening before anything else.

        # 3. Init dashboard (creates FastAPI app + routes)
        self.dashboard_app = create_app(self.session_factory)

        # 4. Register health + metrics endpoints EARLY
        self._register_dashboard_endpoints()

        # 5. Start dashboard server as a background task
        dashboard_task = asyncio.create_task(self._run_dashboard(), name="dashboard")

        # Give uvicorn a moment to bind the port before continuing
        # so Railway's first healthcheck probe succeeds.
        await asyncio.sleep(1.0)
        logger.info(f"✅ Dashboard listening on :{settings.dashboard_port} — healthcheck should pass")

        # ─── Now init everything else (non-blocking for healthcheck) ───

        # 6. Init Telegram (polling can be slow — don't block startup)
        self.telegram = TelegramAlerts(self.session_factory, self.pipeline)
        try:
            await asyncio.wait_for(self.telegram.start(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("Telegram start timed out (15s) — will retry in background")
        except Exception as e:
            logger.warning(f"Telegram start failed: {e} — bot disabled")

        # 7. Init listeners
        self.pump_listener = PumpFunListener(on_launch=self._on_launch)
        self.raydium_listener = RaydiumListener(on_launch=self._on_launch)

        # 8. Init migration listener + analyzer
        self.migration_analyzer = MigrationAnalyzer(self.session_factory)
        self.migration_listener = MigrationListener(
            on_migration=self._on_migration,
            session_factory=self.session_factory,
        )

        # 9. Init outcome tracker
        self.outcome_tracker = OutcomeTracker(self.session_factory)

        # 10. Init ML auto-retrainer
        self.auto_retrainer = AutoRetrainer(self.session_factory)
        self.pipeline.predictor = self.auto_retrainer.predictor
        # Expose on dashboard app.state so /api/train can trigger retraining
        self.dashboard_app.state.auto_retrainer = self.auto_retrainer

        # 11. Init deployer alert network
        self.deployer_network = DeployerAlertNetwork(self.session_factory)
        await self.deployer_network.load()
        await self.deployer_network.auto_watchlist_from_rugs(min_rugs=2)

        # 12. Init sniper bridge
        self.sniper = SniperBridge(
            webhook_url=settings.sniper_webhook_url,
            signal_chat_id=settings.sniper_signal_chat_id,
        )

        # 13. Init channel publisher
        self.channel = ChannelPublisher(
            channel_id=settings.channel_chat_id,
            bot=self.telegram.bot if self.telegram else None,
            min_score_for_warning=settings.channel_min_warning_score,
            max_score_for_gem=settings.channel_max_gem_score,
        )

        # 14. Init post-rug tracker
        self.post_rug_tracker = PostRugTracker(
            self.session_factory, self.deployer_network,
        )

        # 15. Init queue (optional Redis)
        self.queue = AnalysisQueue(
            redis_url=settings.redis_url,
            num_workers=settings.queue_workers,
        )
        if settings.use_redis_queue:
            await self.queue.connect()
            await self.queue.start_workers(self._process_queued_launch)

        # 16. Init mcap milestone tracker
        self.mcap_tracker = McapMilestoneTracker(
            session_factory=self.session_factory,
            pipeline=self.pipeline,
            telegram=self.telegram,
        )

        # 17. Start all background tasks
        tasks = [
            dashboard_task,
            asyncio.create_task(self.pump_listener.start(), name="pump_fun"),
            asyncio.create_task(self.raydium_listener.start(), name="raydium"),
            asyncio.create_task(self.migration_listener.start(), name="migration"),
            asyncio.create_task(self.outcome_tracker.start(), name="outcome_tracker"),
            asyncio.create_task(self.auto_retrainer.start(), name="auto_retrainer"),
        ]

        if settings.post_rug_tracker_enabled:
            tasks.append(asyncio.create_task(self.post_rug_tracker.start(), name="post_rug_tracker"))

        if settings.mcap_milestone_list:
            tasks.append(asyncio.create_task(self.mcap_tracker.start(), name="mcap_tracker"))

        milestones = settings.mcap_milestone_list
        ms_str = ", ".join(f"${m:,.0f}" for m in milestones) if milestones else "OFF"

        logger.info("✅ All systems online")
        logger.info(f"   Dashboard: http://{settings.dashboard_host}:{settings.dashboard_port}")
        logger.info(f"   Telegram alerts: {'ON' if settings.telegram_bot_token else 'OFF'}")
        logger.info(f"   Alert threshold: {settings.min_risk_score_alert}/100")
        logger.info(f"   Min mcap alert: {'$' + f'{settings.min_mcap_alert:,.0f}' if settings.min_mcap_alert > 0 else 'OFF'}")
        logger.info(f"   Mcap milestones: {ms_str}")
        logger.info(f"   Outcome tracker: ON (1h/6h/24h checks)")
        logger.info(f"   Migration listener: ON")
        logger.info(f"   ML model: {'LOADED' if self.auto_retrainer.predictor.is_ready else 'WAITING FOR DATA'}")
        logger.info(f"   Sniper bridge: {'ON' if settings.sniper_webhook_url else 'OFF'}")
        logger.info(f"   Channel: {'ON' if settings.channel_chat_id else 'OFF'}")
        logger.info(f"   Deployer network: {len(self.deployer_network._cache)} cached, {len(self.deployer_network._watchlist)} watchlisted")
        logger.info(f"   Post-rug tracker: {'ON' if settings.post_rug_tracker_enabled else 'OFF'}")
        logger.info(f"   Queue: {'Redis' if self.queue._use_redis else 'asyncio'}")
        logger.info(f"   Metrics: http://{settings.dashboard_host}:{settings.dashboard_port}/metrics")

        # Wait for shutdown
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    def _register_dashboard_endpoints(self):
        """Register health, metrics, backtest endpoints on the dashboard app.

        Called early in startup so /health is available for Railway's
        healthcheck probe before the bot's async services are ready.
        """
        from fastapi import Request as _Request
        from fastapi.responses import PlainTextResponse
        from src.dashboard import limiter, RequireAdmin

        @self.dashboard_app.get("/health")
        @limiter.limit("60/minute")
        async def health(request: _Request):
            """Railway healthcheck endpoint — minimal public response."""
            return {"status": "ok"}

        @self.dashboard_app.get("/api/health", dependencies=[RequireAdmin])
        @limiter.limit("60/minute")
        async def health_detailed(request: _Request):
            """Detailed health info — protected to prevent operational recon."""
            uptime = time.time() - (self._start_time or time.time())
            return {
                "status": "ok",
                "uptime_seconds": int(uptime),
                "database": "connected",
                "ws_connections": self._ws_status,
                "last_scan_age_seconds": (
                    int(time.time() - self._last_scan_time)
                    if self._last_scan_time else None
                ),
                "ml_model_ready": (
                    self.auto_retrainer.predictor.is_ready
                    if self.auto_retrainer else False
                ),
            }

        @self.dashboard_app.get(
            "/metrics",
            response_class=PlainTextResponse,
            dependencies=[RequireAdmin],
        )
        @limiter.limit("30/minute")
        async def prometheus_metrics(request: _Request):
            """Prometheus metrics — protected to prevent operational recon."""
            return metrics.export_prometheus()

        @self.dashboard_app.get("/api/metrics", dependencies=[RequireAdmin])
        @limiter.limit("60/minute")
        async def api_metrics(request: _Request):
            data = metrics.export_json()
            if self.queue:
                data["queue"] = self.queue.get_metrics()
                data["queue"]["depth"] = await self.queue.get_depth()
            # Helius RPC usage monitoring
            data["rpc"] = rpc.stats
            return data

        @self.dashboard_app.get("/api/backtest", dependencies=[RequireAdmin])
        @limiter.limit("5/minute")
        async def api_backtest(request: _Request):
            from src.backtest import BacktestEngine
            engine = BacktestEngine(self.session_factory)
            result = await engine.run()
            return result.to_dict()

    async def _on_launch(self, launch: dict):
        """Callback when a new token launch is detected."""
        start = time.time()

        try:
            deployer = launch.get("deployer", "")
            alert = None  # must be defined before the queue check below

            # FAST PATH: Deployer alert network (<1ms)
            if self.deployer_network and deployer:
                alert = self.deployer_network.check_fast(deployer)
                if alert and alert["severity"] in ("critical", "warning"):
                    logger.warning(f"⚡ DEPLOYER ALERT: {deployer[:12]}... → {alert['alerts']}")
                    track_alert_sent("deployer_network")

                    if self.telegram and self.telegram.bot:
                        msg = self.deployer_network.format_alert(alert, launch)
                        try:
                            await self.telegram.bot.send_message(
                                chat_id=settings.telegram_chat_id,
                                text=msg, parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                        except Exception as e:
                            logger.error(f"Deployer alert send failed: {e}")

            # Queue or direct process
            if settings.use_redis_queue and self.queue:
                priority = alert["severity"] == "critical" if alert else False
                await self.queue.enqueue(launch, priority=priority)
            else:
                await self._process_queued_launch(launch)

        except Exception as e:
            logger.error(f"Launch handler failed: {e}")

    async def _process_queued_launch(self, launch: dict):
        """Process a launch (called directly or from queue worker)."""
        start = time.time()

        try:
            result = await self.pipeline.analyze(launch)
            if not result:
                return

            duration_ms = (time.time() - start) * 1000
            track_scan(result.source, duration_ms, result.total_score)
            self._last_scan_time = time.time()

            # Send Telegram alert
            await self.telegram.send_alert(result)

            # Broadcast to dashboard WebSocket
            if self.dashboard_app and hasattr(self.dashboard_app.state, "broadcast"):
                await self.dashboard_app.state.broadcast(result.to_dict())

            # Track pump.fun tokens for migration events
            if launch.get("source") == "pump_fun" and self.migration_listener:
                self.migration_listener.track_mint(result.mint)

            # Sniper bridge: check for buy signal
            if self.sniper:
                signal = await self.sniper.process(
                    result, bot=self.telegram.bot if self.telegram else None,
                )
                if signal:
                    track_alert_sent("sniper")

            # Channel publisher
            if self.channel:
                await self.channel.maybe_publish(result)

        except Exception as e:
            logger.error(f"Launch processing failed: {e}")

    async def _on_migration(self, event: dict):
        """Callback when a Pump.fun → Raydium migration is detected."""
        try:
            mint = event.get("mint", "")
            logger.info(f"🔄 Processing migration for {mint[:16]}...")

            # Analyze post-migration behavior
            post_analysis = await self.migration_analyzer.analyze_post_migration(event)

            # Re-scan with updated data if suspicious
            if post_analysis.get("deployer_sold_post_migration"):
                logger.warning(f"🚨 Deployer sold after migration: {mint[:16]}...")

                # Send special migration alert
                if self.telegram and self.telegram._alerts_enabled:
                    emoji = "🚨" if post_analysis["deployer_sold_post_migration"] else "🔄"
                    flags = post_analysis.get("flags", [])
                    msg = (
                        f"{emoji} <b>Migration Alert</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"Mint: <code>{mint}</code>\n"
                        f"Event: Pump.fun → Raydium migration\n\n"
                        f"🚩 <b>Post-Migration Flags:</b>\n"
                        + "\n".join(f"  • {f}" for f in flags)
                        + f"\n\n🔗 <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a>"
                    )
                    try:
                        await self.telegram.bot.send_message(
                            chat_id=settings.telegram_chat_id,
                            text=msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    except Exception as e:
                        logger.error(f"Migration alert send failed: {e}")

            # Broadcast to dashboard
            if self.dashboard_app and hasattr(self.dashboard_app.state, "broadcast"):
                await self.dashboard_app.state.broadcast({
                    "type": "migration",
                    "mint": mint,
                    "post_analysis": post_analysis,
                })

        except Exception as e:
            logger.error(f"Migration processing failed: {e}")

    async def _run_dashboard(self):
        """Run FastAPI dashboard."""
        config = uvicorn.Config(
            self.dashboard_app,
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        self._dashboard_server = server
        await server.serve()

    async def shutdown(self):
        logger.info("Shutting down...")
        self._shutdown = True

        # Graceful dashboard shutdown first (drains open connections)
        if hasattr(self, "_dashboard_server") and self._dashboard_server:
            self._dashboard_server.should_exit = True

        if self.pump_listener:
            await self.pump_listener.stop()
        if self.raydium_listener:
            await self.raydium_listener.stop()
        if self.migration_listener:
            await self.migration_listener.stop()
        if self.outcome_tracker:
            await self.outcome_tracker.stop()
        if self.auto_retrainer:
            await self.auto_retrainer.stop()
        if self.post_rug_tracker:
            await self.post_rug_tracker.stop()
        if self.mcap_tracker:
            await self.mcap_tracker.stop()
        if self.queue:
            await self.queue.stop_workers()
            await self.queue.close()
        if self.telegram:
            await self.telegram.stop()
        if rpc:
            await rpc.close()
        if self.engine:
            await self.engine.dispose()

        logger.info("Shutdown complete")


async def main():
    bot = ForensicsBot()

    loop = asyncio.get_running_loop()

    # Signal handlers — Railway sends SIGTERM on deploy
    # Try Unix signals, fall back gracefully on Windows/restricted containers
    def _signal_shutdown():
        asyncio.create_task(bot.shutdown())

    try:
        for sig_name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig_name, _signal_shutdown)
    except (NotImplementedError, OSError):
        # Windows or restricted container — shutdown on KeyboardInterrupt instead
        logger.warning("Signal handlers not supported, using KeyboardInterrupt fallback")

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.shutdown()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        await bot.shutdown()
        raise


if __name__ == "__main__":
    asyncio.run(main())
