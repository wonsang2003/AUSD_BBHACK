"""
Tests for app/chain/ausd.py — the AUSD client's compliance read and the reads the x402
client uses to resolve an ambiguous payment, against MockAUSD on a real local anvil chain.
"""

from __future__ import annotations

import pytest
import requests
from eth_account import Account
from web3 import Web3

from app.chain.ausd import AusdClient
from app.chain.eip712 import (
    build_transfer_with_authorization,
    new_authorization,
    random_nonce,
    sign_typed_data,
)
from app.constants import BASE_SEPOLIA_CHAIN_ID, ONE_AUSD

pytestmark = pytest.mark.chain

PAYMENT = 50_000
UNREACHABLE_RPC = "http://127.0.0.1:1"


def _pay_with_authorization(mock_ausd: AusdClient, settler, payer_key: str, to: str) -> tuple[str, str]:
    """Sign and settle one real ERC-3009 transfer. Returns (nonce, tx_hash)."""
    payer = Account.from_key(payer_key).address
    auth = new_authorization(from_address=payer, to=to, value=PAYMENT, timeout_seconds=120)
    typed = build_transfer_with_authorization(chain_id=BASE_SEPOLIA_CHAIN_ID, ausd_address=mock_ausd.address, auth=auth)
    signature = sign_typed_data(payer_key, typed).hex
    result = settler.send(mock_ausd.transfer_with_authorization_fn(auth, signature))
    return auth.nonce, result.tx_hash


def test_is_account_frozen_reads_the_freeze_list(mock_ausd, mock_ausd_admin, deployer, wallets, chain_snapshot):
    # Arrange
    account = wallets.address("seller")

    # Act
    deployer.send(mock_ausd_admin.functions.freeze(account))
    frozen = mock_ausd.is_account_frozen(account)
    deployer.send(mock_ausd_admin.functions.unfreeze(account))
    unfrozen = mock_ausd.is_account_frozen(account)

    # Assert
    assert frozen is True
    assert unfrozen is False


def test_is_account_frozen_is_false_for_a_token_without_a_freeze_list(w3, wallets):
    # Arrange: an address with no code has no isAccountFrozen (like a token without a freeze list).
    no_freeze_list = AusdClient(w3, wallets.address("user"))

    # Act
    frozen = no_freeze_list.is_account_frozen(wallets.address("seller"))

    # Assert
    assert frozen is False


def test_is_account_frozen_raises_when_the_rpc_is_unreachable(mock_ausd, wallets):
    # Arrange: a real, dead endpoint — the check could not be performed.
    offline = AusdClient(Web3(Web3.HTTPProvider(UNREACHABLE_RPC, request_kwargs={"timeout": 2})), mock_ausd.address)

    # Act + Assert: never "not frozen" by default.
    with pytest.raises(requests.exceptions.ConnectionError):
        offline.is_account_frozen(wallets.address("seller"))


def test_find_authorization_tx_returns_the_settlement_tx(mock_ausd, settler, fund_ausd, wallets, chain_snapshot):
    # Arrange
    payer = Account.from_key(wallets.agent).address
    fund_ausd(payer, ONE_AUSD)
    nonce, tx_hash = _pay_with_authorization(mock_ausd, settler, wallets.agent, wallets.address("seller"))

    # Act
    found = mock_ausd.find_authorization_tx(payer, nonce)
    unknown = mock_ausd.find_authorization_tx(payer, random_nonce())

    # Assert
    assert found == tx_hash
    assert unknown is None


def test_chain_timestamp_is_the_latest_block_timestamp(mock_ausd, w3):
    # Act
    timestamp = mock_ausd.chain_timestamp()

    # Assert
    assert timestamp == w3.eth.get_block("latest")["timestamp"]
