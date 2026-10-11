"""
Seller `news_sentiment` (demo vendor, 0.03 AUSD): what is the news saying about an asset?

`GET /sell/news-sentiment?q=<asset or ticker>` (SPEC §4.4). Data:
  * GDELT DOC 2.0 (`app/data/gdelt.py`): last-24 h headlines (artlist) and the
    news-tone time series (timelinetone), key-less;
  * Context.dev web search (`app/data/contextdev.py`): extra headlines, only when
    CONTEXT_DEV_API_KEY is set.

What we compute:
    tone trend   mean tone of the first half of the window vs the second half;
                 tone_delta = second_half_mean − first_half_mean (GDELT tone ≈ −10…+10).
                 GDELT writes 0.0 for 15-minute buckets with no coverage, so exact zeros
                 are treated as "no data" and left out of the means.
    event flags  keyword rules over headlines: hack, exploit, listing, delisting,
                 lawsuit, sec, guidance, downgrade, upgrade, etf, halt, bankruptcy
    summary      `summarize(headlines, tone, flags)` — a deterministic template today.
                 It is the hook where LLM phrasing can be plugged in later; numbers
                 and flags always come from this module, never from the LLM.

Agents read `tone_delta` and `event_flags` — see
`app/agents/beliefs.update_from_news_sentiment`.

This module never imports the x402 server; `app/sellers/routers.py` wraps `compute`.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Awaitable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TypeVar

from app.config import get_settings
from app.data import contextdev, gdelt
from app.data.contextdev import SearchResult
from app.data.gdelt import Article, GdeltError, TonePoint
from app.data.http import UpstreamError
from app.x402.offer import SellerOffer

# ─── offer ───────────────────────────────────────────────────────────────────

SELLER_ID = "news_sentiment"
SELLER_PATH = "/sell/news-sentiment"
PRICE_ATOMIC = 30_000  # 0.03 AUSD (6 decimals)
DESCRIPTION = "News sentiment (demo vendor): GDELT 24h headlines, tone trend and event flags for an asset or ticker"

# ─── parameters ──────────────────────────────────────────────────────────────

MIN_QUERY_LENGTH = 3      # GDELT rejects keywords shorter than 3 characters
MAX_QUERY_LENGTH = 60
QUERY_PATTERN = re.compile(r"^[A-Za-z0-9 .&'-]+$")

# Crypto tickers are searched by name: news says "bitcoin" far more often than "BTC".
CRYPTO_SEARCH_TERMS: dict[str, list[str]] = {
    "BTC": ["bitcoin"],
    "ETH": ["ethereum"],
    "SOL": ["solana"],
    "XRP": ["XRP"],
    "DOGE": ["dogecoin"],
    "HYPE": ["hyperliquid"],
    "AVAX": ["AVAX"],
    "LINK": ["chainlink"],
    "BNB": ["BNB"],
}

# ─── tone rules ──────────────────────────────────────────────────────────────

NO_COVERAGE_TONE = 0.0          # GDELT's value for a bucket with no matching articles
TONE_STABLE_BAND = 0.5          # |tone_delta| below this many tone points = "stable"
TONE_IMPROVING = "improving"
TONE_WORSENING = "worsening"
TONE_STABLE = "stable"

# ─── event-flag keyword rules (flag name → pattern over headlines) ───────────

_IGNORE_CASE = re.IGNORECASE
EVENT_FLAG_PATTERNS: dict[str, re.Pattern[str]] = {
    "hack": re.compile(r"\bhack(?:s|ed|ers?|ing)?\b", _IGNORE_CASE),
    "exploit": re.compile(r"\bexploit(?:s|ed)?\b", _IGNORE_CASE),
    "listing": re.compile(r"\blistings?\b", _IGNORE_CASE),          # does not match "delisting"
    "delisting": re.compile(r"\bdelist(?:s|ed|ing|ings)?\b", _IGNORE_CASE),
    "lawsuit": re.compile(r"\blawsuits?\b|\bsue[sd]?\b", _IGNORE_CASE),
    "sec": re.compile(r"\bSEC\b"),                                   # case-sensitive: the regulator
    "guidance": re.compile(r"\bguidance\b", _IGNORE_CASE),
    "downgrade": re.compile(r"\bdowngrade[sd]?\b", _IGNORE_CASE),
    "upgrade": re.compile(r"\bupgrade[sd]?\b", _IGNORE_CASE),
    "etf": re.compile(r"\bETFs?\b", _IGNORE_CASE),
    "halt": re.compile(r"\bhalt(?:s|ed|ing)?\b", _IGNORE_CASE),
    "bankruptcy": re.compile(r"\bbankrupt(?:cy|cies)?\b|\bchapter 11\b", _IGNORE_CASE),
}
MAX_EXAMPLES_PER_FLAG = 2

# ─── output / confidence ─────────────────────────────────────────────────────

TOP_HEADLINES = 10
ARTICLES_FOR_FULL_COVERAGE = 30
BASE_CONFIDENCE = 0.2            # some data arrived
COVERAGE_CONFIDENCE_WEIGHT = 0.5 # scaled by min(1, articles / 30)
TONE_CONFIDENCE_BONUS = 0.2      # tone trend available
DATA_DELAY = "GDELT updates every 15 minutes; window = last 24 h"
PROVIDER_GDELT = "gdelt"
PROVIDER_CONTEXT_DEV = "context.dev"


def make_offer() -> SellerOffer:
    """What this demo vendor charges (0.03 AUSD) and where the AUSD goes (SELLER_PAYTO_NEWS)."""
    return SellerOffer(
        seller_id=SELLER_ID,
        path=SELLER_PATH,
        description=DESCRIPTION,
        price_atomic=PRICE_ATOMIC,
        pay_to=get_settings().seller_payto_news,
    )


# ─── data types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Headline:
    """A headline in the response, from either provider."""

    title: str
    url: str
    source: str               # publishing domain
    seen_at: str | None       # ISO UTC when GDELT first saw it (None for Context.dev)
    provider: str             # "gdelt" | "context.dev"


@dataclass(frozen=True)
class ToneTrend:
    """Tone over the window: overall mean and first-half vs second-half means."""

    mean_tone: float
    first_half_mean: float
    second_half_mean: float
    tone_delta: float          # second_half_mean − first_half_mean
    direction: str             # improving | worsening | stable
    points_used: int


# ─── query ───────────────────────────────────────────────────────────────────

def parse_query_param(params: dict) -> str:
    """Validate `q`: 3–60 characters of letters, digits, spaces and . & ' -."""
    query = str(params.get("q") or "").strip()
    if len(query) < MIN_QUERY_LENGTH:
        raise ValueError(f"q must be at least {MIN_QUERY_LENGTH} characters (GDELT rejects shorter keywords)")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"q must be at most {MAX_QUERY_LENGTH} characters")
    if not QUERY_PATTERN.match(query):
        raise ValueError("q may contain only letters, digits, spaces and . & ' -")
    return query


def search_terms_for(query: str) -> list[str]:
    """Known crypto tickers become their names (BTC → bitcoin); anything else is searched as given."""
    return CRYPTO_SEARCH_TERMS.get(query.upper(), [query])


# ─── tone ────────────────────────────────────────────────────────────────────

def covered_points(points: list[TonePoint]) -> list[TonePoint]:
    """Drop buckets whose tone is exactly 0.0 — GDELT's marker for "no coverage"."""
    return [point for point in points if point.tone != NO_COVERAGE_TONE]


def tone_direction(tone_delta: float) -> str:
    """|Δ| < 0.5 tone points → stable; otherwise improving (Δ > 0) or worsening (Δ < 0)."""
    if abs(tone_delta) < TONE_STABLE_BAND:
        return TONE_STABLE
    return TONE_IMPROVING if tone_delta > 0 else TONE_WORSENING


def tone_trend(points: list[TonePoint]) -> ToneTrend | None:
    """
    Split the time-ordered buckets in half (GDELT buckets are evenly spaced, so this
    is the window's time midpoint), average the covered buckets of each half, and
    take tone_delta = second_half_mean − first_half_mean.
    Returns None if either half has no covered bucket.
    """
    ordered = sorted(points, key=lambda point: point.at)
    midpoint = len(ordered) // 2
    first_half = covered_points(ordered[:midpoint])
    second_half = covered_points(ordered[midpoint:])
    if not first_half or not second_half:
        return None
    first_mean = statistics.fmean(point.tone for point in first_half)
    second_mean = statistics.fmean(point.tone for point in second_half)
    delta = second_mean - first_mean
    all_covered = first_half + second_half
    return ToneTrend(
        mean_tone=statistics.fmean(point.tone for point in all_covered),
        first_half_mean=first_mean,
        second_half_mean=second_mean,
        tone_delta=delta,
        direction=tone_direction(delta),
        points_used=len(all_covered),
    )


# ─── event flags ─────────────────────────────────────────────────────────────

def detect_event_flags(titles: list[str]) -> dict[str, list[str]]:
    """
    Flag name → headlines that triggered it (at most 2 examples each), in the fixed
    flag order of EVENT_FLAG_PATTERNS. Flags with no match are left out.
    """
    matches: dict[str, list[str]] = {}
    for flag, pattern in EVENT_FLAG_PATTERNS.items():
        hits = [title for title in titles if pattern.search(title)]
        if hits:
            matches[flag] = hits[:MAX_EXAMPLES_PER_FLAG]
    return matches


# ─── headlines ───────────────────────────────────────────────────────────────

def _normalized_title(title: str) -> str:
    """Case- and whitespace-insensitive key used to drop duplicate headlines."""
    return " ".join(title.casefold().split())


def merge_headlines(
    articles: list[Article], search_results: list[SearchResult], limit: int = TOP_HEADLINES
) -> list[Headline]:
    """GDELT articles newest first, then Context.dev results; duplicates (same title) removed."""
    candidates = [
        Headline(a.title, a.url, a.domain, a.seen_at.isoformat(), PROVIDER_GDELT)
        for a in sorted(articles, key=lambda article: article.seen_at, reverse=True)
    ]
    candidates += [Headline(r.title, r.url, r.domain, None, PROVIDER_CONTEXT_DEV) for r in search_results]
    seen: set[str] = set()
    merged: list[Headline] = []
    for headline in candidates:
        key = _normalized_title(headline.title)
        if key in seen:
            continue
        seen.add(key)
        merged.append(headline)
    return merged[:limit]


# ─── summary hook ────────────────────────────────────────────────────────────

def _tone_sentence(tone: ToneTrend | None) -> str:
    """One sentence describing the tone trend, or that it is unavailable."""
    if tone is None:
        return "News tone is unavailable for this window."
    return (
        f"Average news tone is {tone.mean_tone:+.1f} and {tone.direction} "
        f"({tone.tone_delta:+.1f} from the first to the second half of the window)."
    )


def summarize(headlines: list[Headline], tone: ToneTrend | None, flags: list[str], *, subject: str = "") -> str:
    """
    Plain-language summary. HOOK: an LLM phrasing step may replace this template later,
    but it must only rephrase these facts (counts, tone numbers, flags), never add numbers.
    Deterministic: the same inputs always give the same text.
    """
    about = f" about {subject}" if subject else ""
    parts = [f"{len(headlines)} recent headline(s){about} in the last 24h.", _tone_sentence(tone)]
    parts.append(f"Event flags: {', '.join(flags)}." if flags else "No event flags.")
    if headlines:
        parts.append(f'Latest: "{headlines[0].title}".')
    return " ".join(parts)


# ─── response ────────────────────────────────────────────────────────────────

def confidence_for(article_count: int, tone: ToneTrend | None) -> float:
    """
    0 with no data; otherwise 0.2 + 0.5 · min(1, articles / 30) + (0.2 if a tone trend exists).
    Maximum 0.9: keyword flags and average tone are coarse signals.
    """
    if article_count == 0 and tone is None:
        return 0.0
    coverage = min(1.0, article_count / ARTICLES_FOR_FULL_COVERAGE)
    bonus = TONE_CONFIDENCE_BONUS if tone is not None else 0.0
    return BASE_CONFIDENCE + COVERAGE_CONFIDENCE_WEIGHT * coverage + bonus


def build_response(
    query: str,
    gdelt_query: str,
    articles: list[Article],
    tone_points: list[TonePoint],
    search_results: list[SearchResult],
    errors: dict[str, str],
    now: datetime,
) -> dict:
    """Assemble the JSON body from already-fetched data (pure, so tests can use fixtures)."""
    headlines = merge_headlines(articles, search_results)
    tone = tone_trend(tone_points)
    flag_examples = detect_event_flags([a.title for a in articles] + [r.title for r in search_results])
    flags = list(flag_examples)
    return {
        "seller_id": SELLER_ID,
        "query": query,
        "headlines": [asdict(headline) for headline in headlines],
        "article_count": len(articles),
        "tone": asdict(tone) if tone else None,
        "tone_delta": tone.tone_delta if tone else None,
        "event_flags": flags,
        "event_flag_headlines": flag_examples,
        "summary": summarize(headlines, tone, flags, subject=query),
        "method": (
            "GDELT DOC 2.0 artlist + timelinetone (24h, English sources); tone_delta = mean tone of the "
            "second half of the window − first half (0.0 buckets = no coverage, excluded); event flags = "
            "keyword rules over headlines; summary = deterministic template"
        ),
        "inputs_used": {
            "gdelt_query": gdelt_query,
            "gdelt_articles": len(articles),
            "gdelt_tone_buckets": len(tone_points),
            "context_dev_results": len(search_results),
            "errors": errors,
        },
        "computed_at": now.isoformat(timespec="seconds"),
        "confidence": confidence_for(len(articles), tone),
        "data_delay": DATA_DELAY,
    }


# ─── network orchestration ───────────────────────────────────────────────────

_T = TypeVar("_T")


async def _fetch_or_error(name: str, call: Awaitable[list[_T]], errors: dict[str, str]) -> list[_T]:
    """Await one upstream call; on failure record the reason under `name` and return []."""
    try:
        return await call
    except (UpstreamError, GdeltError) as exc:
        errors[name] = str(exc)
        return []


async def compute(params: dict) -> dict:
    """Fetch GDELT (and Context.dev if configured), then build the sentiment read."""
    query = parse_query_param(params)
    gdelt_query = gdelt.build_query(search_terms_for(query))
    errors: dict[str, str] = {}
    articles = await _fetch_or_error("gdelt_artlist", gdelt.fetch_articles(gdelt_query), errors)
    tone_points = await _fetch_or_error("gdelt_timelinetone", gdelt.fetch_tone_timeline(gdelt_query), errors)
    search_results: list[SearchResult] = []
    api_key = get_settings().context_dev_api_key
    if api_key:
        search_results = await _fetch_or_error("context_dev", contextdev.search_news(query, api_key), errors)
    if not articles and not tone_points and not search_results and errors:
        raise UpstreamError(PROVIDER_GDELT, f"no news source answered: {errors}")
    return build_response(
        query, gdelt_query, articles, tone_points, search_results, errors, datetime.now(timezone.utc)
    )
