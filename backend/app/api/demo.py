"""
Demo wallet: open a budget from the dashboard WITHOUT a browser wallet (local / testnet demos only).

    GET  /api/demo/status  → {available, reason, owner, chain_id, local_chain}
    POST /api/demo/fund    {mode: "permit"|"voucher", total_budget_atomic, daily_cap_atomic, plan_days,
                            agent_caps_atomic?}
                           → {owner, mode, steps: [{label, tx_hash}]}

The server plays the user with `DEMO_USER_PRIVATE_KEY` (a testnet / anvil-only key). It runs the
SAME code paths as the wallet flow, only the signature comes from the server instead of the browser:

    1. faucet      the relayer calls requestFunds(demo user) when its AUSD balance is too low
    2. owner       the demo user becomes the owner stored in Settings
    3a. permit     budget._prepare → sign permit + BudgetConfig → budget._open   (openBudget)
    3b. voucher    vouchers._prepare → sign 7 daily vouchers + BudgetConfig → vouchers._submit
                                                                               (openVoucherBudget)

Rules: the demo wallet is available only when the key is set AND the chain is Base Sepolia (84532)
or a local dev chain (31337) — never on a mainnet. The key never leaves this module: responses
carry the demo user's ADDRESS only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from eth_account import Account
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api import budget as budget_api
from app.api import vouchers as vouchers_api
from app.api.deps import (
    HTTP_BAD_REQUEST,
    call_blocking,
    engine_of,
    get_runtime,
    get_settings_dep,
    tx_hash_of,
)
from app.chain import vouchers as voucher_lib
from app.chain.eip712 import sign_typed_data
from app.chain.faucet import request_test_ausd
from app.config import Settings
from app.constants import ONE_AUSD
from app.db import save_api_settings, session_scope

router = APIRouter(tags=["demo"])

DEMO_CHAIN_IDS = (84532, 31337)          # Base Sepolia, local anvil/hardhat default
LOCAL_RPC_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1")
DEFAULT_TOTAL_BUDGET_ATOMIC = 20 * ONE_AUSD
DEFAULT_DAILY_CAP_ATOMIC = 5 * ONE_AUSD
DEFAULT_PLAN_DAYS = 7
MODE_PERMIT = "permit"
MODE_VOUCHER = "voucher"


# ─── request body ────────────────────────────────────────────────────────────

class DemoFundBody(BaseModel):
    mode: Literal["permit", "voucher"] = MODE_PERMIT
    total_budget_atomic: int = Field(default=DEFAULT_TOTAL_BUDGET_ATOMIC, gt=0)
    daily_cap_atomic: int = Field(default=DEFAULT_DAILY_CAP_ATOMIC, gt=0)
    plan_days: int = Field(default=DEFAULT_PLAN_DAYS, ge=1, le=budget_api.MAX_PLAN_DAYS)
    agent_caps_atomic: dict[str, int] = Field(default_factory=dict)


# ─── availability ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DemoStatus:
    available: bool
    reason: str
    owner: str | None
    chain_id: int
    local_chain: bool

    def to_api(self) -> dict[str, Any]:
        return {"available": self.available, "reason": self.reason, "owner": self.owner,
                "chain_id": self.chain_id, "local_chain": self.local_chain}


def is_local_rpc(rpc_url: str) -> bool:
    """True when the RPC points at this machine (an anvil started by `make local-stack`)."""
    return (urlparse(rpc_url).hostname or "") in LOCAL_RPC_HOSTS


def demo_address(settings: Settings) -> str | None:
    """The demo user's address, or None when the key is missing or not a valid private key."""
    if not settings.demo_user_private_key:
        return None
    try:
        return Account.from_key(settings.demo_user_private_key).address
    except (ValueError, TypeError):
        return None


def demo_status(settings: Settings) -> DemoStatus:
    """Rule: available ⇔ a valid DEMO_USER_PRIVATE_KEY AND chain id 84532 or 31337."""
    owner = demo_address(settings)
    local = is_local_rpc(settings.rpc_url_base_sepolia)
    if not settings.demo_user_private_key:
        reason = "DEMO_USER_PRIVATE_KEY is not set in the backend .env"
    elif owner is None:
        reason = "DEMO_USER_PRIVATE_KEY is not a valid private key"
    elif settings.chain_id not in DEMO_CHAIN_IDS:
        reason = f"demo wallet is disabled on chain {settings.chain_id} (only 84532 or 31337)"
    else:
        where = "local anvil chain" if local else "Base Sepolia testnet"
        return DemoStatus(True, f"demo wallet ready on the {where}", owner, settings.chain_id, local)
    return DemoStatus(False, reason, owner, settings.chain_id, local)


@router.get("/api/demo/status")
async def get_demo_status(settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    """Can the server open a budget as the demo user? (No runtime or RPC needed.)"""
    return demo_status(settings).to_api()


# ─── funding steps ───────────────────────────────────────────────────────────

def step(label: str, tx_hash: str | None) -> dict[str, Any]:
    return {"label": label, "tx_hash": tx_hash}


def faucet_if_needed(runtime: Any, owner: str, needed_atomic: int) -> dict[str, Any] | None:
    """The relayer requests test AUSD for the demo user when its balance is below what the budget needs."""
    if runtime.token.balance_of(owner) >= needed_atomic:
        return None
    if runtime.relayer is None:
        raise RuntimeError("relayer not configured (RELAYER_PRIVATE_KEY): cannot fund the demo user")
    result = request_test_ausd(runtime.relayer, runtime.settings.ausd_faucet, owner)
    return step("faucet: test AUSD to the demo user", tx_hash_of(result))


def set_owner(runtime: Any, owner: str, agent_caps: dict[str, int]) -> None:
    """Store the demo user as the owner (and any requested agent caps) before opening the budget."""
    values: dict[str, Any] = {"owner": owner}
    if agent_caps:
        values["agent_caps_atomic"] = agent_caps
    with session_scope(engine_of(runtime)) as session:
        save_api_settings(session, values)
    runtime.owner = owner


def open_permit_budget(runtime: Any, owner: str, key: str, body: DemoFundBody) -> dict[str, Any]:
    """Same as the wallet flow /api/budget/prepare → sign both messages → /api/budget/open."""
    prepare_body = budget_api.BudgetPrepareBody(
        owner=owner, total_budget_atomic=body.total_budget_atomic, daily_cap_atomic=body.daily_cap_atomic,
        plan_days=body.plan_days, agent_caps_atomic=body.agent_caps_atomic,
    )
    prepared = budget_api._prepare(runtime, prepare_body)
    permit = prepared["permit"]["message"]
    open_body = budget_api.BudgetOpenBody(
        owner=owner,
        permit=budget_api.SignedPermit(
            value=int(permit["value"]), deadline=int(permit["deadline"]),
            signature=sign_typed_data(key, prepared["permit"]).hex,
        ),
        config=prepared["config"]["message"],
        config_signature=sign_typed_data(key, prepared["config"]).hex,
    )
    opened = budget_api._open(runtime, open_body)
    return step("openBudget (permit + signed config)", opened["tx_hash"])


def signed_voucher(key: str, day: int, value: int, typed: dict[str, Any]) -> vouchers_api.SignedVoucherBody:
    """One prepared voucher, signed by the demo user, in the /api/vouchers/submit shape."""
    valid_after, valid_before = voucher_lib.day_bounds(day)
    return vouchers_api.SignedVoucherBody(
        day_index=day, value=value, valid_after=valid_after, valid_before=valid_before,
        nonce=typed["message"]["nonce"], signature=sign_typed_data(key, typed).hex,
    )


def open_voucher_budget(runtime: Any, owner: str, key: str, daily_values: list[int]) -> dict[str, Any]:
    """Same as the wallet flow /api/vouchers/prepare → sign 7 vouchers + config → /api/vouchers/submit."""
    prepared = vouchers_api._prepare(runtime, vouchers_api.VoucherPrepareBody(
        owner=owner, daily_values_atomic=daily_values,
    ))
    signed = [
        signed_voucher(key, day, value, typed)
        for day, value, typed in zip(prepared["day_indexes"], daily_values, prepared["vouchers"], strict=True)
    ]
    submitted = vouchers_api._submit(runtime, vouchers_api.VoucherSubmitBody(
        owner=owner, vouchers=signed, config=prepared["config"]["message"],
        config_signature=sign_typed_data(key, prepared["config"]).hex,
    ))
    return step(f"openVoucherBudget ({submitted['stored']} daily vouchers signed)", submitted["tx_hash"])


def fund_demo_budget(runtime: Any, key: str, owner: str, body: DemoFundBody) -> dict[str, Any]:
    """Faucet (if needed) → owner → open the budget in the requested mode. → {owner, mode, steps}"""
    if body.total_budget_atomic < body.daily_cap_atomic:
        raise HTTPException(HTTP_BAD_REQUEST, "total_budget_atomic must be at least daily_cap_atomic")
    daily_values = [body.daily_cap_atomic] * voucher_lib.DAYS_PER_WEEK
    needed = body.total_budget_atomic if body.mode == MODE_PERMIT else sum(daily_values)
    steps = []
    faucet_step = faucet_if_needed(runtime, owner, needed)
    if faucet_step is not None:
        steps.append(faucet_step)
    set_owner(runtime, owner, body.agent_caps_atomic)
    if body.mode == MODE_PERMIT:
        steps.append(open_permit_budget(runtime, owner, key, body))
    else:
        steps.append(open_voucher_budget(runtime, owner, key, daily_values))
    return {"owner": owner, "mode": body.mode, "steps": steps}


@router.post("/api/demo/fund")
async def demo_fund(request: Request, body: DemoFundBody | None = None) -> dict[str, Any]:
    """Open a budget as the demo user (no browser wallet). 400 when the demo wallet is unavailable."""
    body = body or DemoFundBody()
    settings: Settings = get_settings_dep(request)
    status = demo_status(settings)
    if not status.available:
        raise HTTPException(HTTP_BAD_REQUEST, f"demo wallet unavailable: {status.reason}")
    runtime = get_runtime(request)
    return await call_blocking(fund_demo_budget, runtime, settings.demo_user_private_key, status.owner, body)
