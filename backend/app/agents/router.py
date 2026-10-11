"""
Ask box: a trader's question → which agents and assets → one focused tick → an answer with receipts.

    question ──▶ plan_question (keyword rules, deterministic)
                   tickers / asset names ("bitcoin" → BTC)   → assets
                   earnings → OptionsEvent    CPI / Fed / FOMC / rates / insider → MacroFilings
                   funding / perps / OI     → OnchainPerps     news / headlines   → NewsSentiment
             ──▶ optional llm.classify_question (validated; never adds an unknown agent or asset)
             ──▶ keep only agents that cover the assets (perps are crypto-only, ...)
             ──▶ run ONE focused tick per chosen agent on those assets (real x402 purchases)
             ──▶ compose_answer: findings first, otherwise each agent's current belief; every
                 number comes from code; receipts = the tx hashes of what was bought for it

The answer never invents data: if nothing was bought and nothing changed it says so.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.agents.base import SectorAgent, TickReport
from app.agents.findings import describe_decision
from app.agents.llm import (
    SOURCE_LLM,
    SOURCE_TEMPLATE,
    classify_question,
    mentions_keyword,
    route_question_by_keywords,
)
from app.agents.sectors import SECTORS
from app.config import Settings
from app.constants import AGENT_NAMES

# ─── topic rules (word stems, matched at the start of a word) ────────────────

TOPIC_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "earnings": (("earning", "guidance"), "OptionsEvent"),
    "cpi": (("cpi", "inflation"), "MacroFilings"),
    "fed": (("fed", "fomc", "rate cut", "rate hike", "powell"), "MacroFilings"),
    "funding": (("funding", "perp", "open interest", "liquidat"), "OnchainPerps"),
    "insider": (("insider", "form 4"), "MacroFilings"),
    "news": (("news", "headline", "sentiment"), "NewsSentiment"),
}

MAX_BELIEF_LINES = 6


@dataclass(frozen=True)
class AskPlan:
    """Which agents run a focused tick for which assets, and why."""

    agents: tuple[str, ...]
    assets: tuple[str, ...]
    topics: tuple[str, ...]
    source: str                    # "template" (keyword rules) | "llm"


TickFn = Callable[[SectorAgent, Sequence[str]], Awaitable[TickReport]]


# ─── planning ─────────────────────────────────────────────────────────────────

def detect_topics(question: str) -> tuple[str, ...]:
    """Topic names whose stems start a word of the question, in TOPIC_RULES order."""
    return tuple(topic for topic, (stems, _) in TOPIC_RULES.items() if mentions_keyword(question, stems))


def restrict_to_coverage(agents: Sequence[str], assets: Sequence[str]) -> tuple[str, ...]:
    """
    Keep agents that cover at least one of the assets. If none does (e.g. "funding" for NVDA),
    use every agent that covers the assets instead, so the question still gets answered.
    """
    covering = tuple(name for name in agents if any(SECTORS[name].covers(asset) for asset in assets))
    if covering:
        return covering
    return tuple(name for name in AGENT_NAMES if any(SECTORS[name].covers(asset) for asset in assets))


def plan_question(question: str, watchlist: Sequence[str]) -> AskPlan:
    """
    Deterministic plan: agents = topic agents ∪ llm keyword agents (all agents when nothing
    matches); assets = named tickers/names or the whole watchlist; then restricted to coverage.
    """
    keyword_route = route_question_by_keywords(question, watchlist)
    topics = detect_topics(question)
    topic_agents = [TOPIC_RULES[topic][1] for topic in topics]
    named = list(dict.fromkeys(topic_agents)) if topic_agents else list(keyword_route.agents)
    agents = restrict_to_coverage(named, keyword_route.assets)
    return AskPlan(agents=agents, assets=keyword_route.assets, topics=topics, source=SOURCE_TEMPLATE)


async def plan_with_llm(question: str, watchlist: Sequence[str], settings: Settings) -> AskPlan:
    """The keyword plan, replaced by Claude's validated routing when an API key is configured."""
    plan = plan_question(question, watchlist)
    if not settings.anthropic_api_key:
        return plan
    route = await classify_question(question, watchlist, settings=settings)
    if route.source != SOURCE_LLM:
        return plan
    agents = restrict_to_coverage(route.agents, route.assets) or plan.agents
    return AskPlan(agents=agents, assets=route.assets or plan.assets, topics=plan.topics, source=SOURCE_LLM)


# ─── answering ───────────────────────────────────────────────────────────────

def _belief_lines(agents: Mapping[str, SectorAgent], plan: AskPlan) -> list[str]:
    """"NewsSentiment: BTC up this session at 59%" for each belief of the chosen agents on the assets."""
    lines: list[str] = []
    for name in plan.agents:
        agent = agents.get(name)
        if agent is None:
            continue
        for belief in sorted(agent.beliefs.values(), key=lambda b: b.key):
            if belief.asset in plan.assets:
                lines.append(f"{name}: {describe_decision(belief.asset, belief.decision)} at {belief.p:.0%}")
    return lines[:MAX_BELIEF_LINES]


def compose_answer(
    question: str,
    plan: AskPlan,
    reports: Sequence[TickReport],
    agents: Mapping[str, SectorAgent],
) -> str:
    """Plain-text answer from code numbers: findings (with receipt count) or current beliefs."""
    findings = [finding for report in reports for finding in report.findings]
    purchases = [purchase for report in reports for purchase in report.purchases]
    bought = sum(1 for p in purchases if p["status"] == "bought")
    skipped = sum(1 for p in purchases if p["status"] == "skipped")
    failed = sum(1 for p in purchases if p["status"] == "failed")
    lines = [f"Asked {', '.join(plan.agents)} about {', '.join(plan.assets)}: "
             f"{bought} bought, {skipped} skipped by the VOI gate or limits, {failed} failed."]
    if findings:
        lines.extend(f"• {finding['headline']}" for finding in findings)
    else:
        lines.append("No belief moved enough for a new finding. Current view:")
        lines.extend(f"• {line}" for line in _belief_lines(agents, plan) or ["no beliefs held yet"])
    return "\n".join(lines)


async def answer_question(
    question: str,
    *,
    agents: Mapping[str, SectorAgent],
    watchlist: Sequence[str],
    tick: TickFn,
    settings: Settings,
) -> dict[str, Any]:
    """
    Plan, run one focused tick per chosen agent (via `tick(agent, assets)`), and compose the
    docs/API.md answer `{answer, agents_used, findings, receipts}` (+ assets, route source).
    """
    plan = await plan_with_llm(question, watchlist, settings)
    reports: list[TickReport] = []
    for name in plan.agents:
        agent = agents.get(name)
        if agent is not None:
            reports.append(await tick(agent, plan.assets))
    return {
        "answer": compose_answer(question, plan, reports, agents),
        "agents_used": [report.agent for report in reports],
        "assets": list(plan.assets),
        "findings": [finding for report in reports for finding in report.findings],
        "receipts": [receipt for report in reports for receipt in report.receipts],
        "route_source": plan.source,
    }
