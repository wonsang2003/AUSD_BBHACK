"""
`GET /api/state`: the dashboard's main snapshot (budget, monthly cap, runway, agents, session,
savings, relayer gas, breaker) — the "State" shape of docs/API.md + Update #1 fields.

The runtime assembles it (on-chain reads + the spend ledger); this router only runs that
blocking work off the event loop.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import call_blocking, get_runtime

router = APIRouter(tags=["state"])


@router.get("/api/state")
async def state(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """The State snapshot (also published as the `state` SSE event)."""
    return await call_blocking(runtime.state)
