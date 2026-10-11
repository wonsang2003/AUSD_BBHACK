"""
Demo wallet API on anvil (app/api/demo.py + the session preconditions in app/api/session.py):

    GET  /api/demo/status        available with a demo key on chain 84532
    POST /api/session/start      before any budget → 409 "No budget yet …"
    POST /api/demo/fund {}       faucet → owner → permit + BudgetConfig signed server-side → openBudget
    GET  /api/state              shows the budget (exists, daily cap, demo owner)
    POST /api/session/start      now allocates and recharges agents on-chain
    POST /api/demo/fund voucher  opens a voucher-mode budget with 7 stored vouchers

The runtime is built with the anvil chain injected (w3, MockAUSD, BudgetController, relayer); the
faucet address is MockAUSD itself (same `requestFunds`). No browser wallet, no network.
"""

from __future__ import annotations

import httpx
import pytest
from eth_account import Account

from app.config import Settings, get_settings
from app.constants import BASE_SEPOLIA_CHAIN_ID, ONE_AUSD
from app.keeper.runtime import build_runtime
from app.main import create_app

pytestmark = pytest.mark.chain

AGENT_KEYS = {name: "0x" + byte * 32 for name, byte in
              (("OnchainPerps", "d1"), ("OptionsEvent", "d2"), ("NewsSentiment", "d3"), ("MacroFilings", "d4"))}
PAYTO_ENV = ("SELLER_PAYTO_EXPECTED_MOVE", "SELLER_PAYTO_PM_EDGE", "SELLER_PAYTO_PERPS", "SELLER_PAYTO_NEWS",
             "SELLER_PAYTO_INSIDER", "SELLER_PAYTO_MACRO")
SELLER = Account.from_key("0x" + "c2" * 32).address


@pytest.fixture
def seller_env(monkeypatch):
    """Every demo vendor gets a payout address (env overrides the repo .env)."""
    for name in PAYTO_ENV:
        monkeypatch.setenv(name, SELLER)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def demo_app(w3, mock_ausd, relayer, controller, wallets, seller_env):
    """The FastAPI app over an anvil-backed runtime whose demo user is the anvil `user` wallet."""
    settings = Settings(
        agent_onchainperps_key=AGENT_KEYS["OnchainPerps"], agent_optionsevent_key=AGENT_KEYS["OptionsEvent"],
        agent_newssentiment_key=AGENT_KEYS["NewsSentiment"], agent_macrofilings_key=AGENT_KEYS["MacroFilings"],
        demo_user_private_key=wallets.user, seller_settler_private_key=wallets.settler,
        anthropic_api_key="", telegram_bot_token="", telegram_chat_id="", skip_chain_checks=True,
        rpc_url_base_sepolia="http://127.0.0.1:8545", chain_id=BASE_SEPOLIA_CHAIN_ID,
        ausd_address=mock_ausd.address, ausd_faucet=mock_ausd.address, budget_controller_address=controller.address,
        database_url="sqlite://",
    )
    runtime = build_runtime(settings, w3=w3, token=mock_ausd, controller=controller, relayer=relayer,
                            db_url="sqlite://")

    async def no_events(day, watchlist):
        return []

    runtime.events_provider = no_events
    return create_app(runtime=runtime)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60)


async def test_status_is_available_with_a_demo_key_on_base_sepolia(demo_app, wallets):
    async with _client(demo_app) as client:
        status = (await client.get("/api/demo/status")).json()
    assert status == {"available": True, "reason": status["reason"], "owner": wallets.address("user"),
                      "chain_id": BASE_SEPOLIA_CHAIN_ID, "local_chain": True}
    assert wallets.user[2:] not in str(status)


async def test_demo_fund_opens_a_budget_and_the_session_starts(demo_app, mock_ausd, wallets):
    owner = wallets.address("user")
    async with _client(demo_app) as client:
        early = await client.post("/api/session/start")
        assert early.status_code == 409
        assert early.json()["detail"].startswith("No budget yet")

        funded = await client.post("/api/demo/fund", json={})
        assert funded.status_code == 200, funded.text
        body = funded.json()
        assert body["owner"] == owner and body["mode"] == "permit"
        assert body["steps"][-1]["label"] == "openBudget (permit + signed config)"
        assert all(s["tx_hash"].startswith("0x") for s in body["steps"])
        assert mock_ausd.allowance(owner, demo_app.state.ctx.runtime.controller.address) == 20 * ONE_AUSD

        state = (await client.get("/api/state")).json()
        assert state["owner"] == owner
        assert state["budget"]["exists"] is True
        assert state["budget"]["daily_cap"] == 5 * ONE_AUSD

        started = await client.post("/api/session/start")
        assert started.status_code == 200, started.text
        recharges = started.json()["recharge_txs"]
        assert recharges and all(tx["status"] == "ok" for tx in recharges)

        unknown = await client.post("/api/session/tick", json={"agent": "NoSuchAgent"})
        assert unknown.status_code == 400
        assert "unknown agent" in unknown.json()["detail"]


async def test_demo_fund_voucher_mode_stores_seven_vouchers(demo_app, wallets):
    owner = wallets.address("user")
    async with _client(demo_app) as client:
        funded = await client.post("/api/demo/fund", json={"mode": "voucher", "daily_cap_atomic": 2 * ONE_AUSD,
                                                           "total_budget_atomic": 14 * ONE_AUSD})
        assert funded.status_code == 200, funded.text
        assert funded.json()["mode"] == "voucher"
        vouchers = (await client.get("/api/vouchers", params={"owner": owner})).json()
        state = (await client.get("/api/state")).json()
    assert len(vouchers) == 7 and all(v["value"] == 2 * ONE_AUSD for v in vouchers)
    assert state["budget"]["exists"] is True and state["budget"]["mode"] == "voucher"


async def test_demo_fund_is_refused_without_a_demo_key(demo_app):
    demo_app.state.ctx.settings = demo_app.state.ctx.settings.model_copy(update={"demo_user_private_key": ""})
    async with _client(demo_app) as client:
        status = (await client.get("/api/demo/status")).json()
        refused = await client.post("/api/demo/fund", json={})
    assert status["available"] is False and status["owner"] is None
    assert refused.status_code == 400
    assert "DEMO_USER_PRIVATE_KEY" in refused.json()["detail"]
