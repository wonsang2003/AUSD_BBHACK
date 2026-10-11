"""
Option math shared by the `expected_move` and `pm_edge` sellers (SPEC §4) and by
the belief updates in `app/agents/beliefs.py`.

Everything here is pure: numbers in, numbers out, no network. The model is plain
Black-Scholes with zero rates (r = 0), which is how crypto option desks quote
"expected move" and "probability above strike":

    expected move          = S · σ · √T
    P(S_T > K)             = N(d2),  d2 = (ln(S/K) − σ²T/2) / (σ√T)
    total variance         w(T) = σ² · T          (interpolated linearly in T)
    straddle implied move  = (ATM call mid + ATM put mid) / S
    Gemini taker fee       = ceil_to_cent(0.07 · C · P · (1 − P))

Units: S and K in USD, σ as a decimal annualised volatility (0.55 = 55 %, so
Deribit's `mark_iv` in percent must be divided by 100 first), T in years ACT/365.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal

# ─── constants ────────────────────────────────────────────────────────────────

DAYS_PER_YEAR = 365.0          # ACT/365: every calendar day counts, 365 per year
SECONDS_PER_DAY = 86_400
SECONDS_PER_YEAR = DAYS_PER_YEAR * SECONDS_PER_DAY

SQRT_TWO = math.sqrt(2.0)
SQRT_TWO_PI = math.sqrt(2.0 * math.pi)

# Probability that the underlying finishes above the strike when there is no time
# or no volatility left and spot sits exactly on the strike. 0.5 is the limit of
# N(d2) as σ√T → 0 with S = K, so the function stays continuous.
AT_THE_MONEY_DEGENERATE_PROBABILITY = 0.5

# Gemini Predictions taker fee: 7 % of C · P · (1 − P), rounded *up* to the cent.
GEMINI_TAKER_FEE_RATE = Decimal("0.07")
ONE_CENT = Decimal("0.01")


# ─── normal distribution ─────────────────────────────────────────────────────

def norm_pdf(x: float) -> float:
    """Standard normal density φ(x) = exp(−x²/2) / √(2π)."""
    return math.exp(-0.5 * x * x) / SQRT_TWO_PI


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution Φ(x) = ½ · (1 + erf(x / √2))."""
    return 0.5 * (1.0 + math.erf(x / SQRT_TWO))


# ─── time ────────────────────────────────────────────────────────────────────

def _as_utc(moment: datetime) -> datetime:
    """Treat a naive datetime as UTC; convert an aware one to UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def year_fraction(now: datetime, expiry: datetime) -> float:
    """
    Time to expiry in years, ACT/365: (expiry − now) in seconds / (365 · 86 400).

    Naive datetimes are read as UTC. An expiry in the past returns 0.0 (the
    option has no time value left), never a negative number.
    """
    seconds_left = (_as_utc(expiry) - _as_utc(now)).total_seconds()
    return max(0.0, seconds_left / SECONDS_PER_YEAR)


# ─── Black-Scholes (r = 0) ───────────────────────────────────────────────────

def _require_non_negative(name: str, value: float) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")


def _require_positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")


def expected_move(spot: float, sigma: float, t_years: float) -> float:
    """
    One-standard-deviation move in USD until expiry: S · σ · √T.

    Example: S = 100 000, σ = 0.50, T = 0.25 → 100 000 · 0.5 · 0.5 = 25 000.
    """
    _require_positive("spot", spot)
    _require_non_negative("sigma", sigma)
    _require_non_negative("t_years", t_years)
    return spot * sigma * math.sqrt(t_years)


def d2(spot: float, strike: float, sigma: float, t_years: float) -> float:
    """
    Black-Scholes d2 with r = 0: (ln(S/K) − σ²T/2) / (σ√T).

    Requires σ√T > 0; `prob_above` handles the σ√T = 0 case itself.
    """
    _require_positive("spot", spot)
    _require_positive("strike", strike)
    stdev = sigma * math.sqrt(t_years)
    _require_positive("sigma * sqrt(t_years)", stdev)
    return (math.log(spot / strike) - 0.5 * stdev * stdev) / stdev


def prob_above(spot: float, strike: float, sigma: float, t_years: float) -> float:
    """
    Risk-neutral probability that the underlying finishes above the strike: N(d2).

    With no time or no volatility left (σ√T = 0) the outcome is already known:
    1.0 if S > K, 0.0 if S < K, and 0.5 if S == K (the continuous limit).
    """
    _require_positive("spot", spot)
    _require_positive("strike", strike)
    _require_non_negative("sigma", sigma)
    _require_non_negative("t_years", t_years)
    if sigma * math.sqrt(t_years) == 0.0:
        if spot > strike:
            return 1.0
        if spot < strike:
            return 0.0
        return AT_THE_MONEY_DEGENERATE_PROBABILITY
    return norm_cdf(d2(spot, strike, sigma, t_years))


def prob_below(spot: float, strike: float, sigma: float, t_years: float) -> float:
    """Probability that the underlying finishes below the strike: 1 − N(d2)."""
    return 1.0 - prob_above(spot, strike, sigma, t_years)


# ─── volatility term structure ───────────────────────────────────────────────

def interpolate_total_variance(
    t_years: float,
    near: tuple[float, float],
    far: tuple[float, float],
) -> float:
    """
    Implied volatility at `t_years` from two bracketing expiries `(t1, σ1)`, `(t2, σ2)`.

    Rule: total variance w = σ²·T is linear in time between the two expiries,
        w(t) = σ1²·t1 + (t − t1) / (t2 − t1) · (σ2²·t2 − σ1²·t1),   σ(t) = √(w(t) / t).

    Outside the bracket the nearest volatility is held flat (σ1 before t1, σ2 after
    t2), because extrapolating variance linearly can turn negative. The two points
    may be given in either order; if both expiries are equal, σ1 is returned.
    """
    _require_positive("t_years", t_years)
    (t1, sigma1), (t2, sigma2) = sorted([near, far])
    _require_positive("t1", t1)
    if t_years <= t1 or t1 == t2:
        return sigma1
    if t_years >= t2:
        return sigma2
    variance1 = sigma1 * sigma1 * t1
    variance2 = sigma2 * sigma2 * t2
    weight = (t_years - t1) / (t2 - t1)
    total_variance = variance1 + weight * (variance2 - variance1)
    return math.sqrt(total_variance / t_years)


# ─── equities: straddle ──────────────────────────────────────────────────────

def straddle_implied_move(call_mid: float, put_mid: float, spot: float) -> float:
    """
    Straddle-implied move as a fraction of spot: (ATM call mid + ATM put mid) / S.

    Example: call 4.10, put 3.90, S = 200 → 8.00 / 200 = 0.04 (a ±4 % move).
    Multiply by S for the move in dollars.
    """
    _require_non_negative("call_mid", call_mid)
    _require_non_negative("put_mid", put_mid)
    _require_positive("spot", spot)
    return (call_mid + put_mid) / spot


# ─── prediction markets: Gemini fee ──────────────────────────────────────────

def gemini_taker_fee(contracts: int, price: float | str | Decimal) -> float:
    """
    Gemini Predictions taker fee in USD for `contracts` at `price` (0–1 per contract):

        fee = ceil_to_cent(0.07 · C · P · (1 − P))

    Computed in Decimal so that e.g. 1.75 does not become 1.7500000000000002 and
    round up to 1.76. `price` may be the API's string ("0.45") or a number.
    """
    if contracts < 0:
        raise ValueError(f"contracts must be >= 0, got {contracts}")
    price_decimal = Decimal(str(price))
    if price_decimal < 0 or price_decimal > 1:
        raise ValueError(f"price must be within [0, 1], got {price}")
    raw_fee = GEMINI_TAKER_FEE_RATE * contracts * price_decimal * (1 - price_decimal)
    return float(raw_fee.quantize(ONE_CENT, rounding=ROUND_CEILING))
