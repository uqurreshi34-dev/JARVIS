// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
}

/// Holds the middle of a sandwich.
/// frontRun buys the token with ETH, keeps it here.
/// backRun approves the router, sells the token back to ETH, sends ETH home.
contract SandwichExecutor {
    address public immutable owner;

    error NotOwner();
    error SwapFailed();
    error NoProfit();
    error SendFailed();

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    /// Buy leg. router is the V2 router, V3 router, or UniversalRouter.
    /// swapData is the prebuilt router calldata with recipient = this contract.
    function frontRun(address router, bytes calldata swapData)
        external
        payable
        onlyOwner
    {
        (bool ok, ) = router.call{value: msg.value}(swapData);
        if (!ok) revert SwapFailed();
    }

    /// Sell leg. Approves the router for the token, then swaps.
    /// Reverts if we do not clear minProfit in ETH.
    function backRun(
        address router,
        address tokenIn,
        bytes calldata swapData,
        uint256 minProfit
    ) external onlyOwner {
        IERC20(tokenIn).approve(router, type(uint256).max);

        uint256 before = address(this).balance;
        (bool ok, ) = router.call(swapData);
        if (!ok) revert SwapFailed();

        uint256 afterBal = address(this).balance;
        if (afterBal < before + minProfit) revert NoProfit();

        (bool sent, ) = payable(owner).call{value: address(this).balance}("");
        if (!sent) revert SendFailed();
    }

    receive() external payable {}
}
