"""
x402 v2 wire format: the JSON objects carried (base64-encoded) in the three HTTP headers.

    server → client   PAYMENT-REQUIRED   (with HTTP 402)  → `PaymentRequired`
    client → server   PAYMENT-SIGNATURE                    → `PaymentPayload`
    server → client   PAYMENT-RESPONSE                     → `SettlementResponse`

`parse_payment_required()` is the single parser used by both our agent client and the
live probe of real vendors: it reads the v2 header and falls back to the v1 JSON body
(`accepts[].maxAmountRequired`, network names like "base").
"""

from __future__ import annotations

import base64
import json

from pydantic import BaseModel, Field

from app.constants import HEADER_PAYMENT_REQUIRED, X402_VERSION

# x402 v1 used plain network names; v2 uses CAIP-2 ids.
V1_NETWORK_TO_CAIP2 = {
    "base": "eip155:8453",
    "base-sepolia": "eip155:84532",
    "arbitrum": "eip155:42161",
    "arbitrum-sepolia": "eip155:421614",
    "ethereum": "eip155:1",
    "sepolia": "eip155:11155111",
    "polygon": "eip155:137",
    "avalanche": "eip155:43114",
}


class ResourceInfo(BaseModel):
    url: str
    description: str = ""
    mimeType: str = "application/json"


class PaymentRequirements(BaseModel):
    scheme: str = "exact"
    network: str                 # CAIP-2, e.g. "eip155:84532"
    amount: str                  # atomic units, decimal string
    asset: str                   # token contract address
    payTo: str
    maxTimeoutSeconds: int = 120
    extra: dict = Field(default_factory=dict)  # {name, version, assetTransferMethod}


class PaymentRequired(BaseModel):
    x402Version: int = X402_VERSION
    error: str | None = None
    resource: ResourceInfo
    accepts: list[PaymentRequirements]


class ExactEvmPayload(BaseModel):
    signature: str
    authorization: dict          # {from, to, value, validAfter, validBefore, nonce} — integers as strings


class PaymentPayload(BaseModel):
    x402Version: int = X402_VERSION
    resource: ResourceInfo | None = None
    accepted: PaymentRequirements
    payload: ExactEvmPayload


class SettlementResponse(BaseModel):
    success: bool
    transaction: str | None = None
    network: str
    payer: str | None = None
    errorReason: str | None = None
    compliance_checked: bool = False   # PocketDesk extension: freeze list checked before settling


def encode_header(model: BaseModel) -> str:
    """base64(JSON) as required for x402 headers."""
    return base64.b64encode(model.model_dump_json(exclude_none=True).encode()).decode()


def decode_header(value: str, model_cls: type[BaseModel]) -> BaseModel:
    return model_cls.model_validate(json.loads(base64.b64decode(value)))


def _requirement_from_v1(raw: dict) -> PaymentRequirements:
    network = raw.get("network", "")
    return PaymentRequirements(
        scheme=raw.get("scheme", "exact"),
        network=V1_NETWORK_TO_CAIP2.get(network, network),
        amount=str(raw.get("maxAmountRequired") or raw.get("amount") or "0"),
        asset=raw.get("asset", ""),
        payTo=raw.get("payTo", ""),
        maxTimeoutSeconds=int(raw.get("maxTimeoutSeconds") or 60),
        extra=raw.get("extra") or {},
    )


def parse_payment_required(headers: dict, body: bytes | str | None, url: str = "") -> PaymentRequired:
    """Parse a 402 response: prefer the v2 PAYMENT-REQUIRED header, fall back to a v1 JSON body."""
    header_value = headers.get(HEADER_PAYMENT_REQUIRED) or headers.get(HEADER_PAYMENT_REQUIRED.lower())
    if header_value:
        return decode_header(header_value, PaymentRequired)

    data = json.loads(body) if body else {}
    if data.get("x402Version", 1) >= 2 and "resource" in data:
        return PaymentRequired.model_validate(data)
    accepts = [_requirement_from_v1(item) for item in data.get("accepts", [])]
    first = (data.get("accepts") or [{}])[0]
    resource = ResourceInfo(url=first.get("resource", url), description=first.get("description", ""),
                            mimeType=first.get("mimeType", "application/json"))
    return PaymentRequired(x402Version=data.get("x402Version", 1), error=data.get("error"), resource=resource, accepts=accepts)
