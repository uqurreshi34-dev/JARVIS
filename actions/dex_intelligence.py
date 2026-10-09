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
from decimal import Decimal, InvalidOperation
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
TOKENS = {
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "DAI": "0x6b175474e89094c44da98b954eedeac495271d0f",
    "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
}
# The first three pairs are stablecoin/WETH markets; the rest compare stablecoins.
PAIR_CONFIGS = (
    ("USDC", "WETH"),
    ("USDT", "WETH"),
    ("DAI", "WETH"),
    ("USDC", "USDT"),
    ("USDC", "DAI"),
    ("USDT", "DAI"),
)
# Uniswap V2-style pools charge 30 bps. This scanner intentionally supports
# only the two named V2 factories; it does not assume this fee for other DEXes.
POOL_FEE_BPS = 30
GAS_PRICE_SELECTOR = "0x"
SELECTORS = {
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


def _fetch_pool(
    rpc: EthereumReadOnlyRPC,
    dex: str,
    factory: str,
    token_a: str,
    token_b: str,
    block_number: int,
    decimals_cache: dict[str, int],
) -> PoolSnapshot | None:
    pair_data = rpc.contract_call(
        factory,
        "0x" + SELECTORS["getPair"] + _address_word(token_a) + _address_word(token_b),
        block_number,
    )
    pair_address = _decode_address(pair_data)
    if pair_address == ZERO_ADDRESS:
        return None

    # A Uniswap V2 pair (and SushiSwap's, a fork) stores its tokens sorted by address: token0 is the lower.
    # Known without asking the pool, which halves the requests a scan makes of a rate-limited provider.
    token0, token1 = sorted((normalize_address(token_a), normalize_address(token_b)))
    reserve_data = rpc.contract_call(
        pair_address, "0x" + SELECTORS["getReserves"], block_number
    )
    reserve0, reserve1, _timestamp = _decode_words(reserve_data, 3)

    for token in (token0, token1):
        if token not in decimals_cache:
            result = rpc.contract_call(
                token, "0x" + SELECTORS["decimals"], block_number
            )
            decimals = _decode_words(result, 1)[0]
            if decimals > 36:
                raise RuntimeError("Token reports unsupported decimals.")
            decimals_cache[token] = decimals

    return PoolSnapshot(
        dex=dex,
        address=pair_address,
        token0=token0,
        token1=token1,
        reserve0=reserve0,
        reserve1=reserve1,
        decimals0=decimals_cache[token0],
        decimals1=decimals_cache[token1],
        fee_bps=POOL_FEE_BPS,
    )


def _format_decimal(value: Decimal, places: int = 8) -> str:
    quantum = Decimal(1).scaleb(-places)
    return format(value.quantize(quantum), "f")


def scan_once(
    rpc: EthereumReadOnlyRPC,
    amount_usd: Decimal | str | int = DEFAULT_AMOUNT_USD,
    gas_units: int = DEFAULT_GAS_UNITS,
) -> dict:
    """Scan current public pool state and return paper-only arbitrage candidates."""
    try:
        amount_usd = Decimal(str(amount_usd))
    except InvalidOperation:
        raise ValueError("amount_usd must be numeric.") from None
    if not amount_usd.is_finite() or amount_usd <= 0:
        raise ValueError("amount_usd must be finite and greater than zero.")
    if not isinstance(gas_units, int) or not 21_000 <= gas_units <= 2_000_000:
        raise ValueError("gas_units must be between 21000 and 2000000.")

    chain_id = rpc.chain_id()
    if chain_id != CHAIN_ID:
        raise RuntimeError(
            f"This scanner is configured for Ethereum mainnet (chain ID {CHAIN_ID}); "
            f"RPC reported chain ID {chain_id}."
        )
    block_number = rpc.block_number()
    gas_price_wei = rpc.gas_price_wei()

    decimals_cache: dict[str, int] = {}
    pools_by_pair: dict[tuple[str, str], dict[str, PoolSnapshot]] = {}
    pool_count = 0

    for name_a, name_b in PAIR_CONFIGS:
        token_a, token_b = TOKENS[name_a], TOKENS[name_b]
        by_dex: dict[str, PoolSnapshot] = {}
        for dex, factory in FACTORIES:
            pool = _fetch_pool(
                rpc, dex, factory, token_a, token_b, block_number, decimals_cache
            )
            if pool is not None:
                by_dex[dex] = pool
                pool_count += 1
        pools_by_pair[(name_a, name_b)] = by_dex

    # Stablecoins are treated as $1 only for this rough screening estimate.
    # WETH/USD is derived from the current USDC/WETH pool snapshots, where present.
    token_prices: dict[str, Decimal] = {
        TOKENS[name]: Decimal("1") for name in USD_PEG_ASSUMPTIONS
    }
    weth_usd_prices: list[Decimal] = []
    for pair, pools in pools_by_pair.items():
        if set(pair) != {"USDC", "WETH"}:
            continue
        for pool in pools.values():
            weth_usd_prices.append(
                _pool_price(pool, TOKENS["WETH"], TOKENS["USDC"])
            )
    weth_usd = (
        Decimal(str(statistics.median(weth_usd_prices)))
        if weth_usd_prices else None
    )
    if weth_usd is not None and weth_usd > 0:
        token_prices[TOKENS["WETH"]] = weth_usd

    gas_cost_usd = None
    if weth_usd is not None:
        gas_cost_usd = (
            Decimal(gas_price_wei) * Decimal(gas_units)
            / Decimal(10**18) * weth_usd
        )

    opportunities = []
    for (name_a, name_b), pools in pools_by_pair.items():
        if len(pools) < 2:
            continue
        pool_items = list(pools.values())
        pool_one, pool_two = pool_items[0], pool_items[1]
        pair_tokens = (TOKENS[name_a], TOKENS[name_b])

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
            amount_in = human_to_raw(amount_in_human, decimals)

            for first, second in ((pool_one, pool_two), (pool_two, pool_one)):
                try:
                    intermediate, final_amount = simulate_round_trip(
                        amount_in, token_in, first, second
                    )
                except ValueError:
                    continue
                gross_profit_raw = final_amount - amount_in
                if gross_profit_raw <= 0:
                    continue
                gross_profit_token = raw_to_decimal(gross_profit_raw, decimals)
                gross_profit_usd = gross_profit_token * price_usd
                net_profit_usd = (
                    gross_profit_usd - gas_cost_usd
                    if gas_cost_usd is not None else None
                )
                opportunities.append({
                    "pair": f"{name_a}/{name_b}",
                    "buy_on": first.dex,
                    "sell_on": second.dex,
                    "input_token": name_a if token_in == TOKENS[name_a] else name_b,
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
                })

    opportunities.sort(
        key=lambda item: Decimal(
            item["estimated_net_profit_usd"]
            if item["estimated_net_profit_usd"] is not None
            else item["gross_profit_usd"]
        ),
        reverse=True,
    )
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
        "estimated_eth_usd": _format_decimal(weth_usd, 4) if weth_usd else None,
        "stablecoin_usd_assumption": "USDC, USDT and DAI treated as $1 for screening",
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


# What the scanner is about, and what asking it to look sounds like. A request needs both: "scan the DEX",
# "any arbitrage on uniswap", "check defi price gaps" -- not "what is a DEX", "buy ethereum", or arbitrage
# on anything but the decentralised exchanges.
_DEX_SUBJECT = re.compile(
    r"\b(?:dex(?:es)?|defi|decentrali[sz]ed exchanges?|uniswap|sushi ?swap)\b")
_DEX_LOOK = re.compile(
    r"\b(?:scan(?:ner)?|check|find|show|look|search|any|run|what are)\b"
    r"|\b(?:opportunit(?:y|ies)|price gaps?|dislocations?)\b")
_DEX_NOT = re.compile(r"\b(?:what is|what s|what does|explain|meaning of|define|buy|sell|swap my|send)\b")


def asked(command: str) -> bool:
    """Recognise a request to run the read-only DEX scanner: its subject and a request to look."""
    text = " ".join(re.sub(r"[^a-z0-9 ]+", " ", str(command or "").casefold()).split())
    return bool(_DEX_SUBJECT.search(text) and _DEX_LOOK.search(text) and not _DEX_NOT.search(text))


def describe() -> str:
    """Run one read-only scan and return a concise voice response."""
    try:
        report = scan_once(EthereumReadOnlyRPC())
    except (RuntimeError, ValueError) as error:
        print(f"[DEX scanner] {error}", flush=True)
        return f"I couldn't read the exchanges just now, sir: {str(error).split(';')[0]}."

    print(json.dumps(report, indent=2), flush=True)
    opportunities = report["opportunities"]
    block = report["snapshot_block"]
    pool_count = report["pools_read"]
    if not opportunities:
        return (
            f"I scanned {pool_count} Uniswap and SushiSwap pools at Ethereum "
            f"block {block}, sir. No positive gross price dislocation was found "
            "among the configured pairs in this snapshot. No trades were placed."
        )

    best = opportunities[0]
    gross = Decimal(best["gross_profit_usd"])
    net_value = best["estimated_net_profit_usd"]
    route = (
        f"{best['pair']}, buying on {best['buy_on']} and selling on "
        f"{best['sell_on']}"
    )
    if net_value is None:
        return (
            f"The best paper-only route is {route}, sir, with an estimated "
            f"gross difference of ${gross:.4f}. I couldn't estimate gas in USD, "
            "so this is not a net-profit signal. No trades were placed."
        )
    net = Decimal(net_value)
    if net <= 0:
        return (
            f"The best gross paper route is {route}, sir, but after the rough "
            f"gas estimate it loses about ${abs(net):.4f}. No trades were placed."
        )
    return (
        f"The best paper-only route is {route}, sir. The estimate is "
        f"${gross:.4f} gross and ${net:.4f} after approximate gas for a "
        f"${best['input_amount']} {best['input_token']} route. This is only a "
        "reserve-ratio estimate, not an executable quote. No trades were placed."
    )


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
        default=str(DEFAULT_AMOUNT_USD),
        help="Input size to simulate in USD-equivalent terms (default: 1000).",
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

    try:
        result = scan_once(
            EthereumReadOnlyRPC(options.rpc_url),
            amount_usd=options.amount_usd,
            gas_units=options.gas_units,
        )
    except (ValueError, RuntimeError) as error:
        print(f"[DEX scanner] {error}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    if not result["opportunities"]:
        print(
            "[DEX scanner] No positive gross dislocations found in the configured "
            "pairs at this snapshot. This is not proof that no opportunity exists.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
