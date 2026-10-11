"""
FRED adapter: US macro time series as CSV, no API key needed.

Used by the `macro_pulse` seller (SPEC §4.6):

    GET https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL&cosd=2023-01-01
        observation_date,CPIAUCSL
        2026-07-01,331.2
        2026-08-01,331.9

Series we read:
    CPIAUCSL  CPI-U, all items, index 1982-84 = 100, monthly, seasonally adjusted
    FEDFUNDS  effective federal funds rate, percent, monthly average
    DGS2      2-year Treasury constant-maturity yield, percent, daily
    DGS10     10-year Treasury constant-maturity yield, percent, daily
    UNRATE    unemployment rate, percent, monthly

CSV quirks handled here: the date column is called `observation_date` (older files
said `DATE`), missing observations (e.g. bond-market holidays) are written as ".",
and an unknown series id returns an HTML page rather than an HTTP error — so we check
the header and raise `FredError` if it is not a two-column CSV for the series.

`cosd` (start date) keeps the download small; parsing does not depend on it.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

from app.data.http import get_text

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SOURCE_NAME = "fred"

SERIES_CPI = "CPIAUCSL"
SERIES_FED_FUNDS = "FEDFUNDS"
SERIES_TREASURY_2Y = "DGS2"
SERIES_TREASURY_10Y = "DGS10"
SERIES_UNEMPLOYMENT = "UNRATE"

MISSING_VALUE_MARKERS = {".", ""}
EXPECTED_COLUMN_COUNT = 2
FRED_CACHE_TTL_SECONDS = 300.0


class FredError(ValueError):
    """FRED returned something that is not the expected two-column CSV."""


@dataclass(frozen=True)
class Observation:
    """One dated value of a FRED series."""

    on: date
    value: float


def parse_fred_csv(text: str, series_id: str) -> list[Observation]:
    """
    Parse a fredgraph.csv download into observations, oldest first.

    Rows whose value is "." or empty (missing data) are skipped.
    Raises FredError if the header is not `<date column>,<series_id>`.
    """
    rows = list(csv.reader(io.StringIO(text.strip())))
    if not rows or len(rows[0]) != EXPECTED_COLUMN_COUNT or rows[0][1].strip() != series_id:
        header = rows[0] if rows else []
        raise FredError(f"unexpected FRED header for {series_id}: {header[:3]!r}")
    observations: list[Observation] = []
    for row in rows[1:]:
        if len(row) != EXPECTED_COLUMN_COUNT or row[1].strip() in MISSING_VALUE_MARKERS:
            continue
        observations.append(_parse_row(row, series_id))
    return sorted(observations, key=lambda observation: observation.on)


def _parse_row(row: list[str], series_id: str) -> Observation:
    """One `date,value` row → Observation; a malformed date or number raises FredError."""
    try:
        return Observation(on=date.fromisoformat(row[0].strip()), value=float(row[1]))
    except ValueError as exc:
        raise FredError(f"malformed {series_id} row {row!r}: {exc}") from exc


def latest(observations: list[Observation]) -> Observation:
    """The most recent observation (raises if the series is empty)."""
    if not observations:
        raise FredError("series has no observations")
    return observations[-1]


def _shift_month(day: date, months_back: int) -> tuple[int, int]:
    """(year, month) of the calendar month `months_back` months before `day`."""
    month_index = day.year * 12 + (day.month - 1) - months_back
    return month_index // 12, month_index % 12 + 1


def observation_months_before(
    observations: list[Observation], reference: date, months_back: int
) -> Observation | None:
    """
    The observation dated in the calendar month `months_back` months before
    `reference` (monthly series are dated the 1st of each month), or None.
    """
    year, month = _shift_month(reference, months_back)
    for observation in reversed(observations):
        if observation.on.year == year and observation.on.month == month:
            return observation
    return None


def previous_observation(observations: list[Observation]) -> Observation | None:
    """The observation just before the latest one, or None if there is only one."""
    if len(observations) < 2:
        return None
    return observations[-2]


async def fetch_series(series_id: str, start: date) -> list[Observation]:
    """Download `series_id` from `start` onward and parse it."""
    text = await get_text(
        FRED_CSV_URL,
        source=SOURCE_NAME,
        params={"id": series_id, "cosd": start.isoformat()},
        ttl=FRED_CACHE_TTL_SECONDS,
    )
    return parse_fred_csv(text, series_id)
