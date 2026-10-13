# Orchestrator: listens to mempool, decides, bundles, submits.

"""JARVIS MEV sandwich orchestrator.

Run:  python -m actions.mev.main
"""
import os
import asyncio
import time
from dotenv import load_dotenv
from web3 import Web3
from eth_utils import to_checksum_address

from actions.mev.tx_decoder import DecodedSwap
from actions.mev.sandwich_math import optimal_front_run, get_amount_out
from actions.mev.mempool_listener import MempoolListener
from actions.mev.bundle_executor import BundleExecutor
from actions.mev.safety import SafetyGuard

load_dotenv()

WETH = to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")

# Minimal V2 pair ABI
PAIR_ABI = [
    {"name": "getReserves", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "_reserve0", "type": "uint112"},
                               {"name": "_reserve1", "type": "uint112"},
                               {"name": "_blockTimestampLast", "type": "uint32"}]},
    {"name": "token0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]

FACTORY_ABI = [
    {"name": "getPair", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "address"}],
     "outputs": [{"name": "", "type": "address"}]},
]

UNI_FACTORY = to_checksum_address("0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f")
SUSHI_FACTORY = to_checksum_address(
    "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac")
FACTORIES = {"uniswap_v2": UNI_FACTORY, "sushiswap": SUSHI_FACTORY}


class Orchestrator:
    def __init__(self):
        self.http = Web3(Web3.HTTPProvider(os.getenv("ETHEREUM_HTTP_URL")))
        self.executor = BundleExecutor(
            http_url=os.getenv("ETHEREUM_HTTP_URL"),
            executor_key=os.getenv("EXECUTOR_PRIVATE_KEY"),
            relay_key=os.getenv("FLASHBOTS_RELAY_SIGNING_KEY"),
            relay_url=os.getenv("FLASHBOTS_RELAY",
                                "https://relay.flashbots.net"),
            simulation=os.getenv("SIMULATION_MODE", "true").lower() == "true",
        )
        self.min_profit = int(os.getenv("MIN_PROFIT_WEI", "50000000000000"))
        self.executor_addr = self.executor.executor.address
        # Cache: pair address per (factory, token0, token1) — never changes.
        self._pair_cache: dict = {}
        # Cache: reserves per pair with a short TTL — changes every block.
        self._reserves_cache: dict = {}   # pair_addr -> (r_in, r_out, ts)
        self._reserves_ttl = 3.0          # seconds
        self._on_swap_sem = asyncio.Semaphore(2)
        self.safety = SafetyGuard(
            max_bundle_eth=float(os.getenv("MEV_MAX_BUNDLE_ETH", "0.5")),
            max_daily_loss_eth=float(
                os.getenv("MEV_MAX_DAILY_LOSS_ETH", "0.1")),
            max_consec_failures=int(os.getenv("MEV_MAX_CONSEC_FAILURES", "5")),
            min_wallet_balance_eth=float(
                os.getenv("MEV_MIN_WALLET_BALANCE_ETH", "0.05")),
            enabled=os.getenv("MEV_ENABLED", "true").lower() == "true",
        )

    async def _resync_after(self, target_block: int, timeout: int = 40):
        """Wait for target_block to pass, then resync the nonce manager.

        This is the safety net: whatever happened to our bundle, the chain
        is the source of truth. Resyncing here prevents the local counter
        from drifting out of sync with on-chain state.
        """
        start = time.time()
        while self.http.eth.block_number <= target_block:
            if time.time() - start > timeout:
                break
            await asyncio.sleep(2)
        n = await self.executor.nonces.resync()
        print(f"[nonce] resynced to {n} after target block {target_block}")
        print(f"[safety] {self.safety.status()}")

    def _pair_for(self, factory_name: str, token_a: str, token_b: str) -> str:
        key = (factory_name, token_a.lower(), token_b.lower())
        cached = self._pair_cache.get(key)
        if cached is not None:
            return cached
        f = self.http.eth.contract(
            address=FACTORIES[factory_name], abi=FACTORY_ABI)
        addr = f.functions.getPair(token_a, token_b).call()
        self._pair_cache[key] = addr
        return addr

    def _reserves(self, pair_addr: str, token_in: str):
        now = time.time()
        cached = self._reserves_cache.get(pair_addr)
        if cached is not None:
            r_in, r_out, ts = cached
            if now - ts < self._reserves_ttl:
                return r_in, r_out
        pair = self.http.eth.contract(address=pair_addr, abi=PAIR_ABI)
        r0, r1, _ = pair.functions.getReserves().call()
        t0 = pair.functions.token0().call()
        if to_checksum_address(t0) == token_in:
            r_in, r_out = r0, r1
        else:
            r_in, r_out = r1, r0
        self._reserves_cache[pair_addr] = (r_in, r_out, now)
        return r_in, r_out

    async def on_swap(self, swap: DecodedSwap):
        async with self._on_swap_sem:
            await self._on_swap_inner(swap)

    async def _on_swap_inner(self, swap: DecodedSwap):
        # We only target buys of a token with WETH as input (price-impact up)
        if not swap.is_buy or swap.path[0] != WETH:
            return
        # Skip tiny trades
        if swap.amount_in < self.http.to_wei(0.1, "ether"):
            return

        pair = self._pair_for(swap.router, WETH, swap.base_token)
        if int(pair, 16) == 0:
            return
        r_in, r_out = self._reserves(pair, WETH)
        if r_in == 0 or r_out == 0:
            return

        # How much ETH do we have to play with?
        wallet_bal = self.http.eth.get_balance(self.executor_addr)
        gas_reserve = self.http.to_wei(0.02, "ether")  # keep gas buffer
        if wallet_bal <= gas_reserve:
            return
        max_front = int((wallet_bal - gas_reserve) * 0.5)

        # Safety gate — before we spend any more effort
        allow, reason = self.safety.check_before_bundle(max_front, wallet_bal)
        if not allow:
            print(f"[safety] blocked: {reason}")
            return

        front_in, profit = optimal_front_run(
            victim_in=swap.amount_in,
            victim_min_out=swap.amount_out_min,
            reserve_in=r_in,
            reserve_out=r_out,
            max_front_in=max_front,
        )

        # Cost of gas: rough estimate, 2 txs
        gas_price = swap.gas_price + self.http.to_wei(2, "gwei")
        gas_cost = gas_price * 500_000  # ~500k for both txs
        net_profit = profit - gas_cost

        print(f"[cand] {swap.base_token[:8]} victim_in={swap.amount_in} "
              f"front={front_in} gross={profit} net={net_profit}")

        if net_profit < self.min_profit:
            return

        # Build bundle
        our_out = get_amount_out(front_in, r_in, r_out)
        # Back-run min_out: don't accept less than front_in (break-even gross)
        back_min_out = front_in  # accept any profit
        deadline = int(time.time()) + 60

        try:
            bundle, target, nonce_start, front, back = await self.executor.build_bundle(
                router_name=swap.router,
                base_token=swap.base_token,
                front_in_wei=front_in,
                front_min_out=int(our_out * 0.98),
                back_amount_in=our_out,
                back_min_out=back_min_out,
                victim_raw_tx=self.http.eth.get_raw_transaction(
                    swap.tx_hash).hex()
                if isinstance(self.http.eth.get_raw_transaction(swap.tx_hash), bytes)
                else self.http.eth.get_raw_transaction(swap.tx_hash).to_0x_hex()
                if hasattr(self.http.eth.get_raw_transaction(swap.tx_hash), "to_0x_hex")
                else self.http.eth.get_raw_transaction(swap.tx_hash),
                victim_gas_price=swap.gas_price,
                deadline=deadline,
            )
        except Exception as e:
            print(f"[bundle] build failed: {e}")
            return

        print(f"[bundle] submitting target={target} nonce={nonce_start} "
              f"net_profit={net_profit}")

        try:
            resp = self.executor.submit(bundle, target)
            print(f"[bundle] result: {resp}")
            self.safety.record_result(net_profit, success=True)
        except Exception as e:
            print(f"[bundle] submit failed: {e}")
            await self.executor.nonces.release(nonce_start, 2)
            self.safety.record_result(0, success=False)
            return

        # Non-blocking: resync once the target block has passed, whether
        # the bundle landed or not.
        asyncio.create_task(self._resync_after(target))

    async def run(self):
        print(f"[orchestrator] executor={self.executor_addr}")
        print(f"[orchestrator] simulation={self.executor.simulation}")
        listener = MempoolListener(
            ws_url=os.getenv("ETHEREUM_WS_URL"),
            http_url=os.getenv("ETHEREUM_HTTP_URL"),
            on_swap=self.on_swap,
        )
        await listener.run()


if __name__ == "__main__":
    asyncio.run(Orchestrator().run())
