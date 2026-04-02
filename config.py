"""Configuration with Railway-native support.

Railway sets DATABASE_URL (Postgres) and PORT automatically.
Supports both local dev (SQLite) and Railway (PostgreSQL).
"""
import sys
from pydantic_settings import BaseSettings
from pydantic import Field, model_validator
from loguru import logger


class Settings(BaseSettings):
    # Solana RPC
    helius_api_key: str = ""
    helius_rpc_url: str = "https://mainnet.helius-rpc.com/?api-key="
    helius_ws_url: str = "wss://mainnet.helius-rpc.com/?api-key="

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Alert Settings
    min_risk_score_alert: int = 50
    alert_cooldown_seconds: int = 30

    # Scan Settings
    scan_concurrency: int = 5
    holder_check_top_n: int = 10
    max_deployer_history_lookback: int = 50

    # Dashboard — Railway sets PORT env var
    dashboard_port: int = 8080
    dashboard_host: str = "0.0.0.0"
    port: int = 0  # Railway PORT override

    # Database — Railway sets DATABASE_URL for Postgres
    database_url: str = "sqlite+aiosqlite:///data/forensics.db"
    database_private_url: str = ""  # Railway internal network URL

    # Social
    twitter_bearer_token: str = ""

    # Sniper Bridge
    sniper_webhook_url: str = ""
    sniper_max_risk_score: int = 30
    sniper_signal_chat_id: str = ""

    # Channel Mode
    channel_chat_id: str = ""
    channel_min_warning_score: int = 70
    channel_max_gem_score: int = 25

    # Redis — Railway Redis addon sets REDIS_URL
    redis_url: str = "redis://localhost:6379"
    redis_private_url: str = ""
    use_redis_queue: bool = False
    queue_workers: int = 3

    # Post-Rug Tracker
    post_rug_tracker_enabled: bool = True
    post_rug_check_interval: int = 300

    # Railway environment
    railway_environment: str = ""
    railway_service_name: str = ""

    @model_validator(mode="after")
    def resolve_railway(self):
        """Auto-detect Railway environment and fix URLs."""
        # Railway PORT override
        if self.port > 0:
            self.dashboard_port = self.port

        # Railway PostgreSQL: convert URL for async SQLAlchemy
        if self.database_private_url:
            self.database_url = self.database_private_url
        if self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace(
                "postgres://", "postgresql+asyncpg://", 1
            )
        elif self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )

        # Railway Redis
        if self.redis_private_url:
            self.redis_url = self.redis_private_url

        return self

    @property
    def rpc_url(self) -> str:
        return f"{self.helius_rpc_url}{self.helius_api_key}"

    @property
    def ws_url(self) -> str:
        return f"{self.helius_ws_url}{self.helius_api_key}"

    @property
    def is_railway(self) -> bool:
        return bool(self.railway_environment)

    @property
    def is_postgres(self) -> bool:
        return "postgresql" in self.database_url or "asyncpg" in self.database_url

    class Config:
        env_file = "config/.env"
        env_file_encoding = "utf-8"


settings = Settings()


def validate_env():
    """Validate critical environment variables on startup.
    Returns list of warnings (non-fatal) and exits on fatal errors.
    """
    fatal = []
    warnings = []

    if not settings.helius_api_key:
        fatal.append("HELIUS_API_KEY is required — get one at https://helius.dev")

    if not settings.telegram_bot_token:
        warnings.append("TELEGRAM_BOT_TOKEN not set — Telegram alerts disabled")

    if not settings.telegram_chat_id:
        warnings.append("TELEGRAM_CHAT_ID not set — Telegram alerts disabled")

    if settings.database_url == "sqlite+aiosqlite:///data/forensics.db" and settings.is_railway:
        warnings.append(
            "Using SQLite on Railway — data will be lost on redeploy! "
            "Add a PostgreSQL addon: railway add postgresql"
        )

    # Print warnings
    for w in warnings:
        logger.warning(f"⚠️  {w}")

    # Fatal errors
    if fatal:
        for f in fatal:
            logger.error(f"❌ {f}")
        logger.error("Fix the above and restart.")
        sys.exit(1)

    # Info
    logger.info(f"   Database: {'PostgreSQL' if settings.is_postgres else 'SQLite'}")
    logger.info(f"   Railway: {'YES' if settings.is_railway else 'NO (local dev)'}")
    logger.info(f"   Port: {settings.dashboard_port}")
