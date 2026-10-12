"""
Tests for `app/sellers/insider_cluster.py` — the insider_cluster demo vendor (0.03 AUSD): SEC
Form 4 open-market purchases by ≥ 2 distinct insiders within 10 days.

Fixtures (hand-built OFFLINE in EDGAR's exact formats; NOT live recordings, no network here):
`edgar_company_tickers.json`, `edgar_submissions_acme.json` and three Form 4 XMLs for the
fictitious "Acme Robotics Inc." (ACME). Between 30 Sep and 6 Oct 2026 a director buys twice and
the CFO buys once (a cluster); the CEO sells; one older purchase falls outside the look-back.
Only the HTTP helpers (`edgar.get_json` / `edgar.get_text`) are replaced; URL building, the
User-Agent header, XML parsing and the cluster rule run for real.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.data import edgar
from app.data.edgar import InsiderTransaction
from app.data.http import UpstreamError
from app.sellers import insider_cluster
from app.sellers.expected_move import ERROR_BAD_REQUEST, ERROR_NO_DATA, ERROR_UPSTREAM, SellerConfigError
from app.sellers.insider_cluster import Purchase

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
PAYOUT_ADDRESS = "0x" + "5f" * 20
FORM4_BY_ACCESSION_FOLDER = {
    "000199900126000041": "edgar_form4_acme_000041.xml",
    "000199900126000039": "edgar_form4_acme_000039.xml",
    "000199900126000035": "edgar_form4_acme_000035.xml",
}


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def purchase(insider: str, on: date, value: float) -> Purchase:
    return Purchase(insider, insider, "Director", on, 100.0, value / 100.0, value, "acc")


@pytest.fixture
def edgar_from_fixtures(monkeypatch):
    """Serve EDGAR URLs from fixture files; record the headers of every request."""
    seen_headers: list[dict] = []

    async def fake_get_json(url, *, source, params=None, headers=None, ttl=60.0):
        seen_headers.append(headers)
        if url == edgar.SEC_COMPANY_TICKERS_URL:
            return load_json("edgar_company_tickers.json")
        if url == "https://data.sec.gov/submissions/CIK0001999001.json":
            return load_json("edgar_submissions_acme.json")
        raise UpstreamError("edgar", f"HTTP 404: {url}")

    async def fake_get_text(url, *, source, params=None, headers=None, ttl=300.0):
        seen_headers.append(headers)
        folder = url.split("/")[-2]
        return (FIXTURES / FORM4_BY_ACCESSION_FOLDER[folder]).read_text()

    monkeypatch.setattr(edgar, "get_json", fake_get_json)
    monkeypatch.setattr(edgar, "get_text", fake_get_text)
    return seen_headers


# ─── offer and params ────────────────────────────────────────────────────────

def test_offer_costs_three_cents_and_is_labelled_demo_vendor(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_insider", PAYOUT_ADDRESS)
    offer = insider_cluster.make_offer()
    assert (offer.price_atomic, offer.path) == (30_000, "/sell/insider-cluster")
    assert "demo vendor" in offer.description.lower()


def test_offer_without_payout_address_raises(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_insider", "")
    with pytest.raises(SellerConfigError, match="SELLER_PAYTO_INSIDER"):
        insider_cluster.make_offer()


def test_params_default_to_ninety_days():
    assert insider_cluster.parse_params({"ticker": "acme"}) == ("ACME", 90)


@pytest.mark.parametrize("params", [{}, {"ticker": "1ABC"}, {"ticker": "ACME", "days": "0"},
                                    {"ticker": "ACME", "days": "x"}, {"ticker": "ACME", "days": "400"}])
def test_invalid_params_raise(params):
    with pytest.raises(ValueError):
        insider_cluster.parse_params(params)


# ─── cluster rule (pure) ─────────────────────────────────────────────────────

def test_only_open_market_purchases_inside_the_lookback_count():
    # Arrange
    def tx(code: str, on: date, shares: float = 10.0) -> InsiderTransaction:
        return InsiderTransaction("1", "A", True, False, "", False, "Common", on, code, shares, 5.0, "A", "acc")

    transactions = [tx("P", date(2026, 10, 1)), tx("S", date(2026, 10, 1)), tx("M", date(2026, 10, 1)),
                    tx("P", date(2026, 1, 1)), tx("P", date(2026, 10, 2), shares=0.0)]

    # Act
    purchases = insider_cluster.open_market_purchases(transactions, since=date(2026, 7, 1))

    # Assert
    assert [(p.on, p.value_usd) for p in purchases] == [(date(2026, 10, 1), 50.0)]


def test_two_insiders_within_ten_days_is_a_cluster():
    purchases = [purchase("A", date(2026, 9, 1), 1_000), purchase("B", date(2026, 9, 11), 2_000)]
    window = insider_cluster.best_purchase_window(purchases)
    assert insider_cluster.is_cluster(window)
    assert (window.distinct_insiders, window.total_usd) == (2, 3_000)


def test_two_insiders_eleven_days_apart_is_not_a_cluster():
    purchases = [purchase("A", date(2026, 9, 1), 1_000), purchase("B", date(2026, 9, 12), 2_000)]
    assert not insider_cluster.is_cluster(insider_cluster.best_purchase_window(purchases))


def test_one_insider_buying_twice_is_not_a_cluster():
    purchases = [purchase("A", date(2026, 9, 1), 1_000), purchase("A", date(2026, 9, 2), 1_000)]
    assert not insider_cluster.is_cluster(insider_cluster.best_purchase_window(purchases))
    assert insider_cluster.best_purchase_window([]) is None


def test_confidence_drops_with_parse_failures_and_truncation():
    assert insider_cluster.confidence_for(0, 0, False) == pytest.approx(0.9)
    assert insider_cluster.confidence_for(4, 3, False) == pytest.approx(0.675)
    assert insider_cluster.confidence_for(4, 4, True) == pytest.approx(0.72)


# ─── compute ─────────────────────────────────────────────────────────────────

async def test_compute_finds_the_director_and_cfo_cluster(edgar_from_fixtures, monkeypatch):
    # Arrange
    monkeypatch.setattr(get_settings(), "sec_user_agent", "PocketDesk ops@example.com")

    # Act
    response = await insider_cluster.compute_at({"ticker": "ACME"}, NOW)

    # Assert
    assert response["cluster_detected"] is True
    assert response["distinct_insiders"] == 2
    assert response["total_purchase_usd"] == pytest.approx(5000 * 41.20 + 10000 * 39.85 + 2000 * 40.10)
    assert response["cluster"]["start"] == "2026-09-30" and response["cluster"]["end"] == "2026-10-06"
    assert response["inputs_used"]["form4_in_lookback"] == 3, "the March Form 4 is outside 90 days"
    assert response["confidence"] == pytest.approx(0.9)
    assert all(headers["User-Agent"] == "PocketDesk ops@example.com" for headers in edgar_from_fixtures)
    for field in ("inputs_used", "computed_at", "method", "confidence", "data_delay"):
        assert field in response


async def test_a_short_lookback_sees_only_one_buyer(edgar_from_fixtures):
    response = await insider_cluster.compute_at({"ticker": "ACME", "days": "7"}, NOW)
    assert response["cluster_detected"] is False
    assert response["distinct_insiders"] == 1


async def test_unreadable_form4_is_reported_and_lowers_confidence(edgar_from_fixtures, monkeypatch):
    async def broken_form4(cik, filing):
        if filing.accession_number.endswith("000035"):
            raise edgar.EdgarError("Form 4 XML does not parse")
        return edgar.parse_form4_xml((FIXTURES / FORM4_BY_ACCESSION_FOLDER[filing.accession_number.replace("-", "")]).read_text())

    monkeypatch.setattr(edgar, "fetch_form4_transactions", broken_form4)
    response = await insider_cluster.compute_at({"ticker": "ACME"}, NOW)
    assert response["cluster_detected"] is True
    assert response["confidence"] == pytest.approx(0.6)
    assert response["inputs_used"]["errors"][0].startswith("0001999001-26-000035")


async def test_unknown_ticker_is_a_bad_request_error_dict(edgar_from_fixtures):
    response = await insider_cluster.compute_at({"ticker": "ZZZZ"}, NOW)
    assert response["error_kind"] == ERROR_BAD_REQUEST and response["confidence"] == 0.0


async def test_edgar_outage_returns_an_upstream_error_dict(monkeypatch):
    async def down(*args, **kwargs):
        raise UpstreamError("edgar", "failed after 3 attempts (HTTP 503)")

    monkeypatch.setattr(edgar, "get_json", down)
    response = await insider_cluster.compute_at({"ticker": "ACME"}, NOW)
    assert response["error_kind"] == ERROR_UPSTREAM


async def test_all_form4s_failing_returns_a_no_data_error_dict(edgar_from_fixtures, monkeypatch):
    async def always_broken(cik, filing):
        raise UpstreamError("edgar", "HTTP 503")

    monkeypatch.setattr(edgar, "fetch_form4_transactions", always_broken)
    response = await insider_cluster.compute_at({"ticker": "ACME"}, NOW)
    assert response["error_kind"] == ERROR_NO_DATA
