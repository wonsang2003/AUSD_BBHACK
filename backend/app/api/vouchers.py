"""
Daily vouchers — "Maximum safety" budget mode (SPEC §9 Update #1 D).

Instead of one standing allowance the user signs one ERC-3009 `ReceiveWithAuthorization` per
UTC day (to = BudgetController) plus the BudgetConfig. Each day the keeper redeems that day's
voucher and splits it across agents, so at most ONE day's value is ever exposed; a future day
is killed by cancelling its nonce on the token.

    POST /api/vouchers/prepare         {owner, daily_values_atomic[7], salt?}
                                        → {vouchers: [TypedData ×7], config: TypedData, salt, day_indexes}
    POST /api/vouchers/submit          {owner, vouchers: [{day_index, value, valid_after, valid_before,
                                        nonce, signature}], config, config_signature}
                                        → every signature recovered here; relayer → openVoucherBudget;
                                          vouchers stored → {stored, tx_hash}
    GET  /api/vouchers?owner=           → [Voucher] by day (status pending | redeemed | cancelled | failed | expired)
    POST /api/vouchers/prepare-cancel  {owner, day_indexes[]}      → {typed_data: [CancelAuthorization]}
    POST /api/vouchers/cancel          {owner, cancels: [{nonce, signature}]}
                                        → relayer submits AUSD.cancelAuthorization → {tx_hashes, tx_hash}

Day numbering: `day_index` = floor(unix / 86400) (UTC), the voucher for day d is valid from
d·86400 − 1 to (d+1)·86400 (`chain/vouchers.py:day_bounds`). Nonces are
keccak256(abi.encode("pocketdesk-voucher", owner, day, salt)).
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.budget import (
    agent_roster,
    controller_address,
    parse_config_message,
    remember_budget,
    require_signature,
    require_signer,
)
from app.api.deps import (
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_NOT_FOUND,
    call_blocking,
    checksum,
    engine_of,
    get_runtime,
    resolve_owner,
    tx_hash_of,
)
from app.chain import vouchers as voucher_lib
from app.chain.eip712 import BudgetConfig, build_budget_config, sector_to_bytes32
from app.db import (
    VOUCHER_CANCELLED,
    VOUCHER_SIGNED,
    Voucher,
    add_voucher,
    load_api_settings,
    session_scope,
    voucher_for_day,
    vouchers_for_owner,
)

router = APIRouter(tags=["vouchers"])

DAYS_PER_WEEK = voucher_lib.DAYS_PER_WEEK
API_STATUS_PENDING = "pending"
API_STATUS_EXPIRED = "expired"
PENDING_DB_STATUSES = (VOUCHER_SIGNED, API_STATUS_PENDING)   # db.py says "signed", chain/vouchers.py "pending"


# ─── request bodies ──────────────────────────────────────────────────────────

class VoucherPrepareBody(BaseModel):
    owner: str
    daily_values_atomic: list[int] = Field(min_length=DAYS_PER_WEEK, max_length=DAYS_PER_WEEK)
    salt: str | None = None


class SignedVoucherBody(BaseModel):
    day_index: int = Field(ge=0)
    value: int = Field(ge=0)
    valid_after: int = Field(ge=0)
    valid_before: int = Field(gt=0)
    nonce: str
    signature: str


class VoucherSubmitBody(BaseModel):
    owner: str
    vouchers: list[SignedVoucherBody] = Field(min_length=1, max_length=DAYS_PER_WEEK)
    config: dict[str, Any] | None = None       # the prepared `config.message`, unchanged
    config_signature: str | None = None


class VoucherPrepareCancelBody(BaseModel):
    owner: str
    day_indexes: list[int] = Field(default_factory=list)
    day_index: int | None = None               # single-day form (older web build)


class CancelItem(BaseModel):
    nonce: str
    signature: str


class VoucherCancelBody(BaseModel):
    owner: str
    cancels: list[CancelItem] = Field(min_length=1, max_length=DAYS_PER_WEEK)


# ─── helpers ─────────────────────────────────────────────────────────────────

def new_salt() -> str:
    """A random bytes32 salt (makes this week's nonces unguessable and unique)."""
    return "0x" + os.urandom(32).hex()


def require_bytes32(value: str, what: str) -> str:
    """0x + 64 hex chars, or 400."""
    if not (isinstance(value, str) and value.startswith("0x") and len(value) == 66):
        raise HTTPException(HTTP_BAD_REQUEST, f"{what} must be a 0x-prefixed bytes32 hex string")
    try:
        bytes.fromhex(value[2:])
    except ValueError as exc:
        raise HTTPException(HTTP_BAD_REQUEST, f"{what} is not valid hex") from exc
    return value.lower()


def voucher_config(runtime: Any, owner: str, daily_values: list[int], last_day: int) -> BudgetConfig:
    """
    BudgetConfig for voucher mode. Rules: daily cap = the largest day's value; expiry = the end of
    the last voucher day; agent caps = the caps saved in Settings, clipped to the daily cap
    (default: the daily cap).
    """
    roster = agent_roster(runtime)
    if not roster:
        raise HTTPException(HTTP_BAD_REQUEST, "no agent keys configured (AGENT_*_KEY in .env)")
    daily_cap = max(daily_values)
    with session_scope(engine_of(runtime)) as session:
        saved_caps = load_api_settings(session).get("agent_caps_atomic") or {}
    _, expiry = voucher_lib.day_bounds(last_day)
    return BudgetConfig(
        owner=owner,
        daily_cap=daily_cap,
        expiry=expiry,
        agent_addrs=[address for _, address in roster],
        agent_caps=[min(int(saved_caps.get(name, daily_cap)), daily_cap) for name, _ in roster],
        sectors=[sector_to_bytes32(name) for name, _ in roster],
        nonce=runtime.controller.cfg_nonce(owner),
    )


def api_status(stored_status: str, valid_before: int, now: int) -> str:
    """DB status → API status. Rule: a still-pending voucher whose day is over is "expired"."""
    if stored_status in PENDING_DB_STATUSES:
        return API_STATUS_EXPIRED if valid_before <= now else API_STATUS_PENDING
    return stored_status


def voucher_row(voucher: Voucher, now: int) -> dict[str, Any]:
    """GET /api/vouchers row (the signature is not echoed back)."""
    return {
        "id": voucher.id,
        "owner": voucher.owner,
        "day_index": voucher.day_index,
        "value": voucher.value,
        "value_atomic": voucher.value,
        "valid_after": voucher.valid_after,
        "valid_before": voucher.valid_before,
        "nonce": voucher.nonce,
        "status": api_status(voucher.status, voucher.valid_before, now),
        "tx_hash": voucher.tx_hash,
    }


# ─── prepare + submit ────────────────────────────────────────────────────────

def _prepare(runtime: Any, body: VoucherPrepareBody) -> dict[str, Any]:
    owner = checksum(body.owner, "owner")
    if any(value < 0 for value in body.daily_values_atomic) or max(body.daily_values_atomic) <= 0:
        raise HTTPException(HTTP_BAD_REQUEST, "daily_values_atomic must be ≥ 0 with at least one day > 0")
    salt = require_bytes32(body.salt, "salt") if body.salt else new_salt()
    controller = controller_address(runtime)
    chain_id = runtime.settings.chain_id
    first_day = voucher_lib.day_index(int(time.time()))
    days = [first_day + offset for offset in range(DAYS_PER_WEEK)]
    typed = [
        voucher_lib.build_voucher(
            chain_id=chain_id, ausd_address=runtime.token.address, controller_address=controller, owner=owner,
            day_index=day, value=value, nonce=voucher_lib.compute_voucher_nonce(owner, day, salt),
        )
        for day, value in zip(days, body.daily_values_atomic, strict=True)
    ]
    config = voucher_config(runtime, owner, body.daily_values_atomic, days[-1])
    config_typed = build_budget_config(chain_id=chain_id, controller_address=controller, config=config)
    return {"vouchers": typed, "config": config_typed, "salt": salt, "day_indexes": days}


@router.post("/api/vouchers/prepare")
async def prepare_vouchers(body: VoucherPrepareBody, runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """7 ReceiveWithAuthorization vouchers (today first) + the BudgetConfig, to be signed unchanged."""
    return await call_blocking(_prepare, runtime, body)


def _check_voucher(runtime: Any, owner: str, controller: str, voucher: SignedVoucherBody) -> None:
    """Rule: the voucher's window is exactly its UTC day and it was signed by the owner (to = controller)."""
    require_signature(voucher.signature, f"voucher signature (day {voucher.day_index})")
    nonce = require_bytes32(voucher.nonce, f"voucher nonce (day {voucher.day_index})")
    if (voucher.valid_after, voucher.valid_before) != voucher_lib.day_bounds(voucher.day_index):
        raise HTTPException(HTTP_BAD_REQUEST, f"voucher for day {voucher.day_index} has the wrong validity window")
    typed = voucher_lib.build_voucher(
        chain_id=runtime.settings.chain_id, ausd_address=runtime.token.address, controller_address=controller,
        owner=owner, day_index=voucher.day_index, value=voucher.value, nonce=nonce,
    )
    require_signer(typed, voucher.signature, owner, f"voucher for day {voucher.day_index}")


def _open_voucher_budget(runtime: Any, owner: str, controller: str, body: VoucherSubmitBody) -> tuple[str, BudgetConfig]:
    """Verify the config signature and nonce, then the relayer sends openVoucherBudget."""
    require_signature(body.config_signature or "", "config_signature")
    config = parse_config_message(body.config or {})
    if config.owner != owner:
        raise HTTPException(HTTP_BAD_REQUEST, f"config.owner {config.owner} is not the owner {owner}")
    typed = build_budget_config(chain_id=runtime.settings.chain_id, controller_address=controller, config=config)
    require_signer(typed, body.config_signature, owner, "budget config")
    on_chain_nonce = runtime.controller.cfg_nonce(owner)
    if config.nonce != on_chain_nonce:
        raise HTTPException(HTTP_BAD_REQUEST, f"config nonce {config.nonce} is stale (on-chain {on_chain_nonce}): "
                                              "prepare the vouchers again")
    too_big = [v.day_index for v in body.vouchers if v.value > config.daily_cap]
    if too_big:
        raise HTTPException(HTTP_BAD_REQUEST, f"voucher value above the daily cap for day(s) {too_big}")
    result = runtime.controller.open_voucher_budget(config, body.config_signature)
    return tx_hash_of(result), config


def _store(runtime: Any, owner: str, vouchers: list[SignedVoucherBody]) -> int:
    """Insert each voucher, or replace the not-yet-redeemed one already stored for that day."""
    with session_scope(engine_of(runtime)) as session:
        for item in vouchers:
            row = voucher_for_day(session, owner, item.day_index) or Voucher(
                owner=owner, day_index=item.day_index, value=0, valid_after=0, valid_before=0, nonce="", signature=""
            )
            row.value, row.valid_after, row.valid_before = item.value, item.valid_after, item.valid_before
            row.nonce, row.signature, row.status, row.tx_hash = item.nonce.lower(), item.signature, VOUCHER_SIGNED, None
            add_voucher(session, row)
    return len(vouchers)


def _submit(runtime: Any, body: VoucherSubmitBody) -> dict[str, Any]:
    owner = checksum(body.owner, "owner")
    controller = controller_address(runtime)
    for voucher in body.vouchers:
        _check_voucher(runtime, owner, controller, voucher)
    tx_hash = None
    if body.config is not None:
        tx_hash, config = _open_voucher_budget(runtime, owner, controller, body)
        remember_budget(runtime, owner, config, sum(v.value for v in body.vouchers))
    stored = _store(runtime, owner, body.vouchers)
    return {"stored": stored, "tx_hash": tx_hash}


@router.post("/api/vouchers/submit")
async def submit_vouchers(body: VoucherSubmitBody, runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """Verify every signature, open the voucher budget on-chain, store the vouchers. → {stored, tx_hash}"""
    return await call_blocking(_submit, runtime, body)


# ─── list ────────────────────────────────────────────────────────────────────

def _list(runtime: Any, owner: str | None) -> list[dict[str, Any]]:
    try:
        resolved = resolve_owner(runtime, owner)
    except HTTPException:
        return []          # no owner yet → no vouchers
    now = int(time.time())
    with session_scope(engine_of(runtime)) as session:
        return [voucher_row(v, now) for v in vouchers_for_owner(session, resolved)]


@router.get("/api/vouchers")
async def list_vouchers(owner: str | None = Query(None), runtime: Any = Depends(get_runtime)) -> list[dict[str, Any]]:
    """The owner's stored vouchers by day (empty when no owner is known)."""
    return await call_blocking(_list, runtime, owner)


# ─── cancel future days ──────────────────────────────────────────────────────

def _prepare_cancel(runtime: Any, body: VoucherPrepareCancelBody) -> dict[str, Any]:
    owner = checksum(body.owner, "owner")
    days = list(body.day_indexes) + ([body.day_index] if body.day_index is not None else [])
    if not days:
        raise HTTPException(HTTP_BAD_REQUEST, "day_indexes is empty")
    typed_data = []
    with session_scope(engine_of(runtime)) as session:
        for day in days:
            voucher = voucher_for_day(session, owner, day)
            if voucher is None:
                raise HTTPException(HTTP_NOT_FOUND, f"no stored voucher for day {day}")
            typed_data.append(voucher_lib.build_cancel(
                chain_id=runtime.settings.chain_id, ausd_address=runtime.token.address, owner=owner,
                nonce=voucher.nonce,
            ))
    return {"typed_data": typed_data}


@router.post("/api/vouchers/prepare-cancel")
async def prepare_cancel(body: VoucherPrepareCancelBody, runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """One CancelAuthorization message per requested day's voucher nonce."""
    return await call_blocking(_prepare_cancel, runtime, body)


def _cancel_one(runtime: Any, owner: str, item: CancelItem) -> str:
    """Verify the owner signed the CancelAuthorization, refuse a used nonce, then the relayer submits it."""
    nonce = require_bytes32(item.nonce, "nonce")
    require_signature(item.signature, "signature")
    typed = voucher_lib.build_cancel(chain_id=runtime.settings.chain_id, ausd_address=runtime.token.address,
                                     owner=owner, nonce=nonce)
    require_signer(typed, item.signature, owner, "CancelAuthorization")
    if runtime.token.authorization_state(owner, nonce):
        raise HTTPException(HTTP_CONFLICT, f"voucher nonce {nonce} was already redeemed or cancelled")
    result = runtime.relayer.send(runtime.token.cancel_authorization_fn(owner, nonce, item.signature))
    tx_hash = tx_hash_of(result)
    with session_scope(engine_of(runtime)) as session:
        for voucher in vouchers_for_owner(session, owner):
            if voucher.nonce.lower() == nonce:
                voucher.status, voucher.tx_hash = VOUCHER_CANCELLED, tx_hash
                session.add(voucher)
    return tx_hash


def _cancel(runtime: Any, body: VoucherCancelBody) -> dict[str, Any]:
    owner = checksum(body.owner, "owner")
    tx_hashes = [_cancel_one(runtime, owner, item) for item in body.cancels]
    return {"tx_hashes": tx_hashes, "tx_hash": tx_hashes[-1]}


@router.post("/api/vouchers/cancel")
async def cancel_vouchers(body: VoucherCancelBody, runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """The relayer submits each signed CancelAuthorization to AUSD. → {tx_hashes, tx_hash}"""
    return await call_blocking(_cancel, runtime, body)
