"""
Tests for app/x402/preflight.py — classifying a failed pre-flight simulation, and the pause
path that trips the GLOBAL circuit breaker.

The classification tests use the exact message format web3 produces for a custom error
(only the 4-byte selector is available). The pause tests run on a real local anvil chain
with MockAUSD's test-only pause()/unpause().
"""

from __future__ import annotations

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI
from web3 import Web3

from app.agents.breaker import CircuitBreaker
from app.chain.eip712 import (
    build_transfer_with_authorization,
    new_authorization,
    sign_typed_data,
)
from app.chain.tokens import TokenInfo, TokenRegistry
from app.constants import BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_CHAIN_ID, ONE_AUSD
from app.x402 import preflight
from app.x402.client import PaymentError, X402Client
from app.x402.offer import SellerOffer
from app.x402.server import make_paid_router
from app.x402.settlement import SelfSettler
from app.x402.verify import PaymentVerifier

PRICE = 50_000
SELLER_URL = "http://demo-vendor.test/sell/demo"


def custom_error_message(signature: str) -> str:
    """What simulate_transfer_with_authorization sees for a custom error: the selector only, no name."""
    selector = "0x" + Web3.keccak(text=signature)[:4].hex().removeprefix("0x")
    return f"ContractCustomError: ('{selector}', '{selector}') data={selector}"


# ─── classification (no chain) ───────────────────────────────────────────────
@pytest.mark.parametrize("signature", ["EnforcedPause()", "TransferPaused()", "SignatureVerificationPaused()"])
def test_pause_custom_errors_are_classified_paused_from_their_selector(signature):
    # Act
    kind = preflight.classify_revert(custom_error_message(signature))

    # Assert
    assert kind == preflight.PAUSED


@pytest.mark.parametrize(
    ("signature", "expected"),
    [
        ("AccountFrozen(address)", preflight.FROZEN),
        ("AuthorizationExpired()", preflight.EXPIRED),
        ("AuthorizationAlreadyUsed()", preflight.USED_NONCE),
        ("InvalidSignature()", preflight.BAD_SIGNATURE),
    ],
)
def test_other_known_custom_errors_are_classified_for_agent_backoff(signature, expected):
    # Act
    kind = preflight.classify_revert(custom_error_message(signature))

    # Assert
    assert kind == expected


def test_require_string_mentioning_pause_is_classified_paused():
    # Act
    kind = preflight.classify_revert("ContractLogicError: execution reverted: Pausable: paused")

    # Assert
    assert kind == preflight.PAUSED


def test_unknown_custom_error_is_classified_other():
    # Act
    kind = preflight.classify_revert(custom_error_message("SomethingElse()"))

    # Assert
    assert kind == preflight.OTHER


# ─── pause on a real chain ───────────────────────────────────────────────────
def _signed_authorization(mock_ausd, payer_key: str, to: str):
    payer = Account.from_key(payer_key).address
    auth = new_authorization(from_address=payer, to=to, value=PRICE, timeout_seconds=120)
    typed = build_transfer_with_authorization(chain_id=BASE_SEPOLIA_CHAIN_ID, ausd_address=mock_ausd.address, auth=auth)
    return auth, sign_typed_data(payer_key, typed).hex


@pytest.mark.chain
def test_simulation_on_a_paused_token_is_classified_paused(mock_ausd, mock_ausd_admin, deployer, fund_ausd, wallets, chain_snapshot):
    # Arrange
    fund_ausd(Account.from_key(wallets.agent).address, ONE_AUSD)
    auth, signature = _signed_authorization(mock_ausd, wallets.agent, wallets.address("seller"))
    deployer.send(mock_ausd_admin.functions.pause())

    # Act
    result = preflight.simulate_transfer_with_authorization(mock_ausd, auth, signature)

    # Assert
    assert result.ok is False
    assert result.is_pause


@pytest.mark.chain
def test_token_reports_paused_reads_the_pause_getter(mock_ausd, mock_ausd_admin, deployer, chain_snapshot):
    # Arrange
    deployer.send(mock_ausd_admin.functions.pause())

    # Act
    while_paused = preflight.token_reports_paused(mock_ausd)
    deployer.send(mock_ausd_admin.functions.unpause())
    after_unpause = preflight.token_reports_paused(mock_ausd)

    # Assert
    assert while_paused is True
    assert after_unpause is False


@pytest.mark.chain
async def test_paused_token_trips_the_global_breaker_and_reprobe_resumes_after_unpause(
    mock_ausd, mock_ausd_admin, deployer, settler, fund_ausd, wallets, chain_snapshot
):
    # Arrange: a demo vendor, a funded agent, and a paused token.
    seller = wallets.address("seller")
    fund_ausd(Account.from_key(wallets.agent).address, ONE_AUSD)
    offer = SellerOffer(seller_id="demo", path="/sell/demo", description="Demo item", price_atomic=PRICE, pay_to=seller)
    app = FastAPI()
    app.include_router(make_paid_router(
        offer, lambda params: _answer(),
        verifier=PaymentVerifier(mock_ausd, BASE_SEPOLIA_CHAIN_ID, BASE_SEPOLIA_CAIP2),
        settler=SelfSettler(mock_ausd, settler, BASE_SEPOLIA_CAIP2),
        asset=mock_ausd.address,
    ))
    breaker = CircuitBreaker()
    client = X402Client(
        private_key=wallets.agent,
        registry=TokenRegistry(tokens=[TokenInfo(
            symbol="AUSD", chain_id=BASE_SEPOLIA_CHAIN_ID, caip2=BASE_SEPOLIA_CAIP2, address=mock_ausd.address,
            eip712_name="Agora Dollar", eip712_version="1", decimals=6, is_budget_token=True,
        )]),
        token_clients={mock_ausd.address: mock_ausd},
        max_amount_atomic=ONE_AUSD,
        agent_name="OptionsEvent",
        breaker=breaker,
        http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo-vendor.test"),
    )
    deployer.send(mock_ausd_admin.functions.pause())

    # Act
    with pytest.raises(PaymentError) as error:
        await client.get(SELLER_URL)
    every_agent_halted = not breaker.allows("NewsSentiment")[0]
    still_open_while_paused = breaker.reprobe(preflight.make_pause_probe(mock_ausd))
    deployer.send(mock_ausd_admin.functions.unpause())
    open_after_unpause = breaker.reprobe(preflight.make_pause_probe(mock_ausd))

    # Assert
    assert error.value.kind == preflight.PAUSED
    assert every_agent_halted, "a token pause halts ALL agents, not just the one that paid"
    assert still_open_while_paused is True
    assert open_after_unpause is False
    assert mock_ausd.balance_of(seller) == 0


async def _answer() -> dict:
    return {"answer": 42}
