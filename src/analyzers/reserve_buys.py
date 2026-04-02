"""Reserve-aware buy analyzer.

Derived from Anon-Bundler ``pumpfun.ts``:
  - Pump.fun uses a constant-product bonding curve:
      tokens_out = (virtual_token_reserves × sol_in) / (virtual_sol_reserves + sol_in)
  - Initial reserves: 30 SOL virtual / 1,073,000,000,000,000 tokens (6 decimals)
  - The bundler tracks virtual reserves after each buy to calculate the
    next buyer's exact token output
  - Creator buy uses CONFIG.creatorBuySol (default 0.5 SOL)
  - Bundle buys use randomAmount(0.09, 0.48) SOL

Detection strategy:
  Replay the first N buys on a Pump.fun token and check whether the
  actual token amounts received match the mathematically optimal output
  from the bonding curve.  Bots that pre-calculate get within slippage
  tolerance of the exact amount; organic buyers using Pump.fun's UI
  typically overpay or get less favorable fills.

Signals produced:
  - first_buy_sol:         SOL spent on the creator's first buy
  - first_buy_tokens:      tokens received
  - expected_tokens:       what the bonding curve math predicts
  - accuracy_pct:          how close the actual was to expected (100% = perfect)
  - sequential_accuracy:   average accuracy across sequential bundle buys
  - score:                 0-100 sub-score
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.analyzers.rpc import rpc


# Pump.fun bonding curve initial state (from pumpfun.ts)
INITIAL_VIRTUAL_TOKEN_RESERVES = 1_073_000_000_000_000  # ~1.073B tokens (6 decimals)
INITIAL_VIRTUAL_SOL_RESERVES = 30_000_000_000  # 30 SOL in lamports

# Bundler typical creator buy range
BUNDLER_CREATOR_BUY_RANGE = (0.1, 2.0)  # SOL
# Bundler typical bundle buy range (from randomAmount(0.09, 0.48))
BUNDLER_BUNDLE_BUY_RANGE = (0.05, 0.6)  # SOL

# If actual tokens are within this % of expected, it's a bot
BOT_ACCURACY_THRESHOLD = 0.03  # 3% — within typical slippage


def _calculate_tokens_out(
    sol_lamports: int,
    virtual_sol: int = INITIAL_VIRTUAL_SOL_RESERVES,
    virtual_tokens: int = INITIAL_VIRTUAL_TOKEN_RESERVES,
) -> int:
    """Constant-product bonding curve: tokens_out = (vt × si) / (vs + si)."""
    numerator = virtual_tokens * sol_lamports
    denominator = virtual_sol + sol_lamports
    return numerator // denominator


@dataclass
class ReserveAnalysisResult:
    buys_analyzed: int = 0
    first_buy_sol: float = 0.0
    first_buy_tokens: int = 0
    first_buy_expected: int = 0
    first_buy_accuracy_pct: float = 0.0
    sequential_accuracies: list[float] = field(default_factory=list)
    avg_accuracy_pct: float = 0.0
    bot_like_buys: int = 0
    score: float = 0.0
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "buys_analyzed": self.buys_analyzed,
            "first_buy_sol": self.first_buy_sol,
            "first_buy_accuracy_pct": self.first_buy_accuracy_pct,
            "avg_accuracy_pct": self.avg_accuracy_pct,
            "bot_like_buys": self.bot_like_buys,
            "score": self.score,
            "flags": self.flags,
        }


async def analyze_reserve_buys(mint: str, max_buys: int = 10) -> ReserveAnalysisResult:
    """Replay early buys against the bonding curve to detect bot-precision entries.

    Parameters
    ----------
    mint : str
        Token mint address (Pump.fun token).
    max_buys : int
        How many early buys to analyze (first N after creation).
    """
    result = ReserveAnalysisResult()

    try:
        # Get the earliest transactions for this mint
        sigs = await rpc.get_signatures_for_address(mint, limit=30)
        if not sigs:
            return result

        # Reverse to chronological order (oldest first)
        sigs = list(reversed(sigs))

        # Track virtual reserves as we replay buys
        virtual_sol = INITIAL_VIRTUAL_SOL_RESERVES
        virtual_tokens = INITIAL_VIRTUAL_TOKEN_RESERVES
        buys_parsed = 0

        for sig_info in sigs:
            if buys_parsed >= max_buys:
                break

            sig = sig_info.get("signature", "")
            try:
                tx = await rpc.get_transaction(sig)
                if not tx:
                    continue

                meta = tx.get("meta", {})
                if meta.get("err"):
                    continue

                # Look for SOL balance changes (pre/post) to determine buy amount
                msg = tx.get("transaction", {}).get("message", {})
                account_keys = msg.get("accountKeys", [])
                pre_balances = meta.get("preBalances", [])
                post_balances = meta.get("postBalances", [])

                # Look for token balance changes to determine tokens received
                pre_token = meta.get("preTokenBalances", [])
                post_token = meta.get("postTokenBalances", [])

                # Find token balance change for our mint
                tokens_received = 0
                buyer_address = ""
                for ptb in post_token:
                    if ptb.get("mint") == mint:
                        owner = ptb.get("owner", "")
                        post_amount = int(
                            ptb.get("uiTokenAmount", {}).get("amount", "0")
                        )
                        # Find matching pre-balance
                        pre_amount = 0
                        for prb in pre_token:
                            if prb.get("mint") == mint and prb.get("owner") == owner:
                                pre_amount = int(
                                    prb.get("uiTokenAmount", {}).get("amount", "0")
                                )
                                break

                        delta = post_amount - pre_amount
                        if delta > 0:
                            tokens_received = delta
                            buyer_address = owner
                            break

                if tokens_received == 0:
                    continue  # Not a buy TX

                # Find SOL spent by the buyer
                sol_spent = 0
                for i, ak in enumerate(account_keys):
                    key = ak if isinstance(ak, str) else ak.get("pubkey", "")
                    if key == buyer_address:
                        pre = pre_balances[i] if i < len(pre_balances) else 0
                        post = post_balances[i] if i < len(post_balances) else 0
                        spent = pre - post
                        if spent > 0:
                            sol_spent = spent
                        break

                if sol_spent <= 0:
                    continue

                # Calculate expected tokens from current virtual reserves
                expected_tokens = _calculate_tokens_out(
                    sol_spent, virtual_sol, virtual_tokens
                )

                if expected_tokens == 0:
                    continue

                # Calculate accuracy
                accuracy = 1.0 - abs(tokens_received - expected_tokens) / expected_tokens
                accuracy_pct = max(0.0, accuracy * 100)

                result.sequential_accuracies.append(accuracy_pct)
                buys_parsed += 1

                if accuracy >= (1.0 - BOT_ACCURACY_THRESHOLD):
                    result.bot_like_buys += 1

                # Record first buy details
                if buys_parsed == 1:
                    result.first_buy_sol = round(sol_spent / 1e9, 4)
                    result.first_buy_tokens = tokens_received
                    result.first_buy_expected = expected_tokens
                    result.first_buy_accuracy_pct = round(accuracy_pct, 2)

                # Update virtual reserves for next buy
                virtual_sol += sol_spent
                virtual_tokens -= tokens_received

            except Exception as e:
                logger.debug(f"Reserve analyzer: failed to parse {sig[:16]}: {e}")

        result.buys_analyzed = buys_parsed

        if result.sequential_accuracies:
            result.avg_accuracy_pct = round(
                sum(result.sequential_accuracies) / len(result.sequential_accuracies), 2
            )

        # Flags
        if result.bot_like_buys > 0:
            result.flags.append(
                f"{result.bot_like_buys}/{buys_parsed} buys are within "
                f"{BOT_ACCURACY_THRESHOLD*100:.0f}% of bonding curve optimum"
            )

        if result.first_buy_accuracy_pct >= 97:
            result.flags.append(
                f"First buy is {result.first_buy_accuracy_pct:.1f}% accurate "
                f"— likely pre-calculated"
            )

        # Check if first buy is in bundler's typical creator range
        if BUNDLER_CREATOR_BUY_RANGE[0] <= result.first_buy_sol <= BUNDLER_CREATOR_BUY_RANGE[1]:
            result.flags.append(
                f"Creator buy ({result.first_buy_sol} SOL) in typical bundler range"
            )

        if result.avg_accuracy_pct >= 95:
            result.flags.append(
                f"Average buy accuracy {result.avg_accuracy_pct:.1f}% "
                f"— sequential reserve tracking (bot signature)"
            )

        # Score
        score = 0.0

        # Bot-like buy ratio
        if buys_parsed > 0:
            bot_ratio = result.bot_like_buys / buys_parsed
            score += min(40, bot_ratio * 50)

        # Average accuracy
        if result.avg_accuracy_pct >= 98:
            score += 30
        elif result.avg_accuracy_pct >= 95:
            score += 20
        elif result.avg_accuracy_pct >= 90:
            score += 10

        # First buy in bundler range
        if BUNDLER_CREATOR_BUY_RANGE[0] <= result.first_buy_sol <= BUNDLER_CREATOR_BUY_RANGE[1]:
            score += 10

        # Multiple sequential bot-precision buys (strongest signal)
        if result.bot_like_buys >= 3:
            score += 20
        elif result.bot_like_buys >= 2:
            score += 10

        result.score = min(100.0, round(score, 1))
        return result

    except Exception as e:
        logger.error(f"Reserve-aware buy analysis failed: {e}")
        return result
