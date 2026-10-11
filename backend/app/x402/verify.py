"""
Seller-side verification of an x402 payment (SPEC §3), done BEFORE serving any data.

Every rule has its own reason string so failures are visible in the purchase feed:

    scheme/network/asset mismatch → "unsupported scheme" / "wrong network" / "wrong asset"
    signature does not recover `from` → "invalid signature"
    authorization.to != payTo          → "wrong payTo"
    value < required amount            → "amount too low"
    window not open / about to close   → "authorization not yet valid" / "authorization expired"
    nonce already used                 → "nonce already used"
    balance < value                    → "insufficient balance"
    payer or payTo on the freeze list  → "counterparty frozen"
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from eth_utils import to_checksum_address

from app.chain.ausd import AusdClient
from app.chain.eip712 import Authorization, build_transfer_with_authorization, recover_typed_data_signer
from app.x402.models import PaymentPayload, PaymentRequirements

# Refuse authorizations that expire within this many seconds: we still need time to settle.
MIN_SECONDS_BEFORE_EXPIRY = 6


@dataclass(frozen=True)
class VerificationResult:
    is_valid: bool
    reason: str = ""
    payer: str | None = None


class PaymentVerifier:
    def __init__(self, token: AusdClient, chain_id: int, network: str):
        self.token = token
        self.chain_id = chain_id
        self.network = network

    def verify(self, payment: PaymentPayload, required: PaymentRequirements, now: int | None = None) -> VerificationResult:
        now = int(time.time()) if now is None else now
        accepted = payment.accepted
        if accepted.scheme != "exact" or required.scheme != "exact":
            return VerificationResult(False, "unsupported scheme")
        if accepted.network != required.network or required.network != self.network:
            return VerificationResult(False, "wrong network")
        if accepted.asset.lower() != required.asset.lower() or required.asset.lower() != self.token.address.lower():
            return VerificationResult(False, "wrong asset")

        try:
            auth = Authorization.from_x402_json(payment.payload.authorization)
        except (KeyError, ValueError) as exc:
            return VerificationResult(False, f"malformed authorization: {exc}")
        payer = auth.from_address

        typed = build_transfer_with_authorization(chain_id=self.chain_id, ausd_address=self.token.address, auth=auth)
        try:
            signer = recover_typed_data_signer(typed, payment.payload.signature)
        except Exception:  # noqa: BLE001
            return VerificationResult(False, "invalid signature", payer)
        if signer != payer:
            return VerificationResult(False, "invalid signature", payer)

        if auth.to != to_checksum_address(required.payTo):
            return VerificationResult(False, "wrong payTo", payer)
        if auth.value < int(required.amount):
            return VerificationResult(False, "amount too low", payer)
        if not auth.valid_after < now:
            return VerificationResult(False, "authorization not yet valid", payer)
        if not now + MIN_SECONDS_BEFORE_EXPIRY < auth.valid_before:
            return VerificationResult(False, "authorization expired", payer)
        if self.token.authorization_state(payer, auth.nonce):
            return VerificationResult(False, "nonce already used", payer)
        if self.token.balance_of(payer) < auth.value:
            return VerificationResult(False, "insufficient balance", payer)
        if self.token.is_account_frozen(payer) or self.token.is_account_frozen(auth.to):
            return VerificationResult(False, "counterparty frozen", payer)
        return VerificationResult(True, "", payer)
