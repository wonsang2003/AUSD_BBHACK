"""
Tests for `app/sellers/pm_edge.py` — the pm_edge demo vendor (0.10 AUSD): Gemini crypto
prediction-market YES prices vs options-implied N(d2) from Deribit, edge after Gemini fees.

Data: Gemini answers come from `tests/fixtures/gemini_*.json` and Deribit answers from
`tests/fixtures/deribit_*.json`. All are REALISTIC-SHAPE fixtures hand-built OFFLINE in the exact
upstream formats; they are NOT live recordings (no network from this container). In the
end-to-end tests only the upstream download functions are replaced by fixture loaders and the
clock is pinned to the fixtures' time (2026-10-11 14:00 UTC); parsing, option math, fee math and
error handling all run for real.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import get_settings
from app.data import deribit, gemini
from app.data.http import UpstreamError
from app.sellers import pm_edge
from app.sellers.expected_move import SellerConfigError
from app.sellers.optionmath import gemini_taker_fee, prob_above, year_fraction

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
PAYOUT_ADDRESS = "0x" + "7a" * 20
DERIBIT_FIXTURE_FILES = {
    "BTC": ("deribit_book_summary_btc.json", "deribit_index_btc_usd.json"),
    "ETH": ("deribit_book_summary_eth.json", "deribit_index_eth_usd.json"),
    "SOL": ("deribit_book_summary_usdc.json", "deribit_index_sol_usdc.json"),
}
REQUIRED_RESPONSE_FIELDS = ("inputs_used", "computed_at", "method", "confidence", "data_delay")


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def btc_chain() -> deribit.OptionChain:
    book, index = DERIBIT_FIXTURE_FILES["BTC"]
    return deribit.build_option_chain("BTC", load_json(book), load_json(index))


@pytest.fixture
def upstreams_from_fixtures(monkeypatch):
    """Gemini + Deribit served from fixtures; Get Event works for BTC and fails (503) for others."""

    async def events(category: str) -> dict:
        return load_json("gemini_events_crypto.json")

    async def event_detail(ticker: str) -> dict:
        if ticker == "BTC2610160800":
            return load_json("gemini_event_BTC2610160800.json")
        raise UpstreamError("gemini", "HTTP 503")

    async def book_summary(asset: str) -> dict:
        return load_json(DERIBIT_FIXTURE_FILES[asset][0])

    async def index(asset: str) -> dict:
        return load_json(DERIBIT_FIXTURE_FILES[asset][1])

    monkeypatch.setattr(gemini, "fetch_events_payload", events)
    monkeypatch.setattr(gemini, "fetch_event_payload", event_detail)
    monkeypatch.setattr(deribit, "fetch_book_summary_payload", book_summary)
    monkeypatch.setattr(deribit, "fetch_index_payload", index)


# ─── offer ────────────────────────────────────────────────────────────────────

def test_offer_costs_ten_cents_and_is_labelled_demo_vendor(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_pm_edge", PAYOUT_ADDRESS)
    offer = pm_edge.make_offer()
    assert offer.price_atomic == 100_000, "0.10 AUSD with 6 decimals"
    assert offer.path == "/sell/pm-edge"
    assert offer.pay_to.lower() == PAYOUT_ADDRESS
    assert "demo vendor" in offer.description.lower()


def test_offer_without_payout_address_raises_a_clear_error(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_pm_edge", "")
    with pytest.raises(SellerConfigError, match="SELLER_PAYTO_PM_EDGE is empty"):
        pm_edge.make_offer()


# ─── request parsing ─────────────────────────────────────────────────────────

def test_request_defaults_to_crypto_all_assets_and_twenty_rows():
    assert pm_edge.parse_request({}) == pm_edge.PmEdgeRequest("crypto", None, 20)


@pytest.mark.parametrize("params", [{"category": "sports"}, {"asset": "DOGE"}, {"limit": "0"}, {"limit": "x"}])
def test_invalid_requests_raise_input_errors(params):
    with pytest.raises(pm_edge.PmEdgeInputError):
        pm_edge.parse_request(params)


# ─── edge and fee math (pure) ────────────────────────────────────────────────

def test_trade_side_follows_the_sign_of_the_edge():
    assert [pm_edge.trade_side(edge) for edge in (0.02, -0.02, 0.0)] == ["yes", "no", "none"]


def test_fee_per_contract_is_the_gemini_taker_fee_for_100_contracts_divided_by_100():
    # 0.07 · 100 · 0.86 · 0.14 = 0.8428 → ceil to cent 0.85 → 0.0085 per contract
    assert pm_edge.fee_per_contract(Decimal("0.86")) == pytest.approx(0.0085)
    assert gemini_taker_fee(100, "0.86") == pytest.approx(0.85)


def test_entry_price_for_no_is_one_minus_the_yes_bid():
    quote = gemini.YesQuote(Decimal("0.5"), gemini.BASIS_MID, Decimal("0.52"), Decimal("0.52"), Decimal("0.04"))
    assert pm_edge.entry_price("no", quote) == (Decimal("0.52"), pm_edge.ENTRY_BASIS_ASK)


def test_edge_after_fees_is_zero_when_spread_and_fee_eat_the_edge():
    profit = pm_edge.expected_profit_per_contract("yes", model_p=0.51, price=Decimal("0.51"), fee=0.0175)
    assert profit < 0
    assert pm_edge.edge_after_fees(0.0137, profit) == 0.0
    assert pm_edge.edge_after_fees(-0.05, 0.02) == -0.02


def test_compare_contract_matches_n_d2_from_the_listed_expiry():
    # Arrange: the 112k contract expires at a listed Deribit expiry (16 Oct 08:00 UTC)
    event = gemini.parse_event(load_json("gemini_event_BTC2610160800.json"))
    contract = gemini.parse_contracts(event)[0]
    candidate = pm_edge.Candidate(contract, gemini.parse_price_threshold(contract))
    chain = btc_chain()

    # Act
    row = pm_edge.compare_contract(candidate, chain, FIXTURE_NOW)

    # Assert
    expiry = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)
    sigma = deribit.implied_vol_at(chain, expiry, 112_000.0, FIXTURE_NOW).sigma
    expected_model_p = prob_above(chain.index_price, 112_000.0, sigma, year_fraction(FIXTURE_NOW, expiry))
    assert row["model_p"] == pytest.approx(expected_model_p, abs=1e-4)
    assert row["market_p"] == pytest.approx(0.85)
    assert row["edge"] == pytest.approx(expected_model_p - 0.85, abs=1e-4)
    assert row["sigma_method"] == deribit.VOL_LISTED
    for field in ("ticker", "title", "asset", "strike", "expiry", "market_p", "model_p", "edge", "edge_after_fees"):
        assert field in row


# ─── end to end (fixtures) ───────────────────────────────────────────────────

async def test_compute_returns_rows_sorted_by_absolute_edge(upstreams_from_fixtures):
    # Act
    response = await pm_edge.compute_at({"category": "crypto"}, FIXTURE_NOW)

    # Assert
    assert "error" not in response
    edges = [abs(row["edge"]) for row in response["rows"]]
    assert edges == sorted(edges, reverse=True)
    assert {row["asset"] for row in response["rows"]} == {"BTC", "ETH"}
    for field in REQUIRED_RESPONSE_FIELDS:
        assert field in response


async def test_compute_uses_fresh_get_event_prices_and_falls_back_to_the_list_copy(upstreams_from_fixtures):
    response = await pm_edge.compute_at({}, FIXTURE_NOW)
    row_118k = next(row for row in response["rows"] if row["ticker"] == "BTC2610160800-HI118000")
    assert row_118k["market_p"] == pytest.approx(0.495), "Get Event moved the 118k mid to (0.48 + 0.51) / 2"
    assert "ETH2610200800" in response["inputs_used"]["event_detail_fallbacks"]
    eth_row = next(row for row in response["rows"] if row["asset"] == "ETH")
    assert eth_row["sigma_method"] == deribit.VOL_INTERPOLATED, "20 Oct lies between listed 16 Oct and 23 Oct"


async def test_compute_lists_every_contract_it_could_not_compare_with_a_reason(upstreams_from_fixtures):
    response = await pm_edge.compute_at({}, FIXTURE_NOW)
    reasons = {entry["event_ticker"]: entry["reason"] for entry in response["skipped"]}
    assert "5/15-minute" in reasons["BTC05M2610111405"]
    assert "DOGE" in reasons["DOGE2610160800"]
    assert "touch" in reasons["BTCMAX26"]
    assert "less than 1h" in reasons["SOL2610111430"]


async def test_asset_filter_and_limit_are_applied(upstreams_from_fixtures):
    response = await pm_edge.compute_at({"asset": "eth", "limit": "1"}, FIXTURE_NOW)
    assert len(response["rows"]) == 1 and response["rows_total"] == 2
    assert response["rows"][0]["asset"] == "ETH"


async def test_gemini_outage_returns_an_error_dict_not_an_exception(monkeypatch):
    async def gemini_down(category: str) -> dict:
        raise UpstreamError("gemini", "failed after 3 attempts (HTTP 503)")

    monkeypatch.setattr(gemini, "fetch_events_payload", gemini_down)
    response = await pm_edge.compute_at({}, FIXTURE_NOW)
    assert response["error_kind"] == pm_edge.ERROR_UPSTREAM
    assert response["confidence"] == 0.0


async def test_deribit_outage_means_no_comparable_contract_and_an_error(upstreams_from_fixtures, monkeypatch):
    async def deribit_down(asset: str) -> dict:
        raise UpstreamError("deribit", "HTTP 502")

    monkeypatch.setattr(deribit, "fetch_book_summary_payload", deribit_down)
    response = await pm_edge.compute_at({}, FIXTURE_NOW)
    assert response["error_kind"] == pm_edge.ERROR_NO_DATA
    assert any("chain unavailable" in entry["reason"] for entry in response["skipped"])


async def test_bad_request_returns_an_error_dict():
    response = await pm_edge.compute_at({"category": "politics"}, FIXTURE_NOW)
    assert response["error_kind"] == pm_edge.ERROR_BAD_REQUEST
