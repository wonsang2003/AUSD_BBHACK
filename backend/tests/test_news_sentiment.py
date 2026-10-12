"""
Tests for `app/sellers/news_sentiment.py` — the news_sentiment demo vendor (0.03 AUSD): GDELT
24 h headlines, tone trend and event flags (+ optional Context.dev headlines).

Fixtures: `tests/fixtures/gdelt_artlist_bitcoin.json`, `gdelt_timelinetone_bitcoin.json` and
`contextdev_search_bitcoin.json` are payloads hand-built OFFLINE in the upstream formats (not live
recordings; no network here). In the tone fixture the second half of the window is about +1.2
tone points better than the first half, and some buckets are 0.0 ("no coverage").
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.data import contextdev, gdelt
from app.data.gdelt import GdeltError, TonePoint
from app.data.http import UpstreamError
from app.sellers import news_sentiment
from app.sellers.expected_move import ERROR_BAD_REQUEST, ERROR_UPSTREAM, SellerConfigError

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
PAYOUT_ADDRESS = "0x" + "4d" * 20


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def fixture_articles() -> list[gdelt.Article]:
    return gdelt.parse_artlist(load_json("gdelt_artlist_bitcoin.json"))


def fixture_tone() -> list[TonePoint]:
    return gdelt.parse_timeline_tone(load_json("gdelt_timelinetone_bitcoin.json"))


@pytest.fixture(autouse=True)
def template_summarizer_and_no_context_dev_key(monkeypatch):
    news_sentiment.set_summarizer(None)
    monkeypatch.setattr(get_settings(), "context_dev_api_key", "")
    yield
    news_sentiment.set_summarizer(None)


@pytest.fixture
def gdelt_from_fixtures(monkeypatch):
    queries: list[str] = []

    async def articles(query: str, timespan: str = "24h", max_records: int = 50):
        queries.append(query)
        return fixture_articles()

    async def tone(query: str, timespan: str = "24h"):
        return fixture_tone()

    monkeypatch.setattr(gdelt, "fetch_articles", articles)
    monkeypatch.setattr(gdelt, "fetch_tone_timeline", tone)
    return queries


# ─── offer and query ─────────────────────────────────────────────────────────

def test_offer_costs_three_cents_and_is_labelled_demo_vendor(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_news", PAYOUT_ADDRESS)
    offer = news_sentiment.make_offer()
    assert (offer.price_atomic, offer.path) == (30_000, "/sell/news-sentiment")
    assert "demo vendor" in offer.description.lower()


def test_offer_without_payout_address_raises(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_news", "")
    with pytest.raises(SellerConfigError, match="SELLER_PAYTO_NEWS"):
        news_sentiment.make_offer()


@pytest.mark.parametrize("q", ["", "ab", "x" * 61, "btc; drop"])
def test_invalid_queries_are_rejected(q):
    with pytest.raises(ValueError):
        news_sentiment.parse_query_param({"q": q})


def test_tickers_are_searched_by_name():
    assert news_sentiment.search_terms_for("btc") == ["bitcoin"]
    assert news_sentiment.search_terms_for("NVDA") == ["NVDA", "Nvidia"]
    assert news_sentiment.search_terms_for("Palantir") == ["Palantir"]


# ─── tone ────────────────────────────────────────────────────────────────────

def test_tone_trend_compares_halves_and_ignores_no_coverage_buckets():
    # Act
    trend = news_sentiment.tone_trend(fixture_tone())

    # Assert
    points = fixture_tone()
    first = [p.tone for p in points[:48] if p.tone != 0.0]
    second = [p.tone for p in points[48:] if p.tone != 0.0]
    assert trend.first_half_mean == pytest.approx(sum(first) / len(first))
    assert trend.tone_delta == pytest.approx(sum(second) / len(second) - sum(first) / len(first))
    assert trend.direction == "improving"
    assert trend.points_used == len(first) + len(second)


def test_tone_trend_needs_coverage_in_both_halves():
    at = datetime(2026, 10, 11, tzinfo=timezone.utc)
    assert news_sentiment.tone_trend([TonePoint(at, 0.0), TonePoint(at, -1.0)]) is None


@pytest.mark.parametrize(("delta", "direction"), [(0.49, "stable"), (0.5, "improving"), (-0.7, "worsening")])
def test_tone_direction_band(delta, direction):
    assert news_sentiment.tone_direction(delta) == direction


# ─── flags and headlines ─────────────────────────────────────────────────────

def test_event_flags_are_detected_by_keyword_rules():
    flags = news_sentiment.detect_event_flags([article.title for article in fixture_articles()])
    assert list(flags) == ["hack", "listing", "delisting", "lawsuit", "sec", "upgrade", "etf", "halt", "bankruptcy"]
    assert len(flags["etf"]) == 2, "at most two example headlines per flag"


def test_sec_flag_is_case_sensitive_and_listing_does_not_match_delisting():
    flags = news_sentiment.detect_event_flags(["Second quarter results", "Token delisting announced"])
    assert list(flags) == ["delisting"]


def test_duplicate_headlines_are_merged_case_insensitively():
    headlines = news_sentiment.merge_headlines(fixture_articles(), [])
    titles = [headline.title.casefold() for headline in headlines]
    assert len(titles) == len(set(titles))
    assert headlines[0].seen_at == "2026-10-11T13:15:00+00:00", "newest first"


def test_template_summary_is_deterministic_and_mentions_flags():
    read = news_sentiment.analyze(fixture_articles(), fixture_tone(), [])
    first = news_sentiment.summarize(read.headlines, read.tone, read.flags, subject="BTC")
    second = news_sentiment.summarize(read.headlines, read.tone, read.flags, subject="BTC")
    assert first == second
    assert "Event flags: hack" in first and "improving" in first


async def test_plugged_summarizer_is_used_and_a_failing_one_falls_back_to_the_template():
    read = news_sentiment.analyze(fixture_articles(), fixture_tone(), [])

    async def llm(headlines, tone, flags):
        return "Coverage is busy and turning less negative."

    async def broken(headlines, tone, flags):
        raise RuntimeError("LLM timeout")

    news_sentiment.set_summarizer(llm)
    assert (await news_sentiment.summary_for(read.headlines, read.tone, read.flags, "BTC"))[1] == "plugged_summarizer"
    news_sentiment.set_summarizer(broken)
    assert (await news_sentiment.summary_for(read.headlines, read.tone, read.flags, "BTC"))[1] == "template"


# ─── compute ─────────────────────────────────────────────────────────────────

async def test_compute_returns_tone_delta_flags_and_required_fields(gdelt_from_fixtures):
    # Act
    response = await news_sentiment.compute_at({"q": "BTC"}, NOW)

    # Assert
    assert gdelt_from_fixtures == ["bitcoin sourcelang:english"]
    assert response["tone_delta"] == pytest.approx(response["tone"]["tone_delta"])
    assert "hack" in response["event_flags"]
    assert response["article_count"] == 12
    assert 0 < response["confidence"] <= 0.9
    assert response["inputs_used"]["context_dev_results"] == 0
    for field in ("inputs_used", "computed_at", "method", "confidence", "data_delay", "summary"):
        assert field in response


async def test_context_dev_headlines_are_added_when_a_key_is_set(gdelt_from_fixtures, monkeypatch):
    # Arrange
    monkeypatch.setattr(get_settings(), "context_dev_api_key", "ctx_test")

    async def search(query: str, api_key: str):
        return contextdev.parse_search_results(load_json("contextdev_search_bitcoin.json"))

    monkeypatch.setattr(contextdev, "search_news", search)

    # Act
    response = await news_sentiment.compute_at({"q": "BTC"}, NOW)

    # Assert
    assert response["inputs_used"]["context_dev_results"] == 2
    assert "context_dev" not in response["inputs_used"]["errors"]


def test_context_dev_results_follow_gdelt_headlines_without_duplicates():
    results = contextdev.parse_search_results(load_json("contextdev_search_bitcoin.json"))
    merged = news_sentiment.merge_headlines(fixture_articles()[:3], results)
    assert [headline.provider for headline in merged] == ["gdelt", "gdelt", "gdelt", "context.dev"]
    assert merged[-1].title.startswith("Options traders"), "the duplicate ETF headline is dropped"


async def test_one_failing_gdelt_mode_is_recorded_not_fatal(gdelt_from_fixtures, monkeypatch):
    async def tone_down(query: str, timespan: str = "24h"):
        raise GdeltError("non-JSON response: 'Please limit requests'")

    monkeypatch.setattr(gdelt, "fetch_tone_timeline", tone_down)
    response = await news_sentiment.compute_at({"q": "bitcoin"}, NOW)
    assert "error" not in response
    assert response["tone"] is None and "gdelt_timelinetone" in response["inputs_used"]["errors"]


async def test_all_sources_failing_returns_an_error_dict(monkeypatch):
    async def down(*args, **kwargs):
        raise UpstreamError("gdelt", "failed after 3 attempts")

    monkeypatch.setattr(gdelt, "fetch_articles", down)
    monkeypatch.setattr(gdelt, "fetch_tone_timeline", down)
    response = await news_sentiment.compute_at({"q": "BTC"}, NOW)
    assert response["error_kind"] == ERROR_UPSTREAM and response["confidence"] == 0.0


async def test_bad_query_returns_an_error_dict():
    response = await news_sentiment.compute_at({"q": "x"}, NOW)
    assert response["error_kind"] == ERROR_BAD_REQUEST
