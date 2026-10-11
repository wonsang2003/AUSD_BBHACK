"""
SQLite persistence for PocketDesk (SQLModel tables + small typed repository functions).

What is stored, and who writes it:

    Purchase     every VOI decision of every agent: bought, skipped AND failed, with the
                 numbers behind it (EVOI, price, Δp, tx hash, compliance + pre-flight result)
                 → GET /api/purchases, the `purchase` SSE event, the daily brief
    SpendLedger  one row per AUSD amount that actually left an agent wallet (a bought
                 purchase). Month-to-date spend, today's spend per agent and the average
                 daily spend (runway) are sums over this table.
    Finding      what an agent concluded, with receipts (tx hashes)  → GET /api/findings
    Allocation   one row per agent per allocator run                  → GET /api/allocations
    Alert        dashboard / Telegram alerts (deduplicated by `dedupe_key`)  → GET /api/alerts
    Voucher      "maximum safety" daily ReceiveWithAuthorization vouchers (SPEC §9, optional)
    SettingsRow  one JSON blob: the user's API Settings (watchlist, stakes, caps, subscriptions)

Every table has `to_api()` that returns exactly the JSON shape in docs/API.md, so the
FastAPI layer never re-shapes rows. Amounts are AUSD atomic integers (6 decimals).

Timestamps are timezone-aware UTC datetimes (SQLModel refuses naive ones) and are rendered
as ISO-8601 strings with an explicit "+00:00" by `iso_utc`.

Usage:
    engine = init_db(get_settings().database_url)
    with session_scope(engine) as session:
        add_purchase(session, Purchase(agent="OptionsEvent", seller_id="expected_move", ...))
        rows = recent_purchases(session, limit=100)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import JSON, Column
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, Session, SQLModel, col, create_engine, select

from app.constants import AGENT_NAMES, BASE_SEPOLIA_CAIP2, ONE_AUSD
from app.keeper.allocator import Allocation as AllocatorAllocation

# ─── constants ────────────────────────────────────────────────────────────────

PURCHASE_BOUGHT = "bought"
PURCHASE_SKIPPED = "skipped"
PURCHASE_FAILED = "failed"
PURCHASE_STATUSES = (PURCHASE_BOUGHT, PURCHASE_SKIPPED, PURCHASE_FAILED)

ALERT_INFO = "info"
ALERT_WARN = "warn"
ALERT_CRITICAL = "critical"
ALERT_LEVELS = (ALERT_INFO, ALERT_WARN, ALERT_CRITICAL)

VOUCHER_SIGNED = "signed"         # signed by the user, waiting for its UTC day
VOUCHER_REDEEMED = "redeemed"     # redeemVoucher succeeded (tx_hash set)
VOUCHER_FAILED = "failed"         # redeem reverted / was rejected
VOUCHER_CANCELLED = "cancelled"   # cancelAuthorization sent for a future day

DEFAULT_TOKEN_SYMBOL = "AUSD"
DEFAULT_PURCHASE_LIMIT = 100      # GET /api/purchases default
DEFAULT_FINDING_LIMIT = 50        # GET /api/findings default
DEFAULT_ALERT_LIMIT = 50          # GET /api/alerts default
SETTINGS_ROW_ID = 1               # the settings table holds exactly one row
IN_MEMORY_SQLITE_URLS = ("sqlite://", "sqlite:///:memory:")
DATE_FORMAT = "%Y-%m-%d"

# Defaults for the user's API Settings (docs/API.md "Settings"). All user-editable.
# Subscriptions are empty on purpose: the savings meter only uses what the user enters.
DEFAULT_WATCHLIST = ["BTC", "ETH", "SOL", "NVDA"]
DEFAULT_STAKE_USD = 1_000.0
DEFAULT_SESSION_HOURS = {"start": "09:30", "end": "16:00", "tz": "America/New_York"}
DEFAULT_PLAN_DAYS = 30
DEFAULT_DAILY_CAP_ATOMIC = 2 * ONE_AUSD
DEFAULT_AGENT_CAP_ATOMIC = 1 * ONE_AUSD
DEFAULT_MONTHLY_CAP_ATOMIC = DEFAULT_DAILY_CAP_ATOMIC * DEFAULT_PLAN_DAYS
DEFAULT_TOTAL_BUDGET_ATOMIC = DEFAULT_MONTHLY_CAP_ATOMIC


# ─── time helpers ─────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime (the form stored in every table)."""
    return datetime.now(UTC)


def to_utc(moment: datetime) -> datetime:
    """Convert any datetime to aware UTC; a naive input is assumed to be UTC already."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def iso_utc(moment: datetime | None) -> str | None:
    """Datetime → "2026-10-11T21:39:37.123456+00:00" (naive = UTC); None stays None."""
    if moment is None:
        return None
    return to_utc(moment).isoformat()


def day_key(moment: datetime) -> str:
    """The UTC calendar day of `moment` as "YYYY-MM-DD" (the on-chain day boundary is UTC)."""
    return to_utc(moment).strftime(DATE_FORMAT)


def _new_run_id() -> str:
    """A random id that groups the rows of one allocator run."""
    return uuid.uuid4().hex


# ─── tables ───────────────────────────────────────────────────────────────────

class Purchase(SQLModel, table=True):
    """One VOI decision: a purchase that was bought, skipped (with the reason) or failed."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utc_now, index=True)
    agent: str = Field(index=True)
    seller_id: str
    asset: str = ""
    params: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    price_atomic: int = 0
    evoi_usd: float = 0.0
    status: str = Field(default=PURCHASE_SKIPPED, index=True)   # bought | skipped | failed
    reason: str = ""
    delta_p: float | None = None          # realized |Δp| (bought only)
    tx_hash: str | None = None
    payer: str | None = None
    pay_to: str | None = None
    live_vendor: bool = False
    token: str = DEFAULT_TOKEN_SYMBOL     # registry symbol the payment was signed for
    network: str = BASE_SEPOLIA_CAIP2     # CAIP-2 network id
    compliance_checked: bool = False      # isAccountFrozen(payTo) and (payer) both checked
    preflight: str | None = None          # "ok" or the PreflightResult kind ("paused", "frozen", ...)

    def to_api(self) -> dict[str, Any]:
        """docs/API.md "Purchase" (+ Update #1 fields token, network, compliance_checked, preflight)."""
        return {
            "id": self.id,
            "ts": iso_utc(self.ts),
            "agent": self.agent,
            "seller_id": self.seller_id,
            "asset": self.asset,
            "params": dict(self.params or {}),
            "price_atomic": self.price_atomic,
            "evoi_usd": self.evoi_usd,
            "status": self.status,
            "reason": self.reason,
            "delta_p": self.delta_p,
            "tx_hash": self.tx_hash,
            "payer": self.payer,
            "pay_to": self.pay_to,
            "live_vendor": self.live_vendor,
            "token": self.token,
            "network": self.network,
            "compliance_checked": self.compliance_checked,
            "preflight": self.preflight,
        }


class SpendLedger(SQLModel, table=True):
    """One AUSD amount that left an agent wallet (written for every bought purchase)."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utc_now, index=True)
    day: str = Field(index=True)          # UTC day "YYYY-MM-DD"
    agent: str = Field(index=True)
    amount_atomic: int
    seller_id: str = ""
    tx_hash: str | None = None
    purchase_id: int | None = None


class Finding(SQLModel, table=True):
    """What an agent concluded after research, with the receipts that paid for it."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utc_now, index=True)
    agent: str = Field(index=True)
    asset: str
    decision: str
    headline: str
    direction: str                         # bullish | bearish | neutral | risk
    confidence: float                      # the belief p after the update
    delta_p: float
    summary: str = ""
    receipts: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))

    def to_api(self) -> dict[str, Any]:
        """docs/API.md "Finding"."""
        return {
            "id": self.id,
            "ts": iso_utc(self.ts),
            "agent": self.agent,
            "asset": self.asset,
            "decision": self.decision,
            "headline": self.headline,
            "direction": self.direction,
            "confidence": self.confidence,
            "delta_p": self.delta_p,
            "summary": self.summary,
            "receipts": list(self.receipts or []),
        }


class Allocation(SQLModel, table=True):
    """One agent's share of one allocator run (rows of the same run share `run_id`)."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utc_now, index=True)
    run_id: str = Field(index=True)
    date: str                              # the budget day "YYYY-MM-DD"
    budget_atomic: int                     # B_day of the run
    agent: str
    amount_atomic: int
    weight: float
    reasons: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    def to_api(self) -> dict[str, Any]:
        """One entry of docs/API.md `allocations: [{agent, amount_atomic, weight, reasons}]`."""
        return {
            "agent": self.agent,
            "amount_atomic": self.amount_atomic,
            "weight": self.weight,
            "reasons": list(self.reasons or []),
        }


class Alert(SQLModel, table=True):
    """A dashboard alert (also forwarded to Telegram by AlertService for warn/critical)."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utc_now, index=True)
    level: str                             # info | warn | critical
    kind: str = Field(index=True)          # e.g. "runway_low", "token_paused"
    message: str
    dedupe_key: str = Field(default="", index=True)

    def to_api(self) -> dict[str, Any]:
        """docs/API.md alert row `{id, ts, level, kind, message}`."""
        return {
            "id": self.id,
            "ts": iso_utc(self.ts),
            "level": self.level,
            "kind": self.kind,
            "message": self.message,
        }


class Voucher(SQLModel, table=True):
    """One signed daily ReceiveWithAuthorization voucher (SPEC §9 "Daily vouchers")."""

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=utc_now)
    owner: str = Field(index=True)
    day_index: int = Field(index=True)     # block.timestamp // 1 day of the voucher's UTC day
    value: int                             # AUSD atomic units
    valid_after: int                       # unix seconds (dayStart − 1)
    valid_before: int                      # unix seconds (dayEnd)
    nonce: str                             # 0x-prefixed bytes32
    signature: str                         # 0x-prefixed 65-byte signature
    status: str = VOUCHER_SIGNED
    tx_hash: str | None = None

    def to_api(self) -> dict[str, Any]:
        """JSON form for the dashboard (the signature is public data once signed)."""
        return {
            "id": self.id,
            "created_at": iso_utc(self.created_at),
            "owner": self.owner,
            "day_index": self.day_index,
            "value": self.value,
            "valid_after": self.valid_after,
            "valid_before": self.valid_before,
            "nonce": self.nonce,
            "signature": self.signature,
            "status": self.status,
            "tx_hash": self.tx_hash,
        }


class SettingsRow(SQLModel, table=True):
    """The single row holding the user's API Settings as one JSON blob."""

    id: int = Field(default=SETTINGS_ROW_ID, primary_key=True)
    updated_at: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


# ─── engine and sessions ──────────────────────────────────────────────────────

_engine: Engine | None = None


def init_db(url: str) -> Engine:
    """
    Create the engine for `url`, create any missing tables, and remember it as the app engine.

    SQLite is opened with `check_same_thread=False` because scheduler threads and the
    FastAPI event loop share it; an in-memory URL uses one shared connection (StaticPool)
    so every session sees the same database.
    """
    global _engine
    engine_kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    if url in IN_MEMORY_SQLITE_URLS:
        engine_kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **engine_kwargs)
    SQLModel.metadata.create_all(engine)
    _engine = engine
    return engine


def get_engine() -> Engine:
    """The engine created by the last `init_db` call."""
    if _engine is None:
        raise RuntimeError("database not initialised: call init_db(url) at startup")
    return _engine


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """
    One unit of work: commit on success, roll back on error, always close.

    Objects stay readable after the block (`expire_on_commit=False`), so a repository
    function can return rows that the caller serialises with `to_api()` later.
    """
    session = Session(engine or get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ─── purchases and the spend ledger ───────────────────────────────────────────

def add_purchase(session: Session, purchase: Purchase) -> Purchase:
    """
    Store one decision. Rule: a purchase with status "bought" and a positive price also
    writes one SpendLedger row, so the ledger always equals the sum of bought purchases.
    """
    if purchase.status not in PURCHASE_STATUSES:
        raise ValueError(f"unknown purchase status {purchase.status!r}")
    purchase.ts = to_utc(purchase.ts)
    session.add(purchase)
    session.flush()  # assigns purchase.id
    if purchase.status == PURCHASE_BOUGHT and purchase.price_atomic > 0:
        session.add(
            SpendLedger(
                ts=purchase.ts,
                day=day_key(purchase.ts),
                agent=purchase.agent,
                amount_atomic=purchase.price_atomic,
                seller_id=purchase.seller_id,
                tx_hash=purchase.tx_hash,
                purchase_id=purchase.id,
            )
        )
    return purchase


def recent_purchases(session: Session, limit: int = DEFAULT_PURCHASE_LIMIT) -> list[Purchase]:
    """Newest first, all statuses (bought, skipped, failed)."""
    statement = select(Purchase).order_by(col(Purchase.ts).desc(), col(Purchase.id).desc()).limit(limit)
    return list(session.exec(statement))


def purchases_between(session: Session, start: datetime, end: datetime) -> list[Purchase]:
    """All decisions with start ≤ ts < end, oldest first (used by the daily brief)."""
    statement = (
        select(Purchase)
        .where(Purchase.ts >= to_utc(start), Purchase.ts < to_utc(end))
        .order_by(col(Purchase.ts), col(Purchase.id))
    )
    return list(session.exec(statement))


def spend_by_agent_on(session: Session, day: str) -> dict[str, int]:
    """Σ SpendLedger.amount_atomic per agent for one UTC day "YYYY-MM-DD"."""
    totals: dict[str, int] = {}
    for row in session.exec(select(SpendLedger).where(SpendLedger.day == day)):
        totals[row.agent] = totals.get(row.agent, 0) + row.amount_atomic
    return totals


def month_to_date_spend(session: Session, now: datetime) -> int:
    """Σ spend from 00:00 UTC on the 1st of `now`'s month up to `now` (AUSD atomic)."""
    now_utc = to_utc(now)
    month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    statement = select(SpendLedger).where(SpendLedger.ts >= month_start, SpendLedger.ts <= now_utc)
    return sum(row.amount_atomic for row in session.exec(statement))


def daily_spend_totals(session: Session, days: int, today: date) -> list[int]:
    """
    Total spend of each of the `days` UTC days before `today` (oldest first, zeros included).

    Feed this to `keeper.recharge_policy.average_daily_spend` for the runway estimate.
    Today is excluded because it is not over yet.
    """
    day_keys = [(today - timedelta(days=offset)).strftime(DATE_FORMAT) for offset in range(days, 0, -1)]
    totals = dict.fromkeys(day_keys, 0)
    statement = select(SpendLedger).where(col(SpendLedger.day).in_(day_keys))
    for row in session.exec(statement):
        totals[row.day] += row.amount_atomic
    return [totals[key] for key in day_keys]


# ─── findings ─────────────────────────────────────────────────────────────────

def add_finding(session: Session, finding: Finding) -> Finding:
    """Store one finding and return it with its id."""
    finding.ts = to_utc(finding.ts)
    session.add(finding)
    session.flush()
    return finding


def recent_findings(session: Session, limit: int = DEFAULT_FINDING_LIMIT) -> list[Finding]:
    """Newest first."""
    statement = select(Finding).order_by(col(Finding.ts).desc(), col(Finding.id).desc()).limit(limit)
    return list(session.exec(statement))


def findings_between(session: Session, start: datetime, end: datetime) -> list[Finding]:
    """All findings with start ≤ ts < end, oldest first."""
    statement = (
        select(Finding)
        .where(Finding.ts >= to_utc(start), Finding.ts < to_utc(end))
        .order_by(col(Finding.ts), col(Finding.id))
    )
    return list(session.exec(statement))


# ─── allocations ──────────────────────────────────────────────────────────────

def add_allocation_run(
    session: Session,
    day: str,
    budget_atomic: int,
    allocations: Sequence[AllocatorAllocation],
) -> str:
    """Store one allocator run (one row per agent) and return its run id."""
    run_id = _new_run_id()
    run_ts = utc_now()
    for allocation in allocations:
        session.add(
            Allocation(
                ts=run_ts,
                run_id=run_id,
                date=day,
                budget_atomic=budget_atomic,
                agent=allocation.agent,
                amount_atomic=allocation.amount_atomic,
                weight=allocation.weight,
                reasons=list(allocation.reasons),
            )
        )
    session.flush()
    return run_id


def latest_allocations(session: Session) -> dict[str, Any] | None:
    """docs/API.md GET /api/allocations: `{date, budget_atomic, allocations: [...]}` of the newest run."""
    newest = session.exec(select(Allocation).order_by(col(Allocation.ts).desc(), col(Allocation.id).desc())).first()
    if newest is None:
        return None
    rows = session.exec(select(Allocation).where(Allocation.run_id == newest.run_id).order_by(col(Allocation.id)))
    return {
        "date": newest.date,
        "budget_atomic": newest.budget_atomic,
        "allocations": [row.to_api() for row in rows],
    }


# ─── alerts ───────────────────────────────────────────────────────────────────

def add_alert(session: Session, alert: Alert) -> Alert:
    """Store one alert and return it with its id."""
    if alert.level not in ALERT_LEVELS:
        raise ValueError(f"unknown alert level {alert.level!r}")
    alert.ts = to_utc(alert.ts)
    session.add(alert)
    session.flush()
    return alert


def recent_alerts(session: Session, limit: int = DEFAULT_ALERT_LIMIT) -> list[Alert]:
    """Newest first."""
    statement = select(Alert).order_by(col(Alert.ts).desc(), col(Alert.id).desc()).limit(limit)
    return list(session.exec(statement))


def last_alert_with_key(session: Session, dedupe_key: str) -> Alert | None:
    """The newest alert with this dedupe key (AlertService uses it to suppress repeats)."""
    statement = (
        select(Alert)
        .where(Alert.dedupe_key == dedupe_key)
        .order_by(col(Alert.ts).desc(), col(Alert.id).desc())
    )
    return session.exec(statement).first()


# ─── vouchers ─────────────────────────────────────────────────────────────────

def add_voucher(session: Session, voucher: Voucher) -> Voucher:
    """Store one signed voucher."""
    session.add(voucher)
    session.flush()
    return voucher


def vouchers_for_owner(session: Session, owner: str) -> list[Voucher]:
    """All vouchers of one owner, by day."""
    statement = select(Voucher).where(Voucher.owner == owner).order_by(col(Voucher.day_index))
    return list(session.exec(statement))


def voucher_for_day(session: Session, owner: str, day_index: int) -> Voucher | None:
    """The owner's voucher for one UTC day index, if any."""
    statement = select(Voucher).where(Voucher.owner == owner, Voucher.day_index == day_index)
    return session.exec(statement).first()


def set_voucher_status(session: Session, voucher_id: int, status: str, tx_hash: str | None = None) -> Voucher:
    """Record what happened to a voucher (redeemed / failed / cancelled) and the tx, if any."""
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:
        raise KeyError(f"voucher {voucher_id} not found")
    voucher.status = status
    if tx_hash is not None:
        voucher.tx_hash = tx_hash
    session.add(voucher)
    session.flush()
    return voucher


# ─── settings ─────────────────────────────────────────────────────────────────

def default_api_settings() -> dict[str, Any]:
    """docs/API.md "Settings" with PocketDesk's defaults (a fresh copy each call)."""
    return {
        "owner": None,
        "watchlist": list(DEFAULT_WATCHLIST),
        "stakes_usd": dict.fromkeys(DEFAULT_WATCHLIST, DEFAULT_STAKE_USD),
        "session_hours": dict(DEFAULT_SESSION_HOURS),
        "plan_days": DEFAULT_PLAN_DAYS,
        "monthly_cap_atomic": DEFAULT_MONTHLY_CAP_ATOMIC,
        "daily_cap_atomic": DEFAULT_DAILY_CAP_ATOMIC,
        "total_budget_atomic": DEFAULT_TOTAL_BUDGET_ATOMIC,
        "agent_caps_atomic": dict.fromkeys(AGENT_NAMES, DEFAULT_AGENT_CAP_ATOMIC),
        "subscriptions": [],
    }


def merge_api_settings(current: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    """
    Apply a (partial) Settings update. Rule: only keys that exist in the Settings shape are
    taken; unknown keys are ignored, so a stray field can never end up in the stored blob.
    """
    merged = deepcopy(dict(current))
    for key in default_api_settings():
        if key in update:
            merged[key] = deepcopy(update[key])
    return merged


def load_api_settings(session: Session) -> dict[str, Any]:
    """The stored Settings, with defaults filled in for any key never saved."""
    row = session.get(SettingsRow, SETTINGS_ROW_ID)
    stored = row.data if row is not None else {}
    return merge_api_settings(default_api_settings(), stored)


def save_api_settings(session: Session, update: Mapping[str, Any]) -> dict[str, Any]:
    """Merge `update` into the stored Settings, save, and return the full result."""
    merged = merge_api_settings(load_api_settings(session), update)
    row = session.get(SettingsRow, SETTINGS_ROW_ID)
    if row is None:
        row = SettingsRow(id=SETTINGS_ROW_ID)
    row.data = merged
    row.updated_at = utc_now()
    session.add(row)
    session.flush()
    return deepcopy(merged)
