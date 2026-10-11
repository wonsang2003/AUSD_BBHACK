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

Pause detection has two layers, because web3 only gives us the raw 4-byte selector of a
custom error (our token ABI has no error definitions) and the real AUSD's pause error
names are not verified:
    1. the revert text / selector matches a known pause error, or
    2. after ANY failed pre-flight, the token's own pause getters (`paused()`,
       `isTransferPaused()`, ...) are read directly; if one returns true, it is a pause.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from web3 import Web3
from web3.exceptions import BadFunctionCallOutput, ContractLogicError

from app.chain.ausd import AusdClient
from app.chain.eip712 import Authorization
from app.chain.web3client import redact_urls

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

# Pause errors: OpenZeppelin's EnforcedPause() (used by MockAUSD) plus likely names on stablecoins with
# per-feature pauses. The extra names are UNVERIFIED guesses; the pause-getter probe below is the real safety net.
PAUSE_ERROR_SIGNATURES = (
    "EnforcedPause()",
    "TokenPaused()",
    "Paused()",
    "IsPaused()",
    "TransferPaused()",
    "SignatureVerificationPaused()",
    "TransferWithAuthorizationPaused()",
    "ReceiveWithAuthorizationPaused()",
)

# View functions that report a token-wide (or payment-relevant) pause. Missing ones are skipped.
PAUSE_GETTERS = ("paused", "isPaused", "isTransferPaused", "isSignatureVerificationPaused")
_PAUSE_GETTER_ABI = [
    {"type": "function", "name": name, "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "bool"}]}
    for name in PAUSE_GETTERS
]

# keccak256("Error(...)")[:4] of the custom errors we know (MockAUSD + OpenZeppelin v5).
KNOWN_ERROR_SELECTORS = {
    **{Web3.keccak(text=signature)[:4].hex(): PAUSED for signature in PAUSE_ERROR_SIGNATURES},
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


def token_reports_paused(token: AusdClient) -> bool:
    """True if any pause getter the token has (paused(), isTransferPaused(), ...) returns true.

    Getters the token does not have revert or return nothing and are skipped. An RPC failure
    also counts as "not reported paused": the failed pre-flight still backs off the agent.
    """
    contract = token.w3.eth.contract(address=token.address, abi=_PAUSE_GETTER_ABI)
    for name in PAUSE_GETTERS:
        try:
            if contract.functions[name]().call():
                return True
        except (ContractLogicError, BadFunctionCallOutput):
            continue
        except Exception:  # noqa: BLE001 — transport error: cannot tell, keep the per-agent backoff
            return False
    return False


def make_pause_probe(token: AusdClient) -> Callable[[], bool]:
    """Probe for `CircuitBreaker.reprobe` (run every 60 s): True (= healthy) when no pause getter reports a pause."""
    return lambda: not token_reports_paused(token)


def simulate_transfer_with_authorization(token: AusdClient, auth: Authorization, signature: str) -> PreflightResult:
    """eth_call the settlement transfer with the agent's signature; nothing is sent.

    Rule: kind = classify_revert(message); if that is not already PAUSED but the token's own
    pause getter says it is paused, the kind is PAUSED (so the global breaker trips).
    """
    try:
        token.transfer_with_authorization_fn(auth, signature).call({"from": auth.to})
    except Exception as exc:  # noqa: BLE001 — any revert or decode error is a failed pre-flight
        message = redact_urls(f"{type(exc).__name__}: {exc}")
        data = getattr(exc, "data", None)
        if isinstance(data, str):
            message += f" data={data}"
        kind = classify_revert(message)
        if kind != PAUSED and token_reports_paused(token):
            kind = PAUSED
        return PreflightResult(ok=False, kind=kind, message=message)
    return PreflightResult(ok=True, kind=OK, message="simulation succeeded")
