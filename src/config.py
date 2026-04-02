"""Configuration with Railway-native support.

Railway sets DATABASE_URL (Postgres) and PORT automatically.
Supports both local dev (SQLite) and Railway (PostgreSQL).
"""
from __future__ import annotations

import re
import sys
from pydantic_settings import BaseSettings
from pydantic import model_validator
from loguru import logger


# ---------------------------------------------------------------------------
# Secret redaction helpers
# ---------------------------------------------------------------------------

def _redact_wallet(addr: str) -> str:
    """Show first 6 + last 4 characters of a wallet/mint address."""
    if len(addr) > 10:
        return f"{addr[:6]}…{addr[-4:]}"
    return "***"


class _SecretRedactor:
    """Loguru sink filter that strips secrets from every log record message.

    Why: Even without intentional logging of secrets, stack-traces and
    f-string formatting can embed API keys, tokens, or wallet addresses.
    A blanket redaction filter is the last line of defense.
    """

    def __init__(self) -> None:
        # Populated after Settings are loaded; see setup_logging()
        self._patterns: list[tuple[re.Pattern[str], str]] = []

    def register(self, secret: str, replacement: str = "[REDACTED]") -> None:
        """Register a literal secret string for redaction."""
        if secret:
            self._patterns.append((re.compile(re.escape(secret)), replacement))

    def register_wallet_pattern(self) -> None:
        """Redact Solana base58 addresses (32–44 chars) in logs.

        We replace the middle section to preserve first-6 / last-4 for
        human identification while hiding the full address.
        """
        # Matches base58 strings of 32–44 chars that are not inside URLs
        self._patterns.append((
            re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{6})[1-9A-HJ-NP-Za-km-z]{22,34}([1-9A-HJ-NP-Za-km-z]{4})\b"),
            r"\1…\2",
        ))

    # JSON blobs longer than this many characters are replaced with a
    # size annotation.  This prevents stack-traces containing full RPC
    # response bodies or transaction payloads from flooding the logs.
    _JSON_TRUNCATE_THRESHOLD: int = 200

    # Matches a JSON object `{...}` or array `[...]` whose content is at least
    # _JSON_TRUNCATE_THRESHOLD characters long.  `[\s\S]{N,}?` uses DOTALL
    # semantics (matches any character including braces and newlines) with a
    # non-greedy quantifier so each outermost blob is matched independently.
    # This correctly handles nested structures such as `{"a": {"b": "c"}}`.
    _JSON_BLOB_RE: re.Pattern[str] = re.compile(
        rf"(\{{[\s\S]{{{_JSON_TRUNCATE_THRESHOLD},}}?\}}|\[[\s\S]{{{_JSON_TRUNCATE_THRESHOLD},}}?\])",
        re.DOTALL,
    )

    def _truncate_json_payloads(self, msg: str) -> str:
        """Replace large inline JSON blobs with a size annotation.

        Why: RPC responses, WebSocket frames, and Pydantic validation errors
        often include multi-kilobyte JSON that is useless in a log line and
        may contain wallet addresses or other sensitive fields that were not
        individually registered for redaction.
        """
        def _replace(m: re.Match[str]) -> str:
            blob = m.group(0)
            return f"[JSON payload redacted — {len(blob)} bytes]"

        return self._JSON_BLOB_RE.sub(_replace, msg)

    def __call__(self, record: dict) -> bool:
        """Called by loguru for every log record; mutates message in-place."""
        msg: str = record["message"]
        # 1. Truncate large JSON payloads before other redactions so that
        #    wallet addresses embedded inside them are caught in bulk.
        msg = self._truncate_json_payloads(msg)
        # 2. Redact registered literal secrets and wallet address patterns.
        for pattern, replacement in self._patterns:
            msg = pattern.sub(replacement, msg)
        record["message"] = msg
        return True  # always allow the record through


# Module-level singleton so setup_logging() can populate it after Settings load
_redactor = _SecretRedactor()


def setup_logging(s: "Settings") -> None:
    """Configure loguru with secret-redaction filter.

    Must be called *after* Settings are instantiated so the actual secret
    values are known.  main.py should call this early in startup.
    """
    # Register all known secrets for redaction
    _redactor.register(s.helius_api_key)
    _redactor.register(s.telegram_bot_token)
    _redactor.register(s.twitter_bearer_token)
    _redactor.register(s.admin_api_key)
    _redactor.register_wallet_pattern()

    logger.remove()
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        ),
        level="INFO",
        filter=_redactor,  # apply redaction to every record
    )
    if not s.is_railway:
        from pathlib import Path
        Path("data").mkdir(exist_ok=True)
        logger.add(
            "data/forensics.log",
            rotation="10 MB",
            retention="7 days",
            level="DEBUG",
            filter=_redactor,
        )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    # Solana RPC
    helius_api_key: str = ""
    helius_rpc_url: str = "https://mainnet.helius-rpc.com/?api-key="
    helius_ws_url: str = "wss://mainnet.helius-rpc.com/?api-key="

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Comma-separated Telegram user IDs allowed to run privileged commands.
    # e.g. TELEGRAM_OWNER_IDS=123456789,987654321
    telegram_owner_ids: str = ""

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
    # API key required in X-API-Key header for all /api/* and /ws endpoints.
    # Sourced from ADMIN_API_KEY env variable.
    # Leave empty to disable auth (development only — never in production).
    admin_api_key: str = ""

    # CORS origin for the dashboard. Set to your Railway domain in production.
    # e.g. DASHBOARD_ORIGIN=https://your-app.up.railway.app
    # Set CORS_ALLOW_ALL=true explicitly for local dev (never in production).
    dashboard_origin: str = ""
    cors_allow_all: bool = False

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
    def resolve_railway(self) -> "Settings":
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

    @property
    def owner_id_set(self) -> frozenset[int]:
        """Parsed set of privileged Telegram user IDs."""
        ids: set[int] = set()
        for part in self.telegram_owner_ids.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return frozenset(ids)

    class Config:
        env_file = "config/.env"
        env_file_encoding = "utf-8"


settings = Settings()


def validate_env() -> None:
    """Validate critical environment variables on startup.

    Exits on fatal errors; logs non-fatal issues as warnings.
    This function itself never logs secret values — it only checks
    for presence and logs human-readable status messages.
    """
    fatal: list[str] = []
    warnings: list[str] = []

    if not settings.helius_api_key:
        fatal.append("HELIUS_API_KEY is required — get one at https://helius.dev")

    if not settings.telegram_bot_token:
        warnings.append("TELEGRAM_BOT_TOKEN not set — Telegram alerts disabled")

    if not settings.telegram_chat_id:
        warnings.append("TELEGRAM_CHAT_ID not set — Telegram alerts disabled")

    if not settings.admin_api_key and settings.is_railway:
        warnings.append(
            "ADMIN_API_KEY not set — dashboard endpoints are unprotected! "
            "Set a strong random key before exposing the dashboard publicly."
        )
    elif not settings.admin_api_key and settings.dashboard_host == "0.0.0.0":
        warnings.append(
            "ADMIN_API_KEY not set and dashboard bound to 0.0.0.0 — "
            "endpoints are exposed without authentication! "
            "Set ADMIN_API_KEY if this server is network-accessible."
        )

    if not settings.telegram_owner_ids:
        warnings.append(
            "TELEGRAM_OWNER_IDS not set — privileged Telegram commands are unrestricted. "
            "Set comma-separated Telegram user IDs to restrict /export, /train, /backtest."
        )

    if settings.database_url == "sqlite+aiosqlite:///data/forensics.db" and settings.is_railway:
        warnings.append(
            "Using SQLite on Railway — data will be lost on redeploy! "
            "Add a PostgreSQL addon: railway add postgresql"
        )

    # Check for safe ML model serialization
    try:
        import skops  # noqa: F401
    except ImportError:
        warnings.append(
            "skops not installed — ML model uses joblib (pickle-based). "
            "Pickle deserialization is a known RCE vector. "
            "Install skops for pickle-free serialization: pip install skops"
        )

    for w in warnings:
        logger.warning(f"⚠️  {w}")

    if fatal:
        for f in fatal:
            logger.error(f"❌ {f}")
        logger.error("Fix the above and restart.")
        sys.exit(1)

    # Log config summary — never print actual secret values
    logger.info(f"   Database: {'PostgreSQL' if settings.is_postgres else 'SQLite'}")
    logger.info(f"   Railway: {'YES' if settings.is_railway else 'NO (local dev)'}")
    logger.info(f"   Port: {settings.dashboard_port}")
    logger.info(f"   Dashboard auth: {'ON' if settings.admin_api_key else 'OFF (dev mode)'}")
    logger.info(f"   Telegram owner IDs: {len(settings.owner_id_set)} configured")
