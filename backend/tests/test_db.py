"""
Tests for app/db.py: tables, to_api() JSON shapes (docs/API.md) and repository functions.

Every test uses a fresh in-memory SQLite database, so tests are independent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.constants import AGENT_NAMES, ONE_AUSD
from app.db import (
    ALERT_WARN,
    PURCHASE_BOUGHT,
    PURCHASE_FAILED,
    PURCHASE_SKIPPED,
    VOUCHER_REDEEMED,
    Alert,
    Finding,
    Purchase,
    Voucher,
    add_alert,
    add_allocation_run,
    add_finding,
    add_purchase,
    add_voucher,
    daily_spend_totals,
    default_api_settings,
    findings_between,
    init_db,
    iso_utc,
    last_alert_with_key,
    latest_allocations,
    load_api_settings,
    month_to_date_spend,
    purchases_between,
    recent_alerts,
    recent_findings,
    recent_purchases,
    save_api_settings,
    session_scope,
    set_voucher_status,
    spend_by_agent_on,
    voucher_for_day,
    vouchers_for_owner,
)
from app.keeper.allocator import Allocation as AllocatorAllocation

PURCHASE_API_KEYS = {
    "id", "ts", "agent", "seller_id", "asset", "params", "price_atomic", "evoi_usd", "status",
    "reason", "delta_p", "tx_hash", "payer", "pay_to", "live_vendor",
}
FINDING_API_KEYS = {
    "id", "ts", "agent", "asset", "decision", "headline", "direction", "confidence", "delta_p",
    "summary", "receipts",
}
SETTINGS_API_KEYS = {
    "owner", "watchlist", "stakes_usd", "session_hours", "plan_days", "monthly_cap_atomic",
    "daily_cap_atomic", "total_budget_atomic", "agent_caps_atomic", "subscriptions",
}
TX_HASH = "0x" + "ab" * 32
PAYER = "0x1111111111111111111111111111111111111111"
PAY_TO = "0x2222222222222222222222222222222222222222"
NOON = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine():
    return init_db("sqlite://")


def _bought(agent: str = "OptionsEvent", price: int = 50_000, ts: datetime = NOON) -> Purchase:
    return Purchase(
        ts=ts,
        agent=agent,
        seller_id="expected_move",
        asset="BTC",
        params={"asset": "BTC", "expiry": "next"},
        price_atomic=price,
        evoi_usd=0.12,
        status=PURCHASE_BOUGHT,
        reason="EVOI $0.120 ≥ 1.5 × $0.05",
        delta_p=0.04,
        tx_hash=TX_HASH,
        payer=PAYER,
        pay_to=PAY_TO,
        compliance_checked=True,
        preflight="ok",
    )


def _skipped(ts: datetime = NOON) -> Purchase:
    return Purchase(
        ts=ts, agent="NewsSentiment", seller_id="news_sentiment", asset="ETH",
        price_atomic=30_000, evoi_usd=0.01, status=PURCHASE_SKIPPED, reason="skip_low_evoi",
    )


# ─── purchases and the ledger ────────────────────────────────────────────────

def test_purchase_to_api_has_every_api_md_field_and_update1_fields(engine):
    # Arrange
    with session_scope(engine) as session:
        stored = add_purchase(session, _bought())

    # Act
    payload = stored.to_api()

    # Assert
    assert PURCHASE_API_KEYS <= set(payload)
    assert payload["compliance_checked"] is True
    assert payload["preflight"] == "ok"
    assert payload["token"] == "AUSD"
    assert payload["network"] == "eip155:84532"
    assert payload["params"] == {"asset": "BTC", "expiry": "next"}
    assert payload["ts"] == "2026-10-11T12:00:00+00:00"


def test_bought_purchase_writes_one_ledger_row_and_skipped_writes_none(engine):
    # Arrange / Act
    with session_scope(engine) as session:
        add_purchase(session, _bought(price=50_000))
        add_purchase(session, _skipped())

    # Assert
    with session_scope(engine) as session:
        assert spend_by_agent_on(session, "2026-10-11") == {"OptionsEvent": 50_000}


def test_failed_purchase_is_stored_but_not_counted_as_spend(engine):
    # Arrange
    failed = _bought()
    failed.status = PURCHASE_FAILED
    failed.tx_hash = None
    failed.reason = "pre-flight: insufficient_balance"

    # Act
    with session_scope(engine) as session:
        add_purchase(session, failed)

    # Assert
    with session_scope(engine) as session:
        assert [row.status for row in recent_purchases(session)] == [PURCHASE_FAILED]
        assert spend_by_agent_on(session, "2026-10-11") == {}


def test_add_purchase_rejects_unknown_status(engine):
    # Arrange
    purchase = _skipped()
    purchase.status = "maybe"

    # Act / Assert
    with pytest.raises(ValueError, match="unknown purchase status"):
        with session_scope(engine) as session:
            add_purchase(session, purchase)


def test_recent_purchases_returns_newest_first_and_respects_limit(engine):
    # Arrange
    with session_scope(engine) as session:
        for minute in range(5):
            add_purchase(session, _skipped(ts=NOON + timedelta(minutes=minute)))

    # Act
    with session_scope(engine) as session:
        rows = recent_purchases(session, limit=3)

    # Assert
    assert [row.ts.minute for row in rows] == [4, 3, 2]


def test_purchases_between_is_half_open_and_oldest_first(engine):
    # Arrange
    with session_scope(engine) as session:
        add_purchase(session, _skipped(ts=NOON - timedelta(hours=1)))
        add_purchase(session, _skipped(ts=NOON))
        add_purchase(session, _skipped(ts=NOON + timedelta(hours=1)))

    # Act
    with session_scope(engine) as session:
        rows = purchases_between(session, NOON - timedelta(hours=1), NOON + timedelta(hours=1))

    # Assert
    assert [row.ts for row in rows] == [NOON - timedelta(hours=1), NOON]


def test_month_to_date_spend_counts_only_this_utc_month(engine):
    # Arrange
    with session_scope(engine) as session:
        add_purchase(session, _bought(price=ONE_AUSD, ts=datetime(2026, 9, 30, 23, 59, tzinfo=UTC)))
        add_purchase(session, _bought(price=200_000, ts=datetime(2026, 10, 1, 0, 0, tzinfo=UTC)))
        add_purchase(session, _bought(price=300_000, ts=datetime(2026, 10, 11, 9, 0, tzinfo=UTC)))

    # Act
    with session_scope(engine) as session:
        spent = month_to_date_spend(session, datetime(2026, 10, 11, 12, 0, tzinfo=UTC))

    # Assert
    assert spent == 500_000


def test_daily_spend_totals_lists_previous_days_oldest_first_with_zeros(engine):
    # Arrange
    with session_scope(engine) as session:
        add_purchase(session, _bought(price=100_000, ts=datetime(2026, 10, 8, 10, tzinfo=UTC)))
        add_purchase(session, _bought(price=250_000, ts=datetime(2026, 10, 10, 10, tzinfo=UTC)))
        add_purchase(session, _bought(price=999_000, ts=datetime(2026, 10, 11, 10, tzinfo=UTC)))  # today: excluded

    # Act
    with session_scope(engine) as session:
        totals = daily_spend_totals(session, days=3, today=date(2026, 10, 11))

    # Assert
    assert totals == [100_000, 0, 250_000]


# ─── findings ────────────────────────────────────────────────────────────────

def test_finding_round_trip_keeps_receipts_and_matches_api_shape(engine):
    # Arrange
    receipts = [{"seller_id": "expected_move", "tx_hash": TX_HASH, "price_atomic": 50_000}]
    finding = Finding(
        ts=NOON, agent="OptionsEvent", asset="NVDA", decision="above:200", headline="NVDA: bullish",
        direction="bullish", confidence=0.64, delta_p=0.09, summary="…", receipts=receipts,
    )

    # Act
    with session_scope(engine) as session:
        add_finding(session, finding)
    with session_scope(engine) as session:
        payload = recent_findings(session)[0].to_api()

    # Assert
    assert set(payload) == FINDING_API_KEYS
    assert payload["receipts"] == receipts


def test_findings_between_selects_the_window(engine):
    # Arrange
    with session_scope(engine) as session:
        for hours in (-2, 0, 2):
            add_finding(session, Finding(
                ts=NOON + timedelta(hours=hours), agent="MacroFilings", asset="BTC", decision="direction",
                headline="h", direction="neutral", confidence=0.5, delta_p=0.0,
            ))

    # Act
    with session_scope(engine) as session:
        rows = findings_between(session, NOON - timedelta(hours=1), NOON + timedelta(hours=1))

    # Assert
    assert [row.ts for row in rows] == [NOON]


# ─── allocations ─────────────────────────────────────────────────────────────

def test_latest_allocations_returns_only_the_newest_run_in_api_shape(engine):
    # Arrange
    old_run = [AllocatorAllocation("OptionsEvent", 1_000_000, 1.0, ("only agent",))]
    new_run = [
        AllocatorAllocation("OptionsEvent", 600_000, 0.6, ("OptionsEvent +20%: NVDA earnings today",)),
        AllocatorAllocation("MacroFilings", 400_000, 0.4, ()),
    ]
    with session_scope(engine) as session:
        add_allocation_run(session, "2026-10-10", 1_000_000, old_run)
    with session_scope(engine) as session:
        add_allocation_run(session, "2026-10-11", 1_000_000, new_run)

    # Act
    with session_scope(engine) as session:
        payload = latest_allocations(session)

    # Assert
    assert payload == {
        "date": "2026-10-11",
        "budget_atomic": 1_000_000,
        "allocations": [
            {"agent": "OptionsEvent", "amount_atomic": 600_000, "weight": 0.6,
             "reasons": ["OptionsEvent +20%: NVDA earnings today"]},
            {"agent": "MacroFilings", "amount_atomic": 400_000, "weight": 0.4, "reasons": []},
        ],
    }


def test_latest_allocations_is_none_before_the_first_run(engine):
    # Act
    with session_scope(engine) as session:
        payload = latest_allocations(session)

    # Assert
    assert payload is None


# ─── alerts ──────────────────────────────────────────────────────────────────

def test_alert_to_api_matches_api_md_and_last_alert_with_key_finds_newest(engine):
    # Arrange
    with session_scope(engine) as session:
        add_alert(session, Alert(ts=NOON, level=ALERT_WARN, kind="runway_low", message="old", dedupe_key="runway"))
        add_alert(session, Alert(ts=NOON + timedelta(minutes=1), level=ALERT_WARN, kind="runway_low",
                                 message="new", dedupe_key="runway"))

    # Act
    with session_scope(engine) as session:
        newest = last_alert_with_key(session, "runway")
        listed = recent_alerts(session)

    # Assert
    assert newest is not None and newest.message == "new"
    assert set(listed[0].to_api()) == {"id", "ts", "level", "kind", "message"}


def test_add_alert_rejects_unknown_level(engine):
    # Act / Assert
    with pytest.raises(ValueError, match="unknown alert level"):
        with session_scope(engine) as session:
            add_alert(session, Alert(level="panic", kind="x", message="y"))


# ─── vouchers ────────────────────────────────────────────────────────────────

def test_voucher_lifecycle_signed_then_redeemed(engine):
    # Arrange
    voucher = Voucher(
        owner=PAYER, day_index=20_372, value=7 * ONE_AUSD, valid_after=1_760_140_799,
        valid_before=1_760_227_200, nonce="0x" + "01" * 32, signature="0x" + "02" * 65,
    )
    with session_scope(engine) as session:
        stored = add_voucher(session, voucher)

    # Act
    with session_scope(engine) as session:
        set_voucher_status(session, stored.id, VOUCHER_REDEEMED, tx_hash=TX_HASH)

    # Assert
    with session_scope(engine) as session:
        assert voucher_for_day(session, PAYER, 20_372).status == VOUCHER_REDEEMED
        assert vouchers_for_owner(session, PAYER)[0].to_api()["tx_hash"] == TX_HASH


def test_set_voucher_status_raises_for_unknown_voucher(engine):
    # Act / Assert
    with pytest.raises(KeyError):
        with session_scope(engine) as session:
            set_voucher_status(session, 999, VOUCHER_REDEEMED)


# ─── settings ────────────────────────────────────────────────────────────────

def test_load_api_settings_returns_defaults_with_every_api_key_and_no_subscriptions(engine):
    # Act
    with session_scope(engine) as session:
        settings = load_api_settings(session)

    # Assert
    assert set(settings) == SETTINGS_API_KEYS
    assert settings["subscriptions"] == []
    assert set(settings["agent_caps_atomic"]) == set(AGENT_NAMES)


def test_save_api_settings_merges_partial_update_and_ignores_unknown_keys(engine):
    # Arrange
    update = {"watchlist": ["BTC", "AAPL"], "subscriptions": [{"name": "Data terminal", "monthly_usd": 30.0}],
              "private_key": "must never be stored"}

    # Act
    with session_scope(engine) as session:
        save_api_settings(session, update)
    with session_scope(engine) as session:
        settings = load_api_settings(session)

    # Assert
    assert settings["watchlist"] == ["BTC", "AAPL"]
    assert settings["subscriptions"] == [{"name": "Data terminal", "monthly_usd": 30.0}]
    assert settings["plan_days"] == default_api_settings()["plan_days"]
    assert "private_key" not in settings


def test_iso_utc_renders_naive_and_aware_datetimes_as_the_same_utc_instant():
    # Arrange
    naive = datetime(2026, 10, 11, 12, 0)
    aware = datetime(2026, 10, 11, 21, 0, tzinfo=UTC).astimezone()  # any local zone

    # Act / Assert
    assert iso_utc(naive) == "2026-10-11T12:00:00+00:00"
    assert iso_utc(aware) == "2026-10-11T21:00:00+00:00"
    assert iso_utc(None) is None
