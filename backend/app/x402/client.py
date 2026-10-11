"""
Buyer side of x402: how a PocketDesk agent pays for one research item.

    1. GET the item                         → server answers 402 + payment requirements
    2. choose a requirement                 → first one whose (network, asset) is an enabled
                                              token in our registry (AUSD preferred), price ≤ cap
    3. compliance check                     → isAccountFrozen(payTo) and isAccountFrozen(agent)
    4. sign an ERC-3009 authorization       → single-use nonce, exact amount, exact recipient,
                                              valid for ~2 minutes
    5. PRE-FLIGHT eth_call                  → simulate the transfer; if it would revert, do NOT
                                              send it (pause → global breaker, else agent backoff)
    6. retry the GET with PAYMENT-SIGNATURE → server verifies, settles on-chain, returns data
                                              + PAYMENT-RESPONSE (tx hash)

Ambiguous outcome (timeout / 5xx after the signature was sent): we check
`authorizationState(agent, nonce)`. Used → the payment happened. Unused → at most one
retry with a NEW authorization. A nonce is never reused.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from eth_account import Account

from app.agents.breaker import CircuitBreaker
from app.chain.ausd import AusdClient
from app.chain.eip712 import new_authorization, sign_typed_data, build_transfer_with_authorization
from app.chain.tokens import TokenInfo, TokenRegistry
from app.constants import HEADER_PAYMENT_RESPONSE, HEADER_PAYMENT_SIGNATURE
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

HTTP_TIMEOUT_SECONDS = 60.0
AMBIGUOUS_STATUS_CODES = {500, 502, 503, 504}


class PaymentError(Exception):
    """A purchase did not complete. `kind` is machine-readable (frozen, paused, price_above_cap, ...)."""

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
    ):
        self._account = Account.from_key(private_key)
        self.registry = registry
        self.token_clients = {addr.lower(): client for addr, client in token_clients.items()}
        self.max_amount_atomic = max_amount_atomic
        self.agent_name = agent_name
        self.breaker = breaker
        self.http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)

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
        return requirement, token

    # ─── steps 3–5: compliance, sign, pre-flight ─────────────────────────
    def _token_client(self, token: TokenInfo) -> AusdClient:
        client = self.token_clients.get(token.address.lower())
        if client is None:
            raise PaymentError("unsupported_token", f"no RPC client configured for {token.symbol}")
        return client

    def _sign_checked_payment(self, requirement: PaymentRequirements, token: TokenInfo, resource) -> PaymentPayload:
        token_client = self._token_client(token)
        if token_client.is_account_frozen(requirement.payTo) or token_client.is_account_frozen(self.address):
            raise PaymentError("frozen", "skipped: counterparty frozen")

        auth = new_authorization(
            from_address=self.address,
            to=requirement.payTo,
            value=int(requirement.amount),
            timeout_seconds=requirement.maxTimeoutSeconds,
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
        if self.breaker is not None:
            allowed, reason = self.breaker.allows(self.agent_name)
            if not allowed:
                raise PaymentError("breaker_open", reason)

        first = await self.http.get(url, params=params)
        if first.status_code != 402:
            raise PaymentError("unexpected_status", f"expected 402, got {first.status_code}")
        offer = parse_payment_required(first.headers, first.content, url)
        requirement, token = self.choose_requirement(offer)

        for attempt in range(2):  # at most one retry, always with a fresh nonce
            payment = self._sign_checked_payment(requirement, token, offer.resource)
            try:
                response = await self.http.get(url, params=params, headers={HEADER_PAYMENT_SIGNATURE: encode_header(payment)})
            except httpx.HTTPError as exc:
                response, transport_error = None, exc
            else:
                transport_error = None

            if response is not None and response.status_code == 200:
                return self._paid_result(response, payment, requirement, token, url)
            if response is not None and response.status_code == 502 and response.json().get("charged") is False:
                # The seller could not compute the item and did not settle: nothing was paid.
                raise PaymentError("seller_error", f"seller could not compute: {response.json().get('error')}")
            if response is not None and response.status_code == 402:
                settlement = self._settlement_from(response)
                reason = (settlement.errorReason if settlement else None) or "payment rejected"
                self._record_failure(reason)
                raise PaymentError("rejected", reason, settlement.transaction if settlement else None)

            # Ambiguous: did the authorization get used?
            nonce = payment.payload.authorization["nonce"]
            if self._token_client(token).authorization_state(self.address, nonce):
                raise PaymentError("ambiguous_paid", "payment settled but response was lost (nonce used on-chain)")
            if attempt == 1:
                detail = transport_error or f"HTTP {response.status_code}"
                self._record_failure(str(detail))
                raise PaymentError("ambiguous", f"no response after retry: {detail}")
        raise PaymentError("ambiguous", "unreachable")

    def _record_failure(self, reason: str) -> None:
        if self.breaker is not None:
            self.breaker.record_failure(self.agent_name, reason)

    @staticmethod
    def _settlement_from(response: httpx.Response) -> SettlementResponse | None:
        header = response.headers.get(HEADER_PAYMENT_RESPONSE)
        return decode_header(header, SettlementResponse) if header else None

    def _paid_result(self, response, payment, requirement, token, url) -> PaidResult:
        settlement = self._settlement_from(response)
        if settlement is None or not settlement.success:
            raise PaymentError("no_receipt", "200 without a successful PAYMENT-RESPONSE")
        if self.breaker is not None:
            self.breaker.record_success(self.agent_name)
        return PaidResult(
            data=response.json(),
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
