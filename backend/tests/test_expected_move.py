"""
Tests for `app/sellers/expected_move.py` — the expected_move demo vendor (0.05 AUSD),
which is also the DEFAULT seller of the M2 gate (`scripts/gate_m2.py`).

Data: Deribit answers come from `tests/fixtures/deribit_*.json` and equity answers from
`tests/fixtures/yfin_nvda_option_chain.json`. Both are REALISTIC-SHAPE fixtures, hand-built
in the exact upstream formats; they are NOT live recordings (no network to Deribit or Yahoo
from this container). Where `compute()` runs end to end, only the upstream download
functions are replaced by fixture loaders and the clock is pinned to the fixtures' time;
everything else (parsing, option math, error handling) runs for real.

The last test pays for one expected_move answer over x402 on a real local anvil chain
(chain id 84532, MockAUSD): real ERC-3009 signature, pre-flight, settlement transaction
and receipt — the same path `make gate-m2` takes on Base Sepolia.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI

from app.config import get_settings
from app.data import deribit, yfin
from app.data.http import UpstreamError
from app.sellers import expected_move
from app.sellers.expected_move import (
    ERROR_BAD_REQUEST,
    ERROR_NO_DATA,
    ERROR_UPSTREAM,
    ExpectedMoveInputError,
    SellerConfigError,
)
from app.sellers.optionmath import expected_move as expected_move_formula
from app.sellers.optionmath import prob_above, year_fraction

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)          # Deribit fixtures' time
EQUITY_NOW = datetime(2026, 10, 12, 18, 30, tzinfo=timezone.utc)          # yfinance fixture's time
PAYOUT_ADDRESS = "0x" + "5e" * 20
REQUIRED_RESPONSE_FIELDS = ("inputs_used", "computed_at", "method", "confidence", "data_delay")

DERIBIT_FIXTURE_FILES = {
    "BTC": ("deribit_book_summary_btc.json", "deribit_index_btc_usd.json"),
    "ETH": ("deribit_book_summary_eth.json", "deribit_index_eth_usd.json"),
    "SOL": ("deribit_book_summary_usdc.json", "deribit_index_sol_usdc.json"),
}


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def fixture_chain(asset: str) -> deribit.OptionChain:
    book_file, index_file = DERIBIT_FIXTURE_FILES[asset]
    return deribit.build_option_chain(asset, load_fixture(book_file), load_fixture(index_file))


def request(**params: str) -> expected_move.ExpectedMoveRequest:
    return expected_move.parse_request(params)


@pytest.fixture
def deribit_from_fixtures(monkeypatch):
    """Serve the two Deribit downloads from the fixture files instead of the network."""

    async def book_summary(asset: str) -> dict:
        return load_fixture(DERIBIT_FIXTURE_FILES[asset.upper()][0])

    async def index(asset: str) -> dict:
        return load_fixture(DERIBIT_FIXTURE_FILES[asset.upper()][1])

    monkeypatch.setattr(deribit, "fetch_book_summary_payload", book_summary)
    monkeypatch.setattr(deribit, "fetch_index_payload", index)


@pytest.fixture
def payout_address(monkeypatch):
    """Point SELLER_PAYTO_EXPECTED_MOVE at a known address for make_offer()."""
    monkeypatch.setattr(get_settings(), "seller_payto_expected_move", PAYOUT_ADDRESS)


# ─── offer ────────────────────────────────────────────────────────────────────

def test_offer_costs_five_cents_and_pays_the_configured_address(payout_address):
    # Arrange / Act
    offer = expected_move.make_offer()

    # Assert
    assert offer.price_atomic == 50_000, "0.05 AUSD with 6 decimals"
    assert offer.path == "/sell/expected-move"
    assert offer.seller_id == "expected_move"
    assert offer.pay_to.lower() == PAYOUT_ADDRESS
    assert "demo vendor" in offer.description.lower()


def test_offer_with_empty_payout_setting_raises_a_clear_error(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_expected_move", "")
    with pytest.raises(SellerConfigError, match="SELLER_PAYTO_EXPECTED_MOVE is empty"):
        expected_move.make_offer()


def test_offer_with_malformed_payout_setting_raises(monkeypatch):
    monkeypatch.setattr(get_settings(), "seller_payto_expected_move", "0x1234")
    with pytest.raises(SellerConfigError, match="not a valid address"):
        expected_move.make_offer()


# ─── request parsing ─────────────────────────────────────────────────────────

def test_request_defaults_to_the_next_expiry_and_no_strikes():
    parsed = request(asset="btc")
    assert (parsed.asset, parsed.expiry, parsed.strikes) == ("BTC", "next", ())


def test_request_parses_a_date_and_a_strike_list():
    parsed = request(asset="ETH", expiry="2026-10-23", strikes="4000, 4500,")
    assert parsed.expiry == "2026-10-23"
    assert parsed.strikes == (4000.0, 4500.0)


@pytest.mark.parametrize(
    "params",
    [{}, {"asset": "1BTC"}, {"asset": "BTC", "expiry": "23/10/2026"}, {"asset": "BTC", "strikes": "abc"},
     {"asset": "BTC", "strikes": "-5"}, {"asset": "BTC", "strikes": ",".join(["1"] * 11)}],
)
def test_invalid_requests_raise_input_errors(params):
    with pytest.raises(ExpectedMoveInputError):
        expected_move.parse_request(params)


# ─── crypto (Deribit) ────────────────────────────────────────────────────────

def test_next_crypto_expiry_is_the_first_listed_one_at_least_an_hour_away():
    # Arrange: 07:30 UTC on 12 Oct, the 08:00 expiry is only 30 minutes away
    chain = fixture_chain("BTC")
    half_an_hour_before_expiry = datetime(2026, 10, 12, 7, 30, tzinfo=timezone.utc)

    # Act
    chosen = expected_move.choose_crypto_expiry(chain, "next", half_an_hour_before_expiry)

    # Assert
    assert chosen == datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)


def test_crypto_expiry_after_the_last_listed_one_is_rejected():
    with pytest.raises(ExpectedMoveInputError, match="after the last listed"):
        expected_move.choose_crypto_expiry(fixture_chain("BTC"), "2028-01-01", FIXTURE_NOW)


def test_crypto_expiry_in_the_past_is_rejected():
    with pytest.raises(ExpectedMoveInputError, match="in the past"):
        expected_move.choose_crypto_expiry(fixture_chain("BTC"), "2026-10-01", FIXTURE_NOW)


@pytest.mark.parametrize("asset", ["BTC", "ETH", "SOL"])
def test_crypto_expected_move_is_spot_times_sigma_times_root_t(asset):
    # Arrange
    chain = fixture_chain(asset)

    # Act
    response = expected_move.build_crypto_response(request(asset=asset), chain, FIXTURE_NOW)

    # Assert
    t_years = year_fraction(FIXTURE_NOW, datetime.fromisoformat(response["expiry"]))
    expected = expected_move_formula(chain.index_price, response["sigma_atm"], t_years)
    assert response["expected_move_usd"] == pytest.approx(expected, rel=1e-3)
    assert response["range_1sd"]["low"] < response["spot"] < response["range_1sd"]["high"]
    assert response["sigma_method"] == deribit.VOL_LISTED
    assert response["confidence"] == deribit.CONFIDENCE_BY_VOL_METHOD[deribit.VOL_LISTED]


def test_crypto_strike_probabilities_use_n_d2_with_the_smile_sigma():
    # Arrange
    chain = fixture_chain("BTC")

    # Act
    response = expected_move.build_crypto_response(
        request(asset="BTC", expiry="2026-10-16", strikes="110000,120000,130000"), chain, FIXTURE_NOW
    )

    # Assert
    rows = response["strikes"]
    t_years = year_fraction(FIXTURE_NOW, datetime(2026, 10, 16, 8, tzinfo=timezone.utc))
    for row in rows:
        assert row["prob_above"] == pytest.approx(
            prob_above(chain.index_price, row["strike"], row["sigma_used"], t_years), abs=2e-4
        )
        assert row["prob_above"] + row["prob_below"] == pytest.approx(1.0)
    assert rows[0]["prob_above"] > rows[1]["prob_above"] > rows[2]["prob_above"], "higher strike → less likely"


def test_unlisted_crypto_date_is_interpolated_and_reports_lower_confidence():
    # Arrange
    chain = fixture_chain("BTC")

    # Act
    response = expected_move.build_crypto_response(request(asset="BTC", expiry="2026-10-20"), chain, FIXTURE_NOW)

    # Assert
    assert response["sigma_method"] == deribit.VOL_INTERPOLATED
    assert len(response["atm_strikes"]) == 2
    assert response["confidence"] < deribit.CONFIDENCE_BY_VOL_METHOD[deribit.VOL_LISTED]


def test_crypto_response_carries_the_required_provenance_fields():
    # Arrange / Act
    response = expected_move.build_crypto_response(request(asset="BTC"), fixture_chain("BTC"), FIXTURE_NOW)

    # Assert
    for field in REQUIRED_RESPONSE_FIELDS:
        assert field in response
    assert response["inputs_used"]["index_as_of"] == "2026-10-11T14:00:00+00:00"
    assert response["inputs_used"]["atm_instruments"], "the instruments σ was read from are listed"
    assert response["data_delay"] == expected_move.DERIBIT_DATA_DELAY
    assert response["computed_at"] == "2026-10-11T14:00:00+00:00"


# ─── equities (yfinance) ─────────────────────────────────────────────────────

def test_equity_response_reports_straddle_move_and_sigma_move():
    # Arrange
    snapshot = load_fixture("yfin_nvda_option_chain.json")

    # Act
    response = expected_move.build_equity_response(request(asset="NVDA", strikes="180,195"), snapshot, EQUITY_NOW)

    # Assert
    assert response["atm_strike"] == 187.5
    straddle = (response["call_mid"] + response["put_mid"]) / snapshot["spot"]
    assert response["straddle_move_pct"] == pytest.approx(straddle, abs=1e-3)
    assert response["price_basis"] == yfin.PRICE_BASIS_MID
    assert response["sigma_atm"] == pytest.approx(0.4701, abs=1e-3)
    assert response["strikes"][0]["prob_above"] > response["strikes"][1]["prob_above"]
    assert response["data_delay"] == "15m+ delayed (yfinance)"
    assert response["confidence"] == expected_move.EQUITY_BASE_CONFIDENCE


def test_equity_response_after_hours_uses_last_trades_and_lowers_confidence():
    # Arrange: outside market hours Yahoo shows bid = ask = 0
    snapshot = load_fixture("yfin_nvda_option_chain.json")
    for row in snapshot["calls"] + snapshot["puts"]:
        row["bid"], row["ask"] = 0.0, 0.0

    # Act
    response = expected_move.build_equity_response(request(asset="NVDA"), snapshot, EQUITY_NOW)

    # Assert
    assert response["price_basis"] == yfin.PRICE_BASIS_LAST
    assert response["confidence"] == pytest.approx(
        expected_move.EQUITY_BASE_CONFIDENCE - expected_move.EQUITY_LAST_TRADE_PENALTY
    )


def test_equity_strike_with_junk_iv_falls_back_to_the_atm_sigma():
    # Arrange: the fixture's 205 call has yfinance's junk IV 1e-05 (the 205 put is fine)
    snapshot = load_fixture("yfin_nvda_option_chain.json")
    for row in snapshot["puts"]:
        if row["strike"] == 205.0:
            row["impliedVolatility"] = None

    # Act
    response = expected_move.build_equity_response(request(asset="NVDA", strikes="205"), snapshot, EQUITY_NOW)

    # Assert
    assert response["strikes"][0]["sigma_used"] == response["sigma_atm"]


def test_equity_with_implausible_atm_iv_keeps_the_straddle_and_omits_sigma_numbers():
    # Arrange
    snapshot = load_fixture("yfin_nvda_option_chain.json")
    for row in snapshot["calls"] + snapshot["puts"]:
        row["impliedVolatility"] = 1e-05

    # Act
    response = expected_move.build_equity_response(request(asset="NVDA", strikes="190"), snapshot, EQUITY_NOW)

    # Assert
    assert response["sigma_atm"] is None and response["expected_move_usd"] is None
    assert response["straddle_move_usd"] > 0
    assert response["strikes"][0]["prob_above"] is None
    assert response["notes"]


# ─── compute(): never raises, failures come back as error dicts ──────────────

async def test_compute_answers_btc_from_deribit(deribit_from_fixtures):
    # Act
    response = await expected_move.compute_at({"asset": "BTC", "expiry": "next"}, FIXTURE_NOW)

    # Assert
    assert "error" not in response
    assert response["asset"] == "BTC" and response["expected_move_usd"] > 0


async def test_compute_turns_bad_input_into_a_bad_request_error():
    response = await expected_move.compute_at({"asset": ""}, FIXTURE_NOW)

    assert response["error_kind"] == ERROR_BAD_REQUEST
    assert response["confidence"] == 0.0
    for field in ("inputs_used", "computed_at", "method"):
        assert field in response


async def test_compute_turns_a_deribit_outage_into_an_upstream_error(monkeypatch):
    # Arrange
    async def outage(asset: str) -> dict:
        raise UpstreamError("deribit", "failed after 3 attempts (HTTP 503)")

    monkeypatch.setattr(deribit, "fetch_book_summary_payload", outage)

    # Act
    response = await expected_move.compute_at({"asset": "ETH"}, FIXTURE_NOW)

    # Assert
    assert response["error_kind"] == ERROR_UPSTREAM
    assert "HTTP 503" in response["error"]


async def test_compute_turns_a_non_json_deribit_reply_into_an_upstream_error(monkeypatch):
    # Arrange: e.g. an HTML error page from a proxy
    async def html_page(asset: str) -> dict:
        return json.loads("<html>blocked</html>")

    monkeypatch.setattr(deribit, "fetch_book_summary_payload", html_page)

    # Act
    response = await expected_move.compute_at({"asset": "BTC"}, FIXTURE_NOW)

    # Assert
    assert response["error_kind"] == ERROR_UPSTREAM


async def test_compute_turns_an_empty_deribit_chain_into_a_no_data_error(monkeypatch, deribit_from_fixtures):
    async def empty_book(asset: str) -> dict:
        return {"jsonrpc": "2.0", "result": [], "usOut": 1}

    monkeypatch.setattr(deribit, "fetch_book_summary_payload", empty_book)

    response = await expected_move.compute_at({"asset": "BTC"}, FIXTURE_NOW)

    assert response["error_kind"] == ERROR_NO_DATA


async def test_compute_turns_a_yfinance_failure_into_an_error_dict(monkeypatch):
    # Arrange
    async def yahoo_down(ticker, requested_expiry, now, min_hours):
        raise yfin.YFinanceError("yfinance failed: ConnectionError: Yahoo unreachable")

    monkeypatch.setattr(yfin, "fetch_option_snapshot", yahoo_down)

    # Act
    response = await expected_move.compute_at({"asset": "NVDA"}, EQUITY_NOW)

    # Assert
    assert response["error_kind"] == ERROR_UPSTREAM
    assert response["data_delay"] == "15m+ delayed (yfinance)"


async def test_compute_reports_an_unlisted_equity_expiry_as_a_bad_request(monkeypatch):
    async def unlisted(ticker, requested_expiry, now, min_hours):
        raise yfin.ExpiryNotListedError("expiry 2026-10-20 is not a usable listed expiry")

    monkeypatch.setattr(yfin, "fetch_option_snapshot", unlisted)

    response = await expected_move.compute_at({"asset": "NVDA", "expiry": "2026-10-20"}, EQUITY_NOW)

    assert response["error_kind"] == ERROR_BAD_REQUEST


async def test_compute_answers_an_equity_from_the_yfinance_snapshot(monkeypatch):
    async def snapshot(ticker, requested_expiry, now, min_hours):
        return load_fixture("yfin_nvda_option_chain.json")

    monkeypatch.setattr(yfin, "fetch_option_snapshot", snapshot)

    response = await expected_move.compute_at({"asset": "NVDA"}, EQUITY_NOW)

    assert response["asset_class"] == "equity" and "error" not in response


# ─── paid over x402 on a local chain (the M2 gate's default seller) ──────────

@pytest.mark.chain
async def test_agent_pays_expected_move_demo_vendor_over_x402_on_local_chain(
    mock_ausd, settler, fund_ausd, wallets, w3, chain_snapshot, deribit_from_fixtures, payout_address, monkeypatch
):
    # Arrange: the real offer and compute, served by the real x402 router on anvil
    from app.agents.breaker import CircuitBreaker
    from app.chain.tokens import TokenInfo, TokenRegistry
    from app.constants import BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_CHAIN_ID, ONE_AUSD
    from app.x402.client import X402Client
    from app.x402.server import make_paid_router
    from app.x402.settlement import SelfSettler
    from app.x402.verify import PaymentVerifier

    monkeypatch.setattr(expected_move, "utc_now", lambda: FIXTURE_NOW)
    seller = wallets.address("seller")
    offer = dataclasses.replace(expected_move.make_offer(), pay_to=seller)
    vendor = FastAPI()
    vendor.include_router(make_paid_router(
        offer, expected_move.compute,
        verifier=PaymentVerifier(mock_ausd, BASE_SEPOLIA_CHAIN_ID, BASE_SEPOLIA_CAIP2),
        settler=SelfSettler(mock_ausd, settler, BASE_SEPOLIA_CAIP2),
        asset=mock_ausd.address,
    ))
    agent = Account.from_key(wallets.agent).address
    fund_ausd(agent, ONE_AUSD)
    registry = TokenRegistry(tokens=[TokenInfo(
        symbol="AUSD", chain_id=BASE_SEPOLIA_CHAIN_ID, caip2=BASE_SEPOLIA_CAIP2, address=mock_ausd.address,
        eip712_name="Agora Dollar", eip712_version="1", decimals=6, is_budget_token=True,
    )])
    client = X402Client(
        private_key=wallets.agent, registry=registry, token_clients={mock_ausd.address: mock_ausd},
        max_amount_atomic=ONE_AUSD // 2, agent_name="OptionsEvent", breaker=CircuitBreaker(),
        http=httpx.AsyncClient(transport=httpx.ASGITransport(app=vendor), base_url="http://demo-vendor.test"),
    )

    # Act: exactly what scripts/gate_m2.py asks for
    result = await client.get(f"http://demo-vendor.test{offer.path}", params={"asset": "BTC", "expiry": "next"})

    # Assert
    assert result.data["asset"] == "BTC" and result.data["expected_move_usd"] > 0
    assert w3.eth.get_transaction_receipt(result.tx_hash)["status"] == 1
    assert mock_ausd.balance_of(seller) == 50_000
    assert mock_ausd.balance_of(agent) == ONE_AUSD - 50_000
    assert mock_ausd.eth_balance(agent) == 0, "the agent never holds ETH"


@pytest.mark.chain
async def test_failed_expected_move_computation_is_not_charged(
    mock_ausd, settler, fund_ausd, wallets, chain_snapshot, payout_address, monkeypatch
):
    # Arrange: Deribit is down, so compute() returns an error dict
    from app.chain.tokens import TokenInfo, TokenRegistry
    from app.constants import BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_CHAIN_ID, ONE_AUSD
    from app.x402.client import PaymentError, X402Client
    from app.x402.server import make_paid_router
    from app.x402.settlement import SelfSettler
    from app.x402.verify import PaymentVerifier

    async def outage(asset: str) -> dict:
        raise UpstreamError("deribit", "failed after 3 attempts (HTTP 503)")

    monkeypatch.setattr(deribit, "fetch_book_summary_payload", outage)
    seller = wallets.address("seller")
    offer = dataclasses.replace(expected_move.make_offer(), pay_to=seller)
    vendor = FastAPI()
    vendor.include_router(make_paid_router(
        offer, expected_move.compute,
        verifier=PaymentVerifier(mock_ausd, BASE_SEPOLIA_CHAIN_ID, BASE_SEPOLIA_CAIP2),
        settler=SelfSettler(mock_ausd, settler, BASE_SEPOLIA_CAIP2),
        asset=mock_ausd.address,
    ))
    agent = Account.from_key(wallets.agent).address
    fund_ausd(agent, ONE_AUSD)
    registry = TokenRegistry(tokens=[TokenInfo(
        symbol="AUSD", chain_id=BASE_SEPOLIA_CHAIN_ID, caip2=BASE_SEPOLIA_CAIP2, address=mock_ausd.address,
        eip712_name="Agora Dollar", eip712_version="1", decimals=6, is_budget_token=True,
    )])
    client = X402Client(
        private_key=wallets.agent, registry=registry, token_clients={mock_ausd.address: mock_ausd},
        max_amount_atomic=ONE_AUSD // 2, agent_name="OptionsEvent",
        http=httpx.AsyncClient(transport=httpx.ASGITransport(app=vendor), base_url="http://demo-vendor.test"),
    )

    # Act
    with pytest.raises(PaymentError):
        await client.get(f"http://demo-vendor.test{offer.path}", params={"asset": "BTC"})

    # Assert
    assert mock_ausd.balance_of(seller) == 0, "no data, no charge"
    assert mock_ausd.balance_of(agent) == ONE_AUSD
