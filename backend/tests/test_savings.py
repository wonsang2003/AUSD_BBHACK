"""Tests for app/savings.py: pro-rating, savings, monthly cap progress and input validation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.constants import ONE_AUSD
from app.savings import (
    cap_used_fraction,
    compute_savings,
    describe_savings,
    month_fraction_elapsed,
    parse_subscriptions,
)

OCT_11 = datetime(2026, 10, 11, 15, 0, tzinfo=UTC)   # October has 31 days


def test_month_fraction_counts_today_as_a_full_day():
    # Act / Assert
    assert month_fraction_elapsed(OCT_11) == pytest.approx(11 / 31)
    assert month_fraction_elapsed(datetime(2026, 10, 1, 0, 0, tzinfo=UTC)) == pytest.approx(1 / 31)


def test_compute_savings_prorates_user_subscriptions_and_subtracts_spend():
    # Arrange
    subscriptions = [{"name": "Options data", "monthly_usd": 62.0}, {"name": "News feed", "monthly_usd": 31.0}]

    # Act
    meter = compute_savings(3 * ONE_AUSD, subscriptions, monthly_cap_atomic=60 * ONE_AUSD, now=OCT_11)

    # Assert: (62 + 31) × 11/31 = 33.00
    assert meter.prorated_subscriptions_usd == pytest.approx(33.0)
    assert meter.savings_usd == pytest.approx(30.0)
    assert meter.monthly_cap_used == pytest.approx(0.05)


def test_compute_savings_without_subscriptions_reports_zero_comparison_not_an_invented_price():
    # Act
    meter = compute_savings(1_500_000, [], monthly_cap_atomic=0, now=OCT_11)

    # Assert
    assert meter.prorated_subscriptions_usd == 0.0
    assert meter.savings_usd == pytest.approx(-1.5)
    assert meter.monthly_cap_used == 0.0


def test_parse_subscriptions_skips_invalid_entries():
    # Arrange
    raw = [
        {"name": "Valid", "monthly_usd": 10},
        {"name": "", "monthly_usd": 5},
        {"name": "Negative", "monthly_usd": -1},
        {"name": "Not a number", "monthly_usd": "ten"},
        {"name": "Infinite", "monthly_usd": float("inf")},
        {"monthly_usd": 3},
    ]

    # Act
    parsed = parse_subscriptions(raw)

    # Assert
    assert [(item.name, item.monthly_usd) for item in parsed] == [("Valid", 10.0)]


def test_to_api_matches_state_savings_and_state_monthly_shapes():
    # Arrange
    meter = compute_savings(2 * ONE_AUSD, [{"name": "Feed", "monthly_usd": 31.0}], 10 * ONE_AUSD, OCT_11)

    # Act
    savings = meter.to_api()
    monthly = meter.monthly_to_api()

    # Assert
    assert {"mtd_spend_atomic", "prorated_subscriptions_usd", "subscriptions"} <= set(savings)
    assert savings["subscriptions"] == [{"name": "Feed", "monthly_usd": 31.0}]
    assert monthly == {"cap": 10 * ONE_AUSD, "spent_mtd": 2 * ONE_AUSD}


def test_cap_used_fraction_can_exceed_one_when_over_cap():
    # Act / Assert
    assert cap_used_fraction(spent_atomic=150, cap_atomic=100) == pytest.approx(1.5)


def test_describe_savings_states_saved_or_over_and_asks_for_subscriptions_when_none():
    # Arrange
    saved = compute_savings(ONE_AUSD, [{"name": "Feed", "monthly_usd": 31.0}], 0, OCT_11)
    over = compute_savings(20 * ONE_AUSD, [{"name": "Feed", "monthly_usd": 31.0}], 0, OCT_11)
    empty = compute_savings(ONE_AUSD, [], 0, OCT_11)

    # Act / Assert
    assert describe_savings(saved) == "Spent $1.00 this month vs $11.00 pro-rated subscriptions (saved $10.00)"
    assert describe_savings(over).endswith("(over by $9.00)")
    assert "add your data subscriptions" in describe_savings(empty)
