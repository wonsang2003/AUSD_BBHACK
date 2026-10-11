"""
Gemini Predictions adapter: public prediction-market events and contracts for the
`pm_edge` seller (SPEC §4 item 2). The endpoints are public, so no API key is needed.

Flow:

    fetch_events_payload("crypto")   GET /v1/prediction-markets/events?status=active&category=crypto
    fetch_event_payload(ticker)      GET /v1/prediction-markets/events/{eventTicker}
                    ▼                (async, network, through app/data/http.py)
    parse_events(list_payload)       → [event dict]                       pure, tested with fixtures
    parse_contracts(event)           → [PredictionContract]
    parse_price_threshold(contract)  → PriceThreshold   "BTC finishes above $K at <expiry>"
    yes_quote(contract)              → YesQuote         market P(YES) and the prices to trade at

API facts (developer.gemini.com → Prediction Markets → Events):
    * List Events returns {"data": [Event], "pagination": {limit, offset, total}};
      Get Event returns one bare Event (not wrapped in "data").
    * Event: ticker, title, category, status, expiryDate, contracts[].
    * Contract: ticker, label (e.g. "SOL > $90"), instrumentSymbol (e.g. "GEMI-..."), status,
      marketState ("open"/"closed"), expiryDate, strike {value: "87500.00", type: "above" |
      "reference" | ...}, prices {buy: {yes, no}, sell: {yes, no}, bestBid, bestAsk,
      lastTradePrice}. ALL PRICES ARE DECIMAL STRINGS (or null) in YES space (0–1), so they
      are parsed with Decimal, never float.
    * `instrumentSymbol` is used exactly as returned; we never build one from a title or ticker.
    * Strike type "above" is a Higher/Lower threshold judged at expiry ("finishes above K");
      "reference" is a short-horizon Up/Down market whose strike is fixed later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from app.data.http import get_json

# ─── constants ────────────────────────────────────────────────────────────────

GEMINI_API_BASE = "https://api.gemini.com/v1/prediction-markets"
GEMINI_SOURCE = "gemini"            # cache / rate-limit bucket in app/data/http.py
GEMINI_CACHE_TTL_SECONDS = 30.0     # prediction-market quotes move faster than option marks
EVENTS_PAGE_LIMIT = 200             # the API allows up to 500 per page

STATUS_ACTIVE = "active"
MARKET_STATE_OPEN = "open"
STRIKE_TYPE_ABOVE = "above"

ONE = Decimal("1")
THOUSAND = Decimal("1000")

# Crypto symbols we can recognise in a contract label or event title, with their names.
ASSET_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("BTC", "BITCOIN"),
    "ETH": ("ETH", "ETHEREUM", "ETHER"),
    "SOL": ("SOL", "SOLANA"),
    "XRP": ("XRP", "RIPPLE"),
    "DOGE": ("DOGE", "DOGECOIN"),
}

# "BTC > $110,000", "ETH above $4,200.50", "BTC over $110k"  → amount 110,000 / 4,200.50 / 110k
LABEL_STRIKE_PATTERN = re.compile(
    r"(?:>|\babove\b|\bover\b)\s*\$\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?P<thousands>k\b)?",
    re.IGNORECASE,
)

# Words that make a market path-dependent ("will BTC REACH $150k by ...?"): N(d2) prices a
# finish above K at expiry, not a touch at any time before it, so these are skipped.
TOUCH_PATTERN = re.compile(r"\b(?:reach|reaches|hit|hits|touch|touches)\b", re.IGNORECASE)

# Where the YES market probability came from (reported to buyers as `market_p_basis`).
BASIS_MID = "mid"                 # (bestBid + bestAsk) / 2
BASIS_LAST_TRADE = "last_trade"   # no two-sided quote: last traded price
BASIS_ONE_SIDED = "one_sided"     # only a bid or only an ask


class UnsupportedContract(ValueError):
    """This contract cannot be compared with option prices; the message says why."""


# ─── parsing helpers ─────────────────────────────────────────────────────────

def parse_decimal(value: object) -> Decimal | None:
    """A Gemini price/strike string such as "0.65" → Decimal("0.65"); null or junk → None."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def parse_timestamp(value: str | None) -> datetime | None:
    """ISO-8601 such as "2026-10-16T21:00:00.000Z" → aware UTC datetime; null → None."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ─── events and contracts ────────────────────────────────────────────────────

@dataclass(frozen=True)
class PredictionContract:
    """One YES-space contract of a Gemini event, with its prices as exact Decimals."""

    event_ticker: str
    event_title: str
    ticker: str
    label: str
    instrument_symbol: str        # exactly as returned by the API
    status: str
    market_state: str | None      # "open" / "closed"; None if the API omitted it
    expiry: datetime | None
    best_bid: Decimal | None      # YES best bid
    best_ask: Decimal | None      # YES best ask
    last_trade_price: Decimal | None
    strike_value: Decimal | None
    strike_type: str | None


def parse_events(list_payload: dict) -> list[dict]:
    """The event objects of a List Events payload (`{"data": [...], "pagination": ...}`)."""
    events = list_payload.get("data")
    if not isinstance(events, list):
        raise ValueError("Gemini events payload has no `data` list")
    return events


def _parse_contract(event: dict, contract: dict) -> PredictionContract:
    """Map one raw contract dict (plus its event) to a PredictionContract."""
    prices = contract.get("prices") or {}
    strike = contract.get("strike") or {}
    return PredictionContract(
        event_ticker=event.get("ticker", ""),
        event_title=event.get("title", ""),
        ticker=contract.get("ticker", ""),
        label=contract.get("label", ""),
        instrument_symbol=contract.get("instrumentSymbol", ""),
        status=contract.get("status", ""),
        market_state=contract.get("marketState"),
        expiry=parse_timestamp(contract.get("expiryDate") or event.get("expiryDate")),
        best_bid=parse_decimal(prices.get("bestBid")),
        best_ask=parse_decimal(prices.get("bestAsk")),
        last_trade_price=parse_decimal(prices.get("lastTradePrice")),
        strike_value=parse_decimal(strike.get("value")),
        strike_type=strike.get("type"),
    )


def parse_contracts(event: dict) -> list[PredictionContract]:
    """All contracts of one event (from List Events or Get Event, same shape)."""
    return [_parse_contract(event, contract) for contract in event.get("contracts") or []]


def is_tradable(contract: PredictionContract) -> bool:
    """Active, open for trading (if the API says), and has an instrument symbol to trade."""
    is_open = contract.market_state is None or contract.market_state == MARKET_STATE_OPEN
    return contract.status == STATUS_ACTIVE and is_open and bool(contract.instrument_symbol)


# ─── "ASSET finishes above $K at expiry" ─────────────────────────────────────

@dataclass(frozen=True)
class PriceThreshold:
    """The proposition of a Higher/Lower crypto contract: asset > strike at expiry."""

    asset: str
    strike: float
    expiry: datetime


def detect_asset(text: str) -> str | None:
    """The first crypto symbol named in `text` as a whole word ("BTC", "Bitcoin", ...), else None."""
    for symbol, aliases in ASSET_ALIASES.items():
        for alias in aliases:
            if re.search(rf"\b{alias}\b", text, re.IGNORECASE):
                return symbol
    return None


def parse_label_strike(label: str) -> Decimal | None:
    """Strike from a label like "BTC > $110,000" or "above $110k" (k = × 1 000); None if absent."""
    match = LABEL_STRIKE_PATTERN.search(label)
    if match is None:
        return None
    amount = Decimal(match["amount"].replace(",", ""))
    if match["thousands"]:
        amount = amount * THOUSAND
    return amount


def _threshold_strike(contract: PredictionContract) -> Decimal:
    """
    The strike K of a finish-above contract, or UnsupportedContract with the reason.

    Rule: trust the API's `strike` object when it says type "above"; reject any other
    strike type (e.g. "reference" Up/Down markets); without a strike object, read
    "> $K" / "above $K" from the label unless the wording is a touch ("reach", "hit").
    """
    if contract.strike_type is not None:
        if contract.strike_type != STRIKE_TYPE_ABOVE:
            raise UnsupportedContract(f"strike type {contract.strike_type!r} is not a finish-above threshold")
        if contract.strike_value is None:
            raise UnsupportedContract("strike value not published yet")
        return contract.strike_value

    wording = f"{contract.event_title} {contract.label}"
    if TOUCH_PATTERN.search(wording):
        raise UnsupportedContract("path-dependent (touch) market: N(d2) prices a finish above K, not a touch")
    label_strike = parse_label_strike(contract.label)
    if label_strike is None:
        raise UnsupportedContract("no 'above $K' strike in the contract")
    return label_strike


def parse_price_threshold(contract: PredictionContract) -> PriceThreshold:
    """
    Read "ASSET finishes above $K at expiry T" from a contract (SPEC: "BTC above $X on <date>").

    Asset comes from the label, else the event title; strike from `_threshold_strike`;
    expiry from the contract's (else the event's) expiryDate.
    """
    asset = detect_asset(contract.label) or detect_asset(contract.event_title)
    if asset is None:
        raise UnsupportedContract("no known crypto asset named in the contract")
    strike = _threshold_strike(contract)
    if strike <= 0:
        raise UnsupportedContract(f"strike must be positive, got {strike}")
    if contract.expiry is None:
        raise UnsupportedContract("contract has no expiryDate")
    return PriceThreshold(asset=asset, strike=float(strike), expiry=contract.expiry)


# ─── prices ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class YesQuote:
    """Market view of one contract: P(YES) and the prices a taker would pay now."""

    probability: Decimal        # market-implied P(YES), in [0, 1]
    basis: str                  # BASIS_MID, BASIS_LAST_TRADE or BASIS_ONE_SIDED
    yes_ask: Decimal | None     # price to BUY YES now
    no_ask: Decimal | None      # price to BUY NO now = 1 − YES best bid
    spread: Decimal | None      # YES best ask − best bid


def yes_quote(contract: PredictionContract) -> YesQuote:
    """
    Market probability of YES and the taker prices.

    P(YES) = (bestBid + bestAsk) / 2 when both sides are quoted, else lastTradePrice, else
    whichever single side exists. In YES space, buying NO is selling YES at the bid, so the
    price to buy NO is 1 − bestBid.
    """
    bid, ask = contract.best_bid, contract.best_ask
    no_ask = ONE - bid if bid is not None else None
    if bid is not None and ask is not None:
        return YesQuote(probability=(bid + ask) / 2, basis=BASIS_MID, yes_ask=ask, no_ask=no_ask, spread=ask - bid)
    if contract.last_trade_price is not None:
        return YesQuote(
            probability=contract.last_trade_price, basis=BASIS_LAST_TRADE, yes_ask=ask, no_ask=no_ask, spread=None
        )
    one_side = bid if bid is not None else ask
    if one_side is None:
        raise UnsupportedContract("no bid, ask or last trade price")
    return YesQuote(probability=one_side, basis=BASIS_ONE_SIDED, yes_ask=ask, no_ask=no_ask, spread=None)


# ─── network (async) ─────────────────────────────────────────────────────────

async def fetch_events_payload(category: str, limit: int = EVENTS_PAGE_LIMIT) -> dict:
    """Raw List Events payload: active events of one category (e.g. "crypto"), first page."""
    return await get_json(
        f"{GEMINI_API_BASE}/events",
        params={"status": STATUS_ACTIVE, "category": category, "limit": limit},
        source=GEMINI_SOURCE,
        ttl=GEMINI_CACHE_TTL_SECONDS,
    )


async def fetch_event_payload(event_ticker: str) -> dict:
    """Raw Get Event payload (a bare Event) with fresh contract prices."""
    safe_ticker = quote(event_ticker, safe="")
    return await get_json(
        f"{GEMINI_API_BASE}/events/{safe_ticker}",
        source=GEMINI_SOURCE,
        ttl=GEMINI_CACHE_TTL_SECONDS,
    )
