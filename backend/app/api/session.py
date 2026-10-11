"""
Trading-session control: `POST /api/session/start`, `POST /api/session/stop`.

    start → allocator splits today's budget across agents, the relayer recharges each agent
            wallet through BudgetController (capped on-chain)  → {allocations, recharge_txs}
    stop  → every agent wallet is swept back to the owner        → {sweep_txs}

The keeper `Runtime` does the work (and publishes `allocation` / `recharge` / `sweep` SSE
events); this router only translates its errors into `{detail}` answers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import api_errors, get_runtime

router = APIRouter(tags=["session"])


@router.post("/api/session/start")
async def start_session(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """Allocate + recharge. → {allocations, recharge_txs: [{agent, amount_atomic, tx_hash}]}"""
    with api_errors():
        return await runtime.start_session()


@router.post("/api/session/stop")
async def stop_session(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """End the session and sweep agent balances to the owner. → {sweep_txs: [...]}"""
    with api_errors():
        return await runtime.stop_session()
