# Decodes Uniswap/Sushi V2 router calldata so we know what the victim is trading.

"""Decode pending Uniswap V2 / SushiSwap V2 router transactions."""
from dataclasses import dataclass
from typing import Optional, List
from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address

# Router addresses (checksummed)
UNISWAP_V2_ROUTER = to_checksum_address(
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
SUSHISWAP_ROUTER = to_checksum_address(
    "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F")
ROUTERS = {UNISWAP_V2_ROUTER: "uniswap_v2", SUSHISWAP_ROUTER: "sushiswap"}

# Function selectors → (name, abi_types)
SELECTORS = {
    "0x38ed1739": ("swapExactTokensForTokens", ["uint256", "uint256", "address[]", "address", "uint256"]),
    "0x8803dbee": ("swapTokensForExactTokens", ["uint256", "uint256", "address[]", "address", "uint256"]),
    "0x7ff36ab5": ("swapExactETHForTokens",    ["uint256", "address[]", "address", "uint256"]),
    "0xfb3bdb41": ("swapETHForExactTokens",    ["uint256", "address[]", "address", "uint256"]),
    "0x18cbafe5": ("swapExactTokensForETH",    ["uint256", "uint256", "address[]", "address", "uint256"]),
    "0x4a25d94a": ("swapTokensForExactETH",    ["uint256", "uint256", "address[]", "address", "uint256"]),
}

WETH = to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")


@dataclass
class DecodedSwap:
    router: str
    function: str
    path: List[str]
    amount_in: int
    amount_out_min: int
    recipient: str
    # True if victim is buying the base token (pushing price up)
    is_buy: bool
    base_token: str       # the token being bought/sold (non-WETH)
    gas_price: int
    gas: int
    tx_hash: str


def decode_swap(tx: dict) -> Optional[DecodedSwap]:
    """Return DecodedSwap if tx is a V2 router swap we care about."""
    to = tx.get("to")
    if to is None:
        return None
    to = to_checksum_address(to)
    if to not in ROUTERS:
        return None

    data = tx.get("input") or tx.get("data")
    if not data or len(data) < 10:
        return None
    data = data.hex() if isinstance(data, bytes) else data
    selector = data[:10]
    if selector not in SELECTORS:
        return None

    name, types = SELECTORS[selector]
    raw = bytes.fromhex(data[10:])
    try:
        decoded = abi_decode(types, raw)
    except Exception:
        return None

    # Normalize depending on signature
    if name in ("swapExactTokensForTokens", "swapTokensForExactTokens",
                "swapExactTokensForETH", "swapTokensForExactETH"):
        amount_in, amount_out_min, path, recipient, _deadline = decoded
    else:  # ETH-in variants
        amount_out_min, path, recipient, _deadline = decoded
        amount_in = tx.get("value", 0)

    path = [to_checksum_address(a) for a in path]

    # Determine direction
    if path[0] == WETH:
        is_buy = True
        base_token = path[-1]
    elif path[-1] == WETH:
        is_buy = False
        base_token = path[0]
    else:
        # Token→Token; treat as buy of path[-1]
        is_buy = True
        base_token = path[-1]

    return DecodedSwap(
        router=ROUTERS[to],
        function=name,
        path=path,
        amount_in=int(amount_in),
        amount_out_min=int(amount_out_min),
        recipient=to_checksum_address(recipient),
        is_buy=is_buy,
        base_token=base_token,
        gas_price=int(tx.get("gasPrice") or 0),
        gas=int(tx.get("gas") or 0),
        tx_hash=tx["hash"].hex() if isinstance(
            tx.get("hash"), bytes) else str(tx.get("hash")),
    )
