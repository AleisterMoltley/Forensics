"""Funding fan-out detector.

Derived from Anon-Bundler ``funding.ts``:
  - A master wallet funds N buyer wallets in batches of 8
  - Each wallet receives the same SOL amount (CONFIG.solPerWallet)
  - Transfers use ComputeBudgetProgram priority fees
  - Total budget includes 5% overhead + Jito tip reservation

Detection strategy:
  Given a deployer address, walk its recent outbound transfers and look
  for a "fan-out" pattern: multiple transfers of the same (or near-same)
  lamport amount to distinct fresh wallets within a short time window.
  If those destination wallets later buy the launched token, score HIGH.

Signals produced:
  - fan_out_count:       number of same-amount outbound transfers
  - fan_out_amount_sol:  the repeated SOL amount
  - fan_out_batch_size:  detected batch grouping (8 = strong bundler signal)
  - funded_wallets:      list of destination addresses
  - funded_then_bought:  how many of those wallets bought the token
  - score:               0-100 sub-score for the bundled_buys dimension
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.analyzers.rpc import rpc


# ---------------------------------------------------------------------------
# Constants calibrated from Anon-Bundler defaults
# ---------------------------------------------------------------------------

# Bundler default: 0.28 SOL per wallet.  We match within ±5%.
AMOUNT_TOLERANCE_PCT = 0.05
# Bundler default: batches of 8 transfers per TX
TYPICAL_BATCH_SIZE = 8
# Time window: all funding TXs land within ~60s typically
MAX_FUNDING_WINDOW_SLOTS = 50  # ~25 seconds at 400ms/slot
# Minimum fan-out count to flag
MIN_FAN_OUT = 3


@dataclass
class FanOutResult:
    fan_out_count: int = 0
    fan_out_amount_sol: float = 0.0
    fan_out_batch_size: int = 0
    funded_wallets: list[str] = field(default_factory=list)
    funded_then_bought: int = 0
    score: float = 0.0
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fan_out_count": self.fan_out_count,
            "fan_out_amount_sol": self.fan_out_amount_sol,
            "fan_out_batch_size": self.fan_out_batch_size,
            "funded_wallets_count": len(self.funded_wallets),
            "funded_then_bought": self.funded_then_bought,
            "score": self.score,
            "flags": self.flags,
        }


def _group_by_amount(
    transfers: list[dict[str, Any]], tolerance_pct: float = AMOUNT_TOLERANCE_PCT
) -> dict[int, list[dict]]:
    """Group transfers by approximate lamport amount (within tolerance)."""
    if not transfers:
        return {}

    amounts = sorted(set(t["lamports"] for t in transfers))
    groups: dict[int, list[dict]] = {}
    used = set()

    for anchor in amounts:
        if anchor in used:
            continue
        lo = anchor * (1 - tolerance_pct)
        hi = anchor * (1 + tolerance_pct)
        group = [t for t in transfers if lo <= t["lamports"] <= hi]
        if len(group) >= MIN_FAN_OUT:
            groups[anchor] = group
            for t in group:
                used.add(t["lamports"])

    return groups


async def analyze_funding_fanout(
    deployer: str,
    mint: str | None = None,
    lookback: int = 100,
) -> FanOutResult:
    """Analyze a deployer wallet for bundler-style funding fan-out.

    Parameters
    ----------
    deployer : str
        The deployer/master wallet address.
    mint : str, optional
        Token mint address — if provided, checks whether funded wallets
        later purchased this token (dramatically increases confidence).
    lookback : int
        How many recent signatures to scan from the deployer.
    """
    result = FanOutResult()

    try:
        sigs = await rpc.get_signatures_for_address(deployer, limit=lookback)
        if not sigs:
            return result

        # Parse outbound SOL transfers from deployer
        outbound_transfers: list[dict[str, Any]] = []

        # Fetch TXs concurrently (capped at 10 parallel)
        sem = asyncio.Semaphore(10)

        async def fetch_tx(sig_info: dict) -> None:
            async with sem:
                sig = sig_info.get("signature", "")
                slot = sig_info.get("slot", 0)
                try:
                    tx = await rpc.get_transaction(sig)
                    if not tx:
                        return

                    meta = tx.get("meta", {})
                    if meta.get("err"):
                        return  # skip failed TXs

                    msg = tx.get("transaction", {}).get("message", {})
                    account_keys = msg.get("accountKeys", [])
                    # Find the deployer index
                    deployer_idx = None
                    for i, ak in enumerate(account_keys):
                        key = ak if isinstance(ak, str) else ak.get("pubkey", "")
                        if key == deployer:
                            deployer_idx = i
                            break

                    if deployer_idx is None:
                        return

                    pre_balances = meta.get("preBalances", [])
                    post_balances = meta.get("postBalances", [])

                    # Find all accounts that received SOL from deployer
                    for i, ak in enumerate(account_keys):
                        if i == deployer_idx:
                            continue
                        key = ak if isinstance(ak, str) else ak.get("pubkey", "")
                        pre = pre_balances[i] if i < len(pre_balances) else 0
                        post = post_balances[i] if i < len(post_balances) else 0
                        received = post - pre
                        if received > 10_000_000:  # > 0.01 SOL
                            outbound_transfers.append({
                                "to": key,
                                "lamports": received,
                                "slot": slot,
                                "sig": sig,
                            })
                except Exception as e:
                    logger.debug(f"Fan-out: failed to parse TX {sig[:16]}: {e}")

        tasks = [fetch_tx(s) for s in sigs[:lookback]]
        await asyncio.gather(*tasks)

        if len(outbound_transfers) < MIN_FAN_OUT:
            return result

        # Group by amount
        groups = _group_by_amount(outbound_transfers)
        if not groups:
            return result

        # Pick the largest group
        best_amount = max(groups, key=lambda k: len(groups[k]))
        fan = groups[best_amount]

        result.fan_out_count = len(fan)
        result.fan_out_amount_sol = round(best_amount / 1e9, 4)
        result.funded_wallets = list(set(t["to"] for t in fan))

        # Detect batch size by counting transfers per TX signature
        sig_counts = Counter(t["sig"] for t in fan)
        most_common_batch = sig_counts.most_common(1)[0][1] if sig_counts else 0
        result.fan_out_batch_size = most_common_batch

        # Flags
        result.flags.append(
            f"Fan-out: {result.fan_out_count} wallets funded with ~{result.fan_out_amount_sol} SOL each"
        )

        if most_common_batch == TYPICAL_BATCH_SIZE:
            result.flags.append(
                f"Batch size matches bundler default ({TYPICAL_BATCH_SIZE})"
            )

        # Check if funded wallets bought the token (if mint provided)
        if mint and result.funded_wallets:
            bought_count = 0
            check_sem = asyncio.Semaphore(5)

            async def check_bought(wallet: str) -> bool:
                async with check_sem:
                    try:
                        accounts = await rpc.get_token_accounts_by_owner(wallet, mint)
                        return len(accounts) > 0
                    except Exception:
                        return False

            checks = await asyncio.gather(
                *(check_bought(w) for w in result.funded_wallets[:30])
            )
            bought_count = sum(1 for c in checks if c)
            result.funded_then_bought = bought_count

            if bought_count > 0:
                pct = bought_count / len(result.funded_wallets) * 100
                result.flags.append(
                    f"{bought_count}/{len(result.funded_wallets)} funded wallets hold the token ({pct:.0f}%)"
                )

        # Score calculation
        score = 0.0

        # Base: fan-out count
        if result.fan_out_count >= 20:
            score += 40
        elif result.fan_out_count >= 10:
            score += 30
        elif result.fan_out_count >= 5:
            score += 20
        elif result.fan_out_count >= MIN_FAN_OUT:
            score += 10

        # Batch-size match
        if result.fan_out_batch_size == TYPICAL_BATCH_SIZE:
            score += 15

        # Funded wallets bought the token
        if result.funded_then_bought > 0:
            buy_ratio = result.funded_then_bought / max(len(result.funded_wallets), 1)
            score += min(35, buy_ratio * 45)

        # Amount in typical bundler range (0.1–0.5 SOL)
        if 0.1 <= result.fan_out_amount_sol <= 0.5:
            score += 10

        result.score = min(100.0, round(score, 1))
        return result

    except Exception as e:
        logger.error(f"Funding fan-out analysis failed: {e}")
        return result
