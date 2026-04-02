from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, JSON, Text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime, timezone


class Base(DeclarativeBase):
    pass


class TokenLaunch(Base):
    __tablename__ = "token_launches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mint = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, default="")
    symbol = Column(String, default="")
    deployer = Column(String, index=True, nullable=False)
    source = Column(String, default="unknown")  # pump_fun | raydium
    launched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Risk scores (0-100 each)
    risk_score_total = Column(Float, default=0.0)
    score_deployer = Column(Float, default=0.0)
    score_holders = Column(Float, default=0.0)
    score_lp = Column(Float, default=0.0)
    score_bundled = Column(Float, default=0.0)
    score_contract = Column(Float, default=0.0)
    score_social = Column(Float, default=0.0)

    # Raw analysis data
    deployer_data = Column(JSON, default=dict)
    holder_data = Column(JSON, default=dict)
    lp_data = Column(JSON, default=dict)
    bundle_data = Column(JSON, default=dict)
    contract_data = Column(JSON, default=dict)
    social_data = Column(JSON, default=dict)

    # Outcome tracking
    is_rug = Column(Boolean, default=None, nullable=True)
    rug_detected_at = Column(DateTime, nullable=True)
    peak_mcap = Column(Float, nullable=True)
    current_mcap = Column(Float, nullable=True)

    alerted = Column(Boolean, default=False)
    scanned_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Deployer(Base):
    __tablename__ = "deployers"

    address = Column(String, primary_key=True)
    total_launches = Column(Integer, default=0)
    rug_count = Column(Integer, default=0)
    first_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    watchlisted = Column(Boolean, default=False)
    notes = Column(Text, default="")


class AlertConfig(Base):
    __tablename__ = "alert_config"

    id = Column(Integer, primary_key=True, default=1)
    alerts_enabled = Column(Boolean, default=True)
    min_risk_threshold = Column(Integer, default=50)
    chat_id = Column(String, default="")


async def init_db(database_url: str):
    """Initialize database with appropriate settings for SQLite or PostgreSQL."""
    from loguru import logger

    is_postgres = "postgresql" in database_url or "asyncpg" in database_url

    engine_kwargs = {"echo": False}
    if is_postgres:
        engine_kwargs.update({
            "pool_size": 5,
            "max_overflow": 10,
            "pool_timeout": 30,
            "pool_recycle": 1800,
            "pool_pre_ping": True,
        })
        logger.info(f"Connecting to PostgreSQL...")
    else:
        logger.info(f"Using SQLite: {database_url}")

    engine = create_async_engine(database_url, **engine_kwargs)

    # Create tables (safe to call multiple times)
    retries = 3
    for attempt in range(retries):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables ready")
            break
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"DB init attempt {attempt + 1} failed: {e}, retrying...")
                import asyncio
                await asyncio.sleep(2 ** attempt)
            else:
                raise

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_factory
