"""
AUSD token client: reads (balances, nonces, authorization state, freeze list) and the
transaction builders PocketDesk needs (ERC-3009 settlement, ERC-2612 permit, faucet).

Also holds the startup safety check `assert_ausd_matches_spec`: before anything
signs, the backend reads `decimals()` and `eip712Domain()` (ERC-5267) from the token
and refuses to start if they differ from what our signers assume. A wrong EIP-712
name ("AUSD" vs "Agora Dollar") would otherwise make every signature fail silently.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_utils import to_checksum_address
from web3 import Web3

from app.chain.eip712 import Authorization
from app.constants import AUSD_DECIMALS, AUSD_EIP712_NAME, AUSD_EIP712_VERSION


def _fn(name: str, inputs: list[tuple[str, str]], outputs: list[str] | None = None, mutability: str = "nonpayable") -> dict:
    """Small helper to write the ABI below readably: _fn("balanceOf", [("account", "address")], ["uint256"], "view")."""
    return {
        "type": "function",
        "name": name,
        "stateMutability": mutability,
        "inputs": [{"name": n, "type": t} for n, t in inputs],
        "outputs": [{"name": "", "type": t} for t in (outputs or [])],
    }


_AUTH_FIELDS = [
    ("from", "address"), ("to", "address"), ("value", "uint256"),
    ("validAfter", "uint256"), ("validBefore", "uint256"), ("nonce", "bytes32"),
]

AUSD_ABI: list[dict] = [
    # ERC-20
    _fn("decimals", [], ["uint8"], "view"),
    _fn("name", [], ["string"], "view"),
    _fn("symbol", [], ["string"], "view"),
    _fn("balanceOf", [("account", "address")], ["uint256"], "view"),
    _fn("allowance", [("owner", "address"), ("spender", "address")], ["uint256"], "view"),
    _fn("transfer", [("to", "address"), ("value", "uint256")], ["bool"]),
    # ERC-2612
    _fn("nonces", [("owner", "address")], ["uint256"], "view"),
    _fn("permit", [("owner", "address"), ("spender", "address"), ("value", "uint256"), ("deadline", "uint256"),
                   ("v", "uint8"), ("r", "bytes32"), ("s", "bytes32")]),
    # ERC-3009 (bytes-signature overloads; works for EOAs and ERC-1271 wallets)
    _fn("transferWithAuthorization", _AUTH_FIELDS + [("signature", "bytes")]),
    _fn("receiveWithAuthorization", _AUTH_FIELDS + [("signature", "bytes")]),
    _fn("cancelAuthorization", [("authorizer", "address"), ("nonce", "bytes32"), ("signature", "bytes")]),
    _fn("authorizationState", [("authorizer", "address"), ("nonce", "bytes32")], ["bool"], "view"),
    # compliance
    _fn("isAccountFrozen", [("account", "address")], ["bool"], "view"),
    # ERC-5267
    {
        "type": "function", "name": "eip712Domain", "stateMutability": "view", "inputs": [],
        "outputs": [
            {"name": "fields", "type": "bytes1"}, {"name": "name", "type": "string"},
            {"name": "version", "type": "string"}, {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"}, {"name": "salt", "type": "bytes32"},
            {"name": "extensions", "type": "uint256[]"},
        ],
    },
]

FAUCET_ABI: list[dict] = [_fn("requestFunds", [("recipient", "address")])]


class AusdSpecMismatch(RuntimeError):
    """The on-chain token does not match the decimals / EIP-712 domain our signers assume."""


@dataclass(frozen=True)
class Eip712Domain:
    name: str
    version: str
    chain_id: int
    verifying_contract: str


def _hex32(value: str | bytes) -> bytes:
    return bytes.fromhex(value[2:]) if isinstance(value, str) else value


def _sig_bytes(signature: str | bytes) -> bytes:
    return bytes.fromhex(signature[2:]) if isinstance(signature, str) else signature


class AusdClient:
    """Read helpers + transaction builders for the AUSD token (or MockAUSD on a local chain)."""

    def __init__(self, w3: Web3, address: str):
        self.w3 = w3
        self.address = to_checksum_address(address)
        self.contract = w3.eth.contract(address=self.address, abi=AUSD_ABI)

    # ─── reads ───────────────────────────────────────────────────────────
    def decimals(self) -> int:
        return self.contract.functions.decimals().call()

    def eip712_domain(self) -> Eip712Domain:
        _, name, version, chain_id, verifying_contract, _, _ = self.contract.functions.eip712Domain().call()
        return Eip712Domain(name, version, chain_id, to_checksum_address(verifying_contract))

    def balance_of(self, account: str) -> int:
        return self.contract.functions.balanceOf(to_checksum_address(account)).call()

    def allowance(self, owner: str, spender: str) -> int:
        return self.contract.functions.allowance(to_checksum_address(owner), to_checksum_address(spender)).call()

    def nonces(self, owner: str) -> int:
        return self.contract.functions.nonces(to_checksum_address(owner)).call()

    def authorization_state(self, authorizer: str, nonce: str | bytes) -> bool:
        """True if this ERC-3009 nonce was already used (or cancelled) by `authorizer`."""
        return self.contract.functions.authorizationState(to_checksum_address(authorizer), _hex32(nonce)).call()

    def is_account_frozen(self, account: str) -> bool:
        """Freeze-list check. If the token has no such method, treat the account as not frozen."""
        try:
            return self.contract.functions.isAccountFrozen(to_checksum_address(account)).call()
        except Exception:  # noqa: BLE001 — method absent on non-AUSD tokens
            return False

    def eth_balance(self, account: str) -> int:
        return self.w3.eth.get_balance(to_checksum_address(account))

    # ─── transaction builders (send them with TxSender.send) ─────────────
    def transfer_with_authorization_fn(self, auth: Authorization, signature: str | bytes):
        m = auth.to_message()
        return self.contract.get_function_by_signature(
            "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,bytes)"
        )(m["from"], m["to"], m["value"], m["validAfter"], m["validBefore"], _hex32(m["nonce"]), _sig_bytes(signature))

    def cancel_authorization_fn(self, authorizer: str, nonce: str, signature: str | bytes):
        return self.contract.get_function_by_signature("cancelAuthorization(address,bytes32,bytes)")(
            to_checksum_address(authorizer), _hex32(nonce), _sig_bytes(signature)
        )

    def permit_fn(self, owner: str, spender: str, value: int, deadline: int, v: int, r: bytes, s: bytes):
        return self.contract.functions.permit(
            to_checksum_address(owner), to_checksum_address(spender), int(value), int(deadline), v, r, s
        )


def assert_ausd_matches_spec(
    w3: Web3,
    address: str,
    chain_id: int,
    expected_name: str = AUSD_EIP712_NAME,
    expected_version: str = AUSD_EIP712_VERSION,
    expected_decimals: int = AUSD_DECIMALS,
) -> Eip712Domain:
    """Startup check: refuse to run if the token's decimals or EIP-712 domain differ from our assumptions."""
    token = AusdClient(w3, address)
    decimals = token.decimals()
    domain = token.eip712_domain()
    problems = []
    if decimals != expected_decimals:
        problems.append(f"decimals() = {decimals}, expected {expected_decimals}")
    if domain.name != expected_name:
        problems.append(f"eip712Domain().name = {domain.name!r}, expected {expected_name!r} (fix AUSD_EIP712_NAME in app/constants.py)")
    if domain.version != expected_version:
        problems.append(f"eip712Domain().version = {domain.version!r}, expected {expected_version!r}")
    if domain.chain_id != chain_id:
        problems.append(f"eip712Domain().chainId = {domain.chain_id}, expected {chain_id}")
    if domain.verifying_contract != token.address:
        problems.append("eip712Domain().verifyingContract is not the token address")
    if problems:
        raise AusdSpecMismatch("AUSD token does not match PocketDesk's signing assumptions: " + "; ".join(problems))
    return domain
