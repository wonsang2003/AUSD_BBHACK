"""
Price history for the LIVE UPDATE popup's sparkline: `GET /api/market/history`.

Sources (free, key-less), first one that answers wins:

    crypto (BTC, ETH, SOL, ...)
        1. Hyperliquid  POST https://api.hyperliquid.xyz/info
                        {"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "5m",
                                                           "startTime": ms, "endTime": ms}}
                        → [{"t": open_ms, "T": close_ms, "c": "64250.5", ...}, ...]   (close prices)
        2. CoinGecko    GET /api/v3/coins/{id}/market_chart?vs_currency=usd&days=1
                        → {"prices": [[ms, price], ...], ...}
    equities (NVDA, COIN, ...)
        3. yfinance     Ticker(symbol).history(period="1d", interval="5m") in a worker thread
                        (unofficial Yahoo client, 15 min+ delayed → delayed=True)

The parse functions are pure (tested offline with fixture payloads in tests/test_history.py);
`fetch_history` never raises: any failure answers `{points: [], error}` so the route is always 200.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from app.data.coingecko import SYMBOL_TO_COINGECKO_ID
from app.data.http import get_json, post_json

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
COINGECKO_MARKET_CHART_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
CANDLE_INTERVAL = "5m"
HISTORY_CACHE_TTL_SECONDS = 60.0
YFINANCE_TIMEOUT_SECONDS = 20.0
DEFAULT_MINUTES = 240
MAX_MINUTES = 24 * 60
MS_PER_SECOND = 1000

SOURCE_HYPERLIQUID = "hyperliquid"
SOURCE_COINGECKO = "coingecko"
SOURCE_YFINANCE = "yfinance (15m+ delayed)"

# Hyperliquid-listed crypto beyond the CoinGecko map (perp names are plain tickers).
EXTRA_CRYPTO = {"ARB", "OP", "SUI", "APT", "TIA", "INJ", "SEI", "PEPE", "WIF", "LTC", "BCH", "ADA", "DOT", "NEAR"}
QUOTE_SUFFIXES = ("-USD", "/USD", "USDT", "-PERP")

_equity_cache: dict[str, tuple[float, list[dict[str, float]]]] = {}
_equity_lock = asyncio.Lock()


# ─── pure helpers ────────────────────────────────────────────────────────────

def normalize_symbol(symbol: str) -> str:
    """'btc-usd' → 'BTC', ' nvda ' → 'NVDA'."""
    upper = (symbol or "").strip().upper()
    for suffix in QUOTE_SUFFIXES:
        if upper.endswith(suffix) and len(upper) > len(suffix):
            upper = upper[: -len(suffix)]
    return upper


def is_crypto(symbol: str) -> bool:
    return symbol in SYMBOL_TO_COINGECKO_ID or symbol in EXTRA_CRYPTO


def _price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _sorted_unique(points: list[dict[str, float]]) -> list[dict[str, float]]:
    by_time = {int(point["t"]): point for point in points}
    return [by_time[t] for t in sorted(by_time)]


def trim_window(points: list[dict[str, float]], minutes: int, now_s: float) -> list[dict[str, float]]:
    """Keep the points of the last `minutes` (all of them if that would leave fewer than 2)."""
    cutoff = now_s - minutes * 60
    kept = [point for point in points if point["t"] >= cutoff]
    return kept if len(kept) >= 2 else points


def parse_hyperliquid_candles(payload: Any) -> list[dict[str, float]]:
    """candleSnapshot list → [{t: close_s, p: close}] sorted by time; bad rows are skipped."""
    if not isinstance(payload, list):
        raise ValueError("candleSnapshot: expected a list")
    points: list[dict[str, float]] = []
    for candle in payload:
        if not isinstance(candle, dict):
            continue
        price = _price(candle.get("c"))
        stamp = candle.get("T", candle.get("t"))
        if price is None or not isinstance(stamp, (int, float)):
            continue
        points.append({"t": int(stamp) // MS_PER_SECOND, "p": price})
    return _sorted_unique(points)


def parse_coingecko_market_chart(payload: Any) -> list[dict[str, float]]:
    """market_chart {"prices": [[ms, price], ...]} → [{t, p}] sorted by time."""
    if not isinstance(payload, dict) or not isinstance(payload.get("prices"), list):
        raise ValueError("market_chart: expected {prices: [[ms, price], ...]}")
    points: list[dict[str, float]] = []
    for row in payload["prices"]:
        if not isinstance(row, (list, tuple)) or len(row) < 2 or not isinstance(row[0], (int, float)):
            continue
        price = _price(row[1])
        if price is not None:
            points.append({"t": int(row[0]) // MS_PER_SECOND, "p": price})
    return _sorted_unique(points)


def parse_yfinance_rows(rows: list[tuple[float, Any]]) -> list[dict[str, float]]:
    """[(unix_s, close)] (from a yfinance DataFrame) → [{t, p}], NaN closes dropped."""
    points = [{"t": int(stamp), "p": price} for stamp, close in rows if (price := _price(close)) is not None]
    return _sorted_unique(points)


def result(symbol: str, source: str | None, delayed: bool, points: list[dict[str, float]],
           error: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"symbol": symbol, "source": source, "delayed": delayed, "points": points}
    if error:
        body["error"] = error
    return body


# ─── fetchers ────────────────────────────────────────────────────────────────

async def _hyperliquid(symbol: str, minutes: int, now_s: float) -> list[dict[str, float]]:
    end_ms = int(now_s * MS_PER_SECOND)
    end_ms -= end_ms % 60_000  # round to the minute so bursts of popups share one cached request
    body = {"type": "candleSnapshot", "req": {"coin": symbol, "interval": CANDLE_INTERVAL,
                                              "startTime": end_ms - minutes * 60 * MS_PER_SECOND,
                                              "endTime": end_ms}}
    payload = await post_json(HYPERLIQUID_INFO_URL, body, source="hyperliquid", ttl=HISTORY_CACHE_TTL_SECONDS)
    return parse_hyperliquid_candles(payload)


async def _coingecko(symbol: str) -> list[dict[str, float]]:
    coin_id = SYMBOL_TO_COINGECKO_ID.get(symbol)
    if coin_id is None:
        raise ValueError(f"no CoinGecko id for {symbol}")
    payload = await get_json(COINGECKO_MARKET_CHART_URL.format(coin_id=coin_id), source="coingecko",
                             params={"vs_currency": "usd", "days": "1"}, ttl=HISTORY_CACHE_TTL_SECONDS)
    return parse_coingecko_market_chart(payload)


def _download_yfinance(symbol: str) -> list[tuple[float, Any]]:
    """Blocking (worker thread): 1 day of 5-minute closes as [(unix_s, close)]."""
    import yfinance  # imported lazily: heavy

    frame = yfinance.Ticker(symbol).history(period="1d", interval="5m")
    if frame is None or frame.empty:
        frame = yfinance.Ticker(symbol).history(period="5d", interval="15m")
    if frame is None or frame.empty:
        return []
    return [(index.timestamp(), close) for index, close in zip(frame.index, frame["Close"], strict=False)]


async def _yfinance(symbol: str) -> list[dict[str, float]]:
    cached = _equity_cache.get(symbol)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    async with _equity_lock:
        rows = await asyncio.wait_for(asyncio.to_thread(_download_yfinance, symbol),
                                      timeout=YFINANCE_TIMEOUT_SECONDS)
    points = parse_yfinance_rows(rows)
    _equity_cache[symbol] = (time.monotonic() + HISTORY_CACHE_TTL_SECONDS, points)
    return points


async def fetch_history(symbol: str, minutes: int = DEFAULT_MINUTES, now_s: float | None = None) -> dict[str, Any]:
    """`{symbol, source, delayed, points: [{t, p}]}`; never raises (failure → points [] + error)."""
    clean = normalize_symbol(symbol)
    minutes = max(5, min(int(minutes), MAX_MINUTES))
    now = time.time() if now_s is None else now_s
    if not clean or not all(ch.isalnum() or ch in ".-^=" for ch in clean) or len(clean) > 15:
        return result(clean, None, False, [], f"invalid symbol {symbol!r}")
    errors: list[str] = []
    if is_crypto(clean):
        for source, fetch in ((SOURCE_HYPERLIQUID, lambda: _hyperliquid(clean, minutes, now)),
                              (SOURCE_COINGECKO, lambda: _coingecko(clean))):
            try:
                points = trim_window(await fetch(), minutes, now)
            except Exception as exc:  # degrade: try the next source
                errors.append(f"{source}: {type(exc).__name__}: {exc}"[:200])
                continue
            if points:
                return result(clean, source, False, points)
            errors.append(f"{source}: no points")
        return result(clean, None, False, [], "; ".join(errors))
    try:
        points = trim_window(await _yfinance(clean), minutes, now)
    except Exception as exc:  # yfinance raises many exception types; all mean "no data"
        return result(clean, SOURCE_YFINANCE, True, [], f"yfinance: {type(exc).__name__}: {exc}"[:200])
    if not points:
        return result(clean, SOURCE_YFINANCE, True, [], "yfinance: no points")
    return result(clean, SOURCE_YFINANCE, True, points)
