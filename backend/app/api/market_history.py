"""
GET /api/market/history?symbol=BTC&minutes=240 → {symbol, source, delayed, points: [{t, p}], error?}

Recent price history (free sources) for the LIVE UPDATE popup's sparkline. Always HTTP 200:
when every source fails the body has `points: []` and an `error` string (see app/data/history.py).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.data import history

router = APIRouter(tags=["feeds"])


@router.get("/api/market/history")
async def market_history(
    symbol: str = Query(..., min_length=1, max_length=20),
    minutes: int = Query(history.DEFAULT_MINUTES, ge=5, le=history.MAX_MINUTES),
) -> dict[str, Any]:
    """Close prices of `symbol` over the last `minutes` (crypto: Hyperliquid → CoinGecko; equities: yfinance)."""
    return await history.fetch_history(symbol, minutes)
