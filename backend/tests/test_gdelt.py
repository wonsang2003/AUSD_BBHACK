"""
Tests for `app/data/gdelt.py` — GDELT DOC 2.0 headlines (`mode=artlist`) and news tone
(`mode=timelinetone`) for the `news_sentiment` demo vendor.

Fixtures: `tests/fixtures/gdelt_artlist_bitcoin.json` and `gdelt_timelinetone_bitcoin.json` are
REALISTIC-SHAPE payloads hand-built OFFLINE in GDELT's documented JSON formats (compact
`YYYYMMDDTHHMMSSZ` timestamps, 96 fifteen-minute tone buckets, 0.0 for buckets without
coverage, one article with an empty title). They are not live recordings: no network here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.data import gdelt
from app.data.gdelt import GdeltError

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture(autouse=True)
def fresh_gdelt_state(monkeypatch):
    """No cached answers and no 5-second slot wait between test requests."""
    gdelt.clear_cache()
    monkeypatch.setattr(gdelt, "GDELT_MIN_SECONDS_BETWEEN_REQUESTS", 0.0)
    yield
    gdelt.clear_cache()


def test_query_for_one_term_adds_the_english_filter():
    assert gdelt.build_query(["bitcoin"]) == "bitcoin sourcelang:english"


def test_query_for_several_terms_uses_parenthesised_or_and_quotes_phrases():
    assert gdelt.build_query(["NVDA", "Nvidia Corp"], english_only=False) == '(NVDA OR "Nvidia Corp")'


def test_artlist_parses_titles_and_utc_seen_times_and_skips_untitled_articles():
    # Act
    articles = gdelt.parse_artlist(gdelt.decode_gdelt_body(fixture_text("gdelt_artlist_bitcoin.json")))

    # Assert
    assert len(articles) == 12, "13 raw articles, one without a title"
    first = articles[0]
    assert first.title.startswith("Bitcoin ETF inflows")
    assert first.domain == "reuters.com"
    assert first.seen_at == datetime(2026, 10, 11, 13, 15, tzinfo=timezone.utc)


def test_timeline_tone_is_sorted_oldest_first():
    points = gdelt.parse_timeline_tone(gdelt.decode_gdelt_body(fixture_text("gdelt_timelinetone_bitcoin.json")))
    assert len(points) == 96
    assert points[0].at == datetime(2026, 10, 10, 14, 15, tzinfo=timezone.utc)
    assert points == sorted(points, key=lambda point: point.at)


def test_no_matches_is_an_empty_list_not_an_error():
    assert gdelt.parse_artlist(gdelt.decode_gdelt_body("{}")) == []
    assert gdelt.parse_timeline_tone({}) == []


def test_plain_text_rate_limit_notice_raises_gdelt_error():
    with pytest.raises(GdeltError, match="limit requests"):
        gdelt.decode_gdelt_body("Please limit requests to one every 5 seconds or contact us for larger queries.")


def test_control_characters_inside_titles_are_tolerated():
    body = '{"articles": [{"title": "Bitcoin\tsurges", "seendate": "20261011T120000Z", "url": "u"}]}'
    assert gdelt.parse_artlist(gdelt.decode_gdelt_body(body))[0].title == "Bitcoin\tsurges"


def test_malformed_tone_bucket_raises_gdelt_error():
    with pytest.raises(GdeltError):
        gdelt.parse_timeline_tone({"timeline": [{"data": [{"date": "yesterday", "value": 1}]}]})


async def test_fetch_sends_documented_params_and_caches_the_decoded_answer(monkeypatch):
    # Arrange
    calls: list[dict] = []

    async def fake_get_text(url, *, source, params=None, headers=None, ttl=300.0):
        calls.append(params)
        return fixture_text("gdelt_artlist_bitcoin.json")

    monkeypatch.setattr(gdelt, "get_text", fake_get_text)

    # Act
    first = await gdelt.fetch_articles("bitcoin sourcelang:english")
    second = await gdelt.fetch_articles("bitcoin sourcelang:english")

    # Assert
    assert first == second
    assert len(calls) == 1, "the second call is served from the decoded cache"
    assert calls[0] == {"query": "bitcoin sourcelang:english", "mode": "artlist", "format": "json",
                        "timespan": "24h", "maxrecords": 50, "sort": "datedesc"}
