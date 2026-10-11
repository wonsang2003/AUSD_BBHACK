"""
Session hours: when the research desk is "live" (SPEC §5: agents run during session hours).

The user sets `session_hours = {start: "HH:MM", end: "HH:MM", tz}` in Settings (default
09:30–16:00 America/New_York). A session is started/stopped explicitly (POST
/api/session/start → recharges, /stop → sweep) and the scheduler also closes it at the end
time. `SessionState` is the `session` block of GET /api/state: `{live, started_at, ends_at}`.

Rule for one day: the window is [start, end) on today's date in the session time zone.
If end ≤ start the window runs past midnight (e.g. 22:00–06:00 ends the next day).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.db import iso_utc

DEFAULT_START = "09:30"
DEFAULT_END = "16:00"
DEFAULT_TZ = "America/New_York"
TIME_FORMAT_SEPARATOR = ":"


@dataclass(frozen=True)
class SessionHours:
    """Parsed session hours: local start/end time and the time zone."""

    start: time
    end: time
    tz: ZoneInfo

    @property
    def crosses_midnight(self) -> bool:
        """True when the session ends on the next calendar day (end ≤ start)."""
        return self.end <= self.start


def _parse_hhmm(text: str) -> time:
    """"09:30" → time(9, 30). Raises ValueError for anything else."""
    hours, minutes = text.strip().split(TIME_FORMAT_SEPARATOR)
    return time(int(hours), int(minutes))


def parse_session_hours(raw: Mapping[str, Any] | None) -> SessionHours:
    """Settings.session_hours → SessionHours (missing keys use 09:30–16:00 America/New_York)."""
    raw = raw or {}
    return SessionHours(
        start=_parse_hhmm(str(raw.get("start") or DEFAULT_START)),
        end=_parse_hhmm(str(raw.get("end") or DEFAULT_END)),
        tz=ZoneInfo(str(raw.get("tz") or DEFAULT_TZ)),
    )


def session_window(hours: SessionHours, now: datetime) -> tuple[datetime, datetime]:
    """
    The session window that contains `now`, or else today's (local date) window, as aware UTC datetimes.
    For a session crossing midnight, a `now` before the end time belongs to yesterday's window.
    """
    local_now = now.astimezone(hours.tz)
    day = local_now.date()
    if hours.crosses_midnight and local_now.time() < hours.end:
        day = day - timedelta(days=1)
    start = datetime.combine(day, hours.start, tzinfo=hours.tz)
    end_day = day + timedelta(days=1) if hours.crosses_midnight else day
    end = datetime.combine(end_day, hours.end, tzinfo=hours.tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def within_hours(hours: SessionHours, now: datetime) -> bool:
    """True when `now` lies in [start, end) of its session window."""
    start, end = session_window(hours, now)
    return start <= now.astimezone(UTC) < end


@dataclass
class SessionState:
    """Whether the desk is live right now, and the current session's start / scheduled end."""

    live: bool = False
    started_at: datetime | None = None
    ends_at: datetime | None = None

    def start(self, hours: SessionHours, now: datetime) -> None:
        """Go live now; the session ends at today's end time (or the next one if that has passed)."""
        _, end = session_window(hours, now)
        if end <= now:
            end = end + timedelta(days=1)
        self.live, self.started_at, self.ends_at = True, now, end

    def stop(self) -> None:
        """Close the session (the start time is kept for the daily brief window)."""
        self.live = False

    def should_close(self, now: datetime) -> bool:
        """True when a live session has reached its scheduled end."""
        return self.live and self.ends_at is not None and now >= self.ends_at

    def to_api(self) -> dict[str, Any]:
        """docs/API.md State.session: `{live, started_at, ends_at}`."""
        return {"live": self.live, "started_at": iso_utc(self.started_at), "ends_at": iso_utc(self.ends_at)}
