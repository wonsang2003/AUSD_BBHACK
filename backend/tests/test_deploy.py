"""
Tests for scripts/deploy.py and scripts/_env.py — deploying BudgetController on anvil from the
committed artifact, and updating a .env without clobbering values the user set.
"""

from __future__ import annotations

import pytest

from app.chain.controller import BudgetControllerClient
from app.chain.faucet import request_test_ausd
from scripts._env import read_env, set_env_values
from scripts.deploy import deploy_controller, existing_deployment

CONTROLLER_ADDRESS = "0x5FbDB2315678afecb367f032d93F642f64180aa3"


def test_set_env_values_fills_empty_and_keeps_existing_values(tmp_path):
    # Arrange
    env = tmp_path / ".env"
    env.write_text("BUDGET_CONTROLLER_ADDRESS=\nNEXT_PUBLIC_BUDGET_CONTROLLER_ADDRESS=0xkeep\nOTHER=1\n")

    # Act
    update = set_env_values(
        {"BUDGET_CONTROLLER_ADDRESS": CONTROLLER_ADDRESS, "NEXT_PUBLIC_BUDGET_CONTROLLER_ADDRESS": CONTROLLER_ADDRESS},
        path=env,
    )

    # Assert
    assert update.written == ["BUDGET_CONTROLLER_ADDRESS"] and update.kept == ["NEXT_PUBLIC_BUDGET_CONTROLLER_ADDRESS"]
    assert read_env(env) == {
        "BUDGET_CONTROLLER_ADDRESS": CONTROLLER_ADDRESS, "NEXT_PUBLIC_BUDGET_CONTROLLER_ADDRESS": "0xkeep", "OTHER": "1",
    }


def test_set_env_values_overwrites_only_when_asked_and_appends_missing(tmp_path):
    # Arrange
    env = tmp_path / ".env"
    env.write_text("BUDGET_CONTROLLER_ADDRESS=0xold\n")

    # Act
    set_env_values({"BUDGET_CONTROLLER_ADDRESS": CONTROLLER_ADDRESS, "NEW": "x"}, overwrite=True, path=env)

    # Assert
    assert read_env(env) == {"BUDGET_CONTROLLER_ADDRESS": CONTROLLER_ADDRESS, "NEW": "x"}
    assert oct(env.stat().st_mode & 0o777) == "0o600"


@pytest.mark.chain
def test_deploy_controller_sets_keeper_and_token(w3, relayer, mock_ausd, chain_snapshot):
    # Act
    address, result = deploy_controller(relayer, mock_ausd.address, relayer.address)

    # Assert
    contract = BudgetControllerClient(w3, address).contract
    assert result.ok
    assert contract.functions.keeper().call() == relayer.address
    assert contract.functions.ausd().call() == mock_ausd.address
    assert existing_deployment(w3, {"BUDGET_CONTROLLER_ADDRESS": address}) == address
    assert existing_deployment(w3, {"BUDGET_CONTROLLER_ADDRESS": "0x000000000000000000000000000000000000dEaD"}) is None


@pytest.mark.chain
def test_request_test_ausd_mints_faucet_amount_to_recipient(relayer, mock_ausd, wallets, chain_snapshot):
    # Arrange: MockAUSD has the same requestFunds(recipient) as the real faucet.
    recipient = wallets.address("agent2")
    before = mock_ausd.balance_of(recipient)

    # Act
    result = request_test_ausd(relayer, mock_ausd.address, recipient)

    # Assert
    assert result.ok
    assert mock_ausd.balance_of(recipient) == before + 10_000 * 1_000_000
