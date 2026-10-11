"""
Tests for `app/keeper/allocator.py` — the daily budget formula, the LogNormal
posterior update, and the Thompson-sampling allocation with floors, caps and event
boosts. Includes property-style checks over many seeded random scenarios.
"""

import math
import random

import pytest

from app.constants import ONE_AUSD
from app.keeper.allocator import (
    LIMIT_ALLOWANCE_PACE,
    LIMIT_DAILY_CAP,
    LIMIT_MONTHLY_CAP,
    AgentPosterior,
    allocate,
    daily_budget,
)

TOLERANCE = 1e-9
SHARE_TOLERANCE = 1e-6
NEARLY_CERTAIN_S = 1e-9         # posterior so tight that every draw is e^μ
AGENTS = ("OnchainPerps", "OptionsEvent", "NewsSentiment", "MacroFilings")


def _ausd(amount: float) -> int:
    return round(amount * ONE_AUSD)


def _certain(agent: str, value_per_dollar: float) -> AgentPosterior:
    """A posterior whose Thompson draw is (almost exactly) `value_per_dollar`."""
    return AgentPosterior(agent=agent, mu=math.log(value_per_dollar), s=NEARLY_CERTAIN_S)


# ─── daily budget ────────────────────────────────────────────────────────────

def test_daily_budget_paces_the_allowance_and_adds_a_quarter_of_yesterdays_unspent():
    # Arrange: min(10, 240/30 = 8, 100) = 8; rollover 25 % of 2 = 0.5 → 8.5
    # Act
    budget = daily_budget(
        daily_cap=_ausd(10), allowance_remaining=_ausd(240), days_remaining=30,
        monthly_cap_remaining=_ausd(100), yesterday_unspent=_ausd(2),
    )

    # Assert
    assert budget.amount_atomic == _ausd(8.5)
    assert (budget.base_atomic, budget.rollover_atomic) == (_ausd(8), _ausd(0.5))
    assert budget.limited_by == LIMIT_ALLOWANCE_PACE


def test_daily_budget_rollover_never_exceeds_the_daily_cap():
    # Arrange: base = daily cap 5 (pace is 10); rollover 0.5 would break the cap → 0
    # Act
    budget = daily_budget(
        daily_cap=_ausd(5), allowance_remaining=_ausd(300), days_remaining=30,
        monthly_cap_remaining=_ausd(100), yesterday_unspent=_ausd(2),
    )

    # Assert
    assert (budget.amount_atomic, budget.rollover_atomic) == (_ausd(5), 0)
    assert budget.limited_by == LIMIT_DAILY_CAP


def test_daily_budget_is_limited_by_the_monthly_cap():
    # Arrange
    # Act
    budget = daily_budget(
        daily_cap=_ausd(10), allowance_remaining=_ausd(300), days_remaining=30,
        monthly_cap_remaining=_ausd(3), yesterday_unspent=_ausd(4),
    )

    # Assert
    assert budget.amount_atomic == _ausd(3)
    assert budget.limited_by == LIMIT_MONTHLY_CAP


def test_daily_budget_rollover_is_limited_to_a_quarter_of_unspent():
    # Arrange: base 8; 25 % of 100 = 25 → 33 (daily cap 1000 is not binding)
    # Act
    budget = daily_budget(
        daily_cap=_ausd(1_000), allowance_remaining=_ausd(240), days_remaining=30,
        monthly_cap_remaining=None, yesterday_unspent=_ausd(100),
    )

    # Assert
    assert budget.amount_atomic == _ausd(33)


def test_daily_budget_rollover_never_exceeds_the_allowance():
    # Arrange: last day, allowance 8 → base 8; rollover 2 would exceed the allowance
    # Act
    budget = daily_budget(
        daily_cap=_ausd(10), allowance_remaining=_ausd(8), days_remaining=1,
        monthly_cap_remaining=None, yesterday_unspent=_ausd(8),
    )

    # Assert
    assert budget.amount_atomic == _ausd(8)


def test_daily_budget_treats_zero_days_remaining_as_the_last_day():
    # Arrange / Act
    budget = daily_budget(
        daily_cap=_ausd(10), allowance_remaining=_ausd(7), days_remaining=0,
        monthly_cap_remaining=None, yesterday_unspent=0,
    )

    # Assert
    assert budget.amount_atomic == _ausd(7)


def test_daily_budget_is_zero_after_revocation():
    # Arrange: allowance revoked (0) → nothing to spend, whatever else is configured
    # Act
    budget = daily_budget(
        daily_cap=_ausd(10), allowance_remaining=0, days_remaining=20,
        monthly_cap_remaining=_ausd(100), yesterday_unspent=_ausd(5),
    )

    # Assert
    assert budget.amount_atomic == 0


# ─── posterior ───────────────────────────────────────────────────────────────

def test_posterior_update_is_the_conjugate_normal_update_on_log_value():
    # Arrange: prior μ 0, s 1, noise 1; observe v = e → y = 1 → μ' = 0.5, s' = 1/√2
    prior = AgentPosterior(agent="OptionsEvent")

    # Act
    posterior = prior.update(math.e)

    # Assert
    assert posterior.mu == pytest.approx(0.5, abs=TOLERANCE)
    assert posterior.s == pytest.approx(1 / math.sqrt(2), abs=TOLERANCE)
    assert posterior.observations == 1


def test_posterior_update_floors_zero_value_instead_of_taking_log_of_zero():
    # Arrange: v = 0 → floored to 0.01 → y = ln 0.01 → μ' = ln(0.01) / 2
    prior = AgentPosterior(agent="NewsSentiment")

    # Act
    posterior = prior.update(0.0)

    # Assert
    assert posterior.mu == pytest.approx(math.log(0.01) / 2, abs=TOLERANCE)


# ─── allocation: hand-computed cases ─────────────────────────────────────────

def test_equal_posteriors_without_events_split_the_budget_equally():
    # Arrange
    posteriors = [_certain(agent, 1.0) for agent in AGENTS]

    # Act
    allocations = allocate(_ausd(10), posteriors, events_today={}, rng_seed=1)

    # Assert
    assert [a.amount_atomic for a in allocations] == [_ausd(2.5)] * 4


def test_event_boost_raises_the_agents_weight():
    # Arrange: weights 1.5 : 1 : 1 : 1 → OptionsEvent gets 1.5 / 4.5 = 1/3 (+33 % vs equal)
    posteriors = [_certain(agent, 1.0) for agent in AGENTS]
    events = {"OptionsEvent": ["NVDA earnings today"]}

    # Act
    with_event = allocate(_ausd(9), posteriors, events, rng_seed=1)
    without_event = allocate(_ausd(9), posteriors, {}, rng_seed=1)

    # Assert
    options_with, options_without = with_event[1], without_event[1]
    assert options_with.weight > options_without.weight
    assert options_with.weight == pytest.approx(1 / 3, abs=SHARE_TOLERANCE)
    assert options_with.reasons[0] == "OptionsEvent +33% vs equal split: NVDA earnings today"


def test_dominant_agent_is_held_at_the_cap_and_the_rest_share_the_remainder():
    # Arrange: raw ≈ 148 : 1 : 1 : 1 → first agent capped at 50 %, others 50 % / 3 each
    posteriors = [_certain(AGENTS[0], math.exp(5)), *(_certain(agent, 1.0) for agent in AGENTS[1:])]

    # Act
    allocations = allocate(_ausd(12), posteriors, {}, rng_seed=3)

    # Assert
    assert allocations[0].amount_atomic == _ausd(6)
    assert [a.amount_atomic for a in allocations[1:]] == [_ausd(2)] * 3
    assert "held at the 50% cap" in allocations[0].reasons


def test_weak_agent_is_raised_to_the_floor():
    # Arrange: raw 1 : 1 : 1 : 0.0067 → last agent at 10 %, the rest 30 % each
    posteriors = [*(_certain(agent, 1.0) for agent in AGENTS[:3]), _certain(AGENTS[3], math.exp(-5))]

    # Act
    allocations = allocate(_ausd(10), posteriors, {}, rng_seed=3)

    # Assert
    assert [a.amount_atomic for a in allocations] == [_ausd(3), _ausd(3), _ausd(3), _ausd(1)]
    assert "raised to the 10% floor" in allocations[3].reasons


def test_floor_and_cap_binding_together_still_spend_the_whole_budget():
    # Arrange: floor 30 %, cap 40 %, raw 0.41 : 0.41 : 0.18.
    # Correct answer: last agent at the 30 % floor, the other two split 70 % → 35 % each.
    # (Clipping 0.41 → cap and 0.18 → floor in one pass would wrongly hand out 110 %.)
    posteriors = [_certain("A", 0.41), _certain("B", 0.41), _certain("C", 0.18)]

    # Act
    allocations = allocate(_ausd(100), posteriors, {}, rng_seed=0, floor=0.30, cap=0.40)

    # Assert
    weights = [a.weight for a in allocations]
    assert weights == pytest.approx([0.35, 0.35, 0.30], abs=SHARE_TOLERANCE)


# ─── allocation: edge cases ──────────────────────────────────────────────────

def test_single_agent_gets_the_whole_budget_with_the_cap_relaxed():
    # Arrange
    posteriors = [AgentPosterior(agent="OptionsEvent")]

    # Act
    (allocation,) = allocate(_ausd(5), posteriors, {}, rng_seed=42)

    # Assert
    assert allocation.amount_atomic == _ausd(5)
    assert any("cap raised" in reason for reason in allocation.reasons)


def test_zero_budget_allocates_nothing_and_says_why():
    # Arrange
    posteriors = [AgentPosterior(agent=agent) for agent in AGENTS]

    # Act
    allocations = allocate(0, posteriors, {}, rng_seed=7)

    # Assert
    assert all(a.amount_atomic == 0 for a in allocations)
    assert all("no budget today (B_day = 0)" in a.reasons for a in allocations)


def test_no_agents_means_no_allocations():
    # Arrange / Act / Assert
    assert allocate(_ausd(10), [], {}, rng_seed=0) == []


def test_too_many_agents_for_the_floor_lowers_the_floor_to_an_equal_split():
    # Arrange: 12 agents × 10 % floor = 120 % > 100 % → floor becomes 1/12 → everyone gets 1/12
    posteriors = [AgentPosterior(agent=f"agent{i}") for i in range(12)]

    # Act
    allocations = allocate(_ausd(12), posteriors, {}, rng_seed=5)

    # Assert
    assert [a.weight for a in allocations] == pytest.approx([1 / 12] * 12, abs=SHARE_TOLERANCE)
    assert any("floor lowered" in reason for reason in allocations[0].reasons)


def test_floor_above_cap_is_rejected():
    # Arrange
    posteriors = [AgentPosterior(agent=agent) for agent in AGENTS]

    # Act / Assert
    with pytest.raises(ValueError):
        allocate(_ausd(10), posteriors, {}, rng_seed=0, floor=0.6, cap=0.5)


def test_allocation_is_deterministic_with_a_fixed_seed():
    # Arrange
    posteriors = [AgentPosterior(agent=agent) for agent in AGENTS]
    events = {"MacroFilings": ["CPI release"]}

    # Act
    first = allocate(_ausd(10), posteriors, events, rng_seed=2026)
    second = allocate(_ausd(10), posteriors, events, rng_seed=2026)

    # Assert
    assert first == second


def test_different_seeds_explore_different_allocations():
    # Arrange
    posteriors = [AgentPosterior(agent=agent) for agent in AGENTS]

    # Act
    weights_by_seed = {tuple(a.weight for a in allocate(_ausd(10), posteriors, {}, rng_seed=seed)) for seed in range(5)}

    # Assert
    assert len(weights_by_seed) > 1


# ─── allocation: properties over many random scenarios ───────────────────────

def _random_scenario(seed: int) -> tuple[int, list[AgentPosterior], dict[str, list[str]]]:
    """A random budget, 2–6 agents with random posteriors, and random events."""
    rng = random.Random(seed)
    agent_count = rng.randint(2, 6)
    posteriors = [
        AgentPosterior(agent=f"agent{i}", mu=rng.uniform(-2.0, 2.0), s=rng.uniform(0.05, 1.5))
        for i in range(agent_count)
    ]
    events = {f"agent{i}": ["event"] * rng.randint(0, 3) for i in range(agent_count)}
    budget = rng.randint(0, 1_000 * ONE_AUSD)
    return budget, posteriors, events


@pytest.mark.parametrize("seed", range(200))
def test_allocation_never_exceeds_budget_and_respects_floor_and_cap(seed):
    # Arrange
    budget, posteriors, events = _random_scenario(seed)
    floor, cap = 0.10, 0.50

    # Act
    allocations = allocate(budget, posteriors, events, rng_seed=seed)

    # Assert
    total = sum(a.amount_atomic for a in allocations)
    assert total <= budget
    assert total >= budget - len(allocations)            # only rounding dust is left over
    assert sum(a.weight for a in allocations) == pytest.approx(1.0, abs=SHARE_TOLERANCE)
    for allocation in allocations:
        assert floor - SHARE_TOLERANCE <= allocation.weight <= cap + SHARE_TOLERANCE
        assert math.floor(budget * floor) <= allocation.amount_atomic
        assert allocation.amount_atomic <= budget * cap
        assert allocation.amount_atomic >= 0
