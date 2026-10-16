"""Read state from a Uniswap V3 pool.

V3 pools are looked up by (tokenA, tokenB, fee) through the V3 factory.
A pool's state is:
  sqrtPriceX96  — current price, as sqrt(price) * 2^96
  tick          — current tick (log_1.0001(price))
  liquidity     — in-range virtual liquidity
  fee           — the pool's fee tier in hundredths of a bip

We cache pool addresses forever (they never change) but query state
freshly, since it moves every block.
"""
import time
from typing import Optional
from web3 import Web3
from eth_utils import to_checksum_address


V3_FACTORY = to_checksum_address("0x33128a8fC17869897dcE68Ed026d694621f6FDfD")

FACTORY_ABI = [
    {"name": "getPool", "type": "function", "stateMutability": "view",
     "inputs": [
         {"name": "tokenA", "type": "address"},
         {"name": "tokenB", "type": "address"},
         {"name": "fee", "type": "uint24"},
     ],
     "outputs": [{"name": "pool", "type": "address"}]},
]

POOL_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [
         {"name": "sqrtPriceX96", "type": "uint160"},
         {"name": "tick", "type": "int24"},
         {"name": "observationIndex", "type": "uint16"},
         {"name": "observationCardinality", "type": "uint16"},
         {"name": "observationCardinalityNext", "type": "uint16"},
         {"name": "feeProtocol", "type": "uint8"},
         {"name": "unlocked", "type": "bool"},
     ]},
    {"name": "liquidity", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint128"}]},
    {"name": "token0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "fee", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint24"}]},
]


class V3PoolReader:
    """Reads and caches V3 pool state."""

    def __init__(self, w3: Web3):
        self.w3 = w3
        self._factory = w3.eth.contract(address=V3_FACTORY, abi=FACTORY_ABI)
        # Pool address per (tokenA_lower, tokenB_lower, fee). Never changes.
        self._pool_cache: dict = {}

    def pool_address(self, token_a: str, token_b: str, fee: int) -> Optional[str]:
        """Resolve (and cache) the pool for a (tokenA, tokenB, fee) trio."""
        a = to_checksum_address(token_a)
        b = to_checksum_address(token_b)
        key = (a.lower(), b.lower(), int(fee))
        cached = self._pool_cache.get(key)
        if cached is not None:
            return cached
        try:
            pool = self._factory.functions.getPool(a, b, int(fee)).call()
        except Exception:
            return None
        if pool == "0x0000000000000000000000000000000000000000":
            return None
        self._pool_cache[key] = pool
        return pool

    def pool_state(self, pool_addr: str) -> Optional[dict]:
        """Fetch current state of a pool.

        Returns a dict with:
          sqrt_price_x96  int
          tick            int
          liquidity       int
          token0          checksummed address
          token1          checksummed address
          fee             int
        Or None on failure.
        """
        try:
            pool = self.w3.eth.contract(address=pool_addr, abi=POOL_ABI)
            slot0 = pool.functions.slot0().call()
            liquidity = pool.functions.liquidity().call()
            token0 = pool.functions.token0().call()
            token1 = pool.functions.token1().call()
            fee = pool.functions.fee().call()
        except Exception:
            return None

        return {
            "sqrt_price_x96": int(slot0[0]),
            "tick": int(slot0[1]),
            "liquidity": int(liquidity),
            "token0": to_checksum_address(token0),
            "token1": to_checksum_address(token1),
            "fee": int(fee),
        }
