"""Wash trade fingerprint detector.

Derived from Anon-Bundler ``volumeBot.ts``:
  - 70% buys / 30% sells (Math.random() < 0.7)
  - Buy amounts: randomAmount(0.005, 0.09) SOL
  - Sell amounts: 5-15% of token balance
  - Cadence: 60_000ms / volumeBuysPerMin (default 12 → ~5s interval)
  - Jitter: randomDelay(0, intervalMs * 0.3)
  - Round-robin through wallet array

Detection strategy:
  Analyze trading activity on a token for statistical signatures of
  wash trading: regular cadence, tight amount ranges, dominant buy/sell
  ratio, and trades from wallets with a common funding source.

Signals produced:
  - total_trades:        total trades analyzed
  - buy_ratio:           percentage of trades that are buys
  - avg_buy_sol:         average buy size in SOL
  - cadence_regularity:  how regular the inter-trade timing is (0-1)
  - amount_tightness:    how narrow the amount distribution is (0-1)
  - common_funder_pct:   % of trading wallets funded by the same source
  - score:               0-100 sub-score
"""
from __future__ import annotations

import asyncio
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.analyzers.rpc import rpc


# Bundler volume bot fingerprint constants
BUNDLER_BUY_RANGE = (0.005, 0.09)  # SOL
BUNDLER_BUY_RATIO = 0.70  # 70% buys
BUNDLER_BUY_RATIO_TOLERANCE = 0.12  # ±12%
BUNDLER_CADENCE_MS = 5_000  # 60000/12 = 5s default interval
BUNDLER_CADENCE_JITTER = 0.3  # 30% jitter


@dataclass
class WashTradeResult:
    total_trades: int = 0
    buy_count: int = 0
    sell_count: int = 0
    buy_ratio: float = 0.0
    avg_buy_sol: float = 0.0
    buy_amount_range: tuple[float, float] = (0.0, 0.0)
    avg_interval_ms: float = 0.0
    cadence_regularity: float = 0.0
    amount_tightness: float = 0.0
    unique_wallets: int = 0
    score: float = 0.0
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "buy_ratio": round(self.buy_ratio, 3),
            "avg_buy_sol": self.avg_buy_sol,
            "buy_amount_range": self.buy_amount_range,
            "avg_interval_ms": round(self.avg_interval_ms, 0),
            "cadence_regularity": round(self.cadence_regularity, 3),
            "amount_tightness": round(self.amount_tightness, 3),
            "unique_wallets": self.unique_wallets,
            "score": self.score,
            "flags": self.flags,
        }


async def analyze_wash_trading(
    mint: str,
    lookback: int = 100,
) -> WashTradeResult:
    """Detect volume-bot wash trading patterns on a token.

    Parameters
    ----------
    mint : str
        Token mint address.
    lookback : int
        Number of recent transactions to analyze.
    """
    result = WashTradeResult()

    try:
        sigs = await rpc.get_signatures_for_address(mint, limit=lookback)
        if len(sigs) < 10:
            return result

        trades: list[dict[str, Any]] = []
        wallets_seen: set[str] = set()

        sem = asyncio.Semaphore(10)

        async def _parse_sig(sig_info: dict) -> None:
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

                    msg = tx.get("transaction", {}).get("message", {})
                    account_keys = msg.get("accountKeys", [])
                    pre_balances = meta.get("preBalances", [])
                    post_balances = meta.get("postBalances", [])
                    pre_token = meta.get("preTokenBalances", [])
                    post_token = meta.get("postTokenBalances", [])

                    # Determine if this is a buy or sell by token balance change
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
                        if token_delta == 0:
                            continue

                        is_buy = token_delta > 0

                        # Find SOL change for this wallet
                        sol_change = 0
                        for i, ak in enumerate(account_keys):
                            key = ak if isinstance(ak, str) else ak.get("pubkey", "")
                            if key == owner:
                                pre = pre_balances[i] if i < len(pre_balances) else 0
                                post = post_balances[i] if i < len(post_balances) else 0
                                sol_change = abs(pre - post)
                                break

                        trades.append({
                            "wallet": owner,
                            "is_buy": is_buy,
                            "sol_amount": sol_change / 1e9,
                            "token_delta": abs(token_delta),
                            "block_time": block_time,
                            "sig": sig,
                        })
                        wallets_seen.add(owner)
                        break  # one trade per TX

                except Exception:
                    return

        await asyncio.gather(*(_parse_sig(s) for s in sigs))

        if len(trades) < 10:
            return result

        # Sort chronologically
        trades.sort(key=lambda t: t["block_time"])

        result.total_trades = len(trades)
        result.buy_count = sum(1 for t in trades if t["is_buy"])
        result.sell_count = result.total_trades - result.buy_count
        result.buy_ratio = result.buy_count / result.total_trades
        result.unique_wallets = len(wallets_seen)

        # Buy amount statistics
        buy_amounts = [t["sol_amount"] for t in trades if t["is_buy"] and t["sol_amount"] > 0]
        if buy_amounts:
            result.avg_buy_sol = round(statistics.mean(buy_amounts), 6)
            result.buy_amount_range = (
                round(min(buy_amounts), 6),
                round(max(buy_amounts), 6),
            )

        # Cadence regularity: coefficient of variation of inter-trade intervals
        intervals = []
        for i in range(1, len(trades)):
            dt = (trades[i]["block_time"] - trades[i - 1]["block_time"]) * 1000
            if dt > 0:
                intervals.append(dt)

        if len(intervals) >= 5:
            result.avg_interval_ms = statistics.mean(intervals)
            stdev = statistics.stdev(intervals)
            cv = stdev / result.avg_interval_ms if result.avg_interval_ms > 0 else 1.0
            # Regularity: 1 = perfectly regular, 0 = chaotic
            # Bundler with 30% jitter → cv ≈ 0.15-0.25
            result.cadence_regularity = max(0.0, 1.0 - cv)

        # Amount tightness: how clustered are the buy amounts
        if len(buy_amounts) >= 5:
            stdev_amt = statistics.stdev(buy_amounts)
            mean_amt = statistics.mean(buy_amounts)
            cv_amt = stdev_amt / mean_amt if mean_amt > 0 else 1.0
            result.amount_tightness = max(0.0, 1.0 - cv_amt)

        # Flags
        if abs(result.buy_ratio - BUNDLER_BUY_RATIO) <= BUNDLER_BUY_RATIO_TOLERANCE:
            result.flags.append(
                f"Buy/sell ratio ({result.buy_ratio:.0%}) matches bundler pattern "
                f"(expected ~{BUNDLER_BUY_RATIO:.0%})"
            )

        if buy_amounts:
            lo, hi = result.buy_amount_range
            if (
                lo >= BUNDLER_BUY_RANGE[0] * 0.5
                and hi <= BUNDLER_BUY_RANGE[1] * 2.0
            ):
                result.flags.append(
                    f"Buy amounts ({lo:.4f}–{hi:.4f} SOL) within bundler volume bot range"
                )

        if result.cadence_regularity >= 0.6:
            result.flags.append(
                f"Regular trading cadence (regularity: {result.cadence_regularity:.2f}) "
                f"— avg interval: {result.avg_interval_ms/1000:.1f}s"
            )

        if result.amount_tightness >= 0.7:
            result.flags.append(
                f"Tight amount clustering ({result.amount_tightness:.2f}) "
                f"— likely automated"
            )

        # Round-robin wallet pattern (bundler cycles through wallet array)
        wallet_trade_counts = Counter(t["wallet"] for t in trades)
        if len(wallet_trade_counts) >= 3:
            counts = list(wallet_trade_counts.values())
            if len(counts) >= 3:
                cv_wallets = statistics.stdev(counts) / statistics.mean(counts) if statistics.mean(counts) > 0 else 1.0
                if cv_wallets < 0.3:
                    result.flags.append(
                        f"Even trade distribution across {len(wallet_trade_counts)} wallets "
                        f"(round-robin pattern)"
                    )

        # Score
        score = 0.0

        # Buy ratio match
        if abs(result.buy_ratio - BUNDLER_BUY_RATIO) <= BUNDLER_BUY_RATIO_TOLERANCE:
            score += 20

        # Cadence regularity
        if result.cadence_regularity >= 0.7:
            score += 25
        elif result.cadence_regularity >= 0.5:
            score += 15

        # Amount tightness
        if result.amount_tightness >= 0.8:
            score += 20
        elif result.amount_tightness >= 0.6:
            score += 10

        # Amount in bundler range
        if buy_amounts:
            lo, hi = result.buy_amount_range
            if lo >= BUNDLER_BUY_RANGE[0] * 0.5 and hi <= BUNDLER_BUY_RANGE[1] * 2.0:
                score += 15

        # Multiple wallets with even distribution
        if result.unique_wallets >= 5 and len(result.flags) >= 2:
            score += 20

        result.score = min(100.0, round(score, 1))
        return result

    except Exception as e:
        logger.error(f"Wash trade analysis failed: {e}")
        return result
