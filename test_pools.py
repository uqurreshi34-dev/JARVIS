import os
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()
w3 = Web3(Web3.HTTPProvider(os.getenv("BASE_HTTP_URL")))

PAIR_ABI = [
    {"name": "getReserves", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [
        {"name": "_reserve0", "type": "uint112"},
        {"name": "_reserve1", "type": "uint112"},
        {"name": "_blockTimestampLast", "type": "uint32"}
    ]},
    {"name": "token0", "type": "function", "stateMutability": "view",
        "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]


def check_pool(address):
    print(f"\nChecking {address}...")
    code = w3.eth.get_code(address)
    if len(code) == 0:
        print("  -> ERROR: Address is not a contract! It doesn't exist on Base.")
        return

    pair = w3.eth.contract(address=address, abi=PAIR_ABI)
    try:
        r0, r1, _ = pair.functions.getReserves().call()
        print(f"  -> SUCCESS! Reserves: {r0} / {r1}")
        t0 = pair.functions.token0().call()
        print(f"  -> Token0: {t0}")
    except Exception as e:
        print(f"  -> FAILED to read this pool: {e}")


check_pool(Web3.to_checksum_address(
    "0x54D281c7cc029a9Dd71F9ACb7487dd95B1EecF5a"))
check_pool(Web3.to_checksum_address(
    "0x2c4909355b0c036840819484c3a882a95659abf3"))
