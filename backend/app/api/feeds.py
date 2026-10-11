"""
Read-only feeds for the dashboard panels:

    GET /api/purchases?limit=100   every VOI decision (bought, skipped, failed), newest first
    GET /api/findings?limit=50     agent conclusions with receipts, newest first
    GET /api/allocations           the latest allocator run {date, budget_atomic, allocations}
    GET /api/events-today          calendar events that boost an agent today
    GET /api/pm-vs-options         prediction-market vs options-implied probability (latest pm_edge buy)
    GET /api/market                free, labeled quotes for the user's watchlist
    GET /api/alerts?limit=50       dashboard alerts, newest first

Rows come straight from `db.*.to_api()` or the runtime, so the JSON matches docs/API.md
without re-shaping here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import api_errors, call_blocking, engine_of, get_runtime
from app.data import market as market_data
from app.db import (
    DEFAULT_ALERT_LIMIT,
    DEFAULT_FINDING_LIMIT,
    DEFAULT_PURCHASE_LIMIT,
    load_api_settings,
    recent_alerts,
    recent_findings,
    recent_purchases,
    session_scope,
)

router = APIRouter(tags=["feeds"])

MAX_FEED_LIMIT = 1_000


def _rows(runtime: Any, query, limit: int) -> list[dict[str, Any]]:
    """Run one `recent_*` repository query and serialise the rows."""
    with session_scope(engine_of(runtime)) as session:
        return [row.to_api() for row in query(session, limit=limit)]


@router.get("/api/purchases")
async def purchases(
    limit: int = Query(DEFAULT_PURCHASE_LIMIT, ge=1, le=MAX_FEED_LIMIT),
    runtime: Any = Depends(get_runtime),
) -> list[dict[str, Any]]:
    """Purchase rows, newest first (skipped and failed decisions included)."""
    return await call_blocking(_rows, runtime, recent_purchases, limit)


@router.get("/api/findings")
async def findings(
    limit: int = Query(DEFAULT_FINDING_LIMIT, ge=1, le=MAX_FEED_LIMIT),
    runtime: Any = Depends(get_runtime),
) -> list[dict[str, Any]]:
    """Finding rows, newest first."""
    return await call_blocking(_rows, runtime, recent_findings, limit)


@router.get("/api/alerts")
async def alerts(
    limit: int = Query(DEFAULT_ALERT_LIMIT, ge=1, le=MAX_FEED_LIMIT),
    runtime: Any = Depends(get_runtime),
) -> list[dict[str, Any]]:
    """Alert rows `{id, ts, level, kind, message}`, newest first."""
    return await call_blocking(_rows, runtime, recent_alerts, limit)


@router.get("/api/allocations")
async def allocations(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """The latest allocator run; before the first run: today's date, budget 0, no rows."""
    latest = await call_blocking(runtime.latest_allocations)
    if latest is None:
        return {"date": datetime.now(UTC).strftime("%Y-%m-%d"), "budget_atomic": 0, "allocations": []}
    return latest


@router.get("/api/events-today")
async def events_today(runtime: Any = Depends(get_runtime)) -> list[dict[str, Any]]:
    """`[{date, kind, title, asset, boosted_agent}]` for today (UTC)."""
    return await call_blocking(runtime.events_today)


@router.get("/api/pm-vs-options")
async def pm_vs_options(runtime: Any = Depends(get_runtime)) -> list[dict[str, Any]]:
    """Rows of the latest pm_edge purchase (empty until one was bought)."""
    return await call_blocking(runtime.pm_vs_options)


def _watchlist(runtime: Any) -> list[str]:
    """The user's watchlist from Settings (the market strip shows exactly these symbols)."""
    with session_scope(engine_of(runtime)) as session:
        return list(load_api_settings(session).get("watchlist") or [])


async def fetch_market_rows(watchlist: list[str]) -> list[dict[str, Any]]:
    """Quotes for `watchlist` from the free sources in `app/data/market.py` (a failing source is left out)."""
    market_strip = getattr(market_data, "market_strip", None)
    if market_strip is not None:
        return await market_strip(watchlist)
    strip = await market_data.build_market_strip(watchlist or None)
    return strip.to_rows()


@router.get("/api/market")
async def market(runtime: Any = Depends(get_runtime)) -> list[dict[str, Any]]:
    """`[{symbol, price, change_24h_pct, source, delayed}]` — free sources only, each labeled."""
    watchlist = await call_blocking(_watchlist, runtime)
    with api_errors():
        return await fetch_market_rows(watchlist)
