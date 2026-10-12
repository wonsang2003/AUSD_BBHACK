"""
`POST /api/ask {question}`: the user asks the desk a question; the runtime routes it to the
relevant agents, which may BUY research (real x402 payments) to answer it.

→ `{answer, agents_used, findings: [Finding], receipts: [{seller_id, tx_hash, price_atomic}]}`
(also published as the `ask` SSE event).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import api_errors, get_runtime

router = APIRouter(tags=["ask"])

MAX_QUESTION_CHARS = 1_000


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


@router.post("/api/ask")
async def ask(body: AskBody, runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """Answer one question with the agents' (possibly freshly bought) research."""
    with api_errors():
        return await runtime.ask(body.question.strip())
