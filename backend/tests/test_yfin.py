"""
Tests for `app/data/yfin.py` — the yfinance equity option-chain adapter behind the
equity branch of the `expected_move` demo vendor.

Fixture: `tests/fixtures/yfin_nvda_option_chain.json` is a REALISTIC-SHAPE snapshot,
hand-built (Black-Scholes priced, with a mild smile) in exactly the shape
`fetch_option_snapshot` produces from yfinance's DataFrames, including the artefacts
yfinance really returns on illiquid wings (NaN volume → null, implied volatility 1e-05).
It is NOT a live recording: this container has no network access to Yahoo. Live
verification happens on the user's machine.

The blocking yfinance download is replaced in the failure tests by a function that
raises, so we can check that every yfinance failure degrades to `YFinanceError`.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.data import yfin
from app.data.yfin import EquityOptionQuote, ExpiryNotListedError, YFinanceError

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_NOW = datetime(2026, 10, 12, 18, 30, tzinfo=timezone.utc)   # Monday 14:30 New York


def load_snapshot() -> dict:
    return json.loads((FIXTURES / "yfin_nvda_option_chain.json").read_text())


def option(bid: float | None, ask: float | None, last: float | None) -> EquityOptionQuote:
    return EquityOptionQuote(
        contract_symbol="NVDA261016C00187500", strike=187.5, bid=bid, ask=ask,
        last_price=last, implied_volatility=0.47,
    )


@pytest.fixture(autouse=True)
def empty_snapshot_cache():
    yfin.clear_cache()
    yield
    yfin.clear_cache()


# ─── row parsing ─────────────────────────────────────────────────────────────

def test_option_rows_parse_into_quotes_with_nan_and_null_as_none():
    # Arrange
    rows = [
        {"contractSymbol": "A", "strike": 100.0, "bid": 1.0, "ask": 1.2, "lastPrice": float("nan"),
         "impliedVolatility": 0.4},
        {"contractSymbol": "B", "strike": 105.0, "bid": None, "ask": "n/a", "lastPrice": 0.5,
         "impliedVolatility": None},
    ]

    # Act
    quotes = yfin.parse_option_rows(rows)

    # Assert
    assert quotes[0].last_price is None
    assert quotes[1].bid is None and quotes[1].ask is None
    assert quotes[1].implied_volatility is None


def test_option_rows_without_a_positive_strike_are_dropped():
    rows = [{"contractSymbol": "A", "strike": None}, {"contractSymbol": "B", "strike": 0}, {"contractSymbol": "C", "strike": 5}]
    assert [q.contract_symbol for q in yfin.parse_option_rows(rows)] == ["C"]


def test_fixture_chain_parses_every_row():
    # Arrange
    snapshot = load_snapshot()

    # Act
    calls = yfin.parse_option_rows(snapshot["calls"])
    puts = yfin.parse_option_rows(snapshot["puts"])

    # Assert
    assert len(calls) == len(snapshot["calls"]) and len(puts) == len(snapshot["puts"])
    assert all(quote.strike > 0 for quote in calls + puts)


# ─── prices and volatility ───────────────────────────────────────────────────

def test_mid_price_is_the_bid_ask_mid_when_both_sides_are_quoted():
    assert yfin.mid_price(option(bid=3.70, ask=3.80, last=3.79)) == (pytest.approx(3.75), yfin.PRICE_BASIS_MID)


def test_mid_price_falls_back_to_last_trade_when_bid_and_ask_are_zero():
    # Arrange: outside market hours Yahoo shows bid = ask = 0
    quote = option(bid=0.0, ask=0.0, last=3.79)

    # Act / Assert
    assert yfin.mid_price(quote) == (3.79, yfin.PRICE_BASIS_LAST)


def test_mid_price_falls_back_to_last_trade_when_the_market_is_crossed():
    assert yfin.mid_price(option(bid=3.9, ask=3.8, last=3.85)) == (3.85, yfin.PRICE_BASIS_LAST)


def test_mid_price_is_none_when_nothing_is_usable():
    assert yfin.mid_price(option(bid=None, ask=None, last=0.0)) is None


@pytest.mark.parametrize(
    ("implied_volatility", "plausible"),
    [(0.47, True), (0.01, True), (5.0, True), (1e-05, False), (7.5, False), (None, False)],
)
def test_implausible_yfinance_ivs_are_rejected(implied_volatility, plausible):
    assert yfin.is_plausible_iv(implied_volatility) is plausible


# ─── expiries ────────────────────────────────────────────────────────────────

def test_equity_expiry_is_16_00_new_york_in_utc_during_daylight_saving():
    assert yfin.equity_expiry_datetime("2026-10-16") == datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)


def test_equity_expiry_is_16_00_new_york_in_utc_in_winter():
    assert yfin.equity_expiry_datetime("2026-12-18") == datetime(2026, 12, 18, 21, 0, tzinfo=timezone.utc)


def test_next_expiry_is_the_first_listed_one_far_enough_ahead():
    # Arrange: Friday 15:30 New York — the same-day expiry closes in 30 minutes
    friday_afternoon = datetime(2026, 10, 16, 19, 30, tzinfo=timezone.utc)
    expiries = ["2026-10-23", "2026-10-16", "2026-10-30"]

    # Act
    chosen = yfin.choose_expiry(expiries, "next", friday_afternoon, min_hours_to_expiry=1.0)

    # Assert
    assert chosen == "2026-10-23"


def test_a_requested_listed_date_is_used_as_is():
    assert yfin.choose_expiry(["2026-10-16", "2026-10-23"], "2026-10-23", SNAPSHOT_NOW, 1.0) == "2026-10-23"


def test_an_unlisted_date_raises_expiry_not_listed_with_the_listed_dates():
    with pytest.raises(ExpiryNotListedError, match="2026-10-16, 2026-10-23"):
        yfin.choose_expiry(["2026-10-16", "2026-10-23"], "2026-10-20", SNAPSHOT_NOW, 1.0)


def test_no_usable_expiry_for_next_raises_expiry_not_listed():
    with pytest.raises(ExpiryNotListedError):
        yfin.choose_expiry(["2026-10-01"], "next", SNAPSHOT_NOW, 1.0)


# ─── fetch: failures degrade to YFinanceError, successes are cached ──────────

async def test_any_yfinance_exception_becomes_yfinance_error(monkeypatch):
    # Arrange
    def broken_download(ticker, requested_expiry, now, min_hours):
        raise KeyError("regularMarketPrice")

    monkeypatch.setattr(yfin, "_download_snapshot", broken_download)

    # Act / Assert
    with pytest.raises(YFinanceError, match="yfinance failed: KeyError"):
        await yfin.fetch_option_snapshot("NVDA", "next", SNAPSHOT_NOW, 1.0)


async def test_a_hanging_yfinance_call_times_out_as_yfinance_error(monkeypatch):
    # Arrange
    def slow_download(ticker, requested_expiry, now, min_hours):
        import time
        time.sleep(0.5)
        return load_snapshot()

    monkeypatch.setattr(yfin, "_download_snapshot", slow_download)
    monkeypatch.setattr(yfin, "YFINANCE_TIMEOUT_SECONDS", 0.05)

    # Act / Assert
    with pytest.raises(YFinanceError, match="timed out"):
        await yfin.fetch_option_snapshot("NVDA", "next", SNAPSHOT_NOW, 1.0)
    await asyncio.sleep(0.5)   # let the worker thread finish before the next test


async def test_expiry_not_listed_passes_through_unchanged(monkeypatch):
    def unlisted(ticker, requested_expiry, now, min_hours):
        raise ExpiryNotListedError("expiry 2026-10-20 is not a usable listed expiry")

    monkeypatch.setattr(yfin, "_download_snapshot", unlisted)

    with pytest.raises(ExpiryNotListedError):
        await yfin.fetch_option_snapshot("NVDA", "2026-10-20", SNAPSHOT_NOW, 1.0)


async def test_snapshots_are_cached_per_ticker_and_requested_expiry(monkeypatch):
    # Arrange
    downloads = []

    def counting_download(ticker, requested_expiry, now, min_hours):
        downloads.append(ticker)
        return load_snapshot()

    monkeypatch.setattr(yfin, "_download_snapshot", counting_download)

    # Act
    first = await yfin.fetch_option_snapshot("nvda", "next", SNAPSHOT_NOW, 1.0)
    second = await yfin.fetch_option_snapshot("NVDA", "next", SNAPSHOT_NOW, 1.0)

    # Assert
    assert downloads == ["NVDA"], "the ticker is upper-cased and the second call is served from cache"
    assert first is second


def test_snapshot_values_are_plain_json_types():
    # Arrange
    snapshot = load_snapshot()

    # Act
    encoded = json.dumps(snapshot)

    # Assert
    assert json.loads(encoded) == snapshot
    assert not any(isinstance(value, float) and math.isnan(value) for row in snapshot["calls"] for value in row.values())
