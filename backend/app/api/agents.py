"""
Per-agent switches: `POST /api/agents/{name}/pause` and `/resume`.

A paused agent makes no purchases (its VOI decisions are not executed) until resumed. This is
an off-chain switch in the keeper; the on-chain budget pause is `/api/budget/pause`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import HTTP_NOT_FOUND, agent_names, api_errors, get_runtime

router = APIRouter(tags=["agents"])


def _require_agent(runtime: Any, name: str) -> None:
    """404 unless `name` is one of the runtime's agents."""
    if name not in agent_names(runtime):
        raise HTTPException(HTTP_NOT_FOUND, f"unknown agent {name!r}")


@router.post("/api/agents/{name}/pause")
def pause_agent(name: str, runtime: Any = Depends(get_runtime)) -> dict[str, bool]:
    """Stop one agent from buying. → {ok}"""
    _require_agent(runtime, name)
    with api_errors():
        runtime.pause_agent(name)
    return {"ok": True}


@router.post("/api/agents/{name}/resume")
def resume_agent(name: str, runtime: Any = Depends(get_runtime)) -> dict[str, bool]:
    """Let a paused agent buy again. → {ok}"""
    _require_agent(runtime, name)
    with api_errors():
        runtime.resume_agent(name)
    return {"ok": True}
