"""
The keeper Runtime: one object that wires the whole desk together for the API and the scheduler.

    build_runtime(settings) ──▶ Runtime
        chain      w3, token (AusdClient), controller (BudgetControllerClient), relayer (TxSender)
        payments   registry (tokens agents may sign for), breaker (CircuitBreaker), seller_http
        agents     four SectorAgents (sectors.py), each with its own wallet key and X402Client
        keeper     allocator (allocator.py) + Recharger (recharge.py) + SessionState (session.py)
        output     db (purchases, findings, allocations, alerts), bus (SSE), alerts (Telegram)

The API (`app/api/*`) calls:
    state() / config() / latest_allocations() / events_today() / pm_vs_options()   reads
    start_session()   allocate today's budget → recharge agents (or redeem today's voucher)
    run_tick(name)    one buying round for one or all agents (the scheduler does this every 90 s)
    ask(question)     focused tick for the agents/assets a question is about (router.py)
    stop_session()    learn (allocator posteriors) → sweep agent balances to the owner → daily brief
    pause_agent / resume_agent

Chain reads in `state()` never raise: a failed read shows as null so the dashboard still renders.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, TypeVar

import httpx
from sqlalchemy.engine import Engine
from web3 import Web3

from app.agents.base import SectorAgent, TickReport
from app.agents.breaker import CircuitBreaker
from app.agents.planner import PurchaseCache
from app.agents.router import answer_question
from app.agents.sectors import SECTORS
from app.agents.voi import SigmaLearner
from app.alerts.alerts import TOKEN_PAUSED_BANNER, AlertService, build_daily_brief
from app.alerts.telegram import TelegramBot
from app.chain.ausd import AusdClient
from app.chain.controller import BudgetControllerClient
from app.chain.tokens import TokenInfo, TokenRegistry, default_registry
from app.chain.web3client import TxSender, get_web3, redact_urls
from app.config import Settings, get_settings
from app.constants import (
    AUSD_DECIMALS,
    AUSD_EIP712_NAME,
    AUSD_EIP712_VERSION,
    BASE_SEPOLIA_CAIP2,
    BASE_SEPOLIA_CHAIN_ID,
    BASESCAN_ADDRESS_URL,
    BASESCAN_TX_URL,
)
from app.db import (
    add_allocation_run,
    daily_spend_totals,
    day_key,
    init_db,
    load_api_settings,
    month_to_date_spend,
    session_scope,
    spend_by_agent_on,
    utc_now,
    voucher_for_day,
)
from app.db import latest_allocations as db_latest_allocations
from app.events import EVENT_ALLOCATION, EVENT_ASK, EVENT_BREAKER, EVENT_MARKET, EventBus
from app.keeper.allocator import AgentPosterior, Allocation, allocate, daily_budget
from app.keeper.calendar import CalendarEvent, build_events_today, events_per_agent, today_in
from app.keeper.recharge import SECONDS_PER_DAY, Recharger, is_voucher_mode
from app.keeper.recharge_policy import average_daily_spend, runway_days
from app.keeper.session import SessionState, parse_session_hours
from app.savings import compute_savings
from app.x402.offer import SellerOffer
from app.x402.preflight import make_pause_probe

log = logging.getLogger(__name__)

# ─── constants ────────────────────────────────────────────────────────────────

SELLER_IDS = ("expected_move", "pm_edge", "perps_crowding", "news_sentiment", "insider_cluster", "macro_pulse")
SELLERS_PACKAGE = "app.sellers"
SELLER_HTTP_TIMEOUT_SECONDS = 60.0
RUNWAY_HISTORY_DAYS = 7

T = TypeVar("T")
EventsProvider = Callable[[date, Sequence[str]], Awaitable[list[CalendarEvent]]]
MarketProvider = Callable[[Sequence[str]], Awaitable[list[dict]]]


def _safe(read: Callable[[], T], default: T | None = None) -> T | None:
    """Run a chain/db read; on any error log it (URL-redacted) and return `default`."""
    try:
        return read()
    except Exception as error:  # noqa: BLE001 — the dashboard must render even when the RPC is down
        log.warning("state read failed: %s", redact_urls(f"{type(error).__name__}: {error}"))
        return default


async def _default_events(day: date, watchlist: Sequence[str]) -> list[CalendarEvent]:
    """Today's calendar events (earnings via yfinance, FOMC/CPI, Deribit expiries)."""
    return await build_events_today(day, watchlist)


async def _default_market(watchlist: Sequence[str]) -> list[dict]:
    """Free market strip rows `{symbol, price, change_24h_pct, source, delayed}`."""
    from app.data.market import build_market_strip  # local import: pulls in yfinance

    strip = await build_market_strip(list(watchlist))
    return strip.to_rows()


@dataclass
class _DailyCounter:
    """Turns a process-lifetime counter (relayer gas, tx count) into a "today" figure."""

    day: str = ""
    baseline: int = 0

    def today(self, total: int, now: datetime) -> int:
        """total − total at the first observation of `now`'s UTC day."""
        current_day = day_key(now)
        if current_day != self.day:
            self.day, self.baseline = current_day, total
        return total - self.baseline


# ─── the runtime ─────────────────────────────────────────────────────────────

class Runtime:
    """Everything the API and the scheduler need, built once by `build_runtime`."""

    def __init__(
        self,
        *,
        settings: Settings,
        w3: Web3,
        token: AusdClient,
        controller: BudgetControllerClient | None,
        relayer: TxSender | None,
        registry: TokenRegistry,
        breaker: CircuitBreaker,
        bus: EventBus,
        alerts: AlertService,
        engine: Engine,
        agents: dict[str, SectorAgent],
        seller_http: httpx.AsyncClient,
        offers: Mapping[str, SellerOffer],
        recharger: Recharger | None,
    ):
        self.settings = settings
        self.w3 = w3
        self.token = token
        self.controller = controller
        self.relayer = relayer
        self.registry = registry
        self.breaker = breaker
        self.bus = bus
        self.alerts = alerts
        self.engine = engine
        self.agents = agents
        self.seller_http = seller_http
        self.offers = dict(offers)
        self.recharger = recharger
        self.session = SessionState()
        self.posteriors: dict[str, AgentPosterior] = {name: AgentPosterior(agent=name) for name in agents}
        self.spots: dict[str, float] = {}
        self.market_rows: list[dict] = []
        self.events: list[CalendarEvent] = []
        self.events_provider: EventsProvider = _default_events
        self.market_provider: MarketProvider = _default_market
        self._allocations: list[Allocation] = []
        self._relayer_gas_today = _DailyCounter()
        self._relayer_txs_today = _DailyCounter()
        self.owner: str | None = self._api_settings().get("owner")

    # ─── small helpers ───────────────────────────────────────────────────
    def _api_settings(self) -> dict[str, Any]:
        """The user's stored Settings (watchlist, stakes, caps, session hours, owner)."""
        with session_scope(self.engine) as session:
            return load_api_settings(session)

    def _require_owner(self) -> str:
        """The budget owner, or RuntimeError when no budget was opened yet."""
        owner = self.owner or self._api_settings().get("owner")
        if not owner:
            raise RuntimeError("no owner: open a budget first")
        self.owner = owner
        return owner

    def _require_controller(self) -> BudgetControllerClient:
        """The BudgetController client, or RuntimeError when BUDGET_CONTROLLER_ADDRESS is not set."""
        if self.controller is None or self.recharger is None:
            raise RuntimeError("BudgetController / relayer not configured (BUDGET_CONTROLLER_ADDRESS, RELAYER_PRIVATE_KEY)")
        return self.controller

    def _agent(self, name: str) -> SectorAgent:
        """The named agent, or LookupError."""
        if name not in self.agents:
            raise LookupError(f"unknown agent {name!r}; agents: {', '.join(self.agents)}")
        return self.agents[name]

    def agent_addresses(self) -> dict[str, str]:
        """Agent name → wallet address."""
        return {name: agent.address for name, agent in self.agents.items()}

    def _monthly_remaining(self, stored: Mapping[str, Any], now: datetime) -> int:
        """monthly_cap − month-to-date spend (never below 0)."""
        with session_scope(self.engine) as session:
            spent = month_to_date_spend(session, now)
        return max(0, int(stored["monthly_cap_atomic"]) - spent)

    def _prepare_beliefs(self, stored: Mapping[str, Any]) -> None:
        """Make sure every agent holds beliefs for the current watchlist (with today's spots)."""
        for agent in self.agents.values():
            agent.ensure_beliefs(stored["watchlist"], stored["stakes_usd"], self.spots)

    # ─── reads for the API ───────────────────────────────────────────────
    def config(self) -> dict[str, Any]:
        """docs/API.md GET /api/config."""
        base = self.settings.public_base_url.rstrip("/")
        return {
            "chain_id": self.settings.chain_id,
            "ausd_address": self.token.address,
            "controller_address": self.controller.address if self.controller else self.settings.budget_controller_address,
            "faucet_address": self.settings.ausd_faucet,
            "explorer_tx_url": BASESCAN_TX_URL,
            "explorer_address_url": BASESCAN_ADDRESS_URL,
            "agents": [
                {"name": name, "address": agent.address, "sector": name, "sellers": list(agent.spec.sellers)}
                for name, agent in self.agents.items()
            ],
            "sellers": [
                {"seller_id": offer.seller_id, "path": offer.path, "url": f"{base}{offer.path}",
                 "price_atomic": offer.price_atomic, "pay_to": offer.pay_to, "description": offer.description,
                 "label": "demo vendor"}
                for offer in self.offers.values()
            ],
        }

    def latest_allocations(self) -> dict[str, Any]:
        """docs/API.md GET /api/allocations (empty run when nothing was allocated yet)."""
        with session_scope(self.engine) as session:
            latest = db_latest_allocations(session)
        return latest or {"date": None, "budget_atomic": 0, "allocations": []}

    def events_today(self) -> list[dict[str, Any]]:
        """docs/API.md GET /api/events-today (the last refreshed calendar)."""
        return [event.to_api() for event in self.events]

    def pm_vs_options(self) -> list[dict[str, Any]]:
        """docs/API.md GET /api/pm-vs-options, from the latest bought pm_edge answer."""
        for agent in self.agents.values():
            if "pm_edge" in agent.last_results:
                data, tx_hash = agent.last_results["pm_edge"]
                return [
                    {"ticker": row.get("ticker"), "title": row.get("title"), "asset": row.get("asset"),
                     "strike": row.get("strike"), "expiry": row.get("expiry"), "market_p": row.get("market_p"),
                     "model_p": row.get("model_p"), "edge": row.get("edge"),
                     "edge_after_fees": row.get("edge_after_fees"), "tx_hash": tx_hash}
                    for row in data.get("rows", [])
                ]
        return []

    def state(self) -> dict[str, Any]:
        """docs/API.md State + Update #1 fields (agent ETH, relayer gas today, budget mode, breaker, compliance)."""
        now = utc_now()
        stored = self._api_settings()
        owner = self.owner or stored.get("owner")
        budget = self._budget_block(owner)
        with session_scope(self.engine) as session:
            spend_today = spend_by_agent_on(session, day_key(now))
            mtd_spend = month_to_date_spend(session, now)
            history = daily_spend_totals(session, RUNWAY_HISTORY_DAYS, now.date())
        savings = compute_savings(mtd_spend, stored["subscriptions"], stored["monthly_cap_atomic"], now)
        runway = runway_days(budget.get("allowance_remaining") or 0, average_daily_spend(history))
        agents = self._agent_rows(owner, spend_today)
        return {
            "owner": owner,
            "budget": budget,
            "monthly": savings.monthly_to_api(),
            "runway_days": None if math.isinf(runway) else round(runway, 2),
            "worst_case_exposure": sum(a["balance"] or 0 for a in agents) + (budget.get("remaining_today") or 0),
            "agents": agents,
            "session": self.session.to_api(),
            "savings": savings.to_api(),
            "relayer": self._relayer_block(now),
            "breaker": {**self.breaker.status(), "banner": TOKEN_PAUSED_BANNER if self.breaker.global_open else None},
            "compliance": {
                "freeze_check_before_payment": True,
                "preflight_simulation": True,
                "agents_zero_eth": all((a["eth_balance_wei"] or 0) == 0 for a in agents),
            },
        }

    def _budget_block(self, owner: str | None) -> dict[str, Any]:
        """State.budget from the controller and the token (zeros when there is no owner/controller)."""
        empty = {"exists": False, "daily_cap": 0, "spent_today": 0, "remaining_today": 0, "expiry": 0, "paused": False,
                 "revoked": False, "allowance_remaining": 0, "wallet_balance": 0, "mode": None, "cfg_nonce": 0}
        if not owner or self.controller is None:
            return empty
        controller = self.controller
        view = _safe(lambda: controller.budget(owner))
        if view is None:
            return {**empty, "chain_error": True}
        allowance = _safe(lambda: self.token.allowance(owner, controller.address), 0) or 0
        today_index = (_safe(self.token.chain_timestamp, 0) or 0) // SECONDS_PER_DAY
        mode_name = "voucher" if is_voucher_mode(view.mode) else "permit"
        return {
            "exists": view.exists,
            "daily_cap": view.daily_cap,
            "spent_today": view.spent_today if view.day_index == today_index else 0,
            "remaining_today": _safe(lambda: controller.remaining_today(owner), 0),
            "expiry": view.expiry,
            "paused": view.paused,
            "revoked": view.exists and mode_name == "permit" and allowance == 0,
            "allowance_remaining": allowance,
            "wallet_balance": _safe(lambda: self.token.balance_of(owner), 0),
            "mode": mode_name,
            "cfg_nonce": view.cfg_nonce,
        }

    def _agent_rows(self, owner: str | None, spend_today: Mapping[str, int]) -> list[dict[str, Any]]:
        """State.agents (+ eth_balance_wei, sellers, breaker backoff) — one row per agent."""
        allocations = {a["agent"]: a for a in self.latest_allocations()["allocations"]}
        backoff = self.breaker.status()["agents"]
        rows = []
        for name, agent in self.agents.items():
            on_chain = _safe(lambda: self.controller.agent(owner, agent.address)) if owner and self.controller else None
            allocation = allocations.get(name, {})
            rows.append({
                "name": name,
                "address": agent.address,
                "sector": name,
                "sellers": list(agent.spec.sellers),
                "balance": _safe(lambda: self.token.balance_of(agent.address), None),
                "eth_balance_wei": _safe(lambda: self.token.eth_balance(agent.address), None),
                "allocation": allocation.get("amount_atomic", 0),
                "spent_today": spend_today.get(name, 0),
                "agent_cap": on_chain.daily_cap if on_chain else 0,
                "agent_remaining_today": _safe(lambda: self.controller.agent_remaining_today(owner, agent.address), 0)
                if on_chain else 0,
                "realized_value_per_dollar": round(agent.realized_value_per_dollar(), 4),
                "paused": agent.paused,
                "reasons": allocation.get("reasons", []),
                "backoff": backoff.get(name),
            })
        return rows

    def _relayer_block(self, now: datetime) -> dict[str, Any]:
        """Who pays gas: the relayer's address, ETH balance, gas spent and tx count today."""
        if self.relayer is None:
            return {"address": None, "eth_balance_wei": None, "eth_spent_today_wei": 0, "tx_count_today": 0}
        return {
            "address": self.relayer.address,
            "eth_balance_wei": _safe(self.relayer.eth_balance, None),
            "eth_spent_today_wei": self._relayer_gas_today.today(self.relayer.gas_spent_wei, now),
            "tx_count_today": self._relayer_txs_today.today(self.relayer.tx_count, now),
        }

    # ─── session ─────────────────────────────────────────────────────────
    async def start_session(self) -> dict[str, Any]:
        """Allocate today's budget across agents, then recharge them (or redeem today's voucher)."""
        owner = self._require_owner()
        self._require_controller()
        stored = self._api_settings()
        now = utc_now()
        await self.refresh_events(stored["watchlist"])
        for agent in self.agents.values():
            agent.start_new_day()
        self._prepare_beliefs(stored)
        allocations_payload = await asyncio.to_thread(self._allocate, owner, stored, now)
        recharge_txs = await asyncio.to_thread(
            self.recharger.session_start, owner, self._allocations, self.agent_addresses()
        )
        self.session.start(parse_session_hours(stored["session_hours"]), now)
        return {"allocations": allocations_payload, "recharge_txs": recharge_txs}

    def _today_budget_inputs(self, owner: str) -> tuple[int, int, int]:
        """(daily_cap, allowance_remaining, days_remaining). Voucher mode: today's voucher value, 1 day."""
        view = self.controller.budget(owner)
        chain_now = self.token.chain_timestamp()
        if is_voucher_mode(view.mode):
            with session_scope(self.engine) as session:
                voucher = voucher_for_day(session, owner, chain_now // SECONDS_PER_DAY)
            return view.daily_cap, voucher.value if voucher else 0, 1
        allowance = self.token.allowance(owner, self.controller.address)
        days_remaining = max(1, math.ceil((view.expiry - chain_now) / SECONDS_PER_DAY))
        return view.daily_cap, allowance, days_remaining

    def _yesterday_unspent(self, now: datetime) -> int:
        """B_day of yesterday's latest allocation run − yesterday's spend (0 if no run yesterday)."""
        yesterday = day_key(now - timedelta(days=1))
        latest = self.latest_allocations()
        if latest["date"] != yesterday:
            return 0
        with session_scope(self.engine) as session:
            spent = sum(spend_by_agent_on(session, yesterday).values())
        return max(0, latest["budget_atomic"] - spent)

    def _allocate(self, owner: str, stored: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        """B_day (allocator.daily_budget) → Thompson-sampling split → stored run + `allocation` event."""
        daily_cap, allowance, days_remaining = self._today_budget_inputs(owner)
        budget = daily_budget(
            daily_cap=daily_cap,
            allowance_remaining=allowance,
            days_remaining=days_remaining,
            monthly_cap_remaining=self._monthly_remaining(stored, now),
            yesterday_unspent=self._yesterday_unspent(now),
        )
        today = day_key(now)
        self._allocations = allocate(
            budget.amount_atomic,
            [self.posteriors[name] for name in self.agents],
            events_per_agent(self.events),
            rng_seed=int(today.replace("-", "")),
        )
        with session_scope(self.engine) as session:
            add_allocation_run(session, today, budget.amount_atomic, self._allocations)
        payload = {**self.latest_allocations(), "reason": budget.reason}
        self.bus.publish(EVENT_ALLOCATION, payload)
        return payload

    async def stop_session(self) -> dict[str, Any]:
        """Learn from today (allocator posteriors), sweep every agent balance to the owner, send the brief."""
        owner = self._require_owner()
        if self.recharger is None:
            raise RuntimeError("relayer not configured (RELAYER_PRIVATE_KEY): cannot sweep")
        self.session.stop()
        for name, agent in self.agents.items():
            if agent.outcomes_today:
                self.posteriors[name] = self.posteriors[name].update(agent.realized_value_per_dollar())
        agent_keys = {name: key for name, key in self.settings.agent_keys().items() if name in self.agents}
        sweep_txs = await asyncio.to_thread(self.recharger.sweep, owner, agent_keys)
        self.send_daily_brief()
        return {"sweep_txs": sweep_txs}

    def send_daily_brief(self) -> None:
        """Compose the brief for the current session window (or today) and send it (once per day)."""
        now = utc_now()
        start = self.session.started_at or now.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            self.alerts.send_daily_brief(build_daily_brief(self.engine, start, now + timedelta(seconds=1), now))
        except Exception as error:  # noqa: BLE001 — a failed brief must not undo a sweep
            log.warning("daily brief failed: %s", redact_urls(str(error)))

    # ─── agents ──────────────────────────────────────────────────────────
    async def run_tick(self, agent_name: str | None = None) -> list[dict[str, Any]]:
        """One buying round for one agent (or all, in order); returns every purchase decision."""
        names = [self._agent(agent_name).name] if agent_name else list(self.agents)
        stored = self._api_settings()
        self._prepare_beliefs(stored)
        purchases: list[dict[str, Any]] = []
        for name in names:
            report = await self._focused_tick(self.agents[name], stored, None)
            purchases.extend(report.purchases)
        return purchases

    async def _focused_tick(self, agent: SectorAgent, stored: Mapping[str, Any], assets: Sequence[str] | None) -> TickReport:
        """One agent tick within the monthly cap that is left right now."""
        monthly_remaining = self._monthly_remaining(stored, utc_now())
        return await agent.tick(stored["watchlist"], monthly_remaining_atomic=monthly_remaining, assets=assets)

    async def ask(self, question: str) -> dict[str, Any]:
        """Ask box: route → focused ticks (real purchases) → answer with receipts; also an `ask` event."""
        stored = self._api_settings()
        self._prepare_beliefs(stored)

        async def tick(agent: SectorAgent, assets: Sequence[str]) -> TickReport:
            return await self._focused_tick(agent, stored, assets)

        result = await answer_question(
            question, agents=self.agents, watchlist=stored["watchlist"], tick=tick, settings=self.settings
        )
        self.bus.publish(EVENT_ASK, {"question": question, "answer": result["answer"], "receipts": result["receipts"]})
        return result

    def pause_agent(self, name: str) -> dict[str, Any]:
        """Stop one agent from buying (its wallet keeps its balance until the sweep)."""
        self._agent(name).paused = True
        return {"ok": True, "agent": name, "paused": True}

    def resume_agent(self, name: str) -> dict[str, Any]:
        """Let a paused agent buy again."""
        self._agent(name).paused = False
        return {"ok": True, "agent": name, "paused": False}

    # ─── scheduler jobs (also callable directly) ─────────────────────────
    async def refresh_events(self, watchlist: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Reload today's calendar events (failures keep the previous list)."""
        stored = self._api_settings()
        watchlist = list(watchlist or stored["watchlist"])
        try:
            day = today_in(parse_session_hours(stored["session_hours"]).tz)
            self.events = await self.events_provider(day, watchlist)
        except Exception as error:  # noqa: BLE001 — the calendar only boosts weights; never fatal
            log.warning("calendar refresh failed: %s", redact_urls(str(error)))
        return self.events_today()

    async def refresh_market(self) -> list[dict[str, Any]]:
        """Free market strip → `spots` (for "above:K" strikes) and a `market` event."""
        watchlist = self._api_settings()["watchlist"]
        try:
            rows = await self.market_provider(watchlist)
        except Exception as error:  # noqa: BLE001 — free data only; keep the last rows
            log.warning("market refresh failed: %s", redact_urls(str(error)))
            return self.market_rows
        self.market_rows = rows
        self.spots.update({str(row["symbol"]).upper(): float(row["price"]) for row in rows if row.get("price")})
        self.bus.publish(EVENT_MARKET, rows)
        return rows

    def refill_check(self) -> list[dict[str, Any]]:
        """Intraday refills for agents running dry that spend well (PERMIT mode, live session only)."""
        if not self.session.live or not self._allocations or self.recharger is None or not self.owner:
            return []
        realized = {name: agent.realized_value_per_dollar() for name, agent in self.agents.items()}
        return self.recharger.refill(self.owner, self._allocations, self.agent_addresses(), realized)

    def reprobe_breaker(self) -> bool:
        """Re-run the token pause probe; closes the global breaker when the token is unpaused. Returns open-state."""
        return self.breaker.reprobe(make_pause_probe(self.token))

    def check_alerts(self) -> None:
        """Runway (< 3 days) and daily-cap-reached alerts from on-chain numbers and the spend history."""
        if not self.owner or self.controller is None:
            return
        now = utc_now()
        budget = self._budget_block(self.owner)
        with session_scope(self.engine) as session:
            history = daily_spend_totals(session, RUNWAY_HISTORY_DAYS, now.date())
        self.alerts.check_runway(budget["allowance_remaining"], average_daily_spend(history), budget["exists"])
        self.alerts.check_daily_cap(budget["spent_today"], budget["daily_cap"], day_key(now))

    async def close_if_due(self) -> dict[str, Any] | None:
        """Close a live session at its scheduled end (sweep + daily brief)."""
        if self.session.should_close(utc_now()):
            return await self.stop_session()
        return None


# ─── building ────────────────────────────────────────────────────────────────

def load_offers() -> dict[str, SellerOffer]:
    """Every demo vendor's offer (`app.sellers.<id>.make_offer()`); a seller without a payout address is skipped."""
    offers: dict[str, SellerOffer] = {}
    for seller_id in SELLER_IDS:
        try:
            offers[seller_id] = importlib.import_module(f"{SELLERS_PACKAGE}.{seller_id}").make_offer()
        except Exception as error:  # noqa: BLE001 — e.g. SELLER_PAYTO_* missing: agents skip that seller
            log.warning("seller %s unavailable: %s", seller_id, error)
    return offers


def registry_for(settings: Settings, token: AusdClient) -> TokenRegistry:
    """AUSD at the configured token address (the budget token) + the default optional tokens (USDC)."""
    caip2 = BASE_SEPOLIA_CAIP2 if settings.chain_id == BASE_SEPOLIA_CHAIN_ID else f"eip155:{settings.chain_id}"
    ausd = TokenInfo(
        symbol="AUSD", chain_id=settings.chain_id, caip2=caip2, address=token.address,
        eip712_name=AUSD_EIP712_NAME, eip712_version=AUSD_EIP712_VERSION, decimals=AUSD_DECIMALS,
        faucet=settings.ausd_faucet or None, is_budget_token=True,
    )
    others = [entry for entry in default_registry() if not entry.is_budget_token]
    return TokenRegistry(tokens=[ausd, *others])


def _breaker_with_alerts(bus: EventBus, alerts: AlertService) -> CircuitBreaker:
    """A CircuitBreaker whose changes raise alerts and publish a `breaker` event."""

    def on_change(status: dict) -> None:
        alerts.on_breaker_change(status)
        bus.publish(EVENT_BREAKER, status)

    return CircuitBreaker(on_change=on_change)


def build_runtime(
    settings: Settings | None = None,
    *,
    w3: Web3 | None = None,
    token: AusdClient | None = None,
    controller: BudgetControllerClient | None = None,
    relayer: TxSender | None = None,
    seller_http: httpx.AsyncClient | None = None,
    db_url: str | None = None,
) -> Runtime:
    """
    Wire the desk from settings (Base Sepolia RPC, AUSD, BUDGET_CONTROLLER_ADDRESS, RELAYER key).
    Tests inject a local chain (w3, token, controller, relayer) and an ASGI seller_http.
    Agents without a configured key are left out (logged), as are sellers without a payout address.
    """
    settings = settings or get_settings()
    w3 = w3 or get_web3(settings.rpc_url_base_sepolia)
    token = token or AusdClient(w3, settings.ausd_address)
    if relayer is None and settings.relayer_private_key:
        relayer = TxSender(w3, settings.relayer_private_key, "relayer")
    if controller is None and settings.budget_controller_address:
        controller = BudgetControllerClient(w3, settings.budget_controller_address, relayer)
    engine = init_db(db_url or settings.database_url)
    bus = EventBus()
    telegram = TelegramBot.from_settings(settings)
    alerts = AlertService(engine, bus, notify=telegram.notify if telegram.can_send else None)
    breaker = _breaker_with_alerts(bus, alerts)
    registry = registry_for(settings, token)
    http = seller_http or httpx.AsyncClient(timeout=SELLER_HTTP_TIMEOUT_SECONDS)
    offers = load_offers()
    sigma, cache = SigmaLearner(), PurchaseCache()
    agents: dict[str, SectorAgent] = {}
    for name, spec in SECTORS.items():
        if not settings.agent_keys().get(name):
            log.warning("agent %s has no key configured; it is left out", name)
            continue
        agents[name] = SectorAgent.from_settings(
            spec, settings, registry=registry, token=token, http=http, breaker=breaker,
            offers=offers, seller_base_url=settings.public_base_url, engine=engine, bus=bus, alerts=alerts,
            sigma=sigma, cache=cache,
        )
    recharger = None
    if relayer is not None:
        recharger = Recharger(controller=controller, token=token, relayer=relayer, engine=engine,
                              chain_id=settings.chain_id, bus=bus, alerts=alerts)
    return Runtime(
        settings=settings, w3=w3, token=token, controller=controller, relayer=relayer, registry=registry,
        breaker=breaker, bus=bus, alerts=alerts, engine=engine, agents=agents, seller_http=http,
        offers=offers, recharger=recharger,
    )

