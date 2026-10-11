"""
Buyer side of x402: how a PocketDesk agent pays for one research item.

    1. GET the item                         → server answers 402 + payment requirements
    2. choose a requirement                 → first one whose (network, asset) is an enabled
                                              token in our registry (AUSD preferred), price ≤ cap
    3. compliance check                     → isAccountFrozen(payTo) and isAccountFrozen(agent);
                                              an RPC failure here means "do not pay" (fails closed)
    4. sign an ERC-3009 authorization       → single-use nonce, exact amount, exact recipient,
                                              valid for min(seller's maxTimeoutSeconds, 120 s)
    5. PRE-FLIGHT eth_call                  → simulate the transfer; if it would revert, do NOT
                                              send it (pause → global breaker, else agent backoff)
    6. retry the GET with PAYMENT-SIGNATURE → server verifies, settles on-chain, returns data
                                              + PAYMENT-RESPONSE (tx hash)

Ambiguous outcome (timeout, 5xx, unreadable answer, or a 402 that carries a settlement tx):
a signed authorization stays executable until its `validBefore`, so a second authorization
signed too early could ALSO be settled and the item would be paid twice. Rule:

    * check `authorizationState(agent, nonce)` — used → the payment happened ("ambiguous_paid",
      with the tx hash found from the token's AuthorizationUsed event);
    * unused → keep checking until the chain's latest block is at/after `validBefore`. From then on
      the authorization can never execute, so "unused" is final;
    * only then retry ONCE with a NEW authorization. A nonce is never reused.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from eth_account import Account

from app.agents.breaker import CircuitBreaker
from app.chain.ausd import AusdClient
from app.chain.eip712 import (
    build_transfer_with_authorization,
    new_authorization,
    sign_typed_data,
)
from app.chain.tokens import TokenInfo, TokenRegistry
from app.chain.web3client import redact_urls
from app.constants import (
    DEFAULT_PAYMENT_TIMEOUT_SECONDS,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
)
from app.x402 import preflight
from app.x402.models import (
    ExactEvmPayload,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    SettlementResponse,
    decode_header,
    encode_header,
    parse_payment_required,
)

HTTP_TIMEOUT_SECONDS = 60.0                 # the unpaid GET (402 offer)
MAX_PAYMENT_ATTEMPTS = 2                    # the first try + at most one retry, always with a fresh nonce

# Authorization lifetime = min(seller's maxTimeoutSeconds, MAX). Below MIN the seller (which needs
# ≥ 6 s of headroom to settle) cannot use it, so such offers are refused.
MAX_AUTHORIZATION_SECONDS = DEFAULT_PAYMENT_TIMEOUT_SECONDS
MIN_AUTHORIZATION_SECONDS = 10

# The paid GET may take compute + settlement; wait for the whole authorization lifetime plus this grace.
PAID_REQUEST_GRACE_SECONDS = 30

# Resolving an ambiguous result: poll the chain until its latest block passes validBefore.
AMBIGUOUS_POLL_SECONDS = 2.0                # Base produces a block about every 2 s
EXPIRY_WAIT_GRACE_SECONDS = 60              # give up if the chain has not passed validBefore by then

SleepFn = Callable[[float], Awaitable[None]]

# On-chain status of a signed authorization after an ambiguous result.
AUTHORIZATION_USED = "used"     # executed: the agent paid
AUTHORIZATION_DEAD = "dead"     # unused and past validBefore: can never execute, safe to sign a new one
AUTHORIZATION_OPEN = "open"     # may still execute (or the chain could not be read yet)


class PaymentError(Exception):
    """A purchase did not complete. `kind` is machine-readable (frozen, paused, price_above_cap, ...).

    `kind == "ambiguous_paid"` means the agent WAS charged (tx_hash, if found) but received no data.
    """

    def __init__(self, kind: str, reason: str, tx_hash: str | None = None):
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason
        self.tx_hash = tx_hash


@dataclass
class PaidResult:
    data: dict
    price_atomic: int
    tx_hash: str | None
    payer: str
    pay_to: str
    seller_url: str
    token_symbol: str
    network: str
    settlement: SettlementResponse
    compliance_checked: bool = True
    preflight: str = preflight.OK


def authorization_lifetime(requirement: PaymentRequirements) -> int:
    """Seconds the signed authorization stays valid: min(maxTimeoutSeconds, MAX_AUTHORIZATION_SECONDS).

    A seller cannot stretch an agent's bearer authorization beyond MAX_AUTHORIZATION_SECONDS.
    """
    return min(int(requirement.maxTimeoutSeconds), MAX_AUTHORIZATION_SECONDS)


def json_object_or_empty(response: httpx.Response) -> dict:
    """The response body as a JSON object, or {} if it is not one (e.g. an HTML gateway error page)."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class X402Client:
    def __init__(
        self,
        *,
        private_key: str,
        registry: TokenRegistry,
        token_clients: dict[str, AusdClient],   # token address (lower-case) → client on its chain
        max_amount_atomic: int,
        agent_name: str = "agent",
        breaker: CircuitBreaker | None = None,
        http: httpx.AsyncClient | None = None,
        sleep: SleepFn = asyncio.sleep,         # injectable so local-chain tests can mine blocks while waiting
    ):
        self._account = Account.from_key(private_key)
        self.registry = registry
        self.token_clients = {addr.lower(): client for addr, client in token_clients.items()}
        self.max_amount_atomic = max_amount_atomic
        self.agent_name = agent_name
        self.breaker = breaker
        self.http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        self._sleep = sleep

    @property
    def address(self) -> str:
        return self._account.address

    # ─── step 2: choose what to pay ──────────────────────────────────────
    def choose_requirement(self, offer: PaymentRequired) -> tuple[PaymentRequirements, TokenInfo]:
        """First `accepts[]` entry in an enabled registry token, AUSD first. Never an unknown token."""
        candidates = []
        for requirement in offer.accepts:
            token = self.registry.find(requirement.network, requirement.asset)
            if requirement.scheme == "exact" and token is not None:
                candidates.append((requirement, token))
        if not candidates:
            raise PaymentError("unsupported_token", "no accepted (network, asset) is in the PocketDesk token registry")
        candidates.sort(key=lambda pair: 0 if pair[1].is_budget_token else 1)
        requirement, token = candidates[0]
        if int(requirement.amount) > self.max_amount_atomic:
            raise PaymentError("price_above_cap", f"price {requirement.amount} > cap {self.max_amount_atomic}")
        if authorization_lifetime(requirement) < MIN_AUTHORIZATION_SECONDS:
            raise PaymentError("bad_offer", f"maxTimeoutSeconds {requirement.maxTimeoutSeconds} < {MIN_AUTHORIZATION_SECONDS}")
        return requirement, token

    # ─── steps 3–5: compliance, sign, pre-flight ─────────────────────────
    def _token_client(self, token: TokenInfo) -> AusdClient:
        client = self.token_clients.get(token.address.lower())
        if client is None:
            raise PaymentError("unsupported_token", f"no RPC client configured for {token.symbol}")
        return client

    def _check_compliance(self, token_client: AusdClient, pay_to: str) -> None:
        """Freeze check on payTo and on the agent. Frozen → skip; unreadable → skip too (fails closed)."""
        try:
            frozen = token_client.is_account_frozen(pay_to) or token_client.is_account_frozen(self.address)
        except Exception as exc:  # RPC failure: an unanswered check is not a pass
            self._record_failure("compliance check unavailable")
            raise PaymentError("compliance_unavailable", "skipped: compliance check unavailable (RPC error)") from exc
        if frozen:
            raise PaymentError("frozen", "skipped: counterparty frozen")

    def _sign_checked_payment(self, requirement: PaymentRequirements, token: TokenInfo, resource) -> PaymentPayload:
        token_client = self._token_client(token)
        self._check_compliance(token_client, requirement.payTo)

        auth = new_authorization(
            from_address=self.address,
            to=requirement.payTo,
            value=int(requirement.amount),
            timeout_seconds=authorization_lifetime(requirement),
        )
        typed = build_transfer_with_authorization(chain_id=token.chain_id, ausd_address=token.address, auth=auth)
        typed["domain"]["name"], typed["domain"]["version"] = token.eip712_name, token.eip712_version
        signature = sign_typed_data(self._account.key.hex(), typed).hex

        check = preflight.simulate_transfer_with_authorization(token_client, auth, signature)
        if not check.ok:
            if self.breaker is not None:
                if check.is_pause:
                    self.breaker.trip_global(f"{token.symbol} paused ({check.message[:120]})")
                else:
                    self.breaker.record_failure(self.agent_name, f"pre-flight {check.kind}")
            raise PaymentError(check.kind, f"pre-flight simulation reverted: {check.message[:200]}")

        return PaymentPayload(
            resource=resource,
            accepted=requirement,
            payload=ExactEvmPayload(signature=signature, authorization=auth.to_x402_json()),
        )

    # ─── the whole purchase ──────────────────────────────────────────────
    async def get(self, url: str, params: dict | None = None) -> PaidResult:
        """Buy one item. Returns the data with its receipt, or raises PaymentError (see `kind`).

        Outcomes of the paid request: 200 → paid result; 502 {charged:false} → not charged;
        402 → refused (confirmed on-chain first if it names a tx); anything else → ambiguous:
        resolved on-chain, then at most one retry with a NEW authorization.
        """
        if self.breaker is not None:
            allowed, reason = self.breaker.allows(self.agent_name)
            if not allowed:
                raise PaymentError("breaker_open", reason)

        first = await self.http.get(url, params=params)
        if first.status_code != 402:
            raise PaymentError("unexpected_status", f"expected 402, got {first.status_code}")
        offer = parse_payment_required(first.headers, first.content, url)
        requirement, token = self.choose_requirement(offer)

        for attempt in range(MAX_PAYMENT_ATTEMPTS):
            payment = self._sign_checked_payment(requirement, token, offer.resource)
            response, detail = await self._send_paid_request(url, params, payment, requirement)

            if response is not None and response.status_code == 200:
                return await self._paid_result(response, payment, requirement, token, url)
            if response is not None and response.status_code == 502 and json_object_or_empty(response).get("charged") is False:
                # The seller could not produce the item and did not settle: nothing was paid.
                raise PaymentError("seller_error", f"seller could not compute: {json_object_or_empty(response).get('error')}")
            if response is not None and response.status_code == 402:
                await self._raise_rejection(response, payment, token)

            # Ambiguous (timeout / 5xx / anything else): wait until the first authorization is dead.
            await self._raise_if_paid(payment, token, self._settlement_tx(response))
            if attempt == MAX_PAYMENT_ATTEMPTS - 1:
                self._record_failure(detail)
                raise PaymentError("ambiguous", f"not charged; no usable answer after retry: {detail}")
        raise PaymentError("ambiguous", "unreachable")

    async def _send_paid_request(self, url, params, payment, requirement) -> tuple[httpx.Response | None, str]:
        """GET with PAYMENT-SIGNATURE. Waits for the whole authorization lifetime + grace (the seller may still settle)."""
        timeout = authorization_lifetime(requirement) + PAID_REQUEST_GRACE_SECONDS
        try:
            response = await self.http.get(
                url, params=params, headers={HEADER_PAYMENT_SIGNATURE: encode_header(payment)}, timeout=timeout
            )
        except httpx.HTTPError as exc:
            return None, redact_urls(f"{type(exc).__name__}: {exc}")
        return response, f"HTTP {response.status_code}"

    async def _raise_rejection(self, response: httpx.Response, payment: PaymentPayload, token: TokenInfo) -> None:
        """402 after paying: a definite refusal, unless a settlement tx was broadcast (then confirm on-chain first)."""
        settlement = self._settlement_from(response)
        reason = (settlement.errorReason if settlement else None) or "payment rejected"
        tx_hash = settlement.transaction if settlement else None
        if tx_hash:
            await self._raise_if_paid(payment, token, tx_hash)
        self._record_failure(reason)
        raise PaymentError("rejected", reason, tx_hash)

    # ─── resolving an ambiguous result on-chain ──────────────────────────
    async def _raise_if_paid(self, payment: PaymentPayload, token: TokenInfo, tx_hash: str | None) -> None:
        """Return only once this authorization can NEVER execute and has not. Raise if it was (or may be) paid.

        Loop: USED → raise "ambiguous_paid"; DEAD → return (final: not paid); OPEN → sleep and look
        again. If the chain has not passed validBefore within the grace period, raise "ambiguous"
        (outcome still open — the caller must not retry).
        """
        token_client = self._token_client(token)
        nonce = payment.payload.authorization["nonce"]
        valid_before = int(payment.payload.authorization["validBefore"])
        give_up_at = valid_before + EXPIRY_WAIT_GRACE_SECONDS
        while True:
            status = self._authorization_status(token_client, nonce, valid_before)
            if status == AUTHORIZATION_USED:
                self._raise_paid_without_data(token_client, nonce, tx_hash)
            if status == AUTHORIZATION_DEAD:
                return
            if time.time() > give_up_at:
                self._record_failure("payment outcome unknown")
                raise PaymentError("ambiguous", f"outcome unknown: chain has not passed validBefore {valid_before}", tx_hash)
            await self._sleep(AMBIGUOUS_POLL_SECONDS)

    def _authorization_status(self, token_client: AusdClient, nonce: str, valid_before: int) -> str:
        """USED (executed), DEAD (unused and can never execute), or OPEN (may still execute / cannot tell yet).

        DEAD needs the latest block at/after validBefore AND the nonce still unused at that point:
        a transfer mined just before validBefore is in that state, every later one would revert.
        """
        try:
            if token_client.authorization_state(self.address, nonce):
                return AUTHORIZATION_USED
            if token_client.chain_timestamp() < valid_before:
                return AUTHORIZATION_OPEN
            used = token_client.authorization_state(self.address, nonce)
        except Exception:  # noqa: BLE001 — transient RPC error: cannot tell yet, keep waiting
            return AUTHORIZATION_OPEN
        return AUTHORIZATION_USED if used else AUTHORIZATION_DEAD

    def _raise_paid_without_data(self, token_client: AusdClient, nonce: str, tx_hash: str | None) -> None:
        """The authorization was used on-chain: the agent paid, but no data arrived."""
        found = tx_hash
        if found is None:
            try:
                found = token_client.find_authorization_tx(self.address, nonce)
            except Exception:  # noqa: BLE001 — the hash is only for the report; the payment fact stands
                found = None
        self._record_failure("paid, data not delivered")
        raise PaymentError("ambiguous_paid", "paid, data not delivered (authorization used on-chain)", found)

    # ─── helpers ─────────────────────────────────────────────────────────
    def _record_failure(self, reason: str) -> None:
        if self.breaker is not None:
            self.breaker.record_failure(self.agent_name, reason)

    @staticmethod
    def _settlement_from(response: httpx.Response | None) -> SettlementResponse | None:
        header = response.headers.get(HEADER_PAYMENT_RESPONSE) if response is not None else None
        if not header:
            return None
        try:
            return decode_header(header, SettlementResponse)
        except Exception:  # noqa: BLE001 — unreadable receipt header is treated as absent
            return None

    def _settlement_tx(self, response: httpx.Response | None) -> str | None:
        settlement = self._settlement_from(response)
        return settlement.transaction if settlement else None

    async def _paid_result(self, response, payment, requirement, token, url) -> PaidResult:
        """200: data + a successful PAYMENT-RESPONSE. Anything less is checked on-chain before reporting."""
        settlement = self._settlement_from(response)
        data = json_object_or_empty(response)
        if settlement is None or not settlement.success or not data:
            await self._raise_if_paid(payment, token, settlement.transaction if settlement else None)
            self._record_failure("200 without a usable receipt and data")
            raise PaymentError("no_receipt", "200 without a successful PAYMENT-RESPONSE and a JSON body; not charged")
        if self.breaker is not None:
            self.breaker.record_success(self.agent_name)
        return PaidResult(
            data=data,
            price_atomic=int(requirement.amount),
            tx_hash=settlement.transaction,
            payer=self.address,
            pay_to=requirement.payTo,
            seller_url=url,
            token_symbol=token.symbol,
            network=requirement.network,
            settlement=settlement,
            compliance_checked=True,
            preflight=preflight.OK,
        )
