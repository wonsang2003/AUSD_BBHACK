"""
Tests for app/x402/client.py — the buyer's rules after an AMBIGUOUS paid request.

The danger being tested: an ERC-3009 authorization stays executable until its validBefore.
If the client signs a second authorization while the first can still be settled, one item
can be paid for twice. The client must instead resolve the first one on-chain.

Everything runs against a real local chain (anvil + MockAUSD) and the real demo-vendor app
(`make_paid_router` + `PaymentVerifier` + `SelfSettler`). Only the NETWORK between buyer and
vendor misbehaves, via `LossyNetwork` (a transport that loses / delays the first paid request).
While the client waits, the injected sleep mines a block so anvil's clock keeps moving.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI

from app.chain.tokens import TokenInfo, TokenRegistry
from app.constants import (
    BASE_SEPOLIA_CAIP2,
    BASE_SEPOLIA_CHAIN_ID,
    HEADER_PAYMENT_SIGNATURE,
    ONE_AUSD,
)
from app.x402.client import (
    MAX_AUTHORIZATION_SECONDS,
    MIN_AUTHORIZATION_SECONDS,
    PaymentError,
    X402Client,
    authorization_lifetime,
    json_object_or_empty,
)
from app.x402.models import PaymentPayload, PaymentRequirements, decode_header
from app.x402.offer import SellerOffer
from app.x402.server import make_paid_router
from app.x402.settlement import SelfSettler
from app.x402.verify import PaymentVerifier

PRICE = 50_000  # 0.05 AUSD
SELLER_URL = "http://demo-vendor.test/sell/demo"
SHORT_LIFETIME_SECONDS = MIN_AUTHORIZATION_SECONDS   # keeps the "wait until expired" test to ~10 s
REAL_SECONDS_PER_POLL = 1.0                         # one anvil block per second keeps chain time ≈ wall time


async def demo_compute(params: dict) -> dict:
    return {"answer": 42}


class LossyNetwork(httpx.AsyncBaseTransport):
    """The real vendor app behind a network that misbehaves on the FIRST paid request only.

    "slow"          the request reaches the vendor, which keeps working in the background;
                    the client sees a read timeout (like a gateway timing out).
    "lost_response" the vendor finishes (and settles), then the response is lost (read timeout).
    "dropped"       the request never reaches the vendor (connection error).
    "html_502"      the vendor finishes (and settles), but a proxy answers 502 with an HTML page.
    """

    def __init__(self, app: FastAPI, mode: str):
        self.vendor = httpx.ASGITransport(app=app)
        self.mode = mode
        self.paid_requests = 0
        self.first_payment: PaymentPayload | None = None
        self.background: asyncio.Task | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if HEADER_PAYMENT_SIGNATURE not in request.headers:
            return await self.vendor.handle_async_request(request)
        self.paid_requests += 1
        if self.paid_requests > 1:
            return await self.vendor.handle_async_request(request)
        self.first_payment = decode_header(request.headers[HEADER_PAYMENT_SIGNATURE], PaymentPayload)
        return await self._misbehave(request)

    async def _misbehave(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "slow":
            self.background = asyncio.create_task(self.vendor.handle_async_request(request))
            raise httpx.ReadTimeout("gateway timed out", request=request)
        if self.mode == "lost_response":
            await self.vendor.handle_async_request(request)
            raise httpx.ReadTimeout("response lost", request=request)
        if self.mode == "dropped":
            raise httpx.ConnectError("connection reset", request=request)
        if self.mode == "html_502":
            await self.vendor.handle_async_request(request)
            return httpx.Response(502, text="<html>Bad Gateway</html>", request=request)
        raise ValueError(f"unknown mode {self.mode}")


def make_vendor(mock_ausd, settler, seller: str, max_timeout_seconds: int = MAX_AUTHORIZATION_SECONDS) -> FastAPI:
    offer = SellerOffer(seller_id="demo", path="/sell/demo", description="Demo item", price_atomic=PRICE,
                        pay_to=seller, max_timeout_seconds=max_timeout_seconds)
    app = FastAPI()
    app.include_router(make_paid_router(
        offer, demo_compute,
        verifier=PaymentVerifier(mock_ausd, BASE_SEPOLIA_CHAIN_ID, BASE_SEPOLIA_CAIP2),
        settler=SelfSettler(mock_ausd, settler, BASE_SEPOLIA_CAIP2),
        asset=mock_ausd.address,
    ))
    return app


def make_client(network: LossyNetwork, mock_ausd, agent_key: str, w3) -> X402Client:
    async def sleep_and_mine(seconds: float) -> None:
        await asyncio.sleep(min(seconds, REAL_SECONDS_PER_POLL))
        w3.provider.make_request("evm_mine", [])

    registry = TokenRegistry(tokens=[TokenInfo(
        symbol="AUSD", chain_id=BASE_SEPOLIA_CHAIN_ID, caip2=BASE_SEPOLIA_CAIP2, address=mock_ausd.address,
        eip712_name="Agora Dollar", eip712_version="1", decimals=6, is_budget_token=True,
    )])
    return X402Client(
        private_key=agent_key,
        registry=registry,
        token_clients={mock_ausd.address: mock_ausd},
        max_amount_atomic=ONE_AUSD,
        agent_name="OptionsEvent",
        http=httpx.AsyncClient(transport=network, base_url="http://demo-vendor.test"),
        sleep=sleep_and_mine,
    )


# ─── pure rules ──────────────────────────────────────────────────────────────
def test_authorization_lifetime_is_capped_so_a_seller_cannot_request_a_long_lived_authorization():
    # Arrange
    requirement = PaymentRequirements(network=BASE_SEPOLIA_CAIP2, amount="1", asset="0x0", payTo="0x0",
                                      maxTimeoutSeconds=10**9)

    # Act
    lifetime = authorization_lifetime(requirement)

    # Assert
    assert lifetime == MAX_AUTHORIZATION_SECONDS


def test_json_object_or_empty_returns_empty_dict_for_an_html_error_page():
    # Arrange
    response = httpx.Response(502, text="<html>Bad Gateway</html>")

    # Act
    body = json_object_or_empty(response)

    # Assert
    assert body == {}


# ─── ambiguous results on a real chain ───────────────────────────────────────
@pytest.mark.chain
async def test_slow_vendor_after_client_timeout_charges_exactly_once(mock_ausd, settler, fund_ausd, wallets, w3, chain_snapshot):
    # Arrange: the vendor is still working when the client's request times out.
    seller = wallets.address("seller")
    agent = Account.from_key(wallets.agent).address
    fund_ausd(agent, ONE_AUSD)
    network = LossyNetwork(make_vendor(mock_ausd, settler, seller), mode="slow")
    client = make_client(network, mock_ausd, wallets.agent, w3)

    # Act
    with pytest.raises(PaymentError) as error:
        await client.get(SELLER_URL)

    # Assert: the client never signed a second authorization, and the item was paid once.
    assert error.value.kind == "ambiguous_paid"
    assert network.paid_requests == 1
    assert mock_ausd.balance_of(seller) == PRICE
    assert mock_ausd.balance_of(agent) == ONE_AUSD - PRICE
    assert w3.eth.get_transaction_receipt(error.value.tx_hash)["status"] == 1


@pytest.mark.chain
async def test_lost_response_after_settlement_reports_paid_with_tx_hash(mock_ausd, settler, fund_ausd, wallets, w3, chain_snapshot):
    # Arrange
    seller = wallets.address("seller")
    fund_ausd(Account.from_key(wallets.agent).address, ONE_AUSD)
    network = LossyNetwork(make_vendor(mock_ausd, settler, seller), mode="lost_response")
    client = make_client(network, mock_ausd, wallets.agent, w3)

    # Act
    with pytest.raises(PaymentError) as error:
        await client.get(SELLER_URL)

    # Assert: "paid, data not delivered", with the on-chain tx found from the AuthorizationUsed event.
    assert error.value.kind == "ambiguous_paid"
    assert error.value.tx_hash is not None
    assert w3.eth.get_transaction_receipt(error.value.tx_hash)["status"] == 1
    assert network.paid_requests == 1
    assert mock_ausd.balance_of(seller) == PRICE


@pytest.mark.chain
async def test_html_502_from_a_proxy_is_resolved_on_chain_instead_of_crashing(mock_ausd, settler, fund_ausd, wallets, w3, chain_snapshot):
    # Arrange: the vendor settled, but a proxy replaced its answer with an HTML 502 page.
    seller = wallets.address("seller")
    fund_ausd(Account.from_key(wallets.agent).address, ONE_AUSD)
    network = LossyNetwork(make_vendor(mock_ausd, settler, seller), mode="html_502")
    client = make_client(network, mock_ausd, wallets.agent, w3)

    # Act
    with pytest.raises(PaymentError) as error:
        await client.get(SELLER_URL)

    # Assert: no JSONDecodeError escapes; the payment is reported as made.
    assert error.value.kind == "ambiguous_paid"
    assert mock_ausd.balance_of(seller) == PRICE


@pytest.mark.chain
async def test_dropped_request_is_retried_only_after_the_first_authorization_expired(
    mock_ausd, settler, fund_ausd, wallets, w3, chain_snapshot
):
    # Arrange: the first paid request never reaches the vendor. Short lifetime keeps the wait ~10 s.
    seller = wallets.address("seller")
    agent = Account.from_key(wallets.agent).address
    fund_ausd(agent, ONE_AUSD)
    network = LossyNetwork(make_vendor(mock_ausd, settler, seller, SHORT_LIFETIME_SECONDS), mode="dropped")
    client = make_client(network, mock_ausd, wallets.agent, w3)

    # Act
    result = await client.get(SELLER_URL)

    # Assert: one retry with a NEW nonce, settled only after the chain had passed the first validBefore.
    first = network.first_payment.payload.authorization
    retry_block = w3.eth.get_block(w3.eth.get_transaction_receipt(result.tx_hash)["blockNumber"])
    assert result.data == {"answer": 42}
    assert network.paid_requests == 2
    assert retry_block["timestamp"] >= int(first["validBefore"])
    assert mock_ausd.authorization_state(agent, first["nonce"]) is False, "the first authorization never executed"
    assert mock_ausd.balance_of(seller) == PRICE, "paid exactly once"
    assert mock_ausd.balance_of(agent) == ONE_AUSD - PRICE


@pytest.mark.chain
async def test_compliance_check_that_cannot_be_read_blocks_the_payment(mock_ausd, settler, fund_ausd, wallets, w3, chain_snapshot):
    # Arrange: the agent's RPC for the freeze list is unreachable (a real dead endpoint, not a mock).
    from web3 import Web3

    from app.chain.ausd import AusdClient

    seller = wallets.address("seller")
    fund_ausd(Account.from_key(wallets.agent).address, ONE_AUSD)
    network = LossyNetwork(make_vendor(mock_ausd, settler, seller), mode="dropped")
    unreachable_token = AusdClient(Web3(Web3.HTTPProvider("http://127.0.0.1:1", request_kwargs={"timeout": 2})), mock_ausd.address)
    client = make_client(network, unreachable_token, wallets.agent, w3)

    # Act
    with pytest.raises(PaymentError) as error:
        await client.get(SELLER_URL)

    # Assert: fails closed — nothing signed, nothing sent, nothing paid.
    assert error.value.kind == "compliance_unavailable"
    assert network.paid_requests == 0
    assert mock_ausd.balance_of(seller) == 0
