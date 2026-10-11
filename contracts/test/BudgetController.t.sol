// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

import {BudgetController} from "../src/BudgetController.sol";
import {MockAUSD} from "../src/mocks/MockAUSD.sol";

/**
 * @title  BudgetController tests
 * @notice Covers every rule in docs/SPEC.md §2: daily cap, per-agent cap, UTC day rollover, agent
 *         registration, keeper-only access, pause (direct and signed), expiry, config signatures and
 *         nonces, the ERC-2612 permit flow with 6-decimal AUSD (including a front-run permit), the
 *         "controller never holds funds" invariant, config updates, and EIP-712 digests — both
 *         against the spec's encoding and against digests pinned from backend/app/chain/eip712.py.
 *         Also the daily-voucher mode (SPEC §9 Update #1 D): openVoucherBudget, redeemVoucher splits,
 *         wrong-day / replayed / cancelled vouchers, split and cap checks, and voucherNonce.
 *
 * Each test follows Arrange / Act / Assert. Signatures are produced with Foundry's `vm.sign` over
 * digests built here from the spec (not read back from the contract under test).
 */
contract BudgetControllerTest is Test {
    // ─── test constants ─────────────────────────────────────────────────────────────────────

    uint256 private constant BASE_SEPOLIA_CHAIN_ID = 84532;
    uint128 private constant ONE_AUSD = 1_000_000; // AUSD has 6 decimals

    uint128 private constant DAILY_CAP = 10 * ONE_AUSD;
    uint128 private constant OPTIONS_AGENT_CAP = 6 * ONE_AUSD;
    uint128 private constant PERPS_AGENT_CAP = 6 * ONE_AUSD; // agent caps may sum above the daily cap
    uint128 private constant NEWS_AGENT_CAP = 3 * ONE_AUSD;

    uint256 private constant PERMIT_VALUE = 300 * ONE_AUSD; // 30 days x 10 AUSD
    uint256 private constant OWNER_STARTING_BALANCE = 1_000 * ONE_AUSD;

    uint256 private constant START_TIME = 1_790_000_000; // a mid-day UTC timestamp in 2026
    uint64 private constant BUDGET_LIFETIME = 30 days;
    uint256 private constant SIGNATURE_LIFETIME = 1 hours;

    bytes32 private constant SECTOR_OPTIONS = "OptionsEvent"; // right-padded with zero bytes
    bytes32 private constant SECTOR_PERPS = "OnchainPerps";
    bytes32 private constant SECTOR_NEWS = "NewsSentiment";

    // Test-only private keys (public Foundry fixtures, never used on a real chain).
    uint256 private constant OWNER_PRIVATE_KEY = 0xA11CE;
    uint256 private constant STRANGER_PRIVATE_KEY = 0xB0B;

    // Reference type hashes written out from the spec, independent of the contract's constants.
    bytes32 private constant EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant SPEC_BUDGET_CONFIG_TYPEHASH = keccak256(
        "BudgetConfig(address owner,uint128 dailyCap,uint64 expiry,address[] agentAddrs,uint128[] agentCaps,bytes32[] sectors,uint256 nonce)"
    );
    bytes32 private constant SPEC_SET_PAUSED_TYPEHASH =
        keccak256("SetPaused(address owner,bool paused,uint256 nonce,uint256 deadline)");
    bytes32 private constant PERMIT_TYPEHASH =
        keccak256("Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)");
    bytes32 private constant RECEIVE_WITH_AUTHORIZATION_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );
    bytes32 private constant CANCEL_AUTHORIZATION_TYPEHASH =
        keccak256("CancelAuthorization(address authorizer,bytes32 nonce)");

    /// @dev Salt the owner picks when signing a week of vouchers (any bytes32).
    bytes32 private constant VOUCHER_SALT = keccak256("test-voucher-salt");
    uint128 private constant VOUCHER_VALUE = 8 * ONE_AUSD; // fits the 10 AUSD daily cap

    // Digests pinned from backend/app/chain/eip712.py (build_budget_config / build_set_paused +
    // typed_data_digest) for chain 84532 and a controller at PINNED_CONTROLLER_ADDRESS.
    address private constant PINNED_CONTROLLER_ADDRESS = address(0xC0DE);
    address private constant PINNED_OWNER = 0xe05fcC23807536bEe418f142D19fa0d21BB0cfF7; // vm.addr(0xA11CE)
    bytes32 private constant PYTHON_BUDGET_CONFIG_DIGEST =
        0x91a32f35a295ddddfa971389377ff5436a640dd3da76cfc49af2654b41b101b6;
    bytes32 private constant PYTHON_SET_PAUSED_DIGEST =
        0x087d37a466a1a7443471bc0477034e1cfd234fd032ab35cd205c4b9c697e50f6;

    /// @dev One signed daily voucher (an ERC-3009 ReceiveWithAuthorization to the controller).
    struct Voucher {
        uint256 value;
        uint256 validAfter;
        uint256 validBefore;
        bytes32 nonce;
        bytes signature;
    }

    /// @dev An ERC-2612 permit signature plus the two values it covers.
    struct PermitSig {
        uint256 value;
        uint256 deadline;
        uint8 v;
        bytes32 r;
        bytes32 s;
    }

    // ─── fixtures ───────────────────────────────────────────────────────────────────────────

    MockAUSD private token;
    BudgetController private controller;

    address private owner;
    address private keeper;
    address private stranger;
    address private optionsAgent;
    address private perpsAgent;
    address private newsAgent;

    function setUp() public {
        vm.chainId(BASE_SEPOLIA_CHAIN_ID);
        vm.warp(START_TIME);

        owner = vm.addr(OWNER_PRIVATE_KEY);
        stranger = vm.addr(STRANGER_PRIVATE_KEY);
        keeper = makeAddr("keeper");
        optionsAgent = makeAddr("optionsAgent");
        perpsAgent = makeAddr("perpsAgent");
        newsAgent = makeAddr("newsAgent");

        token = new MockAUSD();
        controller = new BudgetController(address(token), keeper);
        token.mint(owner, OWNER_STARTING_BALANCE);
    }

    // ═══ openBudget + permit flow ═══════════════════════════════════════════════════════════

    function test_open_budget_with_permit_stores_config_and_six_decimal_allowance() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();

        // Act
        _openBudget(cfg);

        // Assert
        assertEq(token.decimals(), 6);
        assertEq(token.allowance(owner, address(controller)), PERMIT_VALUE);
        assertEq(token.allowance(owner, address(controller)), 300_000_000); // 300 AUSD in atomic units

        BudgetController.Budget memory budget = _budget(owner);
        assertEq(budget.dailyCap, DAILY_CAP);
        assertEq(budget.expiry, cfg.expiry);
        assertTrue(budget.exists);
        assertFalse(budget.paused);
        assertEq(budget.cfgNonce, 1);

        BudgetController.Agent memory options = _agent(owner, optionsAgent);
        assertTrue(options.active);
        assertEq(options.dailyCap, OPTIONS_AGENT_CAP);
        assertEq(options.sector, SECTOR_OPTIONS);

        address[] memory expectedAgents = new address[](2);
        expectedAgents[0] = optionsAgent;
        expectedAgents[1] = perpsAgent;
        assertEq(controller.getAgents(owner), expectedAgents);
        assertEq(controller.remainingToday(owner), DAILY_CAP);
    }

    function test_open_budget_emits_budget_opened_and_agent_configured() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();

        // Assert (expected events are declared before the call)
        vm.expectEmit(address(controller));
        emit BudgetController.AgentConfigured(owner, optionsAgent, OPTIONS_AGENT_CAP, SECTOR_OPTIONS);
        vm.expectEmit(address(controller));
        emit BudgetController.AgentConfigured(owner, perpsAgent, PERPS_AGENT_CAP, SECTOR_PERPS);
        vm.expectEmit(address(controller));
        emit BudgetController.BudgetOpened(owner, DAILY_CAP, cfg.expiry, 2);

        // Act
        _openBudget(cfg);
    }

    function test_open_budget_succeeds_when_permit_was_front_run() public {
        // Arrange: a stranger submits the owner's permit to the token before the keeper does.
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        uint256 permitDeadline = block.timestamp + SIGNATURE_LIFETIME;
        (uint8 v, bytes32 r, bytes32 s) = _signPermit(OWNER_PRIVATE_KEY, PERMIT_VALUE, permitDeadline);
        vm.prank(stranger);
        token.permit(owner, address(controller), PERMIT_VALUE, permitDeadline, v, r, s);
        bytes memory cfgSig = _ownerConfigSig(cfg);

        // Act: the keeper's own permit call now fails inside try/catch, but the allowance is in place.
        vm.prank(keeper);
        controller.openBudget(owner, PERMIT_VALUE, permitDeadline, v, r, s, cfg, cfgSig);

        // Assert
        assertTrue(_budget(owner).exists);
        assertEq(token.allowance(owner, address(controller)), PERMIT_VALUE);
        assertEq(token.nonces(owner), 1); // the permit nonce was consumed exactly once
    }

    function test_open_budget_reverts_when_allowance_below_daily_cap() public {
        // Arrange: the permit only covers half of one day's cap.
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        uint256 tooSmallPermit = DAILY_CAP / 2;
        uint256 permitDeadline = block.timestamp + SIGNATURE_LIFETIME;
        (uint8 v, bytes32 r, bytes32 s) = _signPermit(OWNER_PRIVATE_KEY, tooSmallPermit, permitDeadline);
        bytes memory cfgSig = _ownerConfigSig(cfg);

        // Act + Assert
        vm.expectRevert(BudgetController.InsufficientAllowance.selector);
        vm.prank(keeper);
        controller.openBudget(owner, tooSmallPermit, permitDeadline, v, r, s, cfg, cfgSig);
    }

    function test_open_budget_reverts_when_permit_is_invalid_and_no_allowance_exists() public {
        // Arrange: garbage permit signature; the token rejects it and there is no prior approval.
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        bytes memory cfgSig = _ownerConfigSig(cfg);
        uint8 garbageV = 27;

        // Act + Assert
        vm.expectRevert(BudgetController.InsufficientAllowance.selector);
        vm.prank(keeper);
        controller.openBudget(owner, PERMIT_VALUE, block.timestamp, garbageV, bytes32(0), bytes32(0), cfg, cfgSig);
    }

    function test_open_budget_reverts_when_caller_is_not_keeper() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        uint256 permitDeadline = block.timestamp + SIGNATURE_LIFETIME;
        (uint8 v, bytes32 r, bytes32 s) = _signPermit(OWNER_PRIVATE_KEY, PERMIT_VALUE, permitDeadline);
        bytes memory cfgSig = _ownerConfigSig(cfg);

        // Act + Assert
        vm.expectRevert(BudgetController.NotKeeper.selector);
        vm.prank(stranger);
        controller.openBudget(owner, PERMIT_VALUE, permitDeadline, v, r, s, cfg, cfgSig);
    }

    // ═══ config signatures and nonces ═══════════════════════════════════════════════════════

    function test_open_budget_reverts_on_bad_config_signature() public {
        // Arrange: the config is signed by a stranger instead of the owner.
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        bytes memory strangerSig = _sign(STRANGER_PRIVATE_KEY, _specConfigDigest(cfg));

        // Act + Assert
        _expectOpenBudgetRevert(cfg, strangerSig, BudgetController.BadSignature.selector);
    }

    function test_open_budget_reverts_when_signed_config_is_tampered() public {
        // Arrange: the owner signed a 10 AUSD cap, the submitted config claims 20 AUSD.
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        bytes memory ownerSig = _ownerConfigSig(cfg);
        cfg.dailyCap = 2 * DAILY_CAP;

        // Act + Assert
        _expectOpenBudgetRevert(cfg, ownerSig, BudgetController.BadSignature.selector);
    }

    function test_open_budget_reverts_when_config_owner_differs_from_permit_owner() public {
        // Arrange: a config owned (and validly signed) by the stranger, submitted for the owner's permit.
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        cfg.owner = stranger;
        bytes memory strangerSig = _sign(STRANGER_PRIVATE_KEY, _specConfigDigest(cfg));

        // Act + Assert
        _expectOpenBudgetRevert(cfg, strangerSig, BudgetController.BadSignature.selector);
    }

    function test_open_budget_reverts_on_replayed_nonce() public {
        // Arrange: open once, keep the exact same signed config.
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        bytes memory cfgSig = _ownerConfigSig(cfg);
        _submitOpenBudget(cfg, cfgSig);

        // Act + Assert: re-submitting it fails because the nonce was consumed.
        _expectOpenBudgetRevert(cfg, cfgSig, BudgetController.BadNonce.selector);
    }

    function test_update_config_reverts_on_replayed_nonce() public {
        // Arrange
        _openBudget(_defaultConfig());
        BudgetController.BudgetConfig memory update = _defaultConfig();
        bytes memory updateSig = _ownerConfigSig(update);
        vm.prank(keeper);
        controller.updateConfigWithSig(update, updateSig);

        // Act + Assert
        vm.expectRevert(BudgetController.BadNonce.selector);
        vm.prank(keeper);
        controller.updateConfigWithSig(update, updateSig);
    }

    // ═══ config validation ══════════════════════════════════════════════════════════════════

    function test_open_budget_reverts_on_length_mismatch() public {
        // Arrange: two agents but only one cap.
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        cfg.agentCaps = new uint128[](1);
        cfg.agentCaps[0] = OPTIONS_AGENT_CAP;

        // Act + Assert
        _expectOpenBudgetRevert(cfg, _ownerConfigSig(cfg), BudgetController.LengthMismatch.selector);
    }

    function test_open_budget_reverts_on_zero_agent_address() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        cfg.agentAddrs[1] = address(0);

        // Act + Assert
        _expectOpenBudgetRevert(cfg, _ownerConfigSig(cfg), BudgetController.ZeroAddress.selector);
    }

    function test_open_budget_reverts_when_agent_cap_above_daily_cap() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        cfg.agentCaps[0] = DAILY_CAP + 1;

        // Act + Assert
        _expectOpenBudgetRevert(cfg, _ownerConfigSig(cfg), BudgetController.AgentCapAboveDailyCap.selector);
    }

    function test_open_budget_reverts_on_duplicate_agent() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        cfg.agentAddrs[1] = cfg.agentAddrs[0];

        // Act + Assert
        _expectOpenBudgetRevert(cfg, _ownerConfigSig(cfg), BudgetController.DuplicateAgent.selector);
    }

    function test_open_budget_reverts_when_agent_is_the_controller() public {
        // Arrange: recharging the controller itself would trap funds in it.
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        cfg.agentAddrs[1] = address(controller);

        // Act + Assert
        _expectOpenBudgetRevert(cfg, _ownerConfigSig(cfg), BudgetController.InvalidAgent.selector);
    }

    function test_open_budget_reverts_when_too_many_agents() public {
        // Arrange
        uint256 tooMany = controller.MAX_AGENTS() + 1;
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        cfg.agentAddrs = new address[](tooMany);
        cfg.agentCaps = new uint128[](tooMany);
        cfg.sectors = new bytes32[](tooMany);
        for (uint256 i = 0; i < tooMany; ++i) {
            cfg.agentAddrs[i] = makeAddr(string.concat("agent", vm.toString(i)));
            cfg.agentCaps[i] = OPTIONS_AGENT_CAP;
            cfg.sectors[i] = SECTOR_OPTIONS;
        }

        // Act + Assert
        _expectOpenBudgetRevert(cfg, _ownerConfigSig(cfg), BudgetController.TooManyAgents.selector);
    }

    // ═══ recharge: caps ═════════════════════════════════════════════════════════════════════

    function test_recharge_moves_exact_atomic_units_from_owner_to_agent() public {
        // Arrange
        _openBudget(_defaultConfig());
        uint128 amount = 2_500_000; // 2.5 AUSD
        uint256 ownerBalanceBefore = token.balanceOf(owner);

        // Assert (event declared before the call)
        vm.expectEmit(address(controller));
        emit BudgetController.Recharged(owner, optionsAgent, amount, amount, amount);

        // Act
        _recharge(optionsAgent, amount);

        // Assert
        assertEq(token.balanceOf(optionsAgent), amount);
        assertEq(token.balanceOf(owner), ownerBalanceBefore - amount);
        assertEq(token.allowance(owner, address(controller)), PERMIT_VALUE - amount);
        assertEq(token.balanceOf(address(controller)), 0);
    }

    function test_recharge_reverts_when_daily_cap_exceeded() public {
        // Arrange: 6 + 4 = 10 AUSD fills the budget's daily cap; the perps agent itself still has 2 left.
        _openBudget(_defaultConfig());
        _recharge(optionsAgent, OPTIONS_AGENT_CAP);
        _recharge(perpsAgent, DAILY_CAP - OPTIONS_AGENT_CAP);

        // Act + Assert
        vm.expectRevert(BudgetController.DailyCapExceeded.selector);
        _recharge(perpsAgent, 1);
    }

    function test_recharge_reverts_when_agent_cap_exceeded() public {
        // Arrange: the options agent is at its 6 AUSD cap; the budget still has 4 AUSD left.
        _openBudget(_defaultConfig());
        _recharge(optionsAgent, OPTIONS_AGENT_CAP);

        // Act + Assert
        vm.expectRevert(BudgetController.AgentCapExceeded.selector);
        _recharge(optionsAgent, 1);
    }

    function test_day_rollover_resets_budget_and_agent_spend() public {
        // Arrange: exhaust both caps today.
        _openBudget(_defaultConfig());
        _recharge(optionsAgent, OPTIONS_AGENT_CAP);
        _recharge(perpsAgent, DAILY_CAP - OPTIONS_AGENT_CAP);
        uint256 nextDayStart = _nextUtcDayStart();

        // Still the same UTC day one second before midnight.
        vm.warp(nextDayStart - 1);
        vm.expectRevert(BudgetController.DailyCapExceeded.selector);
        _recharge(perpsAgent, 1);

        // Act: cross midnight UTC.
        vm.warp(nextDayStart);

        // Assert: the views already show fresh caps before any transaction ...
        assertEq(controller.remainingToday(owner), DAILY_CAP);
        assertEq(controller.agentRemainingToday(owner, optionsAgent), OPTIONS_AGENT_CAP);

        // ... and the agent can be recharged up to its full cap again.
        _recharge(optionsAgent, OPTIONS_AGENT_CAP);
        assertEq(_budget(owner).spentToday, OPTIONS_AGENT_CAP);
        assertEq(_agent(owner, optionsAgent).spentToday, OPTIONS_AGENT_CAP);
        assertEq(_budget(owner).dayIndex, nextDayStart / 1 days);
    }

    // ═══ recharge: access and state ═════════════════════════════════════════════════════════

    function test_recharge_reverts_for_unregistered_agent() public {
        // Arrange
        _openBudget(_defaultConfig());

        // Act + Assert
        vm.expectRevert(BudgetController.AgentInactive.selector);
        _recharge(stranger, 1);
    }

    function test_recharge_reverts_when_caller_is_not_keeper() public {
        // Arrange
        _openBudget(_defaultConfig());

        // Act + Assert
        vm.expectRevert(BudgetController.NotKeeper.selector);
        vm.prank(stranger);
        controller.recharge(owner, optionsAgent, 1);
    }

    function test_recharge_reverts_when_no_budget() public {
        // Act + Assert
        vm.expectRevert(BudgetController.NoBudget.selector);
        _recharge(optionsAgent, 1);
    }

    function test_recharge_reverts_when_budget_paused_by_owner() public {
        // Arrange
        _openBudget(_defaultConfig());
        vm.prank(owner);
        controller.setPaused(true);

        // Act + Assert
        vm.expectRevert(BudgetController.BudgetPaused.selector);
        _recharge(optionsAgent, 1);
    }

    function test_owner_can_resume_paused_budget_directly() public {
        // Arrange
        _openBudget(_defaultConfig());
        vm.prank(owner);
        controller.setPaused(true);

        // Act
        vm.expectEmit(address(controller));
        emit BudgetController.Paused(owner, false);
        vm.prank(owner);
        controller.setPaused(false);

        // Assert
        _recharge(optionsAgent, 1);
        assertEq(token.balanceOf(optionsAgent), 1);
    }

    function test_set_paused_reverts_when_owner_has_no_budget() public {
        // Act + Assert
        vm.expectRevert(BudgetController.NoBudget.selector);
        vm.prank(stranger);
        controller.setPaused(true);
    }

    function test_recharge_reverts_when_budget_expired() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        _openBudget(cfg);

        // One second before expiry recharging still works ...
        vm.warp(cfg.expiry - 1);
        _recharge(optionsAgent, 1);

        // Act + Assert: ... at the expiry second it no longer does.
        vm.warp(cfg.expiry);
        vm.expectRevert(BudgetController.BudgetExpired.selector);
        _recharge(optionsAgent, 1);
    }

    // ═══ the controller never holds funds ═══════════════════════════════════════════════════

    function test_controller_never_holds_a_balance() public {
        // Arrange
        _openBudget(_defaultConfig());

        // Act: several recharges across two agents and two days.
        _recharge(optionsAgent, 3 * ONE_AUSD);
        _recharge(perpsAgent, 5 * ONE_AUSD);
        vm.warp(_nextUtcDayStart());
        _recharge(optionsAgent, 6 * ONE_AUSD);

        // Assert
        assertEq(token.balanceOf(address(controller)), 0);
        assertEq(token.balanceOf(optionsAgent), 9 * ONE_AUSD);
        assertEq(token.balanceOf(perpsAgent), 5 * ONE_AUSD);
        assertEq(token.balanceOf(owner), OWNER_STARTING_BALANCE - 14 * ONE_AUSD);
    }

    function testFuzz_recharge_never_leaves_funds_in_controller(uint128 amount) public {
        // Arrange
        amount = uint128(bound(amount, 0, OPTIONS_AGENT_CAP));
        _openBudget(_defaultConfig());
        uint256 ownerBalanceBefore = token.balanceOf(owner);

        // Act
        _recharge(optionsAgent, amount);

        // Assert: value is conserved owner -> agent, nothing stays behind.
        assertEq(token.balanceOf(address(controller)), 0);
        assertEq(token.balanceOf(optionsAgent), amount);
        assertEq(ownerBalanceBefore - token.balanceOf(owner), amount);
    }

    function testFuzz_total_recharged_per_day_never_exceeds_daily_cap(uint128[6] memory amounts) public {
        // Arrange
        _openBudget(_defaultConfig());
        uint256 ownerBalanceBefore = token.balanceOf(owner);

        // Act: alternate agents with random amounts; over-cap attempts must revert and change nothing.
        for (uint256 i = 0; i < amounts.length; ++i) {
            address agent = i % 2 == 0 ? optionsAgent : perpsAgent;
            uint128 amount = uint128(bound(amounts[i], 0, DAILY_CAP));
            vm.prank(keeper);
            try controller.recharge(owner, agent, amount) {} catch {}
        }

        // Assert
        uint256 totalMoved = ownerBalanceBefore - token.balanceOf(owner);
        assertLe(totalMoved, DAILY_CAP);
        assertEq(totalMoved, _budget(owner).spentToday);
        assertLe(token.balanceOf(optionsAgent), OPTIONS_AGENT_CAP);
        assertLe(token.balanceOf(perpsAgent), PERPS_AGENT_CAP);
    }

    // ═══ setPausedWithSig ═══════════════════════════════════════════════════════════════════

    function test_set_paused_with_sig_pauses_and_consumes_nonce() public {
        // Arrange
        _openBudget(_defaultConfig());
        uint256 nonce = controller.configNonce(owner);
        uint256 deadline = block.timestamp + SIGNATURE_LIFETIME;
        bytes memory sig = _sign(OWNER_PRIVATE_KEY, _specSetPausedDigest(owner, true, nonce, deadline));

        // Act
        vm.expectEmit(address(controller));
        emit BudgetController.Paused(owner, true);
        vm.prank(keeper);
        controller.setPausedWithSig(owner, true, deadline, sig);

        // Assert
        assertTrue(_budget(owner).paused);
        assertEq(controller.configNonce(owner), nonce + 1);
        vm.expectRevert(BudgetController.BudgetPaused.selector);
        _recharge(optionsAgent, 1);
    }

    function test_set_paused_with_sig_can_resume_with_next_nonce() public {
        // Arrange: pause with nonce n.
        _openBudget(_defaultConfig());
        uint256 deadline = block.timestamp + SIGNATURE_LIFETIME;
        _setPausedWithOwnerSig(true, deadline);

        // Act: resume with nonce n + 1.
        _setPausedWithOwnerSig(false, deadline);

        // Assert
        assertFalse(_budget(owner).paused);
        _recharge(optionsAgent, 1);
        assertEq(token.balanceOf(optionsAgent), 1);
    }

    function test_set_paused_with_sig_reverts_on_replay() public {
        // Arrange
        _openBudget(_defaultConfig());
        uint256 deadline = block.timestamp + SIGNATURE_LIFETIME;
        bytes memory sig =
            _sign(OWNER_PRIVATE_KEY, _specSetPausedDigest(owner, true, controller.configNonce(owner), deadline));
        vm.prank(keeper);
        controller.setPausedWithSig(owner, true, deadline, sig);

        // Act + Assert: the nonce moved on, so the old signature no longer matches.
        vm.expectRevert(BudgetController.BadSignature.selector);
        vm.prank(keeper);
        controller.setPausedWithSig(owner, true, deadline, sig);
    }

    function test_set_paused_with_sig_reverts_after_deadline() public {
        // Arrange
        _openBudget(_defaultConfig());
        uint256 deadline = block.timestamp + SIGNATURE_LIFETIME;
        bytes memory sig =
            _sign(OWNER_PRIVATE_KEY, _specSetPausedDigest(owner, true, controller.configNonce(owner), deadline));
        vm.warp(deadline + 1);

        // Act + Assert
        vm.expectRevert(BudgetController.SignatureExpired.selector);
        vm.prank(keeper);
        controller.setPausedWithSig(owner, true, deadline, sig);
    }

    function test_set_paused_with_sig_reverts_when_signed_by_someone_else() public {
        // Arrange
        _openBudget(_defaultConfig());
        uint256 deadline = block.timestamp + SIGNATURE_LIFETIME;
        bytes memory strangerSig =
            _sign(STRANGER_PRIVATE_KEY, _specSetPausedDigest(owner, true, controller.configNonce(owner), deadline));

        // Act + Assert
        vm.expectRevert(BudgetController.BadSignature.selector);
        vm.prank(keeper);
        controller.setPausedWithSig(owner, true, deadline, strangerSig);
    }

    function test_set_paused_with_sig_reverts_when_caller_is_not_keeper() public {
        // Arrange
        _openBudget(_defaultConfig());
        uint256 deadline = block.timestamp + SIGNATURE_LIFETIME;
        bytes memory sig =
            _sign(OWNER_PRIVATE_KEY, _specSetPausedDigest(owner, true, controller.configNonce(owner), deadline));

        // Act + Assert
        vm.expectRevert(BudgetController.NotKeeper.selector);
        vm.prank(stranger);
        controller.setPausedWithSig(owner, true, deadline, sig);
    }

    function test_set_paused_with_sig_reverts_when_no_budget() public {
        // Arrange
        uint256 deadline = block.timestamp + SIGNATURE_LIFETIME;
        bytes memory sig = _sign(OWNER_PRIVATE_KEY, _specSetPausedDigest(owner, true, 0, deadline));

        // Act + Assert
        vm.expectRevert(BudgetController.NoBudget.selector);
        vm.prank(keeper);
        controller.setPausedWithSig(owner, true, deadline, sig);
    }

    // ═══ updateConfigWithSig ════════════════════════════════════════════════════════════════

    function test_update_config_with_sig_deactivates_dropped_agents() public {
        // Arrange: budget with options + perps; new config lists options + news (perps dropped).
        _openBudget(_defaultConfig());
        BudgetController.BudgetConfig memory update = _configWithOptionsAndNews();
        bytes memory updateSig = _ownerConfigSig(update);

        // Act
        vm.expectEmit(address(controller));
        emit BudgetController.AgentDeactivated(owner, perpsAgent);
        vm.prank(keeper);
        controller.updateConfigWithSig(update, updateSig);

        // Assert
        assertFalse(_agent(owner, perpsAgent).active);
        assertTrue(_agent(owner, optionsAgent).active);
        assertTrue(_agent(owner, newsAgent).active);

        address[] memory expectedAgents = new address[](2);
        expectedAgents[0] = optionsAgent;
        expectedAgents[1] = newsAgent;
        assertEq(controller.getAgents(owner), expectedAgents);

        vm.expectRevert(BudgetController.AgentInactive.selector);
        _recharge(perpsAgent, 1);
        _recharge(newsAgent, NEWS_AGENT_CAP);
        assertEq(token.balanceOf(newsAgent), NEWS_AGENT_CAP);
    }

    function test_update_config_with_sig_keeps_todays_spend() public {
        // Arrange: 6 AUSD already recharged today; the owner lowers the daily cap to 8 AUSD.
        _openBudget(_defaultConfig());
        _recharge(optionsAgent, OPTIONS_AGENT_CAP);
        BudgetController.BudgetConfig memory update = _defaultConfig();
        update.dailyCap = 8 * ONE_AUSD;

        // Act
        _updateConfig(update);

        // Assert: 8 - 6 = 2 AUSD left today, and the agent's own spend was kept too.
        assertEq(controller.remainingToday(owner), 2 * ONE_AUSD);
        assertEq(_agent(owner, optionsAgent).spentToday, OPTIONS_AGENT_CAP);
        assertEq(controller.agentRemainingToday(owner, perpsAgent), 2 * ONE_AUSD);
    }

    function test_update_config_emits_config_updated() public {
        // Arrange
        _openBudget(_defaultConfig());
        BudgetController.BudgetConfig memory update = _configWithOptionsAndNews();
        bytes memory updateSig = _ownerConfigSig(update);

        // Assert (event declared before the call)
        vm.expectEmit(address(controller));
        emit BudgetController.ConfigUpdated(owner, update.dailyCap, update.expiry, 2);

        // Act
        vm.prank(keeper);
        controller.updateConfigWithSig(update, updateSig);
    }

    function test_remaining_today_is_zero_when_cap_lowered_below_todays_spend() public {
        // Arrange: 10 AUSD spent today, then the cap is lowered to 5 AUSD.
        _openBudget(_defaultConfig());
        _recharge(optionsAgent, OPTIONS_AGENT_CAP);
        _recharge(perpsAgent, DAILY_CAP - OPTIONS_AGENT_CAP);
        BudgetController.BudgetConfig memory update = _defaultConfig();
        update.dailyCap = 5 * ONE_AUSD;
        update.agentCaps[0] = 5 * ONE_AUSD;
        update.agentCaps[1] = 5 * ONE_AUSD;

        // Act
        _updateConfig(update);

        // Assert: the views clamp at zero instead of underflowing, and recharges stop.
        assertEq(controller.remainingToday(owner), 0);
        assertEq(controller.agentRemainingToday(owner, optionsAgent), 0);
        vm.expectRevert(BudgetController.DailyCapExceeded.selector);
        _recharge(perpsAgent, 1);
    }

    function test_update_config_with_sig_reverts_without_budget() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        bytes memory cfgSig = _ownerConfigSig(cfg);

        // Act + Assert
        vm.expectRevert(BudgetController.NoBudget.selector);
        vm.prank(keeper);
        controller.updateConfigWithSig(cfg, cfgSig);
    }

    function test_update_config_reverts_when_caller_is_not_keeper() public {
        // Arrange
        _openBudget(_defaultConfig());
        BudgetController.BudgetConfig memory update = _defaultConfig();
        bytes memory updateSig = _ownerConfigSig(update);

        // Act + Assert
        vm.expectRevert(BudgetController.NotKeeper.selector);
        vm.prank(stranger);
        controller.updateConfigWithSig(update, updateSig);
    }

    function test_reopening_budget_keeps_todays_spend_and_unpauses() public {
        // Arrange: spend 6 AUSD, pause, then re-open with a fresh permit + config.
        _openBudget(_defaultConfig());
        _recharge(optionsAgent, OPTIONS_AGENT_CAP);
        vm.prank(owner);
        controller.setPaused(true);

        // Act
        _openBudget(_defaultConfig());

        // Assert: re-opening cannot be used to reset today's cap.
        assertFalse(_budget(owner).paused);
        assertEq(controller.remainingToday(owner), DAILY_CAP - OPTIONS_AGENT_CAP);
        vm.expectRevert(BudgetController.AgentCapExceeded.selector);
        _recharge(optionsAgent, 1);
    }

    // ═══ views ══════════════════════════════════════════════════════════════════════════════

    function test_agent_remaining_today_is_min_of_agent_and_budget_headroom() public {
        // Arrange: options takes 6 of the 10 AUSD daily cap.
        _openBudget(_defaultConfig());
        _recharge(optionsAgent, OPTIONS_AGENT_CAP);

        // Act
        uint256 perpsRemaining = controller.agentRemainingToday(owner, perpsAgent);

        // Assert: perps' own cap has 6 left, but the budget only 4.
        assertEq(perpsRemaining, DAILY_CAP - OPTIONS_AGENT_CAP);
        assertEq(controller.agentRemainingToday(owner, optionsAgent), 0);
    }

    function test_remaining_views_are_zero_when_budget_not_spendable() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();

        // No budget yet.
        assertEq(controller.remainingToday(owner), 0);
        assertEq(controller.agentRemainingToday(owner, optionsAgent), 0);

        // Unregistered agent.
        _openBudget(cfg);
        assertEq(controller.agentRemainingToday(owner, stranger), 0);

        // Paused.
        vm.prank(owner);
        controller.setPaused(true);
        assertEq(controller.remainingToday(owner), 0);
        assertEq(controller.agentRemainingToday(owner, optionsAgent), 0);

        // Expired.
        vm.prank(owner);
        controller.setPaused(false);
        vm.warp(cfg.expiry);
        assertEq(controller.remainingToday(owner), 0);
        assertEq(controller.agentRemainingToday(owner, optionsAgent), 0);
    }

    // ═══ admin ══════════════════════════════════════════════════════════════════════════════

    function test_constructor_sets_admin_keeper_and_token() public view {
        // Assert
        assertEq(controller.admin(), address(this));
        assertEq(controller.keeper(), keeper);
        assertEq(address(controller.ausd()), address(token));
    }

    function test_constructor_reverts_on_zero_addresses() public {
        // Act + Assert
        vm.expectRevert(BudgetController.ZeroAddress.selector);
        new BudgetController(address(0), keeper);
        vm.expectRevert(BudgetController.ZeroAddress.selector);
        new BudgetController(address(token), address(0));
    }

    function test_set_keeper_by_admin_replaces_keeper() public {
        // Arrange
        address newKeeper = makeAddr("newKeeper");
        _openBudget(_defaultConfig());

        // Act
        vm.expectEmit(address(controller));
        emit BudgetController.KeeperChanged(keeper, newKeeper);
        controller.setKeeper(newKeeper);

        // Assert: the old keeper is locked out, the new one can recharge.
        assertEq(controller.keeper(), newKeeper);
        vm.expectRevert(BudgetController.NotKeeper.selector);
        _recharge(optionsAgent, 1);
        vm.prank(newKeeper);
        controller.recharge(owner, optionsAgent, 1);
    }

    function test_set_keeper_reverts_when_caller_is_not_admin() public {
        // Act + Assert
        vm.expectRevert(BudgetController.NotAdmin.selector);
        vm.prank(stranger);
        controller.setKeeper(stranger);
    }

    function test_set_keeper_reverts_on_zero_address() public {
        // Act + Assert
        vm.expectRevert(BudgetController.ZeroAddress.selector);
        controller.setKeeper(address(0));
    }

    // ═══ ERC-1271 smart-wallet owners ═══════════════════════════════════════════════════════

    function test_open_budget_accepts_erc1271_smart_wallet_owner() public {
        // Arrange: a smart wallet whose signer is the owner's key. Smart wallets approve directly
        // (no ERC-2612 permit), so the permit arguments are garbage and the try/catch absorbs it.
        MockSmartWallet wallet = new MockSmartWallet(owner);
        token.mint(address(wallet), OWNER_STARTING_BALANCE);
        vm.prank(owner);
        wallet.approveToken(token, address(controller), PERMIT_VALUE);

        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        cfg.owner = address(wallet);
        cfg.nonce = controller.configNonce(address(wallet));
        bytes memory walletSig = _ownerConfigSig(cfg);

        // Act
        vm.prank(keeper);
        controller.openBudget(address(wallet), 0, 0, 0, bytes32(0), bytes32(0), cfg, walletSig);
        vm.prank(keeper);
        controller.recharge(address(wallet), optionsAgent, 1);

        // Assert
        assertTrue(_budget(address(wallet)).exists);
        assertEq(token.balanceOf(optionsAgent), 1);
    }

    // ═══ EIP-712 digests ════════════════════════════════════════════════════════════════════

    function test_domain_separator_matches_spec_encoding() public view {
        // Act
        (, string memory name, string memory version, uint256 chainId, address verifyingContract,,) =
            controller.eip712Domain();

        // Assert
        assertEq(controller.domainSeparator(), _specDomainSeparator(address(controller)));
        assertEq(name, "PocketDesk");
        assertEq(version, "1");
        assertEq(chainId, BASE_SEPOLIA_CHAIN_ID);
        assertEq(verifyingContract, address(controller));
    }

    function test_hash_budget_config_matches_spec_encoding() public view {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();

        // Act + Assert
        assertEq(controller.hashBudgetConfig(cfg), _specConfigDigest(cfg));
    }

    function test_hash_set_paused_matches_spec_encoding() public view {
        // Arrange
        uint256 nonce = 5;
        uint256 deadline = block.timestamp + SIGNATURE_LIFETIME;

        // Act + Assert
        assertEq(
            controller.hashSetPaused(owner, true, nonce, deadline), _specSetPausedDigest(owner, true, nonce, deadline)
        );
    }

    function test_hash_budget_config_matches_python_eip712_builder() public {
        // Arrange: same inputs the Python digest was computed from.
        BudgetController pinned = _deployControllerAtPinnedAddress();
        address[] memory agentAddrs = new address[](2);
        agentAddrs[0] = 0x1111111111111111111111111111111111111111;
        agentAddrs[1] = 0x2222222222222222222222222222222222222222;
        uint128[] memory agentCaps = new uint128[](2);
        agentCaps[0] = 4_000_000;
        agentCaps[1] = 6_000_000;
        bytes32[] memory sectors = new bytes32[](2);
        sectors[0] = SECTOR_OPTIONS;
        sectors[1] = SECTOR_PERPS;
        BudgetController.BudgetConfig memory cfg = BudgetController.BudgetConfig({
            owner: PINNED_OWNER,
            dailyCap: 10_000_000,
            expiry: 1_800_000_000,
            agentAddrs: agentAddrs,
            agentCaps: agentCaps,
            sectors: sectors,
            nonce: 7
        });

        // Act
        bytes32 digest = pinned.hashBudgetConfig(cfg);

        // Assert
        assertEq(vm.addr(OWNER_PRIVATE_KEY), PINNED_OWNER);
        assertEq(digest, PYTHON_BUDGET_CONFIG_DIGEST);
    }

    function test_hash_set_paused_matches_python_eip712_builder() public {
        // Arrange
        BudgetController pinned = _deployControllerAtPinnedAddress();

        // Act
        bytes32 digest = pinned.hashSetPaused(PINNED_OWNER, true, 3, 1_800_000_000);

        // Assert
        assertEq(digest, PYTHON_SET_PAUSED_DIGEST);
    }

    // ═══ daily vouchers: opening ════════════════════════════════════════════════════════════

    function test_open_voucher_budget_stores_config_in_voucher_mode_without_allowance() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();

        // Act
        _openVoucherBudget(cfg);

        // Assert
        BudgetController.Budget memory budget = _budget(owner);
        assertTrue(budget.exists);
        assertEq(uint8(budget.mode), uint8(BudgetController.BudgetMode.VOUCHER));
        assertEq(budget.dailyCap, DAILY_CAP);
        assertEq(budget.cfgNonce, 1);
        assertEq(token.allowance(owner, address(controller)), 0);
        assertTrue(_agent(owner, optionsAgent).active);
        assertEq(controller.getAgents(owner).length, 2);
    }

    function test_open_voucher_budget_emits_voucher_budget_opened() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        bytes memory cfgSig = _ownerConfigSig(cfg);

        // Assert (event declared before the call)
        vm.expectEmit(address(controller));
        emit BudgetController.VoucherBudgetOpened(owner, DAILY_CAP, cfg.expiry, 2);

        // Act
        vm.prank(keeper);
        controller.openVoucherBudget(cfg, cfgSig);
    }

    function test_open_voucher_budget_reverts_on_bad_config_signature() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        bytes memory strangerSig = _sign(STRANGER_PRIVATE_KEY, _specConfigDigest(cfg));

        // Act + Assert
        vm.expectRevert(BudgetController.BadSignature.selector);
        vm.prank(keeper);
        controller.openVoucherBudget(cfg, strangerSig);
    }

    function test_open_voucher_budget_reverts_when_caller_is_not_keeper() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        bytes memory cfgSig = _ownerConfigSig(cfg);

        // Act + Assert
        vm.expectRevert(BudgetController.NotKeeper.selector);
        vm.prank(stranger);
        controller.openVoucherBudget(cfg, cfgSig);
    }

    function test_recharge_reverts_for_voucher_mode_budget() public {
        // Arrange: even with a leftover allowance, a voucher-mode budget never pulls via transferFrom.
        _openVoucherBudget(_defaultConfig());
        vm.prank(owner);
        token.approve(address(controller), PERMIT_VALUE);

        // Act + Assert
        vm.expectRevert(BudgetController.WrongBudgetMode.selector);
        _recharge(optionsAgent, 1);
    }

    function test_open_budget_with_permit_switches_voucher_budget_back_to_permit_mode() public {
        // Arrange
        _openVoucherBudget(_defaultConfig());

        // Act
        _openBudget(_defaultConfig());

        // Assert
        assertEq(uint8(_budget(owner).mode), uint8(BudgetController.BudgetMode.PERMIT));
        _recharge(optionsAgent, ONE_AUSD);
        assertEq(token.balanceOf(optionsAgent), ONE_AUSD);
    }

    // ═══ daily vouchers: redeeming ══════════════════════════════════════════════════════════

    function test_redeem_voucher_splits_value_across_agents() public {
        // Arrange: 8 AUSD voucher for today, split 5 (options) + 3 (perps).
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);
        uint256 ownerBalanceBefore = token.balanceOf(owner);

        // Act
        _redeem(voucher, agentAddrs, amounts);

        // Assert
        assertEq(token.balanceOf(optionsAgent), 5 * ONE_AUSD);
        assertEq(token.balanceOf(perpsAgent), 3 * ONE_AUSD);
        assertEq(token.balanceOf(owner), ownerBalanceBefore - VOUCHER_VALUE);
        assertEq(token.balanceOf(address(controller)), 0);
        assertEq(_budget(owner).spentToday, VOUCHER_VALUE);
        assertEq(_agent(owner, optionsAgent).spentToday, 5 * ONE_AUSD);
        assertEq(controller.remainingToday(owner), DAILY_CAP - VOUCHER_VALUE);
        assertTrue(token.authorizationState(owner, voucher.nonce));
    }

    function test_redeem_voucher_emits_voucher_redeemed() public {
        // Arrange
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);

        // Assert (event declared before the call)
        vm.expectEmit(address(controller));
        emit BudgetController.VoucherRedeemed(owner, _today(), VOUCHER_VALUE);

        // Act
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_works_with_a_permit_mode_budget_under_shared_caps() public {
        // Arrange: a 4 AUSD recharge already counts against today's 10 AUSD cap.
        _openBudget(_defaultConfig());
        _recharge(optionsAgent, 4 * ONE_AUSD);
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(2 * ONE_AUSD, 6 * ONE_AUSD);

        // Act + Assert: 4 + 8 > 10
        vm.expectRevert(BudgetController.DailyCapExceeded.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_when_voucher_is_for_another_day() public {
        // Arrange: tomorrow's voucher presented today.
        _openVoucherBudget(_defaultConfig());
        Voucher memory tomorrow = _ownerVoucher(_today() + 1, VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);

        // Act + Assert
        vm.expectRevert(BudgetController.VoucherNotForToday.selector);
        _redeem(tomorrow, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_after_its_day_has_passed() public {
        // Arrange: today's voucher, presented tomorrow.
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);
        vm.warp(_nextUtcDayStart());

        // Act + Assert
        vm.expectRevert(BudgetController.VoucherNotForToday.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_when_redeemed_twice() public {
        // Arrange: 4 AUSD voucher redeemed once (two redemptions would still fit the caps).
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), 4 * ONE_AUSD);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(2 * ONE_AUSD, 2 * ONE_AUSD);
        _redeem(voucher, agentAddrs, amounts);

        // Act + Assert: the token refuses the used nonce.
        vm.expectRevert(MockAUSD.AuthorizationAlreadyUsed.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_when_owner_cancelled_it() public {
        // Arrange: the owner cancels today's voucher on the token (anyone may submit the cancellation).
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);
        _cancelVoucher(voucher.nonce);

        // Act + Assert
        vm.expectRevert(MockAUSD.AuthorizationAlreadyUsed.selector);
        _redeem(voucher, agentAddrs, amounts);
        assertEq(token.balanceOf(optionsAgent), 0);
    }

    function test_redeem_voucher_reverts_when_amounts_do_not_sum_to_value() public {
        // Arrange: 5 + 2 = 7 AUSD, but the voucher is for 8.
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 2 * ONE_AUSD);

        // Act + Assert
        vm.expectRevert(BudgetController.VoucherSplitMismatch.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_on_length_mismatch() public {
        // Arrange
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs,) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);
        uint128[] memory oneAmount = new uint128[](1);
        oneAmount[0] = VOUCHER_VALUE;

        // Act + Assert
        vm.expectRevert(BudgetController.LengthMismatch.selector);
        _redeem(voucher, agentAddrs, oneAmount);
    }

    function test_redeem_voucher_reverts_for_unregistered_agent() public {
        // Arrange: the news agent is not in the owner's config.
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);
        agentAddrs[1] = newsAgent;

        // Act + Assert
        vm.expectRevert(BudgetController.AgentInactive.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_when_agent_cap_exceeded() public {
        // Arrange: 7 AUSD to the options agent, whose cap is 6.
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(7 * ONE_AUSD, 1 * ONE_AUSD);

        // Act + Assert
        vm.expectRevert(BudgetController.AgentCapExceeded.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_when_agent_listed_twice_exceeds_its_cap() public {
        // Arrange: options agent listed twice, 4 + 4 = 8 > its 6 AUSD cap.
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(4 * ONE_AUSD, 4 * ONE_AUSD);
        agentAddrs[1] = optionsAgent;

        // Act + Assert
        vm.expectRevert(BudgetController.AgentCapExceeded.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_when_daily_cap_exceeded() public {
        // Arrange: 11 AUSD (6 + 5, each within its agent cap) is above the 10 AUSD daily cap.
        _openVoucherBudget(_defaultConfig());
        uint256 value = 11 * ONE_AUSD;
        Voucher memory voucher = _ownerVoucher(_today(), value);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(6 * ONE_AUSD, 5 * ONE_AUSD);

        // Act + Assert
        vm.expectRevert(BudgetController.DailyCapExceeded.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_when_signed_value_differs() public {
        // Arrange: the keeper claims 9 AUSD for a voucher the owner signed for 8.
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        voucher.value = 9 * ONE_AUSD;
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 4 * ONE_AUSD);

        // Act + Assert
        vm.expectRevert(MockAUSD.InvalidSignature.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_when_budget_paused() public {
        // Arrange
        _openVoucherBudget(_defaultConfig());
        vm.prank(owner);
        controller.setPaused(true);
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);

        // Act + Assert
        vm.expectRevert(BudgetController.BudgetPaused.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_when_budget_expired() public {
        // Arrange
        BudgetController.BudgetConfig memory cfg = _defaultConfig();
        _openVoucherBudget(cfg);
        vm.warp(cfg.expiry);
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);

        // Act + Assert
        vm.expectRevert(BudgetController.BudgetExpired.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_without_budget() public {
        // Arrange
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);

        // Act + Assert
        vm.expectRevert(BudgetController.NoBudget.selector);
        _redeem(voucher, agentAddrs, amounts);
    }

    function test_redeem_voucher_reverts_when_caller_is_not_keeper() public {
        // Arrange
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);

        // Act + Assert
        vm.expectRevert(BudgetController.NotKeeper.selector);
        vm.prank(stranger);
        controller.redeemVoucher(
            owner,
            voucher.value,
            voucher.validAfter,
            voucher.validBefore,
            voucher.nonce,
            voucher.signature,
            agentAddrs,
            amounts
        );
    }

    function test_vouchers_redeem_on_consecutive_days_with_fresh_caps() public {
        // Arrange: two days' vouchers, each 8 AUSD (16 AUSD > one daily cap, fine across two days).
        _openVoucherBudget(_defaultConfig());
        uint64 firstDay = _today();
        Voucher memory dayOne = _ownerVoucher(firstDay, VOUCHER_VALUE);
        Voucher memory dayTwo = _ownerVoucher(firstDay + 1, VOUCHER_VALUE);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(5 * ONE_AUSD, 3 * ONE_AUSD);

        // Act
        _redeem(dayOne, agentAddrs, amounts);
        vm.warp(_nextUtcDayStart());
        _redeem(dayTwo, agentAddrs, amounts);

        // Assert
        assertEq(token.balanceOf(optionsAgent), 10 * ONE_AUSD);
        assertEq(token.balanceOf(perpsAgent), 6 * ONE_AUSD);
        assertEq(_budget(owner).dayIndex, firstDay + 1);
        assertEq(_budget(owner).spentToday, VOUCHER_VALUE);
        assertEq(token.balanceOf(address(controller)), 0);
    }

    function testFuzz_redeem_voucher_never_leaves_funds_in_controller(uint128 optionsAmount, uint128 perpsAmount)
        public
    {
        // Arrange
        optionsAmount = uint128(bound(optionsAmount, 0, OPTIONS_AGENT_CAP));
        uint128 perpsMax = DAILY_CAP - optionsAmount < PERPS_AGENT_CAP ? DAILY_CAP - optionsAmount : PERPS_AGENT_CAP;
        perpsAmount = uint128(bound(perpsAmount, 0, perpsMax));
        uint256 value = uint256(optionsAmount) + perpsAmount;
        _openVoucherBudget(_defaultConfig());
        Voucher memory voucher = _ownerVoucher(_today(), value);
        (address[] memory agentAddrs, uint128[] memory amounts) = _split(optionsAmount, perpsAmount);
        uint256 ownerBalanceBefore = token.balanceOf(owner);

        // Act
        _redeem(voucher, agentAddrs, amounts);

        // Assert: value is conserved owner -> agents, nothing stays behind.
        assertEq(token.balanceOf(address(controller)), 0);
        assertEq(ownerBalanceBefore - token.balanceOf(owner), value);
        assertEq(token.balanceOf(optionsAgent) + token.balanceOf(perpsAgent), value);
    }

    function test_voucher_nonce_matches_spec_encoding() public view {
        // Arrange
        uint64 dayIndex = 20_372;
        bytes32 expected = keccak256(abi.encode(string("pocketdesk-voucher"), owner, uint256(dayIndex), VOUCHER_SALT));

        // Act
        bytes32 nonce = controller.voucherNonce(owner, dayIndex, VOUCHER_SALT);

        // Assert
        assertEq(nonce, expected);
    }

    // ─── helpers: configs ───────────────────────────────────────────────────────────────────

    /// @dev Owner's default config: options (6 AUSD) + perps (6 AUSD) under a 10 AUSD daily cap, 30 days.
    function _defaultConfig() internal view returns (BudgetController.BudgetConfig memory) {
        address[] memory agentAddrs = new address[](2);
        agentAddrs[0] = optionsAgent;
        agentAddrs[1] = perpsAgent;
        uint128[] memory agentCaps = new uint128[](2);
        agentCaps[0] = OPTIONS_AGENT_CAP;
        agentCaps[1] = PERPS_AGENT_CAP;
        bytes32[] memory sectors = new bytes32[](2);
        sectors[0] = SECTOR_OPTIONS;
        sectors[1] = SECTOR_PERPS;
        return BudgetController.BudgetConfig({
            owner: owner,
            dailyCap: DAILY_CAP,
            expiry: uint64(block.timestamp) + BUDGET_LIFETIME,
            agentAddrs: agentAddrs,
            agentCaps: agentCaps,
            sectors: sectors,
            nonce: controller.configNonce(owner)
        });
    }

    /// @dev Same budget, but the perps agent is replaced by the news agent.
    function _configWithOptionsAndNews() internal view returns (BudgetController.BudgetConfig memory cfg) {
        cfg = _defaultConfig();
        cfg.agentAddrs[1] = newsAgent;
        cfg.agentCaps[1] = NEWS_AGENT_CAP;
        cfg.sectors[1] = SECTOR_NEWS;
    }

    // ─── helpers: actions ───────────────────────────────────────────────────────────────────

    /// @dev Owner signs a fresh permit + the config; the keeper submits openBudget.
    function _openBudget(BudgetController.BudgetConfig memory cfg) internal {
        _submitOpenBudget(cfg, _ownerConfigSig(cfg));
    }

    /// @dev Keeper submits openBudget with a fresh owner permit and the given config signature.
    function _submitOpenBudget(BudgetController.BudgetConfig memory cfg, bytes memory cfgSig) internal {
        PermitSig memory permit = _ownerPermit();
        vm.prank(keeper);
        controller.openBudget(owner, permit.value, permit.deadline, permit.v, permit.r, permit.s, cfg, cfgSig);
    }

    /// @dev Like `_submitOpenBudget`, but expects `expectedError`. The permit is signed first, because
    ///      `vm.expectRevert` applies to the very next external call (signing reads `token.nonces`).
    function _expectOpenBudgetRevert(
        BudgetController.BudgetConfig memory cfg,
        bytes memory cfgSig,
        bytes4 expectedError
    ) internal {
        PermitSig memory permit = _ownerPermit();
        vm.expectRevert(expectedError);
        vm.prank(keeper);
        controller.openBudget(owner, permit.value, permit.deadline, permit.v, permit.r, permit.s, cfg, cfgSig);
    }

    /// @dev Owner signs the config; the keeper submits updateConfigWithSig.
    function _updateConfig(BudgetController.BudgetConfig memory cfg) internal {
        bytes memory cfgSig = _ownerConfigSig(cfg);
        vm.prank(keeper);
        controller.updateConfigWithSig(cfg, cfgSig);
    }

    /// @dev Owner signs SetPaused with the current nonce; the keeper submits it.
    function _setPausedWithOwnerSig(bool paused, uint256 deadline) internal {
        uint256 nonce = controller.configNonce(owner);
        bytes memory sig = _sign(OWNER_PRIVATE_KEY, _specSetPausedDigest(owner, paused, nonce, deadline));
        vm.prank(keeper);
        controller.setPausedWithSig(owner, paused, deadline, sig);
    }

    function _recharge(address agent, uint128 amount) internal {
        vm.prank(keeper);
        controller.recharge(owner, agent, amount);
    }

    // ─── helpers: signatures (built from the spec, not from the contract) ───────────────────

    /// @dev An owner-signed ERC-2612 permit for PERMIT_VALUE, valid for SIGNATURE_LIFETIME.
    function _ownerPermit() internal view returns (PermitSig memory permit) {
        permit.value = PERMIT_VALUE;
        permit.deadline = block.timestamp + SIGNATURE_LIFETIME;
        (permit.v, permit.r, permit.s) = _signPermit(OWNER_PRIVATE_KEY, permit.value, permit.deadline);
    }

    /// @dev The owner's signature over `cfg`.
    function _ownerConfigSig(BudgetController.BudgetConfig memory cfg) internal view returns (bytes memory) {
        return _sign(OWNER_PRIVATE_KEY, _specConfigDigest(cfg));
    }

    /// @dev ERC-2612 digest on the token's "Agora Dollar" domain, signed with `privateKey`.
    function _signPermit(uint256 privateKey, uint256 value, uint256 deadline)
        internal
        view
        returns (uint8 v, bytes32 r, bytes32 s)
    {
        address signer = vm.addr(privateKey);
        bytes32 structHash =
            keccak256(abi.encode(PERMIT_TYPEHASH, signer, address(controller), value, token.nonces(signer), deadline));
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", token.DOMAIN_SEPARATOR(), structHash));
        return vm.sign(privateKey, digest);
    }

    /// @dev Domain separator per EIP-712 for ("PocketDesk", "1", chainid, verifyingContract).
    function _specDomainSeparator(address verifyingContract) internal view returns (bytes32) {
        return keccak256(
            abi.encode(
                EIP712_DOMAIN_TYPEHASH, keccak256("PocketDesk"), keccak256("1"), block.chainid, verifyingContract
            )
        );
    }

    /// @dev BudgetConfig digest per docs/SPEC.md §2 (arrays hashed as keccak256(abi.encodePacked(array))).
    function _specConfigDigest(BudgetController.BudgetConfig memory cfg) internal view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                SPEC_BUDGET_CONFIG_TYPEHASH,
                cfg.owner,
                cfg.dailyCap,
                cfg.expiry,
                keccak256(abi.encodePacked(cfg.agentAddrs)),
                keccak256(abi.encodePacked(cfg.agentCaps)),
                keccak256(abi.encodePacked(cfg.sectors)),
                cfg.nonce
            )
        );
        return keccak256(abi.encodePacked("\x19\x01", _specDomainSeparator(address(controller)), structHash));
    }

    /// @dev SetPaused digest per docs/SPEC.md §2.
    function _specSetPausedDigest(address budgetOwner, bool paused, uint256 nonce, uint256 deadline)
        internal
        view
        returns (bytes32)
    {
        bytes32 structHash = keccak256(abi.encode(SPEC_SET_PAUSED_TYPEHASH, budgetOwner, paused, nonce, deadline));
        return keccak256(abi.encodePacked("\x19\x01", _specDomainSeparator(address(controller)), structHash));
    }

    /// @dev 65-byte r ‖ s ‖ v signature, the format wallets and eth_account produce.
    function _sign(uint256 privateKey, bytes32 digest) internal pure returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey, digest);
        return abi.encodePacked(r, s, v);
    }

    // ─── helpers: vouchers ──────────────────────────────────────────────────────────────────

    /// @dev Owner signs the config; the keeper opens the budget in voucher mode.
    function _openVoucherBudget(BudgetController.BudgetConfig memory cfg) internal {
        bytes memory cfgSig = _ownerConfigSig(cfg);
        vm.prank(keeper);
        controller.openVoucherBudget(cfg, cfgSig);
    }

    /// @dev Today's UTC day index, as the contract computes it.
    function _today() internal view returns (uint64) {
        return uint64(block.timestamp / 1 days);
    }

    /**
     * @dev The owner's voucher for UTC day `dayIndex`, built from the spec: a ReceiveWithAuthorization
     *      to the controller with validAfter = dayStart − 1, validBefore = dayEnd and the deterministic nonce.
     */
    function _ownerVoucher(uint64 dayIndex, uint256 value) internal view returns (Voucher memory voucher) {
        uint256 dayStart = uint256(dayIndex) * 1 days;
        voucher.value = value;
        voucher.validAfter = dayStart - 1;
        voucher.validBefore = dayStart + 1 days;
        voucher.nonce = keccak256(abi.encode(string("pocketdesk-voucher"), owner, uint256(dayIndex), VOUCHER_SALT));
        bytes32 structHash = keccak256(
            abi.encode(
                RECEIVE_WITH_AUTHORIZATION_TYPEHASH,
                owner,
                address(controller),
                value,
                voucher.validAfter,
                voucher.validBefore,
                voucher.nonce
            )
        );
        voucher.signature = _signOnToken(OWNER_PRIVATE_KEY, structHash);
    }

    /// @dev Parallel (agents, amounts) arrays: options agent gets `optionsAmount`, perps agent `perpsAmount`.
    function _split(uint128 optionsAmount, uint128 perpsAmount)
        internal
        view
        returns (address[] memory agentAddrs, uint128[] memory amounts)
    {
        agentAddrs = new address[](2);
        agentAddrs[0] = optionsAgent;
        agentAddrs[1] = perpsAgent;
        amounts = new uint128[](2);
        amounts[0] = optionsAmount;
        amounts[1] = perpsAmount;
    }

    /// @dev Keeper redeems `voucher` with the given split.
    function _redeem(Voucher memory voucher, address[] memory agentAddrs, uint128[] memory amounts) internal {
        vm.prank(keeper);
        controller.redeemVoucher(
            owner,
            voucher.value,
            voucher.validAfter,
            voucher.validBefore,
            voucher.nonce,
            voucher.signature,
            agentAddrs,
            amounts
        );
    }

    /// @dev Owner signs CancelAuthorization for `nonce`; a stranger submits it to the token.
    function _cancelVoucher(bytes32 nonce) internal {
        bytes32 structHash = keccak256(abi.encode(CANCEL_AUTHORIZATION_TYPEHASH, owner, nonce));
        bytes memory signature = _signOnToken(OWNER_PRIVATE_KEY, structHash);
        vm.prank(stranger);
        token.cancelAuthorization(owner, nonce, signature);
    }

    /// @dev 65-byte signature over an EIP-712 struct hash in the token's "Agora Dollar" domain.
    function _signOnToken(uint256 privateKey, bytes32 structHash) internal view returns (bytes memory) {
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", token.DOMAIN_SEPARATOR(), structHash));
        return _sign(privateKey, digest);
    }

    // ─── helpers: state readers ─────────────────────────────────────────────────────────────

    function _budget(address budgetOwner) internal view returns (BudgetController.Budget memory budget) {
        (
            budget.dailyCap,
            budget.spentToday,
            budget.dayIndex,
            budget.expiry,
            budget.paused,
            budget.exists,
            budget.cfgNonce,
            budget.mode
        ) = controller.budgets(budgetOwner);
    }

    function _agent(address budgetOwner, address agent) internal view returns (BudgetController.Agent memory a) {
        (a.active, a.dailyCap, a.spentToday, a.dayIndex, a.sector) = controller.agents(budgetOwner, agent);
    }

    /// @dev Unix timestamp of the next 00:00 UTC.
    function _nextUtcDayStart() internal view returns (uint256) {
        return (block.timestamp / 1 days + 1) * 1 days;
    }

    /// @dev Deploy a controller at PINNED_CONTROLLER_ADDRESS (chain 84532) to compare against Python digests.
    function _deployControllerAtPinnedAddress() internal returns (BudgetController) {
        deployCodeTo(
            "BudgetController.sol:BudgetController", abi.encode(address(token), keeper), PINNED_CONTROLLER_ADDRESS
        );
        return BudgetController(PINNED_CONTROLLER_ADDRESS);
    }
}

/**
 * @notice Minimal ERC-1271 smart wallet for tests: a signature is valid iff it recovers to `signer`.
 */
contract MockSmartWallet {
    bytes4 private constant ERC1271_MAGIC_VALUE = 0x1626ba7e; // bytes4(keccak256("isValidSignature(bytes32,bytes)"))
    bytes4 private constant ERC1271_INVALID = 0xffffffff;

    address private immutable SIGNER;

    constructor(address walletSigner) {
        SIGNER = walletSigner;
    }

    function isValidSignature(bytes32 hash, bytes calldata signature) external view returns (bytes4) {
        (address recovered, ECDSA.RecoverError error,) = ECDSA.tryRecover(hash, signature);
        bool valid = error == ECDSA.RecoverError.NoError && recovered == SIGNER;
        return valid ? ERC1271_MAGIC_VALUE : ERC1271_INVALID;
    }

    /// @dev The wallet's signer can make it approve a spender (stands in for a smart-wallet user operation).
    function approveToken(MockAUSD token, address spender, uint256 amount) external {
        require(msg.sender == SIGNER, "only signer");
        token.approve(spender, amount);
    }
}
