"""
Tests for `app/data/hyperliquid.py` — the Hyperliquid `metaAndAssetCtxs` adapter used by the
`perps_crowding` demo vendor and the dashboard market strip.

Fixture: `tests/fixtures/hyperliquid_meta_and_asset_ctxs.json` is a REALISTIC-SHAPE payload,
hand-built OFFLINE in Hyperliquid's exact response format (`[meta, assetCtxs]`, decimal
strings, a delisted market, a market with a one-sided book). It is NOT a live recording: this
container has no network access to Hyperliquid.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.data import hyperliquid
from app.data.hyperliquid import FUNDING_PERIODS_PER_YEAR, HyperliquidParseError

FIXTURES = Path(__file__).parent / "fixtures"


def load_payload() -> list:
    return json.loads((FIXTURES / "hyperliquid_meta_and_asset_ctxs.json").read_text())


def test_parse_zips_universe_names_with_contexts_and_skips_delisted_markets():
    # Act
    contexts = hyperliquid.parse_meta_and_asset_ctxs(load_payload())

    # Assert
    names = [context.name for context in contexts]
    assert names[:3] == ["BTC", "ETH", "SOL"]
    assert "FTT" not in names, "FTT is flagged isDelisted"
    btc = hyperliquid.index_by_symbol(contexts)["BTC"]
    assert btc.funding_hourly == pytest.approx(0.000042)
    assert btc.mark_px == pytest.approx(118262.0)
    assert btc.open_interest_usd == pytest.approx(28512.4421 * 118262.0)


def test_funding_is_annualised_with_hourly_periods():
    btc = hyperliquid.index_by_symbol(hyperliquid.parse_meta_and_asset_ctxs(load_payload()))["BTC"]
    assert btc.funding_annualized == pytest.approx(0.000042 * 24 * 365)
    assert FUNDING_PERIODS_PER_YEAR == 8760


def test_display_price_falls_back_to_mark_when_the_book_is_one_sided():
    tiny = hyperliquid.index_by_symbol(hyperliquid.parse_meta_and_asset_ctxs(load_payload()))["TINY"]
    assert tiny.mid_px is None
    assert tiny.display_px == pytest.approx(0.0412)
    assert tiny.change_24h_pct is None, "no prevDayPx → no 24 h change"


def test_change_24h_is_mid_over_previous_day_price():
    sol = hyperliquid.index_by_symbol(hyperliquid.parse_meta_and_asset_ctxs(load_payload()))["SOL"]
    assert sol.change_24h_pct == pytest.approx((212.56 / 205.11 - 1) * 100)


@pytest.mark.parametrize(
    "payload",
    [{}, [], [{"universe": []}], [{"universe": [{"name": "BTC"}]}, []], [{"no_universe": []}, []]],
)
def test_malformed_payloads_raise_a_parse_error(payload):
    with pytest.raises(HyperliquidParseError):
        hyperliquid.parse_meta_and_asset_ctxs(payload)


async def test_fetch_posts_the_documented_request_body(monkeypatch):
    # Arrange
    sent: dict = {}

    async def fake_post_json(url, body, *, source, headers=None, ttl=30.0):
        sent.update(url=url, body=body, source=source)
        return load_payload()

    monkeypatch.setattr(hyperliquid, "post_json", fake_post_json)

    # Act
    contexts = await hyperliquid.fetch_perp_contexts()

    # Assert
    assert sent == {"url": "https://api.hyperliquid.xyz/info", "body": {"type": "metaAndAssetCtxs"},
                    "source": "hyperliquid"}
    assert len(contexts) == 9
