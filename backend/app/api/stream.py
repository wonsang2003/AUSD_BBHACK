"""
`GET /api/stream`: live dashboard updates as Server-Sent Events (docs/API.md "SSE").

    EventBus.publish(type, data) ──▶ this subscriber's queue ──▶ "event: <type>\ndata: <json>\n\n"

A `ping` is sent immediately on connect (so the browser knows the stream is live) and after
every 15 s of silence (`events.sse_stream`). `?max_events=N` closes the stream after N events —
handy for curl and for tests (in-process HTTP clients wait for the response to finish).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.deps import get_runtime
from app.events import EVENT_PING, PING_INTERVAL_SECONDS, Subscription, format_sse, ping_payload, sse_stream

router = APIRouter(tags=["stream"])

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


async def event_chunks(
    subscription: Subscription,
    max_events: int | None = None,
    ping_interval: float = PING_INTERVAL_SECONDS,
) -> AsyncIterator[str]:
    """The initial ping, then every bus event as SSE text; stops after `max_events` chunks if given."""
    try:
        yield format_sse(EVENT_PING, ping_payload())
        sent = 1
        if max_events is not None and sent >= max_events:
            return
        async with aclosing(sse_stream(subscription, ping_interval)) as chunks:
            async for chunk in chunks:
                yield chunk
                sent += 1
                if max_events is not None and sent >= max_events:
                    return
    finally:
        subscription.close()


@router.get("/api/stream")
async def stream(
    max_events: int | None = Query(None, ge=1),
    runtime: Any = Depends(get_runtime),
) -> StreamingResponse:
    """Server-Sent Events: purchase, finding, allocation, recharge, sweep, alert, state, market, ask, breaker, ping."""
    subscription = runtime.bus.subscribe()
    return StreamingResponse(event_chunks(subscription, max_events), media_type="text/event-stream",
                             headers=SSE_HEADERS)
