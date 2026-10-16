"""Decode Uniswap UniversalRouter swaps.

UniversalRouter (0x3fC91A3a...) is where most modern Uniswap traffic
actually goes. Its `execute(bytes commands, bytes[] inputs, uint256)`
call packs one or more commands, each as a single byte, with a matching
`bytes` payload per command.

We parse just enough to find the first swap command in each tx and pull
the (token_in, token_out, amount) out of it. The rest of the commands
(PERMIT2_TRANSFER_FROM, WRAP_ETH, SWEEP, ...) are ignored — we only need
the shape of the trade, not a full replay.

Command opcodes handled:
  0x00  V3_SWAP_EXACT_IN
  0x01  V3_SWAP_EXACT_OUT
  0x08  V2_SWAP_EXACT_IN
  0x09  V2_SWAP_EXACT_OUT

These four cover the overwhelming majority of UniversalRouter swap
volume. Anything else (NFT ops, Permit2 batches, sub-plans) is skipped.
"""
from dataclasses import dataclass
from typing import Optional
from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address

from actions.mev.tx_decoder import _to_int, _effective_gas_price


WETH = to_checksum_address("0x4200000000000000000000000000000000000006")

UNIVERSAL_ROUTERS = (
    to_checksum_address("0x198ef79f1f515f02dfe9e3115ed9fc07183f02fc"),
    to_checksum_address("0x66a9893cc07d91d95644aedd05d03f95e1dba8af"),
    to_checksum_address("0xfdf682f51fe81aa4898f0ae2163d8a55c127fbc7"),
    to_checksum_address("0x6fF5693b99212Da76ad316178A184AB56D299b43"),
)

# Backwards-compat alias for the older name.
UNIVERSAL_ROUTER = UNIVERSAL_ROUTERS[0]

# execute(bytes,bytes[],uint256) — the deadline form, by far the most common
_EXECUTE_DEADLINE = "0x3593564c"
# execute(bytes,bytes[]) — some versions
_EXECUTE_PLAIN = "0x24856bc3"

# UniversalRouter command opcodes we care about.
_V3_SWAP_EXACT_IN = 0x00
_V3_SWAP_EXACT_OUT = 0x01
_V2_SWAP_EXACT_IN = 0x08
_V2_SWAP_EXACT_OUT = 0x09

# ABI for the packed V3 path (bytes) — read the first hop only.
_V3_HOP_STRUCT = ["address", "uint256", "uint256", "bytes", "bool"]
_V2_HOP_STRUCT = ["address", "uint256", "uint256", "address[]", "bool"]


@dataclass
class DecodedSwapUR:
    router: str           # always "universal_router"
    function: str         # e.g. "V3_SWAP_EXACT_IN"
    token_in: str
    token_out: str
    fee: int              # V3 fee tier, or 0 for V2 swaps
    recipient: str
    amount_in: int
    amount_out_min: int
    is_buy: bool
    base_token: str
    gas_price: int
    gas: int
    tx_hash: str


def _v3_first_hop(path_bytes: bytes, reversed_order: bool):
    """(token_in, fee, token_out) from a packed V3 path.

    exactInput: [token_in][fee][token_out]...
    exactOutput: [token_out][fee][token_in]... (encoded in reverse)
    """
    if len(path_bytes) < 43:
        return None
    a = to_checksum_address(path_bytes[0:20])
    fee = int.from_bytes(path_bytes[20:23], "big")
    b = to_checksum_address(path_bytes[23:43])
    if reversed_order:
        return b, fee, a
    return a, fee, b


def _finish(router_fn_name, token_in, token_out, fee, recipient,
            amount_in, amount_out_min, tx):
    """Common tail: decide direction, build DecodedSwapUR or return None."""
    token_in = to_checksum_address(token_in)
    token_out = to_checksum_address(token_out)
    recipient = to_checksum_address(recipient)

    if token_in == WETH:
        is_buy, base_token = True, token_out
    elif token_out == WETH:
        is_buy, base_token = False, token_in
    else:
        return None

    return DecodedSwapUR(
        router="universal_router",
        function=router_fn_name,
        token_in=token_in,
        token_out=token_out,
        fee=int(fee),
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


def _try_one(cmd: int, payload: bytes, tx: dict):
    """Decode one command's payload, returning DecodedSwapUR or None."""
    try:
        if cmd == _V3_SWAP_EXACT_IN:
            (recipient, amount_in, amount_out_min,
             path_bytes, _payer) = abi_decode(_V3_HOP_STRUCT, payload)
            hop = _v3_first_hop(path_bytes, reversed_order=False)
            if hop is None:
                return None
            token_in, fee, token_out = hop
            return _finish("V3_SWAP_EXACT_IN", token_in, token_out,
                           fee, recipient, amount_in, amount_out_min, tx)

        if cmd == _V3_SWAP_EXACT_OUT:
            (recipient, amount_out, amount_in_max,
             path_bytes, _payer) = abi_decode(_V3_HOP_STRUCT, payload)
            hop = _v3_first_hop(path_bytes, reversed_order=True)
            if hop is None:
                return None
            token_in, fee, token_out = hop
            return _finish("V3_SWAP_EXACT_OUT", token_in, token_out,
                           fee, recipient, amount_in_max, amount_out, tx)

        if cmd == _V2_SWAP_EXACT_IN:
            (recipient, amount_in, amount_out_min,
             path, _payer) = abi_decode(_V2_HOP_STRUCT, payload)
            if not path:
                return None
            return _finish("V2_SWAP_EXACT_IN", path[0], path[-1],
                           0, recipient, amount_in, amount_out_min, tx)

        if cmd == _V2_SWAP_EXACT_OUT:
            (recipient, amount_out, amount_in_max,
             path, _payer) = abi_decode(_V2_HOP_STRUCT, payload)
            if not path:
                return None
            # UniversalRouter's V2 exactOutput path is reversed
            return _finish("V2_SWAP_EXACT_OUT", path[-1], path[0],
                           0, recipient, amount_in_max, amount_out, tx)
    except Exception:
        return None

    return None


def decode_swap_ur(tx: dict) -> Optional[DecodedSwapUR]:
    """Return DecodedSwapUR if tx is a UniversalRouter swap, else None."""
    to = tx.get("to")
    if to is None:
        return None
    to = to_checksum_address(to)
    if to not in UNIVERSAL_ROUTERS:
        return None

    data = tx.get("input") or tx.get("data")
    if not data or len(data) < 10:
        return None
    data = data.hex() if isinstance(data, bytes) else data
    selector = data[:10]
    if selector not in (_EXECUTE_DEADLINE, _EXECUTE_PLAIN):
        return None

    raw = bytes.fromhex(data[10:])

    try:
        if selector == _EXECUTE_DEADLINE:
            commands, inputs, _deadline = abi_decode(
                ["bytes", "bytes[]", "uint256"], raw)
        else:
            commands, inputs = abi_decode(["bytes", "bytes[]"], raw)
    except Exception:
        return None

    # Walk the commands in order; return on the first swap we recognise.
    # A single execute() can hold several ops, but for the sandwich
    # decision only the first swap matters.
    for i, raw_cmd in enumerate(commands):
        if i >= len(inputs):
            break
        cmd = raw_cmd & 0x1F  # strip the allow-revert flag bit
        if cmd not in (_V3_SWAP_EXACT_IN, _V3_SWAP_EXACT_OUT,
                       _V2_SWAP_EXACT_IN, _V2_SWAP_EXACT_OUT):
            continue
        decoded = _try_one(cmd, inputs[i], tx)
        if decoded is not None:
            return decoded

    return None
