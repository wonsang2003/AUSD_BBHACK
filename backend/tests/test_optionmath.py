"""
Tests for `app/sellers/optionmath.py` — Black-Scholes (r = 0) helpers, ACT/365 time,
total-variance interpolation, straddle move and the Gemini taker fee.

Expected values are worked out by hand in each test (standard normal table values
such as Φ(1) = 0.841345 and inputs chosen so that d2 is a round number).
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.sellers.optionmath import (
    expected_move,
    gemini_taker_fee,
    interpolate_total_variance,
    norm_cdf,
    norm_pdf,
    prob_above,
    prob_below,
    straddle_implied_move,
    year_fraction,
)

TOLERANCE = 1e-9
NEW_YEAR_2026 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ─── normal distribution ─────────────────────────────────────────────────────

def test_norm_pdf_at_zero_is_one_over_sqrt_two_pi():
    # Arrange
    expected = 1 / math.sqrt(2 * math.pi)  # 0.398942...

    # Act
    density = norm_pdf(0.0)

    # Assert
    assert density == pytest.approx(expected, abs=TOLERANCE)


def test_norm_pdf_is_symmetric():
    # Arrange / Act
    left, right = norm_pdf(-1.3), norm_pdf(1.3)

    # Assert
    assert left == pytest.approx(right, abs=TOLERANCE)


def test_norm_cdf_matches_standard_table_values():
    # Arrange: values from a standard normal table
    table = {0.0: 0.5, 1.0: 0.8413447461, -1.0: 0.1586552539, 1.96: 0.9750021049}

    # Act / Assert
    for x, expected in table.items():
        assert norm_cdf(x) == pytest.approx(expected, abs=TOLERANCE)


# ─── time ────────────────────────────────────────────────────────────────────

def test_year_fraction_counts_73_days_as_one_fifth_of_a_year():
    # Arrange: ACT/365 → 73 / 365 = 0.2
    expiry = NEW_YEAR_2026 + timedelta(days=73)

    # Act
    t_years = year_fraction(NEW_YEAR_2026, expiry)

    # Assert
    assert t_years == pytest.approx(0.2, abs=TOLERANCE)


def test_year_fraction_is_zero_after_expiry():
    # Arrange
    expiry = NEW_YEAR_2026 - timedelta(hours=1)

    # Act
    t_years = year_fraction(NEW_YEAR_2026, expiry)

    # Assert
    assert t_years == 0.0


def test_year_fraction_treats_naive_datetimes_as_utc():
    # Arrange
    naive_now = datetime(2026, 1, 1)
    aware_expiry = NEW_YEAR_2026 + timedelta(days=365)

    # Act
    t_years = year_fraction(naive_now, aware_expiry)

    # Assert
    assert t_years == pytest.approx(1.0, abs=TOLERANCE)


# ─── expected move ───────────────────────────────────────────────────────────

def test_expected_move_is_spot_times_sigma_times_root_t():
    # Arrange: 100 000 · 0.5 · √0.25 = 25 000
    spot, sigma, t_years = 100_000.0, 0.5, 0.25

    # Act
    move = expected_move(spot, sigma, t_years)

    # Assert
    assert move == pytest.approx(25_000.0, abs=TOLERANCE)


def test_expected_move_rejects_negative_volatility():
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        expected_move(100.0, -0.1, 1.0)


# ─── probability above strike ────────────────────────────────────────────────

def test_prob_above_at_the_money_is_n_of_minus_half_sigma_root_t():
    # Arrange: S = K, σ = 0.2, T = 1 → d2 = (0 − 0.02) / 0.2 = −0.1, Φ(−0.1) = 0.4601721627
    spot = strike = 100.0

    # Act
    probability = prob_above(spot, strike, sigma=0.2, t_years=1.0)

    # Assert
    assert probability == pytest.approx(0.4601721627, abs=TOLERANCE)


def test_prob_above_equals_n_of_one_when_d2_is_one():
    # Arrange: σ = 0.2, T = 1, ln(S/K) = 0.22 → d2 = (0.22 − 0.02) / 0.2 = 1, Φ(1) = 0.8413447461
    strike = 100.0
    spot = strike * math.exp(0.22)

    # Act
    probability = prob_above(spot, strike, sigma=0.2, t_years=1.0)

    # Assert
    assert probability == pytest.approx(0.8413447461, abs=TOLERANCE)


def test_prob_above_and_prob_below_sum_to_one():
    # Arrange
    spot, strike, sigma, t_years = 64_000.0, 70_000.0, 0.55, 30 / 365

    # Act
    total = prob_above(spot, strike, sigma, t_years) + prob_below(spot, strike, sigma, t_years)

    # Assert
    assert total == pytest.approx(1.0, abs=TOLERANCE)


def test_prob_above_at_expiry_is_certain_when_spot_is_above_strike():
    # Arrange / Act
    in_the_money = prob_above(110.0, 100.0, sigma=0.5, t_years=0.0)
    out_of_the_money = prob_above(90.0, 100.0, sigma=0.5, t_years=0.0)
    on_the_strike = prob_above(100.0, 100.0, sigma=0.5, t_years=0.0)

    # Assert
    assert (in_the_money, out_of_the_money, on_the_strike) == (1.0, 0.0, 0.5)


def test_prob_above_rejects_non_positive_strike():
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        prob_above(100.0, 0.0, sigma=0.5, t_years=1.0)


# ─── total-variance interpolation ────────────────────────────────────────────

def test_interpolate_total_variance_midpoint_matches_hand_computation():
    # Arrange: w1 = 0.5²·0.1 = 0.025, w2 = 0.6²·0.3 = 0.108
    #          w(0.2) = 0.025 + 0.5·(0.108 − 0.025) = 0.0665 → σ = √(0.0665/0.2) = √0.3325
    near, far = (0.1, 0.5), (0.3, 0.6)

    # Act
    sigma = interpolate_total_variance(0.2, near, far)

    # Assert
    assert sigma == pytest.approx(math.sqrt(0.3325), abs=TOLERANCE)


def test_interpolate_total_variance_returns_input_vol_at_each_expiry():
    # Arrange
    near, far = (0.1, 0.5), (0.3, 0.6)

    # Act
    at_near = interpolate_total_variance(0.1, near, far)
    at_far = interpolate_total_variance(0.3, near, far)

    # Assert
    assert (at_near, at_far) == (pytest.approx(0.5), pytest.approx(0.6))


def test_interpolate_total_variance_accepts_points_in_either_order():
    # Arrange
    near, far = (0.1, 0.5), (0.3, 0.6)

    # Act
    forward = interpolate_total_variance(0.2, near, far)
    reversed_order = interpolate_total_variance(0.2, far, near)

    # Assert
    assert forward == pytest.approx(reversed_order, abs=TOLERANCE)


def test_interpolate_total_variance_holds_vol_flat_outside_the_bracket():
    # Arrange
    near, far = (0.1, 0.5), (0.3, 0.6)

    # Act
    before = interpolate_total_variance(0.05, near, far)
    after = interpolate_total_variance(1.0, near, far)

    # Assert
    assert (before, after) == (0.5, 0.6)


# ─── straddle ────────────────────────────────────────────────────────────────

def test_straddle_implied_move_is_premium_over_spot():
    # Arrange: (4.10 + 3.90) / 200 = 0.04
    call_mid, put_mid, spot = 4.10, 3.90, 200.0

    # Act
    move = straddle_implied_move(call_mid, put_mid, spot)

    # Assert
    assert move == pytest.approx(0.04, abs=TOLERANCE)


# ─── Gemini fee ──────────────────────────────────────────────────────────────

def test_gemini_taker_fee_exact_cent_amount_is_not_rounded_up():
    # Arrange: 0.07 · 100 · 0.5 · 0.5 = 1.75 exactly (float math would give 1.7500000000000002 → 1.76)
    contracts, price = 100, 0.5

    # Act
    fee = gemini_taker_fee(contracts, price)

    # Assert
    assert fee == 1.75


def test_gemini_taker_fee_rounds_fractional_cents_up():
    # Arrange: 0.07 · 10 · 0.45 · 0.55 = 0.17325 → 0.18
    contracts, price = 10, "0.45"

    # Act
    fee = gemini_taker_fee(contracts, price)

    # Assert
    assert fee == 0.18


def test_gemini_taker_fee_charges_at_least_one_cent_for_a_tiny_order():
    # Arrange: 0.07 · 1 · 0.99 · 0.01 = 0.000693 → 0.01
    contracts, price = 1, 0.99

    # Act
    fee = gemini_taker_fee(contracts, price)

    # Assert
    assert fee == 0.01


def test_gemini_taker_fee_is_zero_for_zero_contracts():
    # Arrange / Act
    fee = gemini_taker_fee(0, 0.5)

    # Assert
    assert fee == 0.0


def test_gemini_taker_fee_rejects_price_outside_zero_one():
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        gemini_taker_fee(10, 1.2)
