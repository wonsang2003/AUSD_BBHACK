"""
AlertService: the alert rules of SPEC §6 + Update #1, and the daily brief.

    rule check (keeper / agents / breaker) ──▶ raise_alert(level, kind, message, dedupe_key)
        1. dedupe: same key within the window (default 30 min) → suppressed, returns None
        2. persist: db.Alert row                                  → GET /api/alerts
        3. publish: `alert` event on the EventBus                 → dashboard (SSE)
        4. forward: warn/critical (and the daily brief) to Telegram via the injected `notify`

Rules (each method decides from numbers it is given; none of them reads the chain itself):

    check_runway         allowance_remaining / avg_daily_spend < 3 days          warn (0 days: critical)
    agent_backoff        an agent paused after ≥ 2 failed payments in a row      warn
    check_daily_cap      spent_today ≥ daily_cap                                  info
    check_revocation     allowance fell by more than PocketDesk itself pulled     critical (→ 0) / warn
    on_breaker_change    CircuitBreaker global trip ("TOKEN PAUSED — agents halted")  critical; resume: info
    counterparty_frozen  isAccountFrozen(payTo or payer) was true; payment skipped  warn
    voucher_failed       a daily voucher could not be redeemed                     critical

`on_breaker_change` has the signature of `CircuitBreaker.on_change`, so main.py can wire
`CircuitBreaker(on_change=alert_service.on_breaker_change)` directly.

Daily brief (`compose_daily_brief`, pure): spend by agent, bought/skipped/failed counts, the top
findings by |Δp|, what the VOI gate skipped, and the savings meter line.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.engine import Engine

from app.agents.voi import format_ausd
from app.db import (
    ALERT_CRITICAL,
    ALERT_INFO,
    ALERT_WARN,
    PURCHASE_BOUGHT,
    PURCHASE_FAILED,
    PURCHASE_SKIPPED,
    Alert,
    Finding,
    Purchase,
    add_alert,
    day_key,
    findings_between,
    last_alert_with_key,
    load_api_settings,
    month_to_date_spend,
    purchases_between,
    session_scope,
    utc_now,
)
from app.events import EVENT_ALERT, EventBus
from app.keeper.recharge_policy import RUNWAY_ALERT_DAYS, is_runway_low, runway_days
from app.savings import SavingsMeter, compute_savings, describe_savings

# ─── tuning ──────────────────────────────────────────────────────────────────

DEFAULT_DEDUPE_WINDOW_SECONDS = 30 * 60
LEVEL_RANK = {ALERT_INFO: 0, ALERT_WARN: 1, ALERT_CRITICAL: 2}
TELEGRAM_MIN_LEVEL = ALERT_WARN               # info alerts stay on the dashboard
AGENT_PAUSE_ALERT_MIN_FAILURES = 2            # "paused after failed payments" (plural)
BRIEF_TOP_FINDINGS = 3
BRIEF_TOP_SKIP_REASONS = 3
SKIP_REASON_MAX_CHARS = 60

# ─── alert kinds ─────────────────────────────────────────────────────────────

KIND_RUNWAY_LOW = "runway_low"
KIND_AGENT_PAUSED = "agent_paused"
KIND_DAILY_CAP_REACHED = "daily_cap_reached"
KIND_REVOCATION = "revocation_detected"
KIND_ALLOWANCE_REDUCED = "allowance_reduced"
KIND_TOKEN_PAUSED = "token_paused"
KIND_TOKEN_RESUMED = "token_resumed"
KIND_COUNTERPARTY_FROZEN = "counterparty_frozen"
KIND_VOUCHER_FAILED = "voucher_failed"
KIND_DAILY_BRIEF = "daily_brief"

TOKEN_PAUSED_BANNER = "TOKEN PAUSED — agents halted"


# ─── the service ─────────────────────────────────────────────────────────────

class AlertService:
    """Deduplicates, stores, publishes and forwards alerts. Safe to call from any thread."""

    def __init__(
        self,
        engine: Engine,
        bus: EventBus | None = None,
        notify: Callable[[str], None] | None = None,
        dedupe_window_seconds: float = DEFAULT_DEDUPE_WINDOW_SECONDS,
        clock: Callable[[], datetime] = utc_now,
        telegram_min_level: str = TELEGRAM_MIN_LEVEL,
    ):
        self._engine = engine
        self._bus = bus
        self._notify = notify                 # e.g. TelegramBot.notify
        self._dedupe_window = timedelta(seconds=dedupe_window_seconds)
        self._clock = clock
        self._telegram_min_rank = LEVEL_RANK[telegram_min_level]
        self._lock = threading.Lock()         # makes "check duplicate, then insert" atomic
        self._breaker_was_open = False

    # ─── core ─────────────────────────────────────────────────────────────
    def _is_duplicate(self, session: Any, dedupe_key: str, now: datetime) -> bool:
        """Rule: an alert with the same key inside the dedupe window is a duplicate."""
        previous = last_alert_with_key(session, dedupe_key)
        return previous is not None and now - previous.ts < self._dedupe_window

    def raise_alert(
        self,
        level: str,
        kind: str,
        message: str,
        dedupe_key: str | None = None,
        force_forward: bool = False,
    ) -> Alert | None:
        """
        Store + publish + forward one alert; returns it, or None when suppressed as a duplicate.
        `dedupe_key` defaults to `kind`; `force_forward` sends to Telegram whatever the level.
        """
        key = dedupe_key or kind
        now = self._clock()
        with self._lock, session_scope(self._engine) as session:
            if self._is_duplicate(session, key, now):
                return None
            alert = add_alert(session, Alert(ts=now, level=level, kind=kind, message=message, dedupe_key=key))
        if self._bus is not None:
            self._bus.publish(EVENT_ALERT, alert.to_api())
        if self._notify is not None and (force_forward or LEVEL_RANK[level] >= self._telegram_min_rank):
            self._notify(_telegram_text(alert))
        return alert

    # ─── rules ────────────────────────────────────────────────────────────
    def check_runway(
        self,
        allowance_remaining: int,
        avg_daily_spend: float,
        budget_exists: bool = True,
    ) -> Alert | None:
        """runway = allowance_remaining / avg_daily_spend; alert below 3 days (critical at 0)."""
        if not budget_exists:
            return None
        days = runway_days(allowance_remaining, avg_daily_spend)
        if not is_runway_low(days):
            return None
        if days <= 0:
            return self.raise_alert(ALERT_CRITICAL, KIND_RUNWAY_LOW,
                                    "Research budget allowance is used up: agents cannot be recharged.")
        return self.raise_alert(
            ALERT_WARN, KIND_RUNWAY_LOW,
            f"Runway {days:.1f} days (< {RUNWAY_ALERT_DAYS:g}): {format_ausd(allowance_remaining)} allowance left "
            f"at {format_ausd(round(avg_daily_spend))}/day. Top up the budget to keep research running.",
        )

    def agent_backoff(self, agent: str, failures: int, reason: str) -> Alert | None:
        """Alert once an agent is paused after ≥ 2 consecutive failed payments."""
        if failures < AGENT_PAUSE_ALERT_MIN_FAILURES:
            return None
        return self.raise_alert(
            ALERT_WARN, KIND_AGENT_PAUSED,
            f"{agent} paused after {failures} failed payments: {reason}",
            dedupe_key=f"{KIND_AGENT_PAUSED}:{agent}",
        )

    def check_daily_cap(self, spent_today: int, daily_cap: int, day: str) -> Alert | None:
        """Info alert (once per day) when today's spend reaches the on-chain daily cap."""
        if daily_cap <= 0 or spent_today < daily_cap:
            return None
        return self.raise_alert(
            ALERT_INFO, KIND_DAILY_CAP_REACHED,
            f"Daily cap reached: {format_ausd(spent_today)} of {format_ausd(daily_cap)} spent; "
            "buying resumes tomorrow.",
            dedupe_key=f"{KIND_DAILY_CAP_REACHED}:{day}",
        )

    def check_revocation(self, previous_allowance: int, current_allowance: int, pulled_since: int) -> Alert | None:
        """
        Rule: allowance_now < allowance_before − what PocketDesk pulled → the user changed the
        permit. Down to 0 → "revoked" (critical); otherwise "reduced" (warn).
        """
        expected_allowance = previous_allowance - pulled_since
        if current_allowance >= expected_allowance:
            return None
        if current_allowance <= 0:
            return self.raise_alert(ALERT_CRITICAL, KIND_REVOCATION,
                                    "Budget revoked: the allowance is now 0. Agents will not be recharged.")
        return self.raise_alert(
            ALERT_WARN, KIND_ALLOWANCE_REDUCED,
            f"Allowance reduced outside PocketDesk: {format_ausd(current_allowance)} left "
            f"(expected {format_ausd(expected_allowance)}).",
        )

    def on_breaker_change(self, status: Mapping[str, Any]) -> None:
        """
        `CircuitBreaker.on_change` callback: a global trip → critical "TOKEN PAUSED — agents halted";
        the breaker closing again → info; agent backoffs → `agent_backoff`.
        """
        is_open = bool(status.get("global_open"))
        if is_open and not self._breaker_was_open:
            reason = status.get("global_reason") or "token paused"
            self.raise_alert(ALERT_CRITICAL, KIND_TOKEN_PAUSED, f"{TOKEN_PAUSED_BANNER}: {reason}")
        elif not is_open and self._breaker_was_open:
            self.raise_alert(ALERT_INFO, KIND_TOKEN_RESUMED, "Token unpaused (pre-flight succeeded): agents resumed.")
        self._breaker_was_open = is_open
        for agent, backoff in (status.get("agents") or {}).items():
            self.agent_backoff(agent, int(backoff.get("failures", 0)), str(backoff.get("last_reason", "")))

    def counterparty_frozen(self, agent: str, address: str, role: str) -> Alert | None:
        """isAccountFrozen(address) was true for the payee ("payTo") or the payer ("agent")."""
        return self.raise_alert(
            ALERT_WARN, KIND_COUNTERPARTY_FROZEN,
            f"{agent} skipped a payment: {role} {address} is frozen (compliance check).",
            dedupe_key=f"{KIND_COUNTERPARTY_FROZEN}:{address.lower()}",
        )

    def voucher_failed(self, day_index: int, reason: str, tx_hash: str | None = None) -> Alert | None:
        """A daily voucher could not be redeemed (maximum-safety mode)."""
        tx_text = f" (tx {tx_hash})" if tx_hash else ""
        return self.raise_alert(
            ALERT_CRITICAL, KIND_VOUCHER_FAILED,
            f"Daily voucher for day {day_index} failed: {reason}{tx_text}",
            dedupe_key=f"{KIND_VOUCHER_FAILED}:{day_index}",
        )

    def send_daily_brief(self, brief: DailyBrief) -> Alert | None:
        """Store the brief as an info alert (once per day) and always forward it to Telegram."""
        return self.raise_alert(ALERT_INFO, KIND_DAILY_BRIEF, brief.text(),
                                dedupe_key=f"{KIND_DAILY_BRIEF}:{brief.day}", force_forward=True)


def _telegram_text(alert: Alert) -> str:
    """Telegram rendering: a level marker plus the message ("[CRITICAL] TOKEN PAUSED …")."""
    if alert.kind == KIND_DAILY_BRIEF:
        return alert.message
    return f"[{alert.level.upper()}] {alert.message}"


# ─── daily brief ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DailyBrief:
    """Session-close summary; every number is computed here, from stored rows."""

    day: str
    spend_by_agent: dict[str, int]
    bought: int
    skipped: int
    failed: int
    skipped_value_atomic: int                       # Σ price of purchases the VOI gate skipped
    top_skip_reasons: tuple[tuple[str, int], ...]   # (reason, count), most common first
    top_findings: tuple[str, ...]                   # headline lines, largest |Δp| first
    savings_line: str

    @property
    def total_spend_atomic(self) -> int:
        """Σ spend over all agents."""
        return sum(self.spend_by_agent.values())

    def text(self) -> str:
        """Plain-text brief for the dashboard and Telegram."""
        lines = [f"PocketDesk daily brief — {self.day}", self._spend_line()]
        lines.append("Top findings:" if self.top_findings else "Top findings: none today")
        lines.extend(f"• {finding}" for finding in self.top_findings)
        lines.append(self._voi_line())
        lines.append(f"Savings: {self.savings_line}")
        return "\n".join(lines)

    def _spend_line(self) -> str:
        """"Spend: $0.37 (OptionsEvent $0.20, MacroFilings $0.17); 8 bought, 14 skipped, 1 failed"."""
        per_agent = ", ".join(f"{agent} {format_ausd(amount)}" for agent, amount in self.spend_by_agent.items())
        detail = f" ({per_agent})" if per_agent else ""
        return (
            f"Spend: {format_ausd(self.total_spend_atomic)}{detail}; "
            f"{self.bought} bought, {self.skipped} skipped, {self.failed} failed"
        )

    def _voi_line(self) -> str:
        """"VOI gate: skipped 14 purchases worth $0.42 (reason × count, …)"."""
        if self.skipped == 0:
            return "VOI gate: nothing skipped"
        reasons = ", ".join(f"{reason} ×{count}" for reason, count in self.top_skip_reasons)
        return f"VOI gate: skipped {self.skipped} purchases worth {format_ausd(self.skipped_value_atomic)} ({reasons})"


def _spend_by_agent(purchases: Sequence[Purchase]) -> dict[str, int]:
    """Σ price of bought purchases per agent, largest spender first."""
    totals: Counter[str] = Counter()
    for purchase in purchases:
        if purchase.status == PURCHASE_BOUGHT:
            totals[purchase.agent] += purchase.price_atomic
    return dict(totals.most_common())


def _finding_line(finding: Finding) -> str:
    """Headline plus how many paid receipts back it."""
    receipt_count = len(finding.receipts or [])
    plural = "s" if receipt_count != 1 else ""
    return f"{finding.headline} ({receipt_count} receipt{plural})"


def compose_daily_brief(
    day: str,
    purchases: Sequence[Purchase],
    findings: Sequence[Finding],
    savings: SavingsMeter,
) -> DailyBrief:
    """Build the brief from one day's purchases and findings and the savings meter (pure)."""
    by_status = Counter(purchase.status for purchase in purchases)
    skipped = [purchase for purchase in purchases if purchase.status == PURCHASE_SKIPPED]
    skip_reasons = Counter((purchase.reason or "no reason")[:SKIP_REASON_MAX_CHARS] for purchase in skipped)
    ranked_findings = sorted(findings, key=lambda finding: abs(finding.delta_p), reverse=True)
    return DailyBrief(
        day=day,
        spend_by_agent=_spend_by_agent(purchases),
        bought=by_status[PURCHASE_BOUGHT],
        skipped=by_status[PURCHASE_SKIPPED],
        failed=by_status[PURCHASE_FAILED],
        skipped_value_atomic=sum(purchase.price_atomic for purchase in skipped),
        top_skip_reasons=tuple(skip_reasons.most_common(BRIEF_TOP_SKIP_REASONS)),
        top_findings=tuple(_finding_line(finding) for finding in ranked_findings[:BRIEF_TOP_FINDINGS]),
        savings_line=describe_savings(savings),
    )


def build_daily_brief(engine: Engine, start: datetime, end: datetime, now: datetime | None = None) -> DailyBrief:
    """Load the session's purchases/findings (start ≤ ts < end), the settings and MTD spend; compose."""
    now = now or utc_now()
    with session_scope(engine) as session:
        purchases = purchases_between(session, start, end)
        findings = findings_between(session, start, end)
        settings = load_api_settings(session)
        mtd_spend = month_to_date_spend(session, now)
    savings = compute_savings(mtd_spend, settings["subscriptions"], settings["monthly_cap_atomic"], now)
    return compose_daily_brief(day_key(start), purchases, findings, savings)
