"""
x402 settlement: actually moving the AUSD after verification.

`SelfSettler` (default): the seller's settlement wallet submits the agent-signed
`transferWithAuthorization` itself and waits for the receipt. Success is reported
ONLY when the receipt has status 1 — a payment is never final without a receipt.

`FacilitatorSettler`: forwards to a public x402 facilitator (`/settle`). Whether
public facilitators accept AUSD is unverified, so it is opt-in via X402_SETTLEMENT.
"""

from __future__ import annotations

import httpx

from app.chain.ausd import AusdClient
from app.chain.eip712 import Authorization
from app.chain.web3client import TxRevertedError, TxSender
from app.x402.models import PaymentPayload, PaymentRequirements, SettlementResponse


class SelfSettler:
    def __init__(self, token: AusdClient, settler: TxSender, network: str):
        self.token = token
        self.settler = settler
        self.network = network

    def settle(self, payment: PaymentPayload, required: PaymentRequirements) -> SettlementResponse:
        auth = Authorization.from_x402_json(payment.payload.authorization)
        payer = auth.from_address
        try:
            result = self.settler.send(self.token.transfer_with_authorization_fn(auth, payment.payload.signature))
        except TxRevertedError as exc:
            return SettlementResponse(success=False, transaction=exc.tx_hash, network=self.network, payer=payer,
                                      errorReason=f"settlement failed: {exc.reason}")
        return SettlementResponse(success=True, transaction=result.tx_hash, network=self.network, payer=payer,
                                  compliance_checked=True)


class FacilitatorSettler:
    def __init__(self, base_url: str, network: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.network = network
        self.timeout = timeout

    def settle(self, payment: PaymentPayload, required: PaymentRequirements) -> SettlementResponse:
        body = {
            "x402Version": payment.x402Version,
            "paymentPayload": payment.model_dump(exclude_none=True),
            "paymentRequirements": required.model_dump(exclude_none=True),
        }
        try:
            response = httpx.post(f"{self.base_url}/settle", json=body, timeout=self.timeout)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return SettlementResponse(success=False, network=self.network, errorReason=f"facilitator error: {exc}")
        return SettlementResponse(
            success=bool(data.get("success")),
            transaction=data.get("transaction"),
            network=data.get("network", self.network),
            payer=data.get("payer"),
            errorReason=data.get("errorReason"),
        )
