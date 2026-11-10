"""Uniswap V3 math for cross-pool arbitrage."""
Q96 = 2 ** 96


def get_amount_out_v3(amount_in: int, sqrt_price_x96: int, liquidity: int, fee: int, zero_for_one: bool):
    if amount_in <= 0 or liquidity <= 0 or sqrt_price_x96 <= 0:
        return 0
    after_fee = amount_in * (1000000 - fee) // 1000000
    if zero_for_one:
        den = liquidity * Q96 + after_fee * sqrt_price_x96
        sp_new = (liquidity * sqrt_price_x96 * Q96) // den
        return (liquidity * (sqrt_price_x96 - sp_new)) // Q96
    else:
        sp_new = sqrt_price_x96 + (after_fee * Q96) // liquidity
        num = liquidity * Q96 * (sp_new - sqrt_price_x96)
        den = sqrt_price_x96 * sp_new
        return num // den


def calculate_v3_profit(amount_in_weth: int, pool_a: dict, pool_b: dict):
    # Direction 1: Buy on A, Sell on B
    tokens_out_ab = get_amount_out_v3(
        amount_in_weth, pool_a["sqrtPriceX96"], pool_a["liquidity"], pool_a["fee"], pool_a["weth_is_token0"]
    )
    profit_ab = 0
    if tokens_out_ab > 0:
        weth_out_ab = get_amount_out_v3(
            tokens_out_ab, pool_b["sqrtPriceX96"], pool_b["liquidity"], pool_b["fee"], not pool_b["weth_is_token0"]
        )
        loan_fee = (amount_in_weth * 5) // 10000
        profit_ab = weth_out_ab - amount_in_weth - loan_fee

    # Direction 2: Buy on B, Sell on A
    tokens_out_ba = get_amount_out_v3(
        amount_in_weth, pool_b["sqrtPriceX96"], pool_b["liquidity"], pool_b["fee"], pool_b["weth_is_token0"]
    )
    profit_ba = 0
    if tokens_out_ba > 0:
        weth_out_ba = get_amount_out_v3(
            tokens_out_ba, pool_a["sqrtPriceX96"], pool_a["liquidity"], pool_a["fee"], not pool_a["weth_is_token0"]
        )
        loan_fee = (amount_in_weth * 5) // 10000
        profit_ba = weth_out_ba - amount_in_weth - loan_fee

    # Return the best of the two directions
    return max(profit_ab, profit_ba)


def optimal_arb_amount_v3(max_in_weth: int, pool_a: dict, pool_b: dict):
    best_amount = 0
    best_profit = 0
    steps = 100
    for i in range(1, steps + 1):
        amount = (max_in_weth * i) // steps
        if amount == 0:
            continue
        profit = calculate_v3_profit(amount, pool_a, pool_b)
        if profit > best_profit:
            best_profit = profit
            best_amount = amount

    return best_amount, best_profit
