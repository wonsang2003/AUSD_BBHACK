"""
Tests for app/chain/web3client.py — TxSender's nonce and outcome rules, on a real local anvil chain.

Failures are injected only at the transport boundary (a build step or a send that raises like a
flaky RPC would); the transactions themselves are real and are mined by anvil.
"""

from __future__ import annotations

import threading

import pytest
from web3 import Web3

from app.chain import web3client
from app.chain.web3client import TxPendingError, TxSender, redact_urls

ONE_GWEI = 10**9
DROP_DELAY_SECONDS = 0.3


class _Transfer:
    """A plain ETH transfer shaped like a web3 contract function (estimate_gas / build_transaction)."""

    def __init__(self, w3: Web3, to: str, fail_builds: int = 0):
        self.w3 = w3
        self.to = to
        self.fail_builds = fail_builds

    def estimate_gas(self, tx: dict) -> int:
        return self.w3.eth.estimate_gas({**tx, "to": self.to, "value": 1})

    def build_transaction(self, tx: dict) -> dict:
        if self.fail_builds > 0:
            self.fail_builds -= 1
            raise ConnectionError("RPC went away while building")
        return {**tx, "to": self.to, "value": 1}


def _set_automine(w3: Web3, enabled: bool) -> None:
    w3.provider.make_request("evm_setAutomine", [enabled])


# ─── redaction ───────────────────────────────────────────────────────────────
def test_redact_urls_hides_rpc_api_key_in_url_path():
    # Arrange
    message = "cannot reach https://base-sepolia.g.alchemy.com/v2/SECRET_KEY_123 (timeout)"

    # Act
    redacted = redact_urls(message)

    # Assert
    assert "SECRET_KEY_123" not in redacted
    assert "https://base-sepolia.g.alchemy.com" in redacted


def test_redact_urls_hides_path_printed_by_requests_after_with_url():
    # Arrange
    message = "HTTPConnectionPool(host='127.0.0.1', port=1): Max retries exceeded with url: /v2/SECRET_KEY_123 (Caused by x)"

    # Act
    redacted = redact_urls(message)

    # Assert
    assert "SECRET_KEY_123" not in redacted


# ─── nonce rules ─────────────────────────────────────────────────────────────
@pytest.mark.chain
def test_failure_after_taking_a_nonce_does_not_leave_a_nonce_gap(w3, wallets, chain_snapshot, monkeypatch):
    # Arrange: building the first tx fails like a flaky RPC would.
    monkeypatch.setattr(web3client, "RECEIPT_TIMEOUT_SECONDS", 5)
    sender = TxSender(w3, wallets.relayer, "relayer")
    recipient = wallets.address("user")
    with pytest.raises(ConnectionError):
        sender.send(_Transfer(w3, recipient, fail_builds=1))

    # Act: the next transaction must use the chain's nonce, not one past it.
    result = sender.send(_Transfer(w3, recipient))

    # Assert
    assert result.ok
    assert w3.eth.get_transaction(result.tx_hash)["nonce"] == w3.eth.get_transaction_count(sender.address) - 1


@pytest.mark.chain
def test_dropped_transaction_raises_pending_and_frees_its_nonce(w3, wallets, chain_snapshot, monkeypatch):
    # Arrange: no automatic mining, a short receipt wait, and the mempool is emptied while we wait.
    monkeypatch.setattr(web3client, "RECEIPT_TIMEOUT_SECONDS", 1)
    sender = TxSender(w3, wallets.relayer, "relayer")
    recipient = wallets.address("user")
    chain_nonce = w3.eth.get_transaction_count(sender.address)
    _set_automine(w3, False)
    drop_all = threading.Timer(DROP_DELAY_SECONDS, lambda: w3.provider.make_request("anvil_dropAllTransactions", []))
    try:
        drop_all.start()
        with pytest.raises(TxPendingError) as pending:
            sender.send(_Transfer(w3, recipient))
    finally:
        drop_all.join()
        _set_automine(w3, True)

    # Act: the next transaction reuses the freed nonce, so it is mined instead of waiting behind a gap.
    result = sender.send(_Transfer(w3, recipient))

    # Assert
    assert pending.value.tx_hash
    assert result.ok
    assert w3.eth.get_transaction(result.tx_hash)["nonce"] == chain_nonce


@pytest.mark.chain
def test_send_whose_response_was_lost_is_not_reported_as_failed(w3, wallets, chain_snapshot, monkeypatch):
    # Arrange: the first send reaches the node but its response is lost; the resend is then refused.
    monkeypatch.setattr(web3client, "RETRY_BACKOFF_SECONDS", 0)
    sender = TxSender(w3, wallets.relayer, "relayer")
    real_send = w3.eth.send_raw_transaction
    calls = {"count": 0}

    def send_then_lose_response(raw_tx):
        calls["count"] += 1
        real_send(raw_tx)
        if calls["count"] == 1:
            raise OSError("connection reset after the request was sent")
        raise AssertionError("unreachable: the resend should be refused by the node")

    monkeypatch.setattr(w3.eth, "send_raw_transaction", send_then_lose_response)

    # Act
    result = sender.send(_Transfer(w3, wallets.address("user")))

    # Assert: the hash of the transaction that landed is returned and its receipt is waited for.
    assert result.ok
    assert calls["count"] == 2


@pytest.mark.chain
def test_missing_receipt_is_a_pending_error_not_a_revert(w3, wallets, chain_snapshot, monkeypatch):
    # Arrange
    monkeypatch.setattr(web3client, "RECEIPT_TIMEOUT_SECONDS", 1)
    sender = TxSender(w3, wallets.relayer, "relayer")
    _set_automine(w3, False)
    try:
        # Act
        with pytest.raises(TxPendingError) as pending:
            sender.send(_Transfer(w3, wallets.address("user")))
        w3.provider.make_request("evm_mine", [])
    finally:
        _set_automine(w3, True)

    # Assert: the "pending" transaction did get mined afterwards.
    assert w3.eth.get_transaction_receipt(pending.value.tx_hash)["status"] == 1
