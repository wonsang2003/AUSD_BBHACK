"""
Tests for `app/sellers/macro_pulse.py` — the macro_pulse demo vendor (0.02 AUSD): FRED CPI YoY,
fed funds, 2s10s, unemployment + next FOMC/CPI dates + Gemini economics odds.

Fixtures: `tests/fixtures/fred_*.csv` and `gemini_events_economics.json` are hand-built OFFLINE
in the upstream formats (plausible values, NOT official data; no network here). The calendar test
reads the real `app/keeper/data/macro_calendar.json`.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.data import fred, gemini
from app.data.http import UpstreamError
from app.sellers import macro_pulse
from app.sellers.expected_move import ERROR_UPSTREAM, SellerConfigError

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
CSV_BY_SERIES = {"CPIAUCSL": "fred_cpiaucsl.csv", "FEDFUNDS": "fred_fedfunds.csv", "DGS2": "fred_dgs2.csv",
                 "DGS10": "fred_dgs10.csv", "UNRATE": "fred_unrate.csv"}


def series(series_id: str) -> list[fred.Observation]:
    return fred.parse_fred_csv((FIXTURES / CSV_BY_SERIES[series_id]).read_text(), series_id)


@pytest.fixture
def upstreams_from_fixtures(monkeypatch):
    async def fetch_series(series_id: str, start: date):
        return series(series_id)

    async def events(category: str):
        return json.loads((FIXTURES / "gemini_events_economics.json").read_text())

    monkeypatch.setattr(fred, "fetch_series", fetch_series)
    monkeypatch.setattr(gemini, "fetch_events_payload", events)


def test_offer_costs_two_cents_and_requires_a_payout_address(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_macro", "0x" + "6a" * 20)
    offer = macro_pulse.make_offer()
    assert (offer.price_atomic, offer.path) == (20_000, "/sell/macro-pulse")
    assert "demo vendor" in offer.description.lower()
    monkeypatch.setattr(get_settings(), "seller_payto_macro", "")
    with pytest.raises(SellerConfigError, match="SELLER_PAYTO_MACRO"):
        macro_pulse.make_offer()


def test_cpi_yoy_uses_the_same_month_a_year_earlier():
    observations = series("CPIAUCSL")
    reading = macro_pulse.cpi_reading(observations)
    by_month = {o.on: o.value for o in observations}
    expected = (by_month[date(2026, 8, 1)] / by_month[date(2025, 8, 1)] - 1) * 100
    assert reading.latest_month == "2026-08"
    assert reading.yoy_pct == pytest.approx(expected)
    assert reading.yoy_change_pts == pytest.approx(reading.yoy_pct - reading.prev_yoy_pct)


def test_curve_uses_the_latest_date_both_yields_exist():
    curve = macro_pulse.curve_reading(series("DGS2"), series("DGS10"))
    assert curve.on == "2026-10-08", "DGS10 for 9 Oct is not posted yet"
    assert curve.slope_2s10s_bp == pytest.approx((curve.dgs10_pct - curve.dgs2_pct) * 100)


def test_next_releases_come_from_the_official_calendar():
    calendar = macro_pulse.load_macro_calendar()
    releases = macro_pulse.next_releases_json(calendar, date(2026, 10, 11))
    assert releases["fomc"]["decision_date"] == "2026-10-28"
    assert releases["cpi"]["release_date"] == "2026-10-14" and releases["cpi"]["days_until"] == 3


async def test_compute_builds_the_full_read(upstreams_from_fixtures):
    response = await macro_pulse.compute_at({}, NOW)
    assert response["fed_funds_change_pts"] == pytest.approx(3.58 - 3.64)
    assert response["prediction_markets"][0]["contracts"][0] == {"label": "Cut 25 bps", "market_p": 0.635,
                                                                "basis": "mid"}
    assert response["confidence"] == pytest.approx(0.9)
    for field in ("inputs_used", "computed_at", "method", "confidence", "data_delay"):
        assert field in response


async def test_partial_fred_failure_lowers_confidence(upstreams_from_fixtures, monkeypatch):
    async def flaky(series_id: str, start: date):
        if series_id == "UNRATE":
            raise UpstreamError("fred", "HTTP 503")
        return series(series_id)

    monkeypatch.setattr(fred, "fetch_series", flaky)
    response = await macro_pulse.compute_at({}, NOW)
    assert response["unemployment"] is None and "UNRATE" in response["inputs_used"]["errors"]
    assert response["confidence"] == pytest.approx(0.72)


async def test_all_fred_series_failing_returns_an_error_dict(upstreams_from_fixtures, monkeypatch):
    async def down(series_id: str, start: date):
        raise UpstreamError("fred", "HTTP 503")

    monkeypatch.setattr(fred, "fetch_series", down)
    response = await macro_pulse.compute_at({}, NOW)
    assert response["error_kind"] == ERROR_UPSTREAM and response["confidence"] == 0.0
