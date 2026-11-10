import os
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()
w3 = Web3(Web3.HTTPProvider(os.getenv("BASE_HTTP_URL")))

# CHANGE THIS LINE TO TOSHI
DEGEN = Web3.to_checksum_address("0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4")
WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")

UNI_V2_FACTORY = Web3.to_checksum_address(
    "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6")
AERO_FACTORY = Web3.to_checksum_address(
    "0x420DD381b31aEf6683db6B902084cB0FFECe40Da")

# Uniswap V2 Factory ABI
uni_abi = [{"name": "getPair", "type": "function", "inputs": [{"name": "tokenA", "type": "address"}, {
    "name": "tokenB", "type": "address"}], "outputs": [{"name": "pair", "type": "address"}]}]
# Aerodrome Factory ABI
aero_abi = [{"name": "getPool", "type": "function", "inputs": [{"name": "tokenA", "type": "address"}, {
    "name": "tokenB", "type": "address"}, {"name": "stable", "type": "bool"}], "outputs": [{"name": "pool", "type": "address"}]}]

uni = w3.eth.contract(address=UNI_V2_FACTORY, abi=uni_abi)
aero = w3.eth.contract(address=AERO_FACTORY, abi=aero_abi)

print("=== OFFICIAL POOL ADDRESSES ===")
print("Uniswap V2 Pool:", uni.functions.getPair(DEGEN, WETH).call())
print("Aerodrome Pool:  ", aero.functions.getPool(DEGEN, WETH, False).call())
