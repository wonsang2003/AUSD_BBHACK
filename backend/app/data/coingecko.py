"""
CoinGecko adapter: free public spot prices for the dashboard's market strip.

This is a FREE input (no x402, no key) used by `GET /api/market` — it is not a seller:

    GET https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true
        → {"bitcoin": {"usd": 64250.12, "usd_24h_change": -1.53}, "ethereum": {...}}

CoinGecko keys coins by id ("bitcoin"), not by ticker ("BTC"), so we keep a small map
for the symbols PocketDesk watches. The keyless public API is rate-limited (the
shared HTTP helper spaces "coingecko" requests ≥ 2 s apart) and its prices are
cached upstream, so quotes can be a little stale.

The paid, per-call CoinGecko x402 endpoint (mainnet USDC) is a different thing: PocketDesk
never pays it (buyer-side testnet only, SPEC §9). It appears in the catalog as an
"external x402 (flagged)" row, and `app/x402/probe.py` only sends it UNPAID requests to
show its live 402 price. `MarketQuote` here is also the row type `app/data/market.py`
uses for Hyperliquid and yfinance rows, so the whole strip has one shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.data.http import get_json

COINGECKO_SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
SOURCE_NAME = "coingecko"
QUOTE_CURRENCY = "usd"
CHANGE_FIELD = f"{QUOTE_CURRENCY}_24h_change"
MARKET_CACHE_TTL_SECONDS = 60.0

# Ticker → CoinGecko coin id, for the crypto symbols the planner treats as crypto.
SYMBOL_TO_COINGECKO_ID: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    "HYPE": "hyperliquid",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "BNB": "binancecoin",
}
DEFAULT_MARKET_STRIP_SYMBOLS = ("BTC", "ETH", "SOL")


@dataclass(frozen=True)
class MarketQuote:
    """One row of the market strip (matches the `/api/market` row shape)."""

    symbol: str
    price: float
    change_24h_pct: float | None
    source: str = SOURCE_NAME
    delayed: bool = False

    def to_dict(self) -> dict:
        """JSON shape used by `/api/market`: {symbol, price, change_24h_pct, source, delayed}."""
        return {
            "symbol": self.symbol,
            "price": self.price,
            "change_24h_pct": self.change_24h_pct,
            "source": self.source,
            "delayed": self.delayed,
        }


def coingecko_ids_for(symbols: list[str]) -> list[str]:
    """CoinGecko ids for the symbols we know; unknown symbols are dropped."""
    return [SYMBOL_TO_COINGECKO_ID[s.upper()] for s in symbols if s.upper() in SYMBOL_TO_COINGECKO_ID]


def build_simple_price_params(symbols: list[str]) -> dict[str, str]:
    """Query parameters for one simple/price call covering all `symbols`."""
    return {
        "ids": ",".join(coingecko_ids_for(symbols)),
        "vs_currencies": QUOTE_CURRENCY,
        "include_24hr_change": "true",
    }


def parse_simple_price(payload: dict, symbols: list[str]) -> list[MarketQuote]:
    """
    Turn the simple/price response into MarketQuotes, in the order of `symbols`.
    Symbols that CoinGecko did not return (or we cannot map) are left out.
    """
    quotes: list[MarketQuote] = []
    for symbol in symbols:
        coin_id = SYMBOL_TO_COINGECKO_ID.get(symbol.upper())
        row = payload.get(coin_id) if coin_id else None
        if not row or QUOTE_CURRENCY not in row:
            continue
        change = row.get(CHANGE_FIELD)
        quotes.append(
            MarketQuote(
                symbol=symbol.upper(),
                price=float(row[QUOTE_CURRENCY]),
                change_24h_pct=float(change) if change is not None else None,
            )
        )
    return quotes


async def fetch_market_strip(symbols: list[str] | None = None) -> list[MarketQuote]:
    """Spot USD prices and 24 h change for `symbols` (default BTC, ETH, SOL) in one request."""
    wanted = list(symbols) if symbols else list(DEFAULT_MARKET_STRIP_SYMBOLS)
    if not coingecko_ids_for(wanted):
        return []  # no symbol CoinGecko knows: nothing to ask for
    payload = await get_json(
        COINGECKO_SIMPLE_PRICE_URL,
        source=SOURCE_NAME,
        params=build_simple_price_params(wanted),
        ttl=MARKET_CACHE_TTL_SECONDS,
    )
    return parse_simple_price(payload, wanted)
