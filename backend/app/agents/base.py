"""
SectorAgent: one research agent with its own wallet, beliefs and buying loop (SPEC §5).

One `tick()`:

    1. candidates  — planner: (this agent's sellers) × (watchlist assets it holds beliefs on)
    2. VOI gate    — planner.rank_and_select: EVOI vs price × 1.5, wallet balance, monthly cap,
                     purchase cache; every decision comes back with its numbers and reason
    3. buy         — X402Client.get (freeze check → ERC-3009 signature → eth_call pre-flight →
                     paid GET → on-chain settlement receipt). The circuit breaker is asked first.
    4. learn       — the matching beliefs.py rule moves p (sectors.read_update); the realized |Δp|
                     feeds SigmaLearner (σ for the next EVOI) and the agent's realized value per $
    5. log         — a db.Purchase row for EVERY decision (bought / skipped with a numeric reason /
                     failed with the PaymentError kind), published as `purchase` on the EventBus
    6. findings    — when a belief crosses τ or moves > 0.05, a Finding (findings.py) is stored and
                     published as `finding`, with the receipts (tx hashes) that paid for it

Shared data: the planner never buys the same seller+params twice within its TTL. When a
second belief wants data that is already bought (a duplicate this round, or still cached), the
stored answer is applied to that belief too, at no cost, each receipt at most once per belief.

The agent wallet never sends a transaction and never holds ETH: it only signs ERC-3009
authorizations; sellers settle them and pay the gas.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from eth_account import Account
from sqlalchemy.engine import Engine

from app.agents.beliefs import Belief, apply_update
from app.agents.breaker import CircuitBreaker
from app.agents.findings import FindingDraft, PhraseFn, build_finding, should_emit
from app.agents.llm import phrase_finding
from app.agents.planner import PurchaseCache, generate_candidates, rank_and_select
from app.agents.sectors import SectorSpec, SellerReading, initial_beliefs, read_update
from app.agents.voi import (
    OUTCOME_SKIP_CACHED,
    OUTCOME_SKIP_DUPLICATE,
    BuyDecision,
    PurchaseOutcome,
    SigmaLearner,
    realized_value_per_dollar,
)
from app.alerts.alerts import AlertService
from app.chain.ausd import AusdClient
from app.chain.tokens import TokenRegistry
from app.chain.web3client import redact_urls
from app.config import Settings
from app.constants import ONE_AUSD
from app.db import (
    PURCHASE_BOUGHT,
    PURCHASE_FAILED,
    PURCHASE_SKIPPED,
    Purchase,
    add_finding,
    add_purchase,
    session_scope,
    utc_now,
)
from app.events import EVENT_FINDING, EVENT_PURCHASE, EventBus
from app.x402.client import PaymentError, X402Client
from app.x402.offer import SellerOffer

log = logging.getLogger(__name__)

# ─── constants ────────────────────────────────────────────────────────────────

MAX_PRICE_PER_CALL_ATOMIC = ONE_AUSD // 2      # an agent never pays more than 0.50 AUSD for one item

# PaymentError kinds that mean "we chose not to pay" (status skipped), not "the payment failed".
SKIP_ERROR_KINDS = frozenset({"frozen", "compliance_unavailable", "breaker_open"})
KIND_FROZEN = "frozen"
PREFLIGHT_MESSAGE_PREFIX = "pre-flight"
REUSED_OUTCOMES = (OUTCOME_SKIP_CACHED, OUTCOME_SKIP_DUPLICATE)
ERROR_TEXT_MAX_CHARS = 200

BalanceFn = Callable[[str], int]
Clock = Callable[[], datetime]


# ─── tick report ──────────────────────────────────────────────────────────────

@dataclass
class TickReport:
    """What one tick did: every purchase decision (API dicts) and every finding (API dicts)."""

    agent: str
    purchases: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def receipts(self) -> list[dict[str, Any]]:
        """{seller_id, tx_hash, price_atomic} for every bought purchase of this tick."""
        return [
            {"seller_id": p["seller_id"], "tx_hash": p["tx_hash"], "price_atomic": p["price_atomic"]}
            for p in self.purchases
            if p["status"] == PURCHASE_BOUGHT
        ]


# ─── building an agent's payment client ──────────────────────────────────────

def make_agent_client(
    *,
    name: str,
    private_key: str,
    registry: TokenRegistry,
    token: AusdClient,
    breaker: CircuitBreaker | None,
    http: httpx.AsyncClient,
    max_amount_atomic: int = MAX_PRICE_PER_CALL_ATOMIC,
) -> X402Client:
    """The x402 buyer for one agent wallet (freeze check, pre-flight and breaker built in)."""
    return X402Client(
        private_key=private_key,
        registry=registry,
        token_clients={token.address: token},
        max_amount_atomic=max_amount_atomic,
        agent_name=name,
        breaker=breaker,
        http=http,
    )


# ─── the agent ────────────────────────────────────────────────────────────────

class SectorAgent:
    """One sector agent: wallet (via its X402Client), beliefs, and the buy-learn-report loop."""

    def __init__(
        self,
        spec: SectorSpec,
        *,
        client: X402Client,
        offers: Mapping[str, SellerOffer],
        seller_base_url: str,
        engine: Engine,
        settings: Settings,
        balance_of: BalanceFn,
        bus: EventBus | None = None,
        breaker: CircuitBreaker | None = None,
        alerts: AlertService | None = None,
        sigma: SigmaLearner | None = None,
        cache: PurchaseCache | None = None,
        phrase: PhraseFn = phrase_finding,
        clock: Clock = utc_now,
    ):
        self.spec = spec
        self.client = client
        self.offers = {sid: offer for sid, offer in offers.items() if sid in spec.sellers}
        self.seller_base_url = seller_base_url.rstrip("/")
        self.engine = engine
        self.settings = settings
        self.bus = bus
        self.breaker = breaker
        self.alerts = alerts
        self.sigma = sigma or SigmaLearner()
        self.cache = cache or PurchaseCache()
        self._balance_of = balance_of
        self._phrase = phrase
        self._clock = clock
        self.paused = False                                  # user pause from the dashboard
        self.beliefs: dict[str, Belief] = {}
        self.outcomes_today: list[PurchaseOutcome] = []      # for realized value per $
        self.last_results: dict[str, tuple[dict, str | None]] = {}   # seller → (data, tx_hash)
        self._applied: dict[str, set[str]] = {}              # belief key → receipt tx hashes applied

    @classmethod
    def from_settings(
        cls,
        spec: SectorSpec,
        settings: Settings,
        *,
        registry: TokenRegistry,
        token: AusdClient,
        http: httpx.AsyncClient,
        breaker: CircuitBreaker | None = None,
        **kwargs: Any,
    ) -> SectorAgent:
        """Build the agent with its wallet key from `settings.agent_keys()` (raises if not configured)."""
        private_key = settings.agent_keys().get(spec.name, "")
        if not private_key:
            raise ValueError(f"{spec.name}: agent private key is not configured")
        client = make_agent_client(
            name=spec.name, private_key=private_key, registry=registry, token=token, breaker=breaker, http=http
        )
        return cls(spec, client=client, settings=settings, breaker=breaker, balance_of=token.balance_of, **kwargs)

    # ─── identity and numbers ────────────────────────────────────────────
    @property
    def name(self) -> str:
        """The sector name, e.g. "OptionsEvent"."""
        return self.spec.name

    @property
    def address(self) -> str:
        """The agent wallet address (it signs payments; it never sends transactions)."""
        return self.client.address

    @property
    def prices(self) -> dict[str, int]:
        """seller_id → price in AUSD atomic units, for the sellers this agent can buy from."""
        return {sid: offer.price_atomic for sid, offer in self.offers.items()}

    def balance(self) -> int:
        """Spendable AUSD in the agent wallet; an unreadable balance counts as 0 (nothing is bought)."""
        try:
            return int(self._balance_of(self.address))
        except Exception as error:  # noqa: BLE001 — RPC trouble must not crash a tick
            log.warning("%s: balance read failed: %s", self.name, redact_urls(str(error)))
            return 0

    def realized_value_per_dollar(self) -> float:
        """Σ ex-post EVOI / Σ spend of today's purchases (0 when nothing was bought)."""
        return realized_value_per_dollar(self.outcomes_today)

    def start_new_day(self) -> None:
        """Forget today's purchase outcomes (called when a new session starts)."""
        self.outcomes_today = []

    # ─── beliefs ─────────────────────────────────────────────────────────
    def ensure_beliefs(
        self,
        watchlist: Iterable[str],
        stakes_usd: Mapping[str, float],
        spots: Mapping[str, float],
        now: datetime | None = None,
    ) -> None:
        """Add a fresh belief for every new (asset, decision); drop beliefs on assets no longer watched."""
        now = now or self._clock()
        watched = {asset.upper() for asset in watchlist}
        self.beliefs = {key: belief for key, belief in self.beliefs.items() if belief.asset in watched}
        for belief in initial_beliefs(self.spec, watched, stakes_usd, spots, now):
            self.beliefs.setdefault(belief.key, belief)

    def set_belief(self, belief: Belief) -> None:
        """Replace (or add) one belief — used by tests and by restores."""
        self.beliefs[belief.key] = belief

    # ─── the tick ────────────────────────────────────────────────────────
    async def tick(
        self,
        watchlist: Sequence[str],
        *,
        monthly_remaining_atomic: int | None = None,
        assets: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> TickReport:
        """One planning-buying round (see the module docstring). A paused agent does nothing."""
        report = TickReport(agent=self.name)
        if self.paused:
            return report
        now = now or self._clock()
        focus = [asset.upper() for asset in (assets or watchlist)]
        beliefs = {key: belief for key, belief in self.beliefs.items() if belief.asset in focus}
        candidates = generate_candidates({self.name: self.spec.sellers}, focus, beliefs, self.prices, self.sigma)
        selection = rank_and_select(
            candidates,
            beliefs,
            {self.name: self.balance()},
            cache=self.cache,
            now=now,
            monthly_remaining_atomic=monthly_remaining_atomic,
        )
        for decision in selection.skipped:
            self._record(report, self._skipped_row(decision, decision.reason))
        for decision in selection.bought:
            await self._buy(decision, report, now)
        for decision in selection.skipped:
            if decision.outcome in REUSED_OUTCOMES:
                await self._reuse_shared_data(decision, report, now)
        return report

    # ─── buying one item ─────────────────────────────────────────────────
    def _seller_url(self, seller_id: str) -> str:
        """Full URL of a demo vendor endpoint, e.g. "http://localhost:8000/sell/perps-crowding"."""
        return f"{self.seller_base_url}{self.offers[seller_id].path}"

    async def _buy(self, decision: BuyDecision, report: TickReport, now: datetime) -> None:
        """Ask the breaker, pay via x402, then learn from the answer and record everything."""
        if self.breaker is not None:
            allowed, why_not = self.breaker.allows(self.name)
            if not allowed:
                self._record(report, self._skipped_row(decision, f"skipped: {why_not}"))
                return
        try:
            result = await self.client.get(self._seller_url(decision.seller_id), params=dict(decision.params))
        except PaymentError as error:
            self._record(report, self._payment_error_row(decision, error))
            return
        except Exception as error:  # noqa: BLE001 — network / seller trouble: record it, never crash the tick
            reason = f"failed: error — {type(error).__name__}: {redact_urls(str(error))[:ERROR_TEXT_MAX_CHARS]}"
            self._record(report, self._row(decision, PURCHASE_FAILED, reason))
            return

        belief = self.beliefs[decision.decision_key]
        receipt = {"seller_id": decision.seller_id, "tx_hash": result.tx_hash, "price_atomic": result.price_atomic}
        offer = self.offers[decision.seller_id]
        self.cache.record(decision.seller_id, decision.params, now, {"data": result.data, "receipt": receipt},
                          ttl_seconds=offer.cache_ttl_seconds)
        self.last_results[decision.seller_id] = (result.data, result.tx_hash)

        reading = read_update(decision.seller_id, belief, result.data)
        delta_p = reading.update.delta_p if reading is not None else 0.0
        self.sigma.observe(decision.seller_id, belief.decision_type, abs(delta_p))
        self.outcomes_today.append(PurchaseOutcome(
            stake_usd=belief.stake_usd, p_before=belief.p, tau=belief.tau,
            realized_abs_dp=abs(delta_p), price_atomic=result.price_atomic,
        ))
        row = self._row(decision, PURCHASE_BOUGHT, decision.reason)
        row.price_atomic = result.price_atomic
        row.delta_p = abs(delta_p)
        row.tx_hash, row.payer, row.pay_to = result.tx_hash, result.payer, result.pay_to
        row.token, row.network = result.token_symbol, result.network
        row.compliance_checked, row.preflight = result.compliance_checked, result.preflight
        self._record(report, row)
        if reading is not None:
            await self._apply_reading(belief, reading, receipt, report, now)

    async def _reuse_shared_data(self, decision: BuyDecision, report: TickReport, now: datetime) -> None:
        """Apply an already-bought answer (duplicate this round / cached) to this belief, once per receipt."""
        entry = self.cache.get(decision.seller_id, decision.params, now)
        belief = self.beliefs.get(decision.decision_key)
        if entry is None or belief is None:
            return
        reading = read_update(decision.seller_id, belief, entry["data"])
        if reading is not None:
            await self._apply_reading(belief, reading, entry["receipt"], report, now)

    async def _apply_reading(
        self, belief: Belief, reading: SellerReading, receipt: Mapping[str, Any], report: TickReport, now: datetime
    ) -> None:
        """Move the belief (each receipt counts once per belief) and emit a Finding if the move matters."""
        applied = self._applied.setdefault(belief.key, set())
        receipt_id = str(receipt.get("tx_hash"))
        if receipt_id in applied:
            return
        applied.add(receipt_id)
        updated = apply_update(belief, reading.update, now)
        self.beliefs[belief.key] = updated
        if not should_emit(belief.p, updated.p, belief.tau):
            return
        draft = FindingDraft(
            agent=self.name, asset=belief.asset, decision=belief.decision,
            old_p=belief.p, new_p=updated.p, tau=belief.tau,
            evidence=reading.evidence, receipts=[dict(receipt)], risk=reading.risk,
        )
        finding = await build_finding(draft, settings=self.settings, phrase=self._phrase)
        with session_scope(self.engine) as session:
            add_finding(session, finding)
        payload = finding.to_api()
        if self.bus is not None:
            self.bus.publish(EVENT_FINDING, payload)
        report.findings.append(payload)

    # ─── purchase rows ───────────────────────────────────────────────────
    def _row(self, decision: BuyDecision, status: str, reason: str) -> Purchase:
        """A Purchase row with the decision's numbers (price, EVOI, params) and the given outcome."""
        belief = self.beliefs.get(decision.decision_key)
        offer = self.offers.get(decision.seller_id)
        return Purchase(
            agent=self.name,
            seller_id=decision.seller_id,
            asset=belief.asset if belief else "",
            params=dict(decision.params),
            price_atomic=decision.price_atomic,
            evoi_usd=decision.evoi_usd,
            status=status,
            reason=reason,
            pay_to=offer.pay_to if offer else None,
            payer=self.address,
        )

    def _skipped_row(self, decision: BuyDecision, reason: str) -> Purchase:
        """A skipped decision (VOI gate, cache, balance, monthly cap, breaker)."""
        return self._row(decision, PURCHASE_SKIPPED, reason)

    def _payment_error_row(self, decision: BuyDecision, error: PaymentError) -> Purchase:
        """
        A purchase that did not complete. Rule: kinds in SKIP_ERROR_KINDS (frozen counterparty,
        compliance check unavailable, breaker open) are "skipped" — we chose not to pay; every other
        kind is "failed". A frozen counterparty also raises a compliance alert.
        """
        if error.kind in SKIP_ERROR_KINDS:
            reason = error.reason if error.reason.startswith("skipped") else f"skipped: {error.kind} — {error.reason}"
            row = self._row(decision, PURCHASE_SKIPPED, reason)
        else:
            row = self._row(decision, PURCHASE_FAILED, f"failed: {error.kind} — {error.reason[:ERROR_TEXT_MAX_CHARS]}")
        row.tx_hash = error.tx_hash
        row.compliance_checked = error.kind == KIND_FROZEN or error.reason.startswith(PREFLIGHT_MESSAGE_PREFIX)
        if error.reason.startswith(PREFLIGHT_MESSAGE_PREFIX):
            row.preflight = error.kind
        if error.kind == KIND_FROZEN and self.alerts is not None:
            self.alerts.counterparty_frozen(self.name, row.pay_to or "unknown", "payTo or agent")
        return row

    def _record(self, report: TickReport, purchase: Purchase) -> None:
        """Store the row, publish it as a `purchase` event, and add it to the report."""
        with session_scope(self.engine) as session:
            add_purchase(session, purchase)
        payload = purchase.to_api()
        if self.bus is not None:
            self.bus.publish(EVENT_PURCHASE, payload)
        report.purchases.append(payload)


def agent_address(private_key: str) -> str:
    """Address of an agent key (helper for config/state without building a client)."""
    return Account.from_key(private_key).address
