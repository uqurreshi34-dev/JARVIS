"""Build and submit Flashbots sandwich bundles via the SandwichExecutor.

Every sandwich is now three txs:
  front  -> SandwichExecutor.frontRun(router, swapData)   [buys token]
  victim -> whatever they sent
  back   -> SandwichExecutor.backRun(router, token, swapData, minProfit)

The executor contract holds the token in between so it can approve the
router for the sell leg. An EOA could not do this.
"""
import os
import time
from typing import Optional
from web3 import Web3
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import to_checksum_address

from actions.mev.nonce_manager import NonceManager

try:
    from flashbots import flashbot
except Exception:
    flashbot = None

WETH = to_checksum_address("0x4200000000000000000000000000000000000006")

UNISWAP_V2_ROUTER = to_checksum_address(
    "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24")
SUSHISWAP_ROUTER = to_checksum_address(
    "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891")
AERODROME_ROUTER = to_checksum_address(
    "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43")
PANCAKESWAP_ROUTER = to_checksum_address(
    "0x8cFe327CEc66d1C090Dd72bd0FF11d690C33a2Eb")
UNISWAP_V3_ROUTER_01 = to_checksum_address(
    "0x2626664c2603336E57B271c5C0b26F421741e481")
UNISWAP_V3_ROUTER_02 = to_checksum_address(
    "0x2626664c2603336E57B271c5C0b26F421741e481")
UNIVERSAL_ROUTER_OLD = to_checksum_address(
    "0x198ef79f1f515f02dfe9e3115ed9fc07183f02fc")
UNIVERSAL_ROUTER_NEW = to_checksum_address(
    "0x198ef79f1f515f02dfe9e3115ed9fc07183f02fc")

ROUTER_BY_NAME = {
    "uniswap_v2": UNISWAP_V2_ROUTER,
    "sushiswap": SUSHISWAP_ROUTER,
    "aerodrome": AERODROME_ROUTER,
    "pancakeswap": PANCAKESWAP_ROUTER,
    "uniswap_v3": UNISWAP_V3_ROUTER_01,
    "uniswap_v3_02": UNISWAP_V3_ROUTER_02,
    "universal_router": UNIVERSAL_ROUTER_NEW,
    "universal_router_old": UNIVERSAL_ROUTER_OLD,
}

V2_ROUTER_ABI = [
    {"name": "swapExactETHForTokens", "type": "function",
     "stateMutability": "payable",
     "inputs": [{"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactTokensForETH", "type": "function",
     "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
]

V3_SINGLE_ABI_01 = [
    {"name": "exactInputSingle", "type": "function",
     "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple",
                 "components": [
                     {"name": "tokenIn", "type": "address"},
                     {"name": "tokenOut", "type": "address"},
                     {"name": "fee", "type": "uint24"},
                     {"name": "recipient", "type": "address"},
                     {"name": "deadline", "type": "uint256"},
                     {"name": "amountIn", "type": "uint256"},
                     {"name": "amountOutMinimum", "type": "uint256"},
                     {"name": "sqrtPriceLimitX96", "type": "uint160"},
                 ]}],
     "outputs": [{"name": "amountOut", "type": "uint256"}]},
]

V3_SINGLE_ABI_02 = [
    {"name": "exactInputSingle", "type": "function",
     "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple",
                 "components": [
                     {"name": "tokenIn", "type": "address"},
                     {"name": "tokenOut", "type": "address"},
                     {"name": "fee", "type": "uint24"},
                     {"name": "recipient", "type": "address"},
                     {"name": "amountIn", "type": "uint256"},
                     {"name": "amountOutMinimum", "type": "uint256"},
                     {"name": "sqrtPriceLimitX96", "type": "uint160"},
                 ]}],
     "outputs": [{"name": "amountOut", "type": "uint256"}]},
]

EXECUTOR_ABI = [
    {"name": "frontRun", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "router", "type": "address"},
                {"name": "swapData", "type": "bytes"}],
     "outputs": []},
    {"name": "backRun", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "router", "type": "address"},
                {"name": "tokenIn", "type": "address"},
                {"name": "swapData", "type": "bytes"},
                {"name": "minProfit", "type": "uint256"}],
     "outputs": []},
]


class BundleExecutor:
    def __init__(self, http_url: str, executor_key: str, relay_key: str,
                 executor_address: str,
                 relay_url: str = "https://relay.flashbots.net",
                 simulation: bool = True):
        self.w3 = Web3(Web3.HTTPProvider(http_url))
        self.executor: LocalAccount = Account.from_key(executor_key)
        self.relay: LocalAccount = Account.from_key(relay_key)
        self.executor_contract = to_checksum_address(executor_address)
        self.simulation = simulation
        self.nonces = NonceManager(self.w3, self.executor.address)
        if not simulation and flashbot is not None:
            flashbot(self.w3, self.relay, relay_url)

    # --- helpers --------------------------------------------------------
    def _sign(self, tx: dict) -> str:
        signed = self.executor.sign_transaction(tx)
        raw = getattr(signed, "rawTransaction",
                      None) or signed["rawTransaction"]
        return raw.hex() if isinstance(raw, bytes) else raw

    def simulate_tx(self, unsigned_tx: dict):
        """eth_call the tx against current state. Not a stateful sim."""
        wallet_bal = self.w3.eth.get_balance(self.executor.address)
        if unsigned_tx.get("value", 0) > wallet_bal:
            return True, "skipped (unfunded wallet)"
        call = {
            "from": self.executor.address,
            "to": unsigned_tx["to"],
            "value": unsigned_tx.get("value", 0),
            "data": unsigned_tx["data"],
            "gas": unsigned_tx.get("gas", 400_000),
        }
        try:
            self.w3.eth.call(call)
            return True, ""
        except Exception as e:
            return False, str(e)[:200]

    # --- calldata builders ---------------------------------------------
    def _v2_swap_data(self, router_addr: str, token_out: str,
                      min_out: int, deadline: int) -> bytes:
        router = self.w3.eth.contract(address=router_addr, abi=V2_ROUTER_ABI)
        path = [WETH, to_checksum_address(token_out)]
        return router.functions.swapExactETHForTokens(
            min_out, path, self.executor_contract, deadline
        ).build_transaction({"value": 0})["data"]

    def _v2_sell_data(self, router_addr: str, token_in: str,
                      amount_in: int, min_out: int,
                      deadline: int) -> bytes:
        router = self.w3.eth.contract(address=router_addr, abi=V2_ROUTER_ABI)
        path = [to_checksum_address(token_in), WETH]
        return router.functions.swapExactTokensForETH(
            amount_in, min_out, path, self.executor_contract, deadline
        ).build_transaction({})["data"]

    def _v3_swap_data(self, router_name: str, router_addr: str,
                      token_out: str, fee: int,
                      amount_in: int, min_out: int,
                      deadline: int) -> bytes:
        abi = V3_SINGLE_ABI_02
        params = (WETH, to_checksum_address(token_out), fee,
                  self.executor_contract, amount_in, min_out, 0)
        router = self.w3.eth.contract(address=router_addr, abi=abi)
        return router.functions.exactInputSingle(params).build_transaction(
            {"value": 0})["data"]

    def _v3_sell_data(self, router_name: str, router_addr: str,
                      token_in: str, fee: int,
                      amount_in: int, min_out: int,
                      deadline: int) -> bytes:
        abi = V3_SINGLE_ABI_02
        params = (to_checksum_address(token_in), WETH, fee,
                  self.executor_contract, amount_in, min_out, 0)
        router = self.w3.eth.contract(address=router_addr, abi=abi)
        return router.functions.exactInputSingle(params).build_transaction(
            {"value": 0})["data"]

    # --- fee logic ------------------------------------------------------
    def _fee_bid(self, victim_gas_price: int):
        latest = self.w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", self.w3.to_wei(0.01, "gwei"))
        victim_tip = max(victim_gas_price - base_fee, 0)
        try:
            suggested_tip = int(self.w3.eth.max_priority_fee)
        except Exception:
            suggested_tip = base_fee // 10

        # On Base, priority fees are tiny. Bid 1.5x the victim's effective
        # tip, floor to 0.001 gwei (still meaningful on Base), cap at
        # 0.1 gwei (expensive but never catastrophic).
        base_bid = max(victim_tip, suggested_tip)
        our_tip = int(base_bid * 1.5)
        min_tip = self.w3.to_wei(0.001, "gwei")
        max_tip = self.w3.to_wei(0.1, "gwei")
        our_tip = max(min_tip, min(our_tip, max_tip))
        max_fee = base_fee * 2 + our_tip
        return base_fee, our_tip, max_fee

    # --- build & submit -------------------------------------------------
    async def build_bundle(self, *,
                           router_name: str,
                           base_token: str,
                           front_in_wei: int,
                           front_min_out: int,
                           back_amount_in: int,
                           back_min_out: int,
                           victim_raw_tx: str,
                           victim_gas_price: int,
                           fee_tier: int = 0,
                           min_profit_wei: int = 0,
                           deadline: int,
                           gas: int = 400_000):
        base_fee, our_tip, max_fee = self._fee_bid(victim_gas_price)
        print(f"[tip] base_fee={base_fee} our_tip={our_tip} max_fee={max_fee}")

        router_addr = ROUTER_BY_NAME[router_name]
        is_v3 = router_name in ("uniswap_v3", "uniswap_v3_02")

        if is_v3:
            swap_data = self._v3_swap_data(
                router_name, router_addr, base_token, fee_tier,
                front_in_wei, front_min_out, deadline)
            sell_data = self._v3_sell_data(
                router_name, router_addr, base_token, fee_tier,
                back_amount_in, back_min_out, deadline)
        else:
            swap_data = self._v2_swap_data(
                router_addr, base_token, front_min_out, deadline)
            sell_data = self._v2_sell_data(
                router_addr, base_token, back_amount_in,
                back_min_out, deadline)

        nonce_start = await self.nonces.reserve(2)
        try:
            contract = self.w3.eth.contract(
                address=self.executor_contract, abi=EXECUTOR_ABI)

            front_data = contract.functions.frontRun(
                router_addr, swap_data).build_transaction({"value": 0})["data"]
            back_data = contract.functions.backRun(
                router_addr, to_checksum_address(base_token),
                sell_data, min_profit_wei
            ).build_transaction({})["data"]

            front_tx = {
                "to": self.executor_contract,
                "value": front_in_wei,
                "data": front_data,
                "gas": gas,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": our_tip,
                "type": 2,
                "nonce": nonce_start,
                "chainId": self.w3.eth.chain_id,
            }
            back_tx = {
                "to": self.executor_contract,
                "value": 0,
                "data": back_data,
                "gas": gas,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": our_tip,
                "type": 2,
                "nonce": nonce_start + 1,
                "chainId": self.w3.eth.chain_id,
            }

            ok, reason = self.simulate_tx(front_tx)
            if not ok:
                raise RuntimeError(f"front would revert: {reason}")
            ok2, reason2 = self.simulate_tx(back_tx)
            if not ok2:
                raise RuntimeError(f"back would revert: {reason2}")

            bundle = [
                {"signed_transaction": self._sign(front_tx)},
                {"signed_transaction": victim_raw_tx},
                {"signed_transaction": self._sign(back_tx)},
            ]
            target = self.w3.eth.block_number + 1
            return bundle, target, nonce_start, front_tx, back_tx
        except Exception:
            await self.nonces.release(nonce_start, 2)
            raise

    def submit(self, bundle, target_block: int):
        if self.simulation:
            return {"simulated": True, "target": target_block}
        # Base has no Flashbots. We broadcast front then back as regular
        # txs. The victim is already in the mempool; the sequencer orders
        # by gas price. Front is sent first so it lands before the victim.
        front_raw = bundle[0]["signed_transaction"]
        back_raw = bundle[2]["signed_transaction"]
        try:
            h_front = self.w3.eth.send_raw_transaction(front_raw)
            h_back = self.w3.eth.send_raw_transaction(back_raw)
            return {"front": h_front.hex(), "back": h_back.hex()}
        except Exception as e:
            return {"error": str(e)[:200]}

    # mainnet
    # def submit(self, bundle, target_block: int):
    #     if self.simulation or flashbot is None:
    #         return {"simulated": True, "target": target_block}
    #     return self.w3.flashbots.send_bundle(
    #         bundle, target_block_number=target_block)
