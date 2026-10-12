"""
Keeper scheduler: the periodic jobs that keep the desk running during session hours (SPEC §5–6).

    job              every     what it does (all delegate to the Runtime)
    agent_ticks      90 s      one buying round for every agent — only while the session is live
    close_check      60 s      close a live session at its end time: sweep + daily brief
    refill_check     300 s     intraday refills (PERMIT mode)
    breaker_reprobe  60 s      re-probe the token's pause state; auto-resume the global breaker
    alert_check      300 s     runway < 3 days, daily cap reached
    market_refresh   60 s      free market strip → spots for "above:K" strikes, `market` SSE
    calendar_refresh 1800 s    today's events (earnings, FOMC/CPI, expiries) for the allocator

`KeeperJobs` holds the job bodies as plain methods, so scripts and tests can run any job once
without a scheduler (`await jobs.agent_ticks()`, or `run_once(jobs.agent_ticks)` from sync code).
`KeeperScheduler` registers them on an APScheduler AsyncIOScheduler (the FastAPI event loop).
A failing job is logged and retried at its next run; it never stops the scheduler.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.chain.web3client import redact_urls
from app.keeper.runtime import Runtime

log = logging.getLogger(__name__)

AGENT_TICK_SECONDS = 90
CLOSE_CHECK_SECONDS = 60
REFILL_CHECK_SECONDS = 300
BREAKER_REPROBE_SECONDS = 60
ALERT_CHECK_SECONDS = 300
MARKET_REFRESH_SECONDS = 60
CALENDAR_REFRESH_SECONDS = 1_800


@dataclass(frozen=True)
class JobSpec:
    """One periodic job: its id, interval and the KeeperJobs method it runs."""

    job_id: str
    seconds: int
    method: str


JOB_SPECS: tuple[JobSpec, ...] = (
    JobSpec("agent_ticks", AGENT_TICK_SECONDS, "agent_ticks"),
    JobSpec("close_check", CLOSE_CHECK_SECONDS, "close_check"),
    JobSpec("refill_check", REFILL_CHECK_SECONDS, "refill_check"),
    JobSpec("breaker_reprobe", BREAKER_REPROBE_SECONDS, "breaker_reprobe"),
    JobSpec("alert_check", ALERT_CHECK_SECONDS, "alert_check"),
    JobSpec("market_refresh", MARKET_REFRESH_SECONDS, "market_refresh"),
    JobSpec("calendar_refresh", CALENDAR_REFRESH_SECONDS, "calendar_refresh"),
)


class KeeperJobs:
    """The job bodies. Each catches and logs its own errors and returns what it did."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def agent_ticks(self) -> list[dict[str, Any]]:
        """All agents buy once — only while a session is live and its end time has not passed."""
        if not self.runtime.session.live:
            return []
        return await _guard("agent_ticks", self.runtime.run_tick(), [])

    async def close_check(self) -> dict[str, Any] | None:
        """Sweep and brief when the live session reaches its end time."""
        return await _guard("close_check", self.runtime.close_if_due(), None)

    async def refill_check(self) -> list[dict[str, Any]]:
        """Intraday refills (blocking chain calls run in a worker thread)."""
        return await _guard("refill_check", asyncio.to_thread(self.runtime.refill_check), [])

    async def breaker_reprobe(self) -> bool:
        """Re-probe the token; True while the global breaker is still open."""
        return await _guard("breaker_reprobe", asyncio.to_thread(self.runtime.reprobe_breaker), True)

    async def alert_check(self) -> None:
        """Runway and daily-cap alerts."""
        await _guard("alert_check", asyncio.to_thread(self.runtime.check_alerts), None)

    async def market_refresh(self) -> list[dict[str, Any]]:
        """Free market strip (spots + `market` event)."""
        return await _guard("market_refresh", self.runtime.refresh_market(), [])

    async def calendar_refresh(self) -> list[dict[str, Any]]:
        """Today's calendar events."""
        return await _guard("calendar_refresh", self.runtime.refresh_events(), [])


async def _guard(name: str, awaitable: Any, default: Any) -> Any:
    """Await one job; log (URL-redacted) and return `default` on any error."""
    try:
        return await awaitable
    except Exception as error:  # noqa: BLE001 — a job failure must never stop the scheduler
        log.warning("keeper job %s failed: %s", name, redact_urls(f"{type(error).__name__}: {error}"))
        return default


def run_once(job: Callable[[], Any]) -> Any:
    """Run one job from synchronous code (scripts): coroutine jobs get their own event loop."""
    result = job()
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


class KeeperScheduler:
    """Registers every JobSpec on an AsyncIOScheduler; start() inside the running event loop."""

    def __init__(self, runtime: Runtime, scheduler: AsyncIOScheduler | None = None):
        self.jobs = KeeperJobs(runtime)
        self.scheduler = scheduler or AsyncIOScheduler(timezone="UTC")

    def register(self) -> list[str]:
        """Add the interval jobs (no overlapping runs; a late run is coalesced). Returns their ids."""
        for spec in JOB_SPECS:
            self.scheduler.add_job(
                getattr(self.jobs, spec.method),
                "interval",
                seconds=spec.seconds,
                id=spec.job_id,
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
        return [spec.job_id for spec in JOB_SPECS]

    def start(self) -> None:
        """Register the jobs and start the scheduler (must be called with an event loop running)."""
        self.register()
        self.scheduler.start()

    def shutdown(self) -> None:
        """Stop the scheduler without waiting for running jobs."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
