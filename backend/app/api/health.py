"""
Health and static configuration: `GET /api/health`, `GET /api/config`.

Health never needs a working chain connection: it reports the outcome of the startup checks
(`chain_checks`: "passed" | "skipped" | "failed: …") so the dashboard can show a red banner
instead of failing silently. Config is what the web needs to build explorer links and label
agents and demo vendors; it comes from the keeper runtime (`Runtime.config()`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import app_context, call_blocking, ensure_started, get_runtime

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health(request: Request) -> dict[str, Any]:
    """`{ok, chain_checks, chain_id, ausd_address, controller_address, live_vendor_mode}` (+ `startup_error`)."""
    ctx = app_context(request)
    ensure_started(ctx)
    settings = ctx.settings
    return {
        "ok": ctx.healthy,
        "chain_checks": ctx.chain_checks,
        "chain_id": settings.chain_id,
        "ausd_address": settings.ausd_address,
        "controller_address": settings.budget_controller_address,
        "live_vendor_mode": settings.live_vendor_mode,
        "startup_error": ctx.startup_error,
    }


@router.get("/api/config")
async def config(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """Chain addresses, explorer URLs, agents (name, address, sector, sellers) and demo vendors."""
    return await call_blocking(runtime.config)
