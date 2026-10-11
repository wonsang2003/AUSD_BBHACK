"""
Recharge policy: how much AUSD the keeper moves into and out of agent wallets (SPEC §6).

Pure functions only; the keeper reads balances / on-chain headroom, calls these,
then executes the result (`BudgetController.recharge` for top-ups, an agent-signed
ERC-3009 `transferWithAuthorization(to=owner)` for sweeps).

    Session start   top_up_k = max(0, a_k − balance_k)                   (a_k from the allocator)
    Intraday        if balance_k < 20 % · a_k  AND  realized value/$ today ≥ threshold
                    AND headroom > 0:   refill min(25 % · a_k, headroom)
    Session end     sweep every positive agent balance back to the owner
    Runway          allowance_remaining / average daily spend (alert below 3 days)

Headroom is what the contract would still accept today: min(remainingToday(owner),
agentRemainingToday(owner, agent)). Clipping to it avoids DailyCapExceeded /
AgentCapExceeded reverts.

All money is integer AUSD atomic units. Percent rules use integer arithmetic so the
20 % boundary is exact. Every returned amount is > 0, so the keeper never sends a
zero or negative transfer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.constants import ONE_AUSD
from app.keeper.allocator import Allocation

# ─── constants ────────────────────────────────────────────────────────────────

PERCENT = 100
LOW_BALANCE_PERCENT = 20        # intraday refill only when balance < 20 % of the allocation
REFILL_PERCENT = 25             # an intraday refill adds 25 % of the allocation (≤ headroom)
RUNWAY_ALERT_DAYS = 3.0         # alert when the allowance lasts fewer than 3 more days


# ─── data types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TopUp:
    """Move `amount` from the owner to the agent via `BudgetController.recharge`."""

    agent: str
    amount: int     # AUSD atomic units, always > 0
    reason: str


@dataclass(frozen=True)
class Sweep:
    """Return `amount` from the agent wallet to the owner at session end."""

    agent: str
    amount: int     # AUSD atomic units, always > 0
    reason: str


# ─── formatting ──────────────────────────────────────────────────────────────

def _format_ausd(amount_atomic: int) -> str:
    """Atomic AUSD → "$2.10" (2 decimals, rounded down)."""
    cents = max(0, amount_atomic) * PERCENT // ONE_AUSD
    return f"${cents // PERCENT:,}.{cents % PERCENT:02d}"


# ─── session start ───────────────────────────────────────────────────────────

def session_top_ups(
    allocations: Sequence[Allocation],
    balances: Mapping[str, int],
    agent_headroom: Mapping[str, int] | None = None,
    budget_headroom: int | None = None,
) -> list[TopUp]:
    """
    Top each agent up to its allocation: top_up_k = max(0, a_k − balance_k).

    Optional limits (pass what the contract reports, to avoid reverts):
      agent_headroom   per agent, agentRemainingToday(owner, agent)
      budget_headroom  shared, remainingToday(owner); consumed in allocation order
    A missing balance counts as 0. Agents that need nothing are left out.
    """
    remaining_budget = budget_headroom
    top_ups: list[TopUp] = []
    for allocation in allocations:
        balance = max(0, balances.get(allocation.agent, 0))
        needed = max(0, allocation.amount_atomic - balance)
        amount = needed
        if agent_headroom is not None:
            amount = min(amount, max(0, agent_headroom.get(allocation.agent, 0)))
        if remaining_budget is not None:
            amount = min(amount, max(0, remaining_budget))
        if amount <= 0:
            continue
        reason = (
            f"session start: allocation {_format_ausd(allocation.amount_atomic)} "
            f"− balance {_format_ausd(balance)} = {_format_ausd(needed)}"
        )
        if amount < needed:
            reason += f" (limited to {_format_ausd(amount)} by on-chain headroom)"
        top_ups.append(TopUp(agent=allocation.agent, amount=amount, reason=reason))
        if remaining_budget is not None:
            remaining_budget -= amount
    return top_ups


# ─── intraday ────────────────────────────────────────────────────────────────

def is_balance_low(balance: int, allocation_atomic: int) -> bool:
    """balance < 20 % · allocation, in integers (balance · 100 < allocation · 20)."""
    return max(0, balance) * PERCENT < allocation_atomic * LOW_BALANCE_PERCENT


def refill_size(allocation_atomic: int) -> int:
    """25 % of the allocation, rounded down to a whole atomic unit."""
    return max(0, allocation_atomic) * REFILL_PERCENT // PERCENT


def intraday_refill(
    allocation: Allocation,
    balance: int,
    realized_vpd: float,
    threshold: float,
    headroom: int,
) -> TopUp | None:
    """
    Refill an agent that is running dry *and* has been spending well today:

        if balance < 20 % · a  and  realized value/$ today ≥ threshold  and  headroom > 0:
            refill min(25 % · a, headroom)

    Returns None when any condition fails (or the refill would round to 0).
    Exactly 20 % is not "low"; exactly the threshold is good enough.
    """
    if not is_balance_low(balance, allocation.amount_atomic):
        return None
    if realized_vpd < threshold:
        return None
    amount = min(refill_size(allocation.amount_atomic), max(0, headroom))
    if amount <= 0:
        return None
    reason = (
        f"intraday refill: balance {_format_ausd(balance)} < {LOW_BALANCE_PERCENT}% of allocation "
        f"{_format_ausd(allocation.amount_atomic)}, value/$ today {realized_vpd:.2f} ≥ {threshold:.2f} "
        f"→ +{_format_ausd(amount)}"
    )
    if amount < refill_size(allocation.amount_atomic):
        reason += " (limited by on-chain headroom)"
    return TopUp(agent=allocation.agent, amount=amount, reason=reason)


# ─── session end ─────────────────────────────────────────────────────────────

def sweep_amounts(balances: Mapping[str, int]) -> list[Sweep]:
    """Sweep every positive agent balance back to the owner; zero balances are skipped."""
    sweeps: list[Sweep] = []
    for agent, balance in balances.items():
        if balance <= 0:
            continue
        reason = f"session end: return {_format_ausd(balance)} to the owner"
        sweeps.append(Sweep(agent=agent, amount=balance, reason=reason))
    return sweeps


# ─── runway ──────────────────────────────────────────────────────────────────

def average_daily_spend(daily_spends: Sequence[int]) -> float:
    """Mean of recent daily spends (atomic units); 0.0 when there is no history yet."""
    if not daily_spends:
        return 0.0
    return sum(daily_spends) / len(daily_spends)


def runway_days(allowance_remaining: int, avg_daily_spend: float) -> float:
    """
    Days the remaining allowance lasts at the current pace: allowance_remaining / avg_daily_spend.

    No spending yet → infinite runway; no allowance left → 0 days.
    """
    if allowance_remaining <= 0:
        return 0.0
    if avg_daily_spend <= 0:
        return math.inf
    return allowance_remaining / avg_daily_spend


def is_runway_low(days: float, alert_below_days: float = RUNWAY_ALERT_DAYS) -> bool:
    """True when the runway is strictly below the alert level (default 3 days)."""
    return days < alert_below_days
