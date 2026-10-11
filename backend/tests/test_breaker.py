"""
Tests for app/agents/breaker.py — the payment circuit breaker (global halt + per-agent backoff).

Pure logic with an injected clock; no chain needed.
"""

from __future__ import annotations

from app.agents.breaker import BACKOFF_BASE_SECONDS, BACKOFF_MAX_SECONDS, CircuitBreaker


class FakeClock:
    """A clock the test moves by hand (seconds)."""

    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_global_trip_halts_every_agent():
    # Arrange
    breaker = CircuitBreaker()

    # Act
    breaker.trip_global("AUSD paused")

    # Assert
    for agent in ("OptionsEvent", "NewsSentiment", "MacroFilings"):
        allowed, reason = breaker.allows(agent)
        assert allowed is False
        assert "AUSD paused" in reason


def test_reprobe_keeps_the_breaker_open_while_the_probe_fails():
    # Arrange
    breaker = CircuitBreaker()
    breaker.trip_global("AUSD paused")

    # Act
    still_open = breaker.reprobe(lambda: False)

    # Assert
    assert still_open is True


def test_reprobe_closes_the_breaker_when_the_probe_succeeds():
    # Arrange
    changes = []
    breaker = CircuitBreaker(on_change=changes.append)
    breaker.trip_global("AUSD paused")

    # Act
    still_open = breaker.reprobe(lambda: True)

    # Assert
    assert still_open is False
    assert breaker.allows("OptionsEvent") == (True, "")
    assert changes[-1]["global_open"] is False


def test_agent_backoff_doubles_per_failure_and_is_capped():
    # Arrange
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock)

    # Act
    for _ in range(20):
        breaker.record_failure("OptionsEvent", "pre-flight insufficient_balance")

    # Assert
    assert breaker.status()["agents"]["OptionsEvent"]["backoff_seconds"] == BACKOFF_MAX_SECONDS


def test_agent_backoff_affects_only_that_agent_and_expires():
    # Arrange
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure("OptionsEvent", "pre-flight frozen")

    # Act
    blocked = breaker.allows("OptionsEvent")[0]
    other_allowed = breaker.allows("NewsSentiment")[0]
    clock.now += BACKOFF_BASE_SECONDS + 1
    allowed_later = breaker.allows("OptionsEvent")[0]

    # Assert
    assert blocked is False
    assert other_allowed is True
    assert allowed_later is True


def test_success_clears_an_agents_backoff():
    # Arrange
    breaker = CircuitBreaker()
    breaker.record_failure("OptionsEvent", "rejected")

    # Act
    breaker.record_success("OptionsEvent")

    # Assert
    assert breaker.allows("OptionsEvent") == (True, "")
