"""
Pre-flight simulation — the payment circuit breaker's sensor.

After an agent signs an ERC-3009 authorization and BEFORE it is sent to a seller, we
`eth_call` `transferWithAuthorization(...)` with that exact signature against the
token. An eth_call executes the transfer on a copy of the chain state and throws the
result away, so nothing moves — but if the real transfer would revert (token paused,
signature verification paused, frozen account, insufficient balance, expired window,
used nonce) we learn it without spending anything and without handing a doomed
signature to a third party.

The revert is classified so the caller can react:
    paused  → trip the GLOBAL breaker (all agents halt until the token is unpaused)
    other   → back off only this agent
"""

from __future__ import annotations

from dataclasses import dataclass

from web3 import Web3

from app.chain.ausd import AusdClient
from app.chain.eip712 import Authorization

# Revert kinds
OK = "ok"
PAUSED = "paused"
FROZEN = "frozen"
INSUFFICIENT_BALANCE = "insufficient_balance"
EXPIRED = "expired"
NOT_YET_VALID = "not_yet_valid"
BAD_SIGNATURE = "bad_signature"
USED_NONCE = "used_nonce"
OTHER = "other"

# Substrings (lower-cased) that identify each kind in a revert message or decoded error name.
# Order matters: the first match wins. Selectors of MockAUSD/OZ custom errors are matched by name
# after web3 decodes them, and by the raw 4-byte selector below when it cannot.
_KIND_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    (PAUSED, ("pause",)),                                   # "Pausable: paused", EnforcedPause, TokenPaused, ...
    (FROZEN, ("frozen", "freeze", "blacklist", "blocked")),
    (INSUFFICIENT_BALANCE, ("insufficientbalance", "insufficient balance", "exceeds balance")),
    (EXPIRED, ("expired",)),
    (NOT_YET_VALID, ("notyetvalid", "not yet valid")),
    (USED_NONCE, ("alreadyused", "already used", "used or canceled", "authorization is used")),
    (BAD_SIGNATURE, ("invalidsignature", "invalid signature", "ecdsa", "signature")),
]

# keccak256("Error(...)")[:4] of the custom errors we know (MockAUSD + OpenZeppelin v5).
KNOWN_ERROR_SELECTORS = {
    Web3.keccak(text="EnforcedPause()")[:4].hex(): PAUSED,
    Web3.keccak(text="TokenPaused()")[:4].hex(): PAUSED,
    Web3.keccak(text="AccountFrozen(address)")[:4].hex(): FROZEN,
    Web3.keccak(text="ERC20InsufficientBalance(address,uint256,uint256)")[:4].hex(): INSUFFICIENT_BALANCE,
    Web3.keccak(text="AuthorizationExpired()")[:4].hex(): EXPIRED,
    Web3.keccak(text="AuthorizationNotYetValid()")[:4].hex(): NOT_YET_VALID,
    Web3.keccak(text="AuthorizationAlreadyUsed()")[:4].hex(): USED_NONCE,
    Web3.keccak(text="InvalidSignature()")[:4].hex(): BAD_SIGNATURE,
}


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    kind: str        # one of the kinds above
    message: str

    @property
    def is_pause(self) -> bool:
        return self.kind == PAUSED


def classify_revert(message: str) -> str:
    """Map a revert message / custom-error description to one of the kinds above."""
    text = message.lower()
    for selector, kind in KNOWN_ERROR_SELECTORS.items():
        if selector in text:
            return kind
    for kind, markers in _KIND_MARKERS:
        if any(marker in text for marker in markers):
            return kind
    return OTHER


def simulate_transfer_with_authorization(token: AusdClient, auth: Authorization, signature: str) -> PreflightResult:
    """eth_call the settlement transfer with the agent's signature; nothing is sent."""
    try:
        token.transfer_with_authorization_fn(auth, signature).call({"from": auth.to})
    except Exception as exc:  # noqa: BLE001 — any revert or decode error is a failed pre-flight
        message = f"{type(exc).__name__}: {exc}"
        data = getattr(exc, "data", None)
        if isinstance(data, str):
            message += f" data={data}"
        return PreflightResult(ok=False, kind=classify_revert(message), message=message)
    return PreflightResult(ok=True, kind=OK, message="simulation succeeded")
