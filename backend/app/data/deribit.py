"""
Deribit adapter: crypto option quotes (BTC, ETH, SOL) for the `expected_move` and
`pm_edge` sellers (SPEC §4 items 1–2).

Flow:

    fetch_book_summary_payload(asset) ──┐   async, network, through app/data/http.py
    fetch_index_payload(asset) ─────────┤   (timeout, retries, 60 s cache, rate limit)
                                        ▼
    build_option_chain(asset, book, index) → OptionChain     pure, tested with fixtures
        parse_book_summary(...)  → [OptionQuote]  one per listed option with a mark IV
        parse_index_price(...)   → spot S (Deribit index, USD)
                                        ▼
    implied_vol_at(chain, expiry, strike, now) → VolEstimate  σ for ANY expiry and strike

`implied_vol_at` reads the mark IV of the listed strike closest to the requested strike
(the volatility smile) and, when the requested expiry is not listed, interpolates total
variance σ²·T linearly in time between the two listed expiries that bracket it
(`optionmath.interpolate_total_variance`).

Deribit facts this module relies on:
    * `public/get_book_summary_by_currency?currency=BTC&kind=option` returns one item per
      listed option with `instrument_name`, `mark_iv` (in PERCENT: 52.3 means 52.3 %),
      `underlying_price`, `bid_price`, `ask_price`, `open_interest`, ...
    * Instrument names are `<UNDERLYING>-<DMMMYY>-<STRIKE>-<C|P>`, e.g. `BTC-27DEC26-100000-C`.
      The day has no leading zero (`BTC-3JAN27-...`). USDC-settled ("linear") options write
      a decimal strike with `d` as the decimal point (`XRP_USDC-27DEC26-2d5-C` is strike 2.5).
    * Every Deribit option expires at 08:00 UTC on its expiry date.
    * SOL options are USDC-settled: they are listed under `currency=USDC` with the prefix
      `SOL_USDC`, next to other USDC options, so the parser filters by prefix.
    * Every JSON-RPC response carries `usOut` (server send time, microseconds since the
      epoch). We use it as the "as of" time of a payload, including recorded fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.data.http import get_json
from app.sellers.optionmath import interpolate_total_variance, year_fraction

# ─── constants ────────────────────────────────────────────────────────────────

DERIBIT_API_BASE = "https://www.deribit.com/api/v2/public"
DERIBIT_SOURCE = "deribit"            # cache / rate-limit bucket in app/data/http.py
DERIBIT_CACHE_TTL_SECONDS = 60.0      # option marks move slowly enough for a 60 s cache
OPTION_KIND = "option"

DERIBIT_EXPIRY_HOUR_UTC = 8           # all Deribit options expire at 08:00 UTC
PERCENT_PER_UNIT = 100.0              # mark_iv 52.3 (percent) → σ = 0.523
MICROSECONDS_PER_SECOND = 1_000_000
TWO_DIGIT_YEAR_BASE = 2000            # "26" in `27DEC26` → 2026
STRIKE_DECIMAL_MARKER = "d"           # `2d5` → 2.5 (USDC linear options)
INSTRUMENT_NAME_PARTS = 4             # prefix, expiry code, strike code, C/P

OPTION_TYPE_CALL = "call"
OPTION_TYPE_PUT = "put"
OPTION_TYPE_BY_CODE = {"C": OPTION_TYPE_CALL, "P": OPTION_TYPE_PUT}

MONTH_BY_CODE = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# `27DEC26` → day "27", month "DEC", year "26"
EXPIRY_CODE_PATTERN = re.compile(r"^(?P<day>\d{1,2})(?P<month>[A-Z]{3})(?P<year>\d{2})$")

# How a VolEstimate was obtained (reported to buyers as `sigma_method`).
VOL_LISTED = "listed_expiry"                     # the requested expiry is listed on Deribit
VOL_INTERPOLATED = "interpolated_total_variance"  # between the two bracketing listed expiries
VOL_FLAT = "flat_nearest_expiry"                 # outside the listed range: nearest σ held flat


# ─── which Deribit listing serves which asset ────────────────────────────────

@dataclass(frozen=True)
class DeribitUnderlying:
    """Where Deribit lists the options of one asset and which index prices it."""

    asset: str               # our symbol, e.g. "SOL"
    currency: str            # `currency` query parameter of get_book_summary_by_currency
    instrument_prefix: str   # first part of the instrument name, e.g. "SOL_USDC"
    index_name: str          # `index_name` of get_index_price, e.g. "sol_usdc"


SUPPORTED_UNDERLYINGS: dict[str, DeribitUnderlying] = {
    "BTC": DeribitUnderlying(asset="BTC", currency="BTC", instrument_prefix="BTC", index_name="btc_usd"),
    "ETH": DeribitUnderlying(asset="ETH", currency="ETH", instrument_prefix="ETH", index_name="eth_usd"),
    "SOL": DeribitUnderlying(asset="SOL", currency="USDC", instrument_prefix="SOL_USDC", index_name="sol_usdc"),
}


class DeribitDataError(ValueError):
    """A Deribit payload is malformed, or it has no usable option for the request."""


def is_supported_asset(asset: str) -> bool:
    """True if Deribit lists options for `asset` (case-insensitive), i.e. BTC, ETH or SOL."""
    return asset.upper() in SUPPORTED_UNDERLYINGS


def get_underlying(asset: str) -> DeribitUnderlying:
    """Look up the Deribit listing for `asset`; raises DeribitDataError if there is none."""
    underlying = SUPPORTED_UNDERLYINGS.get(asset.upper())
    if underlying is None:
        supported = ", ".join(SUPPORTED_UNDERLYINGS)
        raise DeribitDataError(f"Deribit lists no options for {asset!r} (supported: {supported})")
    return underlying


# ─── instrument names ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InstrumentName:
    """The four parts of a Deribit option name such as `BTC-27DEC26-100000-C`."""

    prefix: str          # "BTC" or "SOL_USDC"
    expiry: datetime     # 08:00 UTC on the expiry date
    strike: float
    option_type: str     # "call" or "put"


def parse_expiry_code(code: str) -> datetime:
    """`27DEC26` → 2026-12-27 08:00 UTC (Deribit's fixed expiry time)."""
    match = EXPIRY_CODE_PATTERN.match(code)
    if match is None or match["month"] not in MONTH_BY_CODE:
        raise DeribitDataError(f"not a Deribit expiry code: {code!r}")
    return datetime(
        year=TWO_DIGIT_YEAR_BASE + int(match["year"]),
        month=MONTH_BY_CODE[match["month"]],
        day=int(match["day"]),
        hour=DERIBIT_EXPIRY_HOUR_UTC,
        tzinfo=timezone.utc,
    )


def parse_strike_code(code: str) -> float:
    """`100000` → 100000.0 and `2d5` → 2.5 (USDC options use `d` as the decimal point)."""
    try:
        return float(code.replace(STRIKE_DECIMAL_MARKER, "."))
    except ValueError as exc:
        raise DeribitDataError(f"not a Deribit strike: {code!r}") from exc


def parse_instrument_name(name: str) -> InstrumentName:
    """Split `BTC-27DEC26-100000-C` into prefix, expiry (08:00 UTC), strike and call/put."""
    parts = name.split("-")
    if len(parts) != INSTRUMENT_NAME_PARTS or parts[3] not in OPTION_TYPE_BY_CODE:
        raise DeribitDataError(f"not a Deribit option name: {name!r}")
    prefix, expiry_code, strike_code, type_code = parts
    return InstrumentName(
        prefix=prefix,
        expiry=parse_expiry_code(expiry_code),
        strike=parse_strike_code(strike_code),
        option_type=OPTION_TYPE_BY_CODE[type_code],
    )


# ─── payload parsing ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OptionQuote:
    """One listed Deribit option with a usable mark implied volatility."""

    instrument_name: str
    expiry: datetime
    strike: float
    option_type: str               # "call" or "put"
    mark_iv: float                 # decimal annualised volatility: 0.523 = 52.3 %
    underlying_price: float | None  # Deribit's forward for this expiry (USD)


@dataclass(frozen=True)
class OptionChain:
    """Everything the sellers need from Deribit for one asset."""

    asset: str
    index_name: str
    index_price: float            # spot S in USD
    quotes: list[OptionQuote]
    as_of: datetime               # when Deribit sent the index price (`usOut`)


def _rpc_result(payload: dict) -> Any:
    """Return the `result` of a Deribit JSON-RPC envelope; raise if it carries an error instead."""
    if "error" in payload:
        raise DeribitDataError(f"Deribit returned an error: {payload['error']}")
    if "result" not in payload:
        raise DeribitDataError("Deribit payload has no `result` field")
    return payload["result"]


def response_time(payload: dict) -> datetime:
    """When Deribit sent this response: the envelope's `usOut` (µs since the epoch) as UTC."""
    if "usOut" not in payload:
        raise DeribitDataError("Deribit payload has no `usOut` timestamp")
    return datetime.fromtimestamp(payload["usOut"] / MICROSECONDS_PER_SECOND, tz=timezone.utc)


def _quote_from_item(item: dict, instrument_prefix: str) -> OptionQuote | None:
    """
    Turn one book-summary item into an OptionQuote.

    Returns None for items that belong to another underlying (the USDC listing mixes
    SOL, XRP, ...), for items without a positive `mark_iv` (nothing to read σ from) and
    for names that do not parse (one odd instrument must not sink the whole chain).
    """
    name = item.get("instrument_name", "")
    if not name.startswith(instrument_prefix + "-"):
        return None
    mark_iv_percent = item.get("mark_iv")
    if mark_iv_percent is None or mark_iv_percent <= 0:
        return None
    try:
        parsed = parse_instrument_name(name)
    except DeribitDataError:
        return None
    return OptionQuote(
        instrument_name=name,
        expiry=parsed.expiry,
        strike=parsed.strike,
        option_type=parsed.option_type,
        mark_iv=mark_iv_percent / PERCENT_PER_UNIT,
        underlying_price=item.get("underlying_price"),
    )


def parse_book_summary(payload: dict, asset: str) -> list[OptionQuote]:
    """All options of `asset` with a usable mark IV from a get_book_summary_by_currency payload."""
    underlying = get_underlying(asset)
    quotes = []
    for item in _rpc_result(payload):
        quote = _quote_from_item(item, underlying.instrument_prefix)
        if quote is not None:
            quotes.append(quote)
    return quotes


def parse_index_price(payload: dict) -> float:
    """Spot index price in USD from a get_index_price payload (`result.index_price`)."""
    result = _rpc_result(payload)
    index_price = result.get("index_price")
    if index_price is None or index_price <= 0:
        raise DeribitDataError(f"Deribit index price missing or not positive: {index_price!r}")
    return float(index_price)


def build_option_chain(asset: str, book_payload: dict, index_payload: dict) -> OptionChain:
    """Combine the two raw payloads for `asset` into one OptionChain (pure, no network)."""
    underlying = get_underlying(asset)
    quotes = parse_book_summary(book_payload, asset)
    if not quotes:
        raise DeribitDataError(f"Deribit returned no {underlying.asset} options with a mark IV")
    return OptionChain(
        asset=underlying.asset,
        index_name=underlying.index_name,
        index_price=parse_index_price(index_payload),
        quotes=quotes,
        as_of=response_time(index_payload),
    )


# ─── volatility at any expiry and strike ─────────────────────────────────────

@dataclass(frozen=True)
class StrikeVol:
    """σ read at one listed strike of one expiry."""

    expiry: datetime
    strike: float                  # the listed strike actually used
    sigma: float                   # mean mark IV of the call and put at that strike
    instruments: tuple[str, ...]   # instrument names the σ came from


@dataclass(frozen=True)
class VolEstimate:
    """σ for a requested (expiry, strike), with a record of how it was obtained."""

    sigma: float
    method: str                    # VOL_LISTED, VOL_INTERPOLATED or VOL_FLAT
    points: tuple[StrikeVol, ...]  # the one or two listed expiries used


def listed_expiries(chain: OptionChain, now: datetime) -> list[datetime]:
    """Distinct listed expiries strictly after `now`, earliest first."""
    return sorted({quote.expiry for quote in chain.quotes if quote.expiry > now})


def vol_at_strike(chain: OptionChain, expiry: datetime, target_strike: float) -> StrikeVol:
    """
    σ at the listed strike of `expiry` closest to `target_strike` (the smile, not just ATM).

    Rule: pick the listed strike K* minimising |K* − target| (ties → the lower strike),
    then σ = mean of the mark IVs of the call and the put at K* (whichever are listed).
    For the ATM σ pass the index price as `target_strike`.
    """
    same_expiry = [quote for quote in chain.quotes if quote.expiry == expiry]
    if not same_expiry:
        raise DeribitDataError(f"no {chain.asset} options listed for expiry {expiry.isoformat()}")
    listed_strikes = sorted({quote.strike for quote in same_expiry})
    nearest_strike = min(listed_strikes, key=lambda strike: abs(strike - target_strike))
    at_strike = [quote for quote in same_expiry if quote.strike == nearest_strike]
    sigma = sum(quote.mark_iv for quote in at_strike) / len(at_strike)
    return StrikeVol(
        expiry=expiry,
        strike=nearest_strike,
        sigma=sigma,
        instruments=tuple(sorted(quote.instrument_name for quote in at_strike)),
    )


def _bracketing_expiries(expiries: list[datetime], target: datetime) -> tuple[datetime | None, datetime | None]:
    """The latest listed expiry before `target` and the earliest one after it (None if absent)."""
    before = [expiry for expiry in expiries if expiry < target]
    after = [expiry for expiry in expiries if expiry > target]
    near = before[-1] if before else None
    far = after[0] if after else None
    return near, far


def implied_vol_at(chain: OptionChain, target_expiry: datetime, target_strike: float, now: datetime) -> VolEstimate:
    """
    σ for an option on `chain.asset` expiring at `target_expiry` with strike `target_strike`.

    1. If `target_expiry` is a listed expiry: σ at the nearest listed strike (VOL_LISTED).
    2. If two listed expiries t1 < T < t2 bracket it: σ1, σ2 at the nearest strikes, then
       total variance σ²·T is interpolated linearly in time (VOL_INTERPOLATED):
           w(T) = σ1²·t1 + (T − t1)/(t2 − t1) · (σ2²·t2 − σ1²·t1),   σ = √(w(T)/T)
    3. Before the first / after the last listed expiry: that expiry's σ held flat (VOL_FLAT).
    """
    expiries = listed_expiries(chain, now)
    if not expiries:
        raise DeribitDataError(f"no unexpired {chain.asset} options in the chain")
    if target_expiry <= now:
        raise DeribitDataError(f"target expiry {target_expiry.isoformat()} is not in the future")

    if target_expiry in expiries:
        point = vol_at_strike(chain, target_expiry, target_strike)
        return VolEstimate(sigma=point.sigma, method=VOL_LISTED, points=(point,))

    near, far = _bracketing_expiries(expiries, target_expiry)
    if near is None or far is None:
        nearest_listed = far if near is None else near
        point = vol_at_strike(chain, nearest_listed, target_strike)
        return VolEstimate(sigma=point.sigma, method=VOL_FLAT, points=(point,))

    near_point = vol_at_strike(chain, near, target_strike)
    far_point = vol_at_strike(chain, far, target_strike)
    sigma = interpolate_total_variance(
        year_fraction(now, target_expiry),
        near=(year_fraction(now, near), near_point.sigma),
        far=(year_fraction(now, far), far_point.sigma),
    )
    return VolEstimate(sigma=sigma, method=VOL_INTERPOLATED, points=(near_point, far_point))


# ─── network (async) ─────────────────────────────────────────────────────────

async def fetch_book_summary_payload(asset: str) -> dict:
    """Raw `get_book_summary_by_currency` envelope with every listed option of `asset`'s currency."""
    underlying = get_underlying(asset)
    return await get_json(
        f"{DERIBIT_API_BASE}/get_book_summary_by_currency",
        params={"currency": underlying.currency, "kind": OPTION_KIND},
        source=DERIBIT_SOURCE,
        ttl=DERIBIT_CACHE_TTL_SECONDS,
    )


async def fetch_index_payload(asset: str) -> dict:
    """Raw `get_index_price` envelope for `asset`'s Deribit index (e.g. btc_usd)."""
    underlying = get_underlying(asset)
    return await get_json(
        f"{DERIBIT_API_BASE}/get_index_price",
        params={"index_name": underlying.index_name},
        source=DERIBIT_SOURCE,
        ttl=DERIBIT_CACHE_TTL_SECONDS,
    )


async def fetch_option_chain(asset: str) -> OptionChain:
    """Fetch and parse the option chain and index price of `asset` (BTC, ETH or SOL)."""
    book_payload = await fetch_book_summary_payload(asset)
    index_payload = await fetch_index_payload(asset)
    return build_option_chain(asset, book_payload, index_payload)
