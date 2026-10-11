// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";

/**
 * @title  MockAUSD — LOCAL TEST MOCK of the Agora Dollar (AUSD) testnet token
 * @notice !!! TEST ONLY. Used on a local anvil node (started with --chain-id 84532) by the Foundry and
 *         pytest suites. Never deploy this to a public chain: anyone can mint it. !!!
 *
 * Why it exists: PocketDesk's payment path (permit → BudgetController → ERC-3009 x402 payments) must be
 * tested end to end without network access to Base Sepolia. This contract reproduces the parts of the
 * real AUSD token (0xa9012a055bd4e0eDfF8Ce09f960291C09D5322dC) that PocketDesk relies on:
 *
 *   - ERC-20 with `name()` == "AUSD" and 6 decimals.
 *   - EIP-712 signing domain name "Agora Dollar", version "1" — deliberately different from `name()`,
 *     exactly like the real token. `eip712Domain()` (ERC-5267) is inherited from OpenZeppelin.
 *   - ERC-2612 `permit` / `nonces` (OpenZeppelin ERC20Permit).
 *   - ERC-3009: `transferWithAuthorization`, `receiveWithAuthorization` (each with both the (v,r,s) and
 *     the `bytes signature` overload), `cancelAuthorization`, `authorizationState`, and the events
 *     `AuthorizationUsed` / `AuthorizationCanceled`. Validity window is strict:
 *     validAfter < block.timestamp < validBefore. Signatures are checked with SignatureChecker, so
 *     smart-wallet (ERC-1271) authorizers work too.
 *   - `isAccountFrozen(address)`; transfers from or to a frozen account revert.
 *   - A token-wide pause: while paused, every balance change (transfer, ERC-3009 payment, mint) reverts
 *     with OpenZeppelin's `EnforcedPause()`, and `paused()` returns true. The real token's exact pause
 *     error names are not verified here; the backend's pre-flight therefore also reads `paused()`.
 *
 * Mock-only additions (not on the real token): open `mint`, `setAccountFrozen` / `freeze` / `unfreeze`,
 * `pause` / `unpause` (so the payment circuit breaker can be tested on anvil), and `requestFunds`, which
 * mints FAUCET_AMOUNT like the real faucet contract does — so the backend's faucet code path also works
 * locally by pointing the faucet address at this token.
 */
contract MockAUSD is ERC20, ERC20Permit, Pausable {
    // ─── constants ──────────────────────────────────────────────────────────────────────────

    string private constant TOKEN_NAME = "AUSD";
    string private constant TOKEN_SYMBOL = "AUSD";
    /// @dev The real token signs with "Agora Dollar", not with its ERC-20 name "AUSD".
    string private constant SIGNING_DOMAIN_NAME = "Agora Dollar";
    uint8 private constant AUSD_DECIMALS = 6;

    /// @notice What one `requestFunds` call mints: 10,000 AUSD in atomic units.
    uint256 public constant FAUCET_AMOUNT = 10_000 * 10 ** AUSD_DECIMALS;

    bytes32 public constant TRANSFER_WITH_AUTHORIZATION_TYPEHASH = keccak256(
        "TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );
    bytes32 public constant RECEIVE_WITH_AUTHORIZATION_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );
    bytes32 public constant CANCEL_AUTHORIZATION_TYPEHASH =
        keccak256("CancelAuthorization(address authorizer,bytes32 nonce)");

    // ─── storage ────────────────────────────────────────────────────────────────────────────

    /// @dev authorizer => nonce => used or canceled. ERC-3009 nonces are random, not sequential.
    mapping(address authorizer => mapping(bytes32 nonce => bool usedOrCanceled)) private _authorizationStates;

    mapping(address account => bool frozen) private _frozen;

    // ─── events ─────────────────────────────────────────────────────────────────────────────

    event AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce);
    event AuthorizationCanceled(address indexed authorizer, bytes32 indexed nonce);
    event AccountFrozenSet(address indexed account, bool frozen);

    // ─── errors ─────────────────────────────────────────────────────────────────────────────

    error AuthorizationNotYetValid(); // block.timestamp <= validAfter
    error AuthorizationExpired(); // block.timestamp >= validBefore
    error AuthorizationAlreadyUsed(); // nonce already used or canceled for this authorizer
    error InvalidSignature(); // signature is not the authorizer's over these exact fields
    error InvalidPayee(); // receiveWithAuthorization called by someone other than `to`
    error AccountFrozen(address account); // sender or recipient is frozen

    constructor() ERC20(TOKEN_NAME, TOKEN_SYMBOL) ERC20Permit(SIGNING_DOMAIN_NAME) {}

    // ─── ERC-20 metadata ────────────────────────────────────────────────────────────────────

    /// @notice AUSD uses 6 decimals: 1 AUSD == 1_000_000 atomic units.
    function decimals() public pure override returns (uint8) {
        return AUSD_DECIMALS;
    }

    // ─── mock-only minting ──────────────────────────────────────────────────────────────────

    /// @notice Open mint. TEST ONLY — this is what makes the mock unsafe on any public chain.
    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }

    /// @notice Same signature as the real AUSD faucet: mints FAUCET_AMOUNT to `recipient`.
    function requestFunds(address recipient) external {
        _mint(recipient, FAUCET_AMOUNT);
    }

    // ─── freezing ───────────────────────────────────────────────────────────────────────────

    /// @notice True if `account` can neither send nor receive (mirrors the real token's view).
    function isAccountFrozen(address account) external view returns (bool) {
        return _frozen[account];
    }

    /// @notice Freeze or unfreeze an account. Open to anyone — TEST ONLY, used to test revert handling.
    function setAccountFrozen(address account, bool frozen) public {
        _frozen[account] = frozen;
        emit AccountFrozenSet(account, frozen);
    }

    /// @notice Shorthand for setAccountFrozen(account, true). TEST ONLY.
    function freeze(address account) external {
        setAccountFrozen(account, true);
    }

    /// @notice Shorthand for setAccountFrozen(account, false). TEST ONLY.
    function unfreeze(address account) external {
        setAccountFrozen(account, false);
    }

    // ─── token-wide pause ───────────────────────────────────────────────────────────────────

    /// @notice Halt every balance change. Open to anyone — TEST ONLY, used to test the circuit breaker.
    function pause() external {
        _pause();
    }

    /// @notice Resume balance changes. Open to anyone — TEST ONLY.
    function unpause() external {
        _unpause();
    }

    /**
     * @dev Every balance change (transfer, ERC-3009 payment, mint, burn) passes here.
     *      Rules, in order: the token is not paused (EnforcedPause); the sender is not frozen;
     *      the recipient is not frozen (AccountFrozen).
     */
    function _update(address from, address to, uint256 value) internal override {
        _requireNotPaused();
        if (_frozen[from]) revert AccountFrozen(from);
        if (_frozen[to]) revert AccountFrozen(to);
        super._update(from, to, value);
    }

    // ─── ERC-3009: transferWithAuthorization (anyone may submit) ─────────────────────────────

    /// @notice Execute a transfer signed by `from`, submitted by anyone (relayer / seller). (v, r, s) form.
    function transferWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external {
        bytes memory signature = abi.encodePacked(r, s, v);
        _executeAuthorization(
            TRANSFER_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce, signature
        );
    }

    /// @notice Same as above with a packed 65-byte signature (or an ERC-1271 signature for smart wallets).
    function transferWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        bytes calldata signature
    ) external {
        _executeAuthorization(
            TRANSFER_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce, signature
        );
    }

    // ─── ERC-3009: receiveWithAuthorization (only the payee may submit) ──────────────────────

    /// @notice Like transferWithAuthorization, but only `to` may submit it. (v, r, s) form.
    /// @dev Prevents a front-runner from executing a payment meant to be pulled by a contract.
    function receiveWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external {
        if (msg.sender != to) revert InvalidPayee();
        bytes memory signature = abi.encodePacked(r, s, v);
        _executeAuthorization(
            RECEIVE_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce, signature
        );
    }

    /// @notice Same as above with a packed signature.
    function receiveWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        bytes calldata signature
    ) external {
        if (msg.sender != to) revert InvalidPayee();
        _executeAuthorization(
            RECEIVE_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce, signature
        );
    }

    // ─── ERC-3009: cancelAuthorization ──────────────────────────────────────────────────────

    /// @notice Burn an unused nonce so the matching authorization can never execute. (v, r, s) form.
    function cancelAuthorization(address authorizer, bytes32 nonce, uint8 v, bytes32 r, bytes32 s) external {
        _cancelAuthorization(authorizer, nonce, abi.encodePacked(r, s, v));
    }

    /// @notice Same as above with a packed signature.
    function cancelAuthorization(address authorizer, bytes32 nonce, bytes calldata signature) external {
        _cancelAuthorization(authorizer, nonce, signature);
    }

    /// @notice True if `nonce` was already used or canceled by `authorizer`.
    function authorizationState(address authorizer, bytes32 nonce) external view returns (bool) {
        return _authorizationStates[authorizer][nonce];
    }

    // ─── internal ───────────────────────────────────────────────────────────────────────────

    /**
     * @dev Shared body of transfer- and receiveWithAuthorization. Rules, in order:
     *      1. validAfter < block.timestamp < validBefore (strict on both ends).
     *      2. (from, nonce) not used or canceled before.
     *      3. `signature` is `from`'s over keccak256(abi.encode(typehash, from, to, value,
     *         validAfter, validBefore, nonce)) in the "Agora Dollar" / "1" domain.
     *      Then the nonce is marked used, AuthorizationUsed is emitted, and `value` moves from → to.
     */
    function _executeAuthorization(
        bytes32 typehash,
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        bytes memory signature
    ) internal {
        _requireWithinValidityWindow(validAfter, validBefore);
        _requireUnusedAuthorization(from, nonce);

        bytes32 structHash = keccak256(abi.encode(typehash, from, to, value, validAfter, validBefore, nonce));
        _requireValidSignature(from, structHash, signature);

        _authorizationStates[from][nonce] = true;
        emit AuthorizationUsed(from, nonce);
        _transfer(from, to, value);
    }

    /// @dev Rule: nonce unused, and `signature` is the authorizer's over CancelAuthorization(authorizer, nonce).
    function _cancelAuthorization(address authorizer, bytes32 nonce, bytes memory signature) internal {
        _requireUnusedAuthorization(authorizer, nonce);

        bytes32 structHash = keccak256(abi.encode(CANCEL_AUTHORIZATION_TYPEHASH, authorizer, nonce));
        _requireValidSignature(authorizer, structHash, signature);

        _authorizationStates[authorizer][nonce] = true;
        emit AuthorizationCanceled(authorizer, nonce);
    }

    /// @dev Strict window: valid iff validAfter < block.timestamp < validBefore.
    function _requireWithinValidityWindow(uint256 validAfter, uint256 validBefore) internal view {
        if (block.timestamp <= validAfter) revert AuthorizationNotYetValid();
        if (block.timestamp >= validBefore) revert AuthorizationExpired();
    }

    function _requireUnusedAuthorization(address authorizer, bytes32 nonce) internal view {
        if (_authorizationStates[authorizer][nonce]) revert AuthorizationAlreadyUsed();
    }

    /// @dev Signer check on the full EIP-712 digest; EOAs via ECDSA, contracts via ERC-1271.
    function _requireValidSignature(address signer, bytes32 structHash, bytes memory signature) internal view {
        bytes32 digest = _hashTypedDataV4(structHash);
        if (!SignatureChecker.isValidSignatureNow(signer, digest, signature)) revert InvalidSignature();
    }
}
