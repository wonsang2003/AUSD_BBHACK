"""
Shared HTTP helper for every upstream data adapter (Deribit, Gemini, Hyperliquid,
GDELT, FRED, EDGAR, CoinGecko, Context.dev ...).

It gives every adapter the same four protections, so individual adapters stay short:

    1. a timeout on every request,
    2. retries with exponential backoff on network errors / 429 / 5xx,
    3. a small in-process TTL cache (same URL + params → same answer for `ttl` seconds),
    4. a per-source minimum interval between requests (simple rate limit).

Usage:
    data = await get_json("https://www.deribit.com/api/v2/public/get_index_price",
                          params={"index_name": "btc_usd"}, source="deribit", ttl=60)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Minimum seconds between two requests to the same source (≈ rate limit).
MIN_INTERVAL_SECONDS: dict[str, float] = {
    "deribit": 0.1,
    "gemini": 0.5,       # public API ~120 req/min
    "hyperliquid": 0.2,
    "gdelt": 1.0,
    "fred": 0.5,
    "edgar": 0.12,       # SEC asks for <= 10 req/s
    "coingecko": 2.0,
    "contextdev": 1.0,   # free tier: 1 concurrent request
    "yfinance": 0.5,
}

_cache: dict[str, tuple[float, Any]] = {}
_last_request_at: dict[str, float] = {}
_source_locks: dict[str, asyncio.Lock] = {}


class UpstreamError(Exception):
    """An upstream data source failed after all retries."""

    def __init__(self, source: str, message: str):
        super().__init__(f"[{source}] {message}")
        self.source = source


def _cache_key(method: str, url: str, params: dict | None, body: Any) -> str:
    return json.dumps([method, url, params or {}, body], sort_keys=True, default=str)


def clear_cache() -> None:
    """Forget all cached responses (used by tests)."""
    _cache.clear()


async def _respect_rate_limit(source: str) -> None:
    lock = _source_locks.setdefault(source, asyncio.Lock())
    async with lock:
        min_interval = MIN_INTERVAL_SECONDS.get(source, 0.0)
        wait = _last_request_at.get(source, 0.0) + min_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at[source] = time.monotonic()


async def request(
    method: str,
    url: str,
    *,
    source: str,
    params: dict | None = None,
    json_body: Any = None,
    headers: dict | None = None,
    ttl: float = 60.0,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    parse: str = "json",  # "json" or "text"
) -> Any:
    """Send one HTTP request with caching, rate limiting and retries. Returns parsed JSON or text."""
    key = _cache_key(method, url, params, json_body)
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    last_error = "no attempt made"
    for attempt in range(retries):
        await _respect_rate_limit(source)
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                response = await client.request(method, url, params=params, json=json_body)
            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = f"HTTP {response.status_code}"
            elif response.status_code >= 400:
                raise UpstreamError(source, f"HTTP {response.status_code}: {response.text[:200]}")
            else:
                value = response.json() if parse == "json" else response.text
                if ttl > 0:
                    _cache[key] = (time.monotonic() + ttl, value)
                return value
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(BACKOFF_BASE_SECONDS * (2**attempt))

    raise UpstreamError(source, f"failed after {retries} attempts ({last_error})")


async def get_json(url: str, *, source: str, params: dict | None = None, headers: dict | None = None, ttl: float = 60.0) -> Any:
    return await request("GET", url, source=source, params=params, headers=headers, ttl=ttl)


async def post_json(url: str, body: Any, *, source: str, headers: dict | None = None, ttl: float = 30.0) -> Any:
    return await request("POST", url, source=source, json_body=body, headers=headers, ttl=ttl)


async def get_text(url: str, *, source: str, params: dict | None = None, headers: dict | None = None, ttl: float = 300.0) -> str:
    return await request("GET", url, source=source, params=params, headers=headers, ttl=ttl, parse="text")
