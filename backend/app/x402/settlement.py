"""
x402 settlement: actually moving the AUSD after verification.

`SelfSettler` (default): the seller's settlement wallet submits the agent-signed
`transferWithAuthorization` itself and waits for the receipt. Success is reported
ONLY when the receipt has status 1 — a payment is never final without a receipt.

`FacilitatorSettler`: forwards to a public x402 facilitator (`/settle`). Whether
public facilitators accept AUSD is unverified, so it is opt-in via X402_SETTLEMENT.

Three outcomes, kept strictly apart:
    success           → SettlementResponse(success=True, transaction)       paid
    definite failure  → SettlementResponse(success=False, errorReason)      NOT paid (reverted / refused)
    unknown           → raises SettlementUnknown(reason, tx_hash)           may still be paid
"Unknown" covers a missing receipt, an RPC error after broadcasting, or a facilitator
that did not answer. The server reports it as 504 and the buyer resolves it on-chain.
"""

from __future__ import annotations

import httpx

from app.chain.ausd import AusdClient
from app.chain.eip712 import Authorization
from app.chain.web3client import TxPendingError, TxRevertedError, TxSender, redact_urls
from app.x402.models import PaymentPayload, PaymentRequirements, SettlementResponse


class SettlementUnknown(RuntimeError):
    """The settlement may or may not execute on-chain (no receipt yet / no answer). Never report it as failed."""

    def __init__(self, reason: str, tx_hash: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.tx_hash = tx_hash


class SelfSettler:
    def __init__(self, token: AusdClient, settler: TxSender, network: str):
        self.token = token
        self.settler = settler
        self.network = network

    def settle(self, payment: PaymentPayload, required: PaymentRequirements) -> SettlementResponse:
        """Submit transferWithAuthorization. Reverted → definite failure; no receipt / RPC error → SettlementUnknown."""
        auth = Authorization.from_x402_json(payment.payload.authorization)
        payer = auth.from_address
        try:
            result = self.settler.send(self.token.transfer_with_authorization_fn(auth, payment.payload.signature))
        except TxRevertedError as exc:
            return SettlementResponse(success=False, transaction=exc.tx_hash, network=self.network, payer=payer,
                                      errorReason=f"settlement failed: {exc.reason}")
        except TxPendingError as exc:
            raise SettlementUnknown(f"settlement pending: {exc.reason}", exc.tx_hash) from exc
        except Exception as exc:  # RPC/transport error: the tx may have reached the node
            raise SettlementUnknown(f"settlement outcome unknown: {type(exc).__name__}: {redact_urls(str(exc))}") from exc
        # compliance_checked: the server only settles after PaymentVerifier read the freeze list successfully.
        return SettlementResponse(success=True, transaction=result.tx_hash, network=self.network, payer=payer,
                                  compliance_checked=True)


class FacilitatorSettler:
    def __init__(self, base_url: str, network: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.network = network
        self.timeout = timeout

    def settle(self, payment: PaymentPayload, required: PaymentRequirements) -> SettlementResponse:
        """POST /settle. No (parsable) answer → SettlementUnknown: the facilitator may have settled anyway."""
        body = {
            "x402Version": payment.x402Version,
            "paymentPayload": payment.model_dump(exclude_none=True),
            "paymentRequirements": required.model_dump(exclude_none=True),
        }
        try:
            response = httpx.post(f"{self.base_url}/settle", json=body, timeout=self.timeout)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SettlementUnknown(f"facilitator did not answer: {type(exc).__name__}") from exc
        return SettlementResponse(
            success=bool(data.get("success")),
            transaction=data.get("transaction"),
            network=data.get("network", self.network),
            payer=data.get("payer"),
            errorReason=data.get("errorReason"),
        )
