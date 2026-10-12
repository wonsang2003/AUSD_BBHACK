"""Sector specs, initial beliefs and reading seller answers into belief updates (app/agents/sectors.py)."""

from datetime import UTC, datetime

from app.agents.sectors import SECTORS, agents_covering, initial_beliefs, read_update, strike_above
from app.agents.beliefs import new_belief

NOW = datetime(2026, 10, 11, 14, 0, tzinfo=UTC)


def test_strike_above_is_next_round_number():
    assert strike_above(97_345) == 98_000
    assert strike_above(3_512) == 3_600
    assert strike_above(182.3) == 190


def test_perps_agent_covers_crypto_only():
    assert SECTORS["OnchainPerps"].covers("BTC")
    assert not SECTORS["OnchainPerps"].covers("NVDA")
    assert "OnchainPerps" not in agents_covering("NVDA")


def test_options_agent_holds_above_and_pm_yes_for_crypto_but_no_pm_yes_for_equities():
    beliefs = initial_beliefs(SECTORS["OptionsEvent"], ["BTC", "NVDA"], {}, {"BTC": 97_345, "NVDA": 182.3}, NOW)
    decisions = {(b.asset, b.decision) for b in beliefs}
    assert decisions == {("BTC", "above:98000"), ("BTC", "pm_yes"), ("NVDA", "above:190")}


def test_above_belief_needs_a_spot():
    beliefs = initial_beliefs(SECTORS["OptionsEvent"], ["ETH"], {}, {}, NOW)
    assert [b.decision for b in beliefs] == ["pm_yes"]


def test_news_reading_moves_direction_belief_and_flags_risk():
    belief = new_belief("NewsSentiment", "BTC", "direction", 1000.0, NOW)
    reading = read_update("news_sentiment", belief, {"tone_delta": 3.0, "event_flags": ["etf", "hack"], "article_count": 12})
    assert round(reading.update.new_p, 4) == 0.56   # +0.06 tone, +0.03 etf, −0.03 hack
    assert reading.risk is True


def test_perps_reading_missing_asset_returns_none():
    belief = new_belief("OnchainPerps", "SOL", "direction", 1000.0, NOW)
    assert read_update("perps_crowding", belief, {"assets": [{"asset": "BTC", "crowding_z": 1.0}]}) is None
