"""Offline tests for the read-only Ethereum DEX intelligence module."""

import sys
from pathlib import Path
from unittest import TestCase, main as unittest_main

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402

sandbox.activate()

from actions import dex_intelligence as dex  # noqa: E402
import commands  # noqa: E402


USDC = dex.TOKENS["USDC"]
WETH = dex.TOKENS["WETH"]


def pool(name, usdc_reserve, weth_reserve):
    return dex.PoolSnapshot(
        dex=name,
        address="0x" + ("1" if name == "A" else "2") * 40,
        token0=USDC,
        token1=WETH,
        reserve0=usdc_reserve * (10**6),
        reserve1=weth_reserve * (10**18),
        decimals0=6,
        decimals1=18,
    )


class DexMathTests(TestCase):
    def test_normalize_address_accepts_case_and_rejects_bad_input(self):
        self.assertEqual(dex.normalize_address(USDC), USDC)
        with self.assertRaises(ValueError):
            dex.normalize_address("not-an-address")
        with self.assertRaises(ValueError):
            dex.normalize_address("0x1234")

    def test_human_amount_conversion_is_exact(self):
        self.assertEqual(dex.human_to_raw("12.345678", 6), 12345678)
        self.assertEqual(dex.raw_to_decimal(12345678, 6), dex.Decimal("12.345678"))
        with self.assertRaises(ValueError):
            dex.human_to_raw("0.0000001", 6)
        with self.assertRaises(ValueError):
            dex.human_to_raw("-1", 18)

    def test_constant_product_quote_charges_fee(self):
        with_fee = dex.quote_v2_exact_input(10_000, 1_000_000, 2_000_000, 30)
        without_fee = dex.quote_v2_exact_input(10_000, 1_000_000, 2_000_000, 0)
        self.assertGreater(without_fee, with_fee)
        self.assertGreater(with_fee, 0)
        with self.assertRaises(ValueError):
            dex.quote_v2_exact_input(0, 1_000_000, 2_000_000)

    def test_dislocation_can_show_gross_round_trip_opportunity(self):
        expensive_eth = pool("A", 1_000_000, 500)
        cheaper_eth = pool("B", 1_000_000, 510)
        input_amount = 1_000 * (10**6)
        _intermediate, final_amount = dex.simulate_round_trip(
            input_amount, USDC, cheaper_eth, expensive_eth
        )
        self.assertGreater(final_amount, input_amount)

    def test_identical_pools_do_not_create_free_profit(self):
        first = pool("A", 1_000_000, 500)
        second = pool("B", 1_000_000, 500)
        input_amount = 1_000 * (10**6)
        _intermediate, final_amount = dex.simulate_round_trip(
            input_amount, USDC, first, second
        )
        self.assertLess(final_amount, input_amount)

    def test_round_trip_rejects_different_pairs(self):
        first = pool("A", 1_000_000, 500)
        other = dex.PoolSnapshot(
            dex="other",
            address="0x" + "3" * 40,
            token0=dex.TOKENS["USDT"],
            token1=WETH,
            reserve0=1_000_000 * (10**6),
            reserve1=500 * (10**18),
            decimals0=6,
            decimals1=18,
        )
        with self.assertRaises(ValueError):
            dex.simulate_round_trip(1_000_000, USDC, first, other)

    def test_wrong_network_is_rejected_before_reading_pools(self):
        class WrongNetwork:
            def chain_id(self):
                return 11155111

            def block_number(self):
                raise AssertionError("Must stop at chain ID validation.")

        with self.assertRaisesRegex(RuntimeError, "Ethereum mainnet"):
            dex.scan_once(WrongNetwork())

    def test_voice_command_routes_to_read_only_scanner(self):
        result = commands._fast_path("scan DEX opportunities")
        self.assertEqual(result["intent"], "dex_scan")

    def test_scanner_command_rejects_general_information_questions(self):
        self.assertFalse(dex.asked("what is a DEX"))
        self.assertFalse(dex.asked("buy ethereum"))
        self.assertTrue(dex.asked("run the DEX scanner"))

    def test_rpc_client_has_no_transaction_submission_methods(self):
        client = dex.EthereumReadOnlyRPC("https://ethereum-rpc.publicnode.com")
        with self.assertRaisesRegex(ValueError, "not permitted"):
            client._request("eth_sendRawTransaction", ["0xdeadbeef"])
        with self.assertRaisesRegex(ValueError, "not permitted"):
            client._request("eth_sendTransaction", [{}])

    def test_no_trade_amount_and_gas_inputs_are_accepted(self):
        class Mainnet:
            def chain_id(self):
                return 1

            def block_number(self):
                return 19_000_000

            def gas_price_wei(self):
                return 1_000_000_000

            def contract_call(self, _address, data, _block):
                if data.startswith("0xe6a43905"):
                    return "0x" + "0" * 64
                raise AssertionError(f"Unexpected call data: {data}")

        with self.assertRaises(ValueError):
            dex.scan_once(Mainnet(), amount_usd="-5")
        with self.assertRaises(ValueError):
            dex.scan_once(Mainnet(), gas_units=20_000)


if __name__ == "__main__":
    raise SystemExit(unittest_main())
