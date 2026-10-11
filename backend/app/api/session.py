"""
Trading-session control: `POST /api/session/start`, `/stop` and `/tick`.

    start → allocator splits today's budget across agents, the relayer recharges each agent
            wallet through BudgetController (capped on-chain)  → {allocations, recharge_txs}
    stop  → every agent wallet is swept back to the owner        → {sweep_txs}
    tick  → one buying round NOW for one agent (or all), instead of waiting for the scheduler
                                                                 → {decisions: [Purchase]}

The keeper `Runtime` does the work (and publishes `allocation` / `recharge` / `sweep` / `purchase`
SSE events); this router checks the preconditions the dashboard can fix itself (no budget yet,
budget paused or expired → 409 with a sentence telling the user what to click) and translates the
runtime's errors into `{detail}` answers.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import HTTP_CONFLICT, api_errors, call_blocking, get_runtime, stored_owner

router = APIRouter(tags=["session"])

NO_BUDGET_DETAIL = "No budget yet — fund one on F2 FUND (or click DEMO FUND)"
NO_CONTROLLER_DETAIL = ("BudgetController / relayer not configured (BUDGET_CONTROLLER_ADDRESS, "
                        "RELAYER_PRIVATE_KEY in the backend .env)")
PAUSED_DETAIL = "Budget is paused — resume it on F2 FUND before starting a session"
EXPIRED_DETAIL = "Budget has expired — fund a new one on F2 FUND (or click DEMO FUND)"


class TickBody(BaseModel):
    agent: str | None = None


def require_open_budget(runtime: Any) -> None:
    """409 with a user-facing sentence unless the owner has an open, unpaused, unexpired budget."""
    owner = getattr(runtime, "owner", None) or stored_owner(runtime)
    if not owner:
        raise HTTPException(HTTP_CONFLICT, NO_BUDGET_DETAIL)
    if runtime.controller is None or getattr(runtime, "recharger", None) is None:
        raise HTTPException(HTTP_CONFLICT, NO_CONTROLLER_DETAIL)
    view = runtime.controller.budget(owner)
    if not view.exists:
        raise HTTPException(HTTP_CONFLICT, NO_BUDGET_DETAIL)
    if view.paused:
        raise HTTPException(HTTP_CONFLICT, PAUSED_DETAIL)
    if view.expiry <= int(time.time()):
        raise HTTPException(HTTP_CONFLICT, EXPIRED_DETAIL)


@router.post("/api/session/start")
async def start_session(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """Allocate + recharge. → {allocations, recharge_txs: [{agent, amount_atomic, tx_hash}]}"""
    await call_blocking(require_open_budget, runtime)
    with api_errors():
        return await runtime.start_session()


@router.post("/api/session/stop")
async def stop_session(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """End the session and sweep agent balances to the owner. → {sweep_txs: [...]}"""
    owner = getattr(runtime, "owner", None) or await call_blocking(stored_owner, runtime)
    if not owner:
        raise HTTPException(HTTP_CONFLICT, NO_BUDGET_DETAIL)
    with api_errors():
        return await runtime.stop_session()


@router.post("/api/session/tick")
async def tick(body: TickBody | None = None, runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """Run one agent tick now (all agents when `agent` is omitted). → {decisions: [Purchase]}"""
    agent = body.agent if body else None
    with api_errors():
        decisions = await runtime.run_tick(agent or None)
    return {"decisions": decisions}
