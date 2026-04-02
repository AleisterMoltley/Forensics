"""Lightweight async Helius RPC client with TTL caching.

Provides the RPC primitives used by the bundler-derived analyzers:
transaction history, account info, slot-level queries, and signature
lookups.  All methods include retry + timeout handling.

Caching: ``get_transaction`` results are cached for 5 minutes (TX data
is immutable once confirmed) and ``get_signatures_for_address`` for 30s
(new TXs may appear).  This reduces RPC calls by 60-80% during a full
bundler analysis run where multiple detectors query the same mint.

This is a standalone module so the analyzers work even if the main
``src.rpc`` module (which powers the rest of the pipeline) is not
available.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

import aiohttp
from loguru import logger

from src.config import settings


# ---------------------------------------------------------------------------
# Simple TTL-LRU cache
# ---------------------------------------------------------------------------

class _TTLCache:
    """Thread-unsafe TTL + max-size cache for async RPC results.

    Entries expire after ``ttl`` seconds and the cache is capped at
    ``maxsize`` entries (oldest evicted first).
    """

    def __init__(self, maxsize: int = 2000, ttl: float = 300.0) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.monotonic() - ts > self._ttl:
            del self._data[key]
            return None
        # Move to end (most recently used)
        self._data.move_to_end(key)
        return value

    def put(self, key: str, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (time.monotonic(), value)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    @property
    def size(self) -> int:
        return len(self._data)


class HeliusRPC:
    """Thin async wrapper around Helius JSON-RPC + DAS endpoints.

    Includes:
    - Concurrency semaphore to cap parallel requests
    - Token-bucket rate limiter (global requests/second)
    - Exponential backoff with 429 rate-limit detection
    - Circuit breaker that pauses all calls after repeated failures
    """

    # Circuit breaker: after this many consecutive failures, pause for
    # _CB_PAUSE_SECONDS before allowing new requests.
    _CB_FAILURE_THRESHOLD: int = 10
    _CB_PAUSE_SECONDS: float = 30.0

    # Global rate limit: max requests per second across all callers.
    # Helius free tier = 10 RPS, paid = 50-500 RPS.  Default conservative.
    _RATE_LIMIT_RPS: int = 25
    _RATE_LIMIT_BURST: int = 40  # burst capacity

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        # TX data is immutable once confirmed → long TTL, large cache
        self._tx_cache = _TTLCache(maxsize=5000, ttl=600.0)
        # Signature lists change as new TXs appear → moderate TTL
        self._sig_cache = _TTLCache(maxsize=1000, ttl=45.0)
        # Account info cache — used by token extension checks
        self._account_cache = _TTLCache(maxsize=1000, ttl=120.0)
        # Concurrency limiter — max parallel RPC requests
        self._semaphore = asyncio.Semaphore(settings.scan_concurrency)
        # Circuit breaker state
        self._consecutive_failures: int = 0
        self._circuit_open_until: float = 0.0
        # Token bucket rate limiter
        self._bucket_tokens: float = float(self._RATE_LIMIT_BURST)
        self._bucket_last_refill: float = time.monotonic()
        self._bucket_lock = asyncio.Lock()
        # Metrics
        self._total_calls: int = 0
        self._total_cache_hits: int = 0

    @property
    def rpc_url(self) -> str:
        return f"{settings.helius_rpc_url}{settings.helius_api_key}"

    @property
    def stats(self) -> dict[str, Any]:
        """RPC client statistics for monitoring."""
        return {
            "total_calls": self._total_calls,
            "cache_hits": self._total_cache_hits,
            "tx_cache_size": self._tx_cache.size,
            "sig_cache_size": self._sig_cache.size,
            "account_cache_size": self._account_cache.size,
            "circuit_breaker_failures": self._consecutive_failures,
        }

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _acquire_rate_limit(self) -> None:
        """Token-bucket rate limiter — blocks until a token is available."""
        async with self._bucket_lock:
            now = time.monotonic()
            elapsed = now - self._bucket_last_refill
            self._bucket_tokens = min(
                float(self._RATE_LIMIT_BURST),
                self._bucket_tokens + elapsed * self._RATE_LIMIT_RPS,
            )
            self._bucket_last_refill = now

            if self._bucket_tokens >= 1.0:
                self._bucket_tokens -= 1.0
                return

        # No tokens available — wait for one to refill
        wait_time = (1.0 - self._bucket_tokens) / self._RATE_LIMIT_RPS
        await asyncio.sleep(wait_time)
        async with self._bucket_lock:
            self._bucket_tokens = max(0.0, self._bucket_tokens - 1.0)

    async def _call(
        self, method: str, params: list[Any], retries: int = 2
    ) -> Any:
        # Circuit breaker: if too many consecutive failures, pause
        now = time.monotonic()
        if self._consecutive_failures >= self._CB_FAILURE_THRESHOLD:
            if now < self._circuit_open_until:
                remaining = self._circuit_open_until - now
                logger.warning(
                    f"RPC circuit breaker open — {remaining:.0f}s remaining"
                )
                raise RuntimeError(
                    f"RPC circuit breaker open ({self._consecutive_failures} "
                    f"consecutive failures)"
                )
            # Reset after pause period
            logger.info("RPC circuit breaker: attempting reset")
            self._consecutive_failures = 0

        session = await self._ensure_session()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}

        async with self._semaphore:
            for attempt in range(retries + 1):
                # Global rate limiting before each attempt
                await self._acquire_rate_limit()
                self._total_calls += 1
                try:
                    async with session.post(self.rpc_url, json=payload) as resp:
                        # Handle HTTP 429 rate limit from Helius
                        if resp.status == 429:
                            retry_after = float(
                                resp.headers.get("Retry-After", 2 * (2 ** attempt))
                            )
                            logger.warning(
                                f"RPC 429 rate limited — backing off {retry_after:.1f}s"
                            )
                            await asyncio.sleep(retry_after)
                            continue

                        data = await resp.json()
                        if "error" in data:
                            err = data["error"]
                            # Helius returns -32429 or message containing "rate"
                            # for application-level rate limits
                            err_msg = str(err.get("message", "")) if isinstance(err, dict) else str(err)
                            if "rate" in err_msg.lower() or "-32429" in err_msg:
                                backoff = 2.0 * (2 ** attempt)
                                logger.warning(f"RPC rate limit error — backing off {backoff:.1f}s")
                                await asyncio.sleep(backoff)
                                continue
                            raise RuntimeError(f"RPC error: {err}")
                        # Success — reset failure counter
                        self._consecutive_failures = 0
                        return data.get("result")
                except RuntimeError:
                    raise  # Don't retry our own raised errors
                except Exception as e:
                    if attempt < retries:
                        backoff = 0.5 * (2 ** attempt)
                        await asyncio.sleep(backoff)
                    else:
                        self._consecutive_failures += 1
                        if self._consecutive_failures >= self._CB_FAILURE_THRESHOLD:
                            self._circuit_open_until = (
                                time.monotonic() + self._CB_PAUSE_SECONDS
                            )
                            logger.error(
                                f"RPC circuit breaker OPEN after "
                                f"{self._consecutive_failures} failures — "
                                f"pausing {self._CB_PAUSE_SECONDS}s"
                            )
                        raise

    # ------------------------------------------------------------------
    # High-level helpers used by the analyzers
    # ------------------------------------------------------------------

    async def get_signatures_for_address(
        self,
        address: str,
        limit: int = 50,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch confirmed transaction signatures for *address*."""
        cache_key = f"sig:{address}:{limit}:{before or ''}"
        cached = self._sig_cache.get(cache_key)
        if cached is not None:
            self._total_cache_hits += 1
            return cached

        opts: dict[str, Any] = {"limit": limit}
        if before:
            opts["before"] = before
        result = await self._call("getSignaturesForAddress", [address, opts]) or []
        self._sig_cache.put(cache_key, result)
        return result

    async def get_transaction(self, sig: str) -> dict[str, Any] | None:
        """Fetch a parsed transaction by signature."""
        cached = self._tx_cache.get(sig)
        if cached is not None:
            self._total_cache_hits += 1
            return cached

        result = await self._call(
            "getTransaction",
            [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        if result is not None:
            self._tx_cache.put(sig, result)
        return result

    async def get_block(self, slot: int) -> dict[str, Any] | None:
        """Fetch a block by slot number."""
        return await self._call(
            "getBlock",
            [
                slot,
                {
                    "encoding": "jsonParsed",
                    "transactionDetails": "signatures",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )

    async def get_account_info(self, address: str) -> dict[str, Any] | None:
        """Fetch parsed account info (cached 120s)."""
        cached = self._account_cache.get(address)
        if cached is not None:
            self._total_cache_hits += 1
            return cached

        result = await self._call(
            "getAccountInfo",
            [address, {"encoding": "jsonParsed"}],
        )
        if result is not None:
            self._account_cache.put(address, result)
        return result

    async def get_token_accounts_by_owner(
        self, owner: str, mint: str
    ) -> list[dict[str, Any]]:
        """Fetch SPL token accounts for *owner* filtered by *mint*."""
        result = await self._call(
            "getTokenAccountsByOwner",
            [
                owner,
                {"mint": mint},
                {"encoding": "jsonParsed"},
            ],
        )
        return (result or {}).get("value", [])


# Module-level singleton — importable as ``from src.analyzers.rpc import rpc``
rpc = HeliusRPC()
