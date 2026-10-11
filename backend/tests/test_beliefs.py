"""
Tests for `app/agents/beliefs.py` — the Belief record, the per-seller update rules,
clamping to [0.01, 0.99], and the threshold / significant-move checks.

Each expected number is worked out by hand from the rule in the module docstring.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.beliefs import (
    P_MAX,
    P_MIN,
    Belief,
    BeliefUpdate,
    above_strike_decision,
    apply_update,
    clamp_probability,
    crossed_threshold,
    decision_qualifier,
    decision_type,
    index_beliefs,
    is_significant_move,
    new_belief,
    update_from_expected_move,
    update_from_insider_cluster,
    update_from_macro_pulse,
    update_from_news_sentiment,
    update_from_perps_crowding,
    update_from_pm_edge,
)

TOLERANCE = 1e-9
NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)


# ─── Belief record and decision strings ──────────────────────────────────────

def test_belief_key_joins_agent_asset_and_decision():
    # Arrange
    belief = new_belief("OptionsEvent", "BTC", "above:100000", stake_usd=500.0, now=NOW)

    # Act
    key = belief.key

    # Assert
    assert key == "OptionsEvent:BTC:above:100000"


def test_new_belief_starts_undecided_at_one_half():
    # Arrange / Act
    belief = new_belief("NewsSentiment", "NVDA", "direction", stake_usd=1_000.0, now=NOW)

    # Assert
    assert (belief.p, belief.tau, belief.updated_at) == (0.5, 0.5, NOW)


def test_above_strike_decision_never_uses_scientific_notation():
    # Arrange / Act
    round_strike = above_strike_decision(1_000_000.0)
    fractional_strike = above_strike_decision(2.5)

    # Assert
    assert (round_strike, fractional_strike) == ("above:1000000", "above:2.5")


def test_decision_type_and_qualifier_split_on_first_colon():
    # Arrange / Act / Assert
    assert decision_type("above:100000") == "above"
    assert decision_qualifier("above:100000") == "100000"
    assert decision_type("direction") == "direction"
    assert decision_qualifier("direction") is None


def test_index_beliefs_maps_keys_to_beliefs():
    # Arrange
    first = new_belief("OnchainPerps", "BTC", "direction", 100.0, NOW)
    second = new_belief("OnchainPerps", "ETH", "direction", 100.0, NOW)

    # Act
    index = index_beliefs([first, second])

    # Assert
    assert index == {first.key: first, second.key: second}


def test_apply_update_replaces_p_and_timestamp_only():
    # Arrange
    belief = new_belief("OnchainPerps", "BTC", "direction", 250.0, NOW)
    later = NOW + timedelta(minutes=5)
    update = BeliefUpdate(new_p=0.62, delta_p=0.12, explanation="test")

    # Act
    updated = apply_update(belief, update, later)

    # Assert
    assert (updated.p, updated.updated_at, updated.stake_usd, updated.tau) == (0.62, later, 250.0, 0.5)


# ─── clamping ────────────────────────────────────────────────────────────────

def test_clamp_probability_keeps_values_inside_one_percent_bounds():
    # Arrange / Act / Assert
    assert clamp_probability(-0.3) == P_MIN
    assert clamp_probability(1.7) == P_MAX
    assert clamp_probability(0.42) == 0.42


# ─── pm_edge ─────────────────────────────────────────────────────────────────

def test_pm_edge_sets_p_to_half_plus_net_edge():
    # Arrange: edge = 0.62 − 0.55 = 0.07, minus fee 0.02 → net 0.05 → p = 0.55
    p_old = 0.5

    # Act
    new_p, delta_p, explanation = update_from_pm_edge(p_old, model_prob=0.62, market_prob=0.55, fee_per_contract=0.02)

    # Assert
    assert new_p == pytest.approx(0.55, abs=TOLERANCE)
    assert delta_p == pytest.approx(0.05, abs=TOLERANCE)
    assert "pm_edge" in explanation


def test_pm_edge_negative_edge_favours_no():
    # Arrange: model 0.40 vs market 0.50 → net −0.10 → p = 0.40
    p_old = 0.5

    # Act
    update = update_from_pm_edge(p_old, model_prob=0.40, market_prob=0.50)

    # Assert
    assert update.new_p == pytest.approx(0.40, abs=TOLERANCE)


def test_pm_edge_smaller_than_fee_leaves_no_edge():
    # Arrange: edge 0.02 < fee 0.03 → net 0 → p = 0.5
    p_old = 0.7

    # Act
    update = update_from_pm_edge(p_old, model_prob=0.52, market_prob=0.50, fee_per_contract=0.03)

    # Assert
    assert update.new_p == pytest.approx(0.5, abs=TOLERANCE)
    assert update.delta_p == pytest.approx(-0.2, abs=TOLERANCE)


def test_pm_edge_is_clamped_to_ninety_nine_percent():
    # Arrange: edge 1.0 → 1.5 before clamping
    p_old = 0.5

    # Act
    update = update_from_pm_edge(p_old, model_prob=1.0, market_prob=0.0)

    # Assert
    assert update.new_p == P_MAX


# ─── perps_crowding ──────────────────────────────────────────────────────────

def test_perps_crowding_zero_z_leaves_p_unchanged():
    # Arrange / Act
    update = update_from_perps_crowding(0.6, crowding_z=0.0)

    # Assert
    assert (update.new_p, update.delta_p) == (pytest.approx(0.6), pytest.approx(0.0))


def test_perps_crowding_crowded_long_lowers_p_by_logistic_of_z():
    # Arrange: z = 2 → 2·logistic(2) − 1 = tanh(1) = 0.761594 → shift −0.0761594
    expected_shift = -0.10 * math.tanh(1.0)

    # Act
    update = update_from_perps_crowding(0.5, crowding_z=2.0)

    # Assert
    assert update.delta_p == pytest.approx(expected_shift, abs=TOLERANCE)


def test_perps_crowding_crowded_short_raises_p_but_never_more_than_ten_points():
    # Arrange / Act
    update = update_from_perps_crowding(0.5, crowding_z=-50.0)

    # Assert
    assert 0.0 < update.delta_p <= 0.10


# ─── news_sentiment ──────────────────────────────────────────────────────────

def test_news_sentiment_combines_tone_and_event_flags():
    # Arrange: tone 2.5 → +0.05; one bearish flag (hack) → −0.03; total +0.02
    p_old = 0.5

    # Act
    update = update_from_news_sentiment(p_old, tone_delta=2.5, event_flags=["hack"])

    # Assert
    assert update.new_p == pytest.approx(0.52, abs=TOLERANCE)


def test_news_sentiment_total_shift_is_capped_at_fifteen_points():
    # Arrange: tone 10 → capped +0.10; ETF + listing → +0.06; total 0.16 → capped 0.15
    p_old = 0.5

    # Act
    update = update_from_news_sentiment(p_old, tone_delta=10.0, event_flags=["ETF", "Listing"])

    # Assert
    assert update.new_p == pytest.approx(0.65, abs=TOLERANCE)


def test_news_sentiment_ignores_ambiguous_guidance_flag():
    # Arrange
    p_old = 0.5

    # Act
    update = update_from_news_sentiment(p_old, tone_delta=0.0, event_flags=["guidance"])

    # Assert
    assert update.delta_p == pytest.approx(0.0, abs=TOLERANCE)


# ─── expected_move ───────────────────────────────────────────────────────────

def test_expected_move_sets_p_to_n_of_d2():
    # Arrange: σ = 0.2, T = 1, ln(S/K) = 0.22 → d2 = 1 → Φ(1) = 0.8413447461
    strike = 100_000.0
    spot = strike * math.exp(0.22)

    # Act
    update = update_from_expected_move(0.5, spot=spot, strike=strike, sigma=0.2, t_years=1.0)

    # Assert
    assert update.new_p == pytest.approx(0.8413447461, abs=TOLERANCE)
    assert update.delta_p == pytest.approx(0.3413447461, abs=TOLERANCE)


# ─── macro_pulse ─────────────────────────────────────────────────────────────

def test_macro_pulse_accelerating_inflation_lowers_p():
    # Arrange: ΔCPI +0.3pp → tightening 0.03 → p 0.50 → 0.47
    p_old = 0.5

    # Act
    update = update_from_macro_pulse(p_old, cpi_yoy_change_pts=0.3)

    # Assert
    assert update.new_p == pytest.approx(0.47, abs=TOLERANCE)


def test_macro_pulse_shift_is_capped_at_eight_points():
    # Arrange: 0.10·1.0 + 0.05·0.5 = 0.125 → capped 0.08
    p_old = 0.5

    # Act
    update = update_from_macro_pulse(p_old, cpi_yoy_change_pts=1.0, fed_funds_change_pts=0.5)

    # Assert
    assert update.new_p == pytest.approx(0.42, abs=TOLERANCE)


# ─── insider_cluster ─────────────────────────────────────────────────────────

def test_insider_cluster_single_buyer_is_not_a_cluster():
    # Arrange / Act
    update = update_from_insider_cluster(0.5, distinct_insiders=1, total_purchase_usd=2_000_000.0)

    # Assert
    assert update.delta_p == 0.0


def test_insider_cluster_three_buyers_add_five_points():
    # Arrange: 0.04 + 0.01·(3 − 2) = 0.05; $500k is below the $1M bonus
    p_old = 0.5

    # Act
    update = update_from_insider_cluster(p_old, distinct_insiders=3, total_purchase_usd=500_000.0)

    # Assert
    assert update.new_p == pytest.approx(0.55, abs=TOLERANCE)


def test_insider_cluster_shift_is_capped_at_ten_points():
    # Arrange: 0.04 + 0.08 + 0.02 = 0.14 → capped 0.10
    p_old = 0.5

    # Act
    update = update_from_insider_cluster(p_old, distinct_insiders=10, total_purchase_usd=5_000_000.0)

    # Assert
    assert update.new_p == pytest.approx(0.60, abs=TOLERANCE)


def test_insider_cluster_near_top_is_clamped_and_delta_reflects_clamp():
    # Arrange
    p_old = 0.98

    # Act
    update = update_from_insider_cluster(p_old, distinct_insiders=10, total_purchase_usd=5_000_000.0)

    # Assert
    assert update.new_p == P_MAX
    assert update.delta_p == pytest.approx(0.01, abs=TOLERANCE)


# ─── threshold crossing and significant moves ────────────────────────────────

def test_crossed_threshold_detects_moving_from_below_to_above_tau():
    # Arrange / Act / Assert
    assert crossed_threshold(0.45, 0.55, tau=0.5) is True
    assert crossed_threshold(0.55, 0.45, tau=0.5) is True


def test_crossed_threshold_is_false_when_staying_on_one_side():
    # Arrange / Act / Assert
    assert crossed_threshold(0.55, 0.60, tau=0.5) is False
    assert crossed_threshold(0.30, 0.45, tau=0.5) is False


def test_landing_exactly_on_tau_counts_as_not_acting():
    # Arrange / Act / Assert
    assert crossed_threshold(0.55, 0.50, tau=0.5) is True
    assert crossed_threshold(0.50, 0.45, tau=0.5) is False


def test_is_significant_move_is_strictly_greater_than_delta():
    # Arrange / Act / Assert
    assert is_significant_move(0.06) is True
    assert is_significant_move(-0.06) is True
    assert is_significant_move(0.04) is False
    assert is_significant_move(0.05) is False


def test_is_significant_move_accepts_a_custom_delta():
    # Arrange / Act / Assert
    assert is_significant_move(0.03, delta=0.02) is True


def test_belief_is_immutable():
    # Arrange
    belief = Belief("OnchainPerps", "BTC", "direction", 0.5, 0.5, 100.0, NOW)

    # Act / Assert
    with pytest.raises(AttributeError):
        belief.p = 0.9  # type: ignore[misc]
