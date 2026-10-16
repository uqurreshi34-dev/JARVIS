// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
}

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

    function frontRun(address router, bytes calldata swapData)
        external
        payable
        onlyOwner
    {
        (bool ok, ) = router.call{value: msg.value}(swapData);
        if (!ok) revert SwapFailed();
    }

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
