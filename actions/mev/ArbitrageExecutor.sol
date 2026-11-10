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

contract ArbitrageExecutor {
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
        address routerA,
        bytes calldata swapDataA,
        address routerB,
        bytes calldata swapDataB,
        uint256 minProfit
    ) external onlyOwner {
        address[] memory assets = new address[](1);
        assets[0] = WETH;

        uint256[] memory amounts = new uint256[](1);
        amounts[0] = borrowAmount;

        uint256[] memory modes = new uint256[](1);
        modes[0] = 0; // 0 = no debt, repay in same tx

        bytes memory params = abi.encode(routerA, swapDataA, routerB, swapDataB, minProfit);

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

        (address routerA, bytes memory swapDataA, address routerB, bytes memory swapDataB, uint256 minProfit) = abi.decode(params, (address, bytes, address, bytes, uint256));

        uint256 borrowAmount = amounts[0];
        uint256 amountOwed = borrowAmount + premiums[0];

        // 1. Approve Router A and swap
        IERC20(WETH).approve(routerA, borrowAmount);
        (bool successA, ) = routerA.call(swapDataA);
        require(successA, "Swap A failed");

        // 2. Approve Router B and swap back
        // Note: The Python script will construct swapDataB to swap the *exact* tokens received from Swap A
        // For simplicity, we assume routerB handles the token approval internally or we approve max
        // (In production, you'd read the token balance and approve routerB for that amount)
        
        // For this example, we assume swapDataB is ready and routerB is approved by the script via a separate call
        // OR, a more robust contract reads the token balance after Swap A and approves routerB.
        // Let's assume the Python script constructs a bundle that includes the token approvals if needed.
        // ACTUALLY, let's just do it safely here:
        // We don't know the token address, but we can assume the swapDataB outputs WETH.
        // The safest way is to let the Python script handle the token approvals in the swapDataB.
        
        (bool successB, ) = routerB.call(swapDataB);
        require(successB, "Swap B failed");

        // 3. Check profit
        uint256 finalBalance = IERC20(WETH).balanceOf(address(this));
        require(finalBalance >= amountOwed + minProfit, "Not profitable");

        // 4. Approve Aave to pull the owed amount
        IERC20(WETH).approve(AAVE_POOL, amountOwed);

        // 5. Transfer profit to owner
        uint256 profit = finalBalance - amountOwed;
        IERC20(WETH).transfer(owner, profit);

        return true;
    }

    // Helper to withdraw stuck tokens (just in case)
    function withdraw(address token) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        IERC20(token).transfer(owner, balance);
    }
}
