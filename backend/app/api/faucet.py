"""
`POST /api/faucet {address}` → `{tx_hash}`: the relayer calls the AUSD testnet faucet
(`requestFunds(address)`, 10,000 test AUSD) for a user, so users never need ETH for gas.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import call_blocking, checksum, get_runtime, tx_hash_of
from app.chain.faucet import request_test_ausd

router = APIRouter(tags=["faucet"])


class FaucetBody(BaseModel):
    address: str


def _request(runtime: Any, recipient: str) -> dict[str, str]:
    result = request_test_ausd(runtime.relayer, runtime.settings.ausd_faucet, recipient)
    return {"tx_hash": tx_hash_of(result)}


@router.post("/api/faucet")
async def faucet(body: FaucetBody, runtime: Any = Depends(get_runtime)) -> dict[str, str]:
    """Relayer pays gas; the recipient gets test AUSD. → {tx_hash}"""
    recipient = checksum(body.address, "address")
    return await call_blocking(_request, runtime, recipient)
