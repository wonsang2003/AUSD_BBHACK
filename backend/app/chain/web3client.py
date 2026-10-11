"""
web3 connection + `TxSender`: the only place that signs and sends on-chain transactions.

Only two kinds of wallets ever send transactions (and therefore hold ETH for gas):
    * the relayer / keeper   — openBudget, recharge, faucet calls, sweeps
    * the seller settler     — transferWithAuthorization for x402 settlement
Agent wallets never send transactions; they only sign ERC-3009 authorizations.

`TxSender` keeps its own nonce counter behind a lock so concurrent payments from the
same settler do not collide, waits for every receipt, retries only on transport
errors (never on a revert), and keeps a running total of gas spent so the dashboard
can show "who pays gas".

Nonce rule: the local counter may only run ahead of the chain for transactions the node
actually holds. Whenever a transaction did not reach the node (a build/sign/broadcast
error) or was dropped from the mempool, the counter is forgotten and re-read from the
chain, so one failure can never leave a nonce gap that stalls every later transaction.

Outcome rule: a revert is a definite failure (`TxRevertedError`, nothing moved); a missing
receipt is NOT (`TxPendingError`, the transaction may still be mined).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

from eth_account import Account
from web3 import Web3
from web3.exceptions import (
    ContractCustomError,
    ContractLogicError,
    TimeExhausted,
    TransactionNotFound,
)

from app.config import get_settings

log = logging.getLogger(__name__)

GAS_ESTIMATE_BUFFER = 1.2          # send with 20 % more gas than estimated
RECEIPT_TIMEOUT_SECONDS = 120
TRANSPORT_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0
PRIORITY_FEE_WEI = 1_000_000       # 0.001 gwei tip — Base Sepolia is cheap


# RPC API keys often live in the URL path (e.g. Alchemy ".../v2/<key>"); never let them reach logs or UI.
_URL_WITH_PATH = re.compile(r"(https?://[^/\s'\"]+)/[^\s'\"),]*")
_REQUESTS_URL_PATH = re.compile(r"(with url: )/[^\s'\"),]*")
REDACTED_PATH = "/<redacted>"


def redact_urls(text: str) -> str:
    """Keep only scheme://host of every URL in `text` (and drop the path `requests` prints after "with url:")."""
    text = _URL_WITH_PATH.sub(lambda match: match.group(1) + REDACTED_PATH, text)
    return _REQUESTS_URL_PATH.sub(lambda match: match.group(1) + REDACTED_PATH, text)


def get_web3(rpc_url: str | None = None) -> Web3:
    """Connect to the configured RPC (Base Sepolia by default)."""
    url = rpc_url or get_settings().rpc_url_base_sepolia
    return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))


@dataclass(frozen=True)
class TxResult:
    tx_hash: str
    status: int          # 1 = success, 0 = reverted
    block_number: int
    gas_used: int
    gas_cost_wei: int

    @property
    def ok(self) -> bool:
        return self.status == 1


class TxRevertedError(RuntimeError):
    """DEFINITE failure: the transaction (or its gas estimation) reverted, so nothing moved."""

    def __init__(self, reason: str, tx_hash: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.tx_hash = tx_hash


class TxPendingError(RuntimeError):
    """UNKNOWN outcome: the transaction was broadcast but no receipt arrived in time. It may still be mined."""

    def __init__(self, reason: str, tx_hash: str):
        super().__init__(reason)
        self.reason = reason
        self.tx_hash = tx_hash


def describe_revert(exc: Exception) -> str:
    """Turn a web3 revert exception into a readable reason (custom error selector or message)."""
    if isinstance(exc, ContractCustomError):
        return f"custom error {exc.data if hasattr(exc, 'data') else exc}"
    if isinstance(exc, ContractLogicError):
        return str(exc.message or exc)
    return f"{type(exc).__name__}: {exc}"


class TxSender:
    """Signs and sends transactions for one hot wallet (relayer or settler)."""

    def __init__(self, w3: Web3, private_key: str, label: str):
        if not private_key:
            raise ValueError(f"{label}: private key is not configured")
        self.w3 = w3
        self.label = label
        self._account = Account.from_key(private_key)
        self._lock = threading.Lock()
        self._next_nonce: int | None = None
        self.gas_spent_wei = 0
        self.tx_count = 0

    @property
    def address(self) -> str:
        return self._account.address

    def eth_balance(self) -> int:
        return self.w3.eth.get_balance(self.address)

    # ─── nonce handling ──────────────────────────────────────────────────
    def _take_nonce(self) -> int:
        """Next nonce: the larger of our local counter and the chain's pending count."""
        chain_nonce = self.w3.eth.get_transaction_count(self.address, "pending")
        nonce = chain_nonce if self._next_nonce is None else max(chain_nonce, self._next_nonce)
        self._next_nonce = nonce + 1
        return nonce

    def _forget_nonce(self) -> None:
        self._next_nonce = None

    def reset_nonce(self) -> None:
        """Re-read the nonce from the chain on the next send (after a dropped tx or a test chain revert)."""
        with self._lock:
            self._forget_nonce()

    # ─── fees ────────────────────────────────────────────────────────────
    def _fee_fields(self) -> dict:
        base_fee = self.w3.eth.get_block("latest").get("baseFeePerGas", 0) or 0
        return {
            "maxPriorityFeePerGas": PRIORITY_FEE_WEI,
            "maxFeePerGas": 2 * base_fee + PRIORITY_FEE_WEI,
        }

    # ─── sending ─────────────────────────────────────────────────────────
    def send(self, contract_fn) -> TxResult:
        """Estimate, sign, send and wait for a contract call. Raises TxRevertedError on revert."""
        return self._send_tx(lambda tx: contract_fn.build_transaction(tx), estimate=contract_fn.estimate_gas)

    def deploy(self, abi: list, bytecode: str, *constructor_args) -> tuple[str, TxResult]:
        """Deploy a contract; returns (address, TxResult)."""
        factory = self.w3.eth.contract(abi=abi, bytecode=bytecode)
        ctor = factory.constructor(*constructor_args)
        result = self._send_tx(lambda tx: ctor.build_transaction(tx), estimate=ctor.estimate_gas)
        receipt = self.w3.eth.get_transaction_receipt(result.tx_hash)
        return receipt["contractAddress"], result

    def _send_tx(self, build, estimate) -> TxResult:
        try:
            gas_estimate = estimate({"from": self.address})
        except (ContractLogicError, ContractCustomError) as exc:
            raise TxRevertedError(f"{self.label}: would revert — {describe_revert(exc)}") from exc
        # Read everything that can fail on a flaky RPC BEFORE taking a nonce.
        chain_id = self.w3.eth.chain_id
        fee_fields = self._fee_fields()

        with self._lock:
            nonce = self._take_nonce()
            try:
                tx = build({
                    "from": self.address,
                    "nonce": nonce,
                    "gas": int(gas_estimate * GAS_ESTIMATE_BUFFER),
                    "chainId": chain_id,
                    **fee_fields,
                })
                signed = self._account.sign_transaction(tx)
                tx_hash = self._broadcast(signed.raw_transaction, signed.hash.to_0x_hex())
            except Exception:
                self._forget_nonce()  # the node does not hold a tx with this nonce: re-read it next time
                raise

        return self._wait(tx_hash)

    def _node_has_tx(self, tx_hash: str) -> bool:
        """True if the node knows this transaction (pending or mined). False if unknown or unreachable."""
        try:
            self.w3.eth.get_transaction(tx_hash)
        except TransactionNotFound:
            return False
        except Exception:  # noqa: BLE001 — cannot tell; callers treat it as "not there"
            return False
        return True

    def _broadcast(self, raw_tx: bytes, tx_hash: str) -> str:
        """Send the signed tx, retrying transport errors only.

        A transport error can hide a send that actually reached the node; the retry then fails
        with "already known" / "nonce too low". So after any failed attempt we ask the node for
        `tx_hash` before giving up, and return it if an earlier attempt landed.
        """
        last_error: Exception | None = None
        for attempt in range(TRANSPORT_RETRIES):
            try:
                return self.w3.eth.send_raw_transaction(raw_tx).to_0x_hex()
            except (ConnectionError, TimeoutError, OSError) as exc:  # transport only — never a revert
                last_error = exc
                time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
            except Exception:
                if attempt > 0 and self._node_has_tx(tx_hash):
                    return tx_hash
                raise
        if self._node_has_tx(tx_hash):
            return tx_hash
        raise ConnectionError(
            f"{self.label}: could not broadcast after {TRANSPORT_RETRIES} attempts: {redact_urls(str(last_error))}"
        )

    def _wait(self, tx_hash: str) -> TxResult:
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT_SECONDS)
        except TimeExhausted as exc:
            if not self._node_has_tx(tx_hash):
                with self._lock:
                    self._forget_nonce()  # dropped from the mempool: its nonce is free again, no gap
            raise TxPendingError(
                f"{self.label}: no receipt after {RECEIPT_TIMEOUT_SECONDS}s (it may still be mined)", tx_hash
            ) from exc
        gas_cost = receipt["gasUsed"] * receipt.get("effectiveGasPrice", 0)
        self.gas_spent_wei += gas_cost
        self.tx_count += 1
        result = TxResult(
            tx_hash=tx_hash,
            status=receipt["status"],
            block_number=receipt["blockNumber"],
            gas_used=receipt["gasUsed"],
            gas_cost_wei=gas_cost,
        )
        log.info("%s tx %s status=%s gas=%s", self.label, tx_hash, result.status, result.gas_used)
        if not result.ok:
            raise TxRevertedError(f"{self.label}: transaction reverted on-chain", tx_hash)
        return result
