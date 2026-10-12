"""
Tests for `app/x402/probe.py` — unpaid x402 price probes of real mainnet vendors.

The 402 answers are hand-built OFFLINE (no network here) in the two documented wire formats:
a v1 JSON body (`accepts[].maxAmountRequired`, network "base") and a v2 base64 `PAYMENT-REQUIRED`
header. They are served by `httpx.MockTransport`, which also proves no payment header is sent.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.x402 import probe

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x1111222233334444555566667777888899990000"

V1_BODY = {
    "x402Version": 1, "error": "X-PAYMENT header is required",
    "accepts": [{"scheme": "exact", "network": "base", "maxAmountRequired": "10000",
                 "resource": "https://pro-api.coingecko.com/api/v3/x402/simple/price",
                 "description": "CoinGecko simple price", "mimeType": "application/json", "payTo": PAY_TO,
                 "maxTimeoutSeconds": 60, "asset": USDC_BASE, "extra": {"name": "USD Coin", "version": "2"}}],
}
V2_REQUIRED = {
    "x402Version": 2, "resource": {"url": "https://api.example.com/quote", "description": "Quote"},
    "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "50000", "asset": USDC_BASE,
                 "payTo": PAY_TO, "maxTimeoutSeconds": 60, "extra": {"name": "USD Coin", "version": "2"}}],
}


def transport_returning(response: httpx.Response, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return response

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def empty_cache():
    probe.clear_cache()
    yield
    probe.clear_cache()


async def test_v1_body_402_is_parsed_and_never_paid():
    # Arrange
    seen: list[httpx.Request] = []
    transport = transport_returning(httpx.Response(402, json=V1_BODY), seen)

    # Act
    result = await probe.probe_vendor("CoinGecko", "https://pro-api.coingecko.com/x", transport=transport)

    # Assert
    assert result.error is None
    assert (result.network, result.amount_atomic, result.amount_human) == ("eip155:8453", "10000", 0.01)
    assert result.asset_symbol == "USDC (Base mainnet)"
    assert result.pay_to_short == "0x1111…0000"
    assert result.payable_from_budget is False
    assert result.payable_note == "No — USDC on Base mainnet; AUSD routing = roadmap"
    assert seen[0].method == "GET"
    assert not any(name.lower() in {"payment-signature", "x-payment"} for name in seen[0].headers)


async def test_v2_header_402_is_parsed():
    header = base64.b64encode(json.dumps(V2_REQUIRED).encode()).decode()
    transport = transport_returning(httpx.Response(402, headers={"PAYMENT-REQUIRED": header}))
    result = await probe.probe_vendor("x402stock", "https://api.example.com/quote", transport=transport)
    assert (result.x402_version, result.amount_human, result.description) == (2, 0.05, "Quote")


async def test_non_402_and_unparseable_answers_become_errors():
    ok = await probe.probe_vendor("A", "https://a.example", transport=transport_returning(httpx.Response(200, json={})))
    bad = await probe.probe_vendor("B", "https://b.example",
                                   transport=transport_returning(httpx.Response(402, content=b"not json")))
    assert ok.error == "expected HTTP 402, got 200"
    assert bad.error.startswith("unparseable 402")


async def test_network_failure_becomes_an_error():
    def handler(request):
        raise httpx.ConnectError("no route")

    result = await probe.probe_vendor("C", "https://c.example", transport=httpx.MockTransport(handler))
    assert "ConnectError" in result.error


async def test_probe_all_covers_every_target_and_caches_ten_minutes():
    # Arrange
    seen: list[httpx.Request] = []
    transport = transport_returning(httpx.Response(402, json=V1_BODY), seen)

    # Act
    first = await probe.probe_all(transport=transport)
    second = await probe.probe_all(transport=transport)
    refreshed = await probe.probe_all(refresh=True, transport=transport)

    # Assert
    assert [row["name"] for row in first] == ["CoinGecko", "CoinMarketCap", "Nansen", "x402stock"]
    assert first == second, "served from cache"
    assert [row["name"] for row in refreshed] == [row["name"] for row in first]
    assert len(seen) == 8, "the second call is served from cache; refresh probes again"
    assert {row["verified"] for row in first if row["name"] in {"Nansen", "x402stock"}} == {False}


def test_unknown_asset_and_amount_helpers():
    assert probe.asset_symbol("0xa9012a055bd4e0eDfF8Ce09f960291C09D5322dC") == "AUSD (testnet)"
    assert probe.asset_symbol("0xdead") == "unknown token"
    assert probe.amount_human("750000") == 0.75
