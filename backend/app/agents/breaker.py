"""
Circuit breaker for agent payments.

Two levels:
  * GLOBAL halt — tripped when a pre-flight simulation says the token itself is paused.
    Every agent stops paying; the dashboard shows "TOKEN PAUSED — agents halted".
    A re-probe (every 60 s, driven by the scheduler) closes it automatically when a
    fresh simulation succeeds.
  * PER-AGENT backoff — any other pre-flight / payment failure (frozen counterparty,
    insufficient balance, bad signature, ...) pauses only that agent, with an
    exponentially growing delay.

The breaker never moves funds; it only answers "may this agent pay right now?".
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

REPROBE_INTERVAL_SECONDS = 60
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 15 * 60


@dataclass
class AgentBackoff:
    failures: int = 0
    until: float = 0.0
    last_reason: str = ""


@dataclass
class CircuitBreaker:
    clock: Callable[[], float] = time.time
    on_change: Callable[[dict], None] | None = None
    global_open: bool = False
    global_reason: str = ""
    tripped_at: float | None = None
    _agents: dict[str, AgentBackoff] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ─── questions ───────────────────────────────────────────────────────
    def allows(self, agent: str) -> tuple[bool, str]:
        """(may_pay, reason_if_not)."""
        if self.global_open:
            return False, f"circuit breaker open: {self.global_reason}"
        backoff = self._agents.get(agent)
        if backoff and backoff.until > self.clock():
            seconds_left = int(backoff.until - self.clock())
            return False, f"agent backoff {seconds_left}s after: {backoff.last_reason}"
        return True, ""

    def status(self) -> dict:
        now = self.clock()
        return {
            "global_open": self.global_open,
            "global_reason": self.global_reason,
            "tripped_at": self.tripped_at,
            "agents": {
                name: {"failures": b.failures, "backoff_seconds": max(0, int(b.until - now)), "last_reason": b.last_reason}
                for name, b in self._agents.items()
            },
        }

    # ─── events ──────────────────────────────────────────────────────────
    def trip_global(self, reason: str) -> None:
        with self._lock:
            if self.global_open:
                return
            self.global_open, self.global_reason, self.tripped_at = True, reason, self.clock()
        self._notify()

    def reprobe(self, probe: Callable[[], bool]) -> bool:
        """Run `probe()` (a fresh pre-flight); close the global breaker if it succeeds. Returns open-state."""
        if self.global_open and probe():
            with self._lock:
                self.global_open, self.global_reason, self.tripped_at = False, "", None
            self._notify()
        return self.global_open

    def record_failure(self, agent: str, reason: str) -> None:
        """Put one agent into exponential backoff: 30 s, 60 s, 120 s, ... capped at 15 min."""
        with self._lock:
            backoff = self._agents.setdefault(agent, AgentBackoff())
            backoff.failures += 1
            delay = min(BACKOFF_BASE_SECONDS * 2 ** (backoff.failures - 1), BACKOFF_MAX_SECONDS)
            backoff.until = self.clock() + delay
            backoff.last_reason = reason
        self._notify()

    def record_success(self, agent: str) -> None:
        with self._lock:
            self._agents.pop(agent, None)

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change(self.status())
