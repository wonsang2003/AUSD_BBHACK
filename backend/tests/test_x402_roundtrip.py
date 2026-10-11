"""
End-to-end x402 payment on a real local chain (anvil + MockAUSD):

    agent (X402Client) ──GET──▶ demo vendor (make_paid_router)  → 402
    agent signs ERC-3009, pre-flights it, retries with PAYMENT-SIGNATURE
    vendor verifies, settles transferWithAuthorization on-chain, returns data + PAYMENT-RESPONSE

Nothing in the payment path is mocked: signatures, verification and the settlement
transaction are real; only the chain is local.
"""

from __future__ import annotations

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI

from app.agents.breaker import CircuitBreaker
from app.chain.tokens import TokenInfo, TokenRegistry
from app.constants import BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_CHAIN_ID, HEADER_PAYMENT_REQUIRED, ONE_AUSD
from app.x402.client import PaymentError, X402Client
from app.x402.models import PaymentRequired, decode_header
from app.x402.offer import SellerOffer
from app.x402.server import make_paid_router
from app.x402.settlement import SelfSettler
from app.x402.verify import PaymentVerifier

pytestmark = pytest.mark.chain

PRICE = 50_000  # 0.05 AUSD
SELLER_URL = "http://demo-vendor.test/sell/demo"


async def demo_compute(params: dict) -> dict:
    return {"answer": 42, "asset": params.get("asset", "BTC")}


def registry_for(token_address: str) -> TokenRegistry:
    return TokenRegistry(tokens=[TokenInfo(
        symbol="AUSD", chain_id=BASE_SEPOLIA_CHAIN_ID, caip2=BASE_SEPOLIA_CAIP2, address=token_address,
        eip712_name="Agora Dollar", eip712_version="1", decimals=6, is_budget_token=True,
    )])


@pytest.fixture
def vendor(mock_ausd, settler, wallets):
    seller_address = wallets.address("seller")
    offer = SellerOffer(seller_id="demo", path="/sell/demo", description="Demo item", price_atomic=PRICE, pay_to=seller_address)
    app = FastAPI()
    app.include_router(make_paid_router(
        offer, demo_compute,
        verifier=PaymentVerifier(mock_ausd, BASE_SEPOLIA_CHAIN_ID, BASE_SEPOLIA_CAIP2),
        settler=SelfSettler(mock_ausd, settler, BASE_SEPOLIA_CAIP2),
        asset=mock_ausd.address,
    ))
    return app, seller_address


def make_client(app, mock_ausd, agent_key, breaker=None, cap=ONE_AUSD) -> X402Client:
    return X402Client(
        private_key=agent_key,
        registry=registry_for(mock_ausd.address),
        token_clients={mock_ausd.address: mock_ausd},
        max_amount_atomic=cap,
        agent_name="OptionsEvent",
        breaker=breaker,
        http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo-vendor.test"),
    )


async def test_unpaid_request_gets_402_with_payment_requirements(vendor, mock_ausd):
    app, seller = vendor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await http.get(SELLER_URL)

    assert response.status_code == 402
    offer = decode_header(response.headers[HEADER_PAYMENT_REQUIRED], PaymentRequired)
    assert offer.accepts[0].amount == str(PRICE)
    assert offer.accepts[0].payTo == seller
    assert offer.accepts[0].asset == mock_ausd.address
    assert offer.accepts[0].extra["name"] == "Agora Dollar"


async def test_agent_pays_and_receives_data_with_onchain_receipt(vendor, mock_ausd, fund_ausd, wallets, w3, chain_snapshot):
    app, seller = vendor
    agent = Account.from_key(wallets.agent).address
    fund_ausd(agent, ONE_AUSD)
    client = make_client(app, mock_ausd, wallets.agent)

    result = await client.get(SELLER_URL, params={"asset": "ETH"})

    assert result.data == {"answer": 42, "asset": "ETH"}
    assert result.tx_hash and w3.eth.get_transaction_receipt(result.tx_hash)["status"] == 1
    assert mock_ausd.balance_of(seller) == PRICE
    assert mock_ausd.balance_of(agent) == ONE_AUSD - PRICE
    assert mock_ausd.eth_balance(agent) == 0, "agents never hold ETH — the seller's settler paid the gas"
    assert result.compliance_checked and result.preflight == "ok"


async def test_price_above_cap_is_refused_before_signing(vendor, mock_ausd, wallets):
    app, _ = vendor
    client = make_client(app, mock_ausd, wallets.agent, cap=PRICE - 1)

    with pytest.raises(PaymentError) as error:
        await client.get(SELLER_URL)

    assert error.value.kind == "price_above_cap"


async def test_preflight_blocks_payment_when_agent_balance_is_too_low(vendor, mock_ausd, wallets, chain_snapshot):
    app, seller = vendor
    breaker = CircuitBreaker()
    client = make_client(app, mock_ausd, wallets.agent2, breaker=breaker)  # agent2 has no AUSD

    with pytest.raises(PaymentError) as error:
        await client.get(SELLER_URL)

    assert error.value.kind == "insufficient_balance"
    assert mock_ausd.balance_of(seller) == 0
    assert breaker.allows("OptionsEvent")[0] is False, "the agent is put into backoff"
    assert breaker.global_open is False, "a balance problem must not halt every agent"


async def test_frozen_seller_is_skipped_before_signing(vendor, mock_ausd, mock_ausd_admin, deployer, fund_ausd, wallets, chain_snapshot):
    app, seller = vendor
    fund_ausd(Account.from_key(wallets.agent).address, ONE_AUSD)
    deployer.send(mock_ausd_admin.functions.setAccountFrozen(seller, True))
    client = make_client(app, mock_ausd, wallets.agent)

    with pytest.raises(PaymentError) as error:
        await client.get(SELLER_URL)

    assert error.value.kind == "frozen"
    assert error.value.reason == "skipped: counterparty frozen"


async def test_replayed_payment_signature_is_rejected(vendor, mock_ausd, fund_ausd, wallets, chain_snapshot):
    app, seller = vendor
    fund_ausd(Account.from_key(wallets.agent).address, ONE_AUSD)
    client = make_client(app, mock_ausd, wallets.agent)
    captured = {}
    original_get = client.http.get

    async def capturing_get(url, params=None, headers=None):
        if headers:
            captured.update(headers)
        return await original_get(url, params=params, headers=headers)

    client.http.get = capturing_get
    await client.get(SELLER_URL)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        replay = await http.get(SELLER_URL, headers=captured)

    assert replay.status_code == 402
    assert mock_ausd.balance_of(seller) == PRICE, "paid exactly once"
