"""
In-process publish/subscribe for live dashboard updates (GET /api/stream, Server-Sent Events).

    agents / keeper / alerts ──publish(type, data)──▶ EventBus ──▶ one asyncio.Queue per subscriber
                                                                      │
    FastAPI /api/stream:  sse_stream(bus.subscribe()) ◀───────────────┘  "event: purchase\\ndata: {...}\\n\\n"

Rules:
  * `publish` is thread-safe: the APScheduler keeper runs in worker threads, so an event is
    handed to each subscriber's event loop with `call_soon_threadsafe` unless the caller is
    already on that loop.
  * Each subscriber has a bounded queue. A slow browser never blocks publishers: when its
    queue is full the OLDEST event is dropped to make room for the newest.
  * `state` snapshots are throttled to at most one per 5 s (docs/API.md); a snapshot
    published sooner is dropped (the next periodic snapshot carries the newer state).
  * `sse_stream` sends `event: ping` after 15 s without events so proxies keep the
    connection open and the browser can detect a dead stream.

Event types and payloads are listed in docs/API.md "SSE".
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# ─── event types (docs/API.md "SSE") ─────────────────────────────────────────

EVENT_PURCHASE = "purchase"
EVENT_FINDING = "finding"
EVENT_ALLOCATION = "allocation"
EVENT_RECHARGE = "recharge"
EVENT_SWEEP = "sweep"
EVENT_ALERT = "alert"
EVENT_STATE = "state"
EVENT_MARKET = "market"
EVENT_ASK = "ask"
EVENT_BREAKER = "breaker"     # Update #1: CircuitBreaker.status() changes
EVENT_PING = "ping"

# ─── tuning ──────────────────────────────────────────────────────────────────

PING_INTERVAL_SECONDS = 15.0
SUBSCRIBER_QUEUE_SIZE = 256
STATE_MIN_INTERVAL_SECONDS = 5.0
DEFAULT_THROTTLE_SECONDS: dict[str, float] = {EVENT_STATE: STATE_MIN_INTERVAL_SECONDS}


@dataclass(frozen=True)
class Event:
    """One published event: its SSE type and a JSON-serialisable payload."""

    type: str
    data: Any


# ─── SSE formatting ──────────────────────────────────────────────────────────

def _json_default(value: Any) -> Any:
    """JSON fallback for datetimes (ISO-8601) and anything else (str)."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def format_sse(event_type: str, data: Any) -> str:
    """
    One SSE message: "event: <type>\\ndata: <json>\\n\\n".

    The JSON is written on a single line (json.dumps escapes newlines inside strings), so
    one `data:` line always carries the whole payload.
    """
    payload = json.dumps(data, default=_json_default, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


def ping_payload() -> dict[str, str]:
    """Body of the keep-alive `ping` event: the server time."""
    return {"ts": datetime.now(UTC).isoformat()}


# ─── subscriptions ───────────────────────────────────────────────────────────

class Subscription:
    """
    One listener's queue. Iterate it with `async for event in subscription`; call `close()`
    (or use `async with`) to stop receiving. Must be created on the listener's event loop.
    """

    def __init__(self, bus: EventBus, queue_size: int):
        self._bus = bus
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
        self.dropped = 0          # events discarded because this listener fell behind
        self.closed = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The event loop this subscription delivers on."""
        return self._loop

    def deliver(self, event: Event) -> None:
        """Queue one event (runs on this subscription's loop). Full queue → drop the oldest."""
        if self.closed:
            return
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(event)

    async def next_event(self) -> Event:
        """Wait for the next event."""
        return await self._queue.get()

    def close(self) -> None:
        """Stop receiving events."""
        self.closed = True
        self._bus.unsubscribe(self)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        if self.closed:
            raise StopAsyncIteration
        return await self.next_event()

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()


# ─── the bus ─────────────────────────────────────────────────────────────────

class EventBus:
    """In-process pub/sub: `publish(type, data)` from anywhere, `subscribe()` from async code."""

    def __init__(
        self,
        queue_size: int = SUBSCRIBER_QUEUE_SIZE,
        throttle_seconds: Mapping[str, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._queue_size = queue_size
        self._throttle_seconds = dict(DEFAULT_THROTTLE_SECONDS if throttle_seconds is None else throttle_seconds)
        self._clock = clock
        self._subscribers: list[Subscription] = []
        self._last_published_at: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def subscriber_count(self) -> int:
        """How many listeners are connected."""
        with self._lock:
            return len(self._subscribers)

    def subscribe(self) -> Subscription:
        """Register a new listener on the current event loop (call from async code)."""
        subscription = Subscription(self, self._queue_size)
        with self._lock:
            self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a listener (no error if it is already gone)."""
        with self._lock:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)

    def _passes_throttle(self, event_type: str) -> bool:
        """Rule: a throttled type is published at most once per its minimum interval."""
        min_interval = self._throttle_seconds.get(event_type)
        if min_interval is None:
            return True
        now = self._clock()
        with self._lock:
            last = self._last_published_at.get(event_type)
            if last is not None and now - last < min_interval:
                return False
            self._last_published_at[event_type] = now
        return True

    def publish(self, event_type: str, data: Any) -> bool:
        """
        Send one event to every subscriber. Safe to call from any thread.
        Returns False when the event was dropped by the throttle, else True.
        """
        if not self._passes_throttle(event_type):
            return False
        event = Event(event_type, data)
        with self._lock:
            subscribers = list(self._subscribers)
        for subscription in subscribers:
            self._hand_over(subscription, event)
        return True

    def _hand_over(self, subscription: Subscription, event: Event) -> None:
        """Deliver directly when already on the subscriber's loop, else via call_soon_threadsafe."""
        if _running_loop() is subscription.loop:
            subscription.deliver(event)
            return
        try:
            subscription.loop.call_soon_threadsafe(subscription.deliver, event)
        except RuntimeError:  # the subscriber's loop is closed: forget it
            self.unsubscribe(subscription)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """The event loop running in this thread, or None in a plain (scheduler) thread."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# ─── SSE stream ──────────────────────────────────────────────────────────────

async def sse_stream(
    subscription: Subscription,
    ping_interval: float = PING_INTERVAL_SECONDS,
) -> AsyncIterator[str]:
    """
    Turn a subscription into SSE text chunks for a StreamingResponse:
    each event becomes `format_sse(type, data)`; `ping` is sent after `ping_interval`
    seconds of silence. The subscription is closed when the client disconnects.
    """
    try:
        while not subscription.closed:
            try:
                event = await asyncio.wait_for(subscription.next_event(), timeout=ping_interval)
            except TimeoutError:
                yield format_sse(EVENT_PING, ping_payload())
                continue
            yield format_sse(event.type, event.data)
    finally:
        subscription.close()
