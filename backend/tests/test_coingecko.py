"""
Tests for `app/data/coingecko.py` — the keyless CoinGecko simple/price adapter (FREE input for
the dashboard market strip; PocketDesk never pays CoinGecko's x402 endpoint).

Fixture: `tests/fixtures/coingecko_simple_price.json` is a REALISTIC-SHAPE payload hand-built
OFFLINE in CoinGecko's documented `simple/price` format; it is not a live recording (no network
from this container).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.data import coingecko

FIXTURES = Path(__file__).parent / "fixtures"


def load_payload() -> dict:
    return json.loads((FIXTURES / "coingecko_simple_price.json").read_text())


def test_params_map_tickers_to_coingecko_ids_and_ask_for_24h_change():
    params = coingecko.build_simple_price_params(["btc", "ETH", "NVDA"])
    assert params == {"ids": "bitcoin,ethereum", "vs_currencies": "usd", "include_24hr_change": "true"}


def test_parse_keeps_the_requested_order_and_drops_unpriced_symbols():
    # Act
    quotes = coingecko.parse_simple_price(load_payload(), ["SOL", "BTC", "DOGE"])

    # Assert
    assert [quote.symbol for quote in quotes] == ["SOL", "BTC"], "DOGE is not in the payload"
    assert quotes[1].price == pytest.approx(118254.11)
    assert quotes[1].change_24h_pct == pytest.approx(1.9450318012)
    assert quotes[1].to_dict() == {"symbol": "BTC", "price": 118254.11, "change_24h_pct": 1.9450318012,
                                   "source": "coingecko", "delayed": False}


def test_missing_change_field_gives_none_not_an_error():
    quotes = coingecko.parse_simple_price({"bitcoin": {"usd": 100.0}}, ["BTC"])
    assert quotes[0].change_24h_pct is None


async def test_fetch_defaults_to_btc_eth_sol_in_one_request(monkeypatch):
    # Arrange
    calls: list[dict] = []

    async def fake_get_json(url, *, source, params=None, headers=None, ttl=60.0):
        calls.append({"url": url, "params": params, "source": source})
        return load_payload()

    monkeypatch.setattr(coingecko, "get_json", fake_get_json)

    # Act
    quotes = await coingecko.fetch_market_strip()

    # Assert
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.coingecko.com/api/v3/simple/price"
    assert calls[0]["params"]["ids"] == "bitcoin,ethereum,solana"
    assert [quote.symbol for quote in quotes] == ["BTC", "ETH", "SOL"]


async def test_fetch_with_only_unknown_symbols_sends_no_request(monkeypatch):
    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("no request expected")

    monkeypatch.setattr(coingecko, "get_json", must_not_be_called)
    assert await coingecko.fetch_market_strip(["NVDA"]) == []
