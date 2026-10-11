"""
Tests for `app/keeper/recharge_policy.py` — session-start top-ups, intraday refills
(including the exact 20 % boundary and zero headroom), end-of-session sweeps and
runway. Every amount returned must be a positive integer number of atomic units.
"""

import math

from app.constants import ONE_AUSD
from app.keeper.allocator import Allocation
from app.keeper.recharge_policy import (
    average_daily_spend,
    intraday_refill,
    is_runway_low,
    runway_days,
    session_top_ups,
    sweep_amounts,
)

ONE_AUSD_ALLOCATION = Allocation(agent="OptionsEvent", amount_atomic=ONE_AUSD, weight=0.25, reasons=())


def _allocation(agent: str, amount_atomic: int) -> Allocation:
    return Allocation(agent=agent, amount_atomic=amount_atomic, weight=0.25, reasons=())


# ─── session start ───────────────────────────────────────────────────────────

def test_session_top_up_fills_the_gap_between_allocation_and_balance():
    # Arrange: 2.50 − 0.40 = 2.10
    allocations = [_allocation("OptionsEvent", 2_500_000)]

    # Act
    (top_up,) = session_top_ups(allocations, {"OptionsEvent": 400_000})

    # Assert
    assert (top_up.agent, top_up.amount) == ("OptionsEvent", 2_100_000)
    assert top_up.reason == "session start: allocation $2.50 − balance $0.40 = $2.10"


def test_session_top_up_skips_agents_already_at_or_above_allocation():
    # Arrange
    allocations = [_allocation("OptionsEvent", ONE_AUSD), _allocation("MacroFilings", ONE_AUSD)]
    balances = {"OptionsEvent": ONE_AUSD, "MacroFilings": 1_500_000}

    # Act
    top_ups = session_top_ups(allocations, balances)

    # Assert
    assert top_ups == []


def test_session_top_up_treats_a_missing_balance_as_empty():
    # Arrange
    allocations = [_allocation("NewsSentiment", 750_000)]

    # Act
    (top_up,) = session_top_ups(allocations, {})

    # Assert
    assert top_up.amount == 750_000


def test_session_top_up_is_clipped_to_the_agents_on_chain_headroom():
    # Arrange
    allocations = [_allocation("OptionsEvent", 2_000_000)]

    # Act
    (top_up,) = session_top_ups(allocations, {"OptionsEvent": 0}, agent_headroom={"OptionsEvent": 500_000})

    # Assert
    assert top_up.amount == 500_000
    assert "limited to $0.50 by on-chain headroom" in top_up.reason


def test_session_top_ups_share_the_budget_headroom_in_allocation_order():
    # Arrange: 1.5 AUSD of budget headroom for two agents needing 1.0 each
    allocations = [_allocation("OptionsEvent", ONE_AUSD), _allocation("MacroFilings", ONE_AUSD)]

    # Act
    top_ups = session_top_ups(allocations, {}, budget_headroom=1_500_000)

    # Assert
    assert [(t.agent, t.amount) for t in top_ups] == [("OptionsEvent", ONE_AUSD), ("MacroFilings", 500_000)]


def test_session_top_ups_are_never_zero_or_negative():
    # Arrange: zero headroom, a negative balance reading and an over-funded agent
    allocations = [_allocation("A", ONE_AUSD), _allocation("B", ONE_AUSD), _allocation("C", ONE_AUSD)]
    balances = {"A": 0, "B": -5, "C": 3 * ONE_AUSD}

    # Act
    top_ups = session_top_ups(allocations, balances, agent_headroom={"A": 0, "B": ONE_AUSD, "C": ONE_AUSD})

    # Assert
    assert [(t.agent, t.amount) for t in top_ups] == [("B", ONE_AUSD)]


# ─── intraday refill ─────────────────────────────────────────────────────────

def test_intraday_refill_adds_a_quarter_of_the_allocation_when_balance_is_low():
    # Arrange: balance 0.15 < 20 % of 1.00; value/$ 2.0 ≥ 1.0 → +0.25
    # Act
    top_up = intraday_refill(ONE_AUSD_ALLOCATION, balance=150_000, realized_vpd=2.0, threshold=1.0, headroom=ONE_AUSD)

    # Assert
    assert top_up is not None
    assert top_up.amount == 250_000


def test_intraday_refill_does_not_fire_at_exactly_twenty_percent():
    # Arrange: 0.20 is not *below* 20 % of 1.00
    # Act
    top_up = intraday_refill(ONE_AUSD_ALLOCATION, balance=200_000, realized_vpd=2.0, threshold=1.0, headroom=ONE_AUSD)

    # Assert
    assert top_up is None


def test_intraday_refill_fires_one_atomic_unit_below_twenty_percent():
    # Arrange
    # Act
    top_up = intraday_refill(ONE_AUSD_ALLOCATION, balance=199_999, realized_vpd=2.0, threshold=1.0, headroom=ONE_AUSD)

    # Assert
    assert top_up is not None


def test_intraday_refill_requires_value_per_dollar_at_least_the_threshold():
    # Arrange / Act
    below = intraday_refill(ONE_AUSD_ALLOCATION, balance=0, realized_vpd=0.99, threshold=1.0, headroom=ONE_AUSD)
    exactly = intraday_refill(ONE_AUSD_ALLOCATION, balance=0, realized_vpd=1.0, threshold=1.0, headroom=ONE_AUSD)

    # Assert
    assert below is None
    assert exactly is not None


def test_intraday_refill_with_zero_headroom_does_nothing():
    # Arrange / Act
    top_up = intraday_refill(ONE_AUSD_ALLOCATION, balance=0, realized_vpd=5.0, threshold=1.0, headroom=0)

    # Assert
    assert top_up is None


def test_intraday_refill_is_clipped_to_headroom():
    # Arrange: 25 % would be 0.25, only 0.10 of headroom left today
    # Act
    top_up = intraday_refill(ONE_AUSD_ALLOCATION, balance=0, realized_vpd=5.0, threshold=1.0, headroom=100_000)

    # Assert
    assert top_up is not None
    assert top_up.amount == 100_000
    assert "limited by on-chain headroom" in top_up.reason


def test_intraday_refill_that_rounds_to_zero_is_skipped():
    # Arrange: 25 % of 3 atomic units = 0
    tiny = _allocation("OptionsEvent", 3)

    # Act
    top_up = intraday_refill(tiny, balance=0, realized_vpd=5.0, threshold=1.0, headroom=ONE_AUSD)

    # Assert
    assert top_up is None


def test_intraday_refill_never_fires_for_a_zero_allocation():
    # Arrange
    nothing = _allocation("OptionsEvent", 0)

    # Act
    top_up = intraday_refill(nothing, balance=0, realized_vpd=5.0, threshold=1.0, headroom=ONE_AUSD)

    # Assert
    assert top_up is None


# ─── sweep ───────────────────────────────────────────────────────────────────

def test_sweep_returns_every_positive_balance_and_skips_empty_wallets():
    # Arrange
    balances = {"OptionsEvent": 1_230_000, "MacroFilings": 0, "NewsSentiment": -1}

    # Act
    sweeps = sweep_amounts(balances)

    # Assert
    assert [(s.agent, s.amount) for s in sweeps] == [("OptionsEvent", 1_230_000)]
    assert sweeps[0].reason == "session end: return $1.23 to the owner"


# ─── runway ──────────────────────────────────────────────────────────────────

def test_runway_is_allowance_divided_by_average_daily_spend():
    # Arrange: 30 AUSD / 10 AUSD per day = 3 days
    average = average_daily_spend([8 * ONE_AUSD, 12 * ONE_AUSD])

    # Act
    days = runway_days(30 * ONE_AUSD, average)

    # Assert
    assert days == 3.0


def test_runway_is_infinite_before_any_spending():
    # Arrange / Act / Assert
    assert runway_days(30 * ONE_AUSD, average_daily_spend([])) == math.inf


def test_runway_is_zero_after_revocation():
    # Arrange / Act / Assert
    assert runway_days(0, 10 * ONE_AUSD) == 0.0


def test_runway_alert_fires_strictly_below_three_days():
    # Arrange / Act / Assert
    assert is_runway_low(2.9) is True
    assert is_runway_low(3.0) is False
