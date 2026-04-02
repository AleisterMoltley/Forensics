"""Raydium new pool / token launch listener.

Monitors the Raydium AMM program for new liquidity pool creation events
via Helius WebSocket subscriptions.  When a new pool is detected, it
extracts the token mint and deployer and forwards the event to the
forensic pipeline.
"""
from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from typing import Any, Callable, Coroutine

import websockets
from loguru import logger

from src.config import settings


# Raydium AMM V4 program ID
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# Helius enhanced WebSocket endpoint (logsSubscribe for program mentions)
# We subscribe to logs that mention the Raydium AMM program.
SUBSCRIBE_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "logsSubscribe",
    "params": [
        {"mentions": [RAYDIUM_AMM_V4]},
        {"commitment": "confirmed"},
    ],
}


class RaydiumListener:
    """Listens for new Raydium liquidity pool creations via Helius WebSocket."""

    def __init__(
        self,
        on_launch: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        self._on_launch = on_launch
        self._running = False
        self._ws: Any = None
        self._seen_mints: OrderedDict[str, None] = OrderedDict()

    async def start(self) -> None:
        """Connect to Helius WebSocket and listen for Raydium pool creates."""
        self._running = True
        logger.info("Raydium listener starting...")

        while self._running:
            try:
                ws_url = settings.ws_url
                async with websockets.connect(
                    ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    await ws.send(json.dumps(SUBSCRIBE_PAYLOAD))
                    logger.info("Raydium WebSocket connected — listening for new pools")

                    async for raw_msg in ws:
                        if not self._running:
                            break

                        try:
                            data = json.loads(raw_msg)
                            result = data.get("params", {}).get("result", {})
                            value = result.get("value", {})
                            logs = value.get("logs", [])
                            sig = value.get("signature", "")

                            # Look for "initialize2" or "initialize" in logs
                            # which indicates a new pool creation
                            is_init = any(
                                "initialize" in log.lower() for log in logs
                            )
                            if not is_init or not sig:
                                continue

                            # Fetch the actual transaction to get mint + deployer
                            launch = await self._resolve_launch(sig)
                            if launch and launch["mint"] not in self._seen_mints:
                                self._seen_mints[launch["mint"]] = None
                                # Cap memory: evict oldest entries (FIFO order)
                                while len(self._seen_mints) > 10_000:
                                    self._seen_mints.popitem(last=False)
                                await self._on_launch(launch)

                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.error(f"Raydium event error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Raydium WebSocket error: {e}")

            self._ws = None
            if self._running:
                logger.info("Raydium: reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _resolve_launch(self, sig: str) -> dict[str, Any] | None:
        """Fetch a transaction by signature and extract mint + deployer."""
        from src.analyzers.rpc import rpc

        try:
            tx = await rpc.get_transaction(sig)
            if not tx:
                return None

            meta = tx.get("meta", {})
            if meta.get("err"):
                return None

            msg = tx.get("transaction", {}).get("message", {})
            account_keys = msg.get("accountKeys", [])
            keys = [
                (ak if isinstance(ak, str) else ak.get("pubkey", ""))
                for ak in account_keys
            ]

            # Deployer is typically the fee-payer (index 0)
            deployer = keys[0] if keys else ""

            # Find the token mint from postTokenBalances
            post_token = meta.get("postTokenBalances", [])
            mint = ""
            for ptb in post_token:
                m = ptb.get("mint", "")
                if m and m not in (
                    "So11111111111111111111111111111111111111112",  # wrapped SOL
                ):
                    mint = m
                    break

            if not mint:
                return None

            return {
                "mint": mint,
                "deployer": deployer,
                "source": "raydium",
                "name": "",
                "symbol": "",
                "raw": {"signature": sig},
            }

        except Exception as e:
            logger.debug(f"Raydium: failed to resolve {sig[:16]}: {e}")
            return None

    async def stop(self) -> None:
        """Gracefully stop the listener."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("Raydium listener stopped")
