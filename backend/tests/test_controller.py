"""
Tests for app/chain/controller.py — the BudgetController client, against the real contract and
MockAUSD on a local anvil chain (chain id 84532). Every budget is opened through the real flow:
the owner signs an ERC-2612 permit and an EIP-712 BudgetConfig, the relayer (keeper) submits them.
"""

from __future__ import annotations

import pytest
from eth_account import Account

from app.chain import eip712
from app.chain.controller import (
    BudgetControllerClient,
    BudgetMode,
    ReadOnlyClientError,
    bytes32,
    controller_error_name,
    signature_bytes,
    submit_permit,
)
from app.chain.web3client import TxRevertedError
from app.constants import ONE_AUSD

UNREGISTERED_AGENT = "0x00000000000000000000000000000000DeaDBeef"


def _revert_name(exc_info: pytest.ExceptionInfo[TxRevertedError]) -> str | None:
    """The BudgetController custom error a TxRevertedError carries."""
    return controller_error_name(exc_info.value.reason)


# ─── pure helpers (no chain) ─────────────────────────────────────────────────

def test_signature_bytes_accepts_hex_bytes_and_signature_objects():
    # Arrange
    raw = bytes(range(65))
    signature = eip712.Signature(v=27, r=raw[:32], s=raw[32:64], signature=raw)

    # Act
    results = [signature_bytes(raw), signature_bytes("0x" + raw.hex()), signature_bytes(signature)]

    # Assert
    assert results == [raw, raw, raw]


def test_bytes32_rejects_values_that_are_not_32_bytes():
    # Act + Assert
    assert bytes32("0x" + "ab" * 32) == bytes.fromhex("ab" * 32)
    with pytest.raises(ValueError):
        bytes32("0x1234")


# ─── opening ─────────────────────────────────────────────────────────────────

@pytest.mark.chain
def test_open_budget_stores_config_agents_and_allowance(controller, opened_budget, mock_ausd):
    # Act
    budget = controller.budget(opened_budget.owner)
    first_agent = controller.agent(opened_budget.owner, opened_budget.agents[0])

    # Assert
    assert budget.exists and not budget.paused
    assert budget.daily_cap == opened_budget.daily_cap
    assert budget.expiry == opened_budget.expiry
    assert budget.mode == BudgetMode.PERMIT
    assert budget.cfg_nonce == 1 == controller.cfg_nonce(opened_budget.owner)
    assert first_agent.active and first_agent.daily_cap == opened_budget.agent_caps[0]
    assert first_agent.sector == opened_budget.sectors[0]
    assert controller.get_agents(opened_budget.owner) == opened_budget.agents
    assert mock_ausd.allowance(opened_budget.owner, controller.address) == opened_budget.permit_value
    assert controller.remaining_today(opened_budget.owner) == opened_budget.daily_cap


@pytest.mark.chain
def test_open_budget_reverts_on_replayed_config_nonce(controller, opened_budget, budget_signer, chain_clock):
    # Arrange: a config signed with the already-consumed nonce 0.
    stale = budget_signer.config(
        opened_budget.owner, daily_cap=opened_budget.daily_cap, expiry=opened_budget.expiry,
        agents=opened_budget.agents, agent_caps=opened_budget.agent_caps, sectors=opened_budget.sectors, nonce=0,
    )
    deadline = chain_clock.now() + 3_600
    permit_sig = budget_signer.sign_permit(opened_budget.owner_key, opened_budget.permit_value, deadline)
    stale_sig = budget_signer.sign_config(opened_budget.owner_key, stale)

    # Act + Assert
    with pytest.raises(TxRevertedError) as exc_info:
        controller.open_budget(opened_budget.owner, opened_budget.permit_value, deadline, permit_sig, stale, stale_sig)
    assert _revert_name(exc_info) == "BadNonce"


@pytest.mark.chain
def test_open_voucher_budget_needs_no_allowance(controller, budget_signer, wallets, chain_clock, mock_ausd):
    # Arrange
    owner = wallets.address("user")
    config = budget_signer.config(
        owner, daily_cap=5 * ONE_AUSD, expiry=chain_clock.now() + 86_400,
        agents=[wallets.address("agent")], agent_caps=[5 * ONE_AUSD], sectors=["NewsSentiment"],
    )

    # Act
    controller.open_voucher_budget(config, budget_signer.sign_config(wallets.user, config))

    # Assert
    assert controller.budget(owner).mode == BudgetMode.VOUCHER
    assert mock_ausd.allowance(owner, controller.address) == 0


# ─── recharging ──────────────────────────────────────────────────────────────

@pytest.mark.chain
def test_recharge_moves_ausd_from_owner_to_agent_and_controller_holds_nothing(controller, opened_budget, mock_ausd):
    # Arrange
    agent = opened_budget.agents[0]
    owner_before = mock_ausd.balance_of(opened_budget.owner)
    amount = 2_500_000  # 2.5 AUSD

    # Act
    result = controller.recharge(opened_budget.owner, agent, amount)

    # Assert
    assert result.ok
    assert mock_ausd.balance_of(agent) == amount
    assert mock_ausd.balance_of(opened_budget.owner) == owner_before - amount
    assert mock_ausd.balance_of(controller.address) == 0
    assert controller.agent(opened_budget.owner, agent).spent_today == amount
    assert controller.remaining_today(opened_budget.owner) == opened_budget.daily_cap - amount
    assert controller.agent_remaining_today(opened_budget.owner, agent) == opened_budget.agent_caps[0] - amount


@pytest.mark.chain
def test_recharge_reverts_when_daily_cap_exceeded(controller, opened_budget):
    # Arrange: 6 + 4 AUSD fills the 10 AUSD daily cap.
    owner, (first, second) = opened_budget.owner, opened_budget.agents
    controller.recharge(owner, first, opened_budget.agent_caps[0])
    controller.recharge(owner, second, opened_budget.agent_caps[1])

    # Act + Assert
    with pytest.raises(TxRevertedError) as exc_info:
        controller.recharge(owner, first, 1)
    assert _revert_name(exc_info) == "DailyCapExceeded"


@pytest.mark.chain
def test_recharge_reverts_when_agent_cap_exceeded(controller, opened_budget):
    # Arrange: the second agent's cap is 4 AUSD; the budget still has room.
    owner, second = opened_budget.owner, opened_budget.agents[1]
    controller.recharge(owner, second, opened_budget.agent_caps[1])

    # Act + Assert
    with pytest.raises(TxRevertedError) as exc_info:
        controller.recharge(owner, second, 1)
    assert _revert_name(exc_info) == "AgentCapExceeded"


@pytest.mark.chain
def test_recharge_reverts_for_unregistered_agent(controller, opened_budget):
    # Act + Assert
    with pytest.raises(TxRevertedError) as exc_info:
        controller.recharge(opened_budget.owner, UNREGISTERED_AGENT, ONE_AUSD)
    assert _revert_name(exc_info) == "AgentInactive"


@pytest.mark.chain
def test_recharge_reverts_after_budget_expiry(controller, opened_budget, chain_clock):
    # Arrange
    chain_clock.warp_to(opened_budget.expiry)

    # Act + Assert
    with pytest.raises(TxRevertedError) as exc_info:
        controller.recharge(opened_budget.owner, opened_budget.agents[0], ONE_AUSD)
    assert _revert_name(exc_info) == "BudgetExpired"
    assert controller.remaining_today(opened_budget.owner) == 0


# ─── pausing ─────────────────────────────────────────────────────────────────

@pytest.mark.chain
def test_paused_budget_rejects_recharge_until_resumed_with_signature(
    controller, opened_budget, budget_signer, chain_clock
):
    # Arrange
    owner, agent = opened_budget.owner, opened_budget.agents[0]
    deadline = chain_clock.now() + 3_600
    controller.set_paused_with_sig(owner, True, deadline, budget_signer.sign_set_paused(opened_budget.owner_key, True, deadline))

    # Act + Assert: paused
    assert controller.budget(owner).paused is True
    with pytest.raises(TxRevertedError) as exc_info:
        controller.recharge(owner, agent, ONE_AUSD)
    assert _revert_name(exc_info) == "BudgetPaused"

    # Act + Assert: resumed with the next nonce
    controller.set_paused_with_sig(owner, False, deadline, budget_signer.sign_set_paused(opened_budget.owner_key, False, deadline))
    assert controller.recharge(owner, agent, ONE_AUSD).ok
    assert controller.cfg_nonce(owner) == 3


@pytest.mark.chain
def test_set_paused_with_sig_reverts_when_signature_is_replayed(controller, opened_budget, budget_signer, chain_clock):
    # Arrange
    deadline = chain_clock.now() + 3_600
    sig = budget_signer.sign_set_paused(opened_budget.owner_key, True, deadline)
    controller.set_paused_with_sig(opened_budget.owner, True, deadline, sig)

    # Act + Assert: the nonce moved on, so the same signature no longer matches.
    with pytest.raises(TxRevertedError) as exc_info:
        controller.set_paused_with_sig(opened_budget.owner, True, deadline, sig)
    assert _revert_name(exc_info) == "BadSignature"


# ─── config updates and revoking ─────────────────────────────────────────────

@pytest.mark.chain
def test_update_config_with_sig_deactivates_dropped_agent(controller, opened_budget, budget_signer):
    # Arrange: keep only the first agent.
    owner, (first, second) = opened_budget.owner, opened_budget.agents
    config = budget_signer.config(
        owner, daily_cap=opened_budget.daily_cap, expiry=opened_budget.expiry,
        agents=[first], agent_caps=[opened_budget.agent_caps[0]], sectors=[opened_budget.sectors[0]],
    )

    # Act
    controller.update_config_with_sig(config, budget_signer.sign_config(opened_budget.owner_key, config))

    # Assert
    assert controller.get_agents(owner) == [first]
    assert controller.agent(owner, second).active is False
    with pytest.raises(TxRevertedError):
        controller.recharge(owner, second, ONE_AUSD)


@pytest.mark.chain
def test_revoke_with_zero_value_permit_makes_recharge_fail(controller, opened_budget, budget_signer, mock_ausd, relayer, chain_clock):
    # Arrange: the owner signs a permit with value 0; the keeper submits it straight to the token.
    deadline = chain_clock.now() + 3_600
    zero_permit = budget_signer.sign_permit(opened_budget.owner_key, 0, deadline)

    # Act
    submit_permit(relayer, mock_ausd, opened_budget.owner, controller.address, 0, deadline, zero_permit)

    # Assert
    assert mock_ausd.allowance(opened_budget.owner, controller.address) == 0
    with pytest.raises(TxRevertedError):
        controller.recharge(opened_budget.owner, opened_budget.agents[0], ONE_AUSD)


# ─── read-only client ────────────────────────────────────────────────────────

@pytest.mark.chain
def test_read_only_client_reads_but_refuses_to_send(w3, controller, opened_budget):
    # Arrange
    reader = BudgetControllerClient(w3, controller.address)

    # Act
    remaining = reader.remaining_today(opened_budget.owner)

    # Assert
    assert remaining == opened_budget.daily_cap
    with pytest.raises(ReadOnlyClientError):
        reader.recharge(opened_budget.owner, opened_budget.agents[0], ONE_AUSD)


@pytest.mark.chain
def test_budget_of_unknown_owner_does_not_exist(controller):
    # Act
    budget = controller.budget(Account.create().address)

    # Assert
    assert budget.exists is False
    assert budget.cfg_nonce == 0
