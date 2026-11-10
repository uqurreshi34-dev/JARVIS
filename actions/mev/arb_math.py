"""Two-pool arbitrage maths for constant-product (x * y = k) pools.

Pure integer maths, no web3, so it can be tested anywhere. Amounts are in
the tokens' smallest units (wei for WETH). Fees are in basis points
(30 = 0.30%), the unit Uniswap V2 forks and Aerodrome both use.

A round trip starts and ends in WETH: buy the token on the cheap pool,
sell it on the dear pool, repay the loan plus its fee, keep the rest.
"""
from __future__ import annotations

from dataclasses import dataclass

BPS = 10_000


@dataclass(frozen=True)
class PoolState:
    """One pool as the maths needs it: WETH side, token side, swap fee."""
    label: str
    weth: int
    token: int
    fee_bps: int


@dataclass(frozen=True)
class Opportunity:
    buy_on: str           # pool label where WETH buys the token
    sell_on: str          # pool label where the token is sold for WETH
    amount_in: int        # WETH borrowed and spent (wei)
    tokens: int           # tokens bought on the first pool
    weth_out: int         # WETH back from the second pool (wei)
    loan_fee: int         # flash-loan premium (wei)
    gross_profit: int     # weth_out - amount_in - loan_fee (wei), before gas


def amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
    """Uniswap V2's getAmountOut, exactly, in integers."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    if not 0 <= fee_bps < BPS:
        raise ValueError(f"fee_bps out of range: {fee_bps}")
    in_with_fee = amount_in * (BPS - fee_bps)
    return (in_with_fee * reserve_out) // (reserve_in * BPS + in_with_fee)


def loan_fee(amount: int, loan_fee_bps: int) -> int:
    """Flash-loan premium, rounded up so the estimate never flatters us."""
    return -(-amount * loan_fee_bps // BPS)


def price_weth_per_token(pool: PoolState) -> float:
    """Spot price, for display only (never used for trade sizing)."""
    return pool.weth / pool.token if pool.token else 0.0


def gap_pct(a: PoolState, b: PoolState) -> float:
    """How far apart the two spot prices are, in percent of the lower one."""
    pa, pb = price_weth_per_token(a), price_weth_per_token(b)
    low = min(pa, pb)
    return (abs(pa - pb) / low * 100) if low else 0.0


def hurdle_pct(a: PoolState, b: PoolState, loan_fee_bps: int) -> float:
    """Price gap needed before the first wei of a round trip is profitable."""
    keep = (BPS - a.fee_bps) * (BPS - b.fee_bps) / (BPS * BPS)
    return ((1 + loan_fee_bps / BPS) / keep - 1) * 100


def _profit(x: int, buy: PoolState, sell: PoolState, loan_fee_bps: int) -> int:
    tokens = amount_out(x, buy.weth, buy.token, buy.fee_bps)
    back = amount_out(tokens, sell.token, sell.weth, sell.fee_bps)
    return back - x - loan_fee(x, loan_fee_bps)


def _worth_trying(buy: PoolState, sell: PoolState, loan_fee_bps: int) -> bool:
    """True only if the first tiny trade gains: the marginal rate at zero beats the costs.

    (1-fa)(1-fb) * (token_buy / weth_buy) * (weth_sell / token_sell) > 1 + loan
    written in integers so nothing is lost to floats.
    """
    lhs = (BPS - buy.fee_bps) * (BPS - sell.fee_bps) * buy.token * sell.weth * BPS
    rhs = BPS * BPS * (BPS + loan_fee_bps) * buy.weth * sell.token
    return lhs > rhs


def best_one_way(buy: PoolState, sell: PoolState, loan_fee_bps: int,
                 max_in: int) -> Opportunity | None:
    """Best WETH amount to buy on [buy] and sell on [sell], or None.

    Profit in the amount is concave (one hump), so a ternary search finds
    the top in about a hundred steps, then a short walk settles rounding.
    """
    if min(buy.weth, buy.token, sell.weth, sell.token) <= 0 or max_in <= 0:
        return None
    if not _worth_trying(buy, sell, loan_fee_bps):
        return None

    lo, hi = 1, max_in
    while hi - lo > 2:
        m1 = lo + (hi - lo) // 3
        m2 = hi - (hi - lo) // 3
        if _profit(m1, buy, sell, loan_fee_bps) < _profit(m2, buy, sell, loan_fee_bps):
            lo = m1 + 1
        else:
            hi = m2 - 1
    best_x = max(range(lo, hi + 1), key=lambda x: _profit(x, buy, sell, loan_fee_bps))
    best_p = _profit(best_x, buy, sell, loan_fee_bps)
    if best_p <= 0:
        return None

    tokens = amount_out(best_x, buy.weth, buy.token, buy.fee_bps)
    back = amount_out(tokens, sell.token, sell.weth, sell.fee_bps)
    return Opportunity(buy_on=buy.label, sell_on=sell.label, amount_in=best_x,
                       tokens=tokens, weth_out=back,
                       loan_fee=loan_fee(best_x, loan_fee_bps), gross_profit=best_p)


def best_round_trip(a: PoolState, b: PoolState, loan_fee_bps: int,
                    max_in: int) -> Opportunity | None:
    """Check both directions (A->B and B->A) and return the better one, if any."""
    found = [o for o in (best_one_way(a, b, loan_fee_bps, max_in),
                         best_one_way(b, a, loan_fee_bps, max_in)) if o]
    return max(found, key=lambda o: o.gross_profit) if found else None
