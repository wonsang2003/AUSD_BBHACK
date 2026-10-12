"""
Tests for `app/data/market.py` — the dashboard market strip (`GET /api/market`) built from FREE
sources only: CoinGecko (crypto), Hyperliquid mids (crypto fallback), yfinance (delayed equities).

Fixtures: the CoinGecko and Hyperliquid payloads in `tests/fixtures/` (hand-built OFFLINE in the
documented formats; not live recordings). yfinance is replaced by a stub returning the
`fast_info` numbers, so no network is used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.data import coingecko, hyperliquid, market
from app.data.coingecko import MarketQuote
from app.data.http import UpstreamError

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def fresh_equity_cache():
    market.clear_cache()
    yield
    market.clear_cache()


@pytest.fixture
def free_sources(monkeypatch):
    """CoinGecko and Hyperliquid answer from fixtures; yfinance from a stub."""

    async def coingecko_strip(symbols):
        return coingecko.parse_simple_price(load_json("coingecko_simple_price.json"), symbols)

    async def perp_contexts():
        return hyperliquid.parse_meta_and_asset_ctxs(load_json("hyperliquid_meta_and_asset_ctxs.json"))

    def equity_download(tickers):
        prices = {"NVDA": {"last_price": 190.0, "previous_close": 200.0},
                  "SPY": {"last_price": float("nan"), "previous_close": 650.0}}
        return {ticker: prices[ticker] for ticker in tickers if ticker in prices}

    monkeypatch.setattr(coingecko, "fetch_market_strip", coingecko_strip)
    monkeypatch.setattr(hyperliquid, "fetch_perp_contexts", perp_contexts)
    monkeypatch.setattr(market, "_download_equity_prices", equity_download)


def test_symbols_are_normalised_deduplicated_and_defaulted():
    assert market.normalize_symbols([" btc", "BTC", "nvda"]) == ["BTC", "NVDA"]
    assert market.normalize_symbols([]) == list(market.DEFAULT_STRIP_SYMBOLS)


def test_crypto_and_equity_symbols_are_split():
    assert market.split_symbols(["BTC", "NVDA", "HYPE", "SPY"]) == (["BTC", "HYPE"], ["NVDA", "SPY"])


def test_equity_quote_is_delayed_and_change_is_versus_previous_close():
    quote = market.equity_quote("nvda", 190.0, 200.0)
    assert quote == MarketQuote("NVDA", 190.0, pytest.approx(-5.0), market.SOURCE_YFINANCE, True)
    assert market.equity_quote("X", float("nan"), 1.0) is None
    assert market.equity_quote("X", 10.0, None).change_24h_pct is None


def test_merge_prefers_coingecko_then_hyperliquid_and_keeps_requested_order():
    # Arrange
    cg = [MarketQuote("BTC", 1.0, 0.0)]
    hl = [MarketQuote("BTC", 2.0, 0.0, market.SOURCE_HYPERLIQUID), MarketQuote("HYPE", 3.0, 0.0, market.SOURCE_HYPERLIQUID)]
    eq = [MarketQuote("NVDA", 4.0, 0.0, market.SOURCE_YFINANCE, True)]

    # Act
    merged = market.merge_strip(["NVDA", "HYPE", "BTC", "SOL"], cg, hl, eq)

    # Assert
    assert [(row.symbol, row.price) for row in merged] == [("NVDA", 4.0), ("HYPE", 3.0), ("BTC", 1.0)]


async def test_market_strip_labels_every_row_with_its_source(free_sources):
    # Act
    rows = await market.market_strip(["BTC", "HYPE", "NVDA", "SPY"])

    # Assert
    by_symbol = {row["symbol"]: row for row in rows}
    assert set(by_symbol) == {"BTC", "HYPE", "NVDA"}, "SPY has no usable yfinance price (NaN)"
    assert by_symbol["BTC"]["source"] == "coingecko" and by_symbol["BTC"]["delayed"] is False
    assert by_symbol["HYPE"]["source"] == "hyperliquid perp mid", "CoinGecko fixture has no HYPE price"
    assert by_symbol["HYPE"]["price"] == pytest.approx(47.91)
    assert by_symbol["NVDA"] == {"symbol": "NVDA", "price": 190.0, "change_24h_pct": pytest.approx(-5.0),
                                 "source": "yfinance (15m+ delayed)", "delayed": True}


async def test_a_failing_source_only_removes_its_rows(free_sources, monkeypatch):
    # Arrange
    async def coingecko_down(symbols):
        raise UpstreamError("coingecko", "HTTP 429")

    def yfinance_down(tickers):
        raise RuntimeError("Yahoo blocked the request")

    monkeypatch.setattr(coingecko, "fetch_market_strip", coingecko_down)
    monkeypatch.setattr(market, "_download_equity_prices", yfinance_down)

    # Act
    strip = await market.build_market_strip(["BTC", "ETH", "NVDA"])

    # Assert
    assert [(row.symbol, row.source) for row in strip.rows] == [
        ("BTC", "hyperliquid perp mid"), ("ETH", "hyperliquid perp mid"),
    ]
    assert set(strip.errors) == {"coingecko", "yfinance"}


async def test_equity_prices_are_cached_between_calls(free_sources, monkeypatch):
    # Arrange
    calls: list[list[str]] = []
    original = market._download_equity_prices

    def counting_download(tickers):
        calls.append(list(tickers))
        return original(tickers)

    monkeypatch.setattr(market, "_download_equity_prices", counting_download)

    # Act
    await market.market_strip(["NVDA"])
    await market.market_strip(["NVDA"])

    # Assert
    assert calls == [["NVDA"]]
