"""
Budget endpoints: the user SIGNS, the relayer pays gas (docs/API.md "Write endpoints").

    POST /api/budget/prepare         → {permit, config}   EIP-712 messages with nonces read NOW
    POST /api/budget/open            → {tx_hash}          both signatures recovered here, then
                                                          BudgetController.openBudget via the relayer
    POST /api/budget/prepare-pause   → {typed_data}       SetPaused (nonce = cfgNonce)
    POST /api/budget/pause           → {tx_hash}          setPausedWithSig
    POST /api/budget/prepare-revoke  → {typed_data}       permit with value 0
    POST /api/budget/revoke          → {tx_hash}          the relayer submits AUSD.permit(…, 0, …)

Flow of opening a budget:

    web ──prepare──▶ token.nonces(owner), controller.cfg_nonce(owner), agent addresses
        ◀── permit (owner → BudgetController, value = total budget) + BudgetConfig (caps, agents, expiry)
    wallet signs both unchanged
    web ──open──▶ rebuild both messages with the CURRENT on-chain nonces, recover both signers,
                  refuse unless both are `owner`, then relayer → openBudget(…)

Recovering before relaying means a wrong signature costs no gas and gets a clear message
("permit signed by 0x…, not the owner") instead of an opaque on-chain revert. Agent addresses
come from the agent keys in settings; the keys themselves never leave the server.
"""

from __future__ import annotations

import time
from typing import Any

from eth_account import Account
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import (
    HTTP_BAD_REQUEST,
    HTTP_UNAVAILABLE,
    call_blocking,
    checksum,
    engine_of,
    get_runtime,
    tx_hash_of,
)
from app.chain import controller as controller_module
from app.chain.eip712 import (
    BudgetConfig,
    Signature,
    build_budget_config,
    build_permit,
    build_set_paused,
    recover_typed_data_signer,
    sector_to_bytes32,
)
from app.db import save_api_settings, session_scope

router = APIRouter(tags=["budget"])

SECONDS_PER_DAY = 86_400
PERMIT_DEADLINE_SECONDS = 60 * 60       # the user has 1 hour between prepare and open
PAUSE_DEADLINE_SECONDS = 15 * 60        # a signed pause/resume is valid for 15 minutes
MAX_PLAN_DAYS = 366
SIGNATURE_HEX_LENGTH = 2 + 65 * 2       # "0x" + r ‖ s ‖ v


# ─── request bodies ──────────────────────────────────────────────────────────

class BudgetPrepareBody(BaseModel):
    owner: str
    total_budget_atomic: int = Field(gt=0)
    daily_cap_atomic: int = Field(gt=0)
    plan_days: int = Field(ge=1, le=MAX_PLAN_DAYS)
    agent_caps_atomic: dict[str, int] = Field(default_factory=dict)


class SignedPermit(BaseModel):
    value: int = Field(ge=0)
    deadline: int = Field(gt=0)
    signature: str


class BudgetOpenBody(BaseModel):
    owner: str
    permit: SignedPermit
    config: dict[str, Any]                 # the `config.message` from /prepare, unchanged
    config_signature: str


class PreparePauseBody(BaseModel):
    owner: str
    paused: bool


class PauseBody(BaseModel):
    owner: str
    paused: bool
    deadline: int = Field(gt=0)
    signature: str


class PrepareRevokeBody(BaseModel):
    owner: str


class RevokeBody(BaseModel):
    owner: str
    deadline: int = Field(gt=0)
    signature: str


# ─── helpers ─────────────────────────────────────────────────────────────────

def controller_address(runtime: Any) -> str:
    """The BudgetController address, or 503 when none is deployed / configured yet."""
    address = getattr(runtime.controller, "address", None) or runtime.settings.budget_controller_address
    if not address:
        raise HTTPException(HTTP_UNAVAILABLE, "BUDGET_CONTROLLER_ADDRESS is not set: deploy the controller first")
    return checksum(address, "controller address")


def agent_roster(runtime: Any) -> list[tuple[str, str]]:
    """(agent name, address) for every agent whose key is configured, in canonical order.

    The agent name doubles as its sector label (stored on-chain as bytes32)."""
    roster = []
    for name, key in runtime.settings.agent_keys().items():
        if key:
            roster.append((name, Account.from_key(key).address))
    return roster


def require_signature(signature: str, what: str) -> str:
    """A 0x-prefixed 65-byte hex signature, or 400."""
    if not (isinstance(signature, str) and signature.startswith("0x") and len(signature) == SIGNATURE_HEX_LENGTH):
        raise HTTPException(HTTP_BAD_REQUEST, f"{what} must be a 0x-prefixed 65-byte hex signature")
    try:
        bytes.fromhex(signature[2:])
    except ValueError as exc:
        raise HTTPException(HTTP_BAD_REQUEST, f"{what} is not valid hex") from exc
    return signature


def to_signature(signature: str) -> Signature:
    """0x-hex r ‖ s ‖ v → `eip712.Signature` (v normalised to 27/28), the form the contract calls take."""
    raw = bytes.fromhex(signature.removeprefix("0x"))
    v = raw[64] if raw[64] >= 27 else raw[64] + 27
    return Signature(v=v, r=raw[:32], s=raw[32:64], signature=raw[:64] + bytes([v]))


def require_signer(typed_data: dict, signature: str, owner: str, what: str) -> None:
    """Rule: the recovered signer of `typed_data` must be `owner`, else 400 naming who signed."""
    try:
        signer = recover_typed_data_signer(typed_data, signature)
    except Exception as exc:  # noqa: BLE001 — malformed signature bytes
        raise HTTPException(HTTP_BAD_REQUEST, f"{what}: cannot recover a signer ({exc})") from exc
    if signer != owner:
        raise HTTPException(HTTP_BAD_REQUEST, f"{what} was signed by {signer}, not by the owner {owner}")


def build_config_from_body(runtime: Any, body: BudgetPrepareBody, owner: str, nonce: int, now: int) -> BudgetConfig:
    """BudgetConfig for the request: every configured agent, its cap (default = the daily cap), sector = name."""
    roster = agent_roster(runtime)
    if not roster:
        raise HTTPException(HTTP_UNAVAILABLE, "no agent keys configured (AGENT_*_KEY in .env)")
    known = {name for name, _ in roster}
    unknown = sorted(set(body.agent_caps_atomic) - known)
    if unknown:
        raise HTTPException(HTTP_BAD_REQUEST, f"unknown agent(s) in agent_caps_atomic: {', '.join(unknown)}")
    caps = [int(body.agent_caps_atomic.get(name, body.daily_cap_atomic)) for name, _ in roster]
    for (name, _), cap in zip(roster, caps, strict=True):
        if cap < 0 or cap > body.daily_cap_atomic:
            raise HTTPException(HTTP_BAD_REQUEST, f"agent cap of {name} must be between 0 and the daily cap")
    return BudgetConfig(
        owner=owner,
        daily_cap=body.daily_cap_atomic,
        expiry=now + body.plan_days * SECONDS_PER_DAY,
        agent_addrs=[address for _, address in roster],
        agent_caps=caps,
        sectors=[sector_to_bytes32(name) for name, _ in roster],
        nonce=nonce,
    )


def parse_config_message(message: dict[str, Any]) -> BudgetConfig:
    """The signed `config.message` (JSON numbers or decimal strings) → BudgetConfig, or 400."""
    try:
        return BudgetConfig(
            owner=checksum(message["owner"], "config.owner"),
            daily_cap=int(message["dailyCap"]),
            expiry=int(message["expiry"]),
            agent_addrs=[checksum(a, "config.agentAddrs[]") for a in message["agentAddrs"]],
            agent_caps=[int(c) for c in message["agentCaps"]],
            sectors=[str(s) for s in message["sectors"]],
            nonce=int(message["nonce"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(HTTP_BAD_REQUEST, f"config is not a BudgetConfig message: {exc}") from exc


def remember_budget(runtime: Any, owner: str, config: BudgetConfig, total_atomic: int) -> None:
    """After openBudget: store the owner and the caps in Settings and tell the runtime who the owner is."""
    names_by_address = {address: name for name, address in agent_roster(runtime)}
    agent_caps = {
        names_by_address.get(address, address): cap
        for address, cap in zip(config.agent_addrs, config.agent_caps, strict=True)
    }
    with session_scope(engine_of(runtime)) as session:
        save_api_settings(session, {
            "owner": owner,
            "total_budget_atomic": total_atomic,
            "daily_cap_atomic": config.daily_cap,
            "agent_caps_atomic": agent_caps,
        })
    runtime.owner = owner


# ─── open a budget ───────────────────────────────────────────────────────────

def _prepare(runtime: Any, body: BudgetPrepareBody) -> dict[str, Any]:
    owner = checksum(body.owner, "owner")
    if body.total_budget_atomic < body.daily_cap_atomic:
        raise HTTPException(HTTP_BAD_REQUEST, "total_budget_atomic must be at least daily_cap_atomic")
    spender = controller_address(runtime)
    settings = runtime.settings
    now = int(time.time())
    permit = build_permit(
        chain_id=settings.chain_id,
        ausd_address=runtime.token.address,
        owner=owner,
        spender=spender,
        value=body.total_budget_atomic,
        nonce=runtime.token.nonces(owner),
        deadline=now + PERMIT_DEADLINE_SECONDS,
    )
    config = build_config_from_body(runtime, body, owner, runtime.controller.cfg_nonce(owner), now)
    config_typed = build_budget_config(chain_id=settings.chain_id, controller_address=spender, config=config)
    return {"permit": permit, "config": config_typed}


@router.post("/api/budget/prepare")
async def prepare_budget(body: BudgetPrepareBody, runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """Permit + BudgetConfig typed data (fresh on-chain nonces) for the wallet to sign unchanged."""
    return await call_blocking(_prepare, runtime, body)


def _check_permit(runtime: Any, owner: str, spender: str, permit: SignedPermit, daily_cap: int) -> None:
    """
    Recover the permit signer with the token's CURRENT nonce. Rule for a permit someone already
    submitted (front-run): it recovers with nonce − 1 and the allowance already covers the daily
    cap — openBudget tolerates that (try/catch), so it is accepted too.
    """
    settings = runtime.settings
    if permit.deadline <= int(time.time()):
        raise HTTPException(HTTP_BAD_REQUEST, "permit deadline has passed: prepare the budget again")

    def typed(nonce: int) -> dict:
        return build_permit(chain_id=settings.chain_id, ausd_address=runtime.token.address, owner=owner,
                            spender=spender, value=permit.value, nonce=nonce, deadline=permit.deadline)

    nonce = runtime.token.nonces(owner)
    if recover_typed_data_signer(typed(nonce), permit.signature) == owner:
        return
    already_used = nonce > 0 and recover_typed_data_signer(typed(nonce - 1), permit.signature) == owner
    if already_used and runtime.token.allowance(owner, spender) >= daily_cap:
        return
    require_signer(typed(nonce), permit.signature, owner, "permit")


def _open(runtime: Any, body: BudgetOpenBody) -> dict[str, str]:
    owner = checksum(body.owner, "owner")
    require_signature(body.permit.signature, "permit.signature")
    require_signature(body.config_signature, "config_signature")
    config = parse_config_message(body.config)
    if config.owner != owner:
        raise HTTPException(HTTP_BAD_REQUEST, f"config.owner {config.owner} is not the owner {owner}")
    spender = controller_address(runtime)
    settings = runtime.settings

    _check_permit(runtime, owner, spender, body.permit, config.daily_cap)
    config_typed = build_budget_config(chain_id=settings.chain_id, controller_address=spender, config=config)
    require_signer(config_typed, body.config_signature, owner, "budget config")
    on_chain_nonce = runtime.controller.cfg_nonce(owner)
    if config.nonce != on_chain_nonce:
        raise HTTPException(HTTP_BAD_REQUEST, f"config nonce {config.nonce} is stale (on-chain {on_chain_nonce}): "
                                              "prepare the budget again")

    result = runtime.controller.open_budget(
        owner, body.permit.value, body.permit.deadline, to_signature(body.permit.signature), config,
        body.config_signature,
    )
    remember_budget(runtime, owner, config, body.permit.value)
    return {"tx_hash": tx_hash_of(result)}


@router.post("/api/budget/open")
async def open_budget(body: BudgetOpenBody, runtime: Any = Depends(get_runtime)) -> dict[str, str]:
    """Verify both signatures are the owner's, then the relayer sends openBudget. → {tx_hash}"""
    return await call_blocking(_open, runtime, body)


# ─── pause / resume ──────────────────────────────────────────────────────────

def _set_paused_typed(runtime: Any, owner: str, paused: bool, deadline: int) -> dict:
    """SetPaused typed data with the controller's current cfgNonce (shared with BudgetConfig)."""
    return build_set_paused(
        chain_id=runtime.settings.chain_id,
        controller_address=controller_address(runtime),
        owner=owner,
        paused=paused,
        nonce=runtime.controller.cfg_nonce(owner),
        deadline=deadline,
    )


def _prepare_pause(runtime: Any, body: PreparePauseBody) -> dict[str, Any]:
    owner = checksum(body.owner, "owner")
    deadline = int(time.time()) + PAUSE_DEADLINE_SECONDS
    return {"typed_data": _set_paused_typed(runtime, owner, body.paused, deadline)}


@router.post("/api/budget/prepare-pause")
async def prepare_pause(body: PreparePauseBody, runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """SetPaused typed data for a gasless pause (paused=true) or resume (paused=false)."""
    return await call_blocking(_prepare_pause, runtime, body)


def _pause(runtime: Any, body: PauseBody) -> dict[str, str]:
    owner = checksum(body.owner, "owner")
    require_signature(body.signature, "signature")
    if body.deadline <= int(time.time()):
        raise HTTPException(HTTP_BAD_REQUEST, "pause signature deadline has passed: prepare it again")
    require_signer(_set_paused_typed(runtime, owner, body.paused, body.deadline), body.signature, owner, "SetPaused")
    result = runtime.controller.set_paused_with_sig(owner, body.paused, body.deadline, body.signature)
    return {"tx_hash": tx_hash_of(result)}


@router.post("/api/budget/pause")
async def pause_budget(body: PauseBody, runtime: Any = Depends(get_runtime)) -> dict[str, str]:
    """Relay a signed SetPaused (setPausedWithSig). → {tx_hash}"""
    return await call_blocking(_pause, runtime, body)


# ─── revoke (permit with value 0) ────────────────────────────────────────────

def _revoke_typed(runtime: Any, owner: str, deadline: int) -> dict:
    """ERC-2612 permit owner → BudgetController with value 0 and the token's current nonce."""
    return build_permit(
        chain_id=runtime.settings.chain_id,
        ausd_address=runtime.token.address,
        owner=owner,
        spender=controller_address(runtime),
        value=0,
        nonce=runtime.token.nonces(owner),
        deadline=deadline,
    )


def _prepare_revoke(runtime: Any, body: PrepareRevokeBody) -> dict[str, Any]:
    owner = checksum(body.owner, "owner")
    return {"typed_data": _revoke_typed(runtime, owner, int(time.time()) + PERMIT_DEADLINE_SECONDS)}


@router.post("/api/budget/prepare-revoke")
async def prepare_revoke(body: PrepareRevokeBody, runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """Permit typed data with value 0 (revokes the controller's allowance)."""
    return await call_blocking(_prepare_revoke, runtime, body)


def _revoke(runtime: Any, body: RevokeBody) -> dict[str, str]:
    owner = checksum(body.owner, "owner")
    require_signature(body.signature, "signature")
    if body.deadline <= int(time.time()):
        raise HTTPException(HTTP_BAD_REQUEST, "revoke deadline has passed: prepare it again")
    require_signer(_revoke_typed(runtime, owner, body.deadline), body.signature, owner, "revoke permit")
    result = controller_module.submit_permit(
        runtime.relayer, runtime.token, owner, controller_address(runtime), 0, body.deadline,
        to_signature(body.signature),
    )
    return {"tx_hash": tx_hash_of(result)}


@router.post("/api/budget/revoke")
async def revoke_budget(body: RevokeBody, runtime: Any = Depends(get_runtime)) -> dict[str, str]:
    """The relayer submits the signed value-0 permit to AUSD. → {tx_hash}"""
    return await call_blocking(_revoke, runtime, body)
