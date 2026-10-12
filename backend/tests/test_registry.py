"""
Tests for `app/sellers/registry.py` — the full data catalog behind `GET /api/catalog`.
Pure: no network. Demo-vendor prices are read from each seller's `make_offer()`.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.sellers import registry

PAYOUT_SETTINGS = ("seller_payto_expected_move", "seller_payto_pm_edge", "seller_payto_perps",
                   "seller_payto_news", "seller_payto_insider", "seller_payto_macro")


@pytest.fixture
def payouts_configured(monkeypatch):
    for index, name in enumerate(PAYOUT_SETTINGS):
        monkeypatch.setattr(get_settings(), name, "0x" + f"{index + 1:02x}" * 20)


def by_id() -> dict[str, registry.CatalogEntry]:
    return {entry.id: entry for entry in registry.list_catalog()}


def test_our_six_sellers_are_payable_demo_vendors_priced_from_their_offers(payouts_configured):
    rows = by_id()
    prices = {seller: rows[seller].price_atomic for seller in registry.DEMO_VENDORS}
    assert prices == {"expected_move": 50_000, "news_sentiment": 30_000, "insider_cluster": 30_000,
                      "perps_crowding": 20_000, "macro_pulse": 20_000, "pm_edge": 100_000}
    for seller in registry.DEMO_VENDORS:
        assert rows[seller].status == "live_ausd_seller"
        assert rows[seller].payable_mode == "x402_ausd_testnet"
        assert rows[seller].label == "demo vendor"
        assert rows[seller].pay_to is not None


def test_missing_payout_address_still_lists_the_seller_without_pay_to(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_pm_edge", "")
    row = by_id()["pm_edge"]
    assert row.pay_to is None and row.price_atomic == 100_000
    assert "SELLER_PAYTO_PM_EDGE" in row.notes


@pytest.mark.parametrize(
    ("row_id", "status", "payable_mode"),
    [("nansen_smart_money", "external_x402_flagged", "x402_usdc_mainnet"),
     ("coingecko_x402", "external_x402_flagged", "x402_usdc_mainnet"),
     ("coinmarketcap_x402", "external_x402_flagged", "x402_usdc_mainnet"),
     ("x402stock_earnings", "external_x402_flagged", "x402_usdc_mainnet"),
     ("x_api_posts", "metered_adapter_roadmap", "metered_api_key"),
     ("flightaware_aeroapi", "metered_adapter_roadmap", "metered_api_key"),
     ("databento_opra", "metered_adapter_roadmap", "metered_api_key"),
     ("contextdev_web", "metered_adapter_roadmap", "metered_api_key"),
     ("edgar_filings", "free_input", "free"),
     ("open_meteo_weather", "free_input", "free"),
     ("coingecko_public", "free_input", "free"),
     ("not_per_call", "not_per_call", "subscription_contract")],
)
def test_catalog_rows_have_honest_status_and_payable_mode(payouts_configured, row_id, status, payable_mode):
    row = by_id()[row_id]
    assert (row.status, row.payable_mode) == (status, payable_mode)


def test_not_per_call_row_names_what_cannot_be_bought_and_comes_last(payouts_configured):
    catalog = registry.list_catalog()
    assert catalog[-1].id == "not_per_call"
    for words in ("Terminal data", "consensus estimates", "redistribution", "card", "satellite", "expert"):
        assert words in catalog[-1].description


def test_catalog_json_rows_carry_the_spec_fields(payouts_configured):
    rows = registry.catalog_json()
    assert len(rows) == len(registry.CATALOG_ORDER)
    for field in ("id", "sector", "description", "trader_value", "source", "pricing_model", "payable_mode",
                  "price", "status"):
        assert all(field in row for row in rows)
