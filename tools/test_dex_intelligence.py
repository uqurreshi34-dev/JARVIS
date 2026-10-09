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

# commands needs Windows' own modules; elsewhere its routing is skipped, as the other suites do.
try:
    import commands  # noqa: E402
except Exception as error:  # noqa: BLE001
    commands = None
    print(f"SKIP commands routing (could not import commands: {error})")

import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from unittest import mock, skipIf  # noqa: E402
from urllib.error import HTTPError  # noqa: E402


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

    @skipIf(commands is None, "commands needs Windows")
    def test_voice_command_routes_to_read_only_scanner(self):
        result = commands._fast_path("scan DEX opportunities")
        self.assertEqual(result["intent"], "dex_scan")

    def test_scanner_command_rejects_general_information_questions(self):
        self.assertFalse(dex.asked("what is a DEX"))
        self.assertFalse(dex.asked("buy ethereum"))
        self.assertFalse(dex.asked("check arbitrage on gold"))
        self.assertFalse(dex.asked("scan my downloads folder"))
        self.assertTrue(dex.asked("run the DEX scanner"))
        self.assertTrue(dex.asked("jarvis scan the dex"))
        self.assertTrue(dex.asked("any arbitrage on uniswap"))
        self.assertTrue(dex.asked("check defi price gaps"))

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


class Response:
    def __init__(self, result):
        self._body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def refusal(code, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return HTTPError("https://rpc.example", code, "refused", headers, io.BytesIO(b""))


class RpcClientTests(TestCase):
    def client(self, interval=0.0):
        self.slept = []
        self.now = [100.0]
        return dex.EthereumReadOnlyRPC("https://rpc.example/eth", interval=interval,
                                       sleep=self.slept.append, clock=lambda: self.now[0])

    def test_names_itself_so_cloudflare_fronted_providers_answer(self):
        sent = []

        def answer(request, timeout):
            sent.append(request)
            return Response("0x1")

        with mock.patch.object(dex, "urlopen", answer):
            self.assertEqual(self.client().chain_id(), 1)
        self.assertEqual(sent[0].get_header("User-agent"), dex.USER_AGENT)
        self.assertNotIn("Python-urllib", sent[0].get_header("User-agent"))

    def test_requests_are_spaced_by_the_minimum_interval(self):
        client = self.client(interval=0.5)
        with mock.patch.object(dex, "urlopen", lambda request, timeout: Response("0x1")):
            client.chain_id()
            self.now[0] += 0.1
            client.chain_id()
        self.assertEqual(len(self.slept), 1)
        self.assertAlmostEqual(self.slept[0], 0.4)

    def test_too_many_requests_is_asked_again_after_waiting(self):
        answers = [refusal(429, retry_after=3), Response("0x1")]

        def answer(request, timeout):
            reply = answers.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with mock.patch.object(dex, "urlopen", answer):
            self.assertEqual(self.client().chain_id(), 1)
        self.assertIn(3.0, self.slept)

    def test_a_refusal_says_which_provider_request_and_what_to_try(self):
        def answer(request, timeout):
            raise refusal(403)

        with mock.patch.object(dex, "urlopen", answer):
            with self.assertRaises(RuntimeError) as caught:
                self.client().chain_id()
        message = str(caught.exception)
        for part in ("rpc.example", "refused", "HTTP 403", "request 1", "eth_chainId", "ETHEREUM_RPC_URL"):
            self.assertIn(part, message)

    def test_provider_and_pace_come_from_the_environment_when_used(self):
        with mock.patch.dict(os.environ, {"ETHEREUM_RPC_URL": "https://rpc.nodeflare.app/eth/public",
                                          "ETHEREUM_RPC_MIN_INTERVAL": "0.6"}):
            self.assertEqual(dex.rpc_url(), "https://rpc.nodeflare.app/eth/public")
            self.assertEqual(dex.min_interval(), 0.6)
        with mock.patch.dict(os.environ, {"ETHEREUM_RPC_URL": "", "ETHEREUM_RPC_MIN_INTERVAL": "soon"}):
            self.assertEqual(dex.rpc_url(), dex.FALLBACK_RPC_URL)
            self.assertEqual(dex.min_interval(), dex.DEFAULT_MIN_INTERVAL)

    def test_a_failed_scan_is_said_not_crashed(self):
        with mock.patch.object(dex, "scan_once", side_effect=RuntimeError(
                "Ethereum RPC x refused the request (HTTP 403) on request 1 (eth_chainId); try another")):
            said = dex.describe()
        self.assertIn("couldn't read the exchanges", said)
        self.assertNotIn("try another", said)


class ScanTests(TestCase):
    def test_a_dollar_amount_in_weth_is_rounded_to_what_weth_can_hold(self):
        # $100 at an ETH price that does not divide evenly: 18+ decimal places, once refused outright.
        weth = 10**21              # 1,000 WETH (18 decimals)
        usdc = 2_345_678_901_234   # 2,345,678.901234 USDC (6 decimals): $2,345.678901234 an ETH
        places = {dex.TOKENS["USDC"]: 6, dex.TOKENS["USDT"]: 6, dex.TOKENS["DAI"]: 18, dex.TOKENS["WETH"]: 18,
                  dex.TOKENS["WBTC"]: 8}

        class Mainnet:
            def chain_id(self):
                return 1

            def block_number(self):
                return 19_000_000

            def gas_price_wei(self):
                return 1_000_000_000

            def contract_call(self, address, data, _block):
                if data.startswith("0xe6a43905"):
                    return "0x" + "0" * 24 + "ab" * 20
                if data.startswith("0x0902f1ac"):
                    first, second = sorted((dex.TOKENS["USDC"], dex.TOKENS["WETH"]))
                    reserves = (usdc, weth) if first == dex.TOKENS["USDC"] else (weth, usdc)
                    return "0x" + f"{reserves[0]:064x}" + f"{reserves[1]:064x}" + "0" * 64
                if data.startswith("0x313ce567"):
                    return "0x" + f"{places[address]:064x}"
                raise AssertionError(f"Unexpected call data: {data}")

        report = dex.scan_once(Mainnet(), amount_usd="100")
        self.assertEqual(report["requested_trade_size_usd"], "100.00")
        self.assertEqual(report["estimated_eth_usd"], "2345.6789")
        self.assertEqual(dex.whole_units(dex.Decimal(100) / dex.Decimal("2345.678901234"), 18),
                         42631580966769419)
        self.assertEqual(dex.whole_units(dex.Decimal("1E+11"), 18), 10**29)

    def test_pools_are_read_without_asking_their_token_order(self):
        asked = []

        class Mainnet:
            def chain_id(self):
                return 1

            def block_number(self):
                return 19_000_000

            def gas_price_wei(self):
                return 1_000_000_000

            def contract_call(self, address, data, _block):
                asked.append(data[:10])
                if data.startswith("0xe6a43905"):
                    return "0x" + "0" * 24 + "ab" * 20
                if data.startswith("0x0902f1ac"):
                    return "0x" + f"{10**12:064x}" + f"{10**21:064x}" + "0" * 64
                if data.startswith("0x313ce567"):
                    return "0x" + f"{18:064x}"
                raise AssertionError(f"Unexpected call data: {data}")

        report = dex.scan_once(Mainnet(), amount_usd="100")
        self.assertEqual(report["pools_read"], len(dex.PAIR_CONFIGS) * len(dex.FACTORIES))
        self.assertNotIn("0x0dfe1681", asked)
        self.assertNotIn("0xd21220a7", asked)


DECIMALS = {"USDC": 6, "USDT": 6, "DAI": 18, "WETH": 18, "WBTC": 8}
# Dollar prices on Uniswap; SushiSwap the same but for ether, 1% dearer there.
USD = {"USDC": 1, "USDT": 1, "DAI": 1, "WETH": 2500, "WBTC": 62500}


class Chain:
    """A pretend mainnet: every default pair on both exchanges, each pool holding $10m a side."""

    def __init__(self, sushi_eth=1.01):
        self.pools = {}
        self.calls = 0
        for index, (base, quote) in enumerate(dex.PAIR_CONFIGS):
            for which, (_name, factory) in enumerate(dex.FACTORIES):
                address = "0x" + f"{index * 2 + which + 1:040x}"
                lean = sushi_eth if which == 1 else 1
                value = {name: USD[name] * (lean if name == "WETH" else 1) for name in (base, quote)}
                reserves = {dex.TOKENS[name]: int(10_000_000 / value[name] * 10**DECIMALS[name])
                            for name in (base, quote)}
                key = (factory, *sorted((dex.TOKENS[base], dex.TOKENS[quote])))
                self.pools[key] = address
                self.pools[address] = [reserves[token] for token in key[1:]]

    def chain_id(self):
        return 1

    def block_number(self):
        return 21_000_000

    def gas_price_wei(self):
        return 2_000_000_000   # 2 gwei

    def contract_call(self, address, data, _block):
        self.calls += 1
        if data.startswith("0xe6a43905"):
            first, second = "0x" + data[34:74], "0x" + data[98:138]
            found = self.pools.get((address, *sorted((first, second))))
            return "0x" + (found[2:] if found else "0" * 40).rjust(64, "0")
        if data.startswith("0x0902f1ac"):
            reserve0, reserve1 = self.pools[address]
            return "0x" + f"{reserve0:064x}" + f"{reserve1:064x}" + "0" * 64
        if data.startswith("0x313ce567"):
            name = next(name for name, token in dex.TOKENS.items() if token == address)
            return "0x" + f"{DECIMALS[name]:064x}"
        raise AssertionError(f"Unexpected call data: {data}")


class FullScanTests(TestCase):
    def test_every_pair_is_priced_on_both_exchanges_with_its_gap(self):
        steps = []
        report = dex.scan_once(Chain(), amount_usd="1000", progress=lambda *step: steps.append(step))
        markets = {market["pair"]: market for market in report["markets"]}
        self.assertEqual(set(markets), {f"{base}/{quote}" for base, quote in dex.PAIR_CONFIGS})
        self.assertAlmostEqual(float(markets["WETH/USDC"]["prices"]["Uniswap V2"]), 2500, places=2)
        self.assertAlmostEqual(float(markets["WETH/USDC"]["prices"]["SushiSwap V2"]), 2525, places=2)
        self.assertAlmostEqual(float(markets["WETH/USDC"]["gap_pct"]), 1.0, places=3)
        self.assertAlmostEqual(float(markets["USDT/USDC"]["gap_pct"]), 0.0, places=3)
        # Bitcoin's price comes through ether: 25 ETH a bitcoin at $2,500 = $62,500.
        self.assertAlmostEqual(float(report["estimated_btc_usd"]), 62500, delta=5)
        self.assertAlmostEqual(float(report["estimated_eth_usd"]), 2500, delta=15)
        self.assertEqual(steps[-1][1:], (16, 16))

    def test_the_breakeven_is_fees_and_gas_as_a_share_of_the_trade(self):
        report = dex.scan_once(Chain(), amount_usd="1000")
        gas = float(report["estimated_gas_cost_usd"])
        self.assertAlmostEqual(gas, 2e-9 * 240_000 * float(report["estimated_eth_usd"]), places=3)
        self.assertAlmostEqual(float(report["breakeven_gap_pct"]), 0.5991 + gas / 1000 * 100, places=3)

    def test_a_one_percent_gap_on_a_deep_pool_pays_and_is_said(self):
        report = dex.scan_once(Chain(), amount_usd="1000")
        best = next(market for market in report["markets"] if market["pair"] == "WETH/USDC")["best"]
        # Ether is 1% dearer on SushiSwap: bought on Uniswap and sold there, whichever token the trip starts with.
        self.assertEqual((best["buy_base_on"], best["sell_base_on"]), ("Uniswap V2", "SushiSwap V2"))
        self.assertGreater(float(best["estimated_net_profit_usd"]), 0)
        said = dex.summary(report)
        self.assertIn("Buying WETH on Uniswap V2 and selling it on SushiSwap V2 would clear about", said)
        self.assertIn("Nothing was traded", said)

    def test_no_gap_is_said_plainly(self):
        said = dex.summary(dex.scan_once(Chain(sushi_eth=1.0), amount_usd="1000"))
        self.assertIn("Nothing clears it right now", said)
        self.assertNotIn("would clear", said)

    def test_a_scan_is_shown_and_the_hud_told_when_it_is_done(self):
        shown, steps = [], []
        dex.set_listeners(on_scan=shown.append, on_progress=lambda *step: steps.append(step))
        try:
            with mock.patch.object(dex, "EthereumReadOnlyRPC", lambda: Chain()), \
                    mock.patch.object(dex, "settings", lambda: {"tokens": dex.TOKENS,
                                                                "pairs": list(dex.PAIR_CONFIGS),
                                                                "amount_usd": dex.Decimal(1000)}):
                said = dex.describe()
        finally:
            dex.set_listeners()
        self.assertEqual(len(shown), 1)
        self.assertIn("markets", shown[0])
        self.assertEqual(steps[-1], ("", 0, 0))
        self.assertIn("Uniswap and SushiSwap", said)

    def test_closing_the_panel_is_heard_and_not_taken_for_a_scan(self):
        self.assertTrue(dex.dismissed("close the dex scan"))
        self.assertTrue(dex.dismissed("hide the DEX panel"))
        self.assertFalse(dex.dismissed("close the trade plan"))
        self.assertFalse(dex.asked("close the dex scan"))


class PanelTests(TestCase):
    def test_the_panel_rows_say_what_the_scan_found(self):
        import os as _os

        _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import dex_panel

        table = {row["pair"]: row for row in dex_panel.rows(dex.scan_once(Chain(), amount_usd="1000"))}
        self.assertEqual(table["WETH/USDC"]["prices"], ["2,500.00", "2,525.00"])
        self.assertTrue(table["WETH/USDC"]["clears"])
        self.assertEqual(table["WETH/USDC"]["route"], "buy Uniswap, sell SushiSwap")
        self.assertFalse(table["USDT/USDC"]["clears"])
        self.assertEqual(table["USDT/USDC"]["route"], "no gap")
        self.assertLess(table["USDT/USDC"]["net"], 0)
        self.assertEqual(dex_panel.price_text("0.000012345678"), "0.0000123457")
        self.assertEqual(dex_panel.money_text("-7.394"), "-$7.39")


class SettingsTests(TestCase):
    def test_tokens_and_pairs_come_from_the_file_and_nonsense_is_left_out(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, dex.SETTINGS_NAME)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"tokens": {"link": "0x514910771af9ca656af840dff83e8264ecf986ca", "BAD": "nope"},
                           "pairs": [["LINK", "WETH"], ["BAD", "WETH"], ["WETH", "WETH"]],
                           "amount_usd": "250"}, handle)
            with mock.patch.object(dex, "_settings_path", lambda: path):
                chosen = dex.settings()
        self.assertIn("LINK", chosen["tokens"])
        self.assertNotIn("BAD", chosen["tokens"])
        self.assertEqual(chosen["pairs"], [("LINK", "WETH")])
        self.assertEqual(chosen["amount_usd"], dex.Decimal("250"))

    def test_the_defaults_are_written_out_the_first_time(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, dex.SETTINGS_NAME)
            with mock.patch.object(dex, "_settings_path", lambda: path):
                chosen = dex.settings()
            with open(path, encoding="utf-8") as handle:
                written = json.load(handle)
        self.assertIn(("WBTC", "WETH"), chosen["pairs"])
        self.assertIn("WBTC", written["tokens"])

    def test_the_folder_tidier_leaves_the_settings_alone(self):
        from actions import folder_organizer

        self.assertTrue(folder_organizer.is_protected(dex.SETTINGS_NAME))


if __name__ == "__main__":
    raise SystemExit(unittest_main())
