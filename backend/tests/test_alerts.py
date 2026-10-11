"""
Tests for app/alerts/alerts.py: dedupe, persistence, SSE publish, Telegram forwarding,
each alert rule, the CircuitBreaker hook and the daily brief.

Uses a fresh in-memory database, a real EventBus and a real CircuitBreaker. The Telegram
side is a recording `notify` callable (TelegramBot.notify itself is tested in test_telegram.py).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agents.breaker import CircuitBreaker
from app.alerts.alerts import (
    KIND_AGENT_PAUSED,
    KIND_ALLOWANCE_REDUCED,
    KIND_COUNTERPARTY_FROZEN,
    KIND_DAILY_CAP_REACHED,
    KIND_REVOCATION,
    KIND_RUNWAY_LOW,
    KIND_TOKEN_PAUSED,
    KIND_TOKEN_RESUMED,
    KIND_VOUCHER_FAILED,
    TOKEN_PAUSED_BANNER,
    AlertService,
    build_daily_brief,
    compose_daily_brief,
)
from app.constants import ONE_AUSD
from app.db import (
    ALERT_CRITICAL,
    ALERT_INFO,
    ALERT_WARN,
    PURCHASE_BOUGHT,
    PURCHASE_FAILED,
    PURCHASE_SKIPPED,
    Finding,
    Purchase,
    add_finding,
    add_purchase,
    init_db,
    recent_alerts,
    save_api_settings,
    session_scope,
)
from app.events import EVENT_ALERT, EventBus
from app.savings import compute_savings

T0 = datetime(2026, 10, 11, 14, 0, tzinfo=UTC)


class ManualClock:
    """A UTC clock the test moves by hand."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def engine():
    return init_db("sqlite://")


@pytest.fixture
def clock():
    return ManualClock()


@pytest.fixture
def sent():
    return []


@pytest.fixture
def service(engine, clock, sent):
    return AlertService(engine, bus=None, notify=sent.append, clock=clock)


def _stored_kinds(engine) -> list[str]:
    with session_scope(engine) as session:
        return [alert.kind for alert in recent_alerts(session)]


# ─── core: dedupe, persist, publish, forward ─────────────────────────────────

def test_same_alert_inside_the_dedupe_window_is_suppressed(service, engine, clock):
    # Arrange
    first = service.raise_alert(ALERT_WARN, "test", "first")
    clock.now = T0 + timedelta(minutes=29)

    # Act
    repeat = service.raise_alert(ALERT_WARN, "test", "repeat")

    # Assert
    assert first is not None
    assert repeat is None
    assert _stored_kinds(engine) == ["test"]


def test_same_alert_after_the_dedupe_window_is_raised_again(service, engine, clock):
    # Arrange
    service.raise_alert(ALERT_WARN, "test", "first")
    clock.now = T0 + timedelta(minutes=31)

    # Act
    again = service.raise_alert(ALERT_WARN, "test", "again")

    # Assert
    assert again is not None
    assert _stored_kinds(engine) == ["test", "test"]


async def test_alert_is_published_on_the_bus_in_api_shape(engine, clock):
    # Arrange
    bus = EventBus()
    subscription = bus.subscribe()
    service = AlertService(engine, bus=bus, clock=clock)

    # Act
    service.raise_alert(ALERT_CRITICAL, "test", "boom")

    # Assert
    event = await subscription.next_event()
    assert event.type == EVENT_ALERT
    assert set(event.data) == {"id", "ts", "level", "kind", "message"}
    assert event.data["message"] == "boom"


def test_only_warn_and_critical_alerts_are_forwarded_to_telegram(service, sent):
    # Act
    service.raise_alert(ALERT_INFO, "quiet", "dashboard only")
    service.raise_alert(ALERT_WARN, "loud", "check this")
    service.raise_alert(ALERT_CRITICAL, "louder", "act now")

    # Assert
    assert sent == ["[WARN] check this", "[CRITICAL] act now"]


# ─── rules ───────────────────────────────────────────────────────────────────

def test_runway_below_three_days_raises_a_warning(service, engine):
    # Act: 5 AUSD left at 2 AUSD/day = 2.5 days
    alert = service.check_runway(allowance_remaining=5 * ONE_AUSD, avg_daily_spend=2 * ONE_AUSD)

    # Assert
    assert alert.kind == KIND_RUNWAY_LOW
    assert alert.level == ALERT_WARN
    assert "Runway 2.5 days" in alert.message


def test_runway_of_three_days_or_more_or_no_budget_raises_nothing(service):
    # Act / Assert
    assert service.check_runway(6 * ONE_AUSD, 2 * ONE_AUSD) is None
    assert service.check_runway(0, 0.0, budget_exists=False) is None


def test_empty_allowance_is_critical(service):
    # Act
    alert = service.check_runway(allowance_remaining=0, avg_daily_spend=ONE_AUSD)

    # Assert
    assert alert.level == ALERT_CRITICAL


def test_agent_backoff_alerts_only_after_two_failures_and_per_agent(service):
    # Act
    single = service.agent_backoff("OptionsEvent", 1, "insufficient_balance")
    double = service.agent_backoff("OptionsEvent", 2, "insufficient_balance")
    other_agent = service.agent_backoff("MacroFilings", 2, "bad_signature")

    # Assert
    assert single is None
    assert double.kind == KIND_AGENT_PAUSED
    assert other_agent is not None


def test_daily_cap_reached_raises_one_info_alert_per_day(service):
    # Act
    below = service.check_daily_cap(spent_today=900_000, daily_cap=ONE_AUSD, day="2026-10-11")
    reached = service.check_daily_cap(spent_today=ONE_AUSD, daily_cap=ONE_AUSD, day="2026-10-11")
    repeat = service.check_daily_cap(spent_today=ONE_AUSD, daily_cap=ONE_AUSD, day="2026-10-11")

    # Assert
    assert below is None
    assert reached.kind == KIND_DAILY_CAP_REACHED and reached.level == ALERT_INFO
    assert repeat is None


def test_allowance_drop_explained_by_our_own_recharges_is_not_a_revocation(service):
    # Act: 10 → 8 AUSD after PocketDesk pulled 2 AUSD
    alert = service.check_revocation(10 * ONE_AUSD, 8 * ONE_AUSD, pulled_since=2 * ONE_AUSD)

    # Assert
    assert alert is None


def test_allowance_to_zero_is_a_critical_revocation(service):
    # Act
    alert = service.check_revocation(10 * ONE_AUSD, 0, pulled_since=ONE_AUSD)

    # Assert
    assert alert.kind == KIND_REVOCATION
    assert alert.level == ALERT_CRITICAL


def test_allowance_reduced_outside_pocketdesk_is_a_warning(service):
    # Act
    alert = service.check_revocation(10 * ONE_AUSD, 3 * ONE_AUSD, pulled_since=ONE_AUSD)

    # Assert
    assert alert.kind == KIND_ALLOWANCE_REDUCED
    assert "expected $9.00" in alert.message


def test_counterparty_frozen_and_voucher_failed_alerts(service, sent):
    # Act
    frozen = service.counterparty_frozen("OptionsEvent", "0xAbC0000000000000000000000000000000000001", "payTo")
    voucher = service.voucher_failed(20_372, "receiveWithAuthorization reverted", tx_hash="0x" + "12" * 32)

    # Assert
    assert frozen.kind == KIND_COUNTERPARTY_FROZEN and frozen.level == ALERT_WARN
    assert voucher.kind == KIND_VOUCHER_FAILED and voucher.level == ALERT_CRITICAL
    assert len(sent) == 2


# ─── circuit breaker hook ────────────────────────────────────────────────────

def test_breaker_trip_raises_token_paused_and_reprobe_raises_resumed(engine, clock, sent):
    # Arrange
    service = AlertService(engine, notify=sent.append, clock=clock)
    breaker = CircuitBreaker(on_change=service.on_breaker_change)

    # Act
    breaker.trip_global("pre-flight: EnforcedPause()")
    breaker.reprobe(lambda: True)

    # Assert
    assert _stored_kinds(engine) == [KIND_TOKEN_RESUMED, KIND_TOKEN_PAUSED]
    assert sent == [f"[CRITICAL] {TOKEN_PAUSED_BANNER}: pre-flight: EnforcedPause()"]


def test_breaker_agent_failures_raise_agent_paused_after_the_second_failure(engine, clock):
    # Arrange
    service = AlertService(engine, clock=clock)
    breaker = CircuitBreaker(on_change=service.on_breaker_change)

    # Act
    breaker.record_failure("NewsSentiment", "insufficient_balance")
    kinds_after_one = _stored_kinds(engine)
    breaker.record_failure("NewsSentiment", "insufficient_balance")

    # Assert
    assert kinds_after_one == []
    assert _stored_kinds(engine) == [KIND_AGENT_PAUSED]


# ─── daily brief ─────────────────────────────────────────────────────────────

def _purchase(status: str, agent: str, price: int, reason: str = "") -> Purchase:
    return Purchase(ts=T0, agent=agent, seller_id="expected_move", asset="BTC", price_atomic=price,
                    status=status, reason=reason)


def _finding(headline: str, delta_p: float, receipts: int) -> Finding:
    return Finding(
        ts=T0, agent="OptionsEvent", asset="BTC", decision="direction", headline=headline,
        direction="bullish", confidence=0.6, delta_p=delta_p,
        receipts=[{"seller_id": "expected_move", "tx_hash": "0x" + "ab" * 32, "price_atomic": 50_000}] * receipts,
    )


def test_compose_daily_brief_sums_spend_counts_decisions_and_ranks_findings():
    # Arrange
    purchases = [
        _purchase(PURCHASE_BOUGHT, "OptionsEvent", 50_000),
        _purchase(PURCHASE_BOUGHT, "OptionsEvent", 100_000),
        _purchase(PURCHASE_BOUGHT, "MacroFilings", 20_000),
        _purchase(PURCHASE_SKIPPED, "NewsSentiment", 30_000, "EVOI below 1.5 × price"),
        _purchase(PURCHASE_SKIPPED, "NewsSentiment", 30_000, "EVOI below 1.5 × price"),
        _purchase(PURCHASE_FAILED, "OnchainPerps", 20_000, "pre-flight: insufficient_balance"),
    ]
    findings = [_finding("small move", 0.02, 1), _finding("big move", -0.12, 2)]
    savings = compute_savings(170_000, [{"name": "Feed", "monthly_usd": 31.0}], 0, T0)

    # Act
    brief = compose_daily_brief("2026-10-11", purchases, findings, savings)

    # Assert
    assert brief.spend_by_agent == {"OptionsEvent": 150_000, "MacroFilings": 20_000}
    assert (brief.bought, brief.skipped, brief.failed) == (3, 2, 1)
    assert brief.skipped_value_atomic == 60_000
    assert brief.top_findings == ("big move (2 receipts)", "small move (1 receipt)")
    text = brief.text()
    assert "Spend: $0.17 (OptionsEvent $0.15, MacroFilings $0.02); 3 bought, 2 skipped, 1 failed" in text
    assert "VOI gate: skipped 2 purchases worth $0.06 (EVOI below 1.5 × price ×2)" in text
    assert "Savings: Spent $0.17 this month vs $11.00 pro-rated subscriptions" in text


def test_build_daily_brief_reads_the_session_window_from_the_database(engine):
    # Arrange
    with session_scope(engine) as session:
        save_api_settings(session, {"subscriptions": [{"name": "Feed", "monthly_usd": 31.0}]})
        add_purchase(session, _purchase(PURCHASE_BOUGHT, "OptionsEvent", 50_000))
        add_finding(session, _finding("BTC: bullish", 0.08, 1))

    # Act
    brief = build_daily_brief(engine, T0 - timedelta(hours=1), T0 + timedelta(hours=1), now=T0)

    # Assert
    assert brief.day == "2026-10-11"
    assert brief.spend_by_agent == {"OptionsEvent": 50_000}
    assert brief.top_findings == ("BTC: bullish (1 receipt)",)
    assert "saved $10.95" in brief.savings_line


def test_send_daily_brief_forwards_info_level_brief_to_telegram_once_per_day(service, sent):
    # Arrange
    savings = compute_savings(0, [], 0, T0)
    brief = compose_daily_brief("2026-10-11", [], [], savings)

    # Act
    first = service.send_daily_brief(brief)
    second = service.send_daily_brief(brief)

    # Assert
    assert first is not None and second is None
    assert sent == [brief.text()]
    assert sent[0].startswith("PocketDesk daily brief — 2026-10-11")
