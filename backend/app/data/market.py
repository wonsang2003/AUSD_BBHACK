"""
Market strip for the dashboard (`GET /api/market`): free, clearly labeled quotes only.

Entry point: `await market_strip(watchlist)` → JSON rows of the `/api/market` shape
`{symbol, price, change_24h_pct, source, delayed}` (`MarketQuote` from `app/data/coingecko.py`).
Three free sources, each labeled:

    crypto   1. CoinGecko keyless simple/price          source "coingecko"            delayed False
             2. Hyperliquid perp book mid (fallback)    source "hyperliquid perp mid"  delayed False
                — used for any crypto symbol CoinGecko did not return (its keyless API is
                  often rate-limited), from the same cached metaAndAssetCtxs request the
                  perps_crowding seller uses; 24 h change = mid / prevDayPx − 1.
    equities 3. yfinance last price vs previous close   source "yfinance (15m+ delayed)" delayed True
                — unofficial Yahoo client, quotes delayed 15 minutes or more.

A symbol is crypto when CoinGecko's symbol map knows it (BTC, ETH, SOL, ...); anything
else is treated as an equity ticker. A failing source never fails the strip: its rows
are left out and the reason is kept in `MarketStrip.errors`.

The merge and row-building functions are pure and tested with fixtures; the fetches are
thin wrappers. Nothing here is paid for — PocketDesk only pays for research (x402).
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any

from app.data import coingecko, hyperliquid
from app.data.coingecko import SYMBOL_TO_COINGECKO_ID, MarketQuote
from app.data.http import UpstreamError
from app.data.hyperliquid import HyperliquidParseError, PerpContext

# ─── constants ────────────────────────────────────────────────────────────────

DEFAULT_STRIP_SYMBOLS = ("BTC", "ETH", "SOL", "SPY", "QQQ", "NVDA")
MAX_STRIP_SYMBOLS = 20

SOURCE_COINGECKO = coingecko.SOURCE_NAME
SOURCE_HYPERLIQUID = "hyperliquid perp mid"
SOURCE_YFINANCE = "yfinance (15m+ delayed)"   # unofficial Yahoo client, same delay note as app/data/yfin.py

EQUITY_TIMEOUT_SECONDS = 15.0
EQUITY_CACHE_TTL_SECONDS = 60.0
PERCENT = 100.0

_equity_cache: dict[str, tuple[float, dict[str, float | None]]] = {}
_equity_lock = asyncio.Lock()


@dataclass
class MarketStrip:
    """Quotes in the order asked for, plus a reason for each source that failed."""

    rows: list[MarketQuote] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def to_rows(self) -> list[dict]:
        """The `/api/market` JSON rows."""
        return [quote.to_dict() for quote in self.rows]


# ─── pure helpers ────────────────────────────────────────────────────────────

def normalize_symbols(symbols: list[str] | None) -> list[str]:
    """Upper-case, strip, de-duplicate (order kept), cap at 20; empty → the default strip."""
    cleaned: list[str] = []
    for symbol in symbols or []:
        upper = symbol.strip().upper()
        if upper and upper not in cleaned:
            cleaned.append(upper)
    return cleaned[:MAX_STRIP_SYMBOLS] or list(DEFAULT_STRIP_SYMBOLS)


def is_crypto_symbol(symbol: str) -> bool:
    """Crypto = a symbol with a known CoinGecko id; everything else is an equity ticker."""
    return symbol.upper() in SYMBOL_TO_COINGECKO_ID


def split_symbols(symbols: list[str]) -> tuple[list[str], list[str]]:
    """(crypto symbols, equity tickers), each in the original order."""
    crypto = [symbol for symbol in symbols if is_crypto_symbol(symbol)]
    equities = [symbol for symbol in symbols if not is_crypto_symbol(symbol)]
    return crypto, equities


def hyperliquid_quote(symbol: str, context: PerpContext) -> MarketQuote:
    """A strip row from a Hyperliquid perp: book mid (else mark) and its 24 h change."""
    return MarketQuote(
        symbol=symbol.upper(),
        price=context.display_px,
        change_24h_pct=context.change_24h_pct,
        source=SOURCE_HYPERLIQUID,
        delayed=False,
    )


def hyperliquid_quotes(symbols: list[str], contexts: list[PerpContext]) -> list[MarketQuote]:
    """Rows for the symbols that are listed Hyperliquid perps (others are left out)."""
    by_symbol = hyperliquid.index_by_symbol(contexts)
    return [hyperliquid_quote(symbol, by_symbol[symbol.upper()]) for symbol in symbols if symbol.upper() in by_symbol]


def _finite_positive(value: Any) -> float | None:
    """A usable price: a finite number > 0, else None (yfinance gives NaN for missing data)."""
    if value is None:
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number) or number <= 0:
        return None
    return number


def equity_quote(symbol: str, last_price: Any, previous_close: Any) -> MarketQuote | None:
    """
    A delayed equity row: change_24h_pct = (last / previous close − 1) · 100.
    None when there is no usable last price; change is None without a previous close.
    """
    last = _finite_positive(last_price)
    if last is None:
        return None
    previous = _finite_positive(previous_close)
    change = (last / previous - 1.0) * PERCENT if previous else None
    return MarketQuote(symbol=symbol.upper(), price=last, change_24h_pct=change, source=SOURCE_YFINANCE, delayed=True)


def merge_strip(
    symbols: list[str],
    coingecko_rows: list[MarketQuote],
    hyperliquid_rows: list[MarketQuote],
    equity_rows: list[MarketQuote],
) -> list[MarketQuote]:
    """
    One row per symbol, in the order asked for: crypto prefers CoinGecko and falls back to
    Hyperliquid; equities come from yfinance. Symbols no source could price are left out.
    """
    crypto_primary = {row.symbol: row for row in coingecko_rows}
    crypto_fallback = {row.symbol: row for row in hyperliquid_rows}
    equities = {row.symbol: row for row in equity_rows}
    merged: list[MarketQuote] = []
    for symbol in symbols:
        upper = symbol.upper()
        if is_crypto_symbol(upper):
            row = crypto_primary.get(upper) or crypto_fallback.get(upper)
        else:
            row = equities.get(upper)
        if row is not None:
            merged.append(row)
    return merged


# ─── fetches (each returns [] and records an error on failure) ──────────────

async def _coingecko_rows(symbols: list[str], errors: dict[str, str]) -> list[MarketQuote]:
    """CoinGecko spot rows; failure → [] and errors["coingecko"]."""
    if not symbols:
        return []
    try:
        return await coingecko.fetch_market_strip(symbols)
    except (UpstreamError, ValueError) as exc:
        errors[SOURCE_COINGECKO] = str(exc)
        return []


async def _hyperliquid_rows(symbols: list[str], errors: dict[str, str]) -> list[MarketQuote]:
    """Hyperliquid fallback rows for `symbols`; failure → [] and errors["hyperliquid"]."""
    if not symbols:
        return []
    try:
        contexts = await hyperliquid.fetch_perp_contexts()
    except (UpstreamError, HyperliquidParseError, ValueError) as exc:
        errors[hyperliquid.SOURCE_NAME] = str(exc)
        return []
    return hyperliquid_quotes(symbols, contexts)


def _download_equity_prices(tickers: list[str]) -> dict[str, dict[str, float | None]]:
    """Blocking (runs in a thread): yfinance `fast_info` last price + previous close per ticker."""
    import yfinance  # imported here so importing this module stays cheap

    prices: dict[str, dict[str, float | None]] = {}
    for ticker in tickers:
        info = yfinance.Ticker(ticker).fast_info
        prices[ticker] = {"last_price": info.last_price, "previous_close": info.previous_close}
    return prices


async def fetch_equity_prices(tickers: list[str]) -> dict[str, dict[str, float | None]]:
    """
    {ticker: {"last_price", "previous_close"}} from yfinance, cached 60 s per ticker,
    one yfinance download at a time, with a timeout. Raises UpstreamError on any failure.
    """
    now = time.monotonic()
    missing = [ticker for ticker in tickers if _equity_cache.get(ticker, (0.0, {}))[0] <= now]
    if missing:
        async with _equity_lock:
            try:
                fresh = await asyncio.wait_for(
                    asyncio.to_thread(_download_equity_prices, missing), timeout=EQUITY_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError as exc:
                raise UpstreamError("yfinance", f"timed out after {EQUITY_TIMEOUT_SECONDS:.0f} s") from exc
            except Exception as exc:  # yfinance raises many exception types; all mean "no data"
                raise UpstreamError("yfinance", f"{type(exc).__name__}: {exc}") from exc
        for ticker, prices in fresh.items():
            _equity_cache[ticker] = (time.monotonic() + EQUITY_CACHE_TTL_SECONDS, prices)
    return {ticker: _equity_cache[ticker][1] for ticker in tickers if ticker in _equity_cache}


async def _equity_rows(tickers: list[str], errors: dict[str, str]) -> list[MarketQuote]:
    """Delayed equity rows; failure → [] and errors["yfinance"]."""
    if not tickers:
        return []
    try:
        prices = await fetch_equity_prices(tickers)
    except UpstreamError as exc:
        errors["yfinance"] = str(exc)
        return []
    rows: list[MarketQuote] = []
    for ticker in tickers:
        row = prices.get(ticker)
        quote = equity_quote(ticker, row.get("last_price"), row.get("previous_close")) if row else None
        if quote is not None:
            rows.append(quote)
    return rows


def clear_cache() -> None:
    """Forget cached equity prices (used by tests)."""
    _equity_cache.clear()


async def build_market_strip(symbols: list[str] | None = None) -> MarketStrip:
    """
    The labeled market strip for `symbols` (default BTC, ETH, SOL, SPY, QQQ, NVDA).
    Hyperliquid is asked only for crypto symbols CoinGecko did not price.
    """
    wanted = normalize_symbols(symbols)
    crypto, equities = split_symbols(wanted)
    errors: dict[str, str] = {}
    coingecko_rows, equity_rows = await asyncio.gather(
        _coingecko_rows(crypto, errors), _equity_rows(equities, errors)
    )
    priced = {row.symbol for row in coingecko_rows}
    unpriced = [symbol for symbol in crypto if symbol not in priced]
    hyperliquid_rows = await _hyperliquid_rows(unpriced, errors)
    return MarketStrip(rows=merge_strip(wanted, coingecko_rows, hyperliquid_rows, equity_rows), errors=errors)


async def market_strip(watchlist: list[str] | None = None) -> list[dict]:
    """
    `GET /api/market` rows `[{symbol, price, change_24h_pct, source, delayed}]` for `watchlist`.
    Never raises for an upstream failure: a failing source only leaves its rows out.
    """
    strip = await build_market_strip(watchlist)
    return strip.to_rows()
