"""
Findings: what an agent tells the user after paid research moved one of its beliefs (SPEC §5).

    agent tick ── belief p → p' after a purchase ──▶ should_emit(p, p', τ)?
                                                        │ crossed τ, or |Δp| > δ (0.05)
                                                        ▼
                         FindingDraft (numbers from code) ──▶ build_finding
                                                        │ headline: template (exact numbers)
                                                        │ summary:  llm.phrase_finding (LLM or template,
                                                        │           guarded so it can only repeat numbers)
                                                        ▼
                                              db.Finding (stored + published by the agent)

The direction is decided by code: "risk" when a bearish event flag (hack, halt, …) was seen,
otherwise "bullish" when p' > τ, "bearish" when p' < τ, "neutral" when p' = τ.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agents.beliefs import (
    DECISION_ABOVE_STRIKE,
    DECISION_DIRECTION,
    DECISION_PM_YES,
    SIGNIFICANT_MOVE_DELTA,
    crossed_threshold,
    decision_qualifier,
    decision_type,
    is_significant_move,
)
from app.agents.llm import FindingFacts, PhrasedFinding, phrase_finding
from app.agents.sectors import DIRECTION_BEARISH, DIRECTION_BULLISH, DIRECTION_NEUTRAL, DIRECTION_RISK
from app.config import Settings
from app.db import Finding

PhraseFn = Callable[..., Awaitable[PhrasedFinding]]


@dataclass(frozen=True)
class FindingDraft:
    """Everything a finding says, computed by code before any phrasing."""

    agent: str
    asset: str
    decision: str                 # belief decision string, e.g. "direction" or "above:98000"
    old_p: float
    new_p: float
    tau: float
    evidence: Mapping[str, str] = field(default_factory=dict)
    receipts: Sequence[Mapping[str, Any]] = ()      # [{seller_id, tx_hash, price_atomic}]
    risk: bool = False

    @property
    def delta_p(self) -> float:
        """Signed change of the belief: p' − p."""
        return self.new_p - self.old_p

    @property
    def sellers(self) -> tuple[str, ...]:
        """Seller ids behind the receipts, in order, without repeats."""
        return tuple(dict.fromkeys(str(receipt["seller_id"]) for receipt in self.receipts))


def should_emit(old_p: float, new_p: float, tau: float, delta: float = SIGNIFICANT_MOVE_DELTA) -> bool:
    """Rule: emit a finding when the belief crossed τ or moved by more than δ (0.05)."""
    return crossed_threshold(old_p, new_p, tau) or is_significant_move(new_p - old_p, delta)


def direction_for(new_p: float, tau: float, risk: bool = False) -> str:
    """"risk" if a bearish event flag was seen; else bullish (p' > τ), bearish (p' < τ), neutral."""
    if risk:
        return DIRECTION_RISK
    if new_p > tau:
        return DIRECTION_BULLISH
    if new_p < tau:
        return DIRECTION_BEARISH
    return DIRECTION_NEUTRAL


def format_strike_usd(strike: float) -> str:
    """98000.0 → "$98,000"; 0.63 → "$0.63" (whole strikes without decimals)."""
    if float(strike).is_integer():
        return f"${int(strike):,}"
    return f"${strike:,.2f}"


def describe_decision(asset: str, decision: str) -> str:
    """Human-readable decision: "BTC up this session", "BTC above $98,000 at the next expiry", …"""
    kind = decision_type(decision)
    if kind == DECISION_DIRECTION:
        return f"{asset} up this session"
    if kind == DECISION_ABOVE_STRIKE:
        strike = float(decision_qualifier(decision) or 0)
        return f"{asset} above {format_strike_usd(strike)} at the next expiry"
    if kind == DECISION_PM_YES:
        return f"{asset} prediction-market YES underpriced"
    return f"{asset} {decision}"


def facts_for(draft: FindingDraft) -> FindingFacts:
    """The LLM-safe facts: every number already formatted by code."""
    return FindingFacts(
        agent=draft.agent,
        asset=draft.asset,
        decision=describe_decision(draft.asset, draft.decision),
        direction=direction_for(draft.new_p, draft.tau, draft.risk),
        confidence=draft.new_p,
        delta_p=draft.delta_p,
        evidence=dict(draft.evidence),
        sellers=draft.sellers,
    )


async def build_finding(
    draft: FindingDraft,
    *,
    settings: Settings | None = None,
    phrase: PhraseFn = phrase_finding,
) -> Finding:
    """Phrase the draft (template headline; LLM or template summary) into an unsaved db.Finding."""
    facts = facts_for(draft)
    phrased = await phrase(facts, settings=settings)
    return Finding(
        agent=draft.agent,
        asset=draft.asset,
        decision=draft.decision,
        headline=phrased.headline,
        direction=facts.direction,
        confidence=draft.new_p,
        delta_p=draft.delta_p,
        summary=phrased.summary,
        receipts=[dict(receipt) for receipt in draft.receipts],
    )
