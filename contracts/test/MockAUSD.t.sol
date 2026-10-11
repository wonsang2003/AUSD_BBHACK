// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";

import {MockAUSD} from "../src/mocks/MockAUSD.sol";

/**
 * @title  MockAUSD tests
 * @notice Proves the local-test token behaves like the real AUSD where PocketDesk depends on it:
 *         ERC-20 metadata (name "AUSD", 6 decimals), the "Agora Dollar" / "1" EIP-712 domain, ERC-2612
 *         permit, the faucet, freezing, and a faithful ERC-3009 (both signature overloads, replay,
 *         strict validity window, wrong signer, receive-only-by-payee, cancel). One test replays a
 *         signature produced by backend/app/chain/eip712.py, so the Python signer and the token agree.
 *
 * Each test follows Arrange / Act / Assert. Digests are built here from the ERC-3009 spec.
 */
contract MockAUSDTest is Test {
    // ─── test constants ─────────────────────────────────────────────────────────────────────

    uint256 private constant BASE_SEPOLIA_CHAIN_ID = 84532;
    uint256 private constant ONE_AUSD = 1_000_000; // 6 decimals
    uint256 private constant PAYER_STARTING_BALANCE = 100 * ONE_AUSD;
    uint256 private constant PAYMENT = 50_000; // 0.05 AUSD, a typical x402 research price

    uint256 private constant START_TIME = 1_790_000_000;
    uint256 private constant VALID_AFTER_SKEW = 30; // validAfter = now - 30 s (as in the backend)
    uint256 private constant PAYMENT_TIMEOUT = 120; // validBefore = now + 120 s

    // Test-only private keys (public Foundry fixtures, never used on a real chain).
    uint256 private constant PAYER_PRIVATE_KEY = 0xA11CE;
    uint256 private constant STRANGER_PRIVATE_KEY = 0xB0B;

    bytes32 private constant EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant TRANSFER_TYPEHASH = keccak256(
        "TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );
    bytes32 private constant RECEIVE_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );
    bytes32 private constant CANCEL_TYPEHASH = keccak256("CancelAuthorization(address authorizer,bytes32 nonce)");
    bytes32 private constant PERMIT_TYPEHASH =
        keccak256("Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)");

    // Pinned from backend/app/chain/eip712.py: build_transfer_with_authorization + sign_typed_data with
    // key 0xA11CE, chain 84532, token at PINNED_TOKEN_ADDRESS.
    address private constant PINNED_TOKEN_ADDRESS = address(0xA05D);
    address private constant PINNED_PAYER = 0xe05fcC23807536bEe418f142D19fa0d21BB0cfF7; // vm.addr(0xA11CE)
    address private constant PINNED_SELLER = 0x3333333333333333333333333333333333333333;
    uint256 private constant PINNED_VALID_AFTER = 1_799_999_970;
    uint256 private constant PINNED_VALID_BEFORE = 1_800_000_120;
    bytes32 private constant PINNED_NONCE = 0xabababababababababababababababababababababababababababababababab;
    bytes private constant PYTHON_TRANSFER_SIGNATURE =
        hex"b09f9a88f2966a63f54b5bf767535550bd5bb751eb47614a6ad29756275b51b26c4d59f34db99282b6ff2a8c0b3f763503e3c6b34060c909f6b2ada933c58ba31c";

    /// @dev The six signed fields of an ERC-3009 authorization.
    struct Authorization {
        address from;
        address to;
        uint256 value;
        uint256 validAfter;
        uint256 validBefore;
        bytes32 nonce;
    }

    // ─── fixtures ───────────────────────────────────────────────────────────────────────────

    MockAUSD private token;
    address private payer; // an agent wallet paying for research
    address private seller; // the x402 seller's payout address
    address private relayer; // whoever submits the signed authorization (pays gas)

    function setUp() public {
        vm.chainId(BASE_SEPOLIA_CHAIN_ID);
        vm.warp(START_TIME);

        payer = vm.addr(PAYER_PRIVATE_KEY);
        seller = makeAddr("seller");
        relayer = makeAddr("relayer");

        token = new MockAUSD();
        token.mint(payer, PAYER_STARTING_BALANCE);
    }

    // ═══ metadata, domain, faucet, permit ═══════════════════════════════════════════════════

    function test_metadata_matches_real_ausd_token() public view {
        // Assert
        assertEq(token.name(), "AUSD");
        assertEq(token.symbol(), "AUSD");
        assertEq(token.decimals(), 6);
    }

    function test_eip712_domain_is_agora_dollar_version_1() public view {
        // Act
        (, string memory name, string memory version, uint256 chainId, address verifyingContract,,) =
            token.eip712Domain();

        // Assert: signing name differs from name() on purpose, like the real token.
        assertEq(name, "Agora Dollar");
        assertEq(version, "1");
        assertEq(chainId, BASE_SEPOLIA_CHAIN_ID);
        assertEq(verifyingContract, address(token));
        assertEq(token.DOMAIN_SEPARATOR(), _specDomainSeparator(address(token)));
    }

    function test_request_funds_mints_ten_thousand_ausd() public {
        // Arrange
        address recipient = makeAddr("newUser");

        // Act
        vm.prank(relayer);
        token.requestFunds(recipient);

        // Assert
        assertEq(token.balanceOf(recipient), 10_000 * ONE_AUSD);
        assertEq(token.FAUCET_AMOUNT(), 10_000_000_000);
    }

    function test_mint_is_open_to_anyone() public {
        // Arrange
        address anyone = makeAddr("anyone");

        // Act
        vm.prank(anyone);
        token.mint(anyone, ONE_AUSD);

        // Assert
        assertEq(token.balanceOf(anyone), ONE_AUSD);
    }

    function test_permit_sets_allowance_with_agora_dollar_domain() public {
        // Arrange
        address spender = makeAddr("budgetController");
        uint256 deadline = block.timestamp + PAYMENT_TIMEOUT;
        bytes32 structHash = keccak256(abi.encode(PERMIT_TYPEHASH, payer, spender, PAYMENT, 0, deadline));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PAYER_PRIVATE_KEY, _specDigest(structHash));

        // Act
        vm.prank(relayer);
        token.permit(payer, spender, PAYMENT, deadline, v, r, s);

        // Assert
        assertEq(token.allowance(payer, spender), PAYMENT);
        assertEq(token.nonces(payer), 1);
    }

    // ═══ transferWithAuthorization ══════════════════════════════════════════════════════════

    function test_transfer_with_authorization_moves_funds_and_marks_nonce_used() public {
        // Arrange
        Authorization memory auth = _freshAuthorization(keccak256("nonce-1"));
        (uint8 v, bytes32 r, bytes32 s) = _signAuthorization(PAYER_PRIVATE_KEY, TRANSFER_TYPEHASH, auth);

        // Assert (event declared before the call)
        vm.expectEmit(address(token));
        emit MockAUSD.AuthorizationUsed(payer, auth.nonce);

        // Act: a relayer (not the payer) submits it — anyone may.
        vm.prank(relayer);
        token.transferWithAuthorization(
            auth.from, auth.to, auth.value, auth.validAfter, auth.validBefore, auth.nonce, v, r, s
        );

        // Assert
        assertEq(token.balanceOf(seller), PAYMENT);
        assertEq(token.balanceOf(payer), PAYER_STARTING_BALANCE - PAYMENT);
        assertTrue(token.authorizationState(payer, auth.nonce));
    }

    function test_transfer_with_authorization_accepts_bytes_signature_overload() public {
        // Arrange
        Authorization memory auth = _freshAuthorization(keccak256("nonce-bytes"));
        bytes memory signature = _packedSignature(PAYER_PRIVATE_KEY, TRANSFER_TYPEHASH, auth);

        // Act
        vm.prank(relayer);
        token.transferWithAuthorization(
            auth.from, auth.to, auth.value, auth.validAfter, auth.validBefore, auth.nonce, signature
        );

        // Assert
        assertEq(token.balanceOf(seller), PAYMENT);
        assertTrue(token.authorizationState(payer, auth.nonce));
    }

    function test_transfer_with_authorization_reverts_on_replay() public {
        // Arrange: the authorization executes once.
        Authorization memory auth = _freshAuthorization(keccak256("nonce-replay"));
        bytes memory signature = _packedSignature(PAYER_PRIVATE_KEY, TRANSFER_TYPEHASH, auth);
        _submitTransfer(auth, signature);

        // Act + Assert
        vm.expectRevert(MockAUSD.AuthorizationAlreadyUsed.selector);
        _submitTransfer(auth, signature);
        assertEq(token.balanceOf(seller), PAYMENT); // paid exactly once
    }

    function test_transfer_with_authorization_reverts_when_expired() public {
        // Arrange: block.timestamp == validBefore is already too late (strict bound).
        Authorization memory auth = _freshAuthorization(keccak256("nonce-expired"));
        bytes memory signature = _packedSignature(PAYER_PRIVATE_KEY, TRANSFER_TYPEHASH, auth);
        vm.warp(auth.validBefore);

        // Act + Assert
        vm.expectRevert(MockAUSD.AuthorizationExpired.selector);
        _submitTransfer(auth, signature);
    }

    function test_transfer_with_authorization_reverts_when_not_yet_valid() public {
        // Arrange: block.timestamp == validAfter is still too early (strict bound).
        Authorization memory auth = _freshAuthorization(keccak256("nonce-early"));
        auth.validAfter = block.timestamp;
        bytes memory signature = _packedSignature(PAYER_PRIVATE_KEY, TRANSFER_TYPEHASH, auth);

        // Act + Assert
        vm.expectRevert(MockAUSD.AuthorizationNotYetValid.selector);
        _submitTransfer(auth, signature);
    }

    function test_transfer_with_authorization_reverts_with_wrong_signer() public {
        // Arrange: a stranger signs an authorization that moves the payer's funds.
        Authorization memory auth = _freshAuthorization(keccak256("nonce-wrong-signer"));
        bytes memory strangerSignature = _packedSignature(STRANGER_PRIVATE_KEY, TRANSFER_TYPEHASH, auth);

        // Act + Assert
        vm.expectRevert(MockAUSD.InvalidSignature.selector);
        _submitTransfer(auth, strangerSignature);
    }

    function test_transfer_with_authorization_reverts_when_value_is_altered() public {
        // Arrange: the payer signed 0.05 AUSD; the submitter claims 1 AUSD.
        Authorization memory auth = _freshAuthorization(keccak256("nonce-altered"));
        bytes memory signature = _packedSignature(PAYER_PRIVATE_KEY, TRANSFER_TYPEHASH, auth);
        auth.value = ONE_AUSD;

        // Act + Assert
        vm.expectRevert(MockAUSD.InvalidSignature.selector);
        _submitTransfer(auth, signature);
    }

    function test_transfer_with_authorization_rejects_receive_signature() public {
        // Arrange: a ReceiveWithAuthorization signature must not work as a TransferWithAuthorization.
        Authorization memory auth = _freshAuthorization(keccak256("nonce-typehash"));
        bytes memory receiveSignature = _packedSignature(PAYER_PRIVATE_KEY, RECEIVE_TYPEHASH, auth);

        // Act + Assert
        vm.expectRevert(MockAUSD.InvalidSignature.selector);
        _submitTransfer(auth, receiveSignature);
    }

    function test_transfer_with_authorization_accepts_signature_from_python_builder() public {
        // Arrange: token at the pinned address on chain 84532, inside the signed validity window.
        deployCodeTo("MockAUSD.sol:MockAUSD", PINNED_TOKEN_ADDRESS);
        MockAUSD pinnedToken = MockAUSD(PINNED_TOKEN_ADDRESS);
        pinnedToken.mint(PINNED_PAYER, ONE_AUSD);
        vm.warp(PINNED_VALID_AFTER + VALID_AFTER_SKEW);

        // Act
        vm.prank(relayer);
        pinnedToken.transferWithAuthorization(
            PINNED_PAYER,
            PINNED_SELLER,
            PAYMENT,
            PINNED_VALID_AFTER,
            PINNED_VALID_BEFORE,
            PINNED_NONCE,
            PYTHON_TRANSFER_SIGNATURE
        );

        // Assert
        assertEq(vm.addr(PAYER_PRIVATE_KEY), PINNED_PAYER);
        assertEq(pinnedToken.balanceOf(PINNED_SELLER), PAYMENT);
        assertTrue(pinnedToken.authorizationState(PINNED_PAYER, PINNED_NONCE));
    }

    function testFuzz_authorization_is_valid_only_strictly_inside_window(uint256 submitTime) public {
        // Arrange
        Authorization memory auth = _freshAuthorization(keccak256("nonce-fuzz"));
        bytes memory signature = _packedSignature(PAYER_PRIVATE_KEY, TRANSFER_TYPEHASH, auth);
        submitTime = bound(submitTime, auth.validAfter - 10, auth.validBefore + 10);
        vm.warp(submitTime);

        // Act + Assert
        if (submitTime <= auth.validAfter) {
            vm.expectRevert(MockAUSD.AuthorizationNotYetValid.selector);
        } else if (submitTime >= auth.validBefore) {
            vm.expectRevert(MockAUSD.AuthorizationExpired.selector);
        }
        _submitTransfer(auth, signature);
    }

    // ═══ receiveWithAuthorization ═══════════════════════════════════════════════════════════

    function test_receive_with_authorization_reverts_with_invalid_payee() public {
        // Arrange: a relayer (not the payee) tries to submit a receive-authorization.
        Authorization memory auth = _freshAuthorization(keccak256("nonce-receive-payee"));
        (uint8 v, bytes32 r, bytes32 s) = _signAuthorization(PAYER_PRIVATE_KEY, RECEIVE_TYPEHASH, auth);
        bytes memory signature = abi.encodePacked(r, s, v);

        // Act + Assert: both overloads enforce msg.sender == to.
        vm.expectRevert(MockAUSD.InvalidPayee.selector);
        vm.prank(relayer);
        token.receiveWithAuthorization(
            auth.from, auth.to, auth.value, auth.validAfter, auth.validBefore, auth.nonce, v, r, s
        );

        vm.expectRevert(MockAUSD.InvalidPayee.selector);
        vm.prank(relayer);
        token.receiveWithAuthorization(
            auth.from, auth.to, auth.value, auth.validAfter, auth.validBefore, auth.nonce, signature
        );
    }

    function test_receive_with_authorization_succeeds_when_called_by_payee() public {
        // Arrange
        Authorization memory auth = _freshAuthorization(keccak256("nonce-receive-vrs"));
        (uint8 v, bytes32 r, bytes32 s) = _signAuthorization(PAYER_PRIVATE_KEY, RECEIVE_TYPEHASH, auth);

        // Act
        vm.prank(seller);
        token.receiveWithAuthorization(
            auth.from, auth.to, auth.value, auth.validAfter, auth.validBefore, auth.nonce, v, r, s
        );

        // Assert
        assertEq(token.balanceOf(seller), PAYMENT);
        assertTrue(token.authorizationState(payer, auth.nonce));
    }

    function test_receive_with_authorization_bytes_overload_succeeds_when_called_by_payee() public {
        // Arrange
        Authorization memory auth = _freshAuthorization(keccak256("nonce-receive-bytes"));
        bytes memory signature = _packedSignature(PAYER_PRIVATE_KEY, RECEIVE_TYPEHASH, auth);

        // Act
        vm.prank(seller);
        token.receiveWithAuthorization(
            auth.from, auth.to, auth.value, auth.validAfter, auth.validBefore, auth.nonce, signature
        );

        // Assert
        assertEq(token.balanceOf(seller), PAYMENT);
    }

    // ═══ cancelAuthorization ════════════════════════════════════════════════════════════════

    function test_cancel_authorization_blocks_later_use() public {
        // Arrange: the payer signs a payment, then cancels its nonce before anyone submits it.
        Authorization memory auth = _freshAuthorization(keccak256("nonce-cancel"));
        bytes memory paymentSignature = _packedSignature(PAYER_PRIVATE_KEY, TRANSFER_TYPEHASH, auth);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PAYER_PRIVATE_KEY, _cancelDigest(payer, auth.nonce));

        // Act
        vm.expectEmit(address(token));
        emit MockAUSD.AuthorizationCanceled(payer, auth.nonce);
        vm.prank(relayer);
        token.cancelAuthorization(payer, auth.nonce, v, r, s);

        // Assert
        assertTrue(token.authorizationState(payer, auth.nonce));
        vm.expectRevert(MockAUSD.AuthorizationAlreadyUsed.selector);
        _submitTransfer(auth, paymentSignature);
    }

    function test_cancel_authorization_bytes_overload_works() public {
        // Arrange
        bytes32 nonce = keccak256("nonce-cancel-bytes");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PAYER_PRIVATE_KEY, _cancelDigest(payer, nonce));

        // Act
        vm.prank(relayer);
        token.cancelAuthorization(payer, nonce, abi.encodePacked(r, s, v));

        // Assert
        assertTrue(token.authorizationState(payer, nonce));
    }

    function test_cancel_authorization_reverts_when_nonce_already_used() public {
        // Arrange: the payment already executed.
        Authorization memory auth = _freshAuthorization(keccak256("nonce-cancel-late"));
        _submitTransfer(auth, _packedSignature(PAYER_PRIVATE_KEY, TRANSFER_TYPEHASH, auth));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PAYER_PRIVATE_KEY, _cancelDigest(payer, auth.nonce));

        // Act + Assert
        vm.expectRevert(MockAUSD.AuthorizationAlreadyUsed.selector);
        token.cancelAuthorization(payer, auth.nonce, v, r, s);
    }

    function test_cancel_authorization_reverts_with_wrong_signer() public {
        // Arrange: a stranger tries to cancel the payer's nonce.
        bytes32 nonce = keccak256("nonce-cancel-stranger");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(STRANGER_PRIVATE_KEY, _cancelDigest(payer, nonce));

        // Act + Assert
        vm.expectRevert(MockAUSD.InvalidSignature.selector);
        token.cancelAuthorization(payer, nonce, v, r, s);
        assertFalse(token.authorizationState(payer, nonce));
    }

    // ═══ freezing ═══════════════════════════════════════════════════════════════════════════

    function test_frozen_account_cannot_send_or_receive() public {
        // Arrange
        Authorization memory auth = _freshAuthorization(keccak256("nonce-frozen"));
        bytes memory signature = _packedSignature(PAYER_PRIVATE_KEY, TRANSFER_TYPEHASH, auth);

        // Act
        token.setAccountFrozen(payer, true);

        // Assert: plain transfers and ERC-3009 payments from a frozen account both revert.
        assertTrue(token.isAccountFrozen(payer));
        vm.expectRevert(abi.encodeWithSelector(MockAUSD.AccountFrozen.selector, payer));
        vm.prank(payer);
        // forge-lint: disable-next-line(erc20-unchecked-transfer) — the call reverts, nothing to check
        token.transfer(seller, PAYMENT);

        vm.expectRevert(abi.encodeWithSelector(MockAUSD.AccountFrozen.selector, payer));
        _submitTransfer(auth, signature);

        vm.expectRevert(abi.encodeWithSelector(MockAUSD.AccountFrozen.selector, payer));
        token.mint(payer, ONE_AUSD);
    }

    function test_unfrozen_account_can_transfer_again() public {
        // Arrange
        token.setAccountFrozen(payer, true);

        // Act
        token.setAccountFrozen(payer, false);
        vm.prank(payer);
        bool transferred = token.transfer(seller, PAYMENT);

        // Assert
        assertTrue(transferred);
        assertFalse(token.isAccountFrozen(payer));
        assertEq(token.balanceOf(seller), PAYMENT);
    }

    // ─── helpers ────────────────────────────────────────────────────────────────────────────

    /// @dev A PAYMENT from payer to seller, valid from (now - 30 s) to (now + 120 s), like the backend builds.
    function _freshAuthorization(bytes32 nonce) internal view returns (Authorization memory) {
        return Authorization({
            from: payer,
            to: seller,
            value: PAYMENT,
            validAfter: block.timestamp - VALID_AFTER_SKEW,
            validBefore: block.timestamp + PAYMENT_TIMEOUT,
            nonce: nonce
        });
    }

    /// @dev Relayer submits a transfer-authorization with a packed signature.
    function _submitTransfer(Authorization memory auth, bytes memory signature) internal {
        vm.prank(relayer);
        token.transferWithAuthorization(
            auth.from, auth.to, auth.value, auth.validAfter, auth.validBefore, auth.nonce, signature
        );
    }

    /// @dev Domain separator per EIP-712 for ("Agora Dollar", "1", chainid, verifyingContract).
    function _specDomainSeparator(address verifyingContract) internal view returns (bytes32) {
        return keccak256(
            abi.encode(
                EIP712_DOMAIN_TYPEHASH, keccak256("Agora Dollar"), keccak256("1"), block.chainid, verifyingContract
            )
        );
    }

    /// @dev Full EIP-712 digest: keccak256("\x19\x01" ‖ domainSeparator ‖ structHash).
    function _specDigest(bytes32 structHash) internal view returns (bytes32) {
        return keccak256(abi.encodePacked("\x19\x01", _specDomainSeparator(address(token)), structHash));
    }

    /// @dev ERC-3009 digest for TransferWithAuthorization or ReceiveWithAuthorization (chosen by `typehash`).
    function _authorizationDigest(bytes32 typehash, Authorization memory auth) internal view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(typehash, auth.from, auth.to, auth.value, auth.validAfter, auth.validBefore, auth.nonce)
        );
        return _specDigest(structHash);
    }

    function _cancelDigest(address authorizer, bytes32 nonce) internal view returns (bytes32) {
        return _specDigest(keccak256(abi.encode(CANCEL_TYPEHASH, authorizer, nonce)));
    }

    function _signAuthorization(uint256 privateKey, bytes32 typehash, Authorization memory auth)
        internal
        view
        returns (uint8 v, bytes32 r, bytes32 s)
    {
        return vm.sign(privateKey, _authorizationDigest(typehash, auth));
    }

    /// @dev 65-byte r ‖ s ‖ v signature, the format eth_account and wallets produce.
    function _packedSignature(uint256 privateKey, bytes32 typehash, Authorization memory auth)
        internal
        view
        returns (bytes memory)
    {
        (uint8 v, bytes32 r, bytes32 s) = _signAuthorization(privateKey, typehash, auth);
        return abi.encodePacked(r, s, v);
    }
}
