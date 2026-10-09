"""Read-only Ethereum DEX dislocation scanner and paper-arbitrage simulator.

It compares same-block Uniswap V2 and SushiSwap V2 pool reserves, simulates
two-swap round trips with pool fees, and estimates gas cost. It never inspects
pending transactions and deliberately has no wallet, signing, approval, or
transaction-broadcast implementation. All opportunities are hypothetical.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation, localcontext
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


CHAIN_ID = 1
# The provider: ETHEREUM_RPC_URL in .env, read when a scan starts (not at import, before .env is loaded).
FALLBACK_RPC_URL = "https://ethereum-rpc.publicnode.com"
# Free public providers sit behind Cloudflare, which refuses Python's default User-Agent with HTTP 403.
USER_AGENT = "JARVIS/1.0 (read-only DEX scanner)"
# Seconds between requests, so a free endpoint's rate limit is respected (ETHEREUM_RPC_MIN_INTERVAL).
DEFAULT_MIN_INTERVAL = 0.25
# A 429 (too many requests) is asked again after a growing wait, this many times in all.
RPC_ATTEMPTS = 3
DEFAULT_AMOUNT_USD = Decimal("1000")
DEFAULT_GAS_UNITS = 240_000
BPS = 10_000
ZERO_ADDRESS = "0x" + "0" * 40

# Canonical Ethereum mainnet contracts. Addresses are data, not wallet secrets.
FACTORIES = (
    ("Uniswap V2", "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"),
    ("SushiSwap V2", "0xc0aee478e3658e2610c5f7a4a2e1777ce9e4f2ac"),
)
# The defaults; dex-scan.json in the JARVIS folder adds or replaces tokens and pairs. WETH is Ethereum's
# ether as a token, WBTC bitcoin held on Ethereum one for one.
TOKENS = {
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "DAI": "0x6b175474e89094c44da98b954eedeac495271d0f",
    "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "WBTC": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
}
# Each pair is (what is priced, what it is priced in): "WETH/USDC" is ether's price in USDC.
PAIR_CONFIGS = (
    ("WETH", "USDC"),
    ("WETH", "USDT"),
    ("WETH", "DAI"),
    ("WBTC", "WETH"),
    ("WBTC", "USDC"),
    ("USDT", "USDC"),
    ("DAI", "USDC"),
    ("DAI", "USDT"),
)
SETTINGS_NAME = "dex-scan.json"
# The share a round trip loses to the two pools' fees alone: 1 - 0.997 x 0.997, about 0.6%.
ROUND_TRIP_FEE = 1 - (Decimal(BPS - 30) / BPS) ** 2
# Uniswap V2-style pools charge 30 bps. This scanner intentionally supports
# only the two named V2 factories; it does not assume this fee for other DEXes.
POOL_FEE_BPS = 30
GAS_PRICE_SELECTOR = "0x"
# Multicall3: a public, read-only contract at this address on every Ethereum network since 2022. Handed a
# list of reads, it makes them all within one eth_call and returns every answer -- one request to the
# provider instead of dozens, which is what makes a scan quick on a rate-limited free endpoint.
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
# Reads handed to Multicall3 at once, well inside what providers allow for one eth_call.
MULTICALL_BATCH = 100
SELECTORS = {
    "aggregate3": "82ad56cb",
    "getPair": "e6a43905",
    "getReserves": "0902f1ac",
    "decimals": "313ce567",
}
_ALLOWED_RPC_METHODS = frozenset(
    {"eth_chainId", "eth_blockNumber", "eth_gasPrice", "eth_call"}
)
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_RE = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")
USD_PEG_ASSUMPTIONS = frozenset({"USDC", "USDT", "DAI"})


def rpc_url() -> str:
    """The Ethereum provider to use: ETHEREUM_RPC_URL, else a free public one."""
    return (os.getenv("ETHEREUM_RPC_URL") or "").strip() or FALLBACK_RPC_URL


def min_interval() -> float:
    """Seconds between requests: ETHEREUM_RPC_MIN_INTERVAL, else the default; never negative."""
    try:
        return max(0.0, float(os.getenv("ETHEREUM_RPC_MIN_INTERVAL", DEFAULT_MIN_INTERVAL)))
    except ValueError:
        return DEFAULT_MIN_INTERVAL


def _settings_path() -> str | None:
    try:
        from actions import files

        base = files.root()
    except Exception:  # noqa: BLE001 - no JARVIS folder: the defaults alone
        return None
    return os.path.join(base, SETTINGS_NAME) if base else None


def settings() -> dict:
    """{"tokens": {name: address}, "pairs": [(base, quote)], "amount_usd": Decimal}: the defaults, with
    dex-scan.json's additions; written out the first time so there is a file to edit. A token with a bad
    address, or a pair naming a token not known, is left out and said."""
    chosen = {"tokens": dict(TOKENS), "pairs": [list(pair) for pair in PAIR_CONFIGS],
              "amount_usd": str(DEFAULT_AMOUNT_USD)}
    path = _settings_path()
    saved = {}

    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError) as error:
            print(f"[DEX scanner] {SETTINGS_NAME} could not be read ({error}); using the defaults")
            saved = {}
    elif path:
        try:
            with open(f"{path}.part", "w", encoding="utf-8") as handle:
                json.dump(chosen, handle, indent=1)
            os.replace(f"{path}.part", path)
        except OSError:
            pass

    tokens = dict(TOKENS)

    for name, address in (saved.get("tokens") or {}).items() if isinstance(saved.get("tokens"), dict) else ():
        try:
            tokens[str(name).upper()] = normalize_address(address)
        except ValueError:
            print(f"[DEX scanner] {SETTINGS_NAME}: {name} has no valid address; left out")

    pairs = []

    for pair in saved.get("pairs") or PAIR_CONFIGS:
        names = tuple(str(name).upper() for name in pair) if isinstance(pair, (list, tuple)) else ()

        if len(names) != 2 or names[0] == names[1] or not all(name in tokens for name in names):
            print(f"[DEX scanner] {SETTINGS_NAME}: pair {pair!r} needs two different known tokens; left out")
        elif names not in pairs:
            pairs.append(names)

    try:
        amount = Decimal(str(saved.get("amount_usd", DEFAULT_AMOUNT_USD)))
        amount = amount if amount.is_finite() and amount > 0 else DEFAULT_AMOUNT_USD
    except InvalidOperation:
        amount = DEFAULT_AMOUNT_USD

    return {"tokens": tokens, "pairs": pairs, "amount_usd": amount}


def normalize_address(value: str) -> str:
    """Validate an EVM address and return its lowercase representation."""
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValueError("Expected a 20-byte EVM address.")
    return value.lower()


def _address_word(address: str) -> str:
    return normalize_address(address)[2:].rjust(64, "0")


def _decode_words(data: str, count: int) -> tuple[int, ...]:
    """Decode ABI-encoded uint words from a hex response."""
    if not isinstance(data, str) or not data.startswith("0x"):
        raise ValueError("Ethereum RPC returned malformed ABI data.")
    payload = data[2:]
    if len(payload) < count * 64 or not re.fullmatch(r"[0-9a-fA-F]+", payload):
        raise ValueError("Ethereum RPC returned truncated ABI data.")
    return tuple(
        int(payload[index * 64:(index + 1) * 64], 16)
        for index in range(count)
    )


def _decode_address(data: str) -> str:
    value = _decode_words(data, 1)[0]
    if value >> 160:
        raise ValueError("Ethereum RPC returned a malformed address.")
    return "0x" + f"{value:040x}"


class EthereumReadOnlyRPC:
    """Small JSON-RPC client with an explicit read-only method allowlist."""

    def __init__(self, url: str | None = None, timeout: float = 12.0, interval: float | None = None,
                 sleep=time.sleep, clock=time.monotonic):
        url = url or rpc_url()
        parsed = urlparse(url)
        is_local_http = parsed.scheme == "http" and parsed.hostname in {
            "localhost", "127.0.0.1", "::1"
        }
        if parsed.scheme != "https" and not is_local_http:
            raise ValueError(
                "Use an HTTPS Ethereum RPC URL (HTTP is allowed only for localhost)."
            )
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("The Ethereum RPC URL is invalid.")
        self._url = url
        self._timeout = timeout
        self._request_id = 0
        self._interval = min_interval() if interval is None else max(0.0, interval)
        self._sleep = sleep
        self._clock = clock
        self._last = None
        self.host = parsed.hostname

    def _pace(self):
        """Wait out the minimum interval since the last request."""
        if self._last is not None:
            wait = self._interval - (self._clock() - self._last)

            if wait > 0:
                self._sleep(wait)

        self._last = self._clock()

    def _request(self, method: str, params: list) -> str:
        if method not in _ALLOWED_RPC_METHODS:
            raise ValueError(f"RPC method is not permitted in read-only mode: {method}")
        self._request_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
        body = None

        for attempt in range(RPC_ATTEMPTS):
            self._pace()

            try:
                with urlopen(Request(self._url, data=payload, headers=headers, method="POST"),
                             timeout=self._timeout) as response:
                    body = response.read()
                break
            except HTTPError as error:
                if error.code == 429 and attempt + 1 < RPC_ATTEMPTS:
                    # Too many requests: wait as asked (Retry-After), else a growing pause, and ask again.
                    try:
                        wait = float(error.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        wait = 0.0
                    self._sleep(min(10.0, max(wait, 2.0 ** attempt)))
                    continue
                why = {403: "refused the request", 429: "is rate-limiting this scanner"}.get(error.code, "failed")
                raise RuntimeError(
                    f"Ethereum RPC {self.host} {why} (HTTP {error.code}) on request {self._request_id}"
                    f" ({method}); try another provider in ETHEREUM_RPC_URL, or a slower"
                    " ETHEREUM_RPC_MIN_INTERVAL."
                ) from None
            except (URLError, TimeoutError, OSError):
                raise RuntimeError(
                    f"Could not reach the Ethereum RPC provider {self.host}; check the network or"
                    " ETHEREUM_RPC_URL."
                ) from None

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("Ethereum RPC returned an invalid response.") from None
        if decoded.get("error"):
            error = decoded["error"]
            message = str(error.get("message", "request failed"))[:160]
            raise RuntimeError(f"Ethereum RPC {method} failed: {message}")
        if "result" not in decoded:
            raise RuntimeError("Ethereum RPC response has no result.")
        result = decoded["result"]
        if not isinstance(result, str):
            raise RuntimeError(f"Ethereum RPC {method} returned an unexpected result.")
        return result

    def chain_id(self) -> int:
        return int(self._request("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self._request("eth_blockNumber", []), 16)

    def gas_price_wei(self) -> int:
        return int(self._request("eth_gasPrice", []), 16)

    def contract_call(self, address: str, data: str, block_number: int) -> str:
        address = normalize_address(address)
        if not _HEX_RE.fullmatch(data):
            raise ValueError("Contract call data must be valid hex.")
        return self._request("eth_call", [
            {"to": address, "data": data},
            hex(block_number),
        ])


@dataclass(frozen=True)
class PoolSnapshot:
    """V2 pool state read at one fixed Ethereum block."""

    dex: str
    address: str
    token0: str
    token1: str
    reserve0: int
    reserve1: int
    decimals0: int
    decimals1: int
    fee_bps: int = POOL_FEE_BPS

    def __post_init__(self):
        object.__setattr__(self, "address", normalize_address(self.address))
        object.__setattr__(self, "token0", normalize_address(self.token0))
        object.__setattr__(self, "token1", normalize_address(self.token1))
        if self.token0 == self.token1:
            raise ValueError("A pool must contain two distinct tokens.")
        if self.reserve0 < 0 or self.reserve1 < 0:
            raise ValueError("Pool reserves cannot be negative.")
        if not (0 <= self.decimals0 <= 36 and 0 <= self.decimals1 <= 36):
            raise ValueError("Token decimals are outside the supported range.")
        if not (0 <= self.fee_bps < BPS):
            raise ValueError("Pool fee must be between 0 and 9999 basis points.")

    def other_token(self, token: str) -> str:
        token = normalize_address(token)
        if token == self.token0:
            return self.token1
        if token == self.token1:
            return self.token0
        raise ValueError("Token is not part of this pool.")

    def token_decimals(self, token: str) -> int:
        token = normalize_address(token)
        if token == self.token0:
            return self.decimals0
        if token == self.token1:
            return self.decimals1
        raise ValueError("Token is not part of this pool.")

    def reserves_for(self, token_in: str) -> tuple[int, int]:
        token_in = normalize_address(token_in)
        if token_in == self.token0:
            return self.reserve0, self.reserve1
        if token_in == self.token1:
            return self.reserve1, self.reserve0
        raise ValueError("Token is not part of this pool.")


def human_to_raw(amount: Decimal | str | int, decimals: int) -> int:
    """Convert a human token amount to exact base units without rounding."""
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        raise ValueError("Token amount must be numeric.") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("Token amount must be finite and greater than zero.")
    if not isinstance(decimals, int) or not 0 <= decimals <= 36:
        raise ValueError("Token decimals are outside the supported range.")
    raw = value * (Decimal(10) ** decimals)
    if raw != raw.to_integral_value():
        raise ValueError("Token amount has more precision than the token supports.")
    return int(raw)


def whole_units(amount: Decimal, decimals: int) -> int:
    """A token amount in its smallest units, rounded down: $100 of WETH runs to more places than WETH has,
    and a real swap is sized in whole units, never spending more than asked. Worked at full precision,
    so a large amount is not rounded on the way."""
    if not isinstance(decimals, int) or not 0 <= decimals <= 36:
        raise ValueError("Token decimals are outside the supported range.")
    with localcontext() as context:
        context.prec = 100
        return int((Decimal(amount) * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN))


def raw_to_decimal(amount: int, decimals: int) -> Decimal:
    if amount < 0 or not isinstance(decimals, int) or not 0 <= decimals <= 36:
        raise ValueError("Invalid token amount or decimals.")
    return Decimal(amount) / (Decimal(10) ** decimals)


def quote_v2_exact_input(
    amount_in: int,
    reserve_in: int,
    reserve_out: int,
    fee_bps: int = POOL_FEE_BPS,
) -> int:
    """Return a V2-style exact-input quote, in output base units."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        raise ValueError("Trade amount and both reserves must be positive.")
    if not isinstance(fee_bps, int) or not 0 <= fee_bps < BPS:
        raise ValueError("Fee must be an integer from 0 to 9999 basis points.")
    amount_with_fee = amount_in * (BPS - fee_bps)
    return (amount_with_fee * reserve_out) // (
        reserve_in * BPS + amount_with_fee
    )


def simulate_round_trip(
    amount_in: int,
    token_in: str,
    first_pool: PoolSnapshot,
    second_pool: PoolSnapshot,
) -> tuple[int, int]:
    """Simulate buy-on-first/sell-on-second without sending any transaction.

    Returns (intermediate token amount, final token-in amount), both in raw units.
    """
    token_in = normalize_address(token_in)
    token_out = first_pool.other_token(token_in)
    if second_pool.other_token(token_out) != token_in:
        raise ValueError("Pools must contain the same token pair.")
    reserve_in, reserve_out = first_pool.reserves_for(token_in)
    intermediate = quote_v2_exact_input(
        amount_in, reserve_in, reserve_out, first_pool.fee_bps
    )
    reserve_sell_in, reserve_sell_out = second_pool.reserves_for(token_out)
    final_amount = quote_v2_exact_input(
        intermediate,
        reserve_sell_in,
        reserve_sell_out,
        second_pool.fee_bps,
    )
    return intermediate, final_amount


def _pool_price(pool: PoolSnapshot, base: str, quote: str) -> Decimal:
    """Current reserve ratio: units of quote per one base token."""
    base = normalize_address(base)
    quote = normalize_address(quote)
    if pool.other_token(base) != quote:
        raise ValueError("Price tokens must be the pool's pair.")
    reserve_base, reserve_quote = pool.reserves_for(base)
    base_human = raw_to_decimal(reserve_base, pool.token_decimals(base))
    quote_human = raw_to_decimal(reserve_quote, pool.token_decimals(quote))
    if base_human <= 0 or quote_human <= 0:
        raise ValueError("Cannot derive a price from an empty pool.")
    return quote_human / base_human


def _word(value: int) -> str:
    return f"{value:064x}"


def _padded(data: bytes) -> str:
    return data.hex() + "00" * ((-len(data)) % 32)


def encode_aggregate3(calls: list[tuple[str, str]]) -> str:
    """Call data for Multicall3.aggregate3: each (contract, call data) with allowFailure set, so one read
    that fails (a pool that does not exist) leaves the rest standing. ABI-encoded by hand: an array of
    (address, bool, bytes) tuples, each tuple dynamic for its bytes."""
    tuples = []

    for target, data in calls:
        payload = bytes.fromhex(data[2:] if data.startswith("0x") else data)
        tuples.append(_address_word(target) + _word(1) + _word(96) + _word(len(payload)) + _padded(payload))

    offsets, at = [], 32 * len(tuples)
    for encoded in tuples:
        offsets.append(_word(at))
        at += len(encoded) // 2

    return "0x" + SELECTORS["aggregate3"] + _word(32) + _word(len(tuples)) + "".join(offsets) + "".join(tuples)


def decode_aggregate3(data: str, count: int) -> list[str | None]:
    """Multicall3's answer: each read's return data as hex, or None where that read failed."""
    if not isinstance(data, str) or not data.startswith("0x") or not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", data[2:]):
        raise ValueError("Multicall returned malformed data.")
    raw = bytes.fromhex(data[2:])

    def word(at: int) -> int:
        if at + 32 > len(raw):
            raise ValueError("Multicall returned truncated data.")
        return int.from_bytes(raw[at:at + 32], "big")

    array = word(0)
    if word(array) != count:
        raise ValueError("Multicall returned a different number of answers than reads.")
    items = array + 32
    results = []

    for index in range(count):
        start = items + word(items + 32 * index)
        succeeded = word(start)
        body = start + word(start + 32)
        length = word(body)
        if body + 32 + length > len(raw):
            raise ValueError("Multicall returned truncated data.")
        results.append("0x" + raw[body + 32:body + 32 + length].hex() if succeeded else None)

    return results


def multicall(rpc: EthereumReadOnlyRPC, calls: list[tuple[str, str]], block_number: int) -> list[str | None]:
    """Every read in [calls] at [block_number], MULTICALL_BATCH to a request."""
    answers = []

    for first in range(0, len(calls), MULTICALL_BATCH):
        batch = calls[first:first + MULTICALL_BATCH]
        answers += decode_aggregate3(rpc.contract_call(MULTICALL3, encode_aggregate3(batch), block_number),
                                     len(batch))

    return answers


# What never changes once read: a factory's pool for two tokens, and a token's decimals. Kept for the
# session, so after the first scan only the pools' reserves are asked for.
_known_pools: dict[tuple[str, str, str], str] = {}
_known_decimals: dict[str, int] = {}


def _read_pools(rpc, wanted, tokens, block_number, progress=None) -> dict:
    """{(base, quote): {dex: PoolSnapshot}} for every pair in [wanted] on every factory, in at most three
    requests: the pools' addresses and the tokens' decimals the first time, then the reserves."""
    keys = [((base, quote), dex, factory, *sorted((tokens[base], tokens[quote])))
            for base, quote in wanted for dex, factory in FACTORIES]

    def step(label, done):
        if progress:
            progress(f"Arbitrage scan: {label}", done, 3)

    unknown = [key for key in keys if (key[2], key[3], key[4]) not in _known_pools]
    if unknown:
        step("finding the pools", 1)
        found = multicall(rpc, [(factory, "0x" + SELECTORS["getPair"] + _address_word(token0) + _address_word(token1))
                                for _pair, _dex, factory, token0, token1 in unknown], block_number)
        for (_pair, _dex, factory, token0, token1), answer in zip(unknown, found):
            if answer is not None:
                _known_pools[(factory, token0, token1)] = _decode_address(answer)

    needed = sorted({token for key in keys for token in key[3:]} - set(_known_decimals))
    if needed:
        step("reading the tokens", 2)
        for token, answer in zip(needed, multicall(rpc, [(token, "0x" + SELECTORS["decimals"]) for token in needed],
                                                  block_number)):
            if answer is None:
                raise RuntimeError(f"Token {token} would not say its decimals.")
            decimals = _decode_words(answer, 1)[0]
            if decimals > 36:
                raise RuntimeError("Token reports unsupported decimals.")
            _known_decimals[token] = decimals

    live = [key for key in keys if _known_pools.get((key[2], key[3], key[4]), ZERO_ADDRESS) != ZERO_ADDRESS]
    step("reading the prices", 3)
    reserves = multicall(rpc, [(_known_pools[(key[2], key[3], key[4])], "0x" + SELECTORS["getReserves"])
                               for key in live], block_number)
    pools: dict = {pair: {} for pair in wanted}

    for (pair, dex, factory, token0, token1), answer in zip(live, reserves):
        if answer is None:
            continue
        reserve0, reserve1, _timestamp = _decode_words(answer, 3)
        pools[pair][dex] = PoolSnapshot(
            dex=dex, address=_known_pools[(factory, token0, token1)], token0=token0, token1=token1,
            reserve0=reserve0, reserve1=reserve1, decimals0=_known_decimals[token0],
            decimals1=_known_decimals[token1], fee_bps=POOL_FEE_BPS)

    return pools


def _format_decimal(value: Decimal, places: int = 8) -> str:
    quantum = Decimal(1).scaleb(-places)
    return format(value.quantize(quantum), "f")


def _prices_in_usd(pools_by_pair: dict, tokens: dict) -> dict[str, Decimal]:
    """Each token's dollar price at this block: the stablecoins $1 for screening, the rest worked out from
    their pools against a token already priced (WETH from USDC, then WBTC from WETH), the median of the
    exchanges' prices."""
    prices = {tokens[name]: Decimal("1") for name in USD_PEG_ASSUMPTIONS if name in tokens}
    learned = True

    while learned:
        learned = False

        for (base, quote), pools in pools_by_pair.items():
            for known, unknown in ((quote, base), (base, quote)):
                if tokens[unknown] in prices or tokens[known] not in prices or not pools:
                    continue
                # Only stablecoin pools price a token in dollars directly; a stablecoin's own price stays $1.
                quotes = [_pool_price(pool, tokens[unknown], tokens[known]) for pool in pools.values()]
                prices[tokens[unknown]] = Decimal(str(statistics.median(quotes))) * prices[tokens[known]]
                learned = True

    return prices


def _depth_usd(pool: PoolSnapshot, token_prices: dict) -> Decimal | None:
    """What a pool holds, both sides, in dollars: how much a trade can move through it before its own size
    moves the price. None if either token has no dollar price."""
    sides = ((pool.token0, pool.reserve0, pool.decimals0), (pool.token1, pool.reserve1, pool.decimals1))
    if any(token not in token_prices for token, _reserve, _decimals in sides):
        return None
    return sum(raw_to_decimal(reserve, decimals) * token_prices[token] for token, reserve, decimals in sides)


def scan_once(
    rpc: EthereumReadOnlyRPC,
    amount_usd: Decimal | str | int = DEFAULT_AMOUNT_USD,
    gas_units: int = DEFAULT_GAS_UNITS,
    chosen: dict | None = None,
    progress=None,
) -> dict:
    """Scan current public pool state: every pair's price on each exchange, the gap between them, the best
    round trip after fees and gas, and the paper-only candidates that would gain before gas.

    [chosen] is settings() (tokens and pairs); [progress] is called (label, done, total) as pools are read.
    """
    try:
        amount_usd = Decimal(str(amount_usd))
    except InvalidOperation:
        raise ValueError("amount_usd must be numeric.") from None
    if not amount_usd.is_finite() or amount_usd <= 0:
        raise ValueError("amount_usd must be finite and greater than zero.")
    if not isinstance(gas_units, int) or not 21_000 <= gas_units <= 2_000_000:
        raise ValueError("gas_units must be between 21000 and 2000000.")

    tokens = (chosen or {}).get("tokens") or TOKENS
    pairs = (chosen or {}).get("pairs") or PAIR_CONFIGS

    chain_id = rpc.chain_id()
    if chain_id != CHAIN_ID:
        raise RuntimeError(
            f"This scanner is configured for Ethereum mainnet (chain ID {CHAIN_ID}); "
            f"RPC reported chain ID {chain_id}."
        )
    block_number = rpc.block_number()
    gas_price_wei = rpc.gas_price_wei()

    pools_by_pair = _read_pools(rpc, [tuple(pair) for pair in pairs], tokens, block_number, progress)
    pool_count = sum(len(pools) for pools in pools_by_pair.values())

    token_prices = _prices_in_usd(pools_by_pair, tokens)
    weth_usd = token_prices.get(tokens.get("WETH"))

    gas_cost_usd = None
    if weth_usd is not None:
        gas_cost_usd = (
            Decimal(gas_price_wei) * Decimal(gas_units)
            / Decimal(10**18) * weth_usd
        )

    opportunities = []
    markets = []
    for (name_a, name_b), pools in pools_by_pair.items():
        exact = {}
        for pool in pools.values():
            try:
                exact[pool.dex] = _pool_price(pool, tokens[name_a], tokens[name_b])
            except ValueError:
                continue   # an empty pool has no price
        # Shown to 8 significant figures, so a small price keeps its digits; the gap is worked from the exact.
        market = {"pair": f"{name_a}/{name_b}",
                  "prices": {dex: format(price, ".8g") for dex, price in exact.items()},
                  "depth_usd": {pool.dex: _format_decimal(_depth_usd(pool, token_prices), 2)
                                for pool in pools.values() if _depth_usd(pool, token_prices) is not None},
                  "gap_pct": None, "best": None}
        markets.append(market)

        if len(exact) < 2:
            continue
        pool_items = list(pools.values())
        pool_one, pool_two = pool_items[0], pool_items[1]
        market["gap_pct"] = _format_decimal(
            (max(exact.values()) - min(exact.values())) / min(exact.values()) * 100, 4)
        pair_tokens = (tokens[name_a], tokens[name_b])

        for token_in in pair_tokens:
            price_usd = token_prices.get(token_in)
            if price_usd is None or price_usd <= 0:
                continue
            amount_in_human = amount_usd / price_usd
            decimals = (
                pool_one.token_decimals(token_in)
                if token_in in (pool_one.token0, pool_one.token1)
                else pool_two.token_decimals(token_in)
            )
            amount_in = whole_units(amount_in_human, decimals)
            if amount_in <= 0:
                continue

            for first, second in ((pool_one, pool_two), (pool_two, pool_one)):
                try:
                    intermediate, final_amount = simulate_round_trip(
                        amount_in, token_in, first, second
                    )
                except ValueError:
                    continue
                gross_profit_raw = final_amount - amount_in
                # A loss is negative: raw_to_decimal takes only amounts, so the sign is kept apart.
                gross_profit_token = raw_to_decimal(abs(gross_profit_raw), decimals) * (
                    1 if gross_profit_raw >= 0 else -1)
                gross_profit_usd = gross_profit_token * price_usd
                net_profit_usd = (
                    gross_profit_usd - gas_cost_usd
                    if gas_cost_usd is not None else None
                )
                route = {
                    "pair": f"{name_a}/{name_b}",
                    "buy_on": first.dex,
                    "sell_on": second.dex,
                    "input_token": name_a if token_in == tokens[name_a] else name_b,
                    # In a person's words: where the pair's first token (ether in WETH/USDC) is bought
                    # cheap and where it is sold dear -- the same whichever token the trip starts with.
                    "buy_base_on": (first if token_in == tokens[name_b] else second).dex,
                    "sell_base_on": (second if token_in == tokens[name_b] else first).dex,
                    "input_amount": _format_decimal(
                        raw_to_decimal(amount_in, decimals), 6
                    ),
                    "intermediate_amount_raw": str(intermediate),
                    "final_amount": _format_decimal(
                        raw_to_decimal(final_amount, decimals), 8
                    ),
                    "gross_profit_usd": _format_decimal(gross_profit_usd, 6),
                    "estimated_gas_cost_usd": (
                        _format_decimal(gas_cost_usd, 6)
                        if gas_cost_usd is not None else None
                    ),
                    "estimated_net_profit_usd": (
                        _format_decimal(net_profit_usd, 6)
                        if net_profit_usd is not None else None
                    ),
                    "snapshot_block": block_number,
                    "simulation_only": True,
                }
                score = net_profit_usd if net_profit_usd is not None else gross_profit_usd
                if market["best"] is None or score > Decimal(
                        market["best"]["estimated_net_profit_usd"] or market["best"]["gross_profit_usd"]):
                    market["best"] = route
                if gross_profit_raw > 0:
                    opportunities.append(route)

    opportunities.sort(
        key=lambda item: Decimal(
            item["estimated_net_profit_usd"]
            if item["estimated_net_profit_usd"] is not None
            else item["gross_profit_usd"]
        ),
        reverse=True,
    )
    # The gap a round trip must beat to pay: the two pools' fees, and gas as a share of the trade.
    breakeven = ROUND_TRIP_FEE * 100 + ((gas_cost_usd or Decimal(0)) / amount_usd * 100)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only paper simulation",
        "chain_id": chain_id,
        "snapshot_block": block_number,
        "dexes": [item[0] for item in FACTORIES],
        "pools_read": pool_count,
        "requested_trade_size_usd": _format_decimal(amount_usd, 2),
        "estimated_gas_price_gwei": _format_decimal(
            Decimal(gas_price_wei) / Decimal(10**9), 4
        ),
        "assumed_gas_units_per_round_trip": gas_units,
        "estimated_gas_cost_usd": _format_decimal(gas_cost_usd, 4) if gas_cost_usd is not None else None,
        "estimated_eth_usd": _format_decimal(weth_usd, 4) if weth_usd else None,
        "estimated_btc_usd": (_format_decimal(token_prices[tokens["WBTC"]], 2)
                              if tokens.get("WBTC") in token_prices else None),
        "round_trip_fee_pct": _format_decimal(ROUND_TRIP_FEE * 100, 4),
        "breakeven_gap_pct": _format_decimal(breakeven, 4),
        "stablecoin_usd_assumption": "USDC, USDT and DAI treated as $1 for screening",
        "markets": markets,
        "opportunities": opportunities,
        "safety": {
            "wallet_address_used": False,
            "private_key_required": False,
            "pending_transactions_inspected": False,
            "transactions_signed_or_broadcast": False,
            "live_execution_enabled": False,
            "warning": (
                "Reserve-ratio paper quotes are not executable guarantees. "
                "They omit routing details, changing state, failed transactions, "
                "MEV competition and actual gas variance. Verify independently."
            ),
        },
    }


# What the scanner is about. "Arbitrage" -- buying where a coin is cheaper and selling where it is dearer --
# is a whole word and said plainly, and nothing else in JARVIS does it, so it is enough on its own: "Jarvis,
# arbitrage", "scan for arbitrage", "any crypto arbitrage". The exchanges' names ("scan the DEX", "check
# defi price gaps") also need a request to look. Never "what is arbitrage", "buy ethereum", or a sentence
# naming one of the trade plan's markets ("arbitrage on gold"), which are for it.
_ARBITRAGE = re.compile(r"\barbitrage\b")
_DEX_SUBJECT = re.compile(
    r"\b(?:dex(?:es)?|defi|decentrali[sz]ed exchanges?|uniswap|sushi ?swap)\b")
_DEX_LOOK = re.compile(
    r"\b(?:scan(?:ner)?|check|find|show|look|search|any|run|what are)\b"
    r"|\b(?:opportunit(?:y|ies)|price gaps?|dislocations?)\b")
_DEX_NOT = re.compile(r"\b(?:what is|what s|what does|explain|meaning of|define|buy|sell|swap my|send)\b")


def _trade_plan_market(text: str) -> bool:
    """Whether [text] names a market the trade plan reads (trade-plan.json): those questions are its."""
    try:
        from actions import trade_plan

        return trade_plan.which(text) is not None
    except Exception:  # noqa: BLE001 - no trade plan settings: nothing named
        return False


def asked(command: str) -> bool:
    """Recognise a request to run the read-only arbitrage scanner: "arbitrage", or an exchange and a request
    to look."""
    text = " ".join(re.sub(r"[^a-z0-9 ]+", " ", str(command or "").casefold()).split())

    if _DEX_NOT.search(text) or dismissed(text):
        return False

    if _ARBITRAGE.search(text):
        return not _trade_plan_market(text)

    return bool(_DEX_SUBJECT.search(text) and _DEX_LOOK.search(text))


_listener = None
_hide_listener = None
_progress_listener = None


def set_listeners(on_scan=None, on_hide=None, on_progress=None):
    """The panel's hooks: on_scan(report) shows a scan, on_hide() closes it, on_progress(label, done, total)
    marks the HUD while pools are read."""
    global _listener, _hide_listener, _progress_listener
    _listener, _hide_listener, _progress_listener = on_scan, on_hide, on_progress


def dismissed(command: str) -> bool:
    """ "Close the arbitrage scan", "hide the dex panel"."""
    text = " ".join(re.sub(r"[^a-z0-9 ]+", " ", str(command or "").casefold()).split())
    return bool(re.search(r"\b(?:close|hide|dismiss|shut|put away)\b", text)
                and (_ARBITRAGE.search(text) or _DEX_SUBJECT.search(text)))


def hide() -> bool:
    if _hide_listener:
        _hide_listener()
        return True
    return False


def _done(label=""):
    if _progress_listener:
        _progress_listener(label, 0, 0)


def describe() -> str:
    """Run one read-only scan, show it in the panel, and return a concise voice response."""
    chosen = settings()

    try:
        report = scan_once(EthereumReadOnlyRPC(), amount_usd=chosen["amount_usd"], chosen=chosen,
                           progress=_progress_listener)
    except (RuntimeError, ValueError) as error:
        print(f"[DEX scanner] {error}", flush=True)
        return f"I couldn't read the exchanges just now, sir: {str(error).split(';')[0]}."
    finally:
        _done()

    print(json.dumps(report, indent=2), flush=True)

    if _listener:
        _listener(report)

    return summary(report)


def summary(report: dict) -> str:
    """What to say about a scan: the widest gap against what a round trip needs to pay, and the best route."""
    compared = [market for market in report["markets"] if market["gap_pct"] is not None]
    if not compared:
        return (f"I couldn't find each pair on both exchanges at block {report['snapshot_block']}, sir, so there"
                " was nothing to compare.")

    widest = max(compared, key=lambda market: Decimal(market["gap_pct"]))
    needed = Decimal(report["breakeven_gap_pct"])
    gap = Decimal(widest["gap_pct"])
    gas = report.get("estimated_gas_cost_usd")
    gas_words = f" and gas at about ${Decimal(gas):.2f}" if gas is not None else ""
    opening = (f"I compared {len(compared)} pairs on Uniswap and SushiSwap at block {report['snapshot_block']},"
               f" sir. The widest gap is {widest['pair']} at {gap:.2f} percent; a round trip needs about"
               f" {needed:.2f} percent to pay, for the two swaps' fees{gas_words}.")
    best = widest["best"]

    if best and best["estimated_net_profit_usd"] is not None and Decimal(best["estimated_net_profit_usd"]) > 0:
        coin = widest["pair"].split("/")[0]
        return (opening + f" Buying {coin} on {best['buy_base_on']} and selling it on {best['sell_base_on']}"
                f" would clear about ${Decimal(best['estimated_net_profit_usd']):.2f} on paper. Nothing was traded.")

    return opening + " Nothing clears it right now. Nothing was traded."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Ethereum DEX arbitrage scanner (paper simulation only)."
    )
    parser.add_argument(
        "--rpc-url",
        default=None,
        help="HTTPS Ethereum JSON-RPC URL; or set ETHEREUM_RPC_URL in .env.",
    )
    parser.add_argument(
        "--amount-usd",
        default=None,
        help="Input size to simulate in USD-equivalent terms (default: amount_usd in dex-scan.json, 1000).",
    )
    parser.add_argument(
        "--gas-units",
        type=int,
        default=DEFAULT_GAS_UNITS,
        help="Assumed gas units per two-swap route (default: 240000).",
    )
    options = parser.parse_args(argv)

    # The provider and pace come from .env, as the other tools read theirs.
    try:
        from pathlib import Path

        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass

    chosen = settings()

    try:
        result = scan_once(
            EthereumReadOnlyRPC(options.rpc_url),
            amount_usd=options.amount_usd or chosen["amount_usd"],
            gas_units=options.gas_units,
            chosen=chosen,
        )
    except (ValueError, RuntimeError) as error:
        print(f"[DEX scanner] {error}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    print(f"[DEX scanner] {summary(result)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
