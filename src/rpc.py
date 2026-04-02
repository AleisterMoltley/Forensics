"""Main RPC module — re-exports the Helius RPC singleton.

This module exists so that ``main.py`` can import ``from src.rpc import rpc``
while the analyzers continue to use ``from src.analyzers.rpc import rpc``.
Both references point to the same singleton instance.
"""
from src.analyzers.rpc import rpc, HeliusRPC

__all__ = ["rpc", "HeliusRPC"]
