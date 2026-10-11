"""
Savings meter: what PocketDesk's pay-per-call research cost this month versus the data
subscriptions the user says it replaces, plus progress against the monthly cap.

    prorated_subscriptions_usd = Σ monthly_usd_i × (elapsed_days / days_in_month)
    mtd_spend_usd              = month-to-date AUSD spend (1 AUSD = $1)
    savings_usd                = prorated_subscriptions_usd − mtd_spend_usd   (negative = costs more)
    monthly_cap_used           = mtd_spend_atomic / monthly_cap_atomic         (0 when no cap)

`elapsed_days` counts today as a full day once the month has started (1st of the month at
00:00 UTC → 1 day), so the comparison is never against zero subscription cost.

Subscriptions are ENTERED BY THE USER on the settings page (name + monthly USD); nothing in
here knows any vendor's price. Month-to-date spend comes from `db.month_to_date_spend`
(the spend ledger). The output feeds `State.savings` / `State.monthly` (docs/API.md) and the
daily brief.
"""

from __future__ import annotations

import calendar
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.constants import ONE_AUSD

USD_DECIMALS_SHOWN = 2
MAX_SUBSCRIPTION_NAME_LENGTH = 80


@dataclass(frozen=True)
class Subscription:
    """One data subscription the user pays for (or would pay for) each month."""

    name: str
    monthly_usd: float

    def to_api(self) -> dict[str, Any]:
        """`{name, monthly_usd}` as in docs/API.md Settings.subscriptions."""
        return {"name": self.name, "monthly_usd": self.monthly_usd}


@dataclass(frozen=True)
class SavingsMeter:
    """The month-to-date comparison shown on the dashboard and in the daily brief."""

    mtd_spend_atomic: int
    prorated_subscriptions_usd: float
    savings_usd: float
    subscriptions: tuple[Subscription, ...]
    monthly_cap_atomic: int
    monthly_cap_used: float          # 0–1+ (above 1 means the cap was exceeded)
    month_fraction_elapsed: float    # elapsed_days / days_in_month

    def to_api(self) -> dict[str, Any]:
        """docs/API.md `State.savings` (+ savings_usd and monthly cap progress, additive)."""
        return {
            "mtd_spend_atomic": self.mtd_spend_atomic,
            "prorated_subscriptions_usd": self.prorated_subscriptions_usd,
            "subscriptions": [subscription.to_api() for subscription in self.subscriptions],
            "savings_usd": self.savings_usd,
            "monthly_cap_atomic": self.monthly_cap_atomic,
            "monthly_cap_used": self.monthly_cap_used,
        }

    def monthly_to_api(self) -> dict[str, int]:
        """docs/API.md `State.monthly`: `{cap, spent_mtd}`."""
        return {"cap": self.monthly_cap_atomic, "spent_mtd": self.mtd_spend_atomic}


# ─── inputs ──────────────────────────────────────────────────────────────────

def parse_subscriptions(raw: Iterable[Mapping[str, Any]] | None) -> tuple[Subscription, ...]:
    """
    Validate user-entered subscriptions. Rule: an entry needs a non-empty name and a finite,
    non-negative monthly USD amount; anything else is skipped (never guessed or repaired).
    """
    parsed: list[Subscription] = []
    for entry in raw or []:
        name = str(entry.get("name", "")).strip()[:MAX_SUBSCRIPTION_NAME_LENGTH]
        try:
            monthly_usd = float(entry.get("monthly_usd", math.nan))
        except (TypeError, ValueError):
            continue
        if name and math.isfinite(monthly_usd) and monthly_usd >= 0:
            parsed.append(Subscription(name=name, monthly_usd=monthly_usd))
    return tuple(parsed)


# ─── formulas ────────────────────────────────────────────────────────────────

def month_fraction_elapsed(now: datetime) -> float:
    """elapsed_days / days_in_month in UTC, counting today as a full day (Oct 11 → 11/31)."""
    now_utc = now.astimezone(UTC) if now.tzinfo else now
    days_in_month = calendar.monthrange(now_utc.year, now_utc.month)[1]
    return now_utc.day / days_in_month


def prorated_subscription_cost(subscriptions: Iterable[Subscription], fraction_elapsed: float) -> float:
    """Σ monthly_usd × fraction_elapsed, rounded to cents."""
    total_monthly = sum(subscription.monthly_usd for subscription in subscriptions)
    return round(total_monthly * fraction_elapsed, USD_DECIMALS_SHOWN)


def cap_used_fraction(spent_atomic: int, cap_atomic: int) -> float:
    """spent / cap, or 0 when no cap is set."""
    if cap_atomic <= 0:
        return 0.0
    return spent_atomic / cap_atomic


def compute_savings(
    mtd_spend_atomic: int,
    subscriptions: Iterable[Mapping[str, Any]] | None,
    monthly_cap_atomic: int,
    now: datetime,
) -> SavingsMeter:
    """Build the savings meter from month-to-date spend, the user's subscriptions and the cap."""
    parsed = parse_subscriptions(subscriptions)
    fraction = month_fraction_elapsed(now)
    prorated = prorated_subscription_cost(parsed, fraction)
    spend_usd = mtd_spend_atomic / ONE_AUSD
    return SavingsMeter(
        mtd_spend_atomic=mtd_spend_atomic,
        prorated_subscriptions_usd=prorated,
        savings_usd=round(prorated - spend_usd, USD_DECIMALS_SHOWN),
        subscriptions=parsed,
        monthly_cap_atomic=monthly_cap_atomic,
        monthly_cap_used=cap_used_fraction(mtd_spend_atomic, monthly_cap_atomic),
        month_fraction_elapsed=fraction,
    )


def describe_savings(meter: SavingsMeter) -> str:
    """One line for the daily brief, e.g. "Spent $3.20 this month vs $96.77 pro-rated subscriptions (saved $93.57)"."""
    spend_usd = meter.mtd_spend_atomic / ONE_AUSD
    if not meter.subscriptions:
        return f"Spent ${spend_usd:,.2f} this month (add your data subscriptions in Settings to compare)"
    verdict = "saved" if meter.savings_usd >= 0 else "over by"
    return (
        f"Spent ${spend_usd:,.2f} this month vs ${meter.prorated_subscriptions_usd:,.2f} pro-rated "
        f"subscriptions ({verdict} ${abs(meter.savings_usd):,.2f})"
    )
