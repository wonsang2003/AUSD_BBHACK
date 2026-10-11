"""
Tests for app/x402/verify.py — seller-side verification rules that must answer 402 with a
reason (never crash into a 500 the buyer would read as "ambiguous").

Uses MockAUSD on a real local anvil chain for the token address; no chain call is needed
before the rules under test fire.
"""

from __future__ import annotations

import pytest

from app.chain.eip712 import new_authorization
from app.constants import BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_CHAIN_ID
from app.x402.models import ExactEvmPayload, PaymentPayload, PaymentRequirements
from app.x402.verify import (
    MIN_SECONDS_BEFORE_EXPIRY,
    PaymentVerifier,
    has_settlement_headroom,
)

PRICE = 50_000
NOW = 1_800_000_000
PAYER = "0x00000000000000000000000000000000000000A1"
SELLER = "0x00000000000000000000000000000000000000B2"


def requirement_for(token_address: str) -> PaymentRequirements:
    return PaymentRequirements(network=BASE_SEPOLIA_CAIP2, amount=str(PRICE), asset=token_address, payTo=SELLER,
                               maxTimeoutSeconds=120)


def payment_with(authorization: dict, required: PaymentRequirements) -> PaymentPayload:
    return PaymentPayload(accepted=required, payload=ExactEvmPayload(signature="0x" + "00" * 65, authorization=authorization))


@pytest.mark.chain
def test_authorization_with_null_value_is_rejected_as_malformed(mock_ausd):
    # Arrange
    required = requirement_for(mock_ausd.address)
    authorization = new_authorization(from_address=PAYER, to=SELLER, value=PRICE, timeout_seconds=120).to_x402_json()
    authorization["value"] = None
    verifier = PaymentVerifier(mock_ausd, BASE_SEPOLIA_CHAIN_ID, BASE_SEPOLIA_CAIP2)

    # Act
    verdict = verifier.verify(payment_with(authorization, required), required)

    # Assert
    assert verdict.is_valid is False
    assert verdict.reason.startswith("malformed authorization")


def test_settlement_headroom_requires_more_than_the_minimum_seconds_left():
    # Arrange
    required = requirement_for(SELLER)
    timeout = MIN_SECONDS_BEFORE_EXPIRY + 1
    authorization = new_authorization(from_address=PAYER, to=SELLER, value=PRICE, timeout_seconds=timeout, now=NOW)
    payment = payment_with(authorization.to_x402_json(), required)

    # Act
    enough_now = has_settlement_headroom(payment, now=NOW)
    enough_one_second_later = has_settlement_headroom(payment, now=NOW + 1)

    # Assert
    assert enough_now is True
    assert enough_one_second_later is False
