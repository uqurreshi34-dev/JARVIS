# Decodes Uniswap/Sushi V2 router calldata so we know what the victim is trading.

"""Decode pending Uniswap V2 / SushiSwap V2 router transactions."""
from dataclasses import dataclass
from typing import Optional, List
from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address

# Router addresses (checksummed)
UNISWAP_V2_ROUTER = to_checksum_address(
    "0x4752ba5DBC251Ed37A0A4C9F979A0525FdA0e489")
SUSHISWAP_ROUTER = to_checksum_address(
    "0x6BDED42c0d1e1C3E1F14Dc78cE49b8a65D9E0bD8")  # Sushi on Base
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


def _to_int(v) -> int:
    """Accept int, hex-str ('0x…'), decimal-str, bytes, or None."""
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, bytes):
        return int.from_bytes(v, "big")
    if isinstance(v, str):
        return int(v, 16) if v.startswith("0x") else int(v)
    return int(v)


def _effective_gas_price(tx: dict) -> int:
    """Works for both legacy (gasPrice) and EIP-1559 (maxFeePerGas) txs,
    and both formats (int from HTTP, hex string from WebSocket)."""
    if tx.get("gasPrice"):
        return _to_int(tx["gasPrice"])
    max_fee = tx.get("maxFeePerGas")
    if max_fee:
        return _to_int(max_fee)
    return 0


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
        amount_in = _to_int(tx.get("value", 0))

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
        amount_in=_to_int(amount_in),
        amount_out_min=_to_int(amount_out_min),
        recipient=to_checksum_address(recipient),
        is_buy=is_buy,
        base_token=base_token,
        gas_price=_effective_gas_price(tx),
        gas=_to_int(tx.get("gas")),
        tx_hash=tx["hash"].hex() if isinstance(
            tx.get("hash"), bytes) else str(tx.get("hash")),
    )
