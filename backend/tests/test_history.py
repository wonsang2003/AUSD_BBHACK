"""
Tests for `app/data/history.py` (GET /api/market/history). Offline: the payloads below are
hand-built in the documented Hyperliquid candleSnapshot / CoinGecko market_chart shapes, and the
fetchers are monkeypatched, so no network is used.
"""

from __future__ import annotations

import asyncio

import pytest

from app.data import history

NOW_S = 1_760_000_000

HL_CANDLES = [
    {"t": (NOW_S - 600) * 1000, "T": (NOW_S - 301) * 1000, "s": "BTC", "i": "5m",
     "o": "64000.0", "c": "64100.5", "h": "64150", "l": "63990", "v": "12.3", "n": 410},
    {"t": (NOW_S - 300) * 1000, "T": (NOW_S - 1) * 1000, "s": "BTC", "i": "5m",
     "o": "64100.5", "c": "64250.0", "h": "64300", "l": "64050", "v": "9.1", "n": 377},
    {"t": 0, "T": 1000, "c": "not-a-number"},
]

CG_CHART = {
    "prices": [[(NOW_S - 120) * 1000, 3010.5], [(NOW_S - 60) * 1000, 3020.25], [NOW_S * 1000, None]],
    "market_caps": [],
    "total_volumes": [],
}


def test_parse_hyperliquid_candles_uses_close_time_and_price_and_skips_bad_rows():
    points = history.parse_hyperliquid_candles(HL_CANDLES)
    assert points == [{"t": NOW_S - 301, "p": 64100.5}, {"t": NOW_S - 1, "p": 64250.0}]


def test_parse_coingecko_market_chart_drops_missing_prices():
    points = history.parse_coingecko_market_chart(CG_CHART)
    assert points == [{"t": NOW_S - 120, "p": 3010.5}, {"t": NOW_S - 60, "p": 3020.25}]


def test_parse_rejects_wrong_shapes():
    with pytest.raises(ValueError):
        history.parse_hyperliquid_candles({"error": "x"})
    with pytest.raises(ValueError):
        history.parse_coingecko_market_chart([])


def test_parse_yfinance_rows_drops_nan():
    assert history.parse_yfinance_rows([(NOW_S, 101.5), (NOW_S - 300, float("nan"))]) == [{"t": NOW_S, "p": 101.5}]


def test_normalize_symbol_and_crypto_split():
    assert history.normalize_symbol(" btc-usd ") == "BTC"
    assert history.is_crypto("ETH") and not history.is_crypto("NVDA")


def test_fetch_history_falls_back_to_coingecko_when_hyperliquid_fails(monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    async def chart(*_args, **_kwargs):
        return CG_CHART

    monkeypatch.setattr(history, "post_json", broken)
    monkeypatch.setattr(history, "get_json", chart)
    body = asyncio.run(history.fetch_history("eth", 240, now_s=NOW_S))
    assert body["symbol"] == "ETH" and body["source"] == history.SOURCE_COINGECKO
    assert body["delayed"] is False and len(body["points"]) == 2


def test_fetch_history_degrades_to_empty_points_with_error(monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(history, "post_json", broken)
    monkeypatch.setattr(history, "get_json", broken)
    body = asyncio.run(history.fetch_history("BTC", 240, now_s=NOW_S))
    assert body["points"] == [] and "offline" in body["error"]


def test_fetch_history_uses_hyperliquid_first(monkeypatch):
    async def candles(*_args, **_kwargs):
        return HL_CANDLES

    monkeypatch.setattr(history, "post_json", candles)
    body = asyncio.run(history.fetch_history("BTC", 240, now_s=NOW_S))
    assert body["source"] == history.SOURCE_HYPERLIQUID and body["points"][-1]["p"] == 64250.0


def test_fetch_history_rejects_garbage_symbol_without_network():
    body = asyncio.run(history.fetch_history("BTC;rm", 240, now_s=NOW_S))
    assert body["points"] == [] and "invalid symbol" in body["error"]
