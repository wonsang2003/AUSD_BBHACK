"""Ask-box planning rules (app/agents/router.py)."""

from app.agents.router import detect_topics, plan_question


def test_cpi_question_goes_to_macro():
    plan = plan_question("What does CPI mean for NVDA?", ["BTC", "NVDA"])
    assert plan.agents == ("MacroFilings",)
    assert plan.assets == ("NVDA",)


def test_funding_question_for_equity_falls_back_to_covering_agents():
    plan = plan_question("funding on NVDA?", ["BTC", "NVDA"])
    assert "OnchainPerps" not in plan.agents and plan.agents


def test_topics_detected():
    assert detect_topics("insider buying and earnings") == ("earnings", "insider")
