"""
expected_move seller — "How far is <asset> expected to move by <expiry>, and how likely is
it to finish above my strikes?" (SPEC §4 item 1; demo vendor, 0.05 AUSD per call)

    GET /sell/expected-move?asset=BTC|ETH|SOL|<equity>&expiry=<YYYY-MM-DD|next>&strikes=K1,K2

The x402 server (`app/x402/server.py`, assembled in `app/sellers/routers.py`) gates the
endpoint with `make_offer()` and calls `compute(query_params)` after verifying payment.
This module never imports the server.

Crypto (BTC, ETH, SOL) — Deribit, real-time:
    S             = Deribit index price
    σ_ATM         = mark IV at the listed strike nearest S on the chosen expiry; a date that
                    is not a listed expiry is interpolated in total variance σ²T between
                    its two listed neighbours (see `deribit.implied_vol_at`)
    T             = (expiry − now) in years, ACT/365
    expected move = S · σ_ATM · √T                 (one standard deviation, USD)
    P(S_T > K)    = N(d2),  d2 = (ln(S/K) − σ_K²T/2) / (σ_K√T),  r = 0,
                    with σ_K read at the listed strike nearest K (the smile)

Equities — yfinance option chain (unofficial, 15m+ delayed):
    straddle move = (ATM call mid + ATM put mid) / S
    σ_ATM         = ATM impliedVolatility (mean of call and put);  expected move = S·σ_ATM·√T
    P(S_T > K)    = N(d2) with σ_K = impliedVolatility at the listed strike nearest K
    When yfinance fails the response is an error dict, never an exception.

All option math comes from `app/sellers/optionmath.py`. Every response carries
`inputs_used`, `computed_at`, `method`, `confidence` (0–1) and `data_delay`. A failed
request returns {"error": "...", "error_kind": "..."} — the x402 server should not settle
a payment for a response that contains "error".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from eth_utils import is_address, to_checksum_address

from app.config import get_settings
from app.constants import ONE_AUSD
from app.data import deribit, yfin
from app.data.deribit import OptionChain
from app.data.http import UpstreamError
from app.data.yfin import EquityOptionQuote
from app.sellers.optionmath import DAYS_PER_YEAR, expected_move, prob_above, straddle_implied_move, year_fraction
from app.x402.offer import SellerOffer

# ─── offer ────────────────────────────────────────────────────────────────────

SELLER_ID = "expected_move"
OFFER_PATH = "/sell/expected-move"
PRICE_ATOMIC = 5 * ONE_AUSD // 100          # 0.05 AUSD = 50 000 atomic units
PAYOUT_SETTING_NAME = "SELLER_PAYTO_EXPECTED_MOVE"
OFFER_DESCRIPTION = (
    "Demo vendor: options-implied expected move and P(finish above strike) for BTC/ETH/SOL "
    "(Deribit, real-time) or US equities (yfinance, 15m+ delayed)"
)

# ─── request rules ───────────────────────────────────────────────────────────

EXPIRY_NEXT = "next"
EXPIRY_DATE_FORMAT = "%Y-%m-%d"
MIN_HOURS_TO_EXPIRY = 1.0     # an expiry settling within the hour has no meaningful move left
MAX_STRIKES = 10
ASSET_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")   # BTC, NVDA, BRK-B, ...

ASSET_CLASS_CRYPTO = "crypto"
ASSET_CLASS_EQUITY = "equity"

# ─── response labels ─────────────────────────────────────────────────────────

DERIBIT_DATA_DELAY = "real-time (Deribit public API)"
CRYPTO_METHOD = (
    "Deribit mark IV: expected move = S·σ_ATM·√T (ACT/365); P(S_T>K) = N(d2) with σ at the "
    "listed strike nearest K, r = 0; unlisted expiries interpolated in total variance σ²T"
)
EQUITY_METHOD = (
    "yfinance option chain: straddle move = (ATM call mid + ATM put mid)/S; expected move = "
    "S·σ_ATM·√T with σ_ATM = ATM impliedVolatility; P(S_T>K) = N(d2), r = 0"
)

# Confidence (0–1): how much the numbers can be trusted, from how σ was obtained.
CRYPTO_CONFIDENCE_BY_VOL_METHOD = {
    deribit.VOL_LISTED: 0.9,          # σ read straight off the requested expiry
    deribit.VOL_INTERPOLATED: 0.8,    # σ interpolated between two listed expiries
    deribit.VOL_FLAT: 0.6,            # outside the listed range, nearest σ held flat
}
EQUITY_BASE_CONFIDENCE = 0.6          # unofficial, delayed source
EQUITY_LAST_TRADE_PENALTY = 0.15      # ATM priced from last trades, not a live bid/ask
EQUITY_NO_SIGMA_PENALTY = 0.2         # yfinance IV unusable: only the straddle move is reported
MIN_CONFIDENCE = 0.1

ERROR_BAD_REQUEST = "bad_request"
ERROR_UPSTREAM = "upstream_unavailable"
ERROR_NO_DATA = "no_usable_data"

USD_DECIMALS = 2
PROBABILITY_DECIMALS = 4
SIGMA_DECIMALS = 4
YEARS_DECIMALS = 6
DAYS_DECIMALS = 3


class SellerConfigError(RuntimeError):
    """The seller cannot be offered (e.g. its payout address is not configured)."""


class ExpectedMoveInputError(ValueError):
    """The query parameters are invalid; the message tells the buyer what to fix."""


class EquityDataError(ValueError):
    """The yfinance chain has no usable at-the-money prices."""


# ─── offer ────────────────────────────────────────────────────────────────────

def _require_payout_address(value: str, setting_name: str) -> str:
    """Checksummed payout address, or SellerConfigError naming the setting to fill in."""
    if not value:
        raise SellerConfigError(f"{setting_name} is empty: set it in .env to this seller's payout address")
    if not is_address(value):
        raise SellerConfigError(f"{setting_name} is not a valid address: {value!r}")
    return to_checksum_address(value)


def make_offer() -> SellerOffer:
    """The x402 offer: 0.05 AUSD per call, paid to settings.seller_payto_expected_move."""
    pay_to = _require_payout_address(get_settings().seller_payto_expected_move, PAYOUT_SETTING_NAME)
    return SellerOffer(
        seller_id=SELLER_ID,
        path=OFFER_PATH,
        description=OFFER_DESCRIPTION,
        price_atomic=PRICE_ATOMIC,
        pay_to=pay_to,
    )


# ─── request parsing ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExpectedMoveRequest:
    """Validated query parameters."""

    asset: str                   # upper-case symbol or ticker
    expiry: str                  # "next" or "YYYY-MM-DD"
    strikes: tuple[float, ...]   # may be empty


def _parse_asset(raw: str | None) -> str:
    """Upper-cased asset symbol; must look like a ticker (letters first, ≤ 10 characters)."""
    asset = (raw or "").strip().upper()
    if not ASSET_PATTERN.match(asset):
        raise ExpectedMoveInputError("asset is required: BTC, ETH, SOL or a US equity ticker such as NVDA")
    return asset


def _parse_expiry(raw: str | None) -> str:
    """"next" (default) or a date in YYYY-MM-DD form."""
    expiry = (raw or EXPIRY_NEXT).strip().lower()
    if expiry == EXPIRY_NEXT:
        return EXPIRY_NEXT
    try:
        datetime.strptime(expiry, EXPIRY_DATE_FORMAT)
    except ValueError as exc:
        raise ExpectedMoveInputError(f"expiry must be 'next' or YYYY-MM-DD, got {raw!r}") from exc
    return expiry


def _parse_strikes(raw: str | None) -> tuple[float, ...]:
    """"100000,110000" → (100000.0, 110000.0); empty → (); each strike must be a positive number."""
    if not raw or not raw.strip():
        return ()
    strikes = []
    for part in raw.split(","):
        text = part.strip()
        if not text:
            continue
        try:
            strike = float(text)
        except ValueError as exc:
            raise ExpectedMoveInputError(f"strike {text!r} is not a number") from exc
        if strike <= 0:
            raise ExpectedMoveInputError(f"strike must be positive, got {text}")
        strikes.append(strike)
    if len(strikes) > MAX_STRIKES:
        raise ExpectedMoveInputError(f"at most {MAX_STRIKES} strikes per request")
    return tuple(strikes)


def parse_request(params: dict) -> ExpectedMoveRequest:
    """Validate `asset`, `expiry` and `strikes` from the query string."""
    return ExpectedMoveRequest(
        asset=_parse_asset(params.get("asset")),
        expiry=_parse_expiry(params.get("expiry")),
        strikes=_parse_strikes(params.get("strikes")),
    )


# ─── shared response pieces ──────────────────────────────────────────────────

def _iso(moment: datetime) -> str:
    """ISO-8601 in UTC with seconds, e.g. "2026-10-11T12:00:00+00:00"."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _move_summary(spot: float, sigma: float, t_years: float) -> dict:
    """Expected move S·σ·√T in USD and as a fraction of S, plus the ±1σ price range."""
    move_usd = expected_move(spot, sigma, t_years)
    return {
        "expected_move_usd": round(move_usd, USD_DECIMALS),
        "expected_move_pct": round(move_usd / spot, SIGMA_DECIMALS),
        "range_1sd": {
            "low": round(spot - move_usd, USD_DECIMALS),
            "high": round(spot + move_usd, USD_DECIMALS),
        },
    }


def _probability_row(spot: float, strike: float, sigma: float, t_years: float) -> dict:
    """P(S_T > K) = N(d2) and its complement for one strike, with the σ used."""
    p_above = prob_above(spot, strike, sigma, t_years)
    return {
        "strike": strike,
        "prob_above": round(p_above, PROBABILITY_DECIMALS),
        "prob_below": round(1.0 - p_above, PROBABILITY_DECIMALS),
        "sigma_used": round(sigma, SIGMA_DECIMALS),
    }


def _time_fields(now: datetime, expiry: datetime) -> dict:
    """Expiry instant, T in years (ACT/365) and the same T in days."""
    t_years = year_fraction(now, expiry)
    return {
        "expiry": _iso(expiry),
        "t_years": round(t_years, YEARS_DECIMALS),
        "days_to_expiry": round(t_years * DAYS_PER_YEAR, DAYS_DECIMALS),
    }


def error_response(params: dict, message: str, kind: str, now: datetime, data_delay: str | None = None) -> dict:
    """A failed request: no numbers, the reason, and confidence 0."""
    response = {
        "error": message,
        "error_kind": kind,
        "inputs_used": {"params": dict(params)},
        "computed_at": _iso(now),
        "method": "none (request failed)",
        "confidence": 0.0,
    }
    if data_delay is not None:
        response["data_delay"] = data_delay
    return response


# ─── crypto (Deribit) ────────────────────────────────────────────────────────

def choose_crypto_expiry(chain: OptionChain, requested: str, now: datetime) -> datetime:
    """
    The expiry instant to price.

    "next" → the first listed Deribit expiry at least MIN_HOURS_TO_EXPIRY after `now`.
    "YYYY-MM-DD" → 08:00 UTC that day (Deribit's expiry time), listed or not — an unlisted
    date is interpolated later — but at least MIN_HOURS_TO_EXPIRY ahead and no later than
    the last listed expiry (beyond it there is nothing to interpolate towards).
    """
    earliest_allowed = now + timedelta(hours=MIN_HOURS_TO_EXPIRY)
    expiries = [expiry for expiry in deribit.listed_expiries(chain, now) if expiry >= earliest_allowed]
    if not expiries:
        raise ExpectedMoveInputError(f"no listed {chain.asset} expiry at least {MIN_HOURS_TO_EXPIRY:.0f}h away")
    if requested == EXPIRY_NEXT:
        return expiries[0]

    requested_date = datetime.strptime(requested, EXPIRY_DATE_FORMAT).date()
    target = datetime(
        requested_date.year, requested_date.month, requested_date.day,
        hour=deribit.DERIBIT_EXPIRY_HOUR_UTC, tzinfo=timezone.utc,
    )
    if target < earliest_allowed:
        raise ExpectedMoveInputError(f"expiry {requested} is in the past or less than {MIN_HOURS_TO_EXPIRY:.0f}h away")
    if target > expiries[-1]:
        raise ExpectedMoveInputError(
            f"expiry {requested} is after the last listed {chain.asset} expiry {expiries[-1].date().isoformat()}"
        )
    return target


def _crypto_strike_row(chain: OptionChain, expiry: datetime, strike: float, t_years: float, now: datetime) -> dict:
    """Probability row for one strike, with σ read at the listed strike nearest K."""
    vol = deribit.implied_vol_at(chain, expiry, strike, now)
    row = _probability_row(chain.index_price, strike, vol.sigma, t_years)
    row["sigma_instruments"] = [name for point in vol.points for name in point.instruments]
    return row


def build_crypto_response(request: ExpectedMoveRequest, chain: OptionChain, now: datetime) -> dict:
    """The full expected_move answer for BTC/ETH/SOL from a Deribit chain (pure)."""
    expiry = choose_crypto_expiry(chain, request.expiry, now)
    atm_vol = deribit.implied_vol_at(chain, expiry, chain.index_price, now)
    t_years = year_fraction(now, expiry)
    return {
        "asset": chain.asset,
        "asset_class": ASSET_CLASS_CRYPTO,
        "spot": round(chain.index_price, USD_DECIMALS),
        **_time_fields(now, expiry),
        "sigma_atm": round(atm_vol.sigma, SIGMA_DECIMALS),
        "sigma_method": atm_vol.method,
        "atm_strikes": [point.strike for point in atm_vol.points],
        **_move_summary(chain.index_price, atm_vol.sigma, t_years),
        "strikes": [_crypto_strike_row(chain, expiry, strike, t_years, now) for strike in request.strikes],
        "inputs_used": {
            "source": "deribit",
            "index_name": chain.index_name,
            "index_price": chain.index_price,
            "index_as_of": _iso(chain.as_of),
            "atm_instruments": [name for point in atm_vol.points for name in point.instruments],
            "options_in_chain": len(chain.quotes),
            "requested_expiry": request.expiry,
            "requested_strikes": list(request.strikes),
        },
        "computed_at": _iso(now),
        "method": CRYPTO_METHOD,
        "confidence": CRYPTO_CONFIDENCE_BY_VOL_METHOD[atm_vol.method],
        "data_delay": DERIBIT_DATA_DELAY,
    }


# ─── equities (yfinance) ─────────────────────────────────────────────────────

def _atm_pair(calls: list[EquityOptionQuote], puts: list[EquityOptionQuote], spot: float) -> tuple[EquityOptionQuote, EquityOptionQuote]:
    """The call and put at the strike nearest spot among strikes listed for both (ties → lower)."""
    common_strikes = sorted({call.strike for call in calls} & {put.strike for put in puts})
    if not common_strikes:
        raise EquityDataError("no strike is listed for both calls and puts")
    atm_strike = min(common_strikes, key=lambda strike: abs(strike - spot))
    call = next(quote for quote in calls if quote.strike == atm_strike)
    put = next(quote for quote in puts if quote.strike == atm_strike)
    return call, put


def _mean_plausible_iv(quotes: list[EquityOptionQuote]) -> float | None:
    """Mean of the plausible implied volatilities among `quotes`; None if there are none."""
    ivs = [quote.implied_volatility for quote in quotes if yfin.is_plausible_iv(quote.implied_volatility)]
    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def _equity_sigma_at_strike(
    calls: list[EquityOptionQuote], puts: list[EquityOptionQuote], strike: float, sigma_atm: float | None
) -> float | None:
    """σ_K: plausible IV at the listed strike nearest K (call/put mean), else σ_ATM, else None."""
    all_quotes = calls + puts
    nearest_strike = min({quote.strike for quote in all_quotes}, key=lambda listed: abs(listed - strike))
    at_strike = [quote for quote in all_quotes if quote.strike == nearest_strike]
    sigma = _mean_plausible_iv(at_strike)
    return sigma if sigma is not None else sigma_atm


def _equity_strike_row(
    calls: list[EquityOptionQuote], puts: list[EquityOptionQuote], spot: float, strike: float,
    t_years: float, sigma_atm: float | None,
) -> dict:
    """Probability row for one strike, or a row explaining why no probability is given."""
    sigma = _equity_sigma_at_strike(calls, puts, strike, sigma_atm)
    if sigma is None:
        return {
            "strike": strike,
            "prob_above": None,
            "prob_below": None,
            "sigma_used": None,
            "note": "no plausible implied volatility from yfinance",
        }
    return _probability_row(spot, strike, sigma, t_years)


def _equity_confidence(price_basis: str, sigma_atm: float | None) -> float:
    """0.6 for a delayed unofficial source, −0.15 if priced off last trades, −0.2 if σ unusable."""
    confidence = EQUITY_BASE_CONFIDENCE
    if price_basis == yfin.PRICE_BASIS_LAST:
        confidence -= EQUITY_LAST_TRADE_PENALTY
    if sigma_atm is None:
        confidence -= EQUITY_NO_SIGMA_PENALTY
    return round(max(MIN_CONFIDENCE, confidence), 2)


def _atm_mids(call: EquityOptionQuote, put: EquityOptionQuote) -> tuple[float, float, str]:
    """Call mid, put mid and the weaker of their two price bases."""
    call_price = yfin.mid_price(call)
    put_price = yfin.mid_price(put)
    if call_price is None or put_price is None:
        raise EquityDataError(f"no usable price for the ATM call/put at strike {call.strike}")
    both_mid = call_price[1] == yfin.PRICE_BASIS_MID and put_price[1] == yfin.PRICE_BASIS_MID
    basis = yfin.PRICE_BASIS_MID if both_mid else yfin.PRICE_BASIS_LAST
    return call_price[0], put_price[0], basis


def build_equity_response(request: ExpectedMoveRequest, snapshot: dict, now: datetime) -> dict:
    """The full expected_move answer for a US equity from a yfinance snapshot (pure)."""
    spot = float(snapshot["spot"])
    calls = yfin.parse_option_rows(snapshot["calls"])
    puts = yfin.parse_option_rows(snapshot["puts"])
    expiry = yfin.equity_expiry_datetime(snapshot["expiry"])
    t_years = year_fraction(now, expiry)

    atm_call, atm_put = _atm_pair(calls, puts, spot)
    call_mid, put_mid, price_basis = _atm_mids(atm_call, atm_put)
    straddle_pct = straddle_implied_move(call_mid, put_mid, spot)
    sigma_atm = _mean_plausible_iv([atm_call, atm_put])

    notes = []
    move_fields = {"expected_move_usd": None, "expected_move_pct": None, "range_1sd": None}
    if sigma_atm is None:
        notes.append("yfinance ATM impliedVolatility is implausible; σ-based numbers omitted, straddle move kept")
    else:
        move_fields = _move_summary(spot, sigma_atm, t_years)

    return {
        "asset": request.asset,
        "asset_class": ASSET_CLASS_EQUITY,
        "spot": round(spot, USD_DECIMALS),
        **_time_fields(now, expiry),
        "atm_strike": atm_call.strike,
        "call_mid": round(call_mid, USD_DECIMALS),
        "put_mid": round(put_mid, USD_DECIMALS),
        "price_basis": price_basis,
        "straddle_move_usd": round(straddle_pct * spot, USD_DECIMALS),
        "straddle_move_pct": round(straddle_pct, SIGMA_DECIMALS),
        "sigma_atm": None if sigma_atm is None else round(sigma_atm, SIGMA_DECIMALS),
        **move_fields,
        "strikes": [
            _equity_strike_row(calls, puts, spot, strike, t_years, sigma_atm) for strike in request.strikes
        ],
        "notes": notes,
        "inputs_used": {
            "source": "yfinance",
            "contracts": [atm_call.contract_symbol, atm_put.contract_symbol],
            "listed_expiries": snapshot.get("expiries", []),
            "fetched_at": snapshot.get("fetched_at"),
            "requested_expiry": request.expiry,
            "requested_strikes": list(request.strikes),
        },
        "computed_at": _iso(now),
        "method": EQUITY_METHOD,
        "confidence": _equity_confidence(price_basis, sigma_atm),
        "data_delay": yfin.YFINANCE_DELAY_NOTE,
    }


# ─── entry point (async, network) ────────────────────────────────────────────

def _utc_now() -> datetime:
    """The current time in UTC (one place, so the whole response uses one clock reading)."""
    return datetime.now(timezone.utc)


async def _compute_crypto(params: dict, request: ExpectedMoveRequest, now: datetime) -> dict:
    """Fetch the Deribit chain and build the crypto answer; upstream/data failures → error dict."""
    try:
        chain = await deribit.fetch_option_chain(request.asset)
        return build_crypto_response(request, chain, now)
    except UpstreamError as exc:
        return error_response(params, str(exc), ERROR_UPSTREAM, now, DERIBIT_DATA_DELAY)
    except ExpectedMoveInputError as exc:
        return error_response(params, str(exc), ERROR_BAD_REQUEST, now, DERIBIT_DATA_DELAY)
    except deribit.DeribitDataError as exc:
        return error_response(params, str(exc), ERROR_NO_DATA, now, DERIBIT_DATA_DELAY)


async def _compute_equity(params: dict, request: ExpectedMoveRequest, now: datetime) -> dict:
    """Fetch the yfinance chain and build the equity answer; any yfinance failure → error dict."""
    try:
        snapshot = await yfin.fetch_option_snapshot(request.asset, request.expiry, now, MIN_HOURS_TO_EXPIRY)
        return build_equity_response(request, snapshot, now)
    except yfin.YFinanceError as exc:
        return error_response(params, str(exc), ERROR_UPSTREAM, now, yfin.YFINANCE_DELAY_NOTE)
    except EquityDataError as exc:
        return error_response(params, str(exc), ERROR_NO_DATA, now, yfin.YFINANCE_DELAY_NOTE)


async def compute(params: dict) -> dict:
    """
    Answer one paid request. BTC/ETH/SOL go to Deribit, anything else is treated as a US
    equity ticker and goes to yfinance. Never raises for bad input or upstream failure:
    those come back as {"error", "error_kind", ...} with confidence 0.
    """
    now = _utc_now()
    try:
        request = parse_request(params)
    except ExpectedMoveInputError as exc:
        return error_response(params, str(exc), ERROR_BAD_REQUEST, now)
    if deribit.is_supported_asset(request.asset):
        return await _compute_crypto(params, request, now)
    return await _compute_equity(params, request, now)
