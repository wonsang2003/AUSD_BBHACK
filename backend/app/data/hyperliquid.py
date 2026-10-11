"""
Hyperliquid adapter: perpetual-futures funding, open interest and mark prices.

Used by the `perps_crowding` seller (SPEC §4.3). One public, key-less request
returns every listed perp at once:

    POST https://api.hyperliquid.xyz/info   {"type": "metaAndAssetCtxs"}

    response = [
        {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}, ...], ...},   # static metadata
        [{"funding": "0.0000125", "openInterest": "688.11", "markPx": "14.3161", ...}, ...]  # live contexts
    ]

The two lists are parallel: `universe[i]` names the asset whose live numbers are in
`contexts[i]`, so we zip them. Numbers arrive as decimal strings.

Units (Hyperliquid conventions):
    funding        hourly funding rate as a fraction (0.0000125 = 0.00125 % per hour);
                   positive → longs pay shorts
    openInterest   in coins (base units), so OI in USD = openInterest · markPx
    dayNtlVlm      last-24h traded notional in USD
    midPx          order-book mid price in USD (null when the book is one-sided)
    prevDayPx      price 24 h ago, used for the market strip's 24 h change

Two callers share the one cached request: the `perps_crowding` seller (funding + OI)
and the dashboard market strip in `app/data/market.py` (mid price + 24 h change).

The fetch (network) is separate from `parse_meta_and_asset_ctxs` (pure), which the
tests exercise with a recorded-shape fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.data.http import post_json

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
SOURCE_NAME = "hyperliquid"
META_AND_ASSET_CTXS_REQUEST = {"type": "metaAndAssetCtxs"}

# Hyperliquid pays funding every hour, so a year has 24 · 365 funding periods.
FUNDING_PERIODS_PER_YEAR = 24 * 365

# Live contexts change constantly; a short cache only de-duplicates bursts of calls.
CONTEXTS_CACHE_TTL_SECONDS = 15.0
PERCENT = 100.0


@dataclass(frozen=True)
class PerpContext:
    """One perpetual market's live state, parsed from `metaAndAssetCtxs`."""

    name: str                      # e.g. "BTC" (as listed by Hyperliquid)
    funding_hourly: float          # fraction per hour; > 0 means longs pay shorts
    open_interest_coins: float     # open interest in base units (coins)
    mark_px: float                 # mark price in USD
    mid_px: float | None           # order-book mid price in USD, if the book is two-sided
    oracle_px: float | None        # oracle (index) price in USD, if given
    day_notional_volume_usd: float # last-24h notional volume in USD
    prev_day_px: float | None      # price 24 h ago, if given

    @property
    def open_interest_usd(self) -> float:
        """Open interest in USD = open interest in coins · mark price."""
        return self.open_interest_coins * self.mark_px

    @property
    def funding_annualized(self) -> float:
        """Simple (non-compounded) annualised funding = hourly rate · 24 · 365."""
        return self.funding_hourly * FUNDING_PERIODS_PER_YEAR

    @property
    def display_px(self) -> float:
        """Price to show a person: the book mid when there is one, else the mark price."""
        return self.mid_px if self.mid_px is not None else self.mark_px

    @property
    def change_24h_pct(self) -> float | None:
        """24 h change in percent = (display price / prevDayPx − 1) · 100; None without prevDayPx."""
        if self.prev_day_px is None or self.prev_day_px <= 0:
            return None
        return (self.display_px / self.prev_day_px - 1.0) * PERCENT


class HyperliquidParseError(ValueError):
    """The response did not have the documented `[meta, contexts]` shape."""


def _optional_float(value: Any) -> float | None:
    """Convert a decimal string to float; None stays None (Hyperliquid sends null for some fields)."""
    if value is None:
        return None
    return float(value)


def _split_payload(payload: Any) -> tuple[list[dict], list[dict]]:
    """Return (universe, contexts) from the two-element response, or raise if the shape is wrong."""
    if not isinstance(payload, list) or len(payload) != 2:
        raise HyperliquidParseError("expected a two-element list [meta, assetCtxs]")
    meta, contexts = payload
    if not isinstance(meta, dict) or "universe" not in meta or not isinstance(contexts, list):
        raise HyperliquidParseError("expected meta.universe and a list of asset contexts")
    universe = meta["universe"]
    if len(universe) != len(contexts):
        raise HyperliquidParseError(
            f"universe has {len(universe)} assets but there are {len(contexts)} contexts"
        )
    return universe, contexts


def _parse_one(asset_meta: dict, context: dict) -> PerpContext | None:
    """
    Build one PerpContext. Returns None for markets we must not score:
    delisted assets and markets without a mark price or funding value.
    """
    if asset_meta.get("isDelisted"):
        return None
    if context.get("markPx") is None or context.get("funding") is None:
        return None
    return PerpContext(
        name=str(asset_meta["name"]),
        funding_hourly=float(context["funding"]),
        open_interest_coins=float(context.get("openInterest") or 0.0),
        mark_px=float(context["markPx"]),
        mid_px=_optional_float(context.get("midPx")),
        oracle_px=_optional_float(context.get("oraclePx")),
        day_notional_volume_usd=float(context.get("dayNtlVlm") or 0.0),
        prev_day_px=_optional_float(context.get("prevDayPx")),
    )


def parse_meta_and_asset_ctxs(payload: Any) -> list[PerpContext]:
    """
    Zip `universe[i]` with `contexts[i]` into PerpContext rows.

    Delisted markets and markets with a missing mark price / funding are skipped.
    """
    universe, contexts = _split_payload(payload)
    parsed: list[PerpContext] = []
    for asset_meta, context in zip(universe, contexts):
        row = _parse_one(asset_meta, context)
        if row is not None:
            parsed.append(row)
    return parsed


def index_by_symbol(contexts: list[PerpContext]) -> dict[str, PerpContext]:
    """Map upper-cased asset name → context, so "btc" and "BTC" find the same market."""
    return {context.name.upper(): context for context in contexts}


async def fetch_perp_contexts() -> list[PerpContext]:
    """Fetch and parse every listed Hyperliquid perp (one POST, cached for a few seconds)."""
    payload = await post_json(
        HYPERLIQUID_INFO_URL,
        META_AND_ASSET_CTXS_REQUEST,
        source=SOURCE_NAME,
        ttl=CONTEXTS_CACHE_TTL_SECONDS,
    )
    return parse_meta_and_asset_ctxs(payload)
