"""
Tests for `app/data/edgar.py` — SEC EDGAR ticker → CIK, recent Form 4 filings, Form 4 XML.

Fixtures (hand-built OFFLINE in EDGAR's exact formats; NOT live recordings, no network here):
    edgar_company_tickers.json      company_tickers.json shape
    edgar_submissions_acme.json     submissions JSON with parallel `filings.recent` arrays
    edgar_form4_acme_0000NN.xml     raw `<ownershipDocument>` Form 4 XML
"Acme Robotics Inc." (ticker ACME) and its insiders are fictitious test data.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.config import get_settings
from app.data import edgar
from app.data.edgar import EdgarError, Filing

FIXTURES = Path(__file__).parent / "fixtures"
ACME_CIK = 1999001


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_company_tickers_map_upper_case_tickers_to_integer_ciks():
    tickers = edgar.parse_company_tickers(load_json("edgar_company_tickers.json"))
    assert tickers["NVDA"] == 1045810
    assert tickers["ACME"] == ACME_CIK


def test_malformed_company_tickers_raise_edgar_error():
    with pytest.raises(EdgarError):
        edgar.parse_company_tickers({"0": {"ticker": "X"}})


def test_recent_filings_keep_only_plain_form_4_newest_first():
    # Act
    filings = edgar.parse_recent_filings(load_json("edgar_submissions_acme.json"))

    # Assert
    assert [filing.accession_number for filing in filings] == [
        "0001999001-26-000041", "0001999001-26-000039", "0001999001-26-000035", "0001999001-26-000012",
    ], "the 8-K, 10-Q and 4/A rows are dropped"
    assert filings[0].filing_date == date(2026, 10, 8)


def test_submissions_without_recent_filings_raise_edgar_error():
    with pytest.raises(EdgarError, match="missing"):
        edgar.parse_recent_filings({"filings": {}})


def test_form4_xml_url_drops_the_xsl_folder_and_accession_dashes():
    filing = Filing("0001999001-26-000041", date(2026, 10, 8), "4", "xslF345X05/form4.xml")
    assert edgar.form4_xml_url(ACME_CIK, filing) == (
        "https://www.sec.gov/Archives/edgar/data/1999001/000199900126000041/form4.xml"
    )


def test_form4_purchase_is_parsed_with_owner_role_and_value():
    # Act
    [purchase] = edgar.parse_form4_xml(fixture_text("edgar_form4_acme_000041.xml"), "0001999001-26-000041")

    # Assert
    assert purchase.owner_name == "RIVERA MARTA"
    assert purchase.is_officer and purchase.officer_title == "Chief Financial Officer"
    assert (purchase.code, purchase.shares, purchase.price_per_share) == ("P", 5000.0, 41.20)
    assert purchase.value_usd == pytest.approx(206_000.0)
    assert purchase.transaction_date == date(2026, 10, 6)


def test_joint_filing_is_attributed_to_the_first_owner_and_timezone_suffix_is_dropped():
    transactions = edgar.parse_form4_xml(fixture_text("edgar_form4_acme_000039.xml"))
    assert {transaction.owner_name for transaction in transactions} == {"OKAFOR DANIEL"}
    assert [transaction.transaction_date for transaction in transactions] == [
        date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 1),
    ]
    assert [transaction.code for transaction in transactions] == ["P", "P", "M"]


def test_price_given_only_in_a_footnote_counts_as_zero():
    xml = fixture_text("edgar_form4_acme_000041.xml").replace("<value>41.20</value>", '<footnoteId id="F1"/>')
    assert edgar.parse_form4_xml(xml)[0].price_per_share == 0.0


@pytest.mark.parametrize("xml", ["<ownershipDocument><broken>", "<ownershipDocument/>"])
def test_unparseable_or_ownerless_form4_raises_edgar_error(xml):
    with pytest.raises(EdgarError):
        edgar.parse_form4_xml(xml)


def test_malformed_transaction_number_raises_edgar_error():
    xml = fixture_text("edgar_form4_acme_000041.xml").replace("<value>5000</value>", "<value>five</value>")
    with pytest.raises(EdgarError, match="malformed"):
        edgar.parse_form4_xml(xml)


async def test_every_edgar_request_carries_the_configured_user_agent(monkeypatch):
    # Arrange
    monkeypatch.setattr(get_settings(), "sec_user_agent", "PocketDesk test@example.com")
    seen_headers: list[dict] = []

    async def fake_get_json(url, *, source, params=None, headers=None, ttl=60.0):
        seen_headers.append(headers)
        if url.endswith("company_tickers.json"):
            return load_json("edgar_company_tickers.json")
        assert url == "https://data.sec.gov/submissions/CIK0001999001.json"
        return load_json("edgar_submissions_acme.json")

    monkeypatch.setattr(edgar, "get_json", fake_get_json)

    # Act
    cik = await edgar.fetch_cik_for_ticker("acme")
    filings = await edgar.fetch_recent_form4_filings(cik)

    # Assert
    assert cik == ACME_CIK and len(filings) == 4
    assert all(headers["User-Agent"] == "PocketDesk test@example.com" for headers in seen_headers)


async def test_unknown_ticker_raises_edgar_error(monkeypatch):
    async def fake_get_json(url, **kwargs):
        return load_json("edgar_company_tickers.json")

    monkeypatch.setattr(edgar, "get_json", fake_get_json)
    with pytest.raises(EdgarError, match="not found"):
        await edgar.fetch_cik_for_ticker("ZZZZ")
