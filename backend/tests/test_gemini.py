"""
Tests for `app/data/gemini.py` — Gemini Predictions public events/contracts for `pm_edge`
and the economics odds in `macro_pulse`.

Fixtures: `tests/fixtures/gemini_events_crypto.json`, `gemini_event_BTC2610160800.json` and
`gemini_events_economics.json` are REALISTIC-SHAPE payloads hand-built OFFLINE in the documented
List Events / Get Event formats (prices as decimal strings or null, `strike {value, type}`,
documented crypto ticker formats). They are not live recordings: no network from this container.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.data import gemini
from app.data.gemini import GeminiDataError, PredictionContract, UnsupportedContract

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def crypto_events() -> list[dict]:
    return gemini.parse_events(load_json("gemini_events_crypto.json"))


def contracts_of(ticker: str) -> list[PredictionContract]:
    event = next(event for event in crypto_events() if event["ticker"] == ticker)
    return gemini.parse_contracts(event)


def test_list_events_payload_yields_the_event_objects():
    assert [event["ticker"] for event in crypto_events()][:2] == ["BTC2610160800", "ETH2610200800"]


@pytest.mark.parametrize("payload", [{}, {"data": "x"}, []])
def test_list_payload_without_data_list_raises(payload):
    with pytest.raises(GeminiDataError):
        gemini.parse_events(payload)


def test_get_event_payload_must_have_ticker_and_contracts():
    assert gemini.parse_event(load_json("gemini_event_BTC2610160800.json"))["ticker"] == "BTC2610160800"
    with pytest.raises(GeminiDataError):
        gemini.parse_event({"ticker": "X"})


def test_contract_prices_are_exact_decimals_and_symbol_is_kept_verbatim():
    contract = contracts_of("BTC2610160800")[0]
    assert contract.best_bid == Decimal("0.84") and contract.best_ask == Decimal("0.86")
    assert contract.instrument_symbol == "GEMI-BTC2610160800-HI112000"
    assert contract.expiry == datetime(2026, 10, 16, 8, tzinfo=timezone.utc)
    assert contract.strike_value == Decimal("112000.00") and contract.strike_type == "above"


def test_closed_contract_is_not_tradable():
    closed = contracts_of("BTC2610160800")[4]
    assert closed.market_state == "closed"
    assert not gemini.is_tradable(closed)


def test_documented_instrument_symbol_is_parsed_including_decimal_strike():
    parsed = gemini.parse_crypto_instrument_symbol("GEMI-XRP2603230800-HI2D20")
    assert parsed.underlying == "XRP" and parsed.strike == Decimal("2.20")
    assert parsed.expiry == datetime(2026, 3, 23, 8, tzinfo=timezone.utc)
    assert gemini.parse_crypto_instrument_symbol("GEMI-BTC05M2602251745-UP").strike is None
    assert gemini.parse_crypto_instrument_symbol("not-a-symbol") is None


def test_short_duration_events_are_recognised():
    assert gemini.is_short_duration_event("BTC05M2610111405")
    assert not gemini.is_short_duration_event("BTC2610160800")


def test_event_asset_comes_from_title_or_documented_ticker():
    assert gemini.event_asset({"title": "Ethereum price on Oct 20", "ticker": "X"}) == "ETH"
    assert gemini.event_asset({"title": "Daily close", "ticker": "SOL2610160800"}) == "SOL"
    assert gemini.event_asset({"title": "Fed decision", "ticker": "FEDOCT26"}) is None


@pytest.mark.parametrize(
    ("label", "strike"),
    [("BTC > $110,000", Decimal("110000")), ("ETH above $4,200.50", Decimal("4200.50")),
     ("BTC over $110k", Decimal("110000")), ("$95,000 or above", Decimal("95000")), ("Up", None)],
)
def test_label_strike_patterns(label, strike):
    assert gemini.parse_label_strike(label) == strike


def test_price_threshold_reads_asset_strike_and_expiry():
    threshold = gemini.parse_price_threshold(contracts_of("ETH2610200800")[1])
    assert (threshold.asset, threshold.strike) == ("ETH", 4500.0)
    assert threshold.expiry == datetime(2026, 10, 20, 8, tzinfo=timezone.utc)


def test_reference_strike_and_touch_markets_are_unsupported():
    with pytest.raises(UnsupportedContract, match="reference"):
        gemini.parse_price_threshold(contracts_of("BTC05M2610111405")[0])
    with pytest.raises(UnsupportedContract, match="touch"):
        gemini.parse_price_threshold(contracts_of("BTCMAX26")[0])


def test_yes_quote_uses_mid_then_last_trade():
    # Arrange
    two_sided, _, _, ask_only, _ = contracts_of("BTC2610160800")

    # Act
    mid = gemini.yes_quote(two_sided)
    last = gemini.yes_quote(ask_only)

    # Assert
    assert (mid.probability, mid.basis, mid.no_ask, mid.spread) == (
        Decimal("0.85"), gemini.BASIS_MID, Decimal("0.16"), Decimal("0.02"))
    assert (last.probability, last.basis, last.no_ask) == (Decimal("0.03"), gemini.BASIS_LAST_TRADE, None)


def test_contract_without_any_price_is_unsupported():
    economics = gemini.parse_events(load_json("gemini_events_economics.json"))
    hike = gemini.parse_contracts(economics[0])[2]
    with pytest.raises(UnsupportedContract, match="no bid"):
        gemini.yes_quote(hike)


async def test_fetch_events_asks_for_active_events_of_a_category(monkeypatch):
    # Arrange
    sent: dict = {}

    async def fake_get_json(url, *, source, params=None, headers=None, ttl=60.0):
        sent.update(url=url, params=params, source=source)
        return load_json("gemini_events_crypto.json")

    monkeypatch.setattr(gemini, "get_json", fake_get_json)

    # Act
    await gemini.fetch_events_payload("crypto")
    events_call = dict(sent)
    await gemini.fetch_event_payload("BTC2610160800")

    # Assert
    assert events_call["url"] == "https://api.gemini.com/v1/prediction-markets/events"
    assert events_call["params"] == {"status": "active", "category": "crypto", "limit": 200}
    assert sent["url"] == "https://api.gemini.com/v1/prediction-markets/events/BTC2610160800"
