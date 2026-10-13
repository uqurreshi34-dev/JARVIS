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

    def _build_front_tx(self, router_name: str, token_out: str,
                        amount_in_wei: int, min_out: int,
                        gas_price: int, nonce: int,
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
            "gasPrice": gas_price,
            "nonce": nonce,
            "chainId": self.w3.eth.chain_id,
        }

    def _build_back_tx(self, router_name: str, token_in: str,
                       amount_in: int, min_out: int,
                       gas_price: int, nonce: int,
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
            "gasPrice": gas_price,
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
        nonce_start = await self.nonces.reserve(2)
        try:
            tip = self.w3.to_wei(2, "gwei")
            gas_price = victim_gas_price + tip

            front = self._build_front_tx(
                router_name, base_token, front_in_wei, front_min_out,
                gas_price, nonce_start, deadline, gas,
            )
            back = self._build_back_tx(
                router_name, base_token, back_amount_in, back_min_out,
                gas_price, nonce_start + 1, deadline, gas,
            )
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
