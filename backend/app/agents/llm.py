"""
Optional LLM phrasing for findings and the Ask box (Claude via the official Anthropic SDK).

The LLM only PHRASES. Every number comes from code:

    phrase_finding(facts)        FindingFacts (numbers already formatted by code)
        ├─ no API key / any error / refusal / timeout ──▶ template summary (deterministic)
        └─ Claude writes 1–2 sentences ──▶ number guard ──▶ LLM summary
                                              │ a number not present in the facts?
                                              └──────────▶ template summary
    classify_question(question)  Ask box routing: which agents, which assets
        ├─ keyword router (always computed, deterministic)
        └─ Claude picks agents (JSON schema output) ──▶ validated against AGENT_NAMES and
           against the assets the keyword router / watchlist know; anything else is ignored

Short timeouts and no SDK retries: when the API is slow the dashboard shows the template
immediately instead of waiting. The headline of a finding is always the template (it carries
the exact numbers); only the summary sentence may come from the LLM.

Settings used: `anthropic_api_key` and `llm_model` (default "claude-sonnet-5-5") from
`app/config.py`. The API key is never logged.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import anthropic

from app.agents.planner import AGENT_SELLERS, CRYPTO_ASSETS
from app.config import Settings, get_settings
from app.constants import AGENT_NAMES

logger = logging.getLogger(__name__)

# ─── request tuning ──────────────────────────────────────────────────────────

LLM_TIMEOUT_SECONDS = 10.0         # per request; on timeout the template is used
LLM_MAX_RETRIES = 0                # never retry: a late phrasing is worth less than a template now
PHRASE_MAX_TOKENS = 1_024          # room for (low-effort) thinking plus two sentences
CLASSIFY_MAX_TOKENS = 1_024
LLM_EFFORT = "low"                 # phrasing and routing are simple tasks
MAX_SUMMARY_CHARS = 600            # longer LLM output is rejected (template used)

# Server-side refusal fallback (beta): on a safety decline the API re-runs the request on
# Anthropic's recommended model for that category. Only these models accept the "default" form.
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"
SERVER_FALLBACK_MODE = "default"
SERVER_FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})

STOP_REASON_REFUSAL = "refusal"
SOURCE_LLM = "llm"
SOURCE_TEMPLATE = "template"

# A "number" in text: digits with optional thousands separators and decimals ("4,200.5").
NUMBER_PATTERN = re.compile(r"\d+(?:[.,]\d+)*")

# ─── Ask-box keyword router ──────────────────────────────────────────────────

# Keywords are word STEMS matched at the start of a word ("liquidat" matches "liquidation").
AGENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "OnchainPerps": ("perp", "funding", "open interest", "leverag", "liquidat", "crowd", "hyperliquid",
                     "squeeze"),
    "OptionsEvent": ("option", "implied", "volatil", "expected move", "earning", "strike", "expir",
                     "prediction market", "odds", "gemini", "straddle", "above", "below"),
    "NewsSentiment": ("news", "headline", "sentiment", "tone", "hack", "exploit", "lawsuit", "listing",
                      "delist", "rumo", "etf", "downgrade", "upgrade"),
    "MacroFilings": ("fed", "fomc", "cpi", "inflation", "rate", "yield", "treasur", "macro", "jobs", "payroll",
                     "unemployment", "insider", "form 4", "filing", "edgar"),
}

# Common names → watchlist symbols (only symbols, never prices or numbers).
ASSET_ALIASES: dict[str, str] = {
    "bitcoin": "BTC",
    "ether": "ETH",
    "ethereum": "ETH",
    "solana": "SOL",
    "nvidia": "NVDA",
    "apple": "AAPL",
    "tesla": "TSLA",
}
TICKER_PATTERN = re.compile(r"\b[A-Za-z]{2,5}\b")   # symbol-shaped words

AGENT_DESCRIPTIONS: dict[str, str] = {
    "OnchainPerps": "crypto perpetual futures: funding rates, open interest, crowding, plus crypto news",
    "OptionsEvent": "options-implied moves around events (earnings, expiries) and prediction-market odds",
    "NewsSentiment": "news headlines, tone and event flags (hacks, lawsuits, listings, ETF news)",
    "MacroFilings": "US macro data (CPI, Fed funds, yields, unemployment, FOMC) and SEC insider filings",
}


# ─── data types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FindingFacts:
    """
    Everything a finding may say. Numbers are passed as code-formatted strings in `evidence`
    (e.g. {"expected move": "±4.2%"}), so the LLM can only repeat them, never compute them.
    """

    agent: str
    asset: str
    decision: str                 # human-readable decision, e.g. "BTC above $100,000 on 2026-12-26"
    direction: str                # bullish | bearish | neutral | risk
    confidence: float             # belief p after the update, 0–1
    delta_p: float                # change in p from this research
    evidence: Mapping[str, str] = field(default_factory=dict)
    sellers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhrasedFinding:
    """Headline (always template) and summary (LLM or template), plus which one was used."""

    headline: str
    summary: str
    source: str                   # "llm" | "template"


@dataclass(frozen=True)
class QuestionRoute:
    """Which agents should run a focused tick for an Ask-box question, and for which assets."""

    agents: tuple[str, ...]
    assets: tuple[str, ...]
    source: str                   # "llm" | "template" (keyword router)


# ─── number formatting (code, not the LLM, renders every number) ─────────────

def format_probability(p: float) -> str:
    """0.623 → "62%"."""
    return f"{p:.0%}"


def format_delta_points(delta_p: float) -> str:
    """0.071 → "+7.1 pts" (percentage points of probability)."""
    return f"{delta_p * 100:+.1f} pts"


# ─── templates ───────────────────────────────────────────────────────────────

def template_headline(facts: FindingFacts) -> str:
    """"BTC: bullish — BTC above $100,000 on 2026-12-26 now 62% (+7.1 pts)"."""
    return (
        f"{facts.asset}: {facts.direction} — {facts.decision} now "
        f"{format_probability(facts.confidence)} ({format_delta_points(facts.delta_p)})"
    )


def template_summary(facts: FindingFacts) -> str:
    """Deterministic one-paragraph summary built only from the facts."""
    sentence = (
        f"{facts.agent} now puts \"{facts.decision}\" at {format_probability(facts.confidence)} "
        f"({format_delta_points(facts.delta_p)}), a {facts.direction} read on {facts.asset}."
    )
    if facts.evidence:
        evidence_text = "; ".join(f"{label}: {value}" for label, value in facts.evidence.items())
        sentence += f" Evidence: {evidence_text}."
    if facts.sellers:
        sentence += f" Sources: {', '.join(facts.sellers)}."
    return sentence


def facts_block(facts: FindingFacts) -> str:
    """The facts as the plain text the LLM sees (and the number guard checks against)."""
    lines = [
        f"agent: {facts.agent}",
        f"asset: {facts.asset}",
        f"decision: {facts.decision}",
        f"direction: {facts.direction}",
        f"probability now: {format_probability(facts.confidence)}",
        f"change from this research: {format_delta_points(facts.delta_p)}",
    ]
    lines.extend(f"{label}: {value}" for label, value in facts.evidence.items())
    if facts.sellers:
        lines.append(f"sources: {', '.join(facts.sellers)}")
    return "\n".join(lines)


# ─── number guard ────────────────────────────────────────────────────────────

def numbers_in(text: str) -> set[str]:
    """Every number token in `text`, normalised ("4,200" → "4200")."""
    return {token.replace(",", "") for token in NUMBER_PATTERN.findall(text)}


def uses_only_known_numbers(llm_text: str, facts_text: str) -> bool:
    """Rule: the LLM may only repeat numbers that appear in the facts; any other number → reject."""
    return numbers_in(llm_text) <= numbers_in(facts_text)


# ─── Claude client ───────────────────────────────────────────────────────────

def make_client(settings: Settings | None = None) -> anthropic.AsyncAnthropic | None:
    """An async Claude client with short timeouts, or None when no API key is configured."""
    settings = settings or get_settings()
    if not settings.anthropic_api_key:
        return None
    return anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=LLM_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )


def _fallback_options(model: str) -> dict[str, Any]:
    """Server-side refusal fallback parameters for models that support the "default" form."""
    if model not in SERVER_FALLBACK_MODELS:
        return {}
    return {"betas": [SERVER_FALLBACK_BETA], "fallbacks": SERVER_FALLBACK_MODE}


def _response_text(response: Any) -> str | None:
    """The joined text blocks of a response, or None for a refusal or an empty answer."""
    if response.stop_reason == STOP_REASON_REFUSAL:
        return None
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text or None


async def ask_claude(
    client: anthropic.AsyncAnthropic,
    model: str,
    system: str,
    user_text: str,
    max_tokens: int,
    output_format: dict[str, Any] | None = None,
) -> str | None:
    """
    One short Claude request. Returns the answer text, or None on ANY failure (connection,
    timeout, HTTP error, refusal, empty answer) — callers then use their template.
    """
    output_config: dict[str, Any] = {"effort": LLM_EFFORT}
    if output_format is not None:
        output_config["format"] = output_format
    try:
        response = await client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_text}],
            output_config=output_config,
            **_fallback_options(model),
        )
    except anthropic.APITimeoutError:
        logger.warning("LLM timed out after %.0fs; using template", LLM_TIMEOUT_SECONDS)
        return None
    except anthropic.APIConnectionError:
        logger.warning("LLM unreachable; using template")
        return None
    except anthropic.APIStatusError as error:
        logger.warning("LLM HTTP %s; using template", error.status_code)
        return None
    except Exception as error:  # noqa: BLE001 — the LLM is optional: never let it break a finding
        logger.warning("LLM call failed (%s); using template", type(error).__name__)
        return None
    return _response_text(response)


# ─── phrase_finding ──────────────────────────────────────────────────────────

PHRASE_SYSTEM_PROMPT = (
    "You write one or two plain-English sentences summarising a research finding for a retail "
    "trader's dashboard. Use ONLY the facts given. Copy numbers exactly as written in the facts; "
    "never compute, round, convert or add any other number, date or price. No advice, no hype, "
    "no markdown. Answer with the sentences only."
)


async def phrase_finding(
    facts: FindingFacts,
    *,
    settings: Settings | None = None,
    client: anthropic.AsyncAnthropic | None = None,
) -> PhrasedFinding:
    """
    Headline from the template; summary from Claude when available and safe, else template.
    "Safe" = not a refusal, at most 600 characters, and every number appears in the facts.
    """
    settings = settings or get_settings()
    headline = template_headline(facts)
    fallback = PhrasedFinding(headline=headline, summary=template_summary(facts), source=SOURCE_TEMPLATE)
    client = client or make_client(settings)
    if client is None:
        return fallback
    facts_text = facts_block(facts)
    text = await ask_claude(client, settings.llm_model, PHRASE_SYSTEM_PROMPT, facts_text, PHRASE_MAX_TOKENS)
    if text is None or len(text) > MAX_SUMMARY_CHARS:
        return fallback
    if not uses_only_known_numbers(text, facts_text):
        logger.warning("LLM summary contained a number not in the facts; using template")
        return fallback
    return PhrasedFinding(headline=headline, summary=text, source=SOURCE_LLM)


# ─── classify_question ───────────────────────────────────────────────────────

def detect_assets(question: str, watchlist: Iterable[str]) -> tuple[str, ...]:
    """
    Assets named in the question: symbol-looking words first (in order), then common names
    ("bitcoin" → BTC). Rule: a watchlist symbol matches in any case ("btc"); other known crypto
    symbols only when written in capitals ("LINK", not the word "link"). No other word is an asset.
    """
    watched = {symbol.upper() for symbol in watchlist}
    found: list[str] = []
    for token in TICKER_PATTERN.findall(question):
        symbol = token.upper()
        is_known = symbol in watched or (token.isupper() and symbol in CRYPTO_ASSETS)
        if is_known and symbol not in found:
            found.append(symbol)
    lowered = question.lower()
    for name, symbol in ASSET_ALIASES.items():
        if re.search(rf"\b{name}\b", lowered) and symbol not in found:
            found.append(symbol)
    return tuple(found)


def mentions_keyword(text: str, stems: Iterable[str]) -> bool:
    """True when any stem starts a word of `text` (case-insensitive)."""
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(stem)}", lowered) for stem in stems)


def route_question_by_keywords(question: str, watchlist: Sequence[str]) -> QuestionRoute:
    """
    Deterministic Ask-box router. Rule: an agent is picked when any of its keyword stems starts
    a word of the question; when none match, every agent is asked. Assets come from
    `detect_assets`; when none are named, the whole watchlist is used.
    """
    agents = tuple(name for name in AGENT_NAMES if mentions_keyword(question, AGENT_KEYWORDS[name]))
    assets = detect_assets(question, watchlist) or tuple(symbol.upper() for symbol in watchlist)
    return QuestionRoute(agents=agents or tuple(AGENT_NAMES), assets=assets, source=SOURCE_TEMPLATE)


def _classify_system_prompt() -> str:
    """System prompt listing the agents the LLM may choose from."""
    agent_lines = "\n".join(
        f"- {name}: {AGENT_DESCRIPTIONS[name]} (sources: {', '.join(AGENT_SELLERS[name])})" for name in AGENT_NAMES
    )
    return (
        "Route a trader's question to the research agents that can answer it. Agents:\n"
        f"{agent_lines}\n"
        "Return the agents that are relevant (at least one) and the asset symbols the question is about. "
        "Only use asset symbols from the provided list."
    )


def _classify_output_format(allowed_assets: Sequence[str]) -> dict[str, Any]:
    """JSON schema for the routing answer (structured output guarantees valid JSON)."""
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "agents": {"type": "array", "items": {"type": "string", "enum": list(AGENT_NAMES)}},
                "assets": {"type": "array", "items": {"type": "string", "enum": list(allowed_assets)}},
            },
            "required": ["agents", "assets"],
            "additionalProperties": False,
        },
    }


def parse_route(text: str, allowed_assets: Sequence[str]) -> QuestionRoute | None:
    """
    Validate the LLM's JSON answer: agents must be real agent names, assets must be in
    `allowed_assets`. Returns None when nothing valid remains (the keyword route is used).
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    agents = tuple(name for name in AGENT_NAMES if name in (payload.get("agents") or []))
    allowed = {symbol.upper() for symbol in allowed_assets}
    assets = tuple(dict.fromkeys(str(a).upper() for a in payload.get("assets") or [] if str(a).upper() in allowed))
    if not agents:
        return None
    return QuestionRoute(agents=agents, assets=assets, source=SOURCE_LLM)


async def classify_question(
    question: str,
    watchlist: Sequence[str],
    *,
    settings: Settings | None = None,
    client: anthropic.AsyncAnthropic | None = None,
) -> QuestionRoute:
    """
    Pick agents and assets for an Ask-box question. Uses Claude when available; falls back to
    the keyword router on a missing key or any error. The LLM can never add an asset that is
    not on the watchlist or named in the question.
    """
    settings = settings or get_settings()
    keyword_route = route_question_by_keywords(question, watchlist)
    client = client or make_client(settings)
    if client is None:
        return keyword_route
    allowed_assets = tuple(dict.fromkeys([*keyword_route.assets, *(s.upper() for s in watchlist)]))
    text = await ask_claude(
        client,
        settings.llm_model,
        _classify_system_prompt(),
        f"Asset symbols: {', '.join(allowed_assets)}\nQuestion: {question}",
        CLASSIFY_MAX_TOKENS,
        output_format=_classify_output_format(allowed_assets),
    )
    route = parse_route(text, allowed_assets) if text else None
    if route is None:
        return keyword_route
    return QuestionRoute(agents=route.agents, assets=route.assets or keyword_route.assets, source=SOURCE_LLM)
