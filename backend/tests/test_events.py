"""
Tests for app/events.py: EventBus pub/sub, thread-safe publish, throttling, SSE formatting
and the ping keep-alive.
"""

from __future__ import annotations

import asyncio
import json
import threading

from app.events import (
    EVENT_PING,
    EVENT_PURCHASE,
    EVENT_STATE,
    Event,
    EventBus,
    format_sse,
    sse_stream,
)

RECEIVE_TIMEOUT_SECONDS = 1.0


class ManualClock:
    """A clock the test moves by hand (seconds)."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _next(subscription) -> Event:
    return await asyncio.wait_for(subscription.next_event(), timeout=RECEIVE_TIMEOUT_SECONDS)


def test_format_sse_writes_event_line_and_single_line_json_data():
    # Act
    text = format_sse("purchase", {"agent": "OptionsEvent", "reason": "line one\nline two"})

    # Assert
    lines = text.split("\n")
    assert lines[0] == "event: purchase"
    assert lines[1].startswith("data: ")
    assert json.loads(lines[1][len("data: "):]) == {"agent": "OptionsEvent", "reason": "line one\nline two"}
    assert text.endswith("\n\n")
    assert len(lines) == 4  # event, data, blank, trailing ""


async def test_every_subscriber_receives_a_published_event():
    # Arrange
    bus = EventBus()
    first, second = bus.subscribe(), bus.subscribe()

    # Act
    bus.publish(EVENT_PURCHASE, {"id": 1})

    # Assert
    assert await _next(first) == Event(EVENT_PURCHASE, {"id": 1})
    assert await _next(second) == Event(EVENT_PURCHASE, {"id": 1})


async def test_publish_from_a_scheduler_thread_reaches_the_async_subscriber():
    # Arrange
    bus = EventBus()
    subscription = bus.subscribe()

    # Act
    worker = threading.Thread(target=bus.publish, args=(EVENT_PURCHASE, {"from": "thread"}))
    worker.start()
    worker.join()

    # Assert
    assert await _next(subscription) == Event(EVENT_PURCHASE, {"from": "thread"})


async def test_closed_subscription_no_longer_receives_events():
    # Arrange
    bus = EventBus()
    async with bus.subscribe() as subscription:
        assert bus.subscriber_count == 1

    # Act
    bus.publish(EVENT_PURCHASE, {"id": 1})

    # Assert
    assert bus.subscriber_count == 0
    assert subscription.closed


async def test_full_queue_drops_the_oldest_event_not_the_newest():
    # Arrange
    bus = EventBus(queue_size=2)
    subscription = bus.subscribe()

    # Act
    for number in range(3):
        bus.publish(EVENT_PURCHASE, {"n": number})

    # Assert
    assert (await _next(subscription)).data == {"n": 1}
    assert (await _next(subscription)).data == {"n": 2}
    assert subscription.dropped == 1


async def test_state_events_are_throttled_to_one_per_five_seconds():
    # Arrange
    clock = ManualClock()
    bus = EventBus(clock=clock)
    subscription = bus.subscribe()

    # Act
    first = bus.publish(EVENT_STATE, {"v": 1})
    clock.now = 4.9
    too_soon = bus.publish(EVENT_STATE, {"v": 2})
    clock.now = 5.0
    on_time = bus.publish(EVENT_STATE, {"v": 3})

    # Assert
    assert (first, too_soon, on_time) == (True, False, True)
    assert (await _next(subscription)).data == {"v": 1}
    assert (await _next(subscription)).data == {"v": 3}


async def test_unthrottled_types_are_never_dropped_by_the_throttle():
    # Arrange
    bus = EventBus(clock=ManualClock())

    # Act
    results = [bus.publish(EVENT_PURCHASE, {"n": n}) for n in range(3)]

    # Assert
    assert results == [True, True, True]


async def test_sse_stream_yields_published_events_as_sse_text():
    # Arrange
    bus = EventBus()
    stream = sse_stream(bus.subscribe(), ping_interval=5.0)
    bus.publish(EVENT_PURCHASE, {"id": 7})

    # Act
    chunk = await asyncio.wait_for(stream.__anext__(), timeout=RECEIVE_TIMEOUT_SECONDS)
    await stream.aclose()

    # Assert
    assert chunk == 'event: purchase\ndata: {"id":7}\n\n'
    assert bus.subscriber_count == 0


async def test_sse_stream_sends_ping_after_silence():
    # Arrange
    bus = EventBus()
    stream = sse_stream(bus.subscribe(), ping_interval=0.05)

    # Act
    chunk = await asyncio.wait_for(stream.__anext__(), timeout=RECEIVE_TIMEOUT_SECONDS)
    await stream.aclose()

    # Assert
    assert chunk.startswith(f"event: {EVENT_PING}\ndata: ")
    assert "ts" in json.loads(chunk.split("data: ", 1)[1])
