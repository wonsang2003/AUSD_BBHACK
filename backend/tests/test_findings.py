"""Findings: emit rule, direction and template phrasing (app/agents/findings.py)."""

from app.agents.findings import FindingDraft, build_finding, describe_decision, direction_for, should_emit
from app.config import Settings


def test_should_emit_on_cross_or_large_move():
    assert should_emit(0.5, 0.52, 0.5)          # crossed τ
    assert should_emit(0.6, 0.7, 0.5)           # moved > 0.05
    assert not should_emit(0.6, 0.63, 0.5)


def test_direction_rules():
    assert direction_for(0.6, 0.5) == "bullish"
    assert direction_for(0.4, 0.5) == "bearish"
    assert direction_for(0.6, 0.5, risk=True) == "risk"


def test_describe_above_decision():
    assert describe_decision("BTC", "above:98000") == "BTC above $98,000 at the next expiry"


async def test_build_finding_uses_template_without_api_key():
    draft = FindingDraft(agent="NewsSentiment", asset="BTC", decision="direction", old_p=0.5, new_p=0.59, tau=0.5,
                         evidence={"news tone change": "+3.00"},
                         receipts=[{"seller_id": "news_sentiment", "tx_hash": "0xabc", "price_atomic": 30_000}])
    finding = await build_finding(draft, settings=Settings(anthropic_api_key=""))
    assert finding.direction == "bullish"
    assert round(finding.delta_p, 2) == 0.09
    assert "59%" in finding.headline
    assert finding.receipts[0]["tx_hash"] == "0xabc"
