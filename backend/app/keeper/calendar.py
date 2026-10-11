"""
Event calendar: what happens TODAY that makes some agent's research more valuable (SPEC §6).

Sources (each degrades to "no events" on failure, never to an error):

    earnings    watchlist equities, from yfinance `Ticker.calendar["Earnings Date"]`
                (unofficial, cached 6 h; one date = confirmed, two dates = estimated window)
    fomc / cpi  `keeper/data/macro_calendar.json` (official 2026 Fed / BLS schedules, New York dates)
    pm_expiry   Gemini prediction-market contract expiries for watchlist assets (INJECTED by the
                caller from app/data/gemini.py, so this module makes no Gemini requests)
    crypto      Deribit monthly options expiry for BTC/ETH: the last Friday of the month,
                08:00 UTC (quarterly in March, June, September, December)

Each event names the agent it boosts:

    earnings, pm_expiry, crypto → OptionsEvent      fomc, cpi → MacroFilings

Outputs:
    events_today(...)     → [CalendarEvent]; `to_api()` is one row of GET /api/events-today
    events_per_agent(...) → {agent: ["NVDA earnings today", ...]}, the `events_today` argument
                            of `keeper.allocator.allocate` (w_k = θ_k · (1 + β · events_k))

"Today" is a calendar date in the user's session time zone (default America/New_York, the
time zone of the macro schedule and of US earnings dates); timestamps are converted to it.
"""

from __future__ import annotations

import asyncio
import calendar as month_calendar
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.agents.planner import ASSET_CLASS_EQUITY, asset_class

logger = logging.getLogger(__name__)

# ─── kinds and the agent each one boosts ─────────────────────────────────────

KIND_EARNINGS = "earnings"
KIND_FOMC = "fomc"
KIND_CPI = "cpi"
KIND_PM_EXPIRY = "pm_expiry"
KIND_CRYPTO = "crypto"

BOOSTED_AGENT_BY_KIND: dict[str, str] = {
    KIND_EARNINGS: "OptionsEvent",   # expected_move prices the earnings move
    KIND_PM_EXPIRY: "OptionsEvent",  # pm_edge compares expiring contracts with options
    KIND_CRYPTO: "OptionsEvent",     # big options expiry → expected move matters
    KIND_FOMC: "MacroFilings",       # macro_pulse
    KIND_CPI: "MacroFilings",
}

# ─── sources ─────────────────────────────────────────────────────────────────

MACRO_CALENDAR_PATH = Path(__file__).resolve().parent / "data" / "macro_calendar.json"
DEFAULT_TIMEZONE = ZoneInfo("America/New_York")

EARNINGS_CACHE_TTL_SECONDS = 6 * 60 * 60     # earnings dates change rarely
EARNINGS_FAILURE_TTL_SECONDS = 10 * 60       # retry a failed ticker after 10 minutes
EARNINGS_TIMEOUT_SECONDS = 20.0
YFINANCE_EARNINGS_KEY = "Earnings Date"
ESTIMATED_WINDOW_DATES = 2                   # yfinance gives [start, end] when not confirmed

DERIBIT_MONTHLY_ASSETS = ("BTC", "ETH")
DERIBIT_EXPIRY_TIME_UTC = "08:00 UTC"
QUARTERLY_MONTHS = (3, 6, 9, 12)
FRIDAY = 4                                   # date.weekday(): Monday = 0

DATE_FORMAT = "%Y-%m-%d"


# ─── data types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CalendarEvent:
    """One event today; `reason` is the short text the allocator shows ("NVDA earnings today")."""

    date: date
    kind: str
    title: str
    asset: str | None
    boosted_agent: str | None
    reason: str

    def to_api(self) -> dict[str, Any]:
        """docs/API.md GET /api/events-today row."""
        return {
            "date": self.date.strftime(DATE_FORMAT),
            "kind": self.kind,
            "title": self.title,
            "asset": self.asset,
            "boosted_agent": self.boosted_agent,
        }


@dataclass(frozen=True)
class ContractExpiry:
    """A Gemini prediction-market contract expiry (built by the caller from app/data/gemini.py)."""

    asset: str
    expires_at: datetime          # timezone-aware
    title: str = ""


@dataclass(frozen=True)
class MacroCalendar:
    """The official FOMC decision days and CPI release days from macro_calendar.json."""

    fomc_decisions: Mapping[date, bool] = field(default_factory=dict)          # date → has projections (SEP)
    cpi_releases: Mapping[date, tuple[str, str]] = field(default_factory=dict)  # date → (reference month, time ET)


def _event(day: date, kind: str, title: str, asset: str | None, reason: str) -> CalendarEvent:
    """Build an event with the boosted agent looked up from its kind."""
    return CalendarEvent(day, kind, title, asset, BOOSTED_AGENT_BY_KIND.get(kind), reason)


# ─── macro calendar (FOMC / CPI) ─────────────────────────────────────────────

def _parse_day(value: Any) -> date | None:
    """A date, datetime or "YYYY-MM-DD" string → date; anything else → None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], DATE_FORMAT).date()
    except ValueError:
        return None


def parse_macro_calendar(payload: Mapping[str, Any]) -> MacroCalendar:
    """Read the `fomc` and `cpi` lists of macro_calendar.json; malformed entries are skipped."""
    fomc: dict[date, bool] = {}
    for meeting in payload.get("fomc") or []:
        decision_day = _parse_day(meeting.get("decision_date"))
        if decision_day is not None:
            fomc[decision_day] = bool(meeting.get("has_sep", False))
    cpi: dict[date, tuple[str, str]] = {}
    for release in payload.get("cpi") or []:
        release_day = _parse_day(release.get("release_date"))
        if release_day is not None:
            cpi[release_day] = (str(release.get("reference_month", "")), str(release.get("release_time_et", "")))
    return MacroCalendar(fomc_decisions=fomc, cpi_releases=cpi)


def load_macro_calendar(path: Path = MACRO_CALENDAR_PATH) -> MacroCalendar:
    """Load macro_calendar.json; a missing or unreadable file gives an empty calendar (logged)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("macro calendar unavailable (%s): no FOMC/CPI events", type(error).__name__)
        return MacroCalendar()
    if not isinstance(payload, dict):
        return MacroCalendar()
    return parse_macro_calendar(payload)


def macro_events_on(day: date, macro: MacroCalendar) -> list[CalendarEvent]:
    """FOMC decision and CPI release events on `day` (a New York date)."""
    events: list[CalendarEvent] = []
    if day in macro.fomc_decisions:
        projections = " with economic projections" if macro.fomc_decisions[day] else ""
        events.append(_event(day, KIND_FOMC, f"FOMC rate decision{projections}", None, "FOMC decision today"))
    if day in macro.cpi_releases:
        reference_month, release_time = macro.cpi_releases[day]
        at_time = f" at {release_time} ET" if release_time else ""
        title = f"CPI release ({reference_month}){at_time}" if reference_month else f"CPI release{at_time}"
        events.append(_event(day, KIND_CPI, title, None, "CPI release today"))
    return events


# ─── earnings (yfinance) ─────────────────────────────────────────────────────

def parse_earnings_dates(calendar_payload: Mapping[str, Any] | None) -> tuple[date, ...]:
    """
    Earnings dates from a yfinance `Ticker.calendar` dict (`{"Earnings Date": [date, ...], ...}`).
    Values may be dates, datetimes or ISO strings (fixtures); unparseable values are skipped.
    """
    if not calendar_payload:
        return ()
    raw_dates = calendar_payload.get(YFINANCE_EARNINGS_KEY) or []
    if not isinstance(raw_dates, (list, tuple)):
        raw_dates = [raw_dates]
    parsed = [_parse_day(value) for value in raw_dates]
    return tuple(sorted(day for day in parsed if day is not None))


def earnings_event_on(day: date, ticker: str, earnings_dates: Sequence[date]) -> CalendarEvent | None:
    """
    Rule: one date = confirmed → event on that day; two dates = Yahoo's estimated window →
    event on every day inside [first, last]. More dates: only exact matches count.
    """
    if not earnings_dates:
        return None
    if len(earnings_dates) == ESTIMATED_WINDOW_DATES and earnings_dates[0] <= day <= earnings_dates[-1]:
        return _event(day, KIND_EARNINGS, f"{ticker} earnings window (estimated date)", ticker,
                      f"{ticker} earnings window")
    if day in earnings_dates:
        return _event(day, KIND_EARNINGS, f"{ticker} earnings", ticker, f"{ticker} earnings today")
    return None


_earnings_cache: dict[str, tuple[float, tuple[date, ...]]] = {}
_yfinance_calendar_lock = threading.Lock()


def _download_earnings_dates(ticker: str) -> tuple[date, ...]:
    """Blocking: one yfinance calendar request (runs in a worker thread, one at a time)."""
    import yfinance  # imported lazily: importing this module must stay cheap

    with _yfinance_calendar_lock:
        return parse_earnings_dates(yfinance.Ticker(ticker).calendar)


async def fetch_earnings_dates(ticker: str) -> tuple[date, ...]:
    """
    Earnings dates for one equity ticker, cached 6 h. Any yfinance failure or a timeout
    returns () (no event) and is cached for 10 minutes, so a broken source cannot slow every tick.
    """
    key = ticker.upper()
    cached = _earnings_cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    try:
        dates = await asyncio.wait_for(asyncio.to_thread(_download_earnings_dates, key), EARNINGS_TIMEOUT_SECONDS)
        ttl = EARNINGS_CACHE_TTL_SECONDS
    except Exception as error:  # noqa: BLE001 — yfinance raises many types; all mean "no data"
        logger.warning("earnings dates for %s unavailable (%s)", key, type(error).__name__)
        dates, ttl = (), EARNINGS_FAILURE_TTL_SECONDS
    _earnings_cache[key] = (time.monotonic() + ttl, dates)
    return dates


def clear_earnings_cache() -> None:
    """Forget cached earnings dates (tests, or after the watchlist changes)."""
    _earnings_cache.clear()


# ─── prediction-market expiries (injected) ───────────────────────────────────

def pm_expiry_events_on(
    day: date,
    expiries: Iterable[ContractExpiry],
    watchlist: Iterable[str],
    tz: ZoneInfo = DEFAULT_TIMEZONE,
) -> list[CalendarEvent]:
    """One event per watchlist asset with ≥ 1 contract expiring on `day` (in time zone `tz`)."""
    watched = {symbol.upper() for symbol in watchlist}
    counts: dict[str, int] = {}
    for expiry in expiries:
        asset = expiry.asset.upper()
        if asset in watched and expiry.expires_at.astimezone(tz).date() == day:
            counts[asset] = counts.get(asset, 0) + 1
    events = []
    for asset, count in counts.items():
        plural = "s" if count > 1 else ""
        title = f"{asset}: {count} Gemini prediction contract{plural} expire today"
        events.append(_event(day, KIND_PM_EXPIRY, title, asset, f"{asset} prediction-market expiry today"))
    return events


# ─── crypto options expiry (Deribit monthly / quarterly) ─────────────────────

def last_friday_of_month(year: int, month: int) -> date:
    """The last Friday of a month (Deribit's monthly options expiry day)."""
    last_day = date(year, month, month_calendar.monthrange(year, month)[1])
    days_back = (last_day.weekday() - FRIDAY) % 7
    return last_day - timedelta(days=days_back)


def crypto_expiry_events_on(day: date, watchlist: Iterable[str]) -> list[CalendarEvent]:
    """BTC/ETH monthly (quarterly in Mar/Jun/Sep/Dec) Deribit options expiry, if `day` is that Friday."""
    if day != last_friday_of_month(day.year, day.month):
        return []
    cycle = "quarterly" if day.month in QUARTERLY_MONTHS else "monthly"
    watched = {symbol.upper() for symbol in watchlist}
    return [
        _event(day, KIND_CRYPTO, f"{asset} {cycle} options expiry (Deribit, {DERIBIT_EXPIRY_TIME_UTC})", asset,
               f"{asset} {cycle} options expiry today")
        for asset in DERIBIT_MONTHLY_ASSETS
        if asset in watched
    ]


# ─── combine ─────────────────────────────────────────────────────────────────

def equity_tickers(watchlist: Iterable[str]) -> list[str]:
    """Watchlist entries that are equities (everything that is not a known crypto symbol)."""
    return [symbol.upper() for symbol in watchlist if asset_class(symbol) == ASSET_CLASS_EQUITY]


def events_today(
    day: date,
    watchlist: Sequence[str],
    macro: MacroCalendar,
    earnings_by_ticker: Mapping[str, Sequence[date]],
    pm_expiries: Iterable[ContractExpiry] = (),
    tz: ZoneInfo = DEFAULT_TIMEZONE,
) -> list[CalendarEvent]:
    """All of today's events from already-fetched inputs (pure; ordered macro, earnings, pm, crypto)."""
    events = macro_events_on(day, macro)
    for ticker in equity_tickers(watchlist):
        earnings = earnings_event_on(day, ticker, earnings_by_ticker.get(ticker, ()))
        if earnings is not None:
            events.append(earnings)
    events.extend(pm_expiry_events_on(day, pm_expiries, watchlist, tz))
    events.extend(crypto_expiry_events_on(day, watchlist))
    return events


EarningsFetcher = Callable[[str], Awaitable[Sequence[date]]]


async def build_events_today(
    day: date,
    watchlist: Sequence[str],
    pm_expiries: Iterable[ContractExpiry] = (),
    *,
    macro: MacroCalendar | None = None,
    fetch_earnings: EarningsFetcher = fetch_earnings_dates,
    tz: ZoneInfo = DEFAULT_TIMEZONE,
) -> list[CalendarEvent]:
    """Fetch earnings for the watchlist equities (cached, concurrent), load the macro file, combine."""
    macro = macro if macro is not None else load_macro_calendar()
    tickers = equity_tickers(watchlist)
    fetched = await asyncio.gather(*(fetch_earnings(ticker) for ticker in tickers))
    earnings_by_ticker = dict(zip(tickers, fetched, strict=True))
    return events_today(day, watchlist, macro, earnings_by_ticker, pm_expiries, tz)


def today_in(tz: ZoneInfo = DEFAULT_TIMEZONE, now: datetime | None = None) -> date:
    """Today's date in the session time zone."""
    return (now or datetime.now(UTC)).astimezone(tz).date()


def events_per_agent(events: Iterable[CalendarEvent]) -> dict[str, list[str]]:
    """{agent: [reason, ...]} for `keeper.allocator.allocate(events_today=...)`."""
    per_agent: dict[str, list[str]] = {}
    for event in events:
        if event.boosted_agent is not None:
            per_agent.setdefault(event.boosted_agent, []).append(event.reason)
    return per_agent
