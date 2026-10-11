"""
The user's settings: `GET /api/settings`, `POST /api/settings` (docs/API.md "Settings").

`{owner, watchlist, stakes_usd, session_hours: {start, end, tz}, plan_days, monthly_cap_atomic,
daily_cap_atomic, total_budget_atomic, agent_caps_atomic, subscriptions: [{name, monthly_usd}]}`

POST accepts a full or partial Settings object; only known keys are stored (`db.merge_api_settings`)
and the full result is returned. Saving an `owner` also points the runtime at that owner.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.api.deps import call_blocking, checksum, engine_of, get_runtime
from app.db import load_api_settings, save_api_settings, session_scope

router = APIRouter(tags=["settings"])

MAX_WATCHLIST = 20
MAX_PLAN_DAYS = 366


class SessionHours(BaseModel):
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")
    tz: str


class SubscriptionBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    monthly_usd: float = Field(ge=0)


class SettingsBody(BaseModel):
    """Every field optional: a partial update only changes the fields it sends."""

    owner: str | None = None
    watchlist: list[str] | None = Field(default=None, max_length=MAX_WATCHLIST)
    stakes_usd: dict[str, float] | None = None
    session_hours: SessionHours | None = None
    plan_days: int | None = Field(default=None, ge=1, le=MAX_PLAN_DAYS)
    monthly_cap_atomic: int | None = Field(default=None, ge=0)
    daily_cap_atomic: int | None = Field(default=None, ge=0)
    total_budget_atomic: int | None = Field(default=None, ge=0)
    agent_caps_atomic: dict[str, int] | None = None
    subscriptions: list[SubscriptionBody] | None = None

    @field_validator("watchlist")
    @classmethod
    def _upper_symbols(cls, value: list[str] | None) -> list[str] | None:
        """Symbols are stored upper-case and without blanks."""
        if value is None:
            return None
        return [symbol.strip().upper() for symbol in value if symbol.strip()]


def _load(runtime: Any) -> dict[str, Any]:
    with session_scope(engine_of(runtime)) as session:
        return load_api_settings(session)


def _save(runtime: Any, update: dict[str, Any]) -> dict[str, Any]:
    with session_scope(engine_of(runtime)) as session:
        saved = save_api_settings(session, update)
    if saved.get("owner"):
        runtime.owner = saved["owner"]
    return saved


@router.get("/api/settings")
async def get_settings(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """The stored Settings (defaults filled in)."""
    return await call_blocking(_load, runtime)


@router.post("/api/settings")
async def post_settings(body: SettingsBody, runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """Merge the sent fields into the stored Settings and return the full result."""
    update = body.model_dump(exclude_unset=True)
    if update.get("owner"):
        update["owner"] = checksum(update["owner"], "owner")
    return await call_blocking(_save, runtime, update)
