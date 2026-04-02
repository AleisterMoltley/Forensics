"""Analysis queue — asyncio or Redis-backed job queue.

Provides a unified interface for queueing token launch analysis jobs.
Falls back to a simple asyncio.Queue when Redis is not configured.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Coroutine

from loguru import logger

from src.config import settings


class AnalysisQueue:
    """Job queue for token launch analysis with optional Redis backend."""

    def __init__(
        self,
        redis_url: str = "",
        num_workers: int = 3,
    ) -> None:
        self._redis_url = redis_url
        self._num_workers = num_workers
        self._use_redis = False
        self._redis: Any = None
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self._workers: list[asyncio.Task] = []
        self._running = False
        self._processed = 0
        self._errors = 0

    async def connect(self) -> None:
        """Connect to Redis (if configured)."""
        if not self._redis_url or not settings.use_redis_queue:
            logger.info("Queue: using asyncio (no Redis)")
            return

        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_timeout=5,
            )
            await self._redis.ping()
            self._use_redis = True
            logger.info("Queue: connected to Redis")
        except ImportError:
            logger.warning("Queue: redis package not installed, using asyncio")
        except Exception as e:
            logger.warning(f"Queue: Redis connection failed ({e}), using asyncio")

    async def enqueue(
        self, launch: dict[str, Any], priority: bool = False
    ) -> None:
        """Add a launch to the queue."""
        if self._use_redis and self._redis:
            try:
                key = "forensics:queue:priority" if priority else "forensics:queue"
                await self._redis.rpush(key, json.dumps(launch))
                return
            except Exception as e:
                logger.warning(f"Redis enqueue failed ({e}), falling back")

        try:
            self._queue.put_nowait(launch)
        except asyncio.QueueFull:
            logger.warning("Queue full — dropping launch")

    async def start_workers(
        self,
        handler: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        """Start worker tasks that process queued launches."""
        self._running = True

        for i in range(self._num_workers):
            task = asyncio.create_task(
                self._worker(handler, i), name=f"queue_worker_{i}"
            )
            self._workers.append(task)

        logger.info(f"Queue: started {self._num_workers} workers")

    async def _worker(
        self,
        handler: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
        worker_id: int,
    ) -> None:
        """Single worker loop."""
        while self._running:
            try:
                launch = await self._dequeue()
                if launch:
                    try:
                        await handler(launch)
                        self._processed += 1
                    except Exception as e:
                        self._errors += 1
                        logger.error(f"Worker {worker_id}: handler error: {e}")
                else:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}")
                await asyncio.sleep(1)

    async def _dequeue(self) -> dict[str, Any] | None:
        """Get next item from queue (Redis priority queue first, then normal)."""
        if self._use_redis and self._redis:
            try:
                # Check priority queue first
                data = await self._redis.lpop("forensics:queue:priority")
                if not data:
                    data = await self._redis.lpop("forensics:queue")
                if data:
                    return json.loads(data)
                return None
            except Exception as e:
                logger.debug(f"Redis dequeue failed, falling back to asyncio: {e}")

        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def get_depth(self) -> int:
        """Return current queue depth."""
        if self._use_redis and self._redis:
            try:
                normal = await self._redis.llen("forensics:queue") or 0
                priority = await self._redis.llen("forensics:queue:priority") or 0
                return normal + priority
            except Exception as e:
                logger.debug(f"Redis queue depth check failed: {e}")
        return self._queue.qsize()

    def get_metrics(self) -> dict[str, Any]:
        """Return queue metrics."""
        return {
            "backend": "redis" if self._use_redis else "asyncio",
            "workers": self._num_workers,
            "processed": self._processed,
            "errors": self._errors,
        }

    async def stop_workers(self) -> None:
        """Stop all worker tasks."""
        self._running = False
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("Queue workers stopped")

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()
            self._redis = None
