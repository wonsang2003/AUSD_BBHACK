"""
yfinance adapter: US equity option chains for the `expected_move` seller (SPEC §4 item 1).

yfinance is an UNOFFICIAL Yahoo Finance client and its quotes are delayed (15 minutes or
more), so every response built from it carries `data_delay: "15m+ delayed (yfinance)"`.

Flow:

    fetch_option_snapshot(ticker, requested_expiry, now, min_hours_to_expiry)
        async; runs yfinance in a worker thread with a timeout, caches for 120 s
        → snapshot dict (plain JSON types, the shape of tests/fixtures/yfin_*.json):
          {"ticker", "spot", "expiries": ["YYYY-MM-DD", ...], "expiry", "calls": [row],
           "puts": [row], "fetched_at"}   with row = {contractSymbol, strike, lastPrice,
           bid, ask, volume, openInterest, impliedVolatility}
                    ▼
    parse_option_rows(rows)  → [EquityOptionQuote]               pure, tested with fixtures
    mid_price(quote)         → (price, basis)  bid/ask mid, else last trade
    equity_expiry_datetime() → 16:00 New York on the expiry date, in UTC

Why this adapter does not use app/data/http.py: yfinance makes its own HTTP calls and
manages Yahoo's cookie + "crumb" session, which the raw Yahoo endpoints require and which
http.py (one fresh client per request) cannot keep. To keep the same protections it adds a
timeout, an in-process TTL cache, and a lock so only one yfinance call runs at a time.
Any yfinance failure is raised as `YFinanceError` (an expiry that is not listed as the
subclass `ExpiryNotListedError`, which is the buyer's mistake, not Yahoo's), and the seller
turns it into an error response instead of crashing.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# ─── constants ────────────────────────────────────────────────────────────────

YFINANCE_DELAY_NOTE = "15m+ delayed (yfinance)"
YFINANCE_TIMEOUT_SECONDS = 20.0
YFINANCE_CACHE_TTL_SECONDS = 120.0

EXPIRY_NEXT = "next"
EXPIRY_DATE_FORMAT = "%Y-%m-%d"

# US equity options stop trading at the 16:00 New York close on their expiry date.
NEW_YORK = ZoneInfo("America/New_York")
EQUITY_EXPIRY_LOCAL_TIME = clock_time(hour=16)

# Columns kept from yfinance's option DataFrames (the rest are display-only).
OPTION_ROW_COLUMNS = (
    "contractSymbol",
    "strike",
    "lastPrice",
    "bid",
    "ask",
    "volume",
    "openInterest",
    "impliedVolatility",
)

# yfinance reports implied volatility as a decimal (0.45 = 45 %). Values outside this band
# (e.g. 0.00001 on stale or illiquid strikes) are artefacts, not market volatility.
MIN_PLAUSIBLE_IV = 0.01
MAX_PLAUSIBLE_IV = 5.0

# Where an option price came from (reported to buyers as `price_basis`).
PRICE_BASIS_MID = "bid_ask_mid"
PRICE_BASIS_LAST = "last_trade"

_snapshot_cache: dict[tuple[str, str], tuple[float, dict]] = {}

# yfinance shares one Yahoo session per process; serialise its calls. A threading lock
# (held inside the worker thread) instead of an asyncio lock, so it is not tied to one
# event loop.
_yfinance_thread_lock = threading.Lock()


class YFinanceError(Exception):
    """yfinance failed (network, unknown ticker, no options listed, timeout ...)."""


class ExpiryNotListedError(YFinanceError):
    """The requested expiry is not a listed (or not a usable) option expiry for the ticker."""


# ─── pure helpers ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EquityOptionQuote:
    """One row of a yfinance option chain, with NaN/absent numbers turned into None."""

    contract_symbol: str
    strike: float
    bid: float | None
    ask: float | None
    last_price: float | None
    implied_volatility: float | None


def _clean_number(value: Any) -> float | None:
    """A JSON/pandas number → float; None, NaN and non-numbers → None."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def parse_option_rows(rows: list[dict]) -> list[EquityOptionQuote]:
    """Snapshot rows (calls or puts) → EquityOptionQuote list; rows without a strike are dropped."""
    quotes = []
    for row in rows:
        strike = _clean_number(row.get("strike"))
        if strike is None or strike <= 0:
            continue
        quotes.append(
            EquityOptionQuote(
                contract_symbol=str(row.get("contractSymbol", "")),
                strike=strike,
                bid=_clean_number(row.get("bid")),
                ask=_clean_number(row.get("ask")),
                last_price=_clean_number(row.get("lastPrice")),
                implied_volatility=_clean_number(row.get("impliedVolatility")),
            )
        )
    return quotes


def mid_price(quote: EquityOptionQuote) -> tuple[float, str] | None:
    """
    Price of one option and its basis.

    Rule: (bid + ask) / 2 when both are positive and ask ≥ bid ("bid_ask_mid"); otherwise
    the last trade price if positive ("last_trade", e.g. outside market hours when Yahoo
    shows bid = ask = 0); otherwise None.
    """
    bid, ask = quote.bid, quote.ask
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        return (bid + ask) / 2, PRICE_BASIS_MID
    if quote.last_price is not None and quote.last_price > 0:
        return quote.last_price, PRICE_BASIS_LAST
    return None


def is_plausible_iv(implied_volatility: float | None) -> bool:
    """True if yfinance's implied volatility is a real number within [0.01, 5.0]."""
    if implied_volatility is None:
        return False
    return MIN_PLAUSIBLE_IV <= implied_volatility <= MAX_PLAUSIBLE_IV


def equity_expiry_datetime(expiry: str) -> datetime:
    """"2026-10-16" → 2026-10-16 16:00 America/New_York, returned in UTC (DST handled)."""
    expiry_date = datetime.strptime(expiry, EXPIRY_DATE_FORMAT).date()
    local_close = datetime.combine(expiry_date, EQUITY_EXPIRY_LOCAL_TIME, tzinfo=NEW_YORK)
    return local_close.astimezone(timezone.utc)


def choose_expiry(expiries: list[str], requested: str, now: datetime, min_hours_to_expiry: float) -> str:
    """
    Pick the option expiry to use from yfinance's listed expiry dates.

    "next" → the earliest listed expiry whose 16:00 New York close is at least
    `min_hours_to_expiry` after `now`. "YYYY-MM-DD" → that date, which must be listed and
    far enough in the future. Raises YFinanceError with the listed dates otherwise.
    """
    earliest_allowed = now + timedelta(hours=min_hours_to_expiry)
    usable = [expiry for expiry in sorted(expiries) if equity_expiry_datetime(expiry) >= earliest_allowed]
    if requested == EXPIRY_NEXT:
        if not usable:
            raise ExpiryNotListedError("no listed option expiry far enough in the future")
        return usable[0]
    if requested not in usable:
        listed = ", ".join(sorted(expiries)) or "none"
        raise ExpiryNotListedError(f"expiry {requested} is not a usable listed expiry (listed: {listed})")
    return requested


# ─── blocking yfinance calls (run in a worker thread) ────────────────────────

def _dataframe_rows(frame: Any) -> list[dict]:
    """Keep OPTION_ROW_COLUMNS of a yfinance DataFrame as plain dicts (NaN → None)."""
    rows = []
    for record in frame[list(OPTION_ROW_COLUMNS)].to_dict("records"):
        rows.append({column: _json_value(record[column]) for column in OPTION_ROW_COLUMNS})
    return rows


def _json_value(value: Any) -> Any:
    """A numpy/pandas scalar → plain float (NaN → None); strings stay strings."""
    if isinstance(value, str):
        return value
    return _clean_number(value)


def _spot_price(ticker_object: Any, underlying: dict | None) -> float:
    """Spot from the option chain's `underlying.regularMarketPrice`, else `fast_info.last_price`."""
    spot = _clean_number((underlying or {}).get("regularMarketPrice"))
    if spot is None:
        spot = _clean_number(ticker_object.fast_info.last_price)
    if spot is None or spot <= 0:
        raise YFinanceError("no spot price available")
    return spot


def _download_snapshot_serialised(ticker: str, requested_expiry: str, now: datetime, min_hours: float) -> dict:
    """Blocking: `_download_snapshot` while holding the process-wide yfinance lock."""
    with _yfinance_thread_lock:
        return _download_snapshot(ticker, requested_expiry, now, min_hours)


def _download_snapshot(ticker: str, requested_expiry: str, now: datetime, min_hours_to_expiry: float) -> dict:
    """Blocking: list expiries, choose one, download its chain and spot. Runs in a thread."""
    import yfinance  # imported here so importing this module stays cheap

    ticker_object = yfinance.Ticker(ticker)
    expiries = list(ticker_object.options)
    if not expiries:
        raise YFinanceError(f"yfinance lists no options for {ticker}")
    expiry = choose_expiry(expiries, requested_expiry, now, min_hours_to_expiry)
    chain = ticker_object.option_chain(expiry)
    if chain.calls is None or chain.puts is None:
        raise YFinanceError(f"yfinance returned an empty option chain for {ticker} {expiry}")
    return {
        "ticker": ticker,
        "spot": _spot_price(ticker_object, chain.underlying),
        "expiries": sorted(expiries),
        "expiry": expiry,
        "calls": _dataframe_rows(chain.calls),
        "puts": _dataframe_rows(chain.puts),
        "fetched_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }


# ─── network (async) ─────────────────────────────────────────────────────────

async def fetch_option_snapshot(
    ticker: str,
    requested_expiry: str,
    now: datetime,
    min_hours_to_expiry: float,
) -> dict:
    """
    Option chain snapshot for `ticker` at the chosen expiry (see `choose_expiry`).

    Cached for YFINANCE_CACHE_TTL_SECONDS per (ticker, requested expiry). Any failure —
    including a timeout after YFINANCE_TIMEOUT_SECONDS — is raised as YFinanceError.
    """
    cache_key = (ticker.upper(), requested_expiry)
    cached = _snapshot_cache.get(cache_key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    try:
        snapshot = await asyncio.wait_for(
            asyncio.to_thread(
                _download_snapshot_serialised, ticker.upper(), requested_expiry, now, min_hours_to_expiry
            ),
            timeout=YFINANCE_TIMEOUT_SECONDS,
        )
    except YFinanceError:
        raise
    except asyncio.TimeoutError as exc:
        raise YFinanceError(f"yfinance timed out after {YFINANCE_TIMEOUT_SECONDS:g} s") from exc
    except Exception as exc:  # yfinance raises many exception types; all mean "no data"
        raise YFinanceError(f"yfinance failed: {type(exc).__name__}: {exc}") from exc

    _snapshot_cache[cache_key] = (time.monotonic() + YFINANCE_CACHE_TTL_SECONDS, snapshot)
    return snapshot


def clear_cache() -> None:
    """Forget cached snapshots (used by tests)."""
    _snapshot_cache.clear()
