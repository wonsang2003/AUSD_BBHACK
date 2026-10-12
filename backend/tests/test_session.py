"""Session hours and state (app/keeper/session.py)."""

from datetime import UTC, datetime

from app.keeper.session import SessionState, parse_session_hours, session_window, within_hours

HOURS = parse_session_hours({"start": "09:30", "end": "16:00", "tz": "America/New_York"})


def test_window_is_local_hours_in_utc():
    start, end = session_window(HOURS, datetime(2026, 10, 12, 15, 0, tzinfo=UTC))
    assert (start.hour, start.minute, end.hour) == (13, 30, 20)   # EDT = UTC−4


def test_within_hours():
    assert within_hours(HOURS, datetime(2026, 10, 12, 15, 0, tzinfo=UTC))
    assert not within_hours(HOURS, datetime(2026, 10, 12, 21, 0, tzinfo=UTC))


def test_state_start_stop_and_close():
    state = SessionState()
    now = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
    state.start(HOURS, now)
    assert state.to_api()["live"] is True
    assert state.should_close(datetime(2026, 10, 12, 20, 0, tzinfo=UTC))
    state.stop()
    assert state.to_api()["live"] is False
