"""Cross-DEX arbitrage scanner for Uniswap V3 on Base. Multiple coins."""
import os
import sys
import time
from dotenv import load_dotenv
from web3 import Web3
from eth_utils import to_checksum_address
from actions.mev.arb_monitor import Web3Reader
from actions.mev.arb_math_v3 import optimal_arb_amount_v3

load_dotenv()

# Define your targets here! You can add as many as you want.
TARGETS = {
    "TOSHI": {
        # Make sure these are real V3 pools
        "pool_a": to_checksum_address("0x4b0Aaf3EBb163dd45F663b38b6d93f6093EBC2d3"),
        "pool_b": to_checksum_address("0x5aa4AD647580bfE86258d300Bc9852F4434E2c61"),
    },
    "DEGEN": {
        # Real V3 DEGEN pools on Base
        "pool_a": to_checksum_address("0xc9034c3E7F58003E6ae0C8438e7c8f4598d5ACAA"),
        "pool_b": to_checksum_address("0x0cA6485b7e9cF814A3Fd09d81672B07323535b64"),
    }
}


def say(text):
    print(f"[ARB] {text}", flush=True)


def main():
    load_dotenv()
    rpc_url = os.getenv("BASE_HTTP_URL")
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))

    if not w3.is_connected():
        say("ERROR: could not reach RPC.")
        return 1

    reader = Web3Reader(w3)

    # Identify all pools first
    say("Identifying V3 pools...")
    active_targets = {}
    for name, data in TARGETS.items():
        try:
            state_a = reader.get_pool_state(data["pool_a"])
            state_b = reader.get_pool_state(data["pool_b"])
            active_targets[name] = data
            say(
                f"Loaded {name}: Pool A Liq {state_a['liquidity']}, Pool B Liq {state_b['liquidity']}")
        except Exception as e:
            say(f"FAILED to load {name}: {e}")

    if not active_targets:
        say("No valid targets. Exiting.")
        return 2

    say(f"Watching {len(active_targets)} coins for opportunities...")
    max_in = Web3.to_wei(1.0, "ether")  # Max 1 WETH per trade

    while True:
        try:
            for name, data in active_targets.items():
                # Read live states
                a = reader.get_pool_state(data["pool_a"])
                b = reader.get_pool_state(data["pool_b"])

                # Calculate real profit
                best_amt, profit = optimal_arb_amount_v3(max_in, a, b)

                if profit > 0:
                    say(f"*** OPPORTUNITY on {name} ***")
                    say(f"Size {best_amt / 1e18:.4f} WETH, profit {profit / 1e18:.6f} WETH")
                else:
                    # Calculate the gap for display
                    price_a = (a["sqrtPriceX96"] / 2**96) ** 2
                    price_b = (b["sqrtPriceX96"] / 2**96) ** 2
                    gap = abs(price_a - price_b) / min(price_a, price_b) * 100

                    # Only print if the gap is above 0.1% so we don't spam the screen
                    if gap > 0.1:
                        say(f"{name} | Gap: {gap:.3f}% -> too small")

            # Wait 10 seconds before scanning the whole list again
            time.sleep(10)

        except KeyboardInterrupt:
            return 0
        except Exception as e:
            say(f"Error during scan: {e}")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
