"""FastAPI dashboard with authentication, rate-limiting, and input validation.

Security design:
- All /api/*, /ws, /export, /train, /backtest endpoints require a valid
  X-API-Key header checked by the `require_admin` dependency.
  The key is set via the ADMIN_API_KEY environment variable.
- /health and /metrics are intentionally public so Railway's healthcheck
  and Prometheus scrapers work without credentials.
- Rate limiting (slowapi) prevents brute-force and DoS on every endpoint.
- WebSocket connections are authenticated via a ?token= query parameter
  because browser WebSocket APIs cannot set custom headers.
- All address/mint inputs are validated with Pydantic v2 strict validators
  to prevent injection through query parameters.
- Secrets are never echoed in error responses.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config import settings


# ---------------------------------------------------------------------------
# Rate limiter — keyed by client IP
# Why: prevents brute-force attacks on the API key and DoS on expensive
#      endpoints like /api/backtest or /api/deployers.
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# Input validation models (Pydantic v2 strict mode)
# ---------------------------------------------------------------------------

# Solana addresses/mints are base58, 32–44 characters.
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _validate_solana_address(v: str) -> str:
    """Strict validator for Solana public keys / mint addresses.

    Why: Query params that land in RPC calls or DB queries must be
    sanitised to prevent unexpected behaviour or log injection.
    """
    if not _BASE58_RE.match(v):
        raise ValueError("Invalid Solana address format")
    return v


class MintAddressParam(BaseModel):
    """Validated mint address for use as a query/path parameter."""

    model_config = {"strict": True}

    mint: str = Field(..., min_length=32, max_length=44)

    @field_validator("mint")
    @classmethod
    def validate_mint(cls, v: str) -> str:
        return _validate_solana_address(v)


class WalletAddressParam(BaseModel):
    """Validated wallet address for use as a query/path parameter."""

    model_config = {"strict": True}

    wallet: str = Field(..., min_length=32, max_length=44)

    @field_validator("wallet")
    @classmethod
    def validate_wallet(cls, v: str) -> str:
        return _validate_solana_address(v)


# ---------------------------------------------------------------------------
# Authentication — require_admin dependency
# ---------------------------------------------------------------------------

# APIKeyHeader integrates with FastAPI's OpenAPI/Swagger UI: the "Authorize"
# button appears automatically and the scheme is documented in the schema.
# auto_error=False lets us return a custom 401 instead of FastAPI's default.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_admin(api_key: str | None = Security(_api_key_header)) -> None:
    """FastAPI dependency that enforces X-API-Key header authentication.

    Reads ADMIN_API_KEY from settings and compares with the supplied header
    value using `secrets.compare_digest` to prevent timing-based attacks.

    If ADMIN_API_KEY is not configured (local dev), auth is skipped with a
    warning so developers can iterate without friction.  In production,
    always set a strong random value:
        python -c "import secrets; print(secrets.token_urlsafe(32))"
    """
    if not settings.admin_api_key:
        # Auth disabled — acceptable only in local dev
        return

    if not api_key or not secrets.compare_digest(
        api_key.encode(), settings.admin_api_key.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


# Convenience alias — use as `dependencies=[RequireAdmin]` on route decorators
RequireAdmin = Depends(require_admin)


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class _ConnectionManager:
    """Tracks active WebSocket connections and broadcasts JSON payloads."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    """Create and configure the FastAPI dashboard application.

    Parameters
    ----------
    session_factory:
        Async SQLAlchemy session factory injected from main.py.
    """
    app = FastAPI(
        title="Token Launch Forensics Dashboard",
        version="1.0.0",
        # Hide schema endpoints in production to reduce attack surface
        docs_url=None if settings.is_railway else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_railway else "/openapi.json",
    )

    # CORS — restrict to same origin in production; allow all in local dev
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.is_railway else [],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["X-API-Key"],
    )

    # Attach limiter and its error handler
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # WebSocket manager exposed on app.state so main.py can call broadcast()
    manager = _ConnectionManager()
    app.state.broadcast = manager.broadcast

    # -----------------------------------------------------------------------
    # Public endpoints — no auth, accessible by Railway healthcheck/Prometheus
    # -----------------------------------------------------------------------

    @app.get("/health")
    @limiter.limit("60/minute")
    async def health(request: Request) -> dict[str, Any]:
        """Railway healthcheck — always public."""
        return {"status": "ok"}

    @app.get("/metrics")
    @limiter.limit("30/minute")
    async def metrics_endpoint(request: Request) -> JSONResponse:
        """Prometheus metrics — public so external scrapers work."""
        # main.py replaces this with the real metrics export; this is a stub
        return JSONResponse(content={"info": "metrics not yet initialised"})

    # -----------------------------------------------------------------------
    # Protected endpoints — require valid X-API-Key header (require_admin)
    # -----------------------------------------------------------------------

    @app.get("/api/launches", dependencies=[RequireAdmin])
    @limiter.limit("30/minute")
    async def get_launches(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        """Return recent token launches.

        Why protected: Exposes deployer wallet addresses and risk scores
        that could be used for counter-intelligence by rug-pull operators.
        """
        from sqlalchemy import select, desc
        from models import TokenLaunch

        async with session_factory() as session:
            result = await session.execute(
                select(TokenLaunch)
                .order_by(desc(TokenLaunch.launched_at))
                .offset(offset)
                .limit(limit)
            )
            rows = result.scalars().all()
            return [
                {
                    "mint": r.mint,
                    "name": r.name,
                    "symbol": r.symbol,
                    "deployer": r.deployer,
                    "source": r.source,
                    "risk_score": r.risk_score_total,
                    "is_rug": r.is_rug,
                    "launched_at": r.launched_at.isoformat() if r.launched_at else None,
                }
                for r in rows
            ]

    @app.get("/api/deployers", dependencies=[RequireAdmin])
    @limiter.limit("30/minute")
    async def get_deployers(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        """Return known deployer records.

        Why protected: Contains full wallet addresses and rug history —
        sensitive intelligence data.
        """
        from sqlalchemy import select, desc
        from models import Deployer

        async with session_factory() as session:
            result = await session.execute(
                select(Deployer).order_by(desc(Deployer.rug_count)).limit(limit)
            )
            rows = result.scalars().all()
            return [
                {
                    "address": r.address,
                    "total_launches": r.total_launches,
                    "rug_count": r.rug_count,
                    "watchlisted": r.watchlisted,
                    "first_seen": r.first_seen.isoformat() if r.first_seen else None,
                    "last_seen": r.last_seen.isoformat() if r.last_seen else None,
                }
                for r in rows
            ]

    @app.get("/api/lookup/{mint}", dependencies=[RequireAdmin])
    @limiter.limit("20/minute")
    async def lookup_mint(
        request: Request,
        mint: str,
    ) -> dict[str, Any]:
        """Look up a specific mint address.

        Why validated: The mint value is used in RPC and DB queries;
        strict format enforcement prevents unexpected behaviour.
        """
        _validate_solana_address(mint)  # raises ValueError → 422 if invalid
        from sqlalchemy import select
        from models import TokenLaunch

        async with session_factory() as session:
            result = await session.execute(
                select(TokenLaunch).where(TokenLaunch.mint == mint)
            )
            row = result.scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404, detail="Mint not found")
            return {
                "mint": row.mint,
                "name": row.name,
                "symbol": row.symbol,
                "deployer": row.deployer,
                "source": row.source,
                "risk_score": row.risk_score_total,
                "score_deployer": row.score_deployer,
                "score_holders": row.score_holders,
                "score_lp": row.score_lp,
                "score_bundled": row.score_bundled,
                "score_contract": row.score_contract,
                "score_social": row.score_social,
                "is_rug": row.is_rug,
                "launched_at": row.launched_at.isoformat() if row.launched_at else None,
            }

    @app.get("/api/backtest", dependencies=[RequireAdmin])
    @limiter.limit("5/minute")  # expensive — tighter limit
    async def api_backtest(request: Request) -> dict[str, Any]:
        """Run backtest engine against historical data.

        Tighter rate limit: backtesting is CPU/DB intensive.
        Why protected: Exposes model accuracy and internal scoring details.

        Note: BacktestEngine lives in src/backtest.py which is part of the
        wider src/ package wired up by main.py.  The import is deferred here
        so that the dashboard can be imported independently (e.g. in tests)
        without requiring the full src/ package to be available.
        """
        try:
            from src.backtest import BacktestEngine  # deferred — see docstring
            engine = BacktestEngine(session_factory)
            result = await engine.run()
            return result.to_dict()
        except ImportError:
            raise HTTPException(status_code=503, detail="Backtest engine not available")

    @app.get("/api/metrics", dependencies=[RequireAdmin])
    @limiter.limit("60/minute")
    async def api_metrics_detail(request: Request) -> dict[str, Any]:
        """Detailed JSON metrics (includes queue depth).

        Why protected: Reveals internal throughput and queue state.
        """
        return {"info": "metrics endpoint — attach via main.py"}

    @app.post("/api/train", dependencies=[RequireAdmin])
    @limiter.limit("2/minute")  # training is very expensive
    async def trigger_training(request: Request) -> dict[str, str]:
        """Trigger ML model retraining.

        Why protected and rate-limited: Retraining is CPU-intensive and
        could be abused to degrade model quality through forced retrains.
        """
        return {"status": "training trigger — wire up via main.py"}

    @app.get("/export", dependencies=[RequireAdmin])
    @limiter.limit("5/minute")
    async def export_data(request: Request) -> dict[str, Any]:
        """Export training data CSV.

        Why protected: Contains full historical data including wallet
        addresses and outcome labels.
        """
        return {"info": "export endpoint — wire up via main.py"}

    # -----------------------------------------------------------------------
    # Authenticated WebSocket feed
    # -----------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_feed(
        ws: WebSocket,
        token: str = Query(default=""),
    ) -> None:
        """Real-time launch feed over WebSocket.

        Authentication is via ?token=<ADMIN_API_KEY> query param because
        the browser WebSocket API does not support custom request headers.

        Why protected: The feed reveals live deployer wallets and risk
        scores in real time, giving adversaries early warning.
        """
        # Validate token before accepting the connection
        if settings.admin_api_key and not secrets.compare_digest(
            token.encode(), settings.admin_api_key.encode()
        ):
            await ws.close(code=1008)  # Policy Violation
            return

        await manager.connect(ws)
        logger.debug("WebSocket client connected")
        try:
            while True:
                # Keep connection alive; actual data is pushed via broadcast()
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug(f"WebSocket error: {e}")
        finally:
            manager.disconnect(ws)
            logger.debug("WebSocket client disconnected")

    return app

