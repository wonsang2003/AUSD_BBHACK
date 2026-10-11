"""
Tests for scripts/gate_m2.py — everything that can run without Base Sepolia.

The real gate needs network access and funded wallets (run `make gate-m2` on a machine
that can reach Base Sepolia). Here, on a local anvil chain (chain id 84532) with MockAUSD:
  * the zero-ETH rule for the paying agent,
  * the gas-wallet rule (enough ETH for the gate's transactions, not merely "more than 0"),
  * that a passing run is recorded with its tx hash,
  * a full dry run of the gate script itself: real ERC-3009 signature, real settlement
    transaction and receipt on anvil. Only the Deribit downloads come from recorded
    fixture files, because this container cannot reach Deribit.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.chain.web3client import TxSender
from app.config import get_settings
from app.data import deribit
from app.sellers import expected_move
from app.x402.wiring import default_seller_dependencies
from scripts import gate_m2

FIXTURES = Path(__file__).parent / "fixtures"
DERIBIT_FIXTURE_NOW = datetime(2026, 10, 11, 14, 0, tzinfo=UTC)  # when the BTC Deribit fixtures were recorded
ONE_WEI = 1
UNDERFUNDED_KEY = "0x" + "9a" * 32  # throwaway local key, used only on anvil


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def deribit_btc_from_fixtures(monkeypatch):
    """Serve Deribit's BTC downloads from recorded fixture files (this container cannot reach Deribit)."""

    async def book_summary(asset: str) -> dict:
        return load_fixture("deribit_book_summary_btc.json")

    async def index(asset: str) -> dict:
        return load_fixture("deribit_index_btc_usd.json")

    monkeypatch.setattr(deribit, "fetch_book_summary_payload", book_summary)
    monkeypatch.setattr(deribit, "fetch_index_payload", index)
    monkeypatch.setattr(expected_move, "utc_now", lambda: DERIBIT_FIXTURE_NOW)


@pytest.fixture
def gate_settings_on_anvil(anvil_url, mock_ausd, wallets, monkeypatch, tmp_path):
    """Point the gate's settings at anvil + MockAUSD (which has the real faucet's requestFunds signature)."""
    settings = get_settings()
    overrides = {
        "rpc_url_base_sepolia": anvil_url,
        "ausd_address": mock_ausd.address,
        "ausd_faucet": mock_ausd.address,
        "relayer_private_key": wallets.relayer,
        "seller_settler_private_key": wallets.settler,
        "agent_optionsevent_key": wallets.agent,
        "seller_payto_expected_move": wallets.address("seller"),
        "x402_settlement": "self",
    }
    for field, value in overrides.items():
        monkeypatch.setattr(settings, field, value)
    monkeypatch.setattr(gate_m2, "RESULT_PATH", tmp_path / "gate_m2_result.json")
    default_seller_dependencies.cache_clear()  # built lazily from settings: rebuild them for anvil
    yield settings
    default_seller_dependencies.cache_clear()  # never leak anvil-bound dependencies into other tests


# ─── the paying agent holds no ETH ────────────────────────────────────────────

@pytest.mark.chain
def test_gate_fails_when_the_paying_agent_holds_eth(mock_ausd, wallets):
    # Arrange: anvil's dev account "user" holds ETH.
    agent_with_eth = wallets.address("user")

    # Act + Assert
    with pytest.raises(SystemExit) as exit_info:
        gate_m2.require_agent_holds_no_eth(mock_ausd, agent_with_eth)
    assert exit_info.value.code == gate_m2.EXIT_AGENT_HOLDS_ETH


@pytest.mark.chain
def test_gate_accepts_an_agent_with_zero_eth(mock_ausd, wallets):
    # Arrange: the test agent key was never given ETH.
    agent_without_eth = wallets.address("agent")

    # Act + Assert (no SystemExit)
    gate_m2.require_agent_holds_no_eth(mock_ausd, agent_without_eth)


# ─── gas wallets can pay for the gate's transactions ──────────────────────────

@pytest.mark.chain
def test_gate_fails_when_a_gas_wallet_holds_eth_but_too_little_for_gas(w3, chain_snapshot):
    # Arrange: a settler with 1 wei — not zero, yet the node would refuse its settlement tx.
    underfunded = TxSender(w3, UNDERFUNDED_KEY, "seller-settler")
    w3.provider.make_request("anvil_setBalance", [underfunded.address, hex(ONE_WEI)])

    # Act + Assert
    with pytest.raises(SystemExit) as exit_info:
        gate_m2.require_gas_wallet_funded(underfunded)
    assert exit_info.value.code == gate_m2.EXIT_GAS_WALLET_UNDERFUNDED


@pytest.mark.chain
def test_gate_accepts_a_gas_wallet_that_can_pay_for_its_transactions(relayer):
    # Arrange: anvil's funded dev account.
    required = gate_m2.min_gas_wallet_balance(relayer)

    # Act + Assert (no SystemExit)
    gate_m2.require_gas_wallet_funded(relayer)
    assert relayer.eth_balance() >= required > 0


# ─── recording a pass ─────────────────────────────────────────────────────────

def test_passing_gate_is_recorded_with_its_tx_hash(tmp_path, monkeypatch):
    # Arrange
    record_path = tmp_path / "gate_m2_result.json"
    monkeypatch.setattr(gate_m2, "RESULT_PATH", record_path)
    result = SimpleNamespace(tx_hash="0x" + "ab" * 32, payer="0xAgent", pay_to="0xSeller", price_atomic=50_000,
                             token_symbol="AUSD", network="eip155:84532")

    # Act
    gate_m2.write_gate_record(result, block_number=123, url="http://demo-vendor.local/sell/expected-move")

    # Assert
    record = json.loads(record_path.read_text())
    assert record["tx_hash"] == result.tx_hash
    assert record["explorer_url"].endswith(result.tx_hash)
    assert record["block_number"] == 123


# ─── full dry run of the gate script on anvil ─────────────────────────────────

@pytest.mark.chain
async def test_gate_script_passes_end_to_end_on_local_anvil(
    gate_settings_on_anvil, deribit_btc_from_fixtures, mock_ausd, wallets, w3, chain_snapshot
):
    # Arrange: the same arguments as `make gate-m2` (in-process expected_move demo vendor).
    args = argparse.Namespace(seller="expected_move", seller_url=None)
    agent = wallets.address("agent")
    seller = wallets.address("seller")
    seller_balance_before = mock_ausd.balance_of(seller)

    # Act
    await gate_m2.run(args)

    # Assert: a record with a real, successful settlement tx that moved exactly the price.
    record = json.loads(gate_m2.RESULT_PATH.read_text())
    receipt = w3.eth.get_transaction_receipt(record["tx_hash"])
    assert receipt["status"] == 1
    assert record["explorer_url"].endswith(record["tx_hash"])
    assert record["pay_to"].lower() == seller.lower()
    assert mock_ausd.balance_of(seller) - seller_balance_before == record["price_atomic"] > 0
    assert mock_ausd.eth_balance(agent) == 0, "the agent never holds ETH"
