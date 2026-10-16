"""Decode pending Uniswap V3 router swaps.

Two V3 routers on mainnet, with slightly different struct ABIs:

  SwapRouter    0xE592427A0AEce92De3Edee1F18E0157C05861564
                structs include a `deadline` field

  SwapRouter02  0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45
                structs have no deadline (dropped in the newer router)

We handle single-hop swaps first since that's most pending V3 traffic,
and decode multi-hop's first hop only — enough to know whether the
trade is a WETH-in buy worth sandwiching.
"""
from dataclasses import dataclass
from typing import Optional
from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address

from actions.mev.tx_decoder import _to_int, _effective_gas_price


SWAP_ROUTER_01 = to_checksum_address(
    "0x2626664c2603336E57B271c5C0b26F421741e481")  # Uniswap V3 Router on Base
SWAP_ROUTER_02 = to_checksum_address(
    "0x2626664c2603336E57B271c5C0b26F421741e481")  # Use the same one for both

# For the mempool subscription's toAddress filter.
ROUTER_ADDRS_V3 = [SWAP_ROUTER_01.lower(), SWAP_ROUTER_02.lower()]

WETH = to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")


# --- SwapRouter (original) ----------------------------------------------
_SELECTORS_SR01 = {
    "0x414bf389": "exactInputSingle",
    "0xc04b8d59": "exactInput",
    "0xdb3e2198": "exactOutputSingle",
    "0xf28c0498": "exactOutput",
}
_SR01_SINGLE = "(address,address,uint24,address,uint256,uint256,uint256,uint160)"
_SR01_MULTI = "(bytes,address,uint256,uint256,uint256)"


# --- SwapRouter02 (no deadline) -----------------------------------------
_SELECTORS_SR02 = {
    "0x04e45aaf": "exactInputSingle",
    "0xb858183f": "exactInput",
    "0x5023b4df": "exactOutputSingle",
    "0x09b81346": "exactOutput",
}
_SR02_SINGLE = "(address,address,uint24,address,uint256,uint256,uint160)"
_SR02_MULTI = "(bytes,address,uint256,uint256)"


@dataclass
class DecodedSwapV3:
    router: str           # "uniswap_v3" | "uniswap_v3_02"
    function: str
    token_in: str
    token_out: str
    fee: int
    recipient: str
    amount_in: int
    amount_out_min: int
    is_buy: bool
    base_token: str
    gas_price: int
    gas: int
    tx_hash: str


def _first_hop_from_path(path_bytes: bytes):
    """(token_in, fee, token_out) from the first hop of a packed V3 path.

    Layout: [20-byte token][3-byte fee][20-byte token] repeated.
    """
    if len(path_bytes) < 43:
        return None
    return (
        to_checksum_address(path_bytes[0:20]),
        int.from_bytes(path_bytes[20:23], "big"),
        to_checksum_address(path_bytes[23:43]),
    )


def _decode_sr01(name: str, raw: bytes):
    """Return (token_in, token_out, fee, recipient, amount_in, amount_out_min)."""
    if name == "exactInputSingle":
        (token_in, token_out, fee, recipient,
         _deadline, amount_in, amount_out_min, _lim) = abi_decode(
            [_SR01_SINGLE], raw)[0]
    elif name == "exactOutputSingle":
        (token_in, token_out, fee, recipient,
         _deadline, amount_out, amount_in_max, _lim) = abi_decode(
            [_SR01_SINGLE], raw)[0]
        amount_in, amount_out_min = amount_in_max, amount_out
    elif name == "exactInput":
        path_bytes, recipient, _deadline, amount_in, amount_out_min = (
            abi_decode([_SR01_MULTI], raw)[0])
        hop = _first_hop_from_path(path_bytes)
        if hop is None:
            return None
        token_in, fee, token_out = hop
    elif name == "exactOutput":
        path_bytes, recipient, _deadline, amount_out, amount_in_max = (
            abi_decode([_SR01_MULTI], raw)[0])
        if len(path_bytes) < 43:
            return None
        # exactOutput's path is reversed: output token first, input last.
        token_in = to_checksum_address(path_bytes[-20:])
        fee = int.from_bytes(path_bytes[-23:-20], "big")
        token_out = to_checksum_address(path_bytes[0:20])
        amount_in, amount_out_min = amount_in_max, amount_out
    else:
        return None

    return (token_in, token_out, int(fee), recipient,
            int(amount_in), int(amount_out_min))


def _decode_sr02(name: str, raw: bytes):
    """Same shape as _decode_sr01, but with SwapRouter02's struct layouts."""
    if name == "exactInputSingle":
        (token_in, token_out, fee, recipient,
         amount_in, amount_out_min, _lim) = abi_decode([_SR02_SINGLE], raw)[0]
    elif name == "exactOutputSingle":
        (token_in, token_out, fee, recipient,
         amount_out, amount_in_max, _lim) = abi_decode([_SR02_SINGLE], raw)[0]
        amount_in, amount_out_min = amount_in_max, amount_out
    elif name == "exactInput":
        path_bytes, recipient, amount_in, amount_out_min = (
            abi_decode([_SR02_MULTI], raw)[0])
        hop = _first_hop_from_path(path_bytes)
        if hop is None:
            return None
        token_in, fee, token_out = hop
    elif name == "exactOutput":
        path_bytes, recipient, amount_out, amount_in_max = (
            abi_decode([_SR02_MULTI], raw)[0])
        if len(path_bytes) < 43:
            return None
        token_in = to_checksum_address(path_bytes[-20:])
        fee = int.from_bytes(path_bytes[-23:-20], "big")
        token_out = to_checksum_address(path_bytes[0:20])
        amount_in, amount_out_min = amount_in_max, amount_out
    else:
        return None

    return (token_in, token_out, int(fee), recipient,
            int(amount_in), int(amount_out_min))


def decode_swap_v3(tx: dict) -> Optional[DecodedSwapV3]:
    """Return DecodedSwapV3 if tx is a V3 router swap, else None."""
    to = tx.get("to")
    if to is None:
        return None
    to = to_checksum_address(to)

    if to == SWAP_ROUTER_01:
        router_name = "uniswap_v3"
        table, decode_fn = _SELECTORS_SR01, _decode_sr01
    elif to == SWAP_ROUTER_02:
        router_name = "uniswap_v3_02"
        table, decode_fn = _SELECTORS_SR02, _decode_sr02
    else:
        return None

    data = tx.get("input") or tx.get("data")
    if not data or len(data) < 10:
        return None
    data = data.hex() if isinstance(data, bytes) else data
    selector = data[:10]
    if selector not in table:
        return None
    name = table[selector]

    try:
        decoded = decode_fn(name, bytes.fromhex(data[10:]))
    except Exception:
        return None
    if decoded is None:
        return None

    token_in, token_out, fee, recipient, amount_in, amount_out_min = decoded
    token_in = to_checksum_address(token_in)
    token_out = to_checksum_address(token_out)
    recipient = to_checksum_address(recipient)

    if token_in == WETH:
        is_buy, base_token = True, token_out
    elif token_out == WETH:
        is_buy, base_token = False, token_in
    else:
        # Token → token without WETH. Not a sandwich candidate.
        return None

    return DecodedSwapV3(
        router=router_name,
        function=name,
        token_in=token_in,
        token_out=token_out,
        fee=fee,
        recipient=recipient,
        amount_in=_to_int(amount_in),
        amount_out_min=_to_int(amount_out_min),
        is_buy=is_buy,
        base_token=base_token,
        gas_price=_effective_gas_price(tx),
        gas=_to_int(tx.get("gas")),
        tx_hash=tx["hash"].hex() if isinstance(
            tx.get("hash"), bytes) else str(tx.get("hash")),
    )
