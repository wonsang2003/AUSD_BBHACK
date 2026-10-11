"""
Tests for app/keeper/calendar.py.

No network: yfinance earnings data is represented by a realistic-shape fixture of
`yfinance.Ticker("NVDA").calendar` (the dict yfinance 1.x returns: "Earnings Date" is a list
of one confirmed date or a two-date estimated window, plus estimate fields), and the
earnings fetcher is injected. FOMC/CPI tests read the real `keeper/data/macro_calendar.json`.
Live yfinance calendars are verified on the user's machine.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from app.keeper.allocator import AgentPosterior, allocate
from app.keeper.calendar import (
    KIND_CPI,
    KIND_CRYPTO,
    KIND_EARNINGS,
    KIND_FOMC,
    KIND_PM_EXPIRY,
    ContractExpiry,
    MacroCalendar,
    build_events_today,
    crypto_expiry_events_on,
    earnings_event_on,
    events_per_agent,
    events_today,
    last_friday_of_month,
    load_macro_calendar,
    parse_earnings_dates,
    pm_expiry_events_on,
)

# Shape of yfinance 1.x `Ticker.calendar` (dates serialised as ISO strings for the fixture).
NVDA_CALENDAR_CONFIRMED = {
    "Dividend Date": "2026-10-02",
    "Ex-Dividend Date": "2026-09-11",
    "Earnings Date": ["2026-11-18"],
    "Earnings High": 1.43,
    "Earnings Low": 1.21,
    "Earnings Average": 1.3,
    "Revenue High": 59_900_000_000,
    "Revenue Low": 54_100_000_000,
    "Revenue Average": 56_800_000_000,
}
AAPL_CALENDAR_ESTIMATED = {"Earnings Date": [date(2026, 10, 29), date(2026, 11, 2)], "Earnings Average": 1.75}
WATCHLIST = ["BTC", "ETH", "NVDA", "AAPL"]


async def _fixture_earnings(ticker: str) -> tuple[date, ...]:
    fixtures = {"NVDA": NVDA_CALENDAR_CONFIRMED, "AAPL": AAPL_CALENDAR_ESTIMATED}
    return parse_earnings_dates(fixtures.get(ticker))


# ─── earnings ────────────────────────────────────────────────────────────────

def test_parse_earnings_dates_reads_the_yfinance_calendar_shape():
    # Act / Assert
    assert parse_earnings_dates(NVDA_CALENDAR_CONFIRMED) == (date(2026, 11, 18),)
    assert parse_earnings_dates(AAPL_CALENDAR_ESTIMATED) == (date(2026, 10, 29), date(2026, 11, 2))
    assert parse_earnings_dates({}) == ()
    assert parse_earnings_dates({"Earnings Date": ["not a date"]}) == ()


def test_confirmed_earnings_date_gives_an_event_only_on_that_day():
    # Arrange
    dates = parse_earnings_dates(NVDA_CALENDAR_CONFIRMED)

    # Act
    on_day = earnings_event_on(date(2026, 11, 18), "NVDA", dates)
    day_before = earnings_event_on(date(2026, 11, 17), "NVDA", dates)

    # Assert
    assert on_day is not None and on_day.kind == KIND_EARNINGS
    assert on_day.boosted_agent == "OptionsEvent"
    assert on_day.reason == "NVDA earnings today"
    assert day_before is None


def test_estimated_earnings_window_gives_an_event_on_every_day_inside_it():
    # Arrange
    dates = parse_earnings_dates(AAPL_CALENDAR_ESTIMATED)

    # Act
    inside = earnings_event_on(date(2026, 10, 31), "AAPL", dates)
    after = earnings_event_on(date(2026, 11, 3), "AAPL", dates)

    # Assert
    assert inside is not None and "estimated" in inside.title
    assert after is None


# ─── macro calendar ──────────────────────────────────────────────────────────

def test_real_macro_calendar_has_fomc_decision_and_cpi_release_days():
    # Arrange
    macro = load_macro_calendar()

    # Act
    fomc = events_today(date(2026, 10, 28), [], macro, {})
    cpi = events_today(date(2026, 10, 14), [], macro, {})

    # Assert
    assert [(e.kind, e.boosted_agent) for e in fomc] == [(KIND_FOMC, "MacroFilings")]
    assert [(e.kind, e.title) for e in cpi] == [(KIND_CPI, "CPI release (2026-09) at 08:30 ET")]


def test_missing_macro_calendar_file_degrades_to_no_events(tmp_path: Path):
    # Act
    macro = load_macro_calendar(tmp_path / "missing.json")

    # Assert
    assert macro == MacroCalendar()


def test_malformed_macro_entries_are_skipped(tmp_path: Path):
    # Arrange
    path = tmp_path / "macro.json"
    path.write_text(json.dumps({"fomc": [{"decision_date": "soon"}, {"decision_date": "2026-12-09", "has_sep": True}]}))

    # Act
    macro = load_macro_calendar(path)

    # Assert
    assert dict(macro.fomc_decisions) == {date(2026, 12, 9): True}
    assert "with economic projections" in events_today(date(2026, 12, 9), [], macro, {})[0].title


# ─── prediction markets and crypto expiries ──────────────────────────────────

def test_pm_expiries_are_grouped_per_watchlist_asset_in_the_session_time_zone():
    # Arrange: 00:30 UTC on Oct 12 is still Oct 11 in New York
    expiries = [
        ContractExpiry("BTC", datetime(2026, 10, 12, 0, 30, tzinfo=UTC)),
        ContractExpiry("BTC", datetime(2026, 10, 11, 20, 0, tzinfo=UTC)),
        ContractExpiry("DOGE", datetime(2026, 10, 11, 20, 0, tzinfo=UTC)),   # not on the watchlist
    ]

    # Act
    events = pm_expiry_events_on(date(2026, 10, 11), expiries, WATCHLIST)

    # Assert
    assert [(e.kind, e.asset, e.title) for e in events] == [
        (KIND_PM_EXPIRY, "BTC", "BTC: 2 Gemini prediction contracts expire today"),
    ]


def test_last_friday_of_month_and_quarterly_crypto_expiry():
    # Act / Assert
    assert last_friday_of_month(2026, 10) == date(2026, 10, 30)
    assert last_friday_of_month(2026, 12) == date(2026, 12, 25)
    october = crypto_expiry_events_on(date(2026, 10, 30), WATCHLIST)
    december = crypto_expiry_events_on(date(2026, 12, 25), ["BTC"])
    assert [(e.kind, e.asset) for e in october] == [(KIND_CRYPTO, "BTC"), (KIND_CRYPTO, "ETH")]
    assert "quarterly" in december[0].title
    assert crypto_expiry_events_on(date(2026, 10, 23), WATCHLIST) == []


# ─── combined output ─────────────────────────────────────────────────────────

async def test_build_events_today_uses_injected_earnings_and_degrades_per_ticker():
    # Arrange
    async def flaky_earnings(ticker: str) -> tuple[date, ...]:
        return await _fixture_earnings(ticker) if ticker == "NVDA" else ()

    # Act
    events = await build_events_today(date(2026, 11, 18), WATCHLIST, macro=MacroCalendar(),
                                      fetch_earnings=flaky_earnings)

    # Assert
    assert [e.to_api() for e in events] == [
        {"date": "2026-11-18", "kind": "earnings", "title": "NVDA earnings", "asset": "NVDA",
         "boosted_agent": "OptionsEvent"},
    ]


async def test_build_events_today_only_asks_for_equity_tickers():
    # Arrange
    asked: list[str] = []

    async def recording_fetcher(ticker: str) -> tuple[date, ...]:
        asked.append(ticker)
        return ()

    # Act
    await build_events_today(date(2026, 10, 11), WATCHLIST, macro=MacroCalendar(), fetch_earnings=recording_fetcher)

    # Assert
    assert sorted(asked) == ["AAPL", "NVDA"]


def test_events_per_agent_feeds_the_allocator_event_boost():
    # Arrange
    macro = MacroCalendar(fomc_decisions={date(2026, 11, 18): False})
    events = events_today(date(2026, 11, 18), WATCHLIST, macro, {"NVDA": (date(2026, 11, 18),)})

    # Act
    per_agent = events_per_agent(events)
    allocations = allocate(
        1_000_000,
        [AgentPosterior(name) for name in ("OnchainPerps", "OptionsEvent", "NewsSentiment", "MacroFilings")],
        events_today=per_agent,
        rng_seed=7,
    )

    # Assert
    assert per_agent == {"MacroFilings": ["FOMC decision today"], "OptionsEvent": ["NVDA earnings today"]}
    options_reasons = next(a.reasons for a in allocations if a.agent == "OptionsEvent")
    assert any("NVDA earnings today" in reason for reason in options_reasons)
