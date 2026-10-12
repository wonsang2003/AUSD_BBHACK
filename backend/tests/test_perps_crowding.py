"""
Tests for `app/sellers/perps_crowding.py` — the perps_crowding demo vendor (0.02 AUSD):
Hyperliquid funding / open-interest z-scores → crowded long / crowded short / neutral.

Fixture: `tests/fixtures/hyperliquid_meta_and_asset_ctxs.json`, a REALISTIC-SHAPE payload
hand-built OFFLINE in Hyperliquid's `metaAndAssetCtxs` format (not a live recording; no network
here). In it BTC funding is stretched (+0.0042 %/h), SOL funding is negative and most other
liquid perps sit at the 0.00125 %/h baseline.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.data import hyperliquid
from app.data.http import UpstreamError
from app.data.hyperliquid import PerpContext
from app.sellers import perps_crowding
from app.sellers.expected_move import ERROR_BAD_REQUEST, ERROR_NO_DATA, ERROR_UPSTREAM, SellerConfigError
from app.sellers.perps_crowding import PerpsHistory

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)
PAYOUT_ADDRESS = "0x" + "3c" * 20


def fixture_contexts() -> list[PerpContext]:
    payload = json.loads((FIXTURES / "hyperliquid_meta_and_asset_ctxs.json").read_text())
    return hyperliquid.parse_meta_and_asset_ctxs(payload)


def context(name: str, funding: float, oi: float) -> PerpContext:
    return PerpContext(name, funding, oi, 100.0, 100.0, 100.0, 5e6, 99.0)


@pytest.fixture(autouse=True)
def empty_history():
    perps_crowding.reset_history()
    yield
    perps_crowding.reset_history()


@pytest.fixture
def hyperliquid_from_fixture(monkeypatch):
    async def contexts():
        return fixture_contexts()

    monkeypatch.setattr(hyperliquid, "fetch_perp_contexts", contexts)


# ─── offer ────────────────────────────────────────────────────────────────────

def test_offer_costs_two_cents_and_is_labelled_demo_vendor(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_perps", PAYOUT_ADDRESS)
    offer = perps_crowding.make_offer()
    assert (offer.price_atomic, offer.path) == (20_000, "/sell/perps-crowding")
    assert "demo vendor" in offer.description.lower()


def test_offer_without_payout_address_raises(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_perps", "")
    with pytest.raises(SellerConfigError, match="SELLER_PAYTO_PERPS"):
        perps_crowding.make_offer()


# ─── statistics ──────────────────────────────────────────────────────────────

def test_zscore_matches_mean_and_sample_stdev():
    samples = [1.0, 2.0, 3.0, 4.0]
    expected = (6.0 - statistics.fmean(samples)) / statistics.stdev(samples)
    assert perps_crowding.zscore(6.0, samples) == pytest.approx(expected)
    assert perps_crowding.zscore(1.0, [1.0]) == 0.0
    assert perps_crowding.zscore(1.0, [2.0, 2.0]) == 0.0


def test_robust_spread_uses_mad_and_falls_back_to_mean_absolute_deviation():
    assert perps_crowding.robust_spread([1.0, 2.0, 3.0, 4.0, 100.0]) == pytest.approx(1.4826 * 1.0)
    # more than half identical → MAD = 0 → 1.2533 · mean |x − median|
    assert perps_crowding.robust_spread([1.0, 1.0, 1.0, 5.0]) == pytest.approx(1.2533 * 1.0)


def test_crowding_combines_funding_and_oi_buildup_in_funding_direction():
    assert perps_crowding.combine_crowding_z(1.2, 0.8) == pytest.approx(1.6)
    assert perps_crowding.combine_crowding_z(-1.2, 0.8) == pytest.approx(-1.6)
    assert perps_crowding.combine_crowding_z(1.2, -0.8) == pytest.approx(0.8), "OI unwinding weakens it"
    assert perps_crowding.combine_crowding_z(1.2, None) == 1.2


@pytest.mark.parametrize(("z", "label"), [(1.0, "crowded long"), (-1.0, "crowded short"), (0.99, "neutral")])
def test_labels_at_plus_minus_one(z, label):
    assert perps_crowding.crowding_label(z) == label


# ─── scoring ─────────────────────────────────────────────────────────────────

def test_cold_start_scores_cross_sectionally_against_liquid_perps():
    # Arrange
    contexts = fixture_contexts()

    # Act
    rows, not_found = perps_crowding.score_assets(["BTC", "ETH", "SOL", "NOPE"], contexts, PerpsHistory())

    # Assert
    by_asset = {row.asset: row for row in rows}
    assert not_found == ["NOPE"]
    assert by_asset["BTC"].label == "crowded long"
    assert by_asset["SOL"].label == "crowded short"
    assert by_asset["ETH"].label == "neutral"
    assert all(row.method == "cross_sectional" and row.confidence == 0.5 for row in rows)
    universe = perps_crowding.cross_section(contexts)
    assert "TINY" not in {peer.name for peer in universe}, "illiquid perps are left out of the comparison"


def test_time_series_method_is_used_once_the_window_has_twelve_snapshots():
    # Arrange: 12 snapshots, funding hovering around 1e-5, OI growing 1 % per snapshot
    history = PerpsHistory(min_seconds_between=0.0)
    for step in range(12):
        funding = 1e-5 + (1e-6 if step % 2 else -1e-6)
        history.record_all([context("BTC", funding, 1000.0 * 1.01**step)], taken_at=float(step))

    # Act: funding jumps and OI jumps 5 %
    last_oi = 1000.0 * 1.01**11
    [row], _ = perps_crowding.score_assets(["BTC"], [context("BTC", 4e-5, last_oi * 1.05)], history)

    # Assert
    assert row.method == "time_series" and row.snapshots_used == 12
    assert row.funding_z > 1.0
    assert row.oi_change_pct == pytest.approx(5.0)
    assert row.label == "crowded long"
    assert row.confidence == pytest.approx(0.6)


def test_history_respects_minimum_spacing_and_window_length():
    history = PerpsHistory(window=3, min_seconds_between=60.0)
    for taken_at in (0.0, 30.0, 60.0, 120.0, 180.0):
        history.record_all([context("ETH", 1e-5, 10.0)], taken_at)
    assert [snap.taken_at for snap in history.snapshots("eth")] == [60.0, 120.0, 180.0]


# ─── compute ─────────────────────────────────────────────────────────────────

async def test_compute_returns_assets_with_required_fields(hyperliquid_from_fixture):
    response = await perps_crowding.compute_at({"assets": "btc,sol"}, NOW)
    assert [row["asset"] for row in response["assets"]] == ["BTC", "SOL"]
    assert response["computed_at"] == "2026-10-11T14:00:00+00:00"
    for field in ("inputs_used", "computed_at", "method", "confidence", "data_delay"):
        assert field in response


async def test_compute_records_a_snapshot_for_later_time_series_scores(hyperliquid_from_fixture):
    await perps_crowding.compute_at({}, NOW)
    assert len(perps_crowding._HISTORY.snapshots("BTC")) == 1


async def test_upstream_failure_returns_an_error_dict(monkeypatch):
    async def down():
        raise UpstreamError("hyperliquid", "failed after 3 attempts")

    monkeypatch.setattr(hyperliquid, "fetch_perp_contexts", down)
    response = await perps_crowding.compute_at({}, NOW)
    assert response["error_kind"] == ERROR_UPSTREAM and response["confidence"] == 0.0


async def test_bad_or_unlisted_assets_return_error_dicts(hyperliquid_from_fixture):
    assert (await perps_crowding.compute_at({"assets": "BTC;DROP"}, NOW))["error_kind"] == ERROR_BAD_REQUEST
    assert (await perps_crowding.compute_at({"assets": "NOPE"}, NOW))["error_kind"] == ERROR_NO_DATA
