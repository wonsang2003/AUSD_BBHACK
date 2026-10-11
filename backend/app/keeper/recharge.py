"""
Recharge executor: the keeper moves AUSD into agent wallets and back out (SPEC §6, Update #1 D).

It applies the pure rules of `keeper/recharge_policy.py` with real transactions through
`BudgetControllerClient` (only the keeper/relayer wallet sends transactions and pays gas):

    session start, PERMIT mode   top_up_k = max(0, a_k − balance_k), clipped to on-chain headroom
                                 → controller.recharge(owner, agent, amount) per agent
    session start, VOUCHER mode  today's stored voucher (db.Voucher) is split across agents by the
                                 allocator output → ONE controller.redeem_voucher(...) transaction.
                                 Owner balance below the voucher value → voucher marked failed +
                                 critical alert, nothing is sent.
    intraday (PERMIT mode only)  balance < 20 % of a_k AND value/$ today ≥ threshold AND headroom
                                 → refill min(25 % · a_k, headroom) with the reason. Voucher mode
                                 has no intraday refills (one voucher per day).
    session end                  each agent signs an ERC-3009 transferWithAuthorization(to=owner)
                                 for its whole balance; the keeper submits it via
                                 AusdClient.transfer_with_authorization_fn — the agent needs no gas.

Every step returns plain dicts `{agent, amount_atomic, tx_hash, reason, status, error}` and
publishes a `recharge` / `sweep` event, so a failed transaction is shown honestly, never hidden.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from sqlalchemy.engine import Engine

from app.alerts.alerts import AlertService
from app.chain.ausd import AusdClient
from app.chain.controller import BudgetControllerClient, BudgetMode
from app.chain.eip712 import build_transfer_with_authorization, new_authorization, sign_typed_data
from app.chain.web3client import TxSender, redact_urls
from app.db import (
    VOUCHER_FAILED,
    VOUCHER_REDEEMED,
    VOUCHER_SIGNED,
    set_voucher_status,
    session_scope,
    voucher_for_day,
)
from app.events import EVENT_RECHARGE, EVENT_SWEEP, EventBus
from app.keeper.allocator import Allocation
from app.keeper.recharge_policy import TopUp, intraday_refill, session_top_ups, sweep_amounts

log = logging.getLogger(__name__)

# ─── constants ────────────────────────────────────────────────────────────────

SECONDS_PER_DAY = 86_400
SWEEP_AUTHORIZATION_SECONDS = 300          # the sweep authorization is submitted right away
DEFAULT_REFILL_VALUE_THRESHOLD = 1.0       # refill only agents returning ≥ $1 of value per $1 today
REDEEMABLE_VOUCHER_STATUSES = (VOUCHER_SIGNED, "pending")
STATUS_OK = "ok"
STATUS_FAILED = "failed"
ERROR_TEXT_MAX_CHARS = 200


@dataclass(frozen=True)
class KeeperTx:
    """One keeper action and its outcome (the API / SSE payload is `to_api()`)."""

    agent: str
    amount_atomic: int
    tx_hash: str | None
    reason: str
    status: str = STATUS_OK
    error: str | None = None

    def to_api(self) -> dict[str, Any]:
        """`{agent, amount_atomic, tx_hash, reason, status, error}`."""
        return {
            "agent": self.agent,
            "amount_atomic": self.amount_atomic,
            "tx_hash": self.tx_hash,
            "reason": self.reason,
            "status": self.status,
            "error": self.error,
        }


def is_voucher_mode(mode: Any) -> bool:
    """True for BudgetMode.VOUCHER (also accepts its int value or the string "voucher")."""
    if isinstance(mode, str):
        return mode.lower() == "voucher"
    return int(mode) == int(BudgetMode.VOUCHER)


def _error_text(error: Exception) -> str:
    """Short, URL-redacted description of a failed transaction."""
    return redact_urls(f"{type(error).__name__}: {error}")[:ERROR_TEXT_MAX_CHARS]


# ─── voucher split (pure) ─────────────────────────────────────────────────────

def split_voucher(
    value: int,
    allocations: Sequence[Allocation],
    headroom: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """
    Split a voucher's value across agents in proportion to their allocations:

        share_k = floor(value · a_k / Σa)        (equal split when Σa = 0)
        the rounding remainder goes to agents in allocation order;
        with `headroom`, no agent gets more than its headroom — the excess is handed to agents
        that still have room. Σ shares == value always (the contract requires it), else ValueError.
    """
    if not allocations:
        raise ValueError("no agents to split the voucher across")
    total_weight = sum(max(0, a.amount_atomic) for a in allocations)
    shares: dict[str, int] = {}
    for allocation in allocations:
        weight = max(0, allocation.amount_atomic) if total_weight > 0 else 1
        denominator = total_weight if total_weight > 0 else len(allocations)
        shares[allocation.agent] = value * weight // denominator
    if headroom is not None:
        shares = {agent: min(amount, max(0, headroom.get(agent, 0))) for agent, amount in shares.items()}
    leftover = value - sum(shares.values())
    for allocation in allocations:
        if leftover <= 0:
            break
        room = leftover if headroom is None else max(0, headroom.get(allocation.agent, 0)) - shares[allocation.agent]
        extra = min(leftover, room)
        shares[allocation.agent] += extra
        leftover -= extra
    if leftover > 0:
        raise ValueError(f"voucher value {value} exceeds the agents' on-chain headroom by {leftover}")
    return shares


# ─── the executor ─────────────────────────────────────────────────────────────

class Recharger:
    """Executes top-ups, voucher redemptions and sweeps for one owner's agents."""

    def __init__(
        self,
        *,
        controller: BudgetControllerClient | None,
        token: AusdClient,
        relayer: TxSender,
        engine: Engine,
        chain_id: int,
        bus: EventBus | None = None,
        alerts: AlertService | None = None,
        chain_time: Callable[[], int] | None = None,
    ):
        self.controller = controller
        self.token = token
        self.relayer = relayer
        self.engine = engine
        self.chain_id = chain_id
        self.bus = bus
        self.alerts = alerts
        self._chain_time = chain_time or token.chain_timestamp

    def _require_controller(self) -> BudgetControllerClient:
        """The controller client; RuntimeError when none is configured (sweeps work without one)."""
        if self.controller is None:
            raise RuntimeError("BudgetController not configured (BUDGET_CONTROLLER_ADDRESS)")
        return self.controller

    # ─── session start ───────────────────────────────────────────────────
    def session_start(self, owner: str, allocations: Sequence[Allocation], agents: Mapping[str, str]) -> list[dict]:
        """PERMIT mode: top every agent up to its allocation. VOUCHER mode: redeem today's voucher."""
        budget = self._require_controller().budget(owner)
        if is_voucher_mode(budget.mode):
            return self.redeem_today_voucher(owner, allocations, agents)
        balances = {name: self.token.balance_of(agents[name]) for name in agents}
        agent_headroom = {name: self.controller.agent_remaining_today(owner, agents[name]) for name in agents}
        top_ups = session_top_ups(
            [a for a in allocations if a.agent in agents],
            balances,
            agent_headroom=agent_headroom,
            budget_headroom=self.controller.remaining_today(owner),
        )
        return [self._recharge(owner, agents[top_up.agent], top_up) for top_up in top_ups]

    # ─── intraday ────────────────────────────────────────────────────────
    def refill(
        self,
        owner: str,
        allocations: Sequence[Allocation],
        agents: Mapping[str, str],
        realized_vpd: Mapping[str, float],
        threshold: float = DEFAULT_REFILL_VALUE_THRESHOLD,
    ) -> list[dict]:
        """Intraday refills per recharge_policy.intraday_refill (PERMIT mode only)."""
        if is_voucher_mode(self._require_controller().budget(owner).mode):
            return []
        done: list[dict] = []
        for allocation in allocations:
            address = agents.get(allocation.agent)
            if address is None:
                continue
            top_up = intraday_refill(
                allocation,
                balance=self.token.balance_of(address),
                realized_vpd=realized_vpd.get(allocation.agent, 0.0),
                threshold=threshold,
                headroom=self.controller.agent_remaining_today(owner, address),
            )
            if top_up is not None:
                done.append(self._recharge(owner, address, top_up))
        return done

    def _recharge(self, owner: str, agent_address: str, top_up: TopUp) -> dict:
        """One controller.recharge; a revert or RPC failure is reported, not raised."""
        try:
            result = self.controller.recharge(owner, agent_address, top_up.amount)
            outcome = KeeperTx(top_up.agent, top_up.amount, result.tx_hash, top_up.reason)
        except Exception as error:  # noqa: BLE001 — shown on the dashboard as a failed recharge
            log.warning("recharge %s failed: %s", top_up.agent, _error_text(error))
            outcome = KeeperTx(top_up.agent, top_up.amount, getattr(error, "tx_hash", None), top_up.reason,
                               STATUS_FAILED, _error_text(error))
        return self._publish(EVENT_RECHARGE, outcome)

    # ─── voucher mode ────────────────────────────────────────────────────
    def redeem_today_voucher(self, owner: str, allocations: Sequence[Allocation], agents: Mapping[str, str]) -> list[dict]:
        """
        Redeem the owner's voucher for today's UTC day, split by the allocations. Missing voucher,
        owner balance below its value, or a revert → voucher marked failed (if stored) + critical alert.
        """
        today = self._chain_time() // SECONDS_PER_DAY
        with session_scope(self.engine) as session:
            voucher = voucher_for_day(session, owner, today)
        if voucher is None or voucher.status not in REDEEMABLE_VOUCHER_STATUSES:
            reason = "no signed voucher stored for today" if voucher is None else f"voucher is {voucher.status}"
            self._voucher_failed(None, today, reason)
            return []
        owner_balance = self.token.balance_of(owner)
        if owner_balance < voucher.value:
            self._voucher_failed(voucher.id, today, f"insufficient balance: owner holds {owner_balance} < {voucher.value}")
            return []
        live = [a for a in allocations if a.agent in agents]
        try:
            headroom = {a.agent: self.controller.agent_remaining_today(owner, agents[a.agent]) for a in live}
            shares = split_voucher(voucher.value, live, headroom)
        except ValueError as error:
            self._voucher_failed(voucher.id, today, str(error))
            return []
        names = [name for name, amount in shares.items() if amount > 0]
        try:
            result = self.controller.redeem_voucher(
                owner, voucher.value, voucher.valid_after, voucher.valid_before, voucher.nonce, voucher.signature,
                [agents[name] for name in names], [shares[name] for name in names],
            )
        except Exception as error:  # noqa: BLE001 — revert / RPC failure: mark failed and alert
            self._voucher_failed(voucher.id, today, _error_text(error), getattr(error, "tx_hash", None))
            return []
        with session_scope(self.engine) as session:
            set_voucher_status(session, voucher.id, VOUCHER_REDEEMED, result.tx_hash)
        reason = f"voucher day {today}: {voucher.value} split by allocation"
        return [self._publish(EVENT_RECHARGE, KeeperTx(name, shares[name], result.tx_hash, reason)) for name in names]

    def _voucher_failed(self, voucher_id: int | None, day: int, reason: str, tx_hash: str | None = None) -> None:
        """Mark a stored voucher failed and raise the critical voucher alert."""
        if voucher_id is not None:
            with session_scope(self.engine) as session:
                set_voucher_status(session, voucher_id, VOUCHER_FAILED, tx_hash)
        if self.alerts is not None:
            self.alerts.voucher_failed(day, reason, tx_hash)

    # ─── session end ─────────────────────────────────────────────────────
    def sweep(self, owner: str, agent_keys: Mapping[str, str]) -> list[dict]:
        """Return every positive agent balance to the owner (agent signs, keeper submits)."""
        balances = {name: self.token.balance_of(Account.from_key(key).address) for name, key in agent_keys.items()}
        return [self._sweep_one(owner, agent_keys[sweep.agent], sweep.agent, sweep.amount, sweep.reason)
                for sweep in sweep_amounts(balances)]

    def _sweep_one(self, owner: str, agent_key: str, agent: str, amount: int, reason: str) -> dict:
        """
        The agent signs transferWithAuthorization(from=agent, to=owner, value=amount) valid for
        5 minutes of chain time; the keeper submits it and pays the gas.
        """
        agent_address = Account.from_key(agent_key).address
        auth = new_authorization(
            from_address=agent_address, to=owner, value=amount,
            timeout_seconds=SWEEP_AUTHORIZATION_SECONDS, now=self._chain_time(),
        )
        typed = build_transfer_with_authorization(chain_id=self.chain_id, ausd_address=self.token.address, auth=auth)
        signature = sign_typed_data(agent_key, typed).hex
        try:
            result = self.relayer.send(self.token.transfer_with_authorization_fn(auth, signature))
            outcome = KeeperTx(agent, amount, result.tx_hash, reason)
        except Exception as error:  # noqa: BLE001 — reported as a failed sweep; the funds stay with the agent
            log.warning("sweep %s failed: %s", agent, _error_text(error))
            outcome = KeeperTx(agent, amount, getattr(error, "tx_hash", None), reason, STATUS_FAILED, _error_text(error))
        return self._publish(EVENT_SWEEP, outcome)

    # ─── events ──────────────────────────────────────────────────────────
    def _publish(self, event_type: str, outcome: KeeperTx) -> dict:
        """Publish the outcome on the EventBus and return its API dict."""
        payload = outcome.to_api()
        if self.bus is not None:
            self.bus.publish(event_type, payload)
        return payload
