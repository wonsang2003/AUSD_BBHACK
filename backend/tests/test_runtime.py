"""
End-to-end keeper flow on anvil (app/keeper/runtime.py): open a 4-agent budget → start_session
recharges agents → a tick buys news_sentiment from an in-process demo vendor (real x402 payment)
while another candidate is skipped by the VOI gate → a finding → stop_session sweeps everything back.
Only the seller's upstream DATA fetchers (GDELT) are monkeypatched; the payment path is real.
"""

from datetime import UTC, datetime

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI

from app.agents.beliefs import new_belief
from app.config import Settings, get_settings
from app.constants import BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_CHAIN_ID, ONE_AUSD
from app.data.gdelt import Article, TonePoint
from app.keeper.runtime import build_runtime
from app.sellers import news_sentiment
from app.x402.server import make_paid_router
from app.x402.settlement import SelfSettler
from app.x402.verify import PaymentVerifier

pytestmark = pytest.mark.chain

AGENT_KEYS = {name: "0x" + byte * 32 for name, byte in
              (("OnchainPerps", "b1"), ("OptionsEvent", "b2"), ("NewsSentiment", "b3"), ("MacroFilings", "b4"))}
PAYTO_ENV = ("SELLER_PAYTO_EXPECTED_MOVE", "SELLER_PAYTO_PM_EDGE", "SELLER_PAYTO_PERPS", "SELLER_PAYTO_NEWS",
             "SELLER_PAYTO_INSIDER", "SELLER_PAYTO_MACRO")
SELLER = Account.from_key("0x" + "c1" * 32).address
BASE_URL = "http://demo-vendor.test"


@pytest.fixture
def seller_env(monkeypatch):
    for name in PAYTO_ENV:
        monkeypatch.setenv(name, SELLER)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_gdelt(monkeypatch):
    now = datetime.now(UTC)

    async def articles(query, *args, **kwargs):
        return [Article("Bitcoin ETF inflows top $1 billion", "https://x.test/1", "x.test", now, "English", "US")]

    async def tone(query, *args, **kwargs):
        return [TonePoint(now, -1.0), TonePoint(now, -1.0), TonePoint(now, 2.0), TonePoint(now, 2.0)]

    monkeypatch.setattr(news_sentiment.gdelt, "fetch_articles", articles)
    monkeypatch.setattr(news_sentiment.gdelt, "fetch_tone_timeline", tone)


async def test_session_buys_skips_finds_and_sweeps(
    w3, mock_ausd, settler, relayer, controller, budget_signer, fund_ausd, wallets, chain_clock, seller_env, fake_gdelt
):
    owner_key = wallets.user
    owner = Account.from_key(owner_key).address
    agents = {name: Account.from_key(key).address for name, key in AGENT_KEYS.items()}
    fund_ausd(owner, 100 * ONE_AUSD)
    now = chain_clock.now()
    config = budget_signer.config(owner, daily_cap=2 * ONE_AUSD, expiry=now + 30 * 86_400, agents=list(agents.values()),
                                  agent_caps=[ONE_AUSD] * 4, sectors=list(agents))
    controller.open_budget(owner, 60 * ONE_AUSD, now + 3600, budget_signer.sign_permit(owner_key, 60 * ONE_AUSD, now + 3600),
                           config, budget_signer.sign_config(owner_key, config))

    vendor = FastAPI()
    vendor.include_router(make_paid_router(
        news_sentiment.make_offer(), news_sentiment.compute,
        verifier=PaymentVerifier(mock_ausd, BASE_SEPOLIA_CHAIN_ID, BASE_SEPOLIA_CAIP2),
        settler=SelfSettler(mock_ausd, settler, BASE_SEPOLIA_CAIP2), asset=mock_ausd.address,
    ))
    settings = Settings(
        agent_onchainperps_key=AGENT_KEYS["OnchainPerps"], agent_optionsevent_key=AGENT_KEYS["OptionsEvent"],
        agent_newssentiment_key=AGENT_KEYS["NewsSentiment"], agent_macrofilings_key=AGENT_KEYS["MacroFilings"],
        anthropic_api_key="", telegram_bot_token="", telegram_chat_id="", public_base_url=BASE_URL,
        ausd_address=mock_ausd.address, budget_controller_address=controller.address, chain_id=BASE_SEPOLIA_CHAIN_ID,
    )
    runtime = build_runtime(
        settings, w3=w3, token=mock_ausd, controller=controller, relayer=relayer, db_url="sqlite://",
        seller_http=httpx.AsyncClient(transport=httpx.ASGITransport(app=vendor), base_url=BASE_URL),
    )
    runtime.owner = owner

    async def no_events(day, watchlist):
        return []

    runtime.events_provider = no_events

    started = await runtime.start_session()
    assert started["recharge_txs"] and all(tx["status"] == "ok" for tx in started["recharge_txs"])
    news = runtime.agents["NewsSentiment"]
    assert mock_ausd.balance_of(news.address) > 0

    tick_time = datetime.now(UTC)
    news.beliefs = {}
    news.set_belief(new_belief("NewsSentiment", "BTC", "direction", 1000.0, tick_time, p=0.5))
    news.set_belief(new_belief("NewsSentiment", "ETH", "direction", 1000.0, tick_time, p=0.97))
    report = await news.tick(["BTC", "ETH"])

    statuses = {p["asset"]: p for p in report.purchases}
    assert statuses["BTC"]["status"] == "bought" and statuses["BTC"]["tx_hash"]
    assert statuses["ETH"]["status"] == "skipped" and statuses["ETH"]["reason"].startswith("skipped: EVOI")
    assert report.findings and report.findings[0]["receipts"][0]["tx_hash"] == statuses["BTC"]["tx_hash"]
    assert mock_ausd.balance_of(SELLER) == news_sentiment.PRICE_ATOMIC

    state = runtime.state()
    assert state["budget"]["exists"] and state["budget"]["mode"] == "permit"
    assert {a["name"] for a in state["agents"]} == set(AGENT_KEYS)
    assert runtime.config()["sellers"]

    stopped = await runtime.stop_session()
    assert stopped["sweep_txs"] and all(tx["status"] == "ok" for tx in stopped["sweep_txs"])
    for address in agents.values():
        assert mock_ausd.balance_of(address) == 0
        assert mock_ausd.eth_balance(address) == 0
