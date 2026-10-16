"""JARVIS MEV sandwich orchestrator.

Run:  python -m actions.mev.main
"""
import os
import asyncio
import time
from dotenv import load_dotenv
from web3 import Web3
from eth_utils import to_checksum_address

from actions.mev.sandwich_math import optimal_front_run, get_amount_out
from actions.mev.v3_math import optimal_front_run_v3, get_amount_out_v3
from actions.mev.mempool_listener import MempoolListener
from actions.mev.bundle_executor import BundleExecutor
from actions.mev.safety import SafetyGuard
from actions.mev.v3_pool import V3PoolReader

load_dotenv()

WETH = to_checksum_address("0x4200000000000000000000000000000000000006")
Q96 = 2 ** 96

PAIR_ABI = [
    {"name": "getReserves", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [
         {"name": "_reserve0", "type": "uint112"},
         {"name": "_reserve1", "type": "uint112"},
         {"name": "_blockTimestampLast", "type": "uint32"}]},
    {"name": "token0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]

FACTORY_ABI = [
    {"name": "getPair", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"},
                {"name": "", "type": "address"}],
     "outputs": [{"name": "", "type": "address"}]},
]

# Base factories. Same _pair_for lookup works for all the Uniswap V2
# forks except Aerodrome, which uses getPool(a, b, stable) instead of
# getPair(a, b) — that one is handled separately below.
UNI_FACTORY = to_checksum_address(
    "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6")
SUSHI_FACTORY = to_checksum_address(
    "0x71524B4f93c58fcbF659783284E38825f0622859")
AERODROME_FACTORY = to_checksum_address(
    "0x420DD381b31aEf6683db6B902084cB0FFECe40Da")
PANCAKESWAP_FACTORY = to_checksum_address(
    "0x02a84c1b3BBD7401a5f7fa98a384EBC70bB5749E")

FACTORIES = {
    "uniswap_v2": UNI_FACTORY,
    "sushiswap": SUSHI_FACTORY,
    "aerodrome": AERODROME_FACTORY,
    "pancakeswap": PANCAKESWAP_FACTORY,
}

# Aerodrome's factory has a different signature.
AERO_FACTORY_ABI = [
    {"name": "getPool", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenA", "type": "address"},
                {"name": "tokenB", "type": "address"},
                {"name": "stable", "type": "bool"}],
     "outputs": [{"name": "pool", "type": "address"}]},
]


class Orchestrator:
    def __init__(self):
        self.http = Web3(Web3.HTTPProvider(os.getenv("BASE_HTTP_URL")))
        self.executor = BundleExecutor(
            http_url=os.getenv("BASE_HTTP_URL"),
            executor_key=os.getenv("EXECUTOR_PRIVATE_KEY"),
            relay_key=os.getenv("FLASHBOTS_RELAY_SIGNING_KEY"),
            executor_address=os.getenv("SANDWICH_EXECUTOR_ADDRESS"),
            relay_url=os.getenv("FLASHBOTS_RELAY",
                                "https://relay.flashbots.net"),
            simulation=os.getenv("SIMULATION_MODE", "true").lower() == "true",
        )
        self.min_profit = int(os.getenv("MIN_PROFIT_WEI", "50000000000000"))
        self.executor_addr = self.executor.executor.address
        self.v3 = V3PoolReader(self.http)
        self._pair_cache = {}
        self._reserves_cache = {}
        self._reserves_ttl = 3.0
        self._on_swap_sem = asyncio.Semaphore(2)
        self.safety = SafetyGuard(
            max_bundle_eth=float(os.getenv("MEV_MAX_BUNDLE_ETH", "0.005")),
            max_daily_loss_eth=float(
                os.getenv("MEV_MAX_DAILY_LOSS_ETH", "0.1")),
            max_consec_failures=int(os.getenv("MEV_MAX_CONSEC_FAILURES", "5")),
            min_wallet_balance_eth=float(
                os.getenv("MEV_MIN_WALLET_BALANCE_ETH", "0.001")),
            enabled=os.getenv("MEV_ENABLED", "true").lower() == "true",
        )

    # --- outcome tracking ----------------------------------------------
    async def _resync_after(self, target_block, outcome, timeout=40):
        start = time.time()
        while self.http.eth.block_number <= target_block:
            if time.time() - start > timeout:
                break
            await asyncio.sleep(2)
        after = self.http.eth.get_balance(self.executor_addr)
        delta = after - outcome["balance_before"]
        expected = outcome["expected_profit"]
        if delta > 0:
            print(f"[outcome] WIN target={target_block} "
                  f"profit={self.http.from_wei(delta, 'ether'):.6f} ETH "
                  f"(expected ~{self.http.from_wei(expected, 'ether'):.6f})")
            self.safety.record_result(delta, success=True)
        elif delta < 0:
            print(f"[outcome] LOSS target={target_block} "
                  f"lost={self.http.from_wei(-delta, 'ether'):.6f} ETH")
            self.safety.record_result(delta, success=False)
        else:
            print(f"[outcome] MISS target={target_block} (not included)")
        n = await self.executor.nonces.resync()
        print(f"[nonce] resynced to {n}")
        print(f"[safety] {self.safety.status()}")

    # --- V2 pool state --------------------------------------------------
    def _pair_for(self, factory_name, token_a, token_b):
        key = (factory_name, token_a.lower(), token_b.lower())
        if key in self._pair_cache:
            return self._pair_cache[key]

        if factory_name == "aerodrome":
            # Aerodrome uses getPool(a, b, stable). Try volatile first
            # (that's what we sandwich), fall back to stable.
            f = self.http.eth.contract(
                address=FACTORIES[factory_name], abi=AERO_FACTORY_ABI)
            addr = "0x" + "0" * 40
            try:
                addr = f.functions.getPool(token_a, token_b, False).call()
            except Exception:
                pass
            if int(addr, 16) == 0:
                try:
                    addr = f.functions.getPool(token_a, token_b, True).call()
                except Exception:
                    addr = "0x" + "0" * 40
        else:
            f = self.http.eth.contract(
                address=FACTORIES[factory_name], abi=FACTORY_ABI)
            addr = f.functions.getPair(token_a, token_b).call()

        self._pair_cache[key] = addr
        return addr

    def _reserves(self, pair_addr, token_in):
        now = time.time()
        cached = self._reserves_cache.get(pair_addr)
        if cached and now - cached[2] < self._reserves_ttl:
            return cached[0], cached[1]
        pair = self.http.eth.contract(address=pair_addr, abi=PAIR_ABI)
        r0, r1, _ = pair.functions.getReserves().call()
        t0 = pair.functions.token0().call()
        if to_checksum_address(t0) == token_in:
            r_in, r_out = r0, r1
        else:
            r_in, r_out = r1, r0
        self._reserves_cache[pair_addr] = (r_in, r_out, now)
        return r_in, r_out

    # --- shared pre-checks ---------------------------------------------
    def _wallet_cap(self):
        wallet_bal = self.http.eth.get_balance(self.executor_addr)
        gas_reserve = self.http.to_wei(0.0005, "ether")
        if wallet_bal <= gas_reserve:
            return 0, wallet_bal
        return int((wallet_bal - gas_reserve) * 0.5), wallet_bal

    async def on_swap(self, swap):
        async with self._on_swap_sem:
            try:
                await self._on_swap_inner(swap)
            except Exception as e:
                print(f"[cand] error: {type(e).__name__}: {e}")

    # --- main candidate path -------------------------------------------
    async def _on_swap_inner(self, swap):
        if not swap.is_buy:
            return
        # UR V2_SWAP_EXACT_IN arrives as DecodedSwapUR with function name.
        router = swap.router
        function = getattr(swap, "function", "")
        is_v3 = router in (
            "uniswap_v3", "uniswap_v3_02") or function.startswith("V3_")
        is_ur = router == "universal_router"

        if swap.amount_in < self.http.to_wei(0.001, "ether"):
            return

        max_front, wallet_bal = self._wallet_cap()
        if max_front == 0:
            return
        allow, reason = self.safety.check_before_bundle(max_front, wallet_bal)
        if not allow:
            print(f"[safety] blocked: {reason}")
            return

        if is_v3 or (is_ur and function.startswith("V3_")):
            await self._handle_v3(swap, max_front)
        elif is_ur and function.startswith("V2_"):
            await self._handle_v2(swap, max_front, ur=True)
        else:
            await self._handle_v2(swap, max_front, ur=False)

    async def _handle_v2(self, swap, max_front, ur):
        # UR V2 swaps don't name the underlying DEX, so default to Uniswap.
        # Direct router swaps already carry the correct name.
        factory = "uniswap_v2" if ur else swap.router
        pair = self._pair_for(factory, WETH, swap.base_token)
        if int(pair, 16) == 0:
            return
        r_in, r_out = self._reserves(pair, WETH)
        if r_in == 0 or r_out == 0:
            return

        front_in, profit = optimal_front_run(
            victim_in=swap.amount_in,
            victim_min_out=swap.amount_out_min,
            reserve_in=r_in,
            reserve_out=r_out,
            max_front_in=max_front,
        )
        if front_in == 0:
            return

        # Base gas is fractions of a gwei, not whole gweis. The old
        # "+2 gwei" assumed mainnet and overestimated the bundle's
        # gas cost by ~100x, which made every candidate look unprofitable.
        latest = self.http.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", self.http.to_wei(0.01, "gwei"))
        # generous on Base, still tiny
        our_tip = self.http.to_wei(0.1, "gwei")
        gas_price = base_fee + our_tip
        gas_cost = gas_price * 800_000             # 800k = both txs together
        net_profit = profit - gas_cost
        print(f"[cand] V2 {swap.base_token[:8]} victim_in={swap.amount_in} "
              f"front={front_in} gross={profit} net={net_profit}")
        if net_profit < self.min_profit:
            return

        our_out = get_amount_out(front_in, r_in, r_out)
        deadline = int(time.time()) + 60
        await self._build_and_submit(
            router_name=factory,
            swap=swap,
            front_in=front_in,
            front_min_out=int(our_out * 0.98),
            back_amount_in=our_out,
            back_min_out=front_in,
            fee_tier=0,
            net_profit=net_profit,
            deadline=deadline,
        )

    async def _handle_v3(self, swap, max_front):
        pool_addr = self.v3.pool_address(
            WETH, swap.base_token, swap.fee)
        if pool_addr is None:
            return
        state = self.v3.pool_state(pool_addr)
        if state is None:
            return
        weth_is_token0 = (state["token0"] == WETH)

        front_in, profit = optimal_front_run_v3(
            victim_in=swap.amount_in,
            victim_min_out=swap.amount_out_min,
            sqrt_price_x96=state["sqrt_price_x96"],
            liquidity=state["liquidity"],
            fee=state["fee"],
            weth_is_token0=weth_is_token0,
            max_front_in=max_front,
        )
        if front_in == 0:
            return

        # Base gas is fractions of a gwei, not whole gweis. The old
        # "+2 gwei" assumed mainnet and overestimated the bundle's
        # gas cost by ~100x, which made every candidate look unprofitable.
        latest = self.http.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", self.http.to_wei(0.01, "gwei"))
        # generous on Base, still tiny
        our_tip = self.http.to_wei(0.1, "gwei")
        gas_price = base_fee + our_tip
        gas_cost = gas_price * 800_000             # 800k = both txs together
        net_profit = profit - gas_cost
        print(f"[cand] V3 {swap.base_token[:8]} fee={state['fee']} "
              f"victim_in={swap.amount_in} front={front_in} "
              f"gross={profit} net={net_profit}")
        if net_profit < self.min_profit:
            return

        our_tokens, _ = get_amount_out_v3(
            front_in, state["sqrt_price_x96"], state["liquidity"],
            state["fee"], weth_is_token0)
        deadline = int(time.time()) + 60
        router_name = swap.router if swap.router in (
            "uniswap_v3", "uniswap_v3_02") else "uniswap_v3_02"
        await self._build_and_submit(
            router_name=router_name,
            swap=swap,
            front_in=front_in,
            front_min_out=int(our_tokens * 0.98),
            back_amount_in=our_tokens,
            back_min_out=front_in,
            fee_tier=state["fee"],
            net_profit=net_profit,
            deadline=deadline,
        )

    async def _build_and_submit(self, *, router_name, swap, front_in,
                                front_min_out, back_amount_in, back_min_out,
                                fee_tier, net_profit, deadline):
        try:
            raw = self.http.eth.get_raw_transaction(swap.tx_hash)
            victim_raw = raw.hex() if isinstance(raw, bytes) else raw
            if not victim_raw.startswith("0x"):
                victim_raw = "0x" + victim_raw
        except Exception as e:
            print(f"[bundle] cannot fetch victim raw: {e}")
            return

        try:
            bundle, target, nonce_start, _f, _b = await self.executor.build_bundle(
                router_name=router_name,
                base_token=swap.base_token,
                front_in_wei=front_in,
                front_min_out=front_min_out,
                back_amount_in=back_amount_in,
                back_min_out=back_min_out,
                victim_raw_tx=victim_raw,
                victim_gas_price=swap.gas_price,
                fee_tier=fee_tier,
                min_profit_wei=1,
                deadline=deadline,
            )
        except Exception as e:
            print(f"[bundle] build failed: {e}")
            return

        print(f"[bundle] submitting target={target} nonce={nonce_start} "
              f"net_profit={net_profit}")
        balance_before = self.http.eth.get_balance(self.executor_addr)

        try:
            resp = self.executor.submit(bundle, target)
            print(f"[bundle] result: {resp}")
        except Exception as e:
            print(f"[bundle] submit failed: {e}")
            await self.executor.nonces.release(nonce_start, 2)
            self.safety.record_result(0, success=False)
            return

        outcome = {
            "nonce_start": nonce_start,
            "balance_before": balance_before,
            "expected_profit": net_profit,
        }
        asyncio.create_task(self._resync_after(target, outcome))

    async def run(self):
        print(f"[orchestrator] executor={self.executor_addr}")
        print(f"[orchestrator] contract={self.executor.executor_contract}")
        print(f"[orchestrator] simulation={self.executor.simulation}")
        listener = MempoolListener(
            ws_url=os.getenv("BASE_WS_URL"),
            http_url=os.getenv("BASE_HTTP_URL"),
            on_swap=self.on_swap,
        )
        await listener.run()


if __name__ == "__main__":
    asyncio.run(Orchestrator().run())
