"""
Tests for `app/data/fred.py` — key-less FRED `fredgraph.csv` downloads for `macro_pulse`.

Fixtures: `tests/fixtures/fred_*.csv` are REALISTIC-SHAPE CSVs hand-built OFFLINE in FRED's
current format (`observation_date,<SERIES>` header, "." for missing days such as Labor Day);
the values are plausible but NOT official data (no network from this container).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.data import fred
from app.data.fred import FredError, Observation

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_cpi_csv_parses_to_monthly_observations_oldest_first():
    # Act
    observations = fred.parse_fred_csv(fixture_text("fred_cpiaucsl.csv"), "CPIAUCSL")

    # Assert
    assert len(observations) == 32
    assert observations[0] == Observation(date(2024, 1, 1), 308.417)
    assert fred.latest(observations).on == date(2026, 8, 1)


def test_missing_values_written_as_a_dot_are_skipped():
    observations = fred.parse_fred_csv(fixture_text("fred_dgs2.csv"), "DGS2")
    assert date(2026, 9, 7) not in {observation.on for observation in observations}, "Labor Day"


def test_legacy_date_header_is_accepted():
    observations = fred.parse_fred_csv("DATE,UNRATE\n2026-09-01,4.6\n", "UNRATE")
    assert observations == [Observation(date(2026, 9, 1), 4.6)]


def test_html_page_for_an_unknown_series_raises_fred_error():
    with pytest.raises(FredError, match="unexpected FRED header"):
        fred.parse_fred_csv("<!DOCTYPE html><html><body>Series not found</body></html>", "NOPE")


def test_malformed_row_raises_fred_error():
    with pytest.raises(FredError, match="malformed"):
        fred.parse_fred_csv("observation_date,DGS2\n2026-13-01,3.4\n", "DGS2")


def test_observation_a_year_earlier_is_found_by_calendar_month():
    observations = fred.parse_fred_csv(fixture_text("fred_cpiaucsl.csv"), "CPIAUCSL")
    year_ago = fred.observation_months_before(observations, date(2026, 8, 1), 12)
    assert year_ago is not None and year_ago.on == date(2025, 8, 1)
    assert fred.observation_months_before(observations, date(2024, 1, 1), 1) is None


def test_previous_observation_and_empty_latest():
    observations = fred.parse_fred_csv(fixture_text("fred_fedfunds.csv"), "FEDFUNDS")
    assert fred.previous_observation(observations) == Observation(date(2026, 8, 1), 3.64)
    assert fred.previous_observation(observations[:1]) is None
    with pytest.raises(FredError):
        fred.latest([])


async def test_fetch_series_asks_for_the_series_from_a_start_date(monkeypatch):
    # Arrange
    sent: dict = {}

    async def fake_get_text(url, *, source, params=None, headers=None, ttl=300.0):
        sent.update(url=url, params=params, source=source)
        return fixture_text("fred_unrate.csv")

    monkeypatch.setattr(fred, "get_text", fake_get_text)

    # Act
    observations = await fred.fetch_series("UNRATE", date(2026, 3, 1))

    # Assert
    assert sent == {"url": "https://fred.stlouisfed.org/graph/fredgraph.csv",
                    "params": {"id": "UNRATE", "cosd": "2026-03-01"}, "source": "fred"}
    assert observations[-1] == Observation(date(2026, 9, 1), 4.6)
