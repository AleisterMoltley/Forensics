"""Coordinated exit detector.

Derived from Anon-Bundler ``autoSell.ts``:
  - Sell triggers: profit target (default 35%) or trailing stop (20% from ATH)
  - Execution order: master wallet first (50% of holdings), then buyer
    wallets (100% of holdings) with 2-second stagger
  - All sells routed through Jupiter V6 (quote → swap → sign → send)
  - Sell amounts are percentage-based, not fixed SOL amounts

Detection strategy:
  After a token's price peaks, look for a burst of sell transactions from
  multiple wallets within a short time window (~30s).  If the sellers are
  linked (common funding source), it's a coordinated dump.  The staggered
  timing and master-first pattern are strong bundler signatures.

Signals produced:
  - sell_burst_count:     number of sells in the detection window
  - sell_burst_window_s:  time span of the burst (seconds)
  - sell_wallets:         distinct wallets that sold
  - master_sold_first:    whether the deployer/master sold before others
  - stagger_interval_s:   average time between consecutive sells
  - linked_sellers:       how many sellers share a common funder
  - score:                0-100 sub-score
"""
from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.analyzers.rpc import rpc


# Bundler autoSell pattern constants
BUNDLER_STAGGER_MS = 2000  # 2s between sells
BUNDLER_STAGGER_TOLERANCE_MS = 3000  # ±3s
MAX_BURST_WINDOW_S = 120  # analyze sells within 2 minutes
MIN_BURST_SELLS = 3  # minimum sells to flag as coordinated


@dataclass
class CoordinatedExitResult:
    sell_burst_count: int = 0
    sell_burst_window_s: float = 0.0
    sell_wallets: list[str] = field(default_factory=list)
    master_sold_first: bool = False
    stagger_interval_s: float = 0.0
    linked_sellers: int = 0
    score: float = 0.0
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sell_burst_count": self.sell_burst_count,
            "sell_burst_window_s": round(self.sell_burst_window_s, 1),
            "sell_wallets": self.sell_wallets,
            "master_sold_first": self.master_sold_first,
            "stagger_interval_s": round(self.stagger_interval_s, 2),
            "linked_sellers": self.linked_sellers,
            "score": self.score,
            "flags": self.flags,
        }


async def analyze_coordinated_exit(
    mint: str,
    deployer: str,
    lookback: int = 100,
) -> CoordinatedExitResult:
    """Detect coordinated sell patterns indicating a bundler auto-sell exit.

    Parameters
    ----------
    mint : str
        Token mint address.
    deployer : str
        Deployer / master wallet address.
    lookback : int
        Number of recent transactions to scan.
    """
    result = CoordinatedExitResult()

    try:
        sigs = await rpc.get_signatures_for_address(mint, limit=lookback)
        if len(sigs) < 5:
            return result

        # Parse sells (token balance decrease for any wallet)
        sells: list[dict[str, Any]] = []

        sem = asyncio.Semaphore(10)

        async def _parse_sell(sig_info: dict) -> None:
            sig = sig_info.get("signature", "")
            block_time = sig_info.get("blockTime", 0)

            async with sem:
                try:
                    tx = await rpc.get_transaction(sig)
                    if not tx:
                        return

                    meta = tx.get("meta", {})
                    if meta.get("err"):
                        return

                    pre_token = meta.get("preTokenBalances", [])
                    post_token = meta.get("postTokenBalances", [])

                    for ptb in post_token:
                        if ptb.get("mint") != mint:
                            continue
                        owner = ptb.get("owner", "")
                        post_amount = int(
                            ptb.get("uiTokenAmount", {}).get("amount", "0")
                        )
                        pre_amount = 0
                        for prb in pre_token:
                            if prb.get("mint") == mint and prb.get("owner") == owner:
                                pre_amount = int(
                                    prb.get("uiTokenAmount", {}).get("amount", "0")
                                )
                                break

                        token_delta = post_amount - pre_amount
                        if token_delta < 0:  # This is a sell
                            sells.append({
                                "wallet": owner,
                                "tokens_sold": abs(token_delta),
                                "block_time": block_time,
                                "sig": sig,
                            })
                            break

                except Exception:
                    return

        await asyncio.gather(*(_parse_sell(s) for s in sigs))

        if len(sells) < MIN_BURST_SELLS:
            return result

        # Sort by time
        sells.sort(key=lambda s: s["block_time"])

        # Find the densest burst of sells (sliding window)
        best_burst: list[dict] = []

        for i in range(len(sells)):
            window = [sells[i]]
            for j in range(i + 1, len(sells)):
                if sells[j]["block_time"] - sells[i]["block_time"] <= MAX_BURST_WINDOW_S:
                    window.append(sells[j])
                else:
                    break
            if len(window) > len(best_burst):
                best_burst = window

        if len(best_burst) < MIN_BURST_SELLS:
            return result

        result.sell_burst_count = len(best_burst)
        result.sell_burst_window_s = (
            best_burst[-1]["block_time"] - best_burst[0]["block_time"]
        )

        sell_wallet_list = [s["wallet"] for s in best_burst]
        result.sell_wallets = list(dict.fromkeys(sell_wallet_list))  # unique, ordered

        # Check if deployer sold first
        if sell_wallet_list[0] == deployer:
            result.master_sold_first = True

        # Calculate stagger intervals
        intervals = []
        for i in range(1, len(best_burst)):
            dt = best_burst[i]["block_time"] - best_burst[i - 1]["block_time"]
            intervals.append(dt)

        if intervals:
            result.stagger_interval_s = statistics.mean(intervals)

        # Check how many sellers are linked (funded by deployer)
        # Quick heuristic: check first TX of each selling wallet
        linked = 0
        for wallet in result.sell_wallets[:10]:
            if wallet == deployer:
                linked += 1
                continue
            try:
                wallet_sigs = await rpc.get_signatures_for_address(wallet, limit=5)
                if wallet_sigs:
                    earliest = wallet_sigs[-1]
                    tx = await rpc.get_transaction(earliest.get("signature", ""))
                    if tx:
                        msg = tx.get("transaction", {}).get("message", {})
                        account_keys = msg.get("accountKeys", [])
                        keys = [
                            (ak if isinstance(ak, str) else ak.get("pubkey", ""))
                            for ak in account_keys
                        ]
                        if deployer in keys:
                            linked += 1
            except Exception:
                continue

        result.linked_sellers = linked

        # Flags
        result.flags.append(
            f"{result.sell_burst_count} sells within {result.sell_burst_window_s:.0f}s"
        )

        if result.master_sold_first:
            result.flags.append("Deployer/master wallet sold FIRST (bundler pattern)")

        if 1.0 <= result.stagger_interval_s <= 5.0:
            result.flags.append(
                f"Average stagger: {result.stagger_interval_s:.1f}s "
                f"(bundler default: ~2s)"
            )

        if result.linked_sellers >= 2:
            result.flags.append(
                f"{result.linked_sellers}/{len(result.sell_wallets)} sellers "
                f"are linked to the deployer"
            )

        if len(result.sell_wallets) >= 3 and result.sell_burst_window_s <= 30:
            result.flags.append("Rapid coordinated dump from multiple wallets")

        # Score
        score = 0.0

        # Burst size
        if result.sell_burst_count >= 5:
            score += 25
        elif result.sell_burst_count >= 3:
            score += 15

        # Master sold first
        if result.master_sold_first:
            score += 20

        # Stagger in bundler range
        if 1.0 <= result.stagger_interval_s <= 5.0:
            score += 20
        elif 0.5 <= result.stagger_interval_s <= 10.0:
            score += 10

        # Linked sellers
        if len(result.sell_wallets) > 0:
            link_ratio = result.linked_sellers / len(result.sell_wallets)
            score += min(25, link_ratio * 30)

        # Tight burst window
        if result.sell_burst_window_s <= 30 and result.sell_burst_count >= 3:
            score += 10

        result.score = min(100.0, round(score, 1))
        return result

    except Exception as e:
        logger.error(f"Coordinated exit analysis failed: {e}")
        return result
