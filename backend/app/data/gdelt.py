"""
GDELT DOC 2.0 adapter: recent news headlines and the news-tone time series.

Used by the `news_sentiment` seller (SPEC §4.4). Two key-less requests to the same
endpoint, differing only in `mode`:

    GET https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=artlist&format=json&timespan=24h&maxrecords=50
        → {"articles": [{"url", "title", "seendate": "20261011T121500Z", "domain", "language", "sourcecountry"}, ...]}

    GET https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=timelinetone&format=json&timespan=24h
        → {"timeline": [{"series": "Average Tone", "data": [{"date": "20261011T120000Z", "value": -1.23}, ...]}]}

GDELT quirks this module handles on purpose:
  * Errors and rate-limit notices come back as **plain text** (often with HTTP 200), not
    JSON — so we download text and decode it ourselves (`decode_gdelt_body`).
  * GDELT asks callers to send **at most one request every 5 seconds**. The shared HTTP
    helper spaces "gdelt" requests by only 1 s, so this module adds its own 5-second
    slot (`_wait_for_gdelt_slot`) in front of every request it makes.
  * A query with no matches returns `{}`; that parses to an empty list, not an error.

Tone is GDELT's average document tone (roughly −10 very negative … +10 very positive).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.data.http import get_text

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
SOURCE_NAME = "gdelt"

DEFAULT_TIMESPAN = "24h"
DEFAULT_MAX_RECORDS = 50
ARTICLE_SORT_ORDER = "datedesc"           # newest first
SEENDATE_FORMAT = "%Y%m%dT%H%M%SZ"        # e.g. 20261011T121500Z (always UTC)
ENGLISH_ONLY_OPERATOR = "sourcelang:english"

# GDELT: "Please limit requests to one every 5 seconds".
GDELT_MIN_SECONDS_BETWEEN_REQUESTS = 5.0
GDELT_CACHE_TTL_SECONDS = 300.0

_gdelt_slot_lock = asyncio.Lock()
_last_gdelt_request_at = 0.0


class GdeltError(Exception):
    """GDELT answered with something other than the documented JSON (error text, rate limit...)."""


@dataclass(frozen=True)
class Article:
    """One headline from `mode=artlist`."""

    title: str
    url: str
    domain: str
    seen_at: datetime          # when GDELT first saw the article (UTC)
    language: str
    source_country: str


@dataclass(frozen=True)
class TonePoint:
    """One bucket of `mode=timelinetone`: the average tone of matching coverage at `at`."""

    at: datetime
    tone: float


# ─── query building ──────────────────────────────────────────────────────────

def build_query(terms: list[str], english_only: bool = True) -> str:
    """
    Build a GDELT query string from search terms.

    One term is used as-is; several terms become `(a OR b)` (GDELT requires the
    parentheses around OR). Multi-word terms are quoted as exact phrases.
    """
    quoted = [f'"{term}"' if " " in term else term for term in terms]
    query = quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")"
    if english_only:
        query = f"{query} {ENGLISH_ONLY_OPERATOR}"
    return query


def _request_params(query: str, mode: str, timespan: str) -> dict[str, str]:
    """Query-string parameters shared by both modes."""
    return {"query": query, "mode": mode, "format": "json", "timespan": timespan}


# ─── pure parsing ────────────────────────────────────────────────────────────

def decode_gdelt_body(text: str) -> dict:
    """
    Decode a GDELT response body. GDELT reports errors as plain text, so anything
    that is not a JSON object raises GdeltError carrying the first 200 characters.
    """
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GdeltError(f"non-JSON response: {text.strip()[:200]!r}") from exc
    if not isinstance(decoded, dict):
        raise GdeltError(f"unexpected JSON type {type(decoded).__name__}")
    return decoded


def parse_seendate(value: str) -> datetime:
    """Parse GDELT's compact UTC timestamp `YYYYMMDDTHHMMSSZ`."""
    return datetime.strptime(value, SEENDATE_FORMAT).replace(tzinfo=timezone.utc)


def parse_artlist(payload: dict) -> list[Article]:
    """Parse `mode=artlist` JSON into Articles. `{}` (no matches) gives an empty list."""
    articles: list[Article] = []
    for raw in payload.get("articles", []):
        title = str(raw.get("title", "")).strip()
        if not title or not raw.get("seendate"):
            continue
        articles.append(
            Article(
                title=title,
                url=str(raw.get("url", "")),
                domain=str(raw.get("domain", "")),
                seen_at=parse_seendate(raw["seendate"]),
                language=str(raw.get("language", "")),
                source_country=str(raw.get("sourcecountry", "")),
            )
        )
    return articles


def parse_timeline_tone(payload: dict) -> list[TonePoint]:
    """
    Parse `mode=timelinetone` JSON into TonePoints, oldest first.

    The response holds one series ("Average Tone"); we read the first series' data.
    """
    timeline = payload.get("timeline", [])
    if not timeline:
        return []
    points = [
        TonePoint(at=parse_seendate(item["date"]), tone=float(item["value"]))
        for item in timeline[0].get("data", [])
    ]
    return sorted(points, key=lambda point: point.at)


# ─── network ─────────────────────────────────────────────────────────────────

async def _wait_for_gdelt_slot() -> None:
    """Sleep until at least GDELT_MIN_SECONDS_BETWEEN_REQUESTS have passed since our last GDELT request."""
    global _last_gdelt_request_at
    async with _gdelt_slot_lock:
        wait_seconds = _last_gdelt_request_at + GDELT_MIN_SECONDS_BETWEEN_REQUESTS - time.monotonic()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        _last_gdelt_request_at = time.monotonic()


async def _fetch_mode(query: str, mode: str, timespan: str, extra_params: dict[str, Any] | None = None) -> dict:
    """Fetch one GDELT mode as text (errors are text) and decode it."""
    params: dict[str, Any] = _request_params(query, mode, timespan)
    if extra_params:
        params.update(extra_params)
    await _wait_for_gdelt_slot()
    text = await get_text(GDELT_DOC_URL, source=SOURCE_NAME, params=params, ttl=GDELT_CACHE_TTL_SECONDS)
    return decode_gdelt_body(text)


async def fetch_articles(
    query: str, timespan: str = DEFAULT_TIMESPAN, max_records: int = DEFAULT_MAX_RECORDS
) -> list[Article]:
    """Newest matching articles in the window (mode=artlist)."""
    payload = await _fetch_mode(
        query, "artlist", timespan, {"maxrecords": max_records, "sort": ARTICLE_SORT_ORDER}
    )
    return parse_artlist(payload)


async def fetch_tone_timeline(query: str, timespan: str = DEFAULT_TIMESPAN) -> list[TonePoint]:
    """Average tone of matching coverage over the window (mode=timelinetone)."""
    payload = await _fetch_mode(query, "timelinetone", timespan)
    return parse_timeline_tone(payload)
