"""
The VOI gate: is one research purchase worth its price? (SPEC §5)

Model. An agent holds a belief p about a binary decision and acts iff p > τ, with a
payoff that is linear in the margin: acting is worth S · (p − τ), not acting is
worth 0. Buying research moves the belief to p' = p + ε with ε ~ N(0, σ²), where σ
is how much this seller typically moves beliefs of this kind. The expected value
of information is how much better the decision gets on average:

    m = p − τ,   z = |m| / σ
    EVOI = S · E[max(m + ε, 0)] − S · max(m, 0)
         = S · σ · [φ(z) − z · (1 − Φ(z))]          ("expected improvement")

EVOI is largest when the agent is undecided (p ≈ τ) and the seller is informative
(σ large); it vanishes when the belief is far from τ relative to σ.

Gate. Buy iff EVOI ≥ price × margin (margin 1.5), the agent wallet can pay, the
monthly cap is not hit, and the same seller+params is not cached. Every decision,
bought or skipped, is returned as a `BuyDecision` holding every number and a
human-readable reason such as "skipped: EVOI $0.004 < price $0.05 × 1.5".

Learning. `SigmaLearner` keeps σ per (seller, decision type) as an EMA of the
realized Δp², starting at σ₀ = 0.08 with a floor of 0.01. `realized_value_per_dollar`
scores an agent after the fact (it feeds the allocator in `app/keeper/allocator.py`).

Money: prices and balances are integer AUSD atomic units (6 decimals); EVOI and
stakes are float USD (estimates, never balances). 1 AUSD is treated as $1.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.agents.beliefs import Belief
from app.constants import ONE_AUSD
from app.sellers.optionmath import norm_cdf, norm_pdf

if TYPE_CHECKING:  # imported for type hints only; planner imports this module at runtime
    from app.agents.planner import Candidate

# ─── constants ────────────────────────────────────────────────────────────────

DEFAULT_MARGIN = 1.5            # buy only if EVOI ≥ 1.5 × price
PRIOR_SIGMA = 0.08              # σ₀ before a seller has moved any belief
SIGMA_FLOOR = 0.01              # σ never learned below this
SIGMA_EMA_ALPHA = 0.2           # weight of the newest Δp² in the EMA

DEFAULT_MONTE_CARLO_SAMPLES = 1_000_000
DEFAULT_MONTE_CARLO_SEED = 0

PRICE_DECIMALS_SHOWN_MIN = 2    # "$0.05", never "$0.050000"

# Machine-readable outcome of a decision (the reason text is for humans).
OUTCOME_BUY = "buy"
OUTCOME_SKIP_CACHED = "skip_cached"
OUTCOME_SKIP_LOW_EVOI = "skip_low_evoi"
OUTCOME_SKIP_BALANCE = "skip_balance"
OUTCOME_SKIP_MONTHLY_CAP = "skip_monthly_cap"
OUTCOME_SKIP_DUPLICATE = "skip_duplicate"   # set by the planner for repeats within one round


# ─── formatting ──────────────────────────────────────────────────────────────

def format_ausd(amount_atomic: int) -> str:
    """Atomic AUSD → "$0.05": at least 2 decimals, up to 6, trailing zeros dropped beyond 2."""
    sign = "-" if amount_atomic < 0 else ""
    whole, fraction = divmod(abs(amount_atomic), ONE_AUSD)
    fraction_text = f"{fraction:06d}".rstrip("0").ljust(PRICE_DECIMALS_SHOWN_MIN, "0")
    return f"{sign}${whole:,}.{fraction_text}"


def format_usd(amount_usd: float) -> str:
    """A float USD estimate (EVOI) → "$0.004" (3 decimals)."""
    return f"${amount_usd:,.3f}"


def atomic_to_usd(amount_atomic: int) -> float:
    """Atomic AUSD → USD float (1 AUSD = $1), for comparing against EVOI estimates only."""
    return amount_atomic / ONE_AUSD


# ─── EVOI ────────────────────────────────────────────────────────────────────

def expected_improvement(z: float) -> float:
    """
    Unit expected improvement g(z) = φ(z) − z · (1 − Φ(z)) = E[(X − z)⁺] for X ~ N(0, 1).

    g(0) = 0.3989 (undecided), g(1) = 0.0833, g(3) ≈ 0.0004; always ≥ 0.
    """
    return norm_pdf(z) - z * (1.0 - norm_cdf(z))


def decision_z(p: float, tau: float, sigma: float) -> float:
    """Distance from the threshold in units of σ: z = |p − τ| / σ (infinite if σ ≤ 0)."""
    if sigma <= 0:
        return math.inf
    return abs(p - tau) / sigma


def evoi(stake_usd: float, p: float, tau: float, sigma: float) -> float:
    """
    Closed-form expected value of information in USD:

        EVOI = S · σ · [φ(z) − z · (1 − Φ(z))],   z = |p − τ| / σ

    A seller that cannot move the belief (σ ≤ 0) or a zero stake is worth 0.
    """
    if sigma <= 0 or stake_usd <= 0:
        return 0.0
    z = decision_z(p, tau, sigma)
    return stake_usd * sigma * expected_improvement(z)


def monte_carlo_evoi(
    stake_usd: float,
    p: float,
    tau: float,
    sigma: float,
    n_samples: int = DEFAULT_MONTE_CARLO_SAMPLES,
    seed: int = DEFAULT_MONTE_CARLO_SEED,
) -> float:
    """
    The same EVOI by simulation, used to check the closed form:

        value now         = S · max(m, 0)                     m = p − τ
        value after info  = S · mean(max(m + ε, 0)),          ε ~ N(0, σ²)
        EVOI              = value after info − value now

    Antithetic sampling: every draw ε is paired with −ε, so the sample mean of ε is
    exactly 0 and the noisy linear part S · mean(ε) cancels. Like the closed form,
    p' is not clipped to [0, 1].
    """
    import numpy as np  # dev dependency: only tests and diagnostics simulate

    if sigma <= 0 or stake_usd <= 0:
        return 0.0
    rng = np.random.default_rng(seed)
    half = rng.standard_normal(n_samples // 2) * sigma
    shocks = np.concatenate([half, -half])
    margin = p - tau
    value_after_info = stake_usd * float(np.maximum(margin + shocks, 0.0).mean())
    value_now = stake_usd * max(margin, 0.0)
    return value_after_info - value_now


# ─── the buy / skip decision ─────────────────────────────────────────────────

@dataclass(frozen=True)
class BuyDecision:
    """Every number behind one buy-or-skip decision, plus a sentence explaining it."""

    agent: str
    seller_id: str
    decision_key: str
    params: dict[str, str]
    price_atomic: int
    stake_usd: float
    p: float
    tau: float
    sigma: float
    z: float
    evoi_usd: float
    margin: float
    required_usd: float               # price × margin: the EVOI needed to buy
    evoi_per_price: float             # EVOI / price, the ranking key
    balance_atomic: int
    monthly_remaining_atomic: int | None
    outcome: str                      # one of the OUTCOME_* constants
    reason: str

    @property
    def bought(self) -> bool:
        """True when the gate said "buy"."""
        return self.outcome == OUTCOME_BUY

    def as_log_dict(self) -> dict[str, Any]:
        """Plain dict for the purchases table / SSE stream."""
        return {
            "agent": self.agent,
            "seller_id": self.seller_id,
            "decision_key": self.decision_key,
            "params": dict(self.params),
            "price_atomic": self.price_atomic,
            "stake_usd": self.stake_usd,
            "p": self.p,
            "tau": self.tau,
            "sigma": self.sigma,
            "z": self.z,
            "evoi_usd": self.evoi_usd,
            "margin": self.margin,
            "required_usd": self.required_usd,
            "evoi_per_price": self.evoi_per_price,
            "balance_atomic": self.balance_atomic,
            "monthly_remaining_atomic": self.monthly_remaining_atomic,
            "outcome": self.outcome,
            "bought": self.bought,
            "reason": self.reason,
        }


def _gate_outcome(
    *,
    price_atomic: int,
    evoi_usd: float,
    margin: float,
    balance_atomic: int,
    monthly_remaining_atomic: int | None,
    cached: bool,
    seller_id: str,
) -> tuple[str, str]:
    """
    Apply the four checks in order and return (outcome, reason):
        1. cached (same seller+params within TTL) → skip, it costs nothing to reuse
        2. EVOI < price × margin                    → skip, not worth it
        3. agent balance < price                    → skip, cannot pay
        4. monthly cap remaining < price            → skip, user's limit
    Otherwise buy.
    """
    price_text = format_ausd(price_atomic)
    required_usd = atomic_to_usd(price_atomic) * margin
    if cached:
        return OUTCOME_SKIP_CACHED, f"skipped: cached — {seller_id} with the same params was bought within its TTL"
    if evoi_usd < required_usd:
        return OUTCOME_SKIP_LOW_EVOI, f"skipped: EVOI {format_usd(evoi_usd)} < price {price_text} × {margin:g}"
    if balance_atomic < price_atomic:
        return OUTCOME_SKIP_BALANCE, f"skipped: balance {format_ausd(balance_atomic)} < price {price_text}"
    if monthly_remaining_atomic is not None and monthly_remaining_atomic < price_atomic:
        return (
            OUTCOME_SKIP_MONTHLY_CAP,
            f"skipped: monthly cap — {format_ausd(monthly_remaining_atomic)} left < price {price_text}",
        )
    ratio = evoi_usd / atomic_to_usd(price_atomic) if price_atomic > 0 else math.inf
    return OUTCOME_BUY, f"buy: EVOI {format_usd(evoi_usd)} ≥ price {price_text} × {margin:g} (EVOI/price {ratio:.1f}×)"


def decide(
    candidate: Candidate,
    belief: Belief,
    *,
    balance_atomic: int,
    monthly_remaining_atomic: int | None = None,
    cached: bool = False,
    margin: float = DEFAULT_MARGIN,
) -> BuyDecision:
    """
    Run the VOI gate for one candidate purchase against the belief it would inform.

    `balance_atomic` is the agent wallet's spendable AUSD; `monthly_remaining_atomic`
    is what is left of the user's monthly cap (None = no monthly cap).
    """
    sigma = candidate.sigma
    evoi_usd = evoi(belief.stake_usd, belief.p, belief.tau, sigma)
    price_usd = atomic_to_usd(candidate.price_atomic)
    outcome, reason = _gate_outcome(
        price_atomic=candidate.price_atomic,
        evoi_usd=evoi_usd,
        margin=margin,
        balance_atomic=balance_atomic,
        monthly_remaining_atomic=monthly_remaining_atomic,
        cached=cached,
        seller_id=candidate.seller_id,
    )
    return BuyDecision(
        agent=candidate.agent,
        seller_id=candidate.seller_id,
        decision_key=candidate.decision_key,
        params=dict(candidate.params),
        price_atomic=candidate.price_atomic,
        stake_usd=belief.stake_usd,
        p=belief.p,
        tau=belief.tau,
        sigma=sigma,
        z=decision_z(belief.p, belief.tau, sigma),
        evoi_usd=evoi_usd,
        margin=margin,
        required_usd=price_usd * margin,
        evoi_per_price=evoi_usd / price_usd if price_usd > 0 else math.inf,
        balance_atomic=balance_atomic,
        monthly_remaining_atomic=monthly_remaining_atomic,
        outcome=outcome,
        reason=reason,
    )


# ─── learning σ per (seller, decision type) ──────────────────────────────────

@dataclass
class SigmaLearner:
    """
    σ_i for EVOI, learned per (seller_id, decision_type) from realized belief moves:

        variance ← (1 − α) · variance + α · Δp²      (α = 0.2, start at σ₀² = 0.08²)
        σ        = max(floor, √variance)             (floor 0.01)
    """

    prior_sigma: float = PRIOR_SIGMA
    floor: float = SIGMA_FLOOR
    alpha: float = SIGMA_EMA_ALPHA
    _variances: dict[tuple[str, str], float] = field(default_factory=dict, init=False, repr=False)

    def sigma(self, seller_id: str, decision_type: str) -> float:
        """Current σ for this seller and decision type (the prior if never observed)."""
        variance = self._variances.get((seller_id, decision_type), self.prior_sigma**2)
        return max(self.floor, math.sqrt(variance))

    def observe(self, seller_id: str, decision_type: str, delta_p: float) -> float:
        """Fold one realized belief move Δp into the EMA and return the new σ."""
        key = (seller_id, decision_type)
        previous = self._variances.get(key, self.prior_sigma**2)
        self._variances[key] = (1.0 - self.alpha) * previous + self.alpha * delta_p * delta_p
        return self.sigma(seller_id, decision_type)

    def snapshot(self) -> dict[str, float]:
        """{"seller:decision_type": σ} for the dashboard and for persisting across restarts."""
        return {f"{seller}:{dtype}": self.sigma(seller, dtype) for seller, dtype in sorted(self._variances)}

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, float], **settings: float) -> SigmaLearner:
        """Rebuild a learner from `snapshot()` output (σ values are squared back to variances)."""
        learner = cls(**settings)
        for key, sigma in snapshot.items():
            seller_id, decision_type = key.split(":", 1)
            learner._variances[(seller_id, decision_type)] = sigma * sigma
        return learner


# ─── realized value of an agent's purchases ──────────────────────────────────

@dataclass(frozen=True)
class PurchaseOutcome:
    """What one completed purchase cost and how far it actually moved the belief."""

    stake_usd: float
    p_before: float
    tau: float
    realized_abs_dp: float
    price_atomic: int


def ex_post_value_usd(outcome: PurchaseOutcome) -> float:
    """EVOI evaluated after the fact with σ = realized |Δp|: S · |Δp| · g(|p − τ| / |Δp|)."""
    return evoi(outcome.stake_usd, outcome.p_before, outcome.tau, outcome.realized_abs_dp)


def realized_value_per_dollar(outcomes: Iterable[PurchaseOutcome]) -> float:
    """
    Realized value per $ of an agent = Σ ex-post EVOI / Σ spend (USD).

    Returns 0.0 when nothing was spent (no evidence yet).
    """
    total_value_usd = 0.0
    total_spend_atomic = 0
    for outcome in outcomes:
        total_value_usd += ex_post_value_usd(outcome)
        total_spend_atomic += outcome.price_atomic
    if total_spend_atomic <= 0:
        return 0.0
    return total_value_usd / atomic_to_usd(total_spend_atomic)
