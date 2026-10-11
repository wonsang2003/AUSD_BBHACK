"""
Tests for `app/agents/voi.py` — closed-form EVOI (checked against Monte Carlo within
2 % across a grid), the buy/skip gate and its reasons, σ learning, and realized
value per dollar.
"""

import math
from datetime import datetime, timezone

import pytest

from app.agents.beliefs import new_belief
from app.agents.planner import Candidate
from app.agents.voi import (
    OUTCOME_BUY,
    OUTCOME_SKIP_BALANCE,
    OUTCOME_SKIP_CACHED,
    OUTCOME_SKIP_LOW_EVOI,
    OUTCOME_SKIP_MONTHLY_CAP,
    PurchaseOutcome,
    SigmaLearner,
    decide,
    evoi,
    expected_improvement,
    format_ausd,
    monte_carlo_evoi,
    realized_value_per_dollar,
)

TOLERANCE = 1e-9
MONTE_CARLO_RELATIVE_TOLERANCE = 0.02
MONTE_CARLO_SAMPLES = 2_000_000
NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
PRICE_5_CENTS = 50_000          # 0.05 AUSD in atomic units
TEN_AUSD = 10_000_000

PHI_AT_ZERO = 0.3989422804      # φ(0) = 1/√(2π)
G_AT_ONE = 0.0833154706         # φ(1) − 1·(1 − Φ(1)) = 0.2419707245 − 0.1586552539


def _candidate(sigma: float = 0.08, price_atomic: int = PRICE_5_CENTS) -> Candidate:
    return Candidate(
        agent="OptionsEvent",
        seller_id="expected_move",
        params={"asset": "BTC", "expiry": "next", "strikes": "100000"},
        price_atomic=price_atomic,
        decision_key="OptionsEvent:BTC:above:100000",
        sigma=sigma,
    )


def _belief(p: float, stake_usd: float = 100.0):
    return new_belief("OptionsEvent", "BTC", "above:100000", stake_usd=stake_usd, now=NOW, p=p)


# ─── closed form ─────────────────────────────────────────────────────────────

def test_expected_improvement_at_zero_is_phi_of_zero():
    # Arrange / Act
    value = expected_improvement(0.0)

    # Assert
    assert value == pytest.approx(PHI_AT_ZERO, abs=TOLERANCE)


def test_expected_improvement_at_one_matches_table_values():
    # Arrange / Act
    value = expected_improvement(1.0)

    # Assert
    assert value == pytest.approx(G_AT_ONE, abs=TOLERANCE)


def test_evoi_one_sigma_from_threshold_matches_hand_computation():
    # Arrange: S = 100, σ = 0.08, |p − τ| = 0.08 → z = 1 → 100 · 0.08 · 0.0833154706
    expected = 100 * 0.08 * G_AT_ONE

    # Act
    value = evoi(stake_usd=100.0, p=0.58, tau=0.5, sigma=0.08)

    # Assert
    assert value == pytest.approx(expected, abs=TOLERANCE)


def test_evoi_is_symmetric_around_the_threshold():
    # Arrange / Act
    above = evoi(100.0, p=0.58, tau=0.5, sigma=0.08)
    below = evoi(100.0, p=0.42, tau=0.5, sigma=0.08)

    # Assert
    assert above == pytest.approx(below, abs=TOLERANCE)


def test_evoi_shrinks_as_the_belief_moves_away_from_the_threshold():
    # Arrange
    distances = [0.0, 0.05, 0.10, 0.20, 0.40]

    # Act
    values = [evoi(100.0, p=0.5 + d, tau=0.5, sigma=0.08) for d in distances]

    # Assert
    assert values == sorted(values, reverse=True)


def test_evoi_is_zero_when_the_seller_cannot_move_the_belief():
    # Arrange / Act / Assert
    assert evoi(100.0, p=0.5, tau=0.5, sigma=0.0) == 0.0


def test_evoi_scales_linearly_with_stake():
    # Arrange / Act
    small = evoi(100.0, p=0.55, tau=0.5, sigma=0.08)
    large = evoi(1_000.0, p=0.55, tau=0.5, sigma=0.08)

    # Assert
    assert large == pytest.approx(10 * small, rel=TOLERANCE)


# ─── closed form vs Monte Carlo ──────────────────────────────────────────────

@pytest.mark.parametrize("sigma", [0.03, 0.08, 0.20])
@pytest.mark.parametrize("z", [0.0, 0.5, 1.0, 1.5, 2.0])
@pytest.mark.parametrize("side", [+1, -1])
def test_closed_form_evoi_matches_monte_carlo_within_two_percent(sigma, z, side):
    # Arrange: place p at z standard deviations above / below τ
    stake_usd, tau = 1_000.0, 0.5
    p = tau + side * z * sigma

    # Act
    closed_form = evoi(stake_usd, p, tau, sigma)
    simulated = monte_carlo_evoi(stake_usd, p, tau, sigma, n_samples=MONTE_CARLO_SAMPLES, seed=7)

    # Assert
    assert simulated == pytest.approx(closed_form, rel=MONTE_CARLO_RELATIVE_TOLERANCE)


# ─── money formatting ────────────────────────────────────────────────────────

def test_format_ausd_shows_at_least_two_and_at_most_six_decimals():
    # Arrange / Act / Assert
    assert format_ausd(50_000) == "$0.05"
    assert format_ausd(1_234) == "$0.001234"
    assert format_ausd(12_500_000) == "$12.50"
    assert format_ausd(0) == "$0.00"
    assert format_ausd(1_000_000_000) == "$1,000.00"


# ─── the gate ────────────────────────────────────────────────────────────────

def test_decide_buys_when_evoi_clears_price_times_margin():
    # Arrange: p = τ → EVOI = 100 · 0.08 · 0.398942 = $3.192; price $0.05 × 1.5 = $0.075
    candidate, belief = _candidate(), _belief(p=0.5)

    # Act
    decision = decide(candidate, belief, balance_atomic=TEN_AUSD, monthly_remaining_atomic=TEN_AUSD)

    # Assert
    assert decision.outcome == OUTCOME_BUY
    assert decision.bought is True
    assert decision.reason == "buy: EVOI $3.192 ≥ price $0.05 × 1.5 (EVOI/price 63.8×)"


def test_decide_skips_when_evoi_is_below_price_times_margin():
    # Arrange: |p − τ| = 0.24 = 3σ → g(3) = 0.00038215 → EVOI = 100 · 0.08 · g(3) = $0.003
    candidate, belief = _candidate(), _belief(p=0.74)

    # Act
    decision = decide(candidate, belief, balance_atomic=TEN_AUSD)

    # Assert
    assert decision.outcome == OUTCOME_SKIP_LOW_EVOI
    assert decision.reason == "skipped: EVOI $0.003 < price $0.05 × 1.5"


def test_decide_records_every_number_behind_the_decision():
    # Arrange
    candidate, belief = _candidate(sigma=0.08), _belief(p=0.58)

    # Act
    decision = decide(candidate, belief, balance_atomic=TEN_AUSD, monthly_remaining_atomic=TEN_AUSD)

    # Assert
    assert decision.z == pytest.approx(1.0, abs=TOLERANCE)
    assert decision.evoi_usd == pytest.approx(100 * 0.08 * G_AT_ONE, abs=TOLERANCE)
    assert decision.required_usd == pytest.approx(0.075, abs=TOLERANCE)
    assert decision.evoi_per_price == pytest.approx(decision.evoi_usd / 0.05, abs=TOLERANCE)
    assert decision.as_log_dict()["bought"] is True


def test_decide_skips_when_agent_balance_is_below_price():
    # Arrange
    candidate, belief = _candidate(), _belief(p=0.5)

    # Act
    decision = decide(candidate, belief, balance_atomic=30_000)

    # Assert
    assert decision.outcome == OUTCOME_SKIP_BALANCE
    assert decision.reason == "skipped: balance $0.03 < price $0.05"


def test_decide_buys_when_balance_exactly_equals_price():
    # Arrange
    candidate, belief = _candidate(), _belief(p=0.5)

    # Act
    decision = decide(candidate, belief, balance_atomic=PRICE_5_CENTS)

    # Assert
    assert decision.bought is True


def test_decide_skips_when_monthly_cap_is_nearly_used_up():
    # Arrange
    candidate, belief = _candidate(), _belief(p=0.5)

    # Act
    decision = decide(candidate, belief, balance_atomic=TEN_AUSD, monthly_remaining_atomic=20_000)

    # Assert
    assert decision.outcome == OUTCOME_SKIP_MONTHLY_CAP
    assert decision.reason == "skipped: monthly cap — $0.02 left < price $0.05"


def test_decide_skips_cached_purchases_before_looking_at_value():
    # Arrange: a valuable purchase, but the same data is already cached
    candidate, belief = _candidate(), _belief(p=0.5)

    # Act
    decision = decide(candidate, belief, balance_atomic=TEN_AUSD, cached=True)

    # Assert
    assert decision.outcome == OUTCOME_SKIP_CACHED
    assert decision.reason.startswith("skipped: cached")


def test_decide_reports_low_evoi_before_low_balance():
    # Arrange: both checks fail; the value reason is the more useful one to show
    candidate, belief = _candidate(), _belief(p=0.95)

    # Act
    decision = decide(candidate, belief, balance_atomic=0)

    # Assert
    assert decision.outcome == OUTCOME_SKIP_LOW_EVOI


def test_decide_respects_a_custom_margin():
    # Arrange: EVOI at z = 1 is $0.6665; price $0.05 × 20 = $1.00 → skip
    candidate, belief = _candidate(), _belief(p=0.58)

    # Act
    decision = decide(candidate, belief, balance_atomic=TEN_AUSD, margin=20.0)

    # Assert
    assert decision.outcome == OUTCOME_SKIP_LOW_EVOI


# ─── σ learning ──────────────────────────────────────────────────────────────

def test_sigma_learner_starts_at_the_prior():
    # Arrange
    learner = SigmaLearner()

    # Act
    sigma = learner.sigma("expected_move", "above")

    # Assert
    assert sigma == pytest.approx(0.08, abs=TOLERANCE)


def test_sigma_learner_updates_by_ema_of_squared_moves():
    # Arrange: variance = 0.8 · 0.08² + 0.2 · 0.2² = 0.00512 + 0.008 = 0.01312
    learner = SigmaLearner()

    # Act
    sigma = learner.observe("pm_edge", "pm_yes", delta_p=0.2)

    # Assert
    assert sigma == pytest.approx(math.sqrt(0.01312), abs=TOLERANCE)


def test_sigma_learner_never_drops_below_the_floor():
    # Arrange
    learner = SigmaLearner()

    # Act
    for _ in range(200):
        learner.observe("macro_pulse", "direction", delta_p=0.0)

    # Assert
    assert learner.sigma("macro_pulse", "direction") == pytest.approx(0.01, abs=TOLERANCE)


def test_sigma_learner_keeps_seller_and_decision_type_separate():
    # Arrange
    learner = SigmaLearner()

    # Act
    learner.observe("news_sentiment", "direction", delta_p=0.3)

    # Assert
    assert learner.sigma("news_sentiment", "pm_yes") == pytest.approx(0.08, abs=TOLERANCE)
    assert learner.sigma("perps_crowding", "direction") == pytest.approx(0.08, abs=TOLERANCE)


def test_sigma_learner_snapshot_round_trips():
    # Arrange
    learner = SigmaLearner()
    learner.observe("news_sentiment", "direction", delta_p=0.15)

    # Act
    restored = SigmaLearner.from_snapshot(learner.snapshot())

    # Assert
    assert restored.sigma("news_sentiment", "direction") == pytest.approx(
        learner.sigma("news_sentiment", "direction"), abs=TOLERANCE
    )


# ─── realized value per dollar ───────────────────────────────────────────────

def test_realized_value_per_dollar_uses_realized_move_as_sigma():
    # Arrange: S = 100, p = τ, |Δp| = 0.1 → value = 100 · 0.1 · 0.398942 = $3.98942; spend $0.05
    outcome = PurchaseOutcome(stake_usd=100.0, p_before=0.5, tau=0.5, realized_abs_dp=0.1, price_atomic=PRICE_5_CENTS)

    # Act
    value_per_dollar = realized_value_per_dollar([outcome])

    # Assert
    assert value_per_dollar == pytest.approx(100 * 0.1 * PHI_AT_ZERO / 0.05, abs=1e-6)


def test_realized_value_per_dollar_sums_value_and_spend():
    # Arrange: one useful purchase ($3.98942) and one that moved nothing ($0), $0.10 total spend
    useful = PurchaseOutcome(100.0, 0.5, 0.5, 0.1, PRICE_5_CENTS)
    useless = PurchaseOutcome(100.0, 0.5, 0.5, 0.0, PRICE_5_CENTS)

    # Act
    value_per_dollar = realized_value_per_dollar([useful, useless])

    # Assert
    assert value_per_dollar == pytest.approx(100 * 0.1 * PHI_AT_ZERO / 0.10, abs=1e-6)


def test_realized_value_per_dollar_is_zero_without_spend():
    # Arrange / Act / Assert
    assert realized_value_per_dollar([]) == 0.0
