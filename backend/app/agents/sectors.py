"""
The four sector agents of the research desk: what each one buys, which decisions it holds
beliefs about, which watchlist assets it covers, and how a seller's paid answer becomes a
belief update (SPEC §5).

    sector          sellers                          decisions per asset            covers
    OnchainPerps    perps_crowding, news_sentiment   direction                      crypto
    OptionsEvent    expected_move, pm_edge           above:<K> (+ pm_yes, crypto)   crypto + equities
    NewsSentiment   news_sentiment                   direction                      crypto + equities
    MacroFilings    macro_pulse, insider_cluster     direction                      crypto + equities
                                                     (insider_cluster: equities only, see planner)

Flow: `initial_beliefs` seeds every (agent, asset, decision) at p = 0.5 → the agent loop
(`app/agents/base.py`) buys research → `read_update(seller_id, belief, data)` picks the matching
rule from `app/agents/beliefs.py` and returns the update plus code-formatted evidence that
`app/agents/findings.py` puts into a Finding.

The strike K of an "above:K" decision is the next round number above today's spot:
    step = 10^(floor(log10 S) − 1),   K = (floor(S / step) + 1) · step
e.g. S = 97,345 → step 1,000 → K = 98,000;  S = 182.30 → step 10 → K = 190.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agents.beliefs import (
    DECISION_ABOVE_STRIKE,
    DECISION_DIRECTION,
    DECISION_PM_YES,
    Belief,
    BeliefUpdate,
    above_strike_decision,
    decision_qualifier,
    new_belief,
    update_from_expected_move,
    update_from_insider_cluster,
    update_from_macro_pulse,
    update_from_news_sentiment,
    update_from_perps_crowding,
    update_from_pm_edge,
)
from app.agents.planner import (
    AGENT_SELLERS,
    ASSET_CLASS_CRYPTO,
    ASSET_CLASS_EQUITY,
    SELLER_EXPECTED_MOVE,
    SELLER_INSIDER_CLUSTER,
    SELLER_MACRO_PULSE,
    SELLER_NEWS_SENTIMENT,
    SELLER_PERPS_CROWDING,
    SELLER_PM_EDGE,
    asset_class,
)
from app.constants import AGENT_NAMES

# ─── constants ────────────────────────────────────────────────────────────────

DEFAULT_STAKE_USD = 1_000.0           # used when the user set no stake for an asset
STRIKE_STEP_DIGITS_BELOW = 1          # step = 10^(digits of S − 1 − this) → about 1–10 % OTM
BEARISH_RISK_FLAGS = frozenset({"hack", "exploit", "halt", "bankruptcy", "delisting", "lawsuit"})

# Finding directions (docs/API.md Finding.direction)
DIRECTION_BULLISH = "bullish"
DIRECTION_BEARISH = "bearish"
DIRECTION_NEUTRAL = "neutral"
DIRECTION_RISK = "risk"


# ─── sector specs ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SectorSpec:
    """One sector agent: its sellers, the decision types it holds per asset, and what it covers."""

    name: str
    sellers: tuple[str, ...]
    decisions: tuple[str, ...]            # decision types: "direction", "above", "pm_yes"
    asset_classes: frozenset[str]         # "crypto" and/or "equity"
    description: str
    crypto_only_decisions: frozenset[str] = field(default_factory=frozenset)

    def covers(self, asset: str) -> bool:
        """True when this agent researches `asset` (crypto vs equities)."""
        return asset_class(asset) in self.asset_classes

    def decisions_for(self, asset: str) -> tuple[str, ...]:
        """The decision types this agent holds for one asset (pm_yes only for crypto)."""
        if not self.covers(asset):
            return ()
        is_crypto = asset_class(asset) == ASSET_CLASS_CRYPTO
        return tuple(d for d in self.decisions if is_crypto or d not in self.crypto_only_decisions)


BOTH_CLASSES = frozenset({ASSET_CLASS_CRYPTO, ASSET_CLASS_EQUITY})

SECTORS: dict[str, SectorSpec] = {
    "OnchainPerps": SectorSpec(
        name="OnchainPerps",
        sellers=AGENT_SELLERS["OnchainPerps"],
        decisions=(DECISION_DIRECTION,),
        asset_classes=frozenset({ASSET_CLASS_CRYPTO}),
        description="Crypto perps: funding/open-interest crowding plus crypto news",
    ),
    "OptionsEvent": SectorSpec(
        name="OptionsEvent",
        sellers=AGENT_SELLERS["OptionsEvent"],
        decisions=(DECISION_ABOVE_STRIKE, DECISION_PM_YES),
        asset_classes=BOTH_CLASSES,
        description="Options-implied moves around events and prediction-market edge",
        crypto_only_decisions=frozenset({DECISION_PM_YES}),
    ),
    "NewsSentiment": SectorSpec(
        name="NewsSentiment",
        sellers=AGENT_SELLERS["NewsSentiment"],
        decisions=(DECISION_DIRECTION,),
        asset_classes=BOTH_CLASSES,
        description="News headlines, tone trend and event flags",
    ),
    "MacroFilings": SectorSpec(
        name="MacroFilings",
        sellers=AGENT_SELLERS["MacroFilings"],
        decisions=(DECISION_DIRECTION,),
        asset_classes=BOTH_CLASSES,
        description="US macro (CPI, Fed, curve) and SEC insider-buying clusters",
    ),
}

assert tuple(SECTORS) == AGENT_NAMES, "sector order must match AGENT_NAMES"


# ─── initial beliefs ──────────────────────────────────────────────────────────

def strike_above(spot: float) -> float:
    """
    The next round strike above spot: step = 10^(floor(log10 S) − 1), K = (floor(S/step) + 1)·step.
    97,345 → 98,000; 3,512 → 3,600; 182.3 → 190; 0.62 → 0.63.
    """
    if spot <= 0 or not math.isfinite(spot):
        raise ValueError(f"spot must be a positive number, got {spot}")
    step = 10.0 ** (math.floor(math.log10(spot)) - STRIKE_STEP_DIGITS_BELOW)
    strike = (math.floor(spot / step) + 1) * step
    return round(strike, 10)


def _decision_string(decision_type: str, asset: str, spots: Mapping[str, float]) -> str | None:
    """Full decision string for a type; "above" needs a known spot (else None = not held yet)."""
    if decision_type != DECISION_ABOVE_STRIKE:
        return decision_type
    spot = spots.get(asset.upper())
    if spot is None or spot <= 0:
        return None
    return above_strike_decision(strike_above(spot))


def initial_beliefs(
    spec: SectorSpec,
    watchlist: Iterable[str],
    stakes_usd: Mapping[str, float],
    spots: Mapping[str, float],
    now: datetime,
) -> list[Belief]:
    """
    One fresh belief (p = 0.5, τ = 0.5) per covered watchlist asset and decision type.
    Stake = the user's stake for the asset (default $1,000). "above:K" beliefs need a spot.
    """
    beliefs: list[Belief] = []
    for raw_asset in watchlist:
        asset = raw_asset.upper()
        stake = float(stakes_usd.get(asset, stakes_usd.get(raw_asset, DEFAULT_STAKE_USD)))
        for decision_type in spec.decisions_for(asset):
            decision = _decision_string(decision_type, asset, spots)
            if decision is not None:
                beliefs.append(new_belief(spec.name, asset, decision, stake, now))
    return beliefs


# ─── reading a seller's answer into a belief update ─────────────────────────

@dataclass(frozen=True)
class SellerReading:
    """A belief update read from one paid answer, plus evidence strings (formatted by code)."""

    update: BeliefUpdate
    evidence: dict[str, str]
    risk: bool = False                    # a bearish event flag (hack, halt, ...) was seen


def _find_row(rows: Iterable[Mapping[str, Any]], key: str, value: str) -> Mapping[str, Any] | None:
    """First row whose `key` equals `value` (case-insensitive), or None."""
    for row in rows or []:
        if str(row.get(key, "")).upper() == value.upper():
            return row
    return None


def _read_perps(belief: Belief, data: Mapping[str, Any]) -> SellerReading | None:
    """perps_crowding: the asset's crowding z → contrarian shift."""
    row = _find_row(data.get("assets", []), "asset", belief.asset)
    if row is None or row.get("crowding_z") is None:
        return None
    crowding_z = float(row["crowding_z"])
    evidence = {
        "crowding": f"{row.get('label', 'n/a')} (z {crowding_z:+.2f})",
        "funding (annualized)": f"{float(row.get('funding_annualized_pct', 0.0)):+.1f}%",
    }
    return SellerReading(update_from_perps_crowding(belief.p, crowding_z), evidence)


def _read_news(belief: Belief, data: Mapping[str, Any]) -> SellerReading:
    """news_sentiment: tone delta + event flags."""
    tone_delta = float(data.get("tone_delta") or 0.0)
    flags = [str(flag) for flag in data.get("event_flags") or []]
    evidence = {
        "news tone change": f"{tone_delta:+.2f}",
        "event flags": ", ".join(flags) if flags else "none",
        "articles": str(int(data.get("article_count") or 0)),
    }
    risk = any(flag.lower() in BEARISH_RISK_FLAGS for flag in flags)
    return SellerReading(update_from_news_sentiment(belief.p, tone_delta, flags), evidence, risk)


def _read_macro(belief: Belief, data: Mapping[str, Any]) -> SellerReading:
    """macro_pulse: change in YoY CPI and in fed funds (percentage points)."""
    cpi_change = float(data.get("cpi_yoy_change_pts") or 0.0)
    rate_change = float(data.get("fed_funds_change_pts") or 0.0)
    evidence = {"YoY CPI change": f"{cpi_change:+.2f}pp", "fed funds change": f"{rate_change:+.2f}pp"}
    return SellerReading(update_from_macro_pulse(belief.p, cpi_change, rate_change), evidence)


def _read_insider(belief: Belief, data: Mapping[str, Any]) -> SellerReading:
    """insider_cluster: distinct open-market buyers and their total."""
    insiders = int(data.get("distinct_insiders") or 0)
    total_usd = float(data.get("total_purchase_usd") or 0.0)
    evidence = {"insider buyers": str(insiders), "insider purchases": f"${total_usd:,.0f}"}
    return SellerReading(update_from_insider_cluster(belief.p, insiders, total_usd), evidence)


def _read_expected_move(belief: Belief, data: Mapping[str, Any]) -> SellerReading | None:
    """expected_move: P(S_T > K) = N(d2) from spot, the σ at K and T."""
    strike_text = decision_qualifier(belief.decision)
    if strike_text is None or data.get("spot") is None or data.get("t_years") is None:
        return None
    strike = float(strike_text)
    row = next((r for r in data.get("strikes", []) if math.isclose(float(r.get("strike", -1)), strike)), None)
    if row is None or row.get("sigma_used") is None:
        return None
    spot, sigma, t_years = float(data["spot"]), float(row["sigma_used"]), float(data["t_years"])
    if t_years <= 0 or sigma <= 0:
        return None
    evidence = {
        "spot": f"${spot:,.2f}",
        "implied vol": f"{sigma:.1%}",
        "expected move": f"±{float(data.get('expected_move_pct', 0.0)):.1%}",
    }
    return SellerReading(update_from_expected_move(belief.p, spot, strike, sigma, t_years), evidence)


def _read_pm_edge(belief: Belief, data: Mapping[str, Any]) -> SellerReading | None:
    """pm_edge: the asset's top contract (largest |edge|): model vs market probability after fees."""
    row = _find_row(data.get("rows", []), "asset", belief.asset)
    if row is None:
        return None
    model_p, market_p = float(row["model_p"]), float(row["market_p"])
    fee = float(row.get("fee_per_contract") or 0.0)
    evidence = {
        "contract": str(row.get("title") or row.get("ticker")),
        "market odds": f"{market_p:.1%}",
        "options-implied": f"{model_p:.1%}",
    }
    return SellerReading(update_from_pm_edge(belief.p, model_p, market_p, fee), evidence)


def read_update(seller_id: str, belief: Belief, data: Mapping[str, Any]) -> SellerReading | None:
    """
    Turn one seller's JSON answer into a belief update with the matching `beliefs.py` rule.
    Returns None when the answer has nothing for this belief (e.g. the asset is missing).
    """
    if seller_id == SELLER_PERPS_CROWDING:
        return _read_perps(belief, data)
    if seller_id == SELLER_NEWS_SENTIMENT:
        return _read_news(belief, data)
    if seller_id == SELLER_MACRO_PULSE:
        return _read_macro(belief, data)
    if seller_id == SELLER_INSIDER_CLUSTER:
        return _read_insider(belief, data)
    if seller_id == SELLER_EXPECTED_MOVE:
        return _read_expected_move(belief, data)
    if seller_id == SELLER_PM_EDGE:
        return _read_pm_edge(belief, data)
    return None


def agents_covering(asset: str) -> tuple[str, ...]:
    """Names of the sector agents that research `asset`."""
    return tuple(name for name, spec in SECTORS.items() if spec.covers(asset))
