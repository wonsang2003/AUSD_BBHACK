"""
Planner: which research should the agents buy on this tick? (SPEC §5)

One planning round, all pure (the clock `now` is passed in so tests control time):

    1. generate_candidates  — sellers × watchlist: for every belief an agent holds about
                              a watched asset, each of that agent's sellers that can inform
                              the belief becomes a Candidate (seller, params, price, σ).
    2. rank_and_select      — score every candidate with the VOI gate (`voi.decide`), rank
                              by EVOI / price, and buy greedily while the agent's wallet and
                              the monthly cap allow. Returns bought AND skipped decisions,
                              each with its numbers and reason, so all of them can be logged.

The agent loop then pays for the bought ones with `X402Client`, records each
successful purchase in the `PurchaseCache` (so the same seller+params is not bought
again within its TTL), and updates beliefs with `app/agents/beliefs.py`.

Seller → decision mapping (which beliefs a seller can move):
    perps_crowding, news_sentiment, macro_pulse, insider_cluster → "direction"
    expected_move → "above:<K>"      pm_edge → "pm_yes"
perps_crowding and pm_edge only cover crypto; insider_cluster only covers equities.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from app.agents.beliefs import (
    DECISION_ABOVE_STRIKE,
    DECISION_DIRECTION,
    DECISION_PM_YES,
    Belief,
    decision_qualifier,
)
from app.agents.voi import (
    DEFAULT_MARGIN,
    OUTCOME_SKIP_DUPLICATE,
    BuyDecision,
    SigmaLearner,
    atomic_to_usd,
    decide,
    evoi,
)

# ─── constants ────────────────────────────────────────────────────────────────

DEFAULT_CACHE_TTL_SECONDS = 120     # same default as SellerOffer.cache_ttl_seconds

SELLER_EXPECTED_MOVE = "expected_move"
SELLER_PM_EDGE = "pm_edge"
SELLER_PERPS_CROWDING = "perps_crowding"
SELLER_NEWS_SENTIMENT = "news_sentiment"
SELLER_INSIDER_CLUSTER = "insider_cluster"
SELLER_MACRO_PULSE = "macro_pulse"

# Which sellers each sector agent may buy from (SPEC §5).
AGENT_SELLERS: dict[str, tuple[str, ...]] = {
    "OnchainPerps": (SELLER_PERPS_CROWDING, SELLER_NEWS_SENTIMENT),
    "OptionsEvent": (SELLER_EXPECTED_MOVE, SELLER_PM_EDGE),
    "NewsSentiment": (SELLER_NEWS_SENTIMENT,),
    "MacroFilings": (SELLER_MACRO_PULSE, SELLER_INSIDER_CLUSTER),
}

# Which decision type each seller's output can move.
SELLER_DECISION_TYPE: dict[str, str] = {
    SELLER_PERPS_CROWDING: DECISION_DIRECTION,
    SELLER_NEWS_SENTIMENT: DECISION_DIRECTION,
    SELLER_MACRO_PULSE: DECISION_DIRECTION,
    SELLER_INSIDER_CLUSTER: DECISION_DIRECTION,
    SELLER_EXPECTED_MOVE: DECISION_ABOVE_STRIKE,
    SELLER_PM_EDGE: DECISION_PM_YES,
}

ASSET_CLASS_ANY = "any"
ASSET_CLASS_CRYPTO = "crypto"
ASSET_CLASS_EQUITY = "equity"

# Which asset class each seller covers (sellers not listed cover any asset).
SELLER_ASSET_CLASS: dict[str, str] = {
    SELLER_PERPS_CROWDING: ASSET_CLASS_CRYPTO,     # Hyperliquid perps
    SELLER_PM_EDGE: ASSET_CLASS_CRYPTO,           # Gemini crypto prediction markets
    SELLER_INSIDER_CLUSTER: ASSET_CLASS_EQUITY,   # SEC Form 4
}

# Symbols treated as crypto; everything else on the watchlist is treated as an equity ticker.
CRYPTO_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "AVAX", "LINK", "BNB"})

PM_EDGE_CATEGORY_CRYPTO = "crypto"
EXPIRY_NEXT = "next"


# ─── data types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    """One possible purchase: ask `seller_id` with `params` to inform belief `decision_key`."""

    agent: str
    seller_id: str
    params: dict[str, str]
    price_atomic: int        # AUSD atomic units (6 decimals)
    decision_key: str        # Belief.key of the belief this purchase would update
    sigma: float             # how far this seller typically moves such a belief (SigmaLearner)


@dataclass(frozen=True)
class Selection:
    """Outcome of one planning round: what to buy (in order) and what was skipped and why."""

    bought: list[BuyDecision]
    skipped: list[BuyDecision]

    @property
    def total_price_atomic(self) -> int:
        """Total AUSD the bought decisions will spend."""
        return sum(decision.price_atomic for decision in self.bought)


# ─── purchase cache (TTL dedupe) ─────────────────────────────────────────────

@dataclass(frozen=True)
class _CacheEntry:
    expires_at: datetime
    result: dict | None


class PurchaseCache:
    """
    Remembers recent purchases for their TTL so the same data is never paid for twice.

    Key = seller id + sorted params, e.g. "expected_move?asset=BTC&expiry=next" — the
    agent is not part of the key, so two agents wanting the same data share one
    purchase. An entry is live while now < bought_at + ttl. The stored result (if
    any) lets the second agent reuse the first agent's data.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, _CacheEntry] = {}

    @staticmethod
    def key(seller_id: str, params: Mapping[str, str]) -> str:
        """"<seller>?k1=v1&k2=v2" with params sorted by name, so their order never matters."""
        query = "&".join(f"{name}={params[name]}" for name in sorted(params))
        return f"{seller_id}?{query}"

    def record(
        self,
        seller_id: str,
        params: Mapping[str, str],
        now: datetime,
        result: dict | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        """Store a successful purchase; it stays cached until now + ttl (default: the cache's TTL)."""
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._entries[self.key(seller_id, params)] = _CacheEntry(
            expires_at=now + timedelta(seconds=ttl),
            result=result,
        )

    def _live_entry(self, seller_id: str, params: Mapping[str, str], now: datetime) -> _CacheEntry | None:
        entry = self._entries.get(self.key(seller_id, params))
        if entry is None or now >= entry.expires_at:
            return None
        return entry

    def is_cached(self, seller_id: str, params: Mapping[str, str], now: datetime) -> bool:
        """True if the same seller+params was bought less than TTL seconds ago."""
        return self._live_entry(seller_id, params, now) is not None

    def get(self, seller_id: str, params: Mapping[str, str], now: datetime) -> dict | None:
        """The stored result of a live cached purchase, or None."""
        entry = self._live_entry(seller_id, params, now)
        return entry.result if entry else None

    def prune(self, now: datetime) -> int:
        """Drop expired entries; returns how many were removed."""
        expired = [key for key, entry in self._entries.items() if now >= entry.expires_at]
        for key in expired:
            del self._entries[key]
        return len(expired)


# ─── candidate generation ────────────────────────────────────────────────────

def asset_class(asset: str) -> str:
    """"crypto" for known crypto symbols (BTC, ETH, SOL, ...), otherwise "equity"."""
    return ASSET_CLASS_CRYPTO if asset.upper() in CRYPTO_ASSETS else ASSET_CLASS_EQUITY


def seller_covers(seller_id: str, belief: Belief) -> bool:
    """
    A seller can inform a belief when (a) its output moves that decision type,
    (b) it covers the asset's class, and (c) for "above:<K>" the strike K is known.
    """
    if SELLER_DECISION_TYPE.get(seller_id) != belief.decision_type:
        return False
    required_class = SELLER_ASSET_CLASS.get(seller_id, ASSET_CLASS_ANY)
    if required_class != ASSET_CLASS_ANY and required_class != asset_class(belief.asset):
        return False
    if belief.decision_type == DECISION_ABOVE_STRIKE and decision_qualifier(belief.decision) is None:
        return False
    return True


def seller_params(seller_id: str, belief: Belief) -> dict[str, str]:
    """Query parameters for the seller's endpoint (SPEC §4) when informing this belief."""
    asset = belief.asset.upper()
    if seller_id == SELLER_EXPECTED_MOVE:
        strike = decision_qualifier(belief.decision) or ""
        return {"asset": asset, "expiry": EXPIRY_NEXT, "strikes": strike}
    if seller_id == SELLER_PM_EDGE:
        return {"category": PM_EDGE_CATEGORY_CRYPTO}
    if seller_id == SELLER_PERPS_CROWDING:
        return {"assets": asset}
    if seller_id == SELLER_NEWS_SENTIMENT:
        return {"q": asset}
    if seller_id == SELLER_INSIDER_CLUSTER:
        return {"ticker": asset}
    if seller_id == SELLER_MACRO_PULSE:
        return {}
    raise ValueError(f"unknown seller: {seller_id}")


def generate_candidates(
    agent_sellers: Mapping[str, Sequence[str]],
    watchlist: Iterable[str],
    beliefs: Mapping[str, Belief],
    prices: Mapping[str, int],
    sigma_learner: SigmaLearner,
) -> list[Candidate]:
    """
    Sellers × watchlist → candidates.

    For each belief (in key order) whose asset is on the watchlist, each seller of
    that belief's agent that covers it (see `seller_covers`) and has a known
    positive price yields one Candidate, with σ from the learner for
    (seller, decision type).
    """
    watched = {asset.upper() for asset in watchlist}
    candidates: list[Candidate] = []
    for key in sorted(beliefs):
        belief = beliefs[key]
        if belief.asset.upper() not in watched:
            continue
        for seller_id in agent_sellers.get(belief.agent, ()):
            price_atomic = prices.get(seller_id, 0)
            if price_atomic <= 0 or not seller_covers(seller_id, belief):
                continue
            candidates.append(
                Candidate(
                    agent=belief.agent,
                    seller_id=seller_id,
                    params=seller_params(seller_id, belief),
                    price_atomic=price_atomic,
                    decision_key=belief.key,
                    sigma=sigma_learner.sigma(seller_id, belief.decision_type),
                )
            )
    return candidates


# ─── ranking and greedy selection ────────────────────────────────────────────

def evoi_per_price(candidate: Candidate, belief: Belief) -> float:
    """Ranking key: EVOI (USD) / price (USD). Higher means more information per dollar."""
    return evoi(belief.stake_usd, belief.p, belief.tau, candidate.sigma) / atomic_to_usd(candidate.price_atomic)


def _rank(candidates: Sequence[Candidate], beliefs: Mapping[str, Belief]) -> list[Candidate]:
    """Best EVOI/price first; ties broken by seller id then decision key so the order is stable."""

    def best_first(candidate: Candidate) -> tuple[float, str, str]:
        ratio = evoi_per_price(candidate, beliefs[candidate.decision_key])
        return (-ratio, candidate.seller_id, candidate.decision_key)

    return sorted(candidates, key=best_first)


def _mark_duplicate(decision: BuyDecision, first_decision_key: str) -> BuyDecision:
    """Turn a decision into a "duplicate in this round" skip (the first purchase's data is shared)."""
    reason = (
        f"skipped: duplicate — {decision.seller_id} with the same params is already being bought "
        f"this round for {first_decision_key}"
    )
    return replace(decision, outcome=OUTCOME_SKIP_DUPLICATE, reason=reason)


def rank_and_select(
    candidates: Sequence[Candidate],
    beliefs: Mapping[str, Belief],
    balances: Mapping[str, int],
    *,
    cache: PurchaseCache,
    now: datetime,
    monthly_remaining_atomic: int | None = None,
    margin: float = DEFAULT_MARGIN,
) -> Selection:
    """
    Greedy selection by EVOI / price within each agent's balance and the monthly cap.

    Walk the candidates best-first and run the VOI gate on each with what is *still*
    available; a "buy" deducts the price from that agent's balance and from the
    monthly cap. A second candidate for the same seller+params in this round is
    skipped as a duplicate. The cache is only read here — the agent loop records a
    purchase after its payment succeeds.
    """
    remaining_balance = {agent: int(amount) for agent, amount in balances.items()}
    remaining_monthly = monthly_remaining_atomic
    planned: dict[str, str] = {}            # cache key → decision key that is buying it
    bought: list[BuyDecision] = []
    skipped: list[BuyDecision] = []

    for candidate in _rank(candidates, beliefs):
        belief = beliefs[candidate.decision_key]
        decision = decide(
            candidate,
            belief,
            balance_atomic=remaining_balance.get(candidate.agent, 0),
            monthly_remaining_atomic=remaining_monthly,
            cached=cache.is_cached(candidate.seller_id, candidate.params, now),
            margin=margin,
        )
        cache_key = PurchaseCache.key(candidate.seller_id, candidate.params)
        if cache_key in planned:
            skipped.append(_mark_duplicate(decision, planned[cache_key]))
            continue
        if not decision.bought:
            skipped.append(decision)
            continue
        bought.append(decision)
        planned[cache_key] = candidate.decision_key
        remaining_balance[candidate.agent] = remaining_balance.get(candidate.agent, 0) - candidate.price_atomic
        if remaining_monthly is not None:
            remaining_monthly -= candidate.price_atomic

    return Selection(bought=bought, skipped=skipped)
