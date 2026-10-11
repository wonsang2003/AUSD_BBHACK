"""
Tests for `app/data/contextdev.py` — optional Context.dev web search ("fast mode": plain search,
no scraping) used by `news_sentiment` only when CONTEXT_DEV_API_KEY is set.

Fixture: `tests/fixtures/contextdev_search_bitcoin.json` is a hand-built payload written OFFLINE
in the response shape described in the adapter's docstring (marked UNVERIFIED there — no
network from this container to check the live API).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.data import contextdev

FIXTURES = Path(__file__).parent / "fixtures"


def load_payload() -> dict:
    return json.loads((FIXTURES / "contextdev_search_bitcoin.json").read_text())


def test_search_body_is_the_cheap_fresh_search_without_scraping():
    body = contextdev.build_search_body("bitcoin")
    assert body == {"query": "bitcoin", "numResults": 10, "freshness": "last_24_hours"}
    assert "markdownOptions" not in body and "highlights" not in body


def test_results_are_parsed_with_domains_and_results_without_url_are_skipped():
    # Act
    results = contextdev.parse_search_results(load_payload())

    # Assert
    assert [result.domain for result in results] == ["reuters.com", "coindesk.com"]
    assert results[0].title == "Bitcoin ETF inflows top $1 billion as price nears record"


def test_result_list_under_data_key_is_accepted_too():
    payload = {"data": [{"title": "T", "url": "https://www.example.com/a"}]}
    assert contextdev.parse_search_results(payload)[0].domain == "example.com"


async def test_search_without_api_key_is_refused():
    with pytest.raises(ValueError, match="CONTEXT_DEV_API_KEY"):
        await contextdev.search_news("bitcoin", "")


async def test_search_sends_the_key_only_in_the_authorization_header(monkeypatch):
    # Arrange
    sent: dict = {}

    async def fake_post_json(url, body, *, source, headers=None, ttl=30.0):
        sent.update(url=url, body=body, headers=headers)
        return load_payload()

    monkeypatch.setattr(contextdev, "post_json", fake_post_json)

    # Act
    results = await contextdev.search_news("bitcoin", "ctx_test_key")

    # Assert
    assert sent["headers"] == {"Authorization": "Bearer ctx_test_key"}
    assert "ctx_test_key" not in json.dumps(sent["body"])
    assert len(results) == 2
