"""
Tests for app/agents/llm.py — no network.

The fallback paths are tested for real: a missing API key, and an SDK client pointed at a
closed local port (a genuine connection error). The Claude API's HTTP responses are served
by an in-process httpx2 MockTransport with realistic Messages API payload shapes, so the
real SDK parses them; nothing leaves the machine. Live LLM phrasing is verified on the
user's machine with ANTHROPIC_API_KEY set.
"""

from __future__ import annotations

import json
import socket

import anthropic
import httpx2

from app.agents.llm import (
    SERVER_FALLBACK_BETA,
    SOURCE_LLM,
    SOURCE_TEMPLATE,
    FindingFacts,
    classify_question,
    numbers_in,
    phrase_finding,
    route_question_by_keywords,
    template_headline,
    template_summary,
    uses_only_known_numbers,
)
from app.config import Settings

WATCHLIST = ["BTC", "ETH", "SOL", "NVDA"]
FACTS = FindingFacts(
    agent="OptionsEvent",
    asset="BTC",
    decision="BTC above $100,000 on 2026-12-26",
    direction="bullish",
    confidence=0.623,
    delta_p=0.071,
    evidence={"expected move to expiry": "±8.4%", "implied vol": "52%"},
    sellers=("expected_move",),
)


def _settings(api_key: str = "test-key", model: str = "claude-sonnet-5-5") -> Settings:
    return Settings(_env_file=None, anthropic_api_key=api_key, llm_model=model)


def _message_payload(text: str, stop_reason: str = "end_turn") -> dict:
    """A Messages API response body in the documented shape."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 40},
    }


def _client_answering(status: int, body: dict, seen_requests: list | None = None) -> anthropic.AsyncAnthropic:
    """A real SDK client whose HTTP layer is an in-process transport returning `body`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen_requests is not None:
            seen_requests.append(request)
        return httpx2.Response(status, json=body)

    return anthropic.AsyncAnthropic(
        api_key="test-key",
        base_url="http://anthropic.test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ─── templates and the number guard ──────────────────────────────────────────

def test_template_headline_carries_the_exact_numbers():
    # Act
    headline = template_headline(FACTS)

    # Assert
    assert headline == "BTC: bullish — BTC above $100,000 on 2026-12-26 now 62% (+7.1 pts)"


def test_template_summary_lists_evidence_and_sources():
    # Act
    summary = template_summary(FACTS)

    # Assert
    assert "expected move to expiry: ±8.4%" in summary
    assert "Sources: expected_move." in summary


def test_numbers_in_normalises_thousands_separators():
    # Act / Assert
    assert numbers_in("BTC above $100,000 at 62% (+7.1 pts)") == {"100000", "62", "7.1"}


def test_number_guard_rejects_a_number_not_in_the_facts():
    # Arrange
    facts_text = "probability now: 62%\nchange: +7.1 pts"

    # Act / Assert
    assert uses_only_known_numbers("Odds rose 7.1 pts to 62%.", facts_text)
    assert not uses_only_known_numbers("Odds rose to 65%.", facts_text)


# ─── phrase_finding fallbacks ────────────────────────────────────────────────

async def test_phrase_finding_uses_template_when_no_api_key_is_configured():
    # Act
    phrased = await phrase_finding(FACTS, settings=_settings(api_key=""))

    # Assert
    assert phrased.source == SOURCE_TEMPLATE
    assert phrased.summary == template_summary(FACTS)


async def test_phrase_finding_uses_template_when_the_api_is_unreachable():
    # Arrange: a real client pointed at a closed local port → genuine connection error
    client = anthropic.AsyncAnthropic(api_key="test-key", base_url=f"http://127.0.0.1:{_closed_port()}",
                                      max_retries=0, timeout=2.0)

    # Act
    phrased = await phrase_finding(FACTS, settings=_settings(), client=client)

    # Assert
    assert phrased.source == SOURCE_TEMPLATE


async def test_phrase_finding_uses_template_on_http_500():
    # Arrange
    client = _client_answering(500, {"type": "error", "error": {"type": "api_error", "message": "boom"}})

    # Act
    phrased = await phrase_finding(FACTS, settings=_settings(), client=client)

    # Assert
    assert phrased.source == SOURCE_TEMPLATE


async def test_phrase_finding_uses_template_on_refusal():
    # Arrange
    client = _client_answering(200, _message_payload("", stop_reason="refusal"))

    # Act
    phrased = await phrase_finding(FACTS, settings=_settings(), client=client)

    # Assert
    assert phrased.source == SOURCE_TEMPLATE


async def test_phrase_finding_rejects_llm_text_with_an_invented_number():
    # Arrange
    client = _client_answering(200, _message_payload("BTC odds jumped to 70%, a strong bullish signal."))

    # Act
    phrased = await phrase_finding(FACTS, settings=_settings(), client=client)

    # Assert
    assert phrased.source == SOURCE_TEMPLATE


async def test_phrase_finding_accepts_llm_text_that_only_repeats_fact_numbers():
    # Arrange
    text = "Options now imply 62% odds that BTC finishes above $100,000 on 2026-12-26, up 7.1 pts."
    client = _client_answering(200, _message_payload(text))

    # Act
    phrased = await phrase_finding(FACTS, settings=_settings(), client=client)

    # Assert
    assert phrased.source == SOURCE_LLM
    assert phrased.summary == text
    assert phrased.headline == template_headline(FACTS)


async def test_phrase_finding_requests_low_effort_and_server_side_fallback_for_sonnet_5_5():
    # Arrange
    seen: list = []
    client = _client_answering(200, _message_payload("BTC is at 62%."), seen_requests=seen)

    # Act
    await phrase_finding(FACTS, settings=_settings(model="claude-sonnet-5-5"), client=client)

    # Assert
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["output_config"]["effort"] == "low"
    assert body["fallbacks"] == "default"
    assert SERVER_FALLBACK_BETA in seen[0].headers["anthropic-beta"]


# ─── classify_question ───────────────────────────────────────────────────────

def test_keyword_router_picks_agents_by_word_stems_and_assets_by_name():
    # Act
    route = route_question_by_keywords("Is bitcoin funding crowded before the CPI print?", WATCHLIST)

    # Assert
    assert route.agents == ("OnchainPerps", "MacroFilings")
    assert route.assets == ("BTC",)
    assert route.source == SOURCE_TEMPLATE


def test_keyword_router_does_not_match_inside_words():
    # Act: "separate" contains "rate" but does not start with it
    route = route_question_by_keywords("Give me a separate view on NVDA earnings", WATCHLIST)

    # Assert
    assert route.agents == ("OptionsEvent",)
    assert route.assets == ("NVDA",)


def test_keyword_router_asks_every_agent_about_the_watchlist_when_nothing_matches():
    # Act
    route = route_question_by_keywords("What should I know today?", WATCHLIST)

    # Assert
    assert route.agents == ("OnchainPerps", "OptionsEvent", "NewsSentiment", "MacroFilings")
    assert route.assets == tuple(WATCHLIST)


async def test_classify_question_falls_back_to_keywords_without_api_key():
    # Act
    route = await classify_question("Any news on ETH?", WATCHLIST, settings=_settings(api_key=""))

    # Assert
    assert route.source == SOURCE_TEMPLATE
    assert route.agents == ("NewsSentiment",)
    assert route.assets == ("ETH",)


async def test_classify_question_uses_valid_llm_json_route():
    # Arrange
    answer = json.dumps({"agents": ["OptionsEvent", "NewsSentiment"], "assets": ["NVDA"]})
    client = _client_answering(200, _message_payload(answer))

    # Act
    route = await classify_question("What is priced in for Nvidia?", WATCHLIST, settings=_settings(), client=client)

    # Assert
    assert route.source == SOURCE_LLM
    assert route.agents == ("OptionsEvent", "NewsSentiment")
    assert route.assets == ("NVDA",)


async def test_classify_question_ignores_unknown_agents_and_assets_from_the_llm():
    # Arrange
    answer = json.dumps({"agents": ["HedgeFundGPT"], "assets": ["DOGECOIN2"]})
    client = _client_answering(200, _message_payload(answer))

    # Act
    route = await classify_question("Any news on ETH?", WATCHLIST, settings=_settings(), client=client)

    # Assert
    assert route.source == SOURCE_TEMPLATE
    assert route.agents == ("NewsSentiment",)


def test_detect_assets_needs_capitals_for_crypto_symbols_off_the_watchlist():
    # Act
    route_word = route_question_by_keywords("Send me a link to the latest btc news", WATCHLIST)
    route_symbol = route_question_by_keywords("Is LINK funding crowded?", WATCHLIST)

    # Assert
    assert route_word.assets == ("BTC",)
    assert route_symbol.assets == ("LINK",)
