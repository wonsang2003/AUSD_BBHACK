"""SectorAgent tick bookkeeping without a chain (app/agents/base.py); the paid path is in test_runtime.py."""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.agents.base import SectorAgent
from app.agents.beliefs import new_belief
from app.agents.sectors import SECTORS
from app.config import Settings
from app.db import init_db, recent_purchases, session_scope
from app.x402.offer import SellerOffer

NOW = datetime(2026, 10, 11, 14, 0, tzinfo=UTC)
OFFER = SellerOffer(seller_id="news_sentiment", path="/sell/news-sentiment", description="news",
                    price_atomic=30_000, pay_to="0x" + "11" * 20)


def make_agent(balance: int) -> SectorAgent:
    client = SimpleNamespace(address="0x" + "22" * 20)
    return SectorAgent(SECTORS["NewsSentiment"], client=client, offers={"news_sentiment": OFFER},
                       seller_base_url="http://vendor.test", engine=init_db("sqlite://"),
                       settings=Settings(anthropic_api_key=""), balance_of=lambda address: balance)


async def test_paused_agent_does_nothing():
    agent = make_agent(balance=1_000_000)
    agent.paused = True
    report = await agent.tick(["BTC"], now=NOW)
    assert report.purchases == [] and report.findings == []


async def test_every_decision_is_logged_with_a_numeric_reason():
    agent = make_agent(balance=0)
    agent.set_belief(new_belief("NewsSentiment", "BTC", "direction", 1000.0, NOW, p=0.5))
    agent.set_belief(new_belief("NewsSentiment", "ETH", "direction", 1000.0, NOW, p=0.97))

    report = await agent.tick(["BTC", "ETH"], now=NOW)

    reasons = {p["asset"]: p["reason"] for p in report.purchases}
    assert reasons["BTC"].startswith("skipped: balance $0.00 < price $0.03")
    assert reasons["ETH"].startswith("skipped: EVOI $")
    with session_scope(agent.engine) as session:
        assert len(recent_purchases(session)) == 2
