"""One-time script: compile and deploy SandwichExecutor to Base."""
import os
import json
from solcx import compile_standard, install_solc
from web3 import Web3
from eth_account import Account
from dotenv import load_dotenv

load_dotenv()

# 1. Install and set the Solidity compiler version
print("Installing Solidity compiler 0.8.20...")
install_solc("0.8.20")

# 2. Read the contract source
contract_path = os.path.join(os.path.dirname(__file__), "SandwichExecutor.sol")
with open(contract_path, "r") as f:
    source = f.read()

# 3. Compile it
print("Compiling contract...")
compiled = compile_standard({
    "language": "Solidity",
    "sources": {"SandwichExecutor.sol": {"content": source}},
    "settings": {
        "outputSelection": {
            "*": {"*": ["abi", "evm.bytecode.object"]}
        }
    },
}, solc_version="0.8.20")

bytecode = compiled["contracts"]["SandwichExecutor.sol"]["SandwichExecutor"]["evm"]["bytecode"]["object"]
abi = compiled["contracts"]["SandwichExecutor.sol"]["SandwichExecutor"]["abi"]

# 4. Connect to Base
rpc_url = os.getenv("BASE_HTTP_URL")
private_key = os.getenv("EXECUTOR_PRIVATE_KEY")

w3 = Web3(Web3.HTTPProvider(rpc_url))
account = Account.from_key(private_key)

print(f"Deploying from: {account.address}")
balance = w3.eth.get_balance(account.address)
print(f"Balance: {w3.from_wei(balance, 'ether')} ETH")

if balance == 0:
    print("ERROR: No ETH on Base. Cannot deploy.")
    exit(1)

# 5. Build the deployment transaction
SandwichExecutor = w3.eth.contract(abi=abi, bytecode=bytecode)

nonce = w3.eth.get_transaction_count(account.address, "pending")
gas_price = w3.eth.gas_price

tx = SandwichExecutor.constructor().build_transaction({
    "from": account.address,
    "nonce": nonce,
    "gasPrice": gas_price,
    "chainId": w3.eth.chain_id,
})

# 6. Sign and send
signed = account.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
print(f"Deployment tx sent: {tx_hash.hex()}")
print("Waiting for confirmation...")

receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
print(f"\n--- SUCCESS ---")
print(f"Contract deployed at: {receipt.contractAddress}")
print(f"Copy this address into your .env as SANDWICH_EXECUTOR_ADDRESS")
