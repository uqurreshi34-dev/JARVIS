# Computes the optimal front-run size for a V2 pool given the victim's trade and their slippage limit.

"""Uniswap V2 sandwich math.

All amounts are raw integer token units. Reserves correspond to the
pool the victim is trading in, ordered as (reserve_in, reserve_out)
matching the victim's direction.
"""


def get_amount_out(amount_in: int, reserve_in: int, reserve_out: int) -> int:
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    a = amount_in * 997
    return (a * reserve_out) // (reserve_in * 1000 + a)


def get_amount_in(amount_out: int, reserve_in: int, reserve_out: int) -> int:
    if amount_out <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    return (reserve_in * amount_out * 1000) // ((reserve_out - amount_out) * 997) + 1


def simulate_sandwich(front_in: int, victim_in: int,
                      reserve_in: int, reserve_out: int):
    """Return (profit_in_raw, victim_out, reserves_after_front)."""
    # 1. Our front-run: swap front_in of token_in → token_out
    our_out = get_amount_out(front_in, reserve_in, reserve_out)
    r_in_1 = reserve_in + front_in
    r_out_1 = reserve_out - our_out

    # 2. Victim swaps victim_in
    victim_out = get_amount_out(victim_in, r_in_1, r_out_1)
    r_in_2 = r_in_1 + victim_in
    r_out_2 = r_out_1 - victim_out

    # 3. We back-run: sell our_out of token_out back into token_in
    back_in = get_amount_out(our_out, r_out_2, r_in_2)

    profit = back_in - front_in
    return profit, victim_out, r_in_1, r_out_1


def optimal_front_run(victim_in: int,
                      victim_min_out: int,
                      reserve_in: int, reserve_out: int,
                      max_front_in: int,
                      steps: int = 200):
    """Binary-ish search for the front-run size that maximises profit
    while keeping the victim's output above their slippage floor."""
    if victim_in <= 0:
        return 0, 0

    # Upper bound: largest front_in such that victim_out >= victim_min_out
    lo, hi = 0, max_front_in
    while lo < hi:
        mid = (lo + hi + 1) // 2
        _, v_out, _, _ = simulate_sandwich(
            mid, victim_in, reserve_in, reserve_out)
        if v_out >= victim_min_out:
            lo = mid
        else:
            hi = mid - 1
    hard_cap = lo
    if hard_cap == 0:
        return 0, 0

    # Scan for max profit under the cap
    best_f, best_p = 0, 0
    for i in range(1, steps + 1):
        f = (hard_cap * i) // steps
        if f <= 0:
            continue
        p, _, _, _ = simulate_sandwich(f, victim_in, reserve_in, reserve_out)
        if p > best_p:
            best_p, best_f = p, f
    return best_f, best_p
