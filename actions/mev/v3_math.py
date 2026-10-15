"""Uniswap V3 single-tick swap math.

Assumes the swap does not cross a tick boundary. Good enough for the
sizes we sandwitch. Tick crossing is a known approximation and can be
added later if a real candidate ever needs it.

All amounts are raw integer units.
"""

Q96 = 2 ** 96
FEE_DENOM = 1_000_000


def get_amount_out_v3(amount_in: int,
                      sqrt_price_x96: int,
                      liquidity: int,
                      fee: int,
                      zero_for_one: bool):
    """Return (amount_out, sqrt_price_x96_new) for an exact-in swap.

    zero_for_one True means token0 in, token1 out.
    """
    if amount_in <= 0 or liquidity <= 0 or sqrt_price_x96 <= 0:
        return 0, sqrt_price_x96

    after_fee = amount_in * (FEE_DENOM - fee) // FEE_DENOM
    if after_fee <= 0:
        return 0, sqrt_price_x96

    if zero_for_one:
        # token0 in, price goes down
        den = liquidity * Q96 + after_fee * sqrt_price_x96
        sp_new = (liquidity * sqrt_price_x96 * Q96) // den
        amount_out = (liquidity * (sqrt_price_x96 - sp_new)) // Q96
    else:
        # token1 in, price goes up
        sp_new = sqrt_price_x96 + (after_fee * Q96) // liquidity
        num = liquidity * Q96 * (sp_new - sqrt_price_x96)
        den = sqrt_price_x96 * sp_new
        amount_out = num // den

    return amount_out, sp_new


def simulate_sandwich_v3(front_in: int,
                         victim_in: int,
                         sqrt_price_x96: int,
                         liquidity: int,
                         fee: int,
                         weth_is_token0: bool):
    """Return (profit_in_weth, victim_out, sp_after_front)."""
    # Front: WETH -> token
    zero_for_one_front = weth_is_token0
    our_tokens, sp1 = get_amount_out_v3(
        front_in, sqrt_price_x96, liquidity, fee, zero_for_one_front)
    if our_tokens <= 0:
        return 0, 0, sqrt_price_x96

    # Victim: same direction as us
    victim_out, sp2 = get_amount_out_v3(
        victim_in, sp1, liquidity, fee, zero_for_one_front)

    # Back: token -> WETH (reverse direction)
    back_eth, _ = get_amount_out_v3(
        our_tokens, sp2, liquidity, fee, not zero_for_one_front)

    profit = back_eth - front_in
    return profit, victim_out, sp1


def optimal_front_run_v3(victim_in: int,
                         victim_min_out: int,
                         sqrt_price_x96: int,
                         liquidity: int,
                         fee: int,
                         weth_is_token0: bool,
                         max_front_in: int,
                         steps: int = 200):
    """Same shape as the V2 version: find the front size that keeps the
    victim above their floor and maximises our profit."""
    if victim_in <= 0 or max_front_in <= 0:
        return 0, 0

    # Largest front that still leaves victim_out >= victim_min_out.
    lo, hi = 0, max_front_in
    while lo < hi:
        mid = (lo + hi + 1) // 2
        _, v_out, _ = simulate_sandwich_v3(
            mid, victim_in, sqrt_price_x96, liquidity, fee, weth_is_token0)
        if v_out >= victim_min_out:
            lo = mid
        else:
            hi = mid - 1
    cap = lo
    if cap == 0:
        return 0, 0

    best_f, best_p = 0, 0
    for i in range(1, steps + 1):
        f = (cap * i) // steps
        if f <= 0:
            continue
        p, _, _ = simulate_sandwich_v3(
            f, victim_in, sqrt_price_x96, liquidity, fee, weth_is_token0)
        if p > best_p:
            best_p, best_f = p, f
    return best_f, best_p
