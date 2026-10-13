# Builds and submits Flashbots bundles.

"""Build & submit Flashbots sandwich bundles."""
import os
import time
from typing import Optional, List
from web3 import Web3
from eth_account import Account
from eth_account.signers.local import LocalAccount
from flashbots import flashbot
from eth_utils import to_checksum_address
from actions.mev.nonce_manager import NonceManager

UNISWAP_V2_ROUTER = to_checksum_address(
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
SUSHISWAP_ROUTER = to_checksum_address(
    "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F")
ROUTER_BY_NAME = {"uniswap_v2": UNISWAP_V2_ROUTER,
                  "sushiswap": SUSHISWAP_ROUTER}

# Minimal router ABI for the two functions we use
ROUTER_ABI = [
    {"name": "swapExactETHForTokens", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "amountOutMin", "type": "uint256"}, {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactTokensForETH", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"}, {
                    "name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
]


class BundleExecutor:
    def __init__(self, http_url: str, executor_key: str, relay_key: str,
                 relay_url: str = "https://relay.flashbots.net",
                 simulation: bool = True):
        self.w3 = Web3(Web3.HTTPProvider(http_url))
        self.executor: LocalAccount = Account.from_key(executor_key)
        self.relay: LocalAccount = Account.from_key(relay_key)
        self.simulation = simulation
        self.nonces = NonceManager(self.w3, self.executor.address)
        if not simulation:
            flashbot(self.w3, self.relay, relay_url)

    def _sign(self, tx: dict) -> str:
        signed = self.executor.sign_transaction(tx)
        # web3.py v6 returns an object with .rawTransaction
        raw = getattr(signed, "rawTransaction",
                      None) or signed["rawTransaction"]
        return raw.hex() if isinstance(raw, bytes) else raw

    def simulate_tx(self, unsigned_tx: dict) -> tuple[bool, str]:
        """Pretend-run a transaction against current chain state.

        Uses eth_call: no gas, no broadcast, no signature. Returns
        (True, "") on success, (False, reason) on revert.
        If we don't have the ETH to even pay for it, skip the check.
        """
        wallet_bal = self.w3.eth.get_balance(self.executor.address)
        if unsigned_tx.get("value", 0) > wallet_bal:
            return True, "skipped (unfunded wallet)"

        call = {
            "from": self.executor.address,
            "to": unsigned_tx["to"],
            "value": unsigned_tx.get("value", 0),
            "data": unsigned_tx["data"],
            "gas": unsigned_tx.get("gas", 250_000),
        }
        try:
            self.w3.eth.call(call)
            return True, ""
        except Exception as e:
            return False, str(e)[:200]

    def _build_front_tx(self, router_name: str, token_out: str,
                        amount_in_wei: int, min_out: int,
                        max_fee: int, tip: int, nonce: int,
                        deadline: int, gas: int = 250_000) -> dict:
        router = self.w3.eth.contract(
            address=ROUTER_BY_NAME[router_name], abi=ROUTER_ABI)
        path = [to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
                to_checksum_address(token_out)]
        data = router.functions.swapExactETHForTokens(
            min_out, path, self.executor.address, deadline
        ).build_transaction({"value": 0})["data"]
        return {
            "to": ROUTER_BY_NAME[router_name],
            "value": amount_in_wei,
            "data": data,
            "gas": gas,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": tip,
            "type": 2,
            "nonce": nonce,
            "chainId": self.w3.eth.chain_id,
        }

    def _build_back_tx(self, router_name: str, token_in: str,
                       amount_in: int, min_out: int,
                       max_fee: int, tip: int, nonce: int,
                       deadline: int, gas: int = 250_000) -> dict:
        router = self.w3.eth.contract(
            address=ROUTER_BY_NAME[router_name], abi=ROUTER_ABI)
        path = [to_checksum_address(token_in),
                to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")]
        data = router.functions.swapExactTokensForETH(
            amount_in, min_out, path, self.executor.address, deadline
        ).build_transaction({})["data"]
        return {
            "to": ROUTER_BY_NAME[router_name],
            "value": 0,
            "data": data,
            "gas": gas,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": tip,
            "type": 2,
            "nonce": nonce,
            "chainId": self.w3.eth.chain_id,
        }

    async def build_bundle(self, *,
                           router_name: str,
                           base_token: str,
                           front_in_wei: int,
                           front_min_out: int,
                           back_amount_in: int,
                           back_min_out: int,
                           victim_raw_tx: str,
                           victim_gas_price: int,
                           deadline: int,
                           gas: int = 250_000):
        """Returns (bundle_list, target_block, nonce_start, front, back)."""
        # Fetch current base fee
        latest = self.w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", self.w3.to_wei(20, "gwei"))
        nonce_start = await self.nonces.reserve(2)
        try:
            # --- Dynamic tip calculation ---
            # 1) What effective tip is the victim paying?
            #    (approximation: works for both legacy and 1559 txs)
            victim_tip = max(victim_gas_price - base_fee, 0)

            # 2) Ask the node what a competitive tip looks like right now.
            #    This is the industry-standard source (same call used by
            #    Alchemy, Infura, ethers.js's getFeeData).
            try:
                suggested_tip = int(self.w3.eth.max_priority_fee)
            except Exception:
                suggested_tip = self.w3.to_wei(1, "gwei")

            # 3) Our bid = whoever is higher (victim or market), scaled up.
            # Tune the multiplier once you can observe real bundle outcomes.
            base_bid = max(victim_tip, suggested_tip)
            our_tip = base_bid * 2

            # 4) Floor and cap
            min_tip = self.w3.to_wei(1, "gwei")
            max_tip = self.w3.to_wei(50, "gwei")
            our_tip = max(min_tip, min(our_tip, max_tip))

            # 5) maxFee = base_fee with 2x headroom + tip
            #    (base fee can rise ~12.5% per block; 2x is safe)
            max_fee = base_fee * 2 + our_tip

            # 6) Enforce the two EIP-1559 invariants
            assert max_fee >= base_fee + our_tip, "max_fee too low"
            assert our_tip <= max_fee, "tip > max_fee"

            # Log the fee bid so we can see what we're paying
            print(f"[tip] base_fee={base_fee} victim_tip={victim_tip} "
                  f"suggested={suggested_tip} our_tip={our_tip} "
                  f"max_fee={max_fee}")

            front = self._build_front_tx(
                router_name, base_token, front_in_wei, front_min_out,
                max_fee, our_tip, nonce_start, deadline, gas,
            )
            ok, reason = self.simulate_tx(front)
            if not ok:
                raise RuntimeError(f"front-run would revert: {reason}")
            back = self._build_back_tx(
                router_name, base_token, back_amount_in, back_min_out,
                max_fee, our_tip, nonce_start + 1, deadline, gas,
            )
            ok2, reason2 = self.simulate_tx(back)
            if not ok2:
                raise RuntimeError(
                    f"back-run would revert: {reason2}")
            bundle = [
                {"signed_transaction": self._sign(front)},
                {"signed_transaction": victim_raw_tx},
                {"signed_transaction": self._sign(back)},
            ]
            target = self.w3.eth.block_number + 1
            return bundle, target, nonce_start, front, back
        except Exception:
            # Roll back the reservation so subsequent bundles aren't broken
            await self.nonces.release(nonce_start, 2)
            raise

    def submit(self, bundle, target_block: int):
        if self.simulation:
            # Simulate only — no relay call
            return {"simulated": True, "target": target_block}
        return self.w3.flashbots.send_bundle(bundle, target_block_number=target_block)
