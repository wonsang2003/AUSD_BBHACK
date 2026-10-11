"""
Seller `macro_pulse` (demo vendor, 0.02 AUSD): where is US macro right now, and what is next?

`GET /sell/macro-pulse` (SPEC §4.6) — no parameters; the read is the same for every asset.

Data (all key-less):
  * FRED CSV (`app/data/fred.py`): CPIAUCSL, FEDFUNDS, DGS2, DGS10, UNRATE;
  * the static official 2026 FOMC / CPI schedule in `app/keeper/data/macro_calendar.json`;
  * Gemini Predictions "economics" events (`app/data/gemini.py`) — market odds, when listed.

What we compute:
    CPI YoY %          = (CPI_m / CPI_(m−12) − 1) · 100          for the latest month m
    cpi_yoy_change_pts = YoY_m − YoY_(m−1)                        (percentage points)
    fed_funds_change_pts = FEDFUNDS_m − FEDFUNDS_(m−1)            (percentage points)
    unemployment change  = UNRATE_m − UNRATE_(m−1)
    2s10s slope        = DGS10 − DGS2 on the latest date both are published
                         (in percentage points and basis points; < 0 = inverted curve)
    next releases      = the first FOMC decision / CPI release dated today or later

Agents read `cpi_yoy_change_pts` and `fed_funds_change_pts` — see
`app/agents/beliefs.update_from_macro_pulse`. A series that fails to download is
reported in `inputs_used.errors` and lowers `confidence`; only when every series fails
does the call fail (so the buyer is not charged for nothing).

`load_macro_calendar`, `next_fomc` and `next_cpi` are pure and reusable by the keeper's
event calendar. This module never imports the x402 server; `app/sellers/routers.py`
wraps `compute`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import get_settings
from app.data import fred, gemini
from app.data.fred import FredError, Observation
from app.data.http import UpstreamError
from app.sellers.expected_move import (
    ERROR_UPSTREAM,
    error_response,
    iso_utc,
    require_payout_address,
    utc_now,
)
from app.x402.offer import SellerOffer

# ─── offer ───────────────────────────────────────────────────────────────────

SELLER_ID = "macro_pulse"
OFFER_PATH = "/sell/macro-pulse"
PRICE_ATOMIC = 20_000  # 0.02 AUSD (6 decimals)
PAYOUT_SETTING_NAME = "SELLER_PAYTO_MACRO"   # .env variable holding this seller's payout address
OFFER_DESCRIPTION = (
    "Macro pulse (demo vendor): FRED CPI YoY, fed funds, 2s10s slope, unemployment "
    "+ next FOMC/CPI dates and Gemini economics odds"
)

# ─── inputs ──────────────────────────────────────────────────────────────────

MACRO_CALENDAR_PATH = Path(__file__).resolve().parents[1] / "keeper" / "data" / "macro_calendar.json"

ALL_SERIES = (
    fred.SERIES_CPI,
    fred.SERIES_FED_FUNDS,
    fred.SERIES_TREASURY_2Y,
    fred.SERIES_TREASURY_10Y,
    fred.SERIES_UNEMPLOYMENT,
)
# YoY of the latest AND the previous month needs 14 monthly points; CPI is published
# ~2 weeks after month end, so 30 months of history is comfortably enough.
CPI_LOOKBACK_DAYS = 30 * 31
OTHER_SERIES_LOOKBACK_DAYS = 200   # ≥ 2 monthly points for FEDFUNDS / UNRATE, many daily yields

MONTHS_PER_YEAR = 12
PERCENT = 100.0
BASIS_POINTS_PER_PERCENTAGE_POINT = 100.0

GEMINI_ECONOMICS_CATEGORY = "economics"
MAX_PREDICTION_EVENTS = 5
MAX_CONTRACTS_PER_EVENT = 4

COMPLETE_DATA_CONFIDENCE = 0.9   # official statistics; capped below 1 for revisions and lags
DATA_DELAY = (
    "FRED: monthly CPI/FEDFUNDS/UNRATE are published ~1–6 weeks after the month; "
    "daily Treasury yields lag 1 business day"
)


def make_offer() -> SellerOffer:
    """What this demo vendor charges (0.02 AUSD) and where the AUSD goes (SELLER_PAYTO_MACRO).
    Raises SellerConfigError if that address is empty or invalid."""
    return SellerOffer(
        seller_id=SELLER_ID,
        path=OFFER_PATH,
        description=OFFER_DESCRIPTION,
        price_atomic=PRICE_ATOMIC,
        pay_to=require_payout_address(get_settings().seller_payto_macro, PAYOUT_SETTING_NAME),
    )


# ─── macro calendar (static official schedule) ──────────────────────────────

@dataclass(frozen=True)
class FomcMeeting:
    """A two-day FOMC meeting; the policy decision is announced on `decision_date`."""

    start_date: date
    decision_date: date
    has_sep: bool              # Summary of Economic Projections ("dot plot") released


@dataclass(frozen=True)
class CpiRelease:
    """One scheduled BLS CPI release."""

    reference_month: str       # "2026-09" = the month the data describes
    release_date: date
    release_time_et: str       # "08:30"


@dataclass(frozen=True)
class MacroCalendar:
    """The parsed `macro_calendar.json`."""

    fomc: list[FomcMeeting]
    cpi: list[CpiRelease]
    source: dict[str, str]
    verified_on: str


def parse_macro_calendar(data: dict) -> MacroCalendar:
    """Turn the calendar JSON into dated rows, each list sorted by date."""
    meetings = [
        FomcMeeting(
            start_date=date.fromisoformat(row["start_date"]),
            decision_date=date.fromisoformat(row["decision_date"]),
            has_sep=bool(row["has_sep"]),
        )
        for row in data["fomc"]
    ]
    releases = [
        CpiRelease(
            reference_month=row["reference_month"],
            release_date=date.fromisoformat(row["release_date"]),
            release_time_et=row["release_time_et"],
        )
        for row in data["cpi"]
    ]
    return MacroCalendar(
        fomc=sorted(meetings, key=lambda meeting: meeting.decision_date),
        cpi=sorted(releases, key=lambda release: release.release_date),
        source=dict(data.get("source", {})),
        verified_on=str(data.get("verified_on", "")),
    )


def load_macro_calendar(path: Path = MACRO_CALENDAR_PATH) -> MacroCalendar:
    """Read and parse the static calendar file (raises if it is missing or malformed)."""
    return parse_macro_calendar(json.loads(path.read_text(encoding="utf-8")))


def next_fomc(calendar: MacroCalendar, today: date) -> FomcMeeting | None:
    """The first FOMC meeting whose decision date is today or later; None after the last one."""
    for meeting in calendar.fomc:
        if meeting.decision_date >= today:
            return meeting
    return None


def next_cpi(calendar: MacroCalendar, today: date) -> CpiRelease | None:
    """The first CPI release dated today or later; None after the last one."""
    for release in calendar.cpi:
        if release.release_date >= today:
            return release
    return None


def _days_until(day: date, today: date) -> int:
    """Calendar days from `today` to `day` (0 = today)."""
    return (day - today).days


def next_releases_json(calendar: MacroCalendar, today: date) -> dict:
    """{"fomc": {...} | None, "cpi": {...} | None} with ISO dates and days_until."""
    meeting = next_fomc(calendar, today)
    release = next_cpi(calendar, today)
    return {
        "fomc": {
            "start_date": meeting.start_date.isoformat(),
            "decision_date": meeting.decision_date.isoformat(),
            "has_sep": meeting.has_sep,
            "days_until": _days_until(meeting.decision_date, today),
        } if meeting else None,
        "cpi": {
            "reference_month": release.reference_month,
            "release_date": release.release_date.isoformat(),
            "release_time_et": release.release_time_et,
            "days_until": _days_until(release.release_date, today),
        } if release else None,
    }


# ─── pure macro math ─────────────────────────────────────────────────────────

def yoy_pct(observations: list[Observation], index: int) -> float | None:
    """
    Year-over-year change in percent of the monthly observation at `index`:
        YoY = (value_m / value_(m−12) − 1) · 100
    None when the month a year earlier is not in the data (or is not positive).
    """
    if not -len(observations) <= index < len(observations):
        return None
    current = observations[index]
    year_ago = fred.observation_months_before(observations, current.on, MONTHS_PER_YEAR)
    if year_ago is None or year_ago.value <= 0:
        return None
    return (current.value / year_ago.value - 1.0) * PERCENT


@dataclass(frozen=True)
class CpiReading:
    """Latest CPI level and its year-over-year inflation, now and one month earlier."""

    latest_month: str          # "2026-08"
    index_level: float
    yoy_pct: float | None
    prev_yoy_pct: float | None
    yoy_change_pts: float | None   # yoy_pct − prev_yoy_pct


def cpi_reading(observations: list[Observation]) -> CpiReading | None:
    """CPI YoY for the latest month and the month before, and their difference."""
    if not observations:
        return None
    latest = observations[-1]
    current = yoy_pct(observations, -1)
    previous = yoy_pct(observations, -2)
    change = current - previous if current is not None and previous is not None else None
    return CpiReading(
        latest_month=latest.on.strftime("%Y-%m"),
        index_level=latest.value,
        yoy_pct=current,
        prev_yoy_pct=previous,
        yoy_change_pts=change,
    )


@dataclass(frozen=True)
class MonthlyRate:
    """A percent-valued monthly series (fed funds, unemployment): latest, previous, change."""

    latest_month: str
    value_pct: float
    prev_value_pct: float | None
    change_pts: float | None       # value_pct − prev_value_pct


def monthly_rate(observations: list[Observation]) -> MonthlyRate | None:
    """Latest value and change versus the previous observation, in percentage points."""
    if not observations:
        return None
    latest = observations[-1]
    previous = fred.previous_observation(observations)
    return MonthlyRate(
        latest_month=latest.on.strftime("%Y-%m"),
        value_pct=latest.value,
        prev_value_pct=previous.value if previous else None,
        change_pts=latest.value - previous.value if previous else None,
    )


@dataclass(frozen=True)
class CurveReading:
    """2-year vs 10-year Treasury yields on one day."""

    on: str
    dgs2_pct: float
    dgs10_pct: float
    slope_2s10s_pts: float         # DGS10 − DGS2
    slope_2s10s_bp: float          # same, in basis points
    inverted: bool                 # slope < 0


def curve_reading(two_year: list[Observation], ten_year: list[Observation]) -> CurveReading | None:
    """2s10s slope = DGS10 − DGS2 on the latest date present in BOTH series (they can differ by a day)."""
    two_year_by_date = {observation.on: observation.value for observation in two_year}
    for observation in reversed(ten_year):
        if observation.on in two_year_by_date:
            two = two_year_by_date[observation.on]
            slope = observation.value - two
            return CurveReading(
                on=observation.on.isoformat(),
                dgs2_pct=two,
                dgs10_pct=observation.value,
                slope_2s10s_pts=slope,
                slope_2s10s_bp=slope * BASIS_POINTS_PER_PERCENTAGE_POINT,
                inverted=slope < 0,
            )
    return None


def confidence_for(series_loaded: int, series_total: int = len(ALL_SERIES)) -> float:
    """0.9 · (series loaded / series requested); 0 when nothing loaded."""
    if series_total == 0:
        return 0.0
    return COMPLETE_DATA_CONFIDENCE * series_loaded / series_total


# ─── prediction-market odds (optional) ───────────────────────────────────────

def _contract_odds(contract: gemini.PredictionContract) -> dict | None:
    """{label, market_p, basis} for a tradable contract with a price; None otherwise."""
    if not gemini.is_tradable(contract):
        return None
    try:
        quote = gemini.yes_quote(contract)
    except gemini.UnsupportedContract:
        return None
    return {"label": contract.label, "market_p": float(quote.probability), "basis": quote.basis}


def summarize_prediction_events(events: list[dict]) -> list[dict]:
    """
    The first few economics events with priced, tradable contracts:
    [{event_ticker, title, expiry, contracts: [{label, market_p, basis}]}].
    market_p is the YES probability the market implies (see gemini.yes_quote).
    """
    summaries: list[dict] = []
    for event in events:
        contracts = gemini.parse_contracts(event)
        odds = [row for row in (_contract_odds(contract) for contract in contracts) if row is not None]
        if not odds:
            continue
        expiry = contracts[0].expiry
        summaries.append(
            {
                "event_ticker": event.get("ticker", ""),
                "title": event.get("title", ""),
                "expiry": expiry.isoformat() if expiry else None,
                "contracts": odds[:MAX_CONTRACTS_PER_EVENT],
            }
        )
        if len(summaries) == MAX_PREDICTION_EVENTS:
            break
    return summaries


# ─── response ────────────────────────────────────────────────────────────────

def _series_inputs(series: dict[str, list[Observation]]) -> dict:
    """Per series: how many observations were used and the latest date."""
    return {
        series_id: {
            "observations": len(observations),
            "latest": observations[-1].on.isoformat() if observations else None,
        }
        for series_id, observations in series.items()
    }


def build_response(
    series: dict[str, list[Observation]],
    calendar: MacroCalendar | None,
    prediction_markets: list[dict],
    errors: dict[str, str],
    now: datetime,
) -> dict:
    """Assemble the JSON body from already-fetched data (pure, so tests can use fixtures)."""
    cpi = cpi_reading(series.get(fred.SERIES_CPI, []))
    fed_funds = monthly_rate(series.get(fred.SERIES_FED_FUNDS, []))
    unemployment = monthly_rate(series.get(fred.SERIES_UNEMPLOYMENT, []))
    curve = curve_reading(series.get(fred.SERIES_TREASURY_2Y, []), series.get(fred.SERIES_TREASURY_10Y, []))
    loaded = sum(1 for observations in series.values() if observations)
    return {
        "seller_id": SELLER_ID,
        "cpi": asdict(cpi) if cpi else None,
        "fed_funds": asdict(fed_funds) if fed_funds else None,
        "unemployment": asdict(unemployment) if unemployment else None,
        "curve": asdict(curve) if curve else None,
        "cpi_yoy_change_pts": cpi.yoy_change_pts if cpi else None,
        "fed_funds_change_pts": fed_funds.change_pts if fed_funds else None,
        "next_releases": next_releases_json(calendar, now.date()) if calendar else {"fomc": None, "cpi": None},
        "prediction_markets": prediction_markets,
        "method": (
            "CPI YoY = CPI_m / CPI_(m−12) − 1; cpi_yoy_change_pts = YoY_m − YoY_(m−1); fed funds and "
            "unemployment change = latest − previous month; 2s10s = DGS10 − DGS2 on the latest common date; "
            "next FOMC/CPI from the static official 2026 schedule; odds = Gemini economics YES mid prices"
        ),
        "inputs_used": {
            "source": "FRED fredgraph.csv (no key)",
            "series": _series_inputs(series),
            "calendar_source": calendar.source if calendar else None,
            "calendar_verified_on": calendar.verified_on if calendar else None,
            "prediction_market_category": GEMINI_ECONOMICS_CATEGORY,
            "errors": errors,
        },
        "computed_at": iso_utc(now),
        "confidence": round(confidence_for(loaded), 2),
        "data_delay": DATA_DELAY,
    }


# ─── network orchestration ───────────────────────────────────────────────────

def lookback_start(series_id: str, today: date) -> date:
    """First date to download: ~30 months for CPI (YoY needs a year of history), ~200 days otherwise."""
    days = CPI_LOOKBACK_DAYS if series_id == fred.SERIES_CPI else OTHER_SERIES_LOOKBACK_DAYS
    return today - timedelta(days=days)


async def fetch_all_series(today: date) -> tuple[dict[str, list[Observation]], dict[str, str]]:
    """
    Download every series concurrently (the shared helper spaces FRED requests).
    Returns (series id → observations, series id → error) — a failed series maps to [].
    """
    results = await asyncio.gather(
        *(fred.fetch_series(series_id, lookback_start(series_id, today)) for series_id in ALL_SERIES),
        return_exceptions=True,
    )
    series: dict[str, list[Observation]] = {}
    errors: dict[str, str] = {}
    for series_id, result in zip(ALL_SERIES, results):
        if isinstance(result, (UpstreamError, FredError)):
            series[series_id] = []
            errors[series_id] = str(result)
        elif isinstance(result, BaseException):
            raise result  # a bug, not an upstream problem: do not hide it
        else:
            series[series_id] = result
    return series, errors


async def fetch_prediction_markets(errors: dict[str, str]) -> list[dict]:
    """Gemini economics odds; any failure is recorded under "gemini" and gives []."""
    try:
        payload = await gemini.fetch_events_payload(GEMINI_ECONOMICS_CATEGORY)
        return summarize_prediction_events(gemini.parse_events(payload))
    except (UpstreamError, ValueError) as exc:
        errors["gemini"] = str(exc)
        return []


def load_calendar_or_none(errors: dict[str, str], path: Path = MACRO_CALENDAR_PATH) -> MacroCalendar | None:
    """The static calendar, or None (with the reason under errors["macro_calendar"]) if it cannot be read."""
    try:
        return load_macro_calendar(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors["macro_calendar"] = f"{type(exc).__name__}: {exc}"
        return None


async def compute(params: dict) -> dict:
    """Answer one paid request now (the x402 server calls this); see `compute_at`."""
    return await compute_at(params, utc_now())


async def compute_at(params: dict, now: datetime) -> dict:
    """
    Fetch FRED + Gemini odds, read the static calendar, and build the macro read.
    `params` is accepted for the common seller signature; this seller takes none.

    Never raises for upstream failure: only when every FRED series failed does it return
    {"error", "error_kind", ...} with confidence 0 (the buyer is not charged).
    """
    series, errors = await fetch_all_series(now.date())
    if not any(series.values()):
        message = f"no FRED series could be loaded: {errors}"
        return error_response(params, message, ERROR_UPSTREAM, now, DATA_DELAY)
    prediction_markets = await fetch_prediction_markets(errors)
    calendar = load_calendar_or_none(errors)
    return build_response(series, calendar, prediction_markets, errors, now)
