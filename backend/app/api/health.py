"""
Health and static configuration: `GET /api/health`, `GET /api/config`.

Health never needs a working chain connection: it reports the outcome of the startup checks
(`chain_checks`: "passed" | "skipped" | "failed: …") so the dashboard can show a red banner
instead of failing silently. Config is what the web needs to build explorer links and label
agents and demo vendors; it comes from the keeper runtime (`Runtime.config()`).
"""

from __future__ import annotations

from typing import Any

from eth_account import Account
from fastapi import APIRouter, Request

from app.api.deps import app_context, call_blocking, ensure_started
from app.constants import BASESCAN_ADDRESS_URL, BASESCAN_TX_URL
from app.sellers.registry import DEMO_VENDORS

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


def config_from_settings(request: Request) -> dict[str, Any]:
    """
    /api/config built from settings + the mounted demo vendors only (no runtime, no RPC).
    Used when the keeper runtime is unavailable, so the dashboard can still label everything.
    """
    ctx = app_context(request)
    settings = ctx.settings
    offers = getattr(request.app.state, "demo_vendor_offers", [])
    base_url = settings.public_base_url.rstrip("/")
    agents = [
        {
            "name": name,
            "address": Account.from_key(key).address,
            "sector": name,
            "sellers": [seller_id for seller_id, info in DEMO_VENDORS.items() if info.sector == name],
        }
        for name, key in settings.agent_keys().items()
        if key
    ]
    return {
        "chain_id": settings.chain_id,
        "ausd_address": settings.ausd_address,
        "controller_address": settings.budget_controller_address,
        "faucet_address": settings.ausd_faucet,
        "explorer_tx_url": BASESCAN_TX_URL,
        "explorer_address_url": BASESCAN_ADDRESS_URL,
        "agents": agents,
        "sellers": [
            {"seller_id": offer.seller_id, "path": offer.path, "url": f"{base_url}{offer.path}",
             "price_atomic": offer.price_atomic, "pay_to": offer.pay_to, "description": offer.description}
            for offer in offers
        ],
    }


@router.get("/api/config")
async def config(request: Request) -> dict[str, Any]:
    """Chain addresses, explorer URLs, agents (name, address, sector, sellers) and demo vendors.

    Rule: the runtime's config when it is up; otherwise the same shape from settings alone."""
    ctx = app_context(request)
    ensure_started(ctx)
    runtime_config = getattr(ctx.runtime, "config", None)
    if ctx.startup_error is None and not ctx.fatal and runtime_config is not None:
        return await call_blocking(runtime_config)
    return config_from_settings(request)
