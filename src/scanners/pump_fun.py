"""Pump.fun token launch WebSocket listener.

Connects to the Pump.fun WebSocket stream and emits new token launch
events to the forensic pipeline via the ``on_launch`` callback.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Coroutine

import aiohttp
from loguru import logger

from src.config import settings


# Pump.fun WebSocket endpoint for new token creations
PUMP_FUN_WS_URL = "wss://pumpportal.fun/api/data"

# Subscription message to receive new token events
SUBSCRIBE_MSG = {
    "method": "subscribeNewToken",
}


class PumpFunListener:
    """Listens to Pump.fun for new token creations and forwards them to the pipeline."""

    def __init__(
        self,
        on_launch: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        self._on_launch = on_launch
        self._running = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Connect to Pump.fun WebSocket and listen for new tokens."""
        self._running = True
        logger.info("Pump.fun listener starting...")

        while self._running:
            try:
                self._session = aiohttp.ClientSession()
                self._ws = await self._session.ws_connect(
                    PUMP_FUN_WS_URL,
                    heartbeat=30.0,
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=90),
                )

                # Subscribe to new token events
                await self._ws.send_json(SUBSCRIBE_MSG)
                logger.info("Pump.fun WebSocket connected — listening for new tokens")

                async for msg in self._ws:
                    if not self._running:
                        break

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            launch = self._parse_event(data)
                            if launch:
                                await self._on_launch(launch)
                        except json.JSONDecodeError:
                            logger.debug(f"Pump.fun: invalid JSON: {msg.data[:100]}")
                        except Exception as e:
                            logger.error(f"Pump.fun event processing error: {e}")

                    elif msg.type in (
                        aiohttp.WSMsgType.ERROR,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        logger.warning(f"Pump.fun WebSocket closed: {msg.type}")
                        break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pump.fun WebSocket error: {e}")
            finally:
                await self._cleanup()

            if self._running:
                logger.info("Pump.fun: reconnecting in 5s...")
                await asyncio.sleep(5)

    def _parse_event(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Parse a Pump.fun WebSocket event into a launch dict."""
        # Pump.fun sends different event types; we care about token creations
        mint = data.get("mint") or data.get("token_address") or data.get("tokenAddress")
        if not mint:
            return None

        deployer = (
            data.get("traderPublicKey")
            or data.get("deployer")
            or data.get("creator")
            or ""
        )

        return {
            "mint": mint,
            "deployer": deployer,
            "name": data.get("name", ""),
            "symbol": data.get("symbol", ""),
            "source": "pump_fun",
            "raw": data,
        }

    async def _cleanup(self) -> None:
        """Close WebSocket and HTTP session."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def stop(self) -> None:
        """Gracefully stop the listener."""
        self._running = False
        await self._cleanup()
        logger.info("Pump.fun listener stopped")
