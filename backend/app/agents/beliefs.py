"""
Belief state of the sector agents, and the pure rules that update it (SPEC §5).

A *belief* is one agent's probability `p` that one binary decision about one asset
is "yes", together with the threshold `τ` above which the agent would act and the
user's typical stake `S_usd`. The VOI gate (`app/agents/voi.py`) prices research
against exactly these three numbers, so every purchase is tied to a decision.

Decision types (the `decision` string of a Belief):

    "direction"     Is a long position in <asset> favoured this session?   p = P(up)
    "above:<K>"     Will <asset> finish above strike K at the next expiry?  p = P(S_T > K)
    "pm_yes"        Is the top Gemini prediction-market contract for <asset> mispriced
                    in favour of YES?  p = 0.5 + net edge (p < 0.5 means NO is favoured)

Flow: the agent loop buys a seller's output → picks the matching `update_from_*`
rule below → gets `BeliefUpdate(new_p, delta_p, explanation)` → `apply_update`
stores the new belief → `crossed_threshold` / `is_significant_move` decide whether
a Finding is emitted, and `|delta_p|` is fed to `SigmaLearner` in `voi.py`.

Every update is clamped to [0.01, 0.99]: research never makes the desk certain.
The shift sizes below are documented heuristics, not fitted parameters.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import NamedTuple

from app.sellers.optionmath import prob_above

# ─── probability bounds and defaults ─────────────────────────────────────────

P_MIN = 0.01
P_MAX = 0.99
DEFAULT_P = 0.5            # no view yet
DEFAULT_TAU = 0.5          # act when the decision is more likely right than wrong
SIGNIFICANT_MOVE_DELTA = 0.05   # δ: a move larger than this is worth a Finding

# ─── decision types ──────────────────────────────────────────────────────────

DECISION_DIRECTION = "direction"
DECISION_ABOVE_STRIKE = "above"
DECISION_PM_YES = "pm_yes"
DECISION_SEPARATOR = ":"

# ─── per-seller update parameters (heuristics, documented at each rule) ──────

# perps_crowding: a crowding z-score moves p by at most ±10 points, contrarian.
PERPS_MAX_SHIFT = 0.10

# news_sentiment: GDELT tone is roughly −10..+10; 1 tone point of change ≈ 2 points of p.
NEWS_SHIFT_PER_TONE_POINT = 0.02
NEWS_MAX_TONE_SHIFT = 0.10
NEWS_SHIFT_PER_EVENT_FLAG = 0.03
NEWS_MAX_TOTAL_SHIFT = 0.15
NEWS_BULLISH_FLAGS = frozenset({"listing", "upgrade", "etf"})
NEWS_BEARISH_FLAGS = frozenset({"hack", "exploit", "delisting", "lawsuit", "sec", "downgrade", "halt", "bankruptcy"})

# macro_pulse: accelerating inflation / rising policy rate are bearish for risk assets.
MACRO_SHIFT_PER_CPI_POINT = 0.10      # per percentage point change in YoY CPI
MACRO_SHIFT_PER_RATE_POINT = 0.05     # per percentage point change in the fed funds rate
MACRO_MAX_SHIFT = 0.08

# insider_cluster: ≥ 2 distinct insiders buying in the open market is a bullish signal.
INSIDER_MIN_CLUSTER_SIZE = 2
INSIDER_BASE_SHIFT = 0.04
INSIDER_SHIFT_PER_EXTRA_INSIDER = 0.01
INSIDER_LARGE_PURCHASE_USD = 1_000_000.0
INSIDER_LARGE_PURCHASE_BONUS = 0.02
INSIDER_MAX_SHIFT = 0.10


# ─── data types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Belief:
    """One agent's view on one binary decision about one asset."""

    agent: str              # e.g. "OptionsEvent"
    asset: str              # e.g. "BTC" or "NVDA"
    decision: str           # e.g. "direction", "above:100000", "pm_yes"
    p: float                # probability the decision is "yes"
    tau: float              # act iff p > tau
    stake_usd: float        # user's typical position size for this decision
    updated_at: datetime

    @property
    def key(self) -> str:
        """Stable id used by the planner and the database: "<agent>:<asset>:<decision>"."""
        return belief_key(self.agent, self.asset, self.decision)

    @property
    def decision_type(self) -> str:
        """The decision without its qualifier, e.g. "above" for "above:100000"."""
        return decision_type(self.decision)


class BeliefUpdate(NamedTuple):
    """Result of one update rule. Unpacks as `(new_p, delta_p, explanation)`."""

    new_p: float
    delta_p: float
    explanation: str


# ─── helpers ─────────────────────────────────────────────────────────────────

def belief_key(agent: str, asset: str, decision: str) -> str:
    """Join agent, asset and decision into the key "<agent>:<asset>:<decision>"."""
    return f"{agent}{DECISION_SEPARATOR}{asset}{DECISION_SEPARATOR}{decision}"


def _format_strike(strike: float) -> str:
    """Whole-number strikes print without decimals (100000.0 → "100000"); others as-is (2.5 → "2.5")."""
    if float(strike).is_integer():
        return str(int(strike))
    return str(strike)


def above_strike_decision(strike: float) -> str:
    """Decision string for "finishes above K", e.g. 100000 → "above:100000"."""
    return f"{DECISION_ABOVE_STRIKE}{DECISION_SEPARATOR}{_format_strike(strike)}"


def decision_type(decision: str) -> str:
    """The part of a decision string before the first ":" ("above:100000" → "above")."""
    return decision.split(DECISION_SEPARATOR, 1)[0]


def decision_qualifier(decision: str) -> str | None:
    """The part after the first ":" ("above:100000" → "100000"), or None if there is none."""
    parts = decision.split(DECISION_SEPARATOR, 1)
    return parts[1] if len(parts) == 2 else None


def clamp_probability(p: float) -> float:
    """Clamp a probability to [P_MIN, P_MAX] = [0.01, 0.99]."""
    return min(P_MAX, max(P_MIN, p))


def _clamp_shift(shift: float, limit: float) -> float:
    """Clamp a probability shift to [−limit, +limit]."""
    return min(limit, max(-limit, shift))


def _logistic(x: float) -> float:
    """Logistic function 1 / (1 + e^(−x))."""
    return 1.0 / (1.0 + math.exp(-x))


def _result(p_old: float, p_target: float, explanation: str) -> BeliefUpdate:
    """Clamp the target probability and package it with Δp = new − old."""
    new_p = clamp_probability(p_target)
    return BeliefUpdate(new_p=new_p, delta_p=new_p - p_old, explanation=explanation)


def new_belief(
    agent: str,
    asset: str,
    decision: str,
    stake_usd: float,
    now: datetime,
    p: float = DEFAULT_P,
    tau: float = DEFAULT_TAU,
) -> Belief:
    """A fresh belief with no view yet (p = 0.5 unless given)."""
    return Belief(
        agent=agent,
        asset=asset,
        decision=decision,
        p=clamp_probability(p),
        tau=tau,
        stake_usd=stake_usd,
        updated_at=now,
    )


def index_beliefs(beliefs: Iterable[Belief]) -> dict[str, Belief]:
    """Map each belief's key to the belief (the shape the planner expects)."""
    return {belief.key: belief for belief in beliefs}


def apply_update(belief: Belief, update: BeliefUpdate, now: datetime) -> Belief:
    """Return a copy of `belief` with p = update.new_p and updated_at = now."""
    return replace(belief, p=update.new_p, updated_at=now)


# ─── update rules, one per seller ────────────────────────────────────────────

def update_from_pm_edge(
    p_old: float,
    model_prob: float,
    market_prob: float,
    fee_per_contract: float = 0.0,
) -> BeliefUpdate:
    """
    pm_edge → "pm_yes" belief. Sets p from the model-vs-market gap:

        edge     = model_prob − market_prob              (options-implied N(d2) vs YES price)
        net_edge = sign(edge) · max(0, |edge| − fee)     (fee per $1 contract, in probability points)
        p        = 0.5 + net_edge

    So with τ = 0.5 the decision margin p − τ is exactly the tradable edge after fees.
    """
    edge = model_prob - market_prob
    net_edge = math.copysign(max(0.0, abs(edge) - fee_per_contract), edge)
    explanation = (
        f"pm_edge: options-implied {model_prob:.1%} vs market {market_prob:.1%}, "
        f"fee {fee_per_contract:.3f} → net edge {net_edge:+.3f}"
    )
    return _result(p_old, DEFAULT_TAU + net_edge, explanation)


def update_from_perps_crowding(p_old: float, crowding_z: float) -> BeliefUpdate:
    """
    perps_crowding → "direction" belief. Contrarian shift by a logistic of the crowding z:

        shift = −PERPS_MAX_SHIFT · (2 · logistic(z) − 1)      (in (−0.10, +0.10))
        p     = p_old + shift

    z > 0 means crowded long (high funding / OI build-up) → p(up) goes down;
    z < 0 means crowded short → p(up) goes up; z = 0 leaves p unchanged.
    """
    shift = -PERPS_MAX_SHIFT * (2.0 * _logistic(crowding_z) - 1.0)
    explanation = f"perps_crowding: crowding z {crowding_z:+.2f} → contrarian shift {shift:+.3f}"
    return _result(p_old, p_old + shift, explanation)


def _news_flag_shift(event_flags: Iterable[str]) -> float:
    """+0.03 per bullish flag, −0.03 per bearish flag (case-insensitive); unknown flags count 0."""
    shift = 0.0
    for flag in event_flags:
        normalized = flag.strip().lower()
        if normalized in NEWS_BULLISH_FLAGS:
            shift += NEWS_SHIFT_PER_EVENT_FLAG
        elif normalized in NEWS_BEARISH_FLAGS:
            shift -= NEWS_SHIFT_PER_EVENT_FLAG
    return shift


def update_from_news_sentiment(
    p_old: float,
    tone_delta: float,
    event_flags: Iterable[str] = (),
) -> BeliefUpdate:
    """
    news_sentiment → "direction" belief. Shifts p by the change in news tone plus event flags:

        tone_shift = clamp(0.02 · tone_delta, ±0.10)        (tone_delta = later-half − earlier-half mean tone)
        flag_shift = 0.03 · (#bullish flags − #bearish flags)
        p          = p_old + clamp(tone_shift + flag_shift, ±0.15)

    Bullish flags: listing, upgrade, ETF. Bearish: hack, exploit, delisting, lawsuit,
    SEC, downgrade, halt, bankruptcy. "guidance" is ambiguous and counts 0.
    """
    flags = list(event_flags)
    tone_shift = _clamp_shift(NEWS_SHIFT_PER_TONE_POINT * tone_delta, NEWS_MAX_TONE_SHIFT)
    flag_shift = _news_flag_shift(flags)
    shift = _clamp_shift(tone_shift + flag_shift, NEWS_MAX_TOTAL_SHIFT)
    flag_text = ", ".join(flags) if flags else "none"
    explanation = (
        f"news_sentiment: tone Δ {tone_delta:+.2f} (shift {tone_shift:+.3f}), "
        f"flags [{flag_text}] (shift {flag_shift:+.3f}) → total {shift:+.3f}"
    )
    return _result(p_old, p_old + shift, explanation)


def update_from_expected_move(
    p_old: float,
    spot: float,
    strike: float,
    sigma: float,
    t_years: float,
) -> BeliefUpdate:
    """
    expected_move → "above:<K>" belief. Sets p to the options-implied probability:

        p = N(d2),  d2 = (ln(S/K) − σ²T/2) / (σ√T)       (see app/sellers/optionmath.py)
    """
    implied = prob_above(spot, strike, sigma, t_years)
    explanation = (
        f"expected_move: S {spot:,.2f}, K {strike:,.2f}, σ {sigma:.1%}, T {t_years:.4f}y "
        f"→ P(above) {implied:.1%}"
    )
    return _result(p_old, implied, explanation)


def update_from_macro_pulse(
    p_old: float,
    cpi_yoy_change_pts: float,
    fed_funds_change_pts: float = 0.0,
) -> BeliefUpdate:
    """
    macro_pulse → "direction" belief. Tighter macro conditions lower p(up) for risk assets:

        shift = −clamp(0.10 · ΔCPI_yoy + 0.05 · Δfed_funds, ±0.08)
        p     = p_old + shift

    Both changes are in percentage points versus the previous reading
    (e.g. YoY CPI 3.1 % → 3.4 % is +0.3).
    """
    tightening = MACRO_SHIFT_PER_CPI_POINT * cpi_yoy_change_pts + MACRO_SHIFT_PER_RATE_POINT * fed_funds_change_pts
    shift = -_clamp_shift(tightening, MACRO_MAX_SHIFT)
    explanation = (
        f"macro_pulse: YoY CPI Δ {cpi_yoy_change_pts:+.2f}pp, fed funds Δ {fed_funds_change_pts:+.2f}pp "
        f"→ shift {shift:+.3f}"
    )
    return _result(p_old, p_old + shift, explanation)


def update_from_insider_cluster(
    p_old: float,
    distinct_insiders: int,
    total_purchase_usd: float,
) -> BeliefUpdate:
    """
    insider_cluster → "direction" belief. A cluster (≥ 2 distinct open-market buyers in 10 days) is bullish:

        shift = 0.04 + 0.01 · (insiders − 2) + (0.02 if total ≥ $1M else 0),  capped at 0.10
        p     = p_old + shift

    Fewer than 2 insiders is "no cluster": p is unchanged (absence of buying is weak evidence).
    """
    if distinct_insiders < INSIDER_MIN_CLUSTER_SIZE:
        explanation = f"insider_cluster: {distinct_insiders} insider buyer(s) — no cluster, no change"
        return _result(p_old, p_old, explanation)
    extra_insiders = distinct_insiders - INSIDER_MIN_CLUSTER_SIZE
    shift = INSIDER_BASE_SHIFT + INSIDER_SHIFT_PER_EXTRA_INSIDER * extra_insiders
    if total_purchase_usd >= INSIDER_LARGE_PURCHASE_USD:
        shift += INSIDER_LARGE_PURCHASE_BONUS
    shift = min(shift, INSIDER_MAX_SHIFT)
    explanation = (
        f"insider_cluster: {distinct_insiders} insiders bought ${total_purchase_usd:,.0f} "
        f"→ shift {shift:+.3f}"
    )
    return _result(p_old, p_old + shift, explanation)


# ─── when does a belief change matter? ───────────────────────────────────────

def crossed_threshold(old_p: float, new_p: float, tau: float) -> bool:
    """
    True when the decision flips: the agent acts iff p > τ, so the belief crossed
    iff (old_p > τ) differs from (new_p > τ). Landing exactly on τ counts as "not acting".
    """
    return (old_p > tau) != (new_p > tau)


def is_significant_move(delta_p: float, delta: float = SIGNIFICANT_MOVE_DELTA) -> bool:
    """True when |Δp| is strictly greater than δ (default 0.05)."""
    return abs(delta_p) > delta
