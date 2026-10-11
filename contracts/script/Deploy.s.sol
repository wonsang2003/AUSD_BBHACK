// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Script, console2} from "forge-std/Script.sol";

import {BudgetController} from "../src/BudgetController.sol";

/**
 * @title  Deploy
 * @notice Deploys PocketDesk's BudgetController to Base Sepolia (or a local anvil node).
 *
 * Inputs (environment variables):
 *   AUSD_ADDRESS        AUSD token the controller pulls from (testnet: 0xa9012a055bd4e0eDfF8Ce09f960291C09D5322dC)
 *   KEEPER_ADDRESS      relayer wallet allowed to call openBudget / recharge / ...WithSig
 *   RELAYER_PRIVATE_KEY optional; signs the deployment. Without it, pass `--private-key` (or
 *                       `--account`) to forge instead. The deployer becomes the controller's admin.
 *
 * Usage:
 *   forge script script/Deploy.s.sol:Deploy --rpc-url $RPC_URL_BASE_SEPOLIA --broadcast
 *
 * Then put the printed address in BUDGET_CONTROLLER_ADDRESS (backend) and
 * NEXT_PUBLIC_BUDGET_CONTROLLER_ADDRESS (web). The private key is never logged.
 */
contract Deploy is Script {
    /// @dev Testnet only: Base Sepolia, or a local anvil node (default 31337, or 84532 via --chain-id).
    uint256 private constant BASE_SEPOLIA_CHAIN_ID = 84532;
    uint256 private constant ANVIL_DEFAULT_CHAIN_ID = 31337;

    /// @dev Sentinel meaning "RELAYER_PRIVATE_KEY is not set".
    uint256 private constant NO_PRIVATE_KEY = 0;

    error UnsupportedChain(uint256 chainId);
    error AusdHasNoCode(address ausd);

    function run() external returns (BudgetController controller) {
        _requireTestnet();
        address ausd = vm.envAddress("AUSD_ADDRESS");
        address keeper = vm.envAddress("KEEPER_ADDRESS");
        if (ausd.code.length == 0) revert AusdHasNoCode(ausd);

        _startBroadcast();
        controller = new BudgetController(ausd, keeper);
        vm.stopBroadcast();

        console2.log("BudgetController deployed at:", address(controller));
        console2.log("  chain id:", block.chainid);
        console2.log("  AUSD:    ", ausd);
        console2.log("  keeper:  ", keeper);
        console2.log("  admin:   ", controller.admin());
    }

    /// @dev Rule: refuse to deploy anywhere except Base Sepolia or local anvil.
    function _requireTestnet() internal view {
        bool isTestnet = block.chainid == BASE_SEPOLIA_CHAIN_ID || block.chainid == ANVIL_DEFAULT_CHAIN_ID;
        if (!isTestnet) revert UnsupportedChain(block.chainid);
    }

    /// @dev Sign with RELAYER_PRIVATE_KEY if it is set, otherwise with forge's --private-key / --account.
    function _startBroadcast() internal {
        uint256 relayerKey = vm.envOr("RELAYER_PRIVATE_KEY", NO_PRIVATE_KEY);
        if (relayerKey == NO_PRIVATE_KEY) {
            vm.startBroadcast();
        } else {
            vm.startBroadcast(relayerKey);
        }
    }
}
