// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IPool {
    function flashLoan(
        address receiverAddress,
        address[] calldata assets,
        uint256[] calldata amounts,
        uint256[] calldata interestRateModes,
        address onBehalfOf,
        bytes calldata params,
        uint16 referralCode
    ) external;
}

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address recipient, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

contract ArbitrageExecutorV3 {
    address public owner;
    address public constant AAVE_POOL = 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5; // Aave V3 on Base
    address public constant WETH = 0x4200000000000000000000000000000000000006;

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    // Initiate the flashloan
    function executeArbitrage(
        uint256 borrowAmount,
        address tokenOut, // The token we are buying/selling (e.g., TOSHI)
        address routerA,
        uint24 feeA,
        address routerB,
        uint24 feeB,
        uint256 minProfit
    ) external onlyOwner {
        address[] memory assets = new address[](1);
        assets[0] = WETH;

        uint256[] memory amounts = new uint256[](1);
        amounts[0] = borrowAmount;

        uint256[] memory modes = new uint256[](1);
        modes[0] = 0; // 0 = no debt, repay in same tx

        bytes memory params = abi.encode(tokenOut, routerA, feeA, routerB, feeB, minProfit);

        IPool(AAVE_POOL).flashLoan(
            address(this),
            assets,
            amounts,
            modes,
            address(this),
            params,
            0
        );
    }

    // Aave callback
    function executeOperation(
        address[] calldata assets,
        uint256[] calldata amounts,
        uint256[] calldata premiums,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        require(msg.sender == AAVE_POOL, "Caller must be Aave");
        require(initiator == address(this), "Initiator must be this contract");

        (
            address tokenOut,
            address routerA,
            uint24 feeA,
            address routerB,
            uint24 feeB,
            uint256 minProfit
        ) = abi.decode(params, (address, address, uint24, address, uint24, uint256));

        uint256 borrowAmount = amounts[0];
        uint256 amountOwed = borrowAmount + premiums[0];

        // 1. Approve Router A and swap WETH -> Token
        IERC20(WETH).approve(routerA, borrowAmount);
        ISwapRouter.ExactInputSingleParams memory paramsA = ISwapRouter.ExactInputSingleParams({
            tokenIn: WETH,
            tokenOut: tokenOut,
            fee: feeA,
            recipient: address(this),
            deadline: block.timestamp + 300,
            amountIn: borrowAmount,
            amountOutMinimum: 0, // In production, calculate this to prevent sandwiching
            sqrtPriceLimitX96: 0
        });
        uint256 tokensReceived = ISwapRouter(routerA).exactInputSingle(paramsA);

        // 2. Approve Router B and swap Token -> WETH
        IERC20(tokenOut).approve(routerB, tokensReceived);
        ISwapRouter.ExactInputSingleParams memory paramsB = ISwapRouter.ExactInputSingleParams({
            tokenIn: tokenOut,
            tokenOut: WETH,
            fee: feeB,
            recipient: address(this),
            deadline: block.timestamp + 300,
            amountIn: tokensReceived,
            amountOutMinimum: amountOwed + minProfit, // Must pay back loan + fee + profit
            sqrtPriceLimitX96: 0
        });
        ISwapRouter(routerB).exactInputSingle(paramsB);

        // 3. Check profit
        uint256 finalBalance = IERC20(WETH).balanceOf(address(this));
        require(finalBalance >= amountOwed + minProfit, "Not profitable");

        // 4. Approve Aave to pull the owed amount
        IERC20(WETH).approve(AAVE_POOL, amountOwed);

        // 5. Transfer profit to owner
        uint256 profit = finalBalance - amountOwed;
        if (profit > 0) {
            IERC20(WETH).transfer(owner, profit);
        }

        return true;
    }

    // Helper to withdraw stuck tokens
    function withdraw(address token) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        IERC20(token).transfer(owner, balance);
    }
}
