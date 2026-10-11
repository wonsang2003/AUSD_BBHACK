"""
Tests for `app/data/deribit.py` — the Deribit option-chain adapter used by the
`expected_move` demo vendor (the default seller of the M2 payment gate) and `pm_edge`.

Fixtures: `tests/fixtures/deribit_*.json` are REALISTIC-SHAPE payloads, hand-built in
Deribit's exact JSON-RPC response format (envelope with `usIn`/`usOut`, one book-summary
item per option with `mark_iv` in percent, `SYN.` synthetic underlyings, a USDC listing
that mixes SOL/XRP/BTC linear options). They are NOT live recordings: this container has
no network access to Deribit. Their "as of" time is 2026-10-11 14:00 UTC.
Live verification happens on the user's machine (see the module docstring of deribit.py).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.data import deribit
from app.data.deribit import (
    VOL_FLAT,
    VOL_INTERPOLATED,
    VOL_LISTED,
    DeribitDataError,
    OptionChain,
    OptionQuote,
)
from app.sellers.optionmath import year_fraction

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)   # when the fixtures were "taken"
BTC_INDEX_IN_FIXTURE = 118250.37


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def btc_chain() -> OptionChain:
    return deribit.build_option_chain(
        "BTC", load_fixture("deribit_book_summary_btc.json"), load_fixture("deribit_index_btc_usd.json")
    )


def utc(year: int, month: int, day: int, hour: int = 8) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def quote(expiry: datetime, strike: float, option_type: str, mark_iv: float) -> OptionQuote:
    code = "C" if option_type == "call" else "P"
    return OptionQuote(
        instrument_name=f"BTC-X-{strike:g}-{code}", expiry=expiry, strike=strike,
        option_type=option_type, mark_iv=mark_iv, underlying_price=None,
    )


def tiny_chain(quotes: list[OptionQuote]) -> OptionChain:
    return OptionChain(asset="BTC", index_name="btc_usd", index_price=100_000.0, quotes=quotes, as_of=FIXTURE_NOW)


# ─── instrument names ────────────────────────────────────────────────────────

def test_expiry_code_parses_to_0800_utc_on_that_date():
    # Arrange / Act
    expiry = deribit.parse_expiry_code("27DEC26")

    # Assert
    assert expiry == datetime(2026, 12, 27, 8, 0, tzinfo=timezone.utc)


def test_expiry_code_accepts_a_single_digit_day():
    assert deribit.parse_expiry_code("3JAN27") == utc(2027, 1, 3)


@pytest.mark.parametrize("bad_code", ["27DEX26", "DEC26", "27DEC2026", ""])
def test_expiry_code_rejects_malformed_codes(bad_code):
    with pytest.raises(DeribitDataError):
        deribit.parse_expiry_code(bad_code)


def test_strike_code_reads_d_as_decimal_point_for_usdc_options():
    assert deribit.parse_strike_code("2d5") == 2.5
    assert deribit.parse_strike_code("100000") == 100000.0


@pytest.mark.parametrize("bad_strike", ["abc", "0", "-5", "nan"])
def test_strike_code_rejects_non_positive_or_non_numeric_strikes(bad_strike):
    with pytest.raises(DeribitDataError):
        deribit.parse_strike_code(bad_strike)


def test_instrument_name_splits_into_prefix_expiry_strike_and_type():
    # Arrange / Act
    parsed = deribit.parse_instrument_name("SOL_USDC-16OCT26-212d5-P")

    # Assert
    assert parsed.prefix == "SOL_USDC"
    assert parsed.expiry == utc(2026, 10, 16)
    assert parsed.strike == 212.5
    assert parsed.option_type == "put"


@pytest.mark.parametrize("bad_name", ["BTC-PERPETUAL", "BTC-27DEC26", "BTC-27DEC26-100000-X", "BTC-27DEC26-1-C-extra"])
def test_instrument_name_rejects_futures_and_malformed_names(bad_name):
    with pytest.raises(DeribitDataError):
        deribit.parse_instrument_name(bad_name)


# ─── assets ──────────────────────────────────────────────────────────────────

def test_supported_assets_are_btc_eth_sol_case_insensitive():
    assert deribit.is_supported_asset("btc")
    assert deribit.is_supported_asset("SOL")
    assert not deribit.is_supported_asset("NVDA")


def test_sol_options_live_in_the_usdc_listing_with_the_sol_usdc_index():
    # Arrange / Act
    underlying = deribit.get_underlying("sol")

    # Assert
    assert underlying.currency == "USDC"
    assert underlying.instrument_prefix == "SOL_USDC"
    assert underlying.index_name == "sol_usdc"


def test_unsupported_asset_raises_a_clear_error():
    with pytest.raises(DeribitDataError, match="supported: BTC, ETH, SOL"):
        deribit.get_underlying("DOGE")


# ─── payload parsing ─────────────────────────────────────────────────────────

def test_book_summary_converts_mark_iv_from_percent_to_decimal():
    # Arrange
    payload = load_fixture("deribit_book_summary_btc.json")
    first_item = payload["result"][0]

    # Act
    quotes = deribit.parse_book_summary(payload, "BTC")

    # Assert
    first_quote = next(q for q in quotes if q.instrument_name == first_item["instrument_name"])
    assert first_quote.mark_iv == pytest.approx(first_item["mark_iv"] / 100)
    assert first_quote.underlying_price == first_item["underlying_price"]


def test_usdc_listing_is_filtered_to_the_requested_asset_only():
    # Arrange
    payload = load_fixture("deribit_book_summary_usdc.json")

    # Act
    sol_quotes = deribit.parse_book_summary(payload, "SOL")

    # Assert
    assert sol_quotes, "the fixture lists SOL_USDC options"
    assert all(q.instrument_name.startswith("SOL_USDC-") for q in sol_quotes)
    assert len(sol_quotes) < len(payload["result"]), "XRP_USDC and BTC_USDC rows are dropped"


def test_items_without_a_positive_mark_iv_or_with_odd_names_are_skipped():
    # Arrange
    payload = {"usOut": 1, "result": [
        {"instrument_name": "BTC-16OCT26-120000-C", "mark_iv": 41.5},
        {"instrument_name": "BTC-16OCT26-120000-P", "mark_iv": 0},
        {"instrument_name": "BTC-16OCT26-125000-C"},
        {"instrument_name": "BTC-16OCX26-130000-C", "mark_iv": 40.0},
    ]}

    # Act
    quotes = deribit.parse_book_summary(payload, "BTC")

    # Assert
    assert [q.instrument_name for q in quotes] == ["BTC-16OCT26-120000-C"]


def test_error_envelope_raises_instead_of_returning_an_empty_chain():
    payload = {"jsonrpc": "2.0", "error": {"code": 10001, "message": "rate limit"}, "usOut": 1}
    with pytest.raises(DeribitDataError, match="rate limit"):
        deribit.parse_book_summary(payload, "BTC")


def test_payload_that_is_not_an_object_raises_a_data_error():
    with pytest.raises(DeribitDataError, match="not a JSON object"):
        deribit.parse_index_price(["not", "an", "envelope"])


def test_index_price_is_read_from_result_index_price():
    assert deribit.parse_index_price(load_fixture("deribit_index_btc_usd.json")) == BTC_INDEX_IN_FIXTURE


def test_missing_or_zero_index_price_raises():
    with pytest.raises(DeribitDataError):
        deribit.parse_index_price({"result": {"index_price": 0}, "usOut": 1})


def test_response_time_reads_us_out_as_utc():
    assert deribit.response_time(load_fixture("deribit_index_btc_usd.json")) == FIXTURE_NOW


def test_build_option_chain_combines_quotes_index_and_as_of_time():
    # Arrange / Act
    chain = btc_chain()

    # Assert
    assert chain.asset == "BTC"
    assert chain.index_name == "btc_usd"
    assert chain.index_price == BTC_INDEX_IN_FIXTURE
    assert chain.as_of == FIXTURE_NOW
    assert len(chain.quotes) == len(load_fixture("deribit_book_summary_btc.json")["result"])


def test_build_option_chain_with_no_usable_options_raises():
    empty_book = {"result": [], "usOut": 1}
    with pytest.raises(DeribitDataError, match="no BTC options"):
        deribit.build_option_chain("BTC", empty_book, load_fixture("deribit_index_btc_usd.json"))


# ─── volatility at any expiry and strike ─────────────────────────────────────

def test_listed_expiries_are_sorted_and_exclude_the_past():
    # Arrange
    chain = btc_chain()
    after_first_expiry = utc(2026, 10, 12, 9)

    # Act
    expiries = deribit.listed_expiries(chain, after_first_expiry)

    # Assert
    assert expiries == sorted(expiries)
    assert utc(2026, 10, 12) not in expiries
    assert expiries[0] == utc(2026, 10, 16)


def test_vol_at_strike_averages_call_and_put_at_the_nearest_listed_strike():
    # Arrange
    expiry = utc(2026, 10, 16)
    chain = tiny_chain([
        quote(expiry, 95_000, "call", 0.50), quote(expiry, 100_000, "call", 0.40),
        quote(expiry, 100_000, "put", 0.44), quote(expiry, 105_000, "call", 0.38),
    ])

    # Act
    point = deribit.vol_at_strike(chain, expiry, 101_000)

    # Assert
    assert point.strike == 100_000
    assert point.sigma == pytest.approx(0.42)
    assert len(point.instruments) == 2


def test_vol_at_strike_breaks_ties_towards_the_lower_strike():
    expiry = utc(2026, 10, 16)
    chain = tiny_chain([quote(expiry, 95_000, "call", 0.50), quote(expiry, 105_000, "call", 0.38)])

    assert deribit.vol_at_strike(chain, expiry, 100_000).strike == 95_000


def test_implied_vol_on_a_listed_expiry_is_read_directly():
    # Arrange
    chain = btc_chain()
    listed = utc(2026, 10, 16)

    # Act
    estimate = deribit.implied_vol_at(chain, listed, chain.index_price, FIXTURE_NOW)

    # Assert
    assert estimate.method == VOL_LISTED
    assert estimate.sigma == deribit.vol_at_strike(chain, listed, chain.index_price).sigma


def test_implied_vol_between_two_expiries_interpolates_total_variance_linearly_in_time():
    # Arrange
    near, far, target = utc(2026, 10, 16), utc(2026, 10, 23), utc(2026, 10, 20)
    chain = tiny_chain([quote(near, 100_000, "call", 0.40), quote(far, 100_000, "call", 0.50)])
    t1, t2, t = (year_fraction(FIXTURE_NOW, moment) for moment in (near, far, target))
    expected_variance = 0.40**2 * t1 + (t - t1) / (t2 - t1) * (0.50**2 * t2 - 0.40**2 * t1)

    # Act
    estimate = deribit.implied_vol_at(chain, target, 100_000, FIXTURE_NOW)

    # Assert
    assert estimate.method == VOL_INTERPOLATED
    assert estimate.sigma == pytest.approx(math.sqrt(expected_variance / t))
    assert [point.expiry for point in estimate.points] == [near, far]


def test_implied_vol_after_the_last_listed_expiry_is_held_flat():
    # Arrange
    near = utc(2026, 10, 16)
    chain = tiny_chain([quote(near, 100_000, "call", 0.40)])

    # Act
    estimate = deribit.implied_vol_at(chain, utc(2026, 12, 1), 100_000, FIXTURE_NOW)

    # Assert
    assert estimate.method == VOL_FLAT
    assert estimate.sigma == 0.40


def test_implied_vol_for_a_past_expiry_raises():
    chain = btc_chain()
    with pytest.raises(DeribitDataError, match="not in the future"):
        deribit.implied_vol_at(chain, utc(2026, 10, 1), 100_000, FIXTURE_NOW)


def test_every_vol_method_has_a_confidence_between_zero_and_one():
    for method in (VOL_LISTED, VOL_INTERPOLATED, VOL_FLAT):
        assert 0.0 < deribit.CONFIDENCE_BY_VOL_METHOD[method] <= 1.0


# ─── network functions (the HTTP call itself is replaced by a recorder) ──────

class RecordingGetJson:
    """Stands in for `app.data.http.get_json`: records each call and returns a fixture payload."""

    def __init__(self, responses: dict[str, dict]):
        self.responses = responses
        self.calls: list[dict] = []

    async def __call__(self, url: str, *, source: str, params: dict | None = None, ttl: float = 60.0, **_):
        self.calls.append({"url": url, "source": source, "params": params, "ttl": ttl})
        endpoint = url.rsplit("/", 1)[-1]
        return self.responses[endpoint]


async def test_fetch_option_chain_asks_deribit_for_the_right_listing_and_index(monkeypatch):
    # Arrange
    recorder = RecordingGetJson({
        "get_book_summary_by_currency": load_fixture("deribit_book_summary_usdc.json"),
        "get_index_price": load_fixture("deribit_index_sol_usdc.json"),
    })
    monkeypatch.setattr(deribit, "get_json", recorder)

    # Act
    chain = await deribit.fetch_option_chain("SOL")

    # Assert
    book_call, index_call = recorder.calls
    assert book_call["url"] == "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
    assert book_call["params"] == {"currency": "USDC", "kind": "option"}
    assert index_call["params"] == {"index_name": "sol_usdc"}
    assert {call["source"] for call in recorder.calls} == {"deribit"}
    assert chain.asset == "SOL" and chain.index_price == 212.48
