"""Offline tests for the read-only cross-DEX arbitrage scanner (maths, pool identification, runner)."""

import sys
from pathlib import Path
from unittest import TestCase, main as unittest_main

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import sandbox  # noqa: E402

sandbox.activate()

from actions.mev import arb_math as m  # noqa: E402
from actions.mev import arb_monitor as mon  # noqa: E402
from actions import arb_runner  # noqa: E402

E18 = 10 ** 18
TOKEN = "0x4ed4e862860bed51a9570b96d89af5e1b0efefed"
UNI_FACTORY = "0x8909dc15e40173ff4699343b6eb8132c65e18ec6"


def pool(label, weth_eth, token_units, fee=30):
    return m.PoolState(label, int(weth_eth * E18), int(token_units * E18), fee)


class MathTests(TestCase):
    def test_amount_out_matches_uniswap_v2(self):
        # Uniswap V2: in*997*rOut / (rIn*1000 + in*997)
        a_in, r_in, r_out = 10 ** 18, 50 * 10 ** 18, 100_000 * 10 ** 18
        expected = (a_in * 997 * r_out) // (r_in * 1000 + a_in * 997)
        self.assertEqual(m.amount_out(a_in, r_in, r_out, 30), expected)

    def test_bad_inputs_give_zero(self):
        self.assertEqual(m.amount_out(0, 1, 1, 30), 0)
        self.assertEqual(m.amount_out(1, 0, 1, 30), 0)

    def test_same_price_is_never_an_arb(self):
        a, b = pool("A", 10, 12_500_000), pool("B", 50, 62_500_000)
        self.assertIsNone(m.best_round_trip(a, b, 5, 10 * E18))

    def test_gap_below_fees_is_not_an_arb(self):
        # 0.25% apart (like the screenshot) against ~0.65% of fees.
        a, b = pool("A", 0.8010, 1000), pool("B", 0.7990, 1000)
        self.assertLess(m.gap_pct(a, b), m.hurdle_pct(a, b, 5))
        self.assertIsNone(m.best_round_trip(a, b, 5, 10 * E18))

    def test_big_gap_finds_profit_and_direction(self):
        cheap, dear = pool("A", 10, 12_000_000), pool("B", 10, 10_000_000)
        opp = m.best_round_trip(cheap, dear, 5, 10 * E18)
        self.assertIsNotNone(opp)
        self.assertEqual((opp.buy_on, opp.sell_on), ("A", "B"))
        self.assertGreater(opp.gross_profit, 0)
        # Reported profit is exactly what the two swaps and the loan fee give.
        tokens = m.amount_out(opp.amount_in, cheap.weth, cheap.token, 30)
        back = m.amount_out(tokens, dear.token, dear.weth, 30)
        self.assertEqual(opp.gross_profit, back - opp.amount_in - m.loan_fee(opp.amount_in, 5))
        # And it is the top of the hump: a bit more or less does no better.
        for x in (opp.amount_in * 9 // 10, opp.amount_in * 11 // 10):
            self.assertLessEqual(m._profit(x, cheap, dear, 5), opp.gross_profit)

    def test_reverse_direction_is_found(self):
        opp = m.best_round_trip(pool("A", 10, 10_000_000), pool("B", 10, 12_000_000), 5, 10 * E18)
        self.assertEqual((opp.buy_on, opp.sell_on), ("B", "A"))

    def test_size_cap_is_respected(self):
        opp = m.best_round_trip(pool("A", 10, 12_000_000), pool("B", 10, 10_000_000), 5, E18 // 100)
        self.assertLessEqual(opp.amount_in, E18 // 100)

    def test_loan_fee_rounds_up(self):
        self.assertEqual(m.loan_fee(1, 5), 1)
        self.assertEqual(m.loan_fee(10_000, 5), 5)


class FakeReader:
    """A pretend chain: {address: {function: value or Exception}}."""

    def __init__(self, contracts):
        self.contracts = {k.lower(): v for k, v in contracts.items()}

    def has_code(self, address):
        return address.lower() in self.contracts

    def call(self, address, name, *args):
        value = self.contracts.get(address.lower(), {}).get(name, RuntimeError("execution reverted"))
        if isinstance(value, Exception):
            raise value
        return value(*args) if callable(value) else value


V2 = "0x" + "a" * 40
AERO = "0x" + "b" * 40
CL = "0x" + "c" * 40
STABLE = "0x" + "d" * 40
ODD = "0x" + "e" * 40


def chain():
    return FakeReader({
        V2: {"token0": mon.WETH, "token1": TOKEN, "factory": UNI_FACTORY,
             "getReserves": (2 * E18, 2_500_000 * E18, 0)},
        AERO: {"token0": TOKEN, "token1": mon.WETH, "factory": mon.AERODROME_FACTORY,
               "getReserves": (3_000_000 * E18, 3 * E18, 0), "stable": False},
        CL: {"token0": mon.WETH, "token1": TOKEN, "slot0": (1, 0)},
        STABLE: {"token0": mon.WETH, "token1": TOKEN, "getReserves": (1, 1, 0), "stable": True,
                 "factory": mon.AERODROME_FACTORY},
        ODD: {"token0": mon.WETH, "token1": TOKEN, "getReserves": (1, 1, 0), "factory": "0x" + "f" * 40},
        mon.AERODROME_FACTORY: {"getFee": lambda pool, stable: 30},
        mon.AAVE_POOL: {"FLASHLOAN_PREMIUM_TOTAL": 5},
    })


class MonitorTests(TestCase):
    def setUp(self):
        self.monitor = mon.ArbMonitor(chain(), TOKEN)

    def test_v2_and_aerodrome_are_identified(self):
        a = self.monitor.describe(V2, "A")
        b = self.monitor.describe(AERO, "B")
        self.assertEqual((a.dex, a.fee_bps, a.weth_is_token0), ("Uniswap V2", 30, True))
        self.assertEqual((b.dex, b.fee_bps, b.weth_is_token0), ("Aerodrome", 30, False))

    def test_reserves_are_oriented_weth_first(self):
        b = self.monitor.describe(AERO, "B")
        s = self.monitor.snapshot(b)
        self.assertEqual((s.weth, s.token), (3 * E18, 3_000_000 * E18))

    def test_concentrated_pool_is_explained_not_reverted(self):
        with self.assertRaises(mon.PoolNotSupported) as caught:
            self.monitor.describe(CL, "A")
        self.assertIn("concentrated-liquidity", str(caught.exception))
        self.assertIn("Pool A", str(caught.exception))

    def test_stable_pool_is_refused(self):
        with self.assertRaisesRegex(mon.PoolNotSupported, "STABLE"):
            self.monitor.describe(STABLE, "B")

    def test_unknown_factory_is_refused_unless_overridden(self):
        with self.assertRaisesRegex(mon.PoolNotSupported, "ARB_FEE_OVERRIDES"):
            self.monitor.describe(ODD, "B")
        allowed = mon.ArbMonitor(chain(), TOKEN, fee_overrides={ODD: 25})
        self.assertEqual(allowed.describe(ODD, "B").fee_bps, 25)

    def test_token_address_instead_of_pair_is_explained(self):
        reader = chain()
        reader.contracts[TOKEN] = {"symbol": "DEGEN"}
        with self.assertRaisesRegex(mon.PoolNotSupported, "not a pool"):
            mon.ArbMonitor(reader, TOKEN).describe(TOKEN, "A")

    def test_wrong_token_pair_is_refused(self):
        with self.assertRaisesRegex(mon.PoolNotSupported, "not WETH with"):
            mon.ArbMonitor(chain(), "0x" + "9" * 40).describe(V2, "A")

    def test_missing_contract(self):
        with self.assertRaisesRegex(mon.PoolNotSupported, "no contract"):
            self.monitor.describe("0x" + "1" * 40, "A")

    def test_loan_fee_is_read_live(self):
        self.assertEqual(self.monitor.loan_fee_bps(default=9), 5)

    def test_check_finds_the_gap_between_real_shaped_pools(self):
        a = self.monitor.describe(V2, "A")    # 1,250,000 tokens per WETH (cheap)
        b = self.monitor.describe(AERO, "B")  # 1,000,000 tokens per WETH (dear)
        sa, sb, opp = self.monitor.check(a, b, 5, 10 * E18)
        self.assertEqual((opp.buy_on, opp.sell_on), ("A", "B"))
        self.assertLessEqual(opp.amount_in, min(sa.weth, sb.weth) // 3)


class RunnerTests(TestCase):
    def test_no_hardcoded_user_path(self):
        source = (ROOT / "actions" / "arb_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("C:\\Users", source)

    def test_disarm_when_idle_is_quiet(self):
        seen = []
        arb_runner.disarm(seen.append)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    raise SystemExit(unittest_main())
