"""
Allocator: how much of today's research budget each sector agent gets (SPEC §6).

Two steps, both pure (the keeper passes in chain state and a random seed):

    1. daily_budget — how much may be spent today at all:
           B_day = min(dailyCap, allowance_remaining / days_remaining, monthly_cap_remaining)
                   + rollover (≤ 25 % of yesterday's unspent)
       and the rollover can never push B_day past any of those hard limits.

    2. allocate — split B_day across agents with Thompson sampling:
           θ_k ~ LogNormal(μ_k, s_k²)          (a draw of agent k's value per $ spent)
           w_k = θ_k · (1 + β · events_k)      (β = 0.5; events_k = today's events for k)
           shares = w normalised, then held within [floor 10 %, cap 50 %]
           a_k = B_day · share_k               (rounded to integer AUSD atomic units)
       Each Allocation carries human-readable reasons, e.g.
       "OptionsEvent +40% vs equal split: NVDA earnings today".

Learning: `AgentPosterior.update` folds in one realized value per $ (from
`app/agents/voi.realized_value_per_dollar`) with a conjugate normal update on the
log scale, so agents that turn research into belief changes get more budget.

Invariants (tested): Σ a_k ≤ B_day; every share within [floor, cap]; the same seed
gives the same allocation. Infeasible bounds are relaxed, not ignored: if
n · floor > 1 the floor becomes 1/n, and if n · cap < 1 the cap becomes 1/n (so a
single agent gets the whole budget). floor > cap is a configuration error.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.constants import ONE_AUSD

# ─── daily budget ────────────────────────────────────────────────────────────

DEFAULT_ROLLOVER_LIMIT = 0.25       # at most 25 % of yesterday's unspent carries over
MIN_DAYS_REMAINING = 1              # the last day may use everything that is left

LIMIT_DAILY_CAP = "daily cap"
LIMIT_ALLOWANCE_PACE = "allowance pace"
LIMIT_MONTHLY_CAP = "monthly cap"

# ─── Thompson sampling ───────────────────────────────────────────────────────

PRIOR_MU = 0.0                      # log(1): prior median of $1 of value per $1 spent
PRIOR_S = 1.0                       # prior standard deviation of μ on the log scale
OBSERVATION_NOISE_SD = 1.0          # known noise of one realized log value-per-$
MIN_VALUE_PER_DOLLAR = 0.01         # realized values are floored here before taking log

DEFAULT_EVENT_BETA = 0.5            # each event today boosts the weight by +50 %
DEFAULT_FLOOR_SHARE = 0.10          # every agent gets at least 10 % ...
DEFAULT_CAP_SHARE = 0.50            # ... and at most 50 % of B_day

# ─── numerics ────────────────────────────────────────────────────────────────

BISECTION_STEPS = 100               # far more than the 53 bits a float can resolve
MAX_SCALE_DOUBLINGS = 200
SHARE_TOLERANCE = 1e-9              # "at the floor / cap" when this close to it
PERCENT = 100


# ─── data types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DailyBudget:
    """Today's budget and how it was derived (all amounts in AUSD atomic units)."""

    amount_atomic: int        # B_day: what the allocator may hand out today
    base_atomic: int          # min(daily cap, allowance pace, monthly cap remaining)
    rollover_atomic: int      # what yesterday's unspent added on top (after hard limits)
    limited_by: str           # which hard limit set the base
    reason: str


@dataclass(frozen=True)
class AgentPosterior:
    """
    Belief about agent k's value per $ spent: LogNormal(μ, s²), i.e. log(value/$) ~ N(μ, s²).

    The prior is μ = log(1) = 0, s = 1: "probably about break-even, very unsure".
    """

    agent: str
    mu: float = PRIOR_MU
    s: float = PRIOR_S
    observations: int = 0

    @property
    def median_value_per_dollar(self) -> float:
        """Median of the LogNormal: e^μ."""
        return math.exp(self.mu)

    def update(self, value_per_dollar: float, noise_sd: float = OBSERVATION_NOISE_SD) -> AgentPosterior:
        """
        Return a new posterior after one realized value per $ (conjugate normal update on y = log v):

            precision' = 1/s² + 1/noise²
            μ'         = (μ/s² + y/noise²) / precision'
            s'         = 1 / √precision'

        v is floored at 0.01 first, so a day with zero value counts as very low, not −∞.
        """
        log_value = math.log(max(value_per_dollar, MIN_VALUE_PER_DOLLAR))
        prior_precision = 1.0 / (self.s * self.s)
        noise_precision = 1.0 / (noise_sd * noise_sd)
        posterior_precision = prior_precision + noise_precision
        posterior_mu = (self.mu * prior_precision + log_value * noise_precision) / posterior_precision
        return AgentPosterior(
            agent=self.agent,
            mu=posterior_mu,
            s=1.0 / math.sqrt(posterior_precision),
            observations=self.observations + 1,
        )

    def sample(self, rng: random.Random) -> float:
        """One Thompson draw θ = e^X with X ~ N(μ, s²)."""
        return math.exp(rng.gauss(self.mu, self.s))


@dataclass(frozen=True)
class Allocation:
    """One agent's share of today's budget."""

    agent: str
    amount_atomic: int          # AUSD atomic units the agent should hold for today
    weight: float               # final share of B_day, within [floor, cap]
    reasons: tuple[str, ...]    # human-readable, most important first


# ─── formatting ──────────────────────────────────────────────────────────────

def _format_ausd(amount_atomic: int) -> str:
    """Atomic AUSD → "$12.34" (2 decimals, rounded down so it never overstates)."""
    cents = amount_atomic * PERCENT // ONE_AUSD
    return f"${cents // PERCENT:,}.{cents % PERCENT:02d}"


# ─── step 1: daily budget ────────────────────────────────────────────────────

def _fraction_of(amount_atomic: int, fraction: float) -> int:
    """floor(amount · fraction) for a non-negative amount, as an integer."""
    return math.floor(max(0, amount_atomic) * fraction)


def _tightest_limit(limits: Mapping[str, int]) -> str:
    """Name of the smallest limit (the first one listed wins a tie)."""
    tightest = next(iter(limits))
    for name, value in limits.items():
        if value < limits[tightest]:
            tightest = name
    return tightest


def daily_budget(
    daily_cap: int,
    allowance_remaining: int,
    days_remaining: int,
    monthly_cap_remaining: int | None,
    yesterday_unspent: int,
    rollover_limit: float = DEFAULT_ROLLOVER_LIMIT,
) -> DailyBudget:
    """
    B_day = min(daily_cap, allowance_remaining // days_remaining, monthly_cap_remaining)
            + floor(rollover_limit · yesterday_unspent),
    capped again by daily_cap, monthly_cap_remaining and allowance_remaining.

    All amounts are AUSD atomic units. days_remaining < 1 counts as 1. A None
    monthly cap means "no monthly cap". Negative inputs count as 0.
    """
    days = max(days_remaining, MIN_DAYS_REMAINING)
    allowance = max(0, allowance_remaining)
    limits = {
        LIMIT_DAILY_CAP: max(0, daily_cap),
        LIMIT_ALLOWANCE_PACE: allowance // days,
    }
    if monthly_cap_remaining is not None:
        limits[LIMIT_MONTHLY_CAP] = max(0, monthly_cap_remaining)

    limited_by = _tightest_limit(limits)
    base = limits[limited_by]

    # The rollover may not break a hard limit: the on-chain daily cap, the allowance
    # itself, or the monthly cap. (base is already below all three.)
    hard_ceiling = min(limits[LIMIT_DAILY_CAP], allowance)
    if monthly_cap_remaining is not None:
        hard_ceiling = min(hard_ceiling, limits[LIMIT_MONTHLY_CAP])
    proposed_rollover = _fraction_of(yesterday_unspent, rollover_limit)
    amount = min(base + proposed_rollover, hard_ceiling)
    rollover = amount - base

    reason = (
        f"B_day {_format_ausd(amount)} = base {_format_ausd(base)} (limited by {limited_by}) "
        f"+ rollover {_format_ausd(rollover)} "
        f"(≤ {rollover_limit:.0%} of yesterday's unspent {_format_ausd(max(0, yesterday_unspent))})"
    )
    return DailyBudget(
        amount_atomic=amount,
        base_atomic=base,
        rollover_atomic=rollover,
        limited_by=limited_by,
        reason=reason,
    )


# ─── step 2: allocation ──────────────────────────────────────────────────────

def _effective_bounds(agent_count: int, floor: float, cap: float) -> tuple[float, float, list[str]]:
    """
    Relax floor/cap when they cannot all hold for `agent_count` agents:
        n · floor > 1 → floor = 1/n      (not enough budget for every floor)
        n · cap   < 1 → cap   = 1/n      (caps would leave budget unspent, e.g. 1 agent)
    Returns (floor, cap, notes explaining any relaxation). floor > cap raises ValueError.
    """
    if floor > cap:
        raise ValueError(f"floor share {floor} is above cap share {cap}")
    equal_share = 1.0 / agent_count
    notes: list[str] = []
    if agent_count * floor > 1.0:
        notes.append(f"floor lowered from {floor:.0%} to {equal_share:.0%} ({agent_count} agents)")
        floor = equal_share
    if agent_count * cap < 1.0:
        notes.append(f"cap raised from {cap:.0%} to {equal_share:.0%} ({agent_count} agent(s))")
        cap = equal_share
    return floor, cap, notes


def _event_boosted_weight(draw: float, event_count: int, beta: float) -> float:
    """w_k = θ_k · (1 + β · events_k): each event today raises the weight by β (50 %)."""
    return draw * (1.0 + beta * event_count)


def _clamped_shares(raw_weights: Sequence[float], scale: float, floor: float, cap: float) -> list[float]:
    """share_k = clamp(scale · raw_k, floor, cap) for every agent."""
    return [min(cap, max(floor, scale * weight)) for weight in raw_weights]


def _shares_within_floor_and_cap(raw_weights: Sequence[float], floor: float, cap: float) -> list[float]:
    """
    Normalise weights to shares that sum to 1 while every share stays in [floor, cap].

    The result is the fixed point of "clip to [floor, cap], renormalise the others,
    repeat": shares proportional to the raw weights except where a bound binds,

        share_k = clamp(λ · raw_k, floor, cap),   with λ chosen so that Σ share_k = 1.

    Σ clamp(λ · raw_k) only grows with λ, so λ is found by bisection. (Clipping both
    ends at once and renormalising can strand budget when a floor and a cap bind on
    the same day; solving for λ cannot.) The lower end of the bisection is returned,
    so Σ share_k ≤ 1 and the allocator never hands out more than B_day.
    """
    if sum(raw_weights) <= 0:
        raw_weights = [1.0] * len(raw_weights)
    low, high = 0.0, 1.0
    for _ in range(MAX_SCALE_DOUBLINGS):
        if sum(_clamped_shares(raw_weights, high, floor, cap)) >= 1.0:
            break
        high *= 2.0
    for _ in range(BISECTION_STEPS):
        middle = (low + high) / 2.0
        if sum(_clamped_shares(raw_weights, middle, floor, cap)) <= 1.0:
            low = middle
        else:
            high = middle
    return _clamped_shares(raw_weights, low, floor, cap)


def _to_atomic_amounts(budget_atomic: int, shares: Sequence[float], cap: float) -> list[int]:
    """
    Turn shares into whole atomic units without overspending (largest-remainder rounding):

        1. a_k = floor(budget · share_k)
        2. hand the leftover units out one each, largest fractional part first,
           skipping an agent if one more unit would put it above budget · cap.

    So 0.2999999999 of 10 AUSD becomes exactly 3.000000, not 2.999999. Amounts only
    go up in step 2, so floors still hold; Σ a_k ≤ budget always.
    """
    exact = [budget_atomic * share for share in shares]
    amounts = [math.floor(value) for value in exact]
    leftover = budget_atomic - sum(amounts)

    def fractional_part(index: int) -> float:
        return exact[index] - amounts[index]

    for index in sorted(range(len(shares)), key=fractional_part, reverse=True):
        if leftover <= 0:
            break
        if amounts[index] + 1 <= budget_atomic * cap:
            amounts[index] += 1
            leftover -= 1
    return amounts


def _agent_reasons(
    *,
    agent: str,
    share: float,
    agent_count: int,
    draw: float,
    posterior: AgentPosterior,
    events: Sequence[str],
    beta: float,
    floor: float,
    cap: float,
    budget_atomic: int,
) -> list[str]:
    """Human-readable reasons for one allocation, the headline first."""
    versus_equal = (share * agent_count - 1.0) * PERCENT
    headline = f"{agent} {versus_equal:+.0f}% vs equal split"
    if events:
        headline += ": " + ", ".join(events)
    reasons = [headline]
    if budget_atomic <= 0:
        reasons.append("no budget today (B_day = 0)")
    reasons.append(
        f"Thompson draw θ = {draw:.2f} value/$ (posterior median {posterior.median_value_per_dollar:.2f}, "
        f"{posterior.observations} observation(s))"
    )
    if events:
        reasons.append(f"event boost ×{1.0 + beta * len(events):.2f} for {len(events)} event(s) today")
    if share >= cap - SHARE_TOLERANCE:
        reasons.append(f"held at the {cap:.0%} cap")
    elif share <= floor + SHARE_TOLERANCE:
        reasons.append(f"raised to the {floor:.0%} floor")
    return reasons


def allocate(
    budget_atomic: int,
    posteriors: Sequence[AgentPosterior],
    events_today: Mapping[str, Sequence[str]],
    rng_seed: int,
    beta: float = DEFAULT_EVENT_BETA,
    floor: float = DEFAULT_FLOOR_SHARE,
    cap: float = DEFAULT_CAP_SHARE,
) -> list[Allocation]:
    """
    Split `budget_atomic` across agents by Thompson sampling with event boosts:

        θ_k = draw from agent k's posterior        (draws taken in the order of `posteriors`)
        w_k = θ_k · (1 + β · len(events_today[k]))
        share_k = w_k normalised within [floor, cap]   (see _shares_within_floor_and_cap)
        a_k = budget · share_k in whole atomic units   (see _to_atomic_amounts)

    Σ a_k ≤ budget always (and normally equal to it). Results come back in the
    order of `posteriors`.
    """
    if not posteriors:
        return []
    budget = max(0, budget_atomic)
    agent_count = len(posteriors)
    effective_floor, effective_cap, bound_notes = _effective_bounds(agent_count, floor, cap)

    rng = random.Random(rng_seed)
    draws = [posterior.sample(rng) for posterior in posteriors]
    raw_weights = [
        _event_boosted_weight(draw, len(events_today.get(posterior.agent, ())), beta)
        for draw, posterior in zip(draws, posteriors, strict=True)
    ]
    shares = _shares_within_floor_and_cap(raw_weights, effective_floor, effective_cap)
    amounts = _to_atomic_amounts(budget, shares, effective_cap)

    allocations: list[Allocation] = []
    for posterior, draw, share, amount in zip(posteriors, draws, shares, amounts, strict=True):
        reasons = _agent_reasons(
            agent=posterior.agent,
            share=share,
            agent_count=agent_count,
            draw=draw,
            posterior=posterior,
            events=list(events_today.get(posterior.agent, ())),
            beta=beta,
            floor=effective_floor,
            cap=effective_cap,
            budget_atomic=budget,
        )
        allocations.append(
            Allocation(
                agent=posterior.agent,
                amount_atomic=amount,
                weight=share,
                reasons=tuple(reasons + bound_notes),
            )
        )
    return allocations
