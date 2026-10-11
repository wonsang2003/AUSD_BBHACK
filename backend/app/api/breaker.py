"""
`GET /api/breaker`: the payment circuit breaker (SPEC §9 Update #1 C).

`{global_open, global_reason, tripped_at, agents: {name: {failures, backoff_seconds, last_reason}}}`
— `global_open` means a pre-flight simulation found the token paused, so ALL agents halted
(dashboard banner "TOKEN PAUSED — agents halted"); per-agent entries are individual backoffs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_runtime

router = APIRouter(tags=["breaker"])


@router.get("/api/breaker")
def breaker(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """`CircuitBreaker.status()` of the runtime."""
    return runtime.breaker.status()
