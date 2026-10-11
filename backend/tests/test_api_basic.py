"""
API tests that need no chain: the app must start and serve health, config, catalog, the x402
probe, demo-vendor 402s and SSE even when the keeper runtime is unavailable or faked.
"""

from __future__ import annotations

import sys
import types

import httpx
import pytest
from eth_account import Account

from app.api.catalog import probe_payload
from app.api.stream import event_chunks
from app.config import Settings, get_settings
from app.events import EventBus
from app.main import create_app

PAYOUT_ENV = ("SELLER_PAYTO_EXPECTED_MOVE", "SELLER_PAYTO_PM_EDGE", "SELLER_PAYTO_PERPS",
              "SELLER_PAYTO_NEWS", "SELLER_PAYTO_INSIDER", "SELLER_PAYTO_MACRO")


@pytest.fixture
def payouts(monkeypatch):
    """Every demo vendor gets a payout address (env overrides the repo .env)."""
    for name in PAYOUT_ENV:
        monkeypatch.setenv(name, Account.create().address)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(**overrides) -> Settings:
    base = {"skip_chain_checks": True, "database_url": "sqlite://", "agent_optionsevent_key": "0x" + "11" * 32}
    return Settings(**{**base, **overrides})


def _failing_factory(settings):
    raise RuntimeError("relayer key missing")


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_health_reports_unavailable_runtime_without_failing(payouts):
    app = create_app(settings=_settings(), runtime_factory=_failing_factory)
    async with _client(app) as client:
        response = await client.get("/api/health")
    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    assert "relayer key missing" in body["startup_error"]
    assert body["chain_id"] == 84532


async def test_config_falls_back_to_settings_when_runtime_is_unavailable(payouts):
    app = create_app(settings=_settings(), runtime_factory=_failing_factory)
    async with _client(app) as client:
        body = (await client.get("/api/config")).json()
    assert body["explorer_tx_url"].startswith("https://sepolia.basescan.org")
    assert [agent["name"] for agent in body["agents"]] == ["OptionsEvent"]
    assert {seller["seller_id"] for seller in body["sellers"]} >= {"expected_move", "pm_edge"}


async def test_runtime_endpoints_answer_503_with_detail_when_runtime_is_unavailable(payouts):
    app = create_app(settings=_settings(), runtime_factory=_failing_factory)
    async with _client(app) as client:
        response = await client.get("/api/state")
    assert response.status_code == 503
    assert "backend not ready" in response.json()["detail"]


async def test_catalog_lists_demo_vendors_labeled(payouts):
    app = create_app(settings=_settings(), runtime_factory=_failing_factory)
    async with _client(app) as client:
        rows = (await client.get("/api/catalog")).json()
    demo = [row for row in rows if row["label"] == "demo vendor"]
    assert len(demo) == 6
    assert all(row["price_atomic"] > 0 for row in demo)


async def test_x402_probe_wraps_vendor_rows(payouts, monkeypatch):
    calls = []

    async def fake_probe_all(refresh: bool = False):
        calls.append(refresh)
        return [{"id": "coingecko", "ok": True, "cached": False, "probed_at": "2026-10-11T00:00:00+00:00"}]

    monkeypatch.setitem(sys.modules, "app.x402.probe", types.SimpleNamespace(probe_all=fake_probe_all))
    app = create_app(settings=_settings(), runtime_factory=_failing_factory)
    async with _client(app) as client:
        body = (await client.get("/api/x402-probe?refresh=1")).json()
    assert calls == [True]
    assert body["cached"] is False
    assert body["vendors"][0]["id"] == "coingecko"


def test_probe_payload_defaults_when_rows_carry_no_times():
    payload = probe_payload([{"id": "x"}], refreshed=False)
    assert payload["cached"] is True
    assert payload["vendors"] == [{"id": "x"}]


async def test_demo_vendor_answers_402_without_payment(payouts):
    app = create_app(settings=_settings(), runtime_factory=_failing_factory)
    async with _client(app) as client:
        response = await client.get("/sell/expected-move", params={"asset": "BTC"})
    assert response.status_code == 402
    assert "PAYMENT-REQUIRED" in response.headers


async def test_stream_yields_the_initial_ping(payouts):
    fake_runtime = types.SimpleNamespace(bus=EventBus(), settings=_settings(), w3=None, registry=None,
                                         token=types.SimpleNamespace(address=Account.create().address))
    app = create_app(runtime=fake_runtime, settings=_settings())
    async with _client(app) as client:
        response = await client.get("/api/stream?max_events=1")
    assert response.status_code == 200
    assert response.text.startswith("event: ping\n")


async def test_event_chunks_forward_published_events():
    bus = EventBus()
    subscription = bus.subscribe()
    bus.publish("purchase", {"id": 1})
    chunks = [chunk async for chunk in event_chunks(subscription, max_events=2)]
    assert chunks[0].startswith("event: ping")
    assert chunks[1] == 'event: purchase\ndata: {"id":1}\n\n'
