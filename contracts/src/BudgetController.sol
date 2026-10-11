// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Permit.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/**
 * @title  BudgetController
 * @notice PocketDesk's on-chain spending guard. A user grants this contract a capped, revocable AUSD
 *         allowance (ERC-2612 permit). That allowance can only ever flow to the user's own registered
 *         agent wallets, within a daily cap for the whole budget and a daily cap per agent.
 *
 * Where this fits in the PocketDesk flow:
 *   1. In the web app the user signs two messages (no gas needed):
 *        a. an ERC-2612 `Permit` on the AUSD token, naming this contract as spender, and
 *        b. a `BudgetConfig` (EIP-712 domain "PocketDesk" / "1") listing the daily cap, expiry,
 *           agent wallets, per-agent daily caps and sector labels.
 *   2. The keeper (our relayer; the only wallet that pays gas) submits both via `openBudget`.
 *   3. Each session the keeper calls `recharge(owner, agent, amount)` to top up an agent wallet.
 *      AUSD moves straight from the owner to the agent with `transferFrom`, so this contract
 *      never holds any funds itself.
 *   4. Agents spend their AUSD on research items over x402 (ERC-3009) — outside this contract.
 *   5. The user stays in control: pause / resume (`setPaused`, or gasless `setPausedWithSig`),
 *      change caps or agents (`updateConfigWithSig`), or revoke everything by signing a permit
 *      with value 0, which the keeper submits straight to the token.
 *
 * Typed data (must match backend/app/chain/eip712.py exactly):
 *   BudgetConfig(address owner,uint128 dailyCap,uint64 expiry,address[] agentAddrs,
 *                uint128[] agentCaps,bytes32[] sectors,uint256 nonce)
 *   SetPaused(address owner,bool paused,uint256 nonce,uint256 deadline)
 *   Array members are hashed as keccak256(abi.encodePacked(array)); every element of these
 *   arrays is a 32-byte padded static type, which is exactly EIP-712's array encoding.
 *   Both messages share one replay counter per owner: `budgets[owner].cfgNonce`.
 *
 * Days are UTC calendar days: dayIndex = block.timestamp / 1 days. Spend counters reset lazily —
 * the first recharge of a new day zeroes them, and the view functions apply the same rule.
 */
contract BudgetController is EIP712, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ─── constants ──────────────────────────────────────────────────────────────────────────

    /// @dev EIP-712 signing domain of this contract (mirrors POCKETDESK_EIP712_NAME / _VERSION).
    string private constant SIGNING_DOMAIN_NAME = "PocketDesk";
    string private constant SIGNING_DOMAIN_VERSION = "1";

    /// @notice Type hash of the `BudgetConfig` struct the owner signs.
    bytes32 public constant BUDGET_CONFIG_TYPEHASH = keccak256(
        "BudgetConfig(address owner,uint128 dailyCap,uint64 expiry,address[] agentAddrs,uint128[] agentCaps,bytes32[] sectors,uint256 nonce)"
    );

    /// @notice Type hash of the `SetPaused` struct the owner signs for a gasless pause / resume.
    bytes32 public constant SET_PAUSED_TYPEHASH =
        keccak256("SetPaused(address owner,bool paused,uint256 nonce,uint256 deadline)");

    /// @notice Upper bound on agents per budget. Keeps every agent loop (and the keeper's gas) bounded.
    uint256 public constant MAX_AGENTS = 16;

    /// @dev Length of one budget "day". Day boundaries are 00:00 UTC.
    uint256 private constant SECONDS_PER_DAY = 1 days;

    // ─── types ──────────────────────────────────────────────────────────────────────────────

    /// @notice One user's budget. `spentToday` counts what was recharged on day `dayIndex`.
    struct Budget {
        uint128 dailyCap; // max AUSD (atomic units, 6 decimals) recharged per UTC day, all agents combined
        uint128 spentToday; // recharged so far on `dayIndex`
        uint64 dayIndex; // UTC day the spend counter belongs to
        uint64 expiry; // unix seconds; recharges stop at this moment
        bool paused; // owner's kill switch
        bool exists; // true once `openBudget` succeeded
        uint256 cfgNonce; // replay counter shared by BudgetConfig and SetPaused signatures
    }

    /// @notice One agent wallet registered under an owner's budget.
    struct Agent {
        bool active; // false for agents dropped from the latest config
        uint128 dailyCap; // max AUSD recharged to this agent per UTC day
        uint128 spentToday; // recharged to this agent so far on `dayIndex`
        uint64 dayIndex; // UTC day the spend counter belongs to
        bytes32 sector; // label such as "OptionsEvent", right-padded with zero bytes
    }

    /// @notice The budget settings an owner signs (EIP-712 `BudgetConfig`).
    /// @dev The three arrays are parallel: agent i is agentAddrs[i] with cap agentCaps[i] and sector sectors[i].
    struct BudgetConfig {
        address owner;
        uint128 dailyCap;
        uint64 expiry;
        address[] agentAddrs;
        uint128[] agentCaps;
        bytes32[] sectors;
        uint256 nonce;
    }

    // ─── storage ────────────────────────────────────────────────────────────────────────────

    /// @notice The AUSD token. It also implements IERC20Permit (ERC-2612).
    /// @dev Lower-case name kept on purpose: the public getter `ausd()` is part of the documented interface.
    // forge-lint: disable-next-line(screaming-snake-case-immutable)
    IERC20 public immutable ausd;

    /// @notice Relayer allowed to call openBudget / recharge / updateConfigWithSig / setPausedWithSig.
    address public keeper;

    /// @notice Deployer; the only address that can replace the keeper.
    address public admin;

    /// @notice owner => budget. Note: `spentToday` here is the raw stored value; use `remainingToday`
    ///         for a figure that already accounts for a day rollover.
    mapping(address owner => Budget) public budgets;

    /// @notice owner => agent wallet => agent settings.
    mapping(address owner => mapping(address agent => Agent)) public agents;

    /// @dev owner => agents listed in the owner's latest config (used to deactivate dropped agents).
    mapping(address owner => address[]) internal agentList;

    // ─── events ─────────────────────────────────────────────────────────────────────────────

    event BudgetOpened(address indexed owner, uint128 dailyCap, uint64 expiry, uint256 agentCount);
    event ConfigUpdated(address indexed owner, uint128 dailyCap, uint64 expiry, uint256 agentCount);
    event AgentConfigured(address indexed owner, address indexed agent, uint128 dailyCap, bytes32 sector);
    event AgentDeactivated(address indexed owner, address indexed agent);
    event Recharged(
        address indexed owner, address indexed agent, uint128 amount, uint128 spentToday, uint128 agentSpentToday
    );
    event Paused(address indexed owner, bool paused);
    event KeeperChanged(address indexed previousKeeper, address indexed newKeeper);

    // ─── errors ─────────────────────────────────────────────────────────────────────────────

    error NotKeeper(); // caller is not the keeper
    error NotAdmin(); // caller is not the admin
    error BadSignature(); // signature is not the owner's, or signs a different owner / stale nonce
    error BadNonce(); // BudgetConfig.nonce != budgets[owner].cfgNonce
    error NoBudget(); // owner has never opened a budget
    error BudgetPaused(); // owner paused the budget
    error BudgetExpired(); // block.timestamp >= expiry
    error AgentInactive(); // agent is not listed in the owner's current config
    error DailyCapExceeded(); // recharge would push the budget's spend today above dailyCap
    error AgentCapExceeded(); // recharge would push the agent's spend today above its dailyCap
    error LengthMismatch(); // agentAddrs, agentCaps and sectors have different lengths
    error ZeroAddress(); // a required address is address(0)
    error AgentCapAboveDailyCap(); // one agent's cap is larger than the whole budget's cap
    error InsufficientAllowance(); // allowance(owner, this) < dailyCap after the permit
    error SignatureExpired(); // SetPaused deadline has passed
    error DuplicateAgent(); // the same agent address appears twice in one config
    error TooManyAgents(); // config lists more than MAX_AGENTS agents
    error InvalidAgent(); // agent address is this contract (funds would be stuck here)

    // ─── modifiers ──────────────────────────────────────────────────────────────────────────

    modifier onlyKeeper() {
        if (msg.sender != keeper) revert NotKeeper();
        _;
    }

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    // ─── constructor ────────────────────────────────────────────────────────────────────────

    /// @param ausdToken     the AUSD token (ERC-20 + ERC-2612)
    /// @param initialKeeper the relayer wallet that submits users' signed messages and recharges
    constructor(address ausdToken, address initialKeeper) EIP712(SIGNING_DOMAIN_NAME, SIGNING_DOMAIN_VERSION) {
        if (ausdToken == address(0) || initialKeeper == address(0)) revert ZeroAddress();
        ausd = IERC20(ausdToken);
        keeper = initialKeeper;
        admin = msg.sender;
        emit KeeperChanged(address(0), initialKeeper);
    }

    // ─── budget lifecycle (keeper submits the owner's signatures) ───────────────────────────

    /**
     * @notice Open (or re-open) `owner`'s budget from their permit + signed config, in one transaction.
     * @dev Rules, in order:
     *      1. `cfgSig` must be `owner`'s signature over `cfg`, `cfg.owner == owner`, and
     *         `cfg.nonce == budgets[owner].cfgNonce` (the nonce is then incremented).
     *      2. The permit is applied inside try/catch: anyone may submit the same permit to the token
     *         first (front-running), which would make our call revert although the allowance is in place.
     *      3. Afterwards `allowance(owner, this) >= cfg.dailyCap` must hold, whichever way it got there.
     *      4. The config is stored; agents from an earlier config that are not listed are deactivated.
     *         Re-opening un-pauses, but keeps today's spend counters so it cannot reset a daily cap.
     */
    function openBudget(
        address owner,
        uint256 permitValue,
        uint256 permitDeadline,
        uint8 v,
        bytes32 r,
        bytes32 s,
        BudgetConfig calldata cfg,
        bytes calldata cfgSig
    ) external onlyKeeper nonReentrant {
        if (cfg.owner != owner) revert BadSignature();
        _consumeConfigSignature(cfg, cfgSig);

        _tryPermit(owner, permitValue, permitDeadline, v, r, s);
        if (ausd.allowance(owner, address(this)) < cfg.dailyCap) revert InsufficientAllowance();

        Budget storage budget = budgets[owner];
        budget.dailyCap = cfg.dailyCap;
        budget.expiry = cfg.expiry;
        budget.paused = false;
        budget.exists = true;
        _replaceAgents(cfg);

        emit BudgetOpened(owner, cfg.dailyCap, cfg.expiry, cfg.agentAddrs.length);
    }

    /**
     * @notice Change caps, expiry or agents of an existing budget using a newly signed config.
     * @dev Same signature + nonce rules as `openBudget` (the owner is `cfg.owner`). Keeps today's
     *      spend counters and the paused flag. Agents not listed in `cfg` are deactivated.
     *      The allowance is not re-checked: it shrinks with every recharge, so it may legitimately
     *      be below the daily cap late in a budget's life.
     */
    function updateConfigWithSig(BudgetConfig calldata cfg, bytes calldata sig) external onlyKeeper {
        Budget storage budget = budgets[cfg.owner];
        if (!budget.exists) revert NoBudget();
        _consumeConfigSignature(cfg, sig);

        budget.dailyCap = cfg.dailyCap;
        budget.expiry = cfg.expiry;
        _replaceAgents(cfg);

        emit ConfigUpdated(cfg.owner, cfg.dailyCap, cfg.expiry, cfg.agentAddrs.length);
    }

    // ─── recharging agents ──────────────────────────────────────────────────────────────────

    /**
     * @notice Move `amount` AUSD from `owner` straight to one of their registered agent wallets.
     * @dev Requires: budget exists, not paused, block.timestamp < expiry, agent active.
     *      Day rollover: if the stored dayIndex is not today, that spend counter restarts at 0.
     *      Caps:  budget.spentToday + amount <= budget.dailyCap
     *             agent.spentToday  + amount <= agent.dailyCap
     *      Counters are updated before the token transfer (checks-effects-interactions).
     */
    function recharge(address owner, address agent, uint128 amount) external onlyKeeper nonReentrant {
        Budget storage budget = budgets[owner];
        _requireSpendable(budget);
        Agent storage agentCfg = agents[owner][agent];
        if (!agentCfg.active) revert AgentInactive();

        uint64 today = _currentDayIndex();
        _rollBudgetDay(budget, today);
        _rollAgentDay(agentCfg, today);

        uint256 newBudgetSpent = uint256(budget.spentToday) + amount;
        uint256 newAgentSpent = uint256(agentCfg.spentToday) + amount;
        if (newBudgetSpent > budget.dailyCap) revert DailyCapExceeded();
        if (newAgentSpent > agentCfg.dailyCap) revert AgentCapExceeded();

        // Both values are <= a uint128 cap (checked just above), so the downcasts are lossless.
        // forge-lint: disable-next-line(unsafe-typecast)
        budget.spentToday = uint128(newBudgetSpent);
        // forge-lint: disable-next-line(unsafe-typecast)
        agentCfg.spentToday = uint128(newAgentSpent);

        ausd.safeTransferFrom(owner, agent, amount);
        emit Recharged(owner, agent, amount, budget.spentToday, agentCfg.spentToday);
    }

    // ─── pausing ────────────────────────────────────────────────────────────────────────────

    /// @notice Owner pauses or resumes their own budget directly (the owner pays gas).
    function setPaused(bool paused) external {
        Budget storage budget = budgets[msg.sender];
        if (!budget.exists) revert NoBudget();
        budget.paused = paused;
        emit Paused(msg.sender, paused);
    }

    /**
     * @notice Gasless pause / resume: the owner signs `SetPaused`, the keeper submits it.
     * @dev Rules: block.timestamp <= deadline; the signature must be the owner's over
     *      (owner, paused, budgets[owner].cfgNonce, deadline); the nonce is then incremented.
     *      A signature over a stale nonce therefore fails as `BadSignature`.
     */
    function setPausedWithSig(address owner, bool paused, uint256 deadline, bytes calldata sig) external onlyKeeper {
        if (block.timestamp > deadline) revert SignatureExpired();
        Budget storage budget = budgets[owner];
        if (!budget.exists) revert NoBudget();

        bytes32 digest = hashSetPaused(owner, paused, budget.cfgNonce, deadline);
        if (!SignatureChecker.isValidSignatureNow(owner, digest, sig)) revert BadSignature();
        budget.cfgNonce += 1;

        budget.paused = paused;
        emit Paused(owner, paused);
    }

    // ─── admin ──────────────────────────────────────────────────────────────────────────────

    /// @notice Replace the keeper (e.g. after rotating the relayer key). Admin only.
    function setKeeper(address newKeeper) external onlyAdmin {
        if (newKeeper == address(0)) revert ZeroAddress();
        emit KeeperChanged(keeper, newKeeper);
        keeper = newKeeper;
    }

    // ─── views ──────────────────────────────────────────────────────────────────────────────

    /**
     * @notice How much more can be recharged today across all of `owner`'s agents.
     * @dev = max(0, dailyCap − spentToday), with spentToday read as 0 if the stored day is not today.
     *      Returns 0 if the budget does not exist, is paused, or has expired.
     */
    function remainingToday(address owner) external view returns (uint256) {
        Budget storage budget = budgets[owner];
        if (!_isSpendable(budget)) return 0;
        return _budgetHeadroom(budget);
    }

    /**
     * @notice The largest amount `recharge(owner, agent, ·)` would accept right now.
     * @dev = min(agent headroom, budget headroom), each with the same day-rollover rule as
     *      `remainingToday`. Returns 0 if the budget is not spendable or the agent is inactive.
     *      Does not look at the owner's token balance or remaining allowance.
     */
    function agentRemainingToday(address owner, address agent) external view returns (uint256) {
        Budget storage budget = budgets[owner];
        Agent storage agentCfg = agents[owner][agent];
        if (!_isSpendable(budget) || !agentCfg.active) return 0;

        uint256 agentHeadroom = _agentHeadroom(agentCfg);
        uint256 budgetHeadroom = _budgetHeadroom(budget);
        return agentHeadroom < budgetHeadroom ? agentHeadroom : budgetHeadroom;
    }

    /// @notice Agents listed in `owner`'s current config (dropped agents are not included).
    function getAgents(address owner) external view returns (address[] memory) {
        return agentList[owner];
    }

    /// @notice The nonce the owner's next BudgetConfig or SetPaused signature must use.
    function configNonce(address owner) external view returns (uint256) {
        return budgets[owner].cfgNonce;
    }

    /// @notice EIP-712 domain separator of this contract ("PocketDesk", "1", chainId, this).
    function domainSeparator() external view returns (bytes32) {
        return _domainSeparatorV4();
    }

    /**
     * @notice Full EIP-712 digest of a BudgetConfig: keccak256("\x19\x01" ‖ domainSeparator ‖ structHash).
     * @dev Exposed so the Python and TypeScript signers can cross-check their digests on-chain.
     */
    function hashBudgetConfig(BudgetConfig calldata cfg) public view returns (bytes32) {
        return _hashTypedDataV4(_budgetConfigStructHash(cfg));
    }

    /// @notice Full EIP-712 digest of a SetPaused message (same layout as `hashBudgetConfig`).
    function hashSetPaused(address owner, bool paused, uint256 nonce, uint256 deadline) public view returns (bytes32) {
        bytes32 structHash = keccak256(abi.encode(SET_PAUSED_TYPEHASH, owner, paused, nonce, deadline));
        return _hashTypedDataV4(structHash);
    }

    // ─── internal: signatures ───────────────────────────────────────────────────────────────

    /**
     * @dev structHash = keccak256(abi.encode(BUDGET_CONFIG_TYPEHASH, owner, dailyCap, expiry,
     *          keccak256(abi.encodePacked(agentAddrs)), keccak256(abi.encodePacked(agentCaps)),
     *          keccak256(abi.encodePacked(sectors)), nonce))
     */
    function _budgetConfigStructHash(BudgetConfig calldata cfg) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(
                BUDGET_CONFIG_TYPEHASH,
                cfg.owner,
                cfg.dailyCap,
                cfg.expiry,
                keccak256(abi.encodePacked(cfg.agentAddrs)),
                keccak256(abi.encodePacked(cfg.agentCaps)),
                keccak256(abi.encodePacked(cfg.sectors)),
                cfg.nonce
            )
        );
    }

    /**
     * @dev Rule: cfg.nonce == budgets[cfg.owner].cfgNonce and `sig` is cfg.owner's signature over the
     *      config digest. Uses SignatureChecker, so both EOAs (ECDSA) and smart wallets (ERC-1271) work.
     *      On success the nonce is incremented, so the same signed config can never be replayed.
     */
    function _consumeConfigSignature(BudgetConfig calldata cfg, bytes calldata sig) internal {
        Budget storage budget = budgets[cfg.owner];
        if (cfg.nonce != budget.cfgNonce) revert BadNonce();
        if (!SignatureChecker.isValidSignatureNow(cfg.owner, hashBudgetConfig(cfg), sig)) revert BadSignature();
        budget.cfgNonce += 1;
    }

    /**
     * @dev Submit the owner's ERC-2612 permit, ignoring failure. If someone already submitted this
     *      permit (front-run), the token rejects the reused nonce; that is fine, because the caller
     *      checks the resulting allowance right afterwards.
     */
    function _tryPermit(address owner, uint256 value, uint256 deadline, uint8 v, bytes32 r, bytes32 s) internal {
        try IERC20Permit(address(ausd)).permit(owner, address(this), value, deadline, v, r, s) {} catch {}
    }

    // ─── internal: agents ───────────────────────────────────────────────────────────────────

    /**
     * @dev Make `cfg`'s agent list the owner's only active agents.
     *      1. Deactivate every agent from the previous config.
     *      2. Validate and (re)activate every agent in `cfg`. Spend counters of re-listed agents are kept.
     *      3. Emit AgentDeactivated for previous agents that were not re-listed.
     */
    function _replaceAgents(BudgetConfig calldata cfg) internal {
        uint256 count = cfg.agentAddrs.length;
        if (cfg.agentCaps.length != count || cfg.sectors.length != count) revert LengthMismatch();
        if (count > MAX_AGENTS) revert TooManyAgents();

        address owner = cfg.owner;
        address[] memory previousAgents = agentList[owner];
        for (uint256 i = 0; i < previousAgents.length; ++i) {
            agents[owner][previousAgents[i]].active = false;
        }
        delete agentList[owner];

        for (uint256 i = 0; i < count; ++i) {
            _registerAgent(owner, cfg.dailyCap, cfg.agentAddrs[i], cfg.agentCaps[i], cfg.sectors[i]);
        }

        for (uint256 i = 0; i < previousAgents.length; ++i) {
            if (!agents[owner][previousAgents[i]].active) emit AgentDeactivated(owner, previousAgents[i]);
        }
    }

    /**
     * @dev Rules for one agent entry: agent != address(0); agent != this contract; agentCap <= dailyCap;
     *      not already listed in this config (all agents were deactivated first, so an active
     *      entry here means a duplicate).
     */
    function _registerAgent(address owner, uint128 budgetDailyCap, address agent, uint128 agentCap, bytes32 sector)
        internal
    {
        if (agent == address(0)) revert ZeroAddress();
        if (agent == address(this)) revert InvalidAgent();
        if (agentCap > budgetDailyCap) revert AgentCapAboveDailyCap();

        Agent storage agentCfg = agents[owner][agent];
        if (agentCfg.active) revert DuplicateAgent();
        agentCfg.active = true;
        agentCfg.dailyCap = agentCap;
        agentCfg.sector = sector;

        agentList[owner].push(agent);
        emit AgentConfigured(owner, agent, agentCap, sector);
    }

    // ─── internal: days and caps ────────────────────────────────────────────────────────────

    /// @dev Today's UTC day number: floor(block.timestamp / 1 day).
    function _currentDayIndex() internal view returns (uint64) {
        // A uint64 day count lasts ~5e16 years, so this downcast cannot truncate.
        // forge-lint: disable-next-line(unsafe-typecast)
        return uint64(block.timestamp / SECONDS_PER_DAY);
    }

    /// @dev Spend recorded on `dayIndex`, read as 0 when that day is not today (lazy daily reset).
    function _spentOn(uint128 spentToday, uint64 dayIndex) internal view returns (uint256) {
        return dayIndex == _currentDayIndex() ? spentToday : 0;
    }

    /// @dev On a new day, restart the budget's spend counter at 0.
    function _rollBudgetDay(Budget storage budget, uint64 today) internal {
        if (budget.dayIndex != today) {
            budget.dayIndex = today;
            budget.spentToday = 0;
        }
    }

    /// @dev On a new day, restart the agent's spend counter at 0.
    function _rollAgentDay(Agent storage agentCfg, uint64 today) internal {
        if (agentCfg.dayIndex != today) {
            agentCfg.dayIndex = today;
            agentCfg.spentToday = 0;
        }
    }

    /// @dev max(0, dailyCap − spent today). The max(0, ·) matters after a config lowered the cap mid-day.
    function _budgetHeadroom(Budget storage budget) internal view returns (uint256) {
        uint256 spent = _spentOn(budget.spentToday, budget.dayIndex);
        return spent >= budget.dailyCap ? 0 : budget.dailyCap - spent;
    }

    /// @dev max(0, agent dailyCap − agent spent today).
    function _agentHeadroom(Agent storage agentCfg) internal view returns (uint256) {
        uint256 spent = _spentOn(agentCfg.spentToday, agentCfg.dayIndex);
        return spent >= agentCfg.dailyCap ? 0 : agentCfg.dailyCap - spent;
    }

    /// @dev A budget can be recharged from iff it exists, is not paused and block.timestamp < expiry.
    function _isSpendable(Budget storage budget) internal view returns (bool) {
        return budget.exists && !budget.paused && block.timestamp < budget.expiry;
    }

    /// @dev Same rule as `_isSpendable`, but reverts with the specific reason.
    function _requireSpendable(Budget storage budget) internal view {
        if (!budget.exists) revert NoBudget();
        if (budget.paused) revert BudgetPaused();
        if (block.timestamp >= budget.expiry) revert BudgetExpired();
    }
}
