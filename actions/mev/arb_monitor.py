"""Read Uniswap V3 pools on Base."""
from web3 import Web3

WETH = "0x4200000000000000000000000000000000000006"

ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"},
        {"name": "tick", "type": "int24"}
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


class Web3Reader:
    def __init__(self, w3):
        self.w3 = w3

    def get_pool_state(self, pool_addr):
        contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=ABI)
        slot0 = contract.functions.slot0().call()
        liq = contract.functions.liquidity().call()
        t0 = contract.functions.token0().call().lower()
        t1 = contract.functions.token1().call().lower()
        fee = contract.functions.fee().call()
        return {
            "sqrtPriceX96": slot0[0],
            "liquidity": liq,
            "weth_is_token0": (t0 == WETH.lower()),
            "fee": fee
        }
