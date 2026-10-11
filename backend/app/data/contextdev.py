"""
Context.dev adapter: optional extra headlines for the `news_sentiment` seller.

Only used when `CONTEXT_DEV_API_KEY` is set (SPEC §4.4). Context.dev is a metered,
API-key web-search service:

    POST https://api.context.dev/v1/web/search
    Authorization: Bearer <CONTEXT_DEV_API_KEY>
    {"query": "...", "numResults": 10, "freshness": "last_24_hours"}
      → {"results": [{"url", "title", "description", "relevance"}, ...], "query", "request_id", ...}

"Fast mode only" (SPEC): we call the plain search and never switch on the optional
per-result page scraping (`markdownOptions`) or passage extraction (`highlights`),
which are the slow and credit-hungry parts. The `/v1/web/answers` research endpoint
(which has an explicit `mode: fast|ultra`) is not used at all.

The API key goes only into the Authorization header and is never logged.

UNVERIFIED SHAPE: the request/response shape above could not be checked against live
docs from the build container. The parser therefore accepts the result list under
either "results" or "data", and any failure is recorded by the seller as an input
error rather than failing the purchase. Verify with a real key on a networked machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from app.data.http import post_json

CONTEXT_DEV_SEARCH_URL = "https://api.context.dev/v1/web/search"
SOURCE_NAME = "contextdev"

# The API accepts 10–100 results; 10 is the minimum and the cheapest.
SEARCH_NUM_RESULTS = 10
RESULT_LIST_KEYS = ("results", "data")   # where the result list may live in the response
SEARCH_FRESHNESS = "last_24_hours"
SEARCH_CACHE_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class SearchResult:
    """One ranked web result."""

    title: str
    url: str
    description: str
    domain: str


def build_search_body(query: str) -> dict:
    """Request body for a cheap, fresh search (no scraping, no highlights)."""
    return {"query": query, "numResults": SEARCH_NUM_RESULTS, "freshness": SEARCH_FRESHNESS}


def _domain_of(url: str) -> str:
    """Host part of a URL without a leading "www."."""
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.")


def _result_list(payload: dict) -> list[dict]:
    """The first list found under RESULT_LIST_KEYS, or [] if there is none."""
    for key in RESULT_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def parse_search_results(payload: dict) -> list[SearchResult]:
    """Parse the result list; results without a title or URL are skipped."""
    parsed: list[SearchResult] = []
    for raw in _result_list(payload):
        title = str(raw.get("title") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not title or not url:
            continue
        parsed.append(
            SearchResult(
                title=title,
                url=url,
                description=str(raw.get("description") or ""),
                domain=_domain_of(url),
            )
        )
    return parsed


async def search_news(query: str, api_key: str) -> list[SearchResult]:
    """Search the last 24 h of the web for `query`. Requires a Context.dev API key."""
    if not api_key:
        raise ValueError("Context.dev search needs CONTEXT_DEV_API_KEY")
    payload = await post_json(
        CONTEXT_DEV_SEARCH_URL,
        build_search_body(query),
        source=SOURCE_NAME,
        headers={"Authorization": f"Bearer {api_key}"},
        ttl=SEARCH_CACHE_TTL_SECONDS,
    )
    return parse_search_results(payload)
