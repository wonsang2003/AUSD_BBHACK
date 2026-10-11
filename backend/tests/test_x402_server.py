"""
Tests for app/x402/server.py (+ app/x402/settlement.py) — the demo vendor's paid endpoint.

Rules tested: a result that cannot be rendered as JSON is never charged; a settlement whose
receipt has not arrived is reported as UNKNOWN (504 + tx hash), never as a failure, and the
buyer then resolves it on-chain; only x402 v2 headers are accepted.

Runs on a real local anvil chain with MockAUSD; signatures and settlement txs are real.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI

from app.chain import web3client
from app.chain.tokens import TokenInfo, TokenRegistry
from app.constants import BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_CHAIN_ID, ONE_AUSD
from app.x402.client import PaymentError, X402Client
from app.x402.offer import SellerOffer
from app.x402.server import make_paid_router, render_json_body
from app.x402.settlement import SelfSettler
from app.x402.verify import PaymentVerifier

pytestmark = pytest.mark.chain

PRICE = 50_000
SELLER_URL = "http://demo-vendor.test/sell/demo"


def make_app(mock_ausd, settler, seller: str, compute) -> FastAPI:
    offer = SellerOffer(seller_id="demo", path="/sell/demo", description="Demo item", price_atomic=PRICE, pay_to=seller)
    app = FastAPI()
    app.include_router(make_paid_router(
        offer, compute,
        verifier=PaymentVerifier(mock_ausd, BASE_SEPOLIA_CHAIN_ID, BASE_SEPOLIA_CAIP2),
        settler=SelfSettler(mock_ausd, settler, BASE_SEPOLIA_CAIP2),
        asset=mock_ausd.address,
    ))
    return app


def make_client(app: FastAPI, mock_ausd, agent_key: str, w3) -> X402Client:
    async def sleep_and_mine(seconds: float) -> None:
        await asyncio.sleep(0.05)
        w3.provider.make_request("evm_mine", [])

    return X402Client(
        private_key=agent_key,
        registry=TokenRegistry(tokens=[TokenInfo(
            symbol="AUSD", chain_id=BASE_SEPOLIA_CHAIN_ID, caip2=BASE_SEPOLIA_CAIP2, address=mock_ausd.address,
            eip712_name="Agora Dollar", eip712_version="1", decimals=6, is_budget_token=True,
        )]),
        token_clients={mock_ausd.address: mock_ausd},
        max_amount_atomic=ONE_AUSD,
        agent_name="OptionsEvent",
        http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo-vendor.test"),
        sleep=sleep_and_mine,
    )


def test_render_json_body_refuses_nan():
    # Act
    body = render_json_body({"expected_move": float("nan")})

    # Assert
    assert body is None


async def test_result_that_is_not_valid_json_is_not_charged(mock_ausd, settler, fund_ausd, wallets, w3, chain_snapshot):
    # Arrange: the seller's computation yields NaN, which JSON cannot carry.
    async def nan_compute(params: dict) -> dict:
        return {"expected_move": float("nan")}

    seller = wallets.address("seller")
    agent = Account.from_key(wallets.agent).address
    fund_ausd(agent, ONE_AUSD)
    client = make_client(make_app(mock_ausd, settler, seller, nan_compute), mock_ausd, wallets.agent, w3)

    # Act
    with pytest.raises(PaymentError) as error:
        await client.get(SELLER_URL)

    # Assert: 502 charged:false → nothing settled, nothing paid.
    assert error.value.kind == "seller_error"
    assert mock_ausd.balance_of(seller) == 0
    assert mock_ausd.balance_of(agent) == ONE_AUSD


async def test_settlement_without_receipt_is_reported_unknown_and_resolved_on_chain(
    mock_ausd, settler, fund_ausd, wallets, w3, chain_snapshot, monkeypatch
):
    # Arrange: blocks are only mined when the client's wait loop mines one, so the settler's
    # receipt wait (shortened to 1 s) runs out while the settlement tx sits in the mempool.
    async def answer(params: dict) -> dict:
        return {"answer": 42}

    monkeypatch.setattr(web3client, "RECEIPT_TIMEOUT_SECONDS", 1)
    seller = wallets.address("seller")
    fund_ausd(Account.from_key(wallets.agent).address, ONE_AUSD)
    client = make_client(make_app(mock_ausd, settler, seller, answer), mock_ausd, wallets.agent, w3)
    w3.provider.make_request("evm_setAutomine", [False])
    try:
        # Act
        with pytest.raises(PaymentError) as error:
            await client.get(SELLER_URL)
    finally:
        w3.provider.make_request("evm_setAutomine", [True])

    # Assert: the pending tx was mined while the client waited; it is reported as paid, not as failed.
    assert error.value.kind == "ambiguous_paid"
    assert w3.eth.get_transaction_receipt(error.value.tx_hash)["status"] == 1
    assert mock_ausd.balance_of(seller) == PRICE


async def test_legacy_v1_payment_header_is_not_accepted(mock_ausd, settler, wallets):
    # Arrange
    async def answer(params: dict) -> dict:
        return {"answer": 42}

    app = make_app(mock_ausd, settler, wallets.address("seller"), answer)

    # Act
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        response = await http.get(SELLER_URL, headers={"X-PAYMENT": "eyJ4NDAyVmVyc2lvbiI6MX0="})

    # Assert: answered as an unpaid request (v2 PAYMENT-SIGNATURE only).
    assert response.status_code == 402
    assert response.json()["error"] == "payment required"
