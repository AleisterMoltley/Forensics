"""Recovery sweep detector.

Derived from Anon-Bundler ``recover.ts``:
  - After a rug or exit, the operator sweeps SOL from all buyer wallets
    back to the master wallet
  - Transfer amount = wallet_balance - 5000 lamports (fee reservation)
  - Processes wallets sequentially with 500ms delay
  - Skips wallets with balance ≤ rent-exempt minimum (890,880 lamports)

Detection strategy:
  Given a set of wallets suspected to be bundled buyers (from other
  detectors), check whether they all sent their remaining SOL to a
  single destination address shortly after trading stopped.  The
  ``balance - 5000`` pattern is a strong fingerprint.

This detector is designed to run AFTER the other analyzers have
identified suspected bundle wallets, OR as part of the post-rug
tracker to identify sweep patterns retroactively.

Signals produced:
  - sweep_detected:     whether a sweep pattern was found
  - sweep_destination:  the address that received all the SOL
  - swept_wallets:      how many wallets were swept
  - sweep_total_sol:    total SOL recovered
  - fee_pattern:        whether the balance-5000 pattern was detected
  - sweep_window_s:     time span of all sweep TXs
  - score:              0-100 sub-score
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.analyzers.rpc import rpc


# Bundler recover.ts constants
RECOVER_FEE_LAMPORTS = 5000
RECOVER_FEE_TOLERANCE = 1000  # ±1000 lamports
MIN_RENT_EXEMPT = 890_880
RECOVER_DELAY_MS = 500  # 500ms between sweeps


@dataclass
class SweepResult:
    sweep_detected: bool = False
    sweep_destination: str = ""
    swept_wallets: int = 0
    sweep_total_sol: float = 0.0
    fee_pattern_count: int = 0
    sweep_window_s: float = 0.0
    score: float = 0.0
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sweep_detected": self.sweep_detected,
            "sweep_destination": self.sweep_destination,
            "swept_wallets": self.swept_wallets,
            "sweep_total_sol": round(self.sweep_total_sol, 4),
            "fee_pattern_count": self.fee_pattern_count,
            "sweep_window_s": round(self.sweep_window_s, 1),
            "score": self.score,
            "flags": self.flags,
        }


async def analyze_recovery_sweep(
    wallets: list[str],
    deployer: str | None = None,
) -> SweepResult:
    """Detect bundler-style SOL recovery sweep from a list of wallets.

    Parameters
    ----------
    wallets : list[str]
        List of suspected bundle buyer wallet addresses.
    deployer : str, optional
        Expected sweep destination (deployer/master wallet).
        If not provided, the detector infers it from the data.
    """
    result = SweepResult()

    if len(wallets) < 2:
        return result

    try:
        # For each wallet, find its most recent outbound SOL transfer
        sweep_txs: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(8)

        async def check_wallet(wallet: str) -> None:
            async with sem:
                try:
                    sigs = await rpc.get_signatures_for_address(wallet, limit=10)
                    if not sigs:
                        return

                    # Check recent TXs for an outbound SOL transfer (sweep)
                    for sig_info in sigs[:5]:
                        sig = sig_info.get("signature", "")
                        block_time = sig_info.get("blockTime", 0)

                        tx = await rpc.get_transaction(sig)
                        if not tx:
                            continue

                        meta = tx.get("meta", {})
                        if meta.get("err"):
                            continue

                        msg = tx.get("transaction", {}).get("message", {})
                        account_keys = msg.get("accountKeys", [])
                        pre_balances = meta.get("preBalances", [])
                        post_balances = meta.get("postBalances", [])

                        keys_list = [
                            (ak if isinstance(ak, str) else ak.get("pubkey", ""))
                            for ak in account_keys
                        ]

                        # Find the wallet's index
                        wallet_idx = None
                        for i, k in enumerate(keys_list):
                            if k == wallet:
                                wallet_idx = i
                                break

                        if wallet_idx is None:
                            continue

                        pre = pre_balances[wallet_idx] if wallet_idx < len(pre_balances) else 0
                        post = post_balances[wallet_idx] if wallet_idx < len(post_balances) else 0
                        sent = pre - post

                        if sent <= MIN_RENT_EXEMPT:
                            continue

                        # Find who received the SOL
                        for i, k in enumerate(keys_list):
                            if i == wallet_idx:
                                continue
                            r_pre = pre_balances[i] if i < len(pre_balances) else 0
                            r_post = post_balances[i] if i < len(post_balances) else 0
                            received = r_post - r_pre

                            if received > MIN_RENT_EXEMPT:
                                # Check for the balance-5000 pattern
                                # Bundler's recover.ts sends (balance - 5000 lamports)
                                fee_match = abs(sent - (pre - RECOVER_FEE_LAMPORTS)) <= RECOVER_FEE_TOLERANCE

                                sweep_txs.append({
                                    "from": wallet,
                                    "to": k,
                                    "lamports": sent,
                                    "block_time": block_time,
                                    "fee_pattern": fee_match,
                                    "pre_balance": pre,
                                    "sig": sig,
                                })
                                return  # found the sweep TX for this wallet

                except Exception as e:
                    logger.debug(f"Sweep check failed for {wallet[:12]}: {e}")

        await asyncio.gather(*(check_wallet(w) for w in wallets))

        if len(sweep_txs) < 2:
            return result

        # Find the most common destination (the master wallet)
        destinations = Counter(t["to"] for t in sweep_txs)
        most_common_dest, dest_count = destinations.most_common(1)[0]

        # If deployer is provided, check if it matches
        if deployer and most_common_dest != deployer:
            # Check if deployer is in the destinations at all
            if deployer in destinations:
                most_common_dest = deployer
                dest_count = destinations[deployer]

        # Filter to sweeps going to the detected master
        master_sweeps = [t for t in sweep_txs if t["to"] == most_common_dest]

        if len(master_sweeps) < 2:
            return result

        result.sweep_detected = True
        result.sweep_destination = most_common_dest
        result.swept_wallets = len(master_sweeps)
        result.sweep_total_sol = round(
            sum(t["lamports"] for t in master_sweeps) / 1e9, 4
        )
        result.fee_pattern_count = sum(1 for t in master_sweeps if t["fee_pattern"])

        # Calculate sweep window
        times = sorted(t["block_time"] for t in master_sweeps if t["block_time"])
        if len(times) >= 2:
            result.sweep_window_s = times[-1] - times[0]

        # Flags
        result.flags.append(
            f"{result.swept_wallets} wallets swept {result.sweep_total_sol} SOL "
            f"to {most_common_dest[:12]}..."
        )

        if result.fee_pattern_count > 0:
            result.flags.append(
                f"{result.fee_pattern_count}/{result.swept_wallets} transfers use "
                f"balance-{RECOVER_FEE_LAMPORTS} pattern (bundler recover.ts signature)"
            )

        if result.sweep_window_s > 0:
            avg_delay = result.sweep_window_s / max(result.swept_wallets - 1, 1)
            result.flags.append(
                f"Sweep window: {result.sweep_window_s:.0f}s "
                f"(avg {avg_delay:.1f}s between sweeps)"
            )
            # Bundler uses 500ms delay → very fast sweeps
            if avg_delay <= 3.0:
                result.flags.append("Rapid sequential sweeps (automated)")

        if deployer and most_common_dest == deployer:
            result.flags.append("Sweep destination matches the token deployer")

        # Score
        score = 0.0

        # Sweep to single destination
        if result.swept_wallets >= 5:
            score += 25
        elif result.swept_wallets >= 3:
            score += 15
        elif result.swept_wallets >= 2:
            score += 10

        # Fee pattern match (balance - 5000)
        if result.fee_pattern_count > 0:
            fee_ratio = result.fee_pattern_count / result.swept_wallets
            score += min(30, fee_ratio * 35)

        # Destination = deployer
        if deployer and most_common_dest == deployer:
            score += 20

        # Rapid sweeps (automated)
        if result.sweep_window_s > 0:
            avg_delay = result.sweep_window_s / max(result.swept_wallets - 1, 1)
            if avg_delay <= 3.0:
                score += 15
            elif avg_delay <= 10.0:
                score += 8

        # High sweep ratio (most wallets swept)
        if len(wallets) > 0:
            sweep_ratio = result.swept_wallets / len(wallets)
            score += min(10, sweep_ratio * 15)

        result.score = min(100.0, round(score, 1))
        return result

    except Exception as e:
        logger.error(f"Recovery sweep analysis failed: {e}")
        return result
