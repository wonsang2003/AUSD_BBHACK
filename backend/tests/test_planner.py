"""
Tests for `app/agents/planner.py` — the TTL purchase cache, candidate generation
(sellers × watchlist, with asset-class and decision-type coverage), and greedy
selection by EVOI / price within balances and the monthly cap.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.beliefs import index_beliefs, new_belief
from app.agents.planner import (
    AGENT_SELLERS,
    Candidate,
    PurchaseCache,
    asset_class,
    generate_candidates,
    rank_and_select,
)
from app.agents.voi import (
    OUTCOME_SKIP_BALANCE,
    OUTCOME_SKIP_CACHED,
    OUTCOME_SKIP_DUPLICATE,
    OUTCOME_SKIP_LOW_EVOI,
    OUTCOME_SKIP_MONTHLY_CAP,
    SigmaLearner,
)

NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
TEN_AUSD = 10_000_000

PRICES = {
    "expected_move": 50_000,
    "pm_edge": 100_000,
    "perps_crowding": 20_000,
    "news_sentiment": 30_000,
    "insider_cluster": 30_000,
    "macro_pulse": 20_000,
}


def _pairs(candidates: list[Candidate]) -> set[tuple[str, str]]:
    """(decision key, seller id) for each candidate — easy to compare as a set."""
    return {(candidate.decision_key, candidate.seller_id) for candidate in candidates}


def _candidate(agent: str, seller_id: str, params: dict, price_atomic: int, decision_key: str) -> Candidate:
    return Candidate(agent, seller_id, params, price_atomic, decision_key, sigma=0.08)


# ─── PurchaseCache ───────────────────────────────────────────────────────────

def test_cache_key_does_not_depend_on_param_order():
    # Arrange / Act
    first = PurchaseCache.key("expected_move", {"expiry": "next", "asset": "BTC"})
    second = PurchaseCache.key("expected_move", {"asset": "BTC", "expiry": "next"})

    # Assert
    assert first == second == "expected_move?asset=BTC&expiry=next"


def test_cache_hit_within_ttl_and_miss_exactly_at_ttl():
    # Arrange
    cache = PurchaseCache(ttl_seconds=120)
    params = {"q": "BTC"}
    cache.record("news_sentiment", params, NOW, result={"tone": 1.2})

    # Act
    hit = cache.is_cached("news_sentiment", params, NOW + timedelta(seconds=119))
    at_ttl = cache.is_cached("news_sentiment", params, NOW + timedelta(seconds=120))

    # Assert
    assert (hit, at_ttl) == (True, False)


def test_cache_returns_the_stored_result_while_live():
    # Arrange
    cache = PurchaseCache()
    cache.record("news_sentiment", {"q": "BTC"}, NOW, result={"tone": 1.2})

    # Act
    live = cache.get("news_sentiment", {"q": "BTC"}, NOW + timedelta(seconds=10))
    expired = cache.get("news_sentiment", {"q": "BTC"}, NOW + timedelta(hours=1))

    # Assert
    assert (live, expired) == ({"tone": 1.2}, None)


def test_cache_distinguishes_different_params():
    # Arrange
    cache = PurchaseCache()
    cache.record("news_sentiment", {"q": "BTC"}, NOW)

    # Act / Assert
    assert cache.is_cached("news_sentiment", {"q": "ETH"}, NOW) is False


def test_cache_honours_a_per_record_ttl():
    # Arrange
    cache = PurchaseCache(ttl_seconds=120)
    cache.record("macro_pulse", {}, NOW, ttl_seconds=600)

    # Act / Assert
    assert cache.is_cached("macro_pulse", {}, NOW + timedelta(seconds=300)) is True


def test_cache_prune_drops_only_expired_entries():
    # Arrange
    cache = PurchaseCache(ttl_seconds=60)
    cache.record("macro_pulse", {}, NOW - timedelta(minutes=5))
    cache.record("news_sentiment", {"q": "BTC"}, NOW)

    # Act
    removed = cache.prune(NOW)

    # Assert
    assert removed == 1
    assert cache.is_cached("news_sentiment", {"q": "BTC"}, NOW) is True


# ─── candidate generation ────────────────────────────────────────────────────

def test_asset_class_knows_crypto_symbols_and_defaults_to_equity():
    # Arrange / Act / Assert
    assert asset_class("btc") == "crypto"
    assert asset_class("NVDA") == "equity"


def test_generate_candidates_maps_each_belief_to_the_sellers_that_can_inform_it():
    # Arrange
    beliefs = index_beliefs(
        [
            new_belief("OnchainPerps", "BTC", "direction", 500.0, NOW),
            new_belief("OptionsEvent", "BTC", "above:100000", 500.0, NOW),
            new_belief("OptionsEvent", "BTC", "pm_yes", 500.0, NOW),
            new_belief("MacroFilings", "NVDA", "direction", 500.0, NOW),
            new_belief("NewsSentiment", "NVDA", "direction", 500.0, NOW),
        ]
    )

    # Act
    candidates = generate_candidates(AGENT_SELLERS, ["BTC", "NVDA"], beliefs, PRICES, SigmaLearner())

    # Assert
    assert _pairs(candidates) == {
        ("OnchainPerps:BTC:direction", "perps_crowding"),
        ("OnchainPerps:BTC:direction", "news_sentiment"),
        ("OptionsEvent:BTC:above:100000", "expected_move"),
        ("OptionsEvent:BTC:pm_yes", "pm_edge"),
        ("MacroFilings:NVDA:direction", "macro_pulse"),
        ("MacroFilings:NVDA:direction", "insider_cluster"),
        ("NewsSentiment:NVDA:direction", "news_sentiment"),
    }


def test_generate_candidates_builds_seller_params_from_the_belief():
    # Arrange
    beliefs = index_beliefs([new_belief("OptionsEvent", "BTC", "above:100000", 500.0, NOW)])

    # Act
    (candidate,) = generate_candidates(AGENT_SELLERS, ["BTC"], beliefs, PRICES, SigmaLearner())

    # Assert
    assert candidate.params == {"asset": "BTC", "expiry": "next", "strikes": "100000"}
    assert candidate.price_atomic == PRICES["expected_move"]


def test_generate_candidates_skips_perps_for_equities_and_insiders_for_crypto():
    # Arrange
    beliefs = index_beliefs(
        [
            new_belief("OnchainPerps", "NVDA", "direction", 500.0, NOW),
            new_belief("MacroFilings", "ETH", "direction", 500.0, NOW),
        ]
    )

    # Act
    candidates = generate_candidates(AGENT_SELLERS, ["NVDA", "ETH"], beliefs, PRICES, SigmaLearner())

    # Assert
    assert _pairs(candidates) == {
        ("OnchainPerps:NVDA:direction", "news_sentiment"),
        ("MacroFilings:ETH:direction", "macro_pulse"),
    }


def test_generate_candidates_ignores_assets_not_on_the_watchlist():
    # Arrange
    beliefs = index_beliefs([new_belief("NewsSentiment", "SOL", "direction", 500.0, NOW)])

    # Act
    candidates = generate_candidates(AGENT_SELLERS, ["BTC"], beliefs, PRICES, SigmaLearner())

    # Assert
    assert candidates == []


def test_generate_candidates_skips_sellers_without_a_price():
    # Arrange
    beliefs = index_beliefs([new_belief("NewsSentiment", "BTC", "direction", 500.0, NOW)])

    # Act
    candidates = generate_candidates(AGENT_SELLERS, ["BTC"], beliefs, {"pm_edge": 100_000}, SigmaLearner())

    # Assert
    assert candidates == []


def test_generate_candidates_takes_sigma_from_the_learner():
    # Arrange
    learner = SigmaLearner()
    learner.observe("news_sentiment", "direction", delta_p=0.3)
    beliefs = index_beliefs([new_belief("NewsSentiment", "BTC", "direction", 500.0, NOW)])

    # Act
    (candidate,) = generate_candidates(AGENT_SELLERS, ["BTC"], beliefs, PRICES, learner)

    # Assert
    assert candidate.sigma == pytest.approx(learner.sigma("news_sentiment", "direction"))


# ─── rank and select ─────────────────────────────────────────────────────────

def test_rank_and_select_buys_highest_evoi_per_price_first():
    # Arrange: BTC is undecided (p = τ), ETH leans (p = 0.6) → BTC has the higher EVOI
    btc = new_belief("NewsSentiment", "BTC", "direction", 500.0, NOW, p=0.5)
    eth = new_belief("NewsSentiment", "ETH", "direction", 500.0, NOW, p=0.6)
    beliefs = index_beliefs([eth, btc])
    candidates = generate_candidates(AGENT_SELLERS, ["BTC", "ETH"], beliefs, PRICES, SigmaLearner())

    # Act
    selection = rank_and_select(candidates, beliefs, {"NewsSentiment": TEN_AUSD}, cache=PurchaseCache(), now=NOW)

    # Assert
    assert [decision.decision_key for decision in selection.bought] == [btc.key, eth.key]
    assert selection.total_price_atomic == 2 * PRICES["news_sentiment"]


def test_rank_and_select_stops_buying_when_the_agent_balance_runs_out():
    # Arrange: 60 000 atomic buys one 50 000 purchase, not two
    first = new_belief("OptionsEvent", "BTC", "above:100000", 500.0, NOW, p=0.5)
    second = new_belief("OptionsEvent", "BTC", "above:110000", 500.0, NOW, p=0.5)
    beliefs = index_beliefs([first, second])
    candidates = generate_candidates(AGENT_SELLERS, ["BTC"], beliefs, PRICES, SigmaLearner())

    # Act
    selection = rank_and_select(candidates, beliefs, {"OptionsEvent": 60_000}, cache=PurchaseCache(), now=NOW)

    # Assert
    assert len(selection.bought) == 1
    assert [decision.outcome for decision in selection.skipped] == [OUTCOME_SKIP_BALANCE]


def test_rank_and_select_still_buys_a_cheaper_item_that_fits_the_remaining_balance():
    # Arrange: the 50 000 item does not fit in 40 000, the 30 000 item does
    belief = new_belief("OptionsEvent", "BTC", "above:100000", 500.0, NOW)
    beliefs = index_beliefs([belief])
    expensive = _candidate("OptionsEvent", "expected_move", {"asset": "BTC"}, 50_000, belief.key)
    cheap = _candidate("OptionsEvent", "expected_move", {"asset": "BTC", "x": "1"}, 30_000, belief.key)

    # Act
    selection = rank_and_select([expensive, cheap], beliefs, {"OptionsEvent": 40_000}, cache=PurchaseCache(), now=NOW)

    # Assert
    assert [decision.price_atomic for decision in selection.bought] == [30_000]
    assert [decision.outcome for decision in selection.skipped] == [OUTCOME_SKIP_BALANCE]


def test_rank_and_select_shares_the_monthly_cap_across_agents():
    # Arrange: two agents with plenty of balance but only 50 000 left this month
    perps = new_belief("OnchainPerps", "BTC", "direction", 500.0, NOW)
    news = new_belief("NewsSentiment", "ETH", "direction", 500.0, NOW)
    beliefs = index_beliefs([perps, news])
    candidates = [
        _candidate("OnchainPerps", "perps_crowding", {"assets": "BTC"}, 30_000, perps.key),
        _candidate("NewsSentiment", "news_sentiment", {"q": "ETH"}, 30_000, news.key),
    ]
    balances = {"OnchainPerps": TEN_AUSD, "NewsSentiment": TEN_AUSD}

    # Act
    selection = rank_and_select(
        candidates, beliefs, balances, cache=PurchaseCache(), now=NOW, monthly_remaining_atomic=50_000
    )

    # Assert
    assert len(selection.bought) == 1
    assert [decision.outcome for decision in selection.skipped] == [OUTCOME_SKIP_MONTHLY_CAP]


def test_rank_and_select_skips_purchases_that_are_still_cached():
    # Arrange
    beliefs = index_beliefs([new_belief("NewsSentiment", "BTC", "direction", 500.0, NOW)])
    candidates = generate_candidates(AGENT_SELLERS, ["BTC"], beliefs, PRICES, SigmaLearner())
    cache = PurchaseCache()
    cache.record("news_sentiment", {"q": "BTC"}, NOW - timedelta(seconds=30))

    # Act
    selection = rank_and_select(candidates, beliefs, {"NewsSentiment": TEN_AUSD}, cache=cache, now=NOW)

    # Assert
    assert selection.bought == []
    assert [decision.outcome for decision in selection.skipped] == [OUTCOME_SKIP_CACHED]


def test_rank_and_select_buys_the_same_data_only_once_per_round():
    # Arrange: two agents both want news_sentiment for BTC
    beliefs = index_beliefs(
        [
            new_belief("OnchainPerps", "BTC", "direction", 500.0, NOW),
            new_belief("NewsSentiment", "BTC", "direction", 500.0, NOW),
        ]
    )
    candidates = [
        candidate
        for candidate in generate_candidates(AGENT_SELLERS, ["BTC"], beliefs, PRICES, SigmaLearner())
        if candidate.seller_id == "news_sentiment"
    ]
    balances = {"OnchainPerps": TEN_AUSD, "NewsSentiment": TEN_AUSD}

    # Act
    selection = rank_and_select(candidates, beliefs, balances, cache=PurchaseCache(), now=NOW)

    # Assert
    assert len(selection.bought) == 1
    assert [decision.outcome for decision in selection.skipped] == [OUTCOME_SKIP_DUPLICATE]
    assert "duplicate" in selection.skipped[0].reason


def test_rank_and_select_logs_low_value_skips_with_their_numbers():
    # Arrange: belief far from τ with a seller that barely moves it → EVOI ≈ 0
    belief = new_belief("NewsSentiment", "BTC", "direction", 500.0, NOW, p=0.95)
    beliefs = index_beliefs([belief])
    learner = SigmaLearner()
    for _ in range(50):
        learner.observe("news_sentiment", "direction", delta_p=0.0)
    candidates = generate_candidates(AGENT_SELLERS, ["BTC"], beliefs, PRICES, learner)

    # Act
    selection = rank_and_select(candidates, beliefs, {"NewsSentiment": TEN_AUSD}, cache=PurchaseCache(), now=NOW)

    # Assert
    (skip,) = selection.skipped
    assert skip.outcome == OUTCOME_SKIP_LOW_EVOI
    assert skip.reason.startswith("skipped: EVOI $0.000 < price $0.03 × 1.5")


def test_rank_and_select_does_not_write_to_the_cache():
    # Arrange
    beliefs = index_beliefs([new_belief("NewsSentiment", "BTC", "direction", 500.0, NOW)])
    candidates = generate_candidates(AGENT_SELLERS, ["BTC"], beliefs, PRICES, SigmaLearner())
    cache = PurchaseCache()

    # Act
    rank_and_select(candidates, beliefs, {"NewsSentiment": TEN_AUSD}, cache=cache, now=NOW)

    # Assert
    assert cache.is_cached("news_sentiment", {"q": "BTC"}, NOW) is False


def test_rank_and_select_treats_an_unknown_agent_balance_as_zero():
    # Arrange
    beliefs = index_beliefs([new_belief("NewsSentiment", "BTC", "direction", 500.0, NOW)])
    candidates = generate_candidates(AGENT_SELLERS, ["BTC"], beliefs, PRICES, SigmaLearner())

    # Act
    selection = rank_and_select(candidates, beliefs, {}, cache=PurchaseCache(), now=NOW)

    # Assert
    assert [decision.outcome for decision in selection.skipped] == [OUTCOME_SKIP_BALANCE]
