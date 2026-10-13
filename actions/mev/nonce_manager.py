"""Serialised nonce allocator for the executor wallet.

- Reserve `count` consecutive nonces; increments atomically under a lock.
- Release nonces if a bundle never broadcast (rollback on build failure).
- Resync from chain after a target block passes, whether the bundle
  landed or not — the chain is always the source of truth.
"""
import asyncio
from typing import Optional

from web3 import Web3


class NonceManager:
    def __init__(self, w3: Web3, address: str):
        self._w3 = w3
        self._address = Web3.to_checksum_address(address)
        self._lock = asyncio.Lock()
        self._next: Optional[int] = None

    async def _sync_from_chain(self) -> int:
        # 'pending' includes txs already in the mempool, so we don't
        # collide with a still-unconfirmed tx we ourselves sent.
        return await asyncio.to_thread(
            self._w3.eth.get_transaction_count, self._address, "pending"
        )

    async def peek(self) -> int:
        async with self._lock:
            if self._next is None:
                self._next = await self._sync_from_chain()
            return self._next

    async def reserve(self, count: int = 1) -> int:
        """Reserve `count` consecutive nonces and return the first one."""
        if count < 1:
            raise ValueError("count must be >= 1")
        async with self._lock:
            if self._next is None:
                self._next = await self._sync_from_chain()
            start = self._next
            self._next += count
            return start

    async def release(self, start: int, count: int) -> None:
        """Return nonces reserved but never broadcast.

        Only reclaims when the range sits at the tail of the allocator;
        releasing a middle range would create a gap.
        """
        async with self._lock:
            if self._next is None:
                return
            if start + count == self._next:
                self._next = start

    async def resync(self) -> int:
        """Force a fresh read from the chain. Call after every target block."""
        async with self._lock:
            self._next = await self._sync_from_chain()
            return self._next
