"""
pm_edge seller — "Which Gemini crypto prediction markets disagree with the options market?"
(SPEC §4 item 2; demo vendor, 0.10 AUSD per call)

    GET /sell/pm-edge?category=crypto[&asset=BTC|ETH|SOL][&limit=20]

The x402 server (`app/x402/server.py`, assembled in `app/sellers/routers.py`) gates the
endpoint with `make_offer()` and calls `compute(query_params)` after verifying payment.
This module never imports the server. The OptionsEvent agent reads the rows with
`app/agents/beliefs.update_from_pm_edge(p, model_p, market_p, fee_per_contract)`, and
`GET /api/pm-vs-options` shows the rows of the latest purchase.

For every open Gemini contract of the form "ASSET finishes at or above $K at time T":

    market_p         = YES mid (bestBid + bestAsk)/2; else last trade; else the one quoted side
    S                = Deribit index price of ASSET
    σ                = Deribit mark IV at the listed strike nearest K for expiry T; a T between two
                       listed Deribit expiries is interpolated in total variance σ²T, linear in time
    model_p          = N(d2),  d2 = (ln(S/K) − σ²T/2) / (σ√T),  r = 0,  T in years ACT/365
    edge             = model_p − market_p        (> 0: YES looks cheap; < 0: NO looks cheap)
    entry price      = YES best ask (buy YES) or 1 − YES best bid (buy NO)
    fee per contract = ceil_to_cent(0.07 · C · P · (1 − P)) / C at the entry price, C = 100
    expected profit  = P(win) − entry price − fee per contract        (per $1 contract)
    edge_after_fees  = sign(edge) · max(0, expected profit)     0 when spread + fee eat the edge

Rows are sorted by |edge|, largest first. Contracts that cannot be compared (Up/Down
markets whose strike is set later, touch markets, assets without Deribit options, no
prices, expiring within the hour) are listed under `skipped` with the reason.

All option math comes from `app/sellers/optionmath.py`; prediction-market parsing from
`app/data/gemini.py`; option chains from `app/data/deribit.py`. A failed request (or one
with no comparable contract) returns {"error": "...", "error_kind": "..."} so the x402
server does not settle a payment for it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.config import get_settings
from app.constants import ONE_AUSD
from app.data import deribit, gemini
from app.data.deribit import OptionChain
from app.data.gemini import PredictionContract, PriceThreshold, UnsupportedContract, YesQuote
from app.data.http import UpstreamError
from app.sellers.expected_move import (
    ERROR_BAD_REQUEST,
    ERROR_NO_DATA,
    ERROR_UPSTREAM,
    MIN_HOURS_TO_EXPIRY,
    PROBABILITY_DECIMALS,
    SIGMA_DECIMALS,
    USD_DECIMALS,
    YEARS_DECIMALS,
    error_response,
    iso_utc,
    require_payout_address,
    utc_now,
)
from app.sellers.optionmath import gemini_taker_fee, prob_above, year_fraction
from app.x402.offer import SellerOffer

# ─── offer ────────────────────────────────────────────────────────────────────

SELLER_ID = "pm_edge"
OFFER_PATH = "/sell/pm-edge"
PRICE_ATOMIC = 10 * ONE_AUSD // 100          # 0.10 AUSD = 100 000 atomic units
PAYOUT_SETTING_NAME = "SELLER_PAYTO_PM_EDGE"
OFFER_DESCRIPTION = (
    "Demo vendor: Gemini crypto prediction-market YES prices vs options-implied N(d2) from "
    "Deribit, with the edge after Gemini taker fees"
)

# ─── request rules ───────────────────────────────────────────────────────────

CATEGORY_CRYPTO = "crypto"
SUPPORTED_CATEGORIES = (CATEGORY_CRYPTO,)
DEFAULT_ROW_LIMIT = 20
MAX_ROW_LIMIT = 50
MAX_EVENT_DETAILS = 10        # Get Event calls per request (Gemini is rate limited to ~2 req/s here)
MAX_SKIPPED_REPORTED = 50     # keep the response small; the total count is always reported

# ─── trade and fee rules ─────────────────────────────────────────────────────

REFERENCE_CONTRACTS = 100     # fee per contract is quoted for a 100-contract order (cent rounding is small)
SIDE_YES = "yes"
SIDE_NO = "no"
SIDE_NONE = "none"            # model and market agree exactly
ENTRY_BASIS_ASK = "best_ask"
ENTRY_BASIS_MARKET = "market_p (no ask quoted)"

# ─── confidence ──────────────────────────────────────────────────────────────

NON_MID_MARKET_PENALTY = 0.2  # market_p from a last trade or one quoted side, not a two-sided mid
MIN_CONFIDENCE = 0.1
CONFIDENCE_DECIMALS = 2

# ─── response labels ─────────────────────────────────────────────────────────

DATA_DELAY = "real-time (Gemini Predictions + Deribit public APIs, cached ≤ 60 s)"
METHOD = (
    "market_p = Gemini YES mid; model_p = N(d2) from Deribit mark IV at the strike nearest K "
    "(total variance σ²T interpolated between bracketing expiries), r = 0, ACT/365; "
    "edge = model_p − market_p; edge_after_fees = sign(edge)·max(0, P(win) − ask − "
    "ceil_to_cent(0.07·C·P·(1−P))/C), C = 100"
)


class PmEdgeInputError(ValueError):
    """The query parameters are invalid; the message tells the buyer what to fix."""


# ─── offer ────────────────────────────────────────────────────────────────────

def make_offer() -> SellerOffer:
    """The x402 offer: 0.10 AUSD per call, paid to settings.seller_payto_pm_edge."""
    pay_to = require_payout_address(get_settings().seller_payto_pm_edge, PAYOUT_SETTING_NAME)
    return SellerOffer(
        seller_id=SELLER_ID,
        path=OFFER_PATH,
        description=OFFER_DESCRIPTION,
        price_atomic=PRICE_ATOMIC,
        pay_to=pay_to,
    )


# ─── request parsing ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PmEdgeRequest:
    """Validated query parameters."""

    category: str           # only "crypto" is supported
    asset: str | None       # optional filter: BTC, ETH or SOL
    limit: int              # maximum number of rows returned


def _parse_category(raw: str | None) -> str:
    """"crypto" (the default); any other category has no options market to compare with."""
    category = (raw or CATEGORY_CRYPTO).strip().lower()
    if category not in SUPPORTED_CATEGORIES:
        supported = ", ".join(SUPPORTED_CATEGORIES)
        raise PmEdgeInputError(f"category must be one of: {supported} (got {raw!r})")
    return category


def _parse_asset_filter(raw: str | None) -> str | None:
    """Optional asset filter; it must be an asset Deribit lists options for (BTC, ETH, SOL)."""
    if raw is None or not raw.strip():
        return None
    asset = raw.strip().upper()
    if not deribit.is_supported_asset(asset):
        supported = ", ".join(deribit.SUPPORTED_UNDERLYINGS)
        raise PmEdgeInputError(f"asset must be one of: {supported} (got {raw!r})")
    return asset


def _parse_limit(raw: str | None) -> int:
    """Row limit: an integer from 1 to MAX_ROW_LIMIT; DEFAULT_ROW_LIMIT when absent."""
    if raw is None or not str(raw).strip():
        return DEFAULT_ROW_LIMIT
    try:
        limit = int(str(raw).strip())
    except ValueError as exc:
        raise PmEdgeInputError(f"limit must be an integer, got {raw!r}") from exc
    if not 1 <= limit <= MAX_ROW_LIMIT:
        raise PmEdgeInputError(f"limit must be between 1 and {MAX_ROW_LIMIT}, got {limit}")
    return limit


def parse_request(params: dict) -> PmEdgeRequest:
    """Validate `category`, `asset` and `limit` from the query string."""
    return PmEdgeRequest(
        category=_parse_category(params.get("category")),
        asset=_parse_asset_filter(params.get("asset")),
        limit=_parse_limit(params.get("limit")),
    )


# ─── choosing events and contracts (pure) ────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    """A Gemini contract that reads as "ASSET finishes at or above K at T"."""

    contract: PredictionContract
    threshold: PriceThreshold


def _skip(event_ticker: str, contract_ticker: str | None, label: str | None, reason: str) -> dict:
    """One `skipped` entry: which event/contract was not compared, and why."""
    return {"event_ticker": event_ticker, "ticker": contract_ticker, "label": label, "reason": reason}


def select_events(events: list[dict], request: PmEdgeRequest) -> tuple[list[dict], list[dict]]:
    """
    The events worth a Get Event call, and the ones skipped (with the reason).

    Rule: keep events about an asset Deribit lists options for (and matching the asset
    filter), drop 5/15-minute Up/Down events (their strike is set at the window start),
    then keep the first MAX_EVENT_DETAILS in API order.
    """
    selected: list[dict] = []
    skipped: list[dict] = []
    for event in events:
        ticker = event.get("ticker") or ""
        asset = gemini.event_asset(event)
        if not ticker:
            skipped.append(_skip(ticker, None, None, "event has no ticker"))
        elif gemini.is_short_duration_event(ticker):
            skipped.append(_skip(ticker, None, None, "5/15-minute Up/Down event: strike is set at the window start"))
        elif asset is None or not deribit.is_supported_asset(asset):
            skipped.append(_skip(ticker, None, None, f"no Deribit options for the event's asset ({asset or 'unknown'})"))
        elif request.asset is not None and asset != request.asset:
            continue
        elif len(selected) >= MAX_EVENT_DETAILS:
            skipped.append(_skip(ticker, None, None, f"over the {MAX_EVENT_DETAILS}-event limit per request"))
        else:
            selected.append(event)
    return selected, skipped


def _candidate_or_reason(contract: PredictionContract, now: datetime) -> Candidate | str:
    """The contract as a Candidate, or the reason it cannot be compared with option prices."""
    if not gemini.is_tradable(contract):
        return "contract is not active and open for trading"
    try:
        threshold = gemini.parse_price_threshold(contract)
    except UnsupportedContract as exc:
        return str(exc)
    if not deribit.is_supported_asset(threshold.asset):
        return f"no Deribit options for {threshold.asset}"
    if threshold.expiry < now + timedelta(hours=MIN_HOURS_TO_EXPIRY):
        return f"expires in less than {MIN_HOURS_TO_EXPIRY:g}h"
    return Candidate(contract=contract, threshold=threshold)


def find_candidates(events: list[dict], now: datetime) -> tuple[list[Candidate], list[dict]]:
    """Every comparable contract of `events`, and a `skipped` entry for every other one."""
    candidates: list[Candidate] = []
    skipped: list[dict] = []
    for event in events:
        for contract in gemini.parse_contracts(event):
            outcome = _candidate_or_reason(contract, now)
            if isinstance(outcome, Candidate):
                candidates.append(outcome)
            else:
                skipped.append(_skip(contract.event_ticker, contract.ticker, contract.label, outcome))
    return candidates, skipped


# ─── edge and fees (pure) ────────────────────────────────────────────────────

def trade_side(edge: float) -> str:
    """"yes" if the model says YES is underpriced (edge > 0), "no" if overpriced, else "none"."""
    if edge > 0:
        return SIDE_YES
    if edge < 0:
        return SIDE_NO
    return SIDE_NONE


def entry_price(side: str, quote: YesQuote) -> tuple[Decimal, str]:
    """
    Price a taker pays per $1 contract on `side`, and its basis.

    YES → the YES best ask; NO → 1 − YES best bid (`quote.no_ask`). If that side has no
    ask quoted, fall back to the market probability (YES) or its complement (NO).
    """
    if side == SIDE_YES and quote.yes_ask is not None:
        return quote.yes_ask, ENTRY_BASIS_ASK
    if side == SIDE_NO and quote.no_ask is not None:
        return quote.no_ask, ENTRY_BASIS_ASK
    if side == SIDE_NO:
        return gemini.ONE - quote.probability, ENTRY_BASIS_MARKET
    return quote.probability, ENTRY_BASIS_MARKET


def fee_per_contract(price: Decimal) -> float:
    """Gemini taker fee per contract at `price`: ceil_to_cent(0.07 · C · P · (1 − P)) / C, C = 100."""
    return gemini_taker_fee(REFERENCE_CONTRACTS, price) / REFERENCE_CONTRACTS


def expected_profit_per_contract(side: str, model_p: float, price: Decimal, fee: float) -> float:
    """
    Model expected profit of buying one $1 contract on `side` at `price` and paying `fee`:

        YES: model_p − price − fee        NO: (1 − model_p) − price − fee
    """
    win_probability = model_p if side == SIDE_YES else 1.0 - model_p
    return win_probability - float(price) - fee


def edge_after_fees(edge: float, expected_profit: float) -> float:
    """sign(edge) · max(0, expected profit): the tradable edge, 0 when spread + fee eat it."""
    if edge == 0:
        return 0.0
    return math.copysign(max(0.0, expected_profit), edge)


def _row_confidence(vol_method: str, market_basis: str) -> float:
    """σ confidence (`deribit.CONFIDENCE_BY_VOL_METHOD`) − 0.2 if market_p is not a two-sided mid."""
    confidence = deribit.CONFIDENCE_BY_VOL_METHOD[vol_method]
    if market_basis != gemini.BASIS_MID:
        confidence -= NON_MID_MARKET_PENALTY
    return round(max(MIN_CONFIDENCE, confidence), CONFIDENCE_DECIMALS)


def _title(contract: PredictionContract) -> str:
    """Event title and contract label, e.g. "BTC price on Oct 16 — BTC > $110,000"."""
    if contract.label and contract.label != contract.event_title:
        return f"{contract.event_title} — {contract.label}"
    return contract.event_title or contract.label


def _optional_float(value: Decimal | None) -> float | None:
    """Decimal → float for JSON; None stays None."""
    return None if value is None else float(value)


def compare_contract(candidate: Candidate, chain: OptionChain, now: datetime) -> dict:
    """
    One `rows` entry: Gemini market_p vs options-implied model_p for one contract (pure).

    Raises UnsupportedContract (no price at all) or DeribitDataError (no usable option).
    """
    contract, threshold = candidate.contract, candidate.threshold
    quote = gemini.yes_quote(contract)
    vol = deribit.implied_vol_at(chain, threshold.expiry, threshold.strike, now)
    t_years = year_fraction(now, threshold.expiry)
    model_p = prob_above(chain.index_price, threshold.strike, vol.sigma, t_years)
    market_p = float(quote.probability)
    edge = model_p - market_p
    side = trade_side(edge)
    price, price_basis = entry_price(side, quote)
    fee = fee_per_contract(price)
    profit = expected_profit_per_contract(side, model_p, price, fee)
    return {
        "ticker": contract.ticker or contract.instrument_symbol,
        "instrument_symbol": contract.instrument_symbol,
        "event_ticker": contract.event_ticker,
        "title": _title(contract),
        "asset": threshold.asset,
        "strike": threshold.strike,
        "expiry": iso_utc(threshold.expiry),
        "market_p": round(market_p, PROBABILITY_DECIMALS),
        "market_p_basis": quote.basis,
        "model_p": round(model_p, PROBABILITY_DECIMALS),
        "edge": round(edge, PROBABILITY_DECIMALS),
        "side": side,
        "entry_price": float(price),
        "entry_price_basis": price_basis,
        "yes_bid": _optional_float(contract.best_bid),
        "yes_ask": _optional_float(quote.yes_ask),
        "spread": _optional_float(quote.spread),
        "fee_per_contract": round(fee, PROBABILITY_DECIMALS),
        "expected_profit_per_contract": round(profit, PROBABILITY_DECIMALS),
        "edge_after_fees": round(edge_after_fees(edge, profit), PROBABILITY_DECIMALS),
        "spot": round(chain.index_price, USD_DECIMALS),
        "sigma": round(vol.sigma, SIGMA_DECIMALS),
        "sigma_method": vol.method,
        "sigma_instruments": [name for point in vol.points for name in point.instruments],
        "t_years": round(t_years, YEARS_DECIMALS),
        "confidence": _row_confidence(vol.method, quote.basis),
    }


def compare_all(
    candidates: list[Candidate],
    chains: dict[str, OptionChain],
    chain_errors: dict[str, str],
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    """Rows for every candidate whose asset has a chain, sorted by |edge| (largest first)."""
    rows: list[dict] = []
    skipped: list[dict] = []
    for candidate in candidates:
        contract, asset = candidate.contract, candidate.threshold.asset
        chain = chains.get(asset)
        if chain is None:
            reason = f"Deribit {asset} chain unavailable: {chain_errors.get(asset, 'not fetched')}"
            skipped.append(_skip(contract.event_ticker, contract.ticker, contract.label, reason))
            continue
        try:
            rows.append(compare_contract(candidate, chain, now))
        except (UnsupportedContract, deribit.DeribitDataError) as exc:
            skipped.append(_skip(contract.event_ticker, contract.ticker, contract.label, str(exc)))
    rows.sort(key=lambda row: (-abs(row["edge"]), row["ticker"]))
    return rows, skipped


# ─── response (pure) ─────────────────────────────────────────────────────────

def _chain_inputs(chains: dict[str, OptionChain]) -> dict:
    """What was read from Deribit, per asset: index name, S, its timestamp, chain size."""
    return {
        asset: {
            "index_name": chain.index_name,
            "index_price": chain.index_price,
            "index_as_of": iso_utc(chain.as_of),
            "options_in_chain": len(chain.quotes),
        }
        for asset, chain in sorted(chains.items())
    }


def _overall_confidence(rows: list[dict]) -> float:
    """Mean of the row confidences (0 when there are no rows)."""
    if not rows:
        return 0.0
    return round(sum(row["confidence"] for row in rows) / len(rows), CONFIDENCE_DECIMALS)


def build_response(
    request: PmEdgeRequest,
    rows: list[dict],
    skipped: list[dict],
    chains: dict[str, OptionChain],
    event_inputs: dict,
    now: datetime,
) -> dict:
    """The full pm_edge answer: the top `limit` rows plus what was skipped and the inputs used."""
    return {
        "category": request.category,
        "asset_filter": request.asset,
        "rows": rows[: request.limit],
        "rows_total": len(rows),
        "skipped": skipped[:MAX_SKIPPED_REPORTED],
        "skipped_total": len(skipped),
        "inputs_used": {
            "sources": ["gemini", "deribit"],
            **event_inputs,
            "deribit": _chain_inputs(chains),
            "fee_reference_contracts": REFERENCE_CONTRACTS,
        },
        "computed_at": iso_utc(now),
        "method": METHOD,
        "confidence": _overall_confidence(rows[: request.limit]),
        "data_delay": DATA_DELAY,
    }


# ─── network (async) ─────────────────────────────────────────────────────────

async def _event_details(events: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Get Event (fresh contract prices) for each selected event. If one call fails, the
    event's List Events copy is used instead and its ticker is reported as a fallback.
    """
    detailed: list[dict] = []
    fallbacks: list[str] = []
    for event in events:
        try:
            detailed.append(gemini.parse_event(await gemini.fetch_event_payload(event["ticker"])))
        except (UpstreamError, gemini.GeminiDataError):
            detailed.append(event)
            fallbacks.append(event["ticker"])
    return detailed, fallbacks


async def _option_chains(assets: set[str]) -> tuple[dict[str, OptionChain], dict[str, str]]:
    """Deribit chains for `assets`; an asset whose fetch fails is reported in the errors dict."""
    chains: dict[str, OptionChain] = {}
    errors: dict[str, str] = {}
    for asset in sorted(assets):
        try:
            chains[asset] = await deribit.fetch_option_chain(asset)
        except (UpstreamError, deribit.DeribitDataError) as exc:
            errors[asset] = str(exc)
    return chains, errors


async def compute(params: dict) -> dict:
    """Answer one paid request now (the x402 server calls this); see `compute_at`."""
    return await compute_at(params, utc_now())


async def compute_at(params: dict, now: datetime) -> dict:
    """
    Answer one request as of `now`: list Gemini events → Get Event for the selected ones →
    comparable contracts → Deribit chains → rows. Never raises for bad input or upstream
    failure; those (and "no comparable contract") come back as an error dict.
    """
    try:
        request = parse_request(params)
    except PmEdgeInputError as exc:
        return error_response(params, str(exc), ERROR_BAD_REQUEST, now, DATA_DELAY)
    try:
        listed = gemini.parse_events(await gemini.fetch_events_payload(request.category))
    except (UpstreamError, gemini.GeminiDataError) as exc:
        return error_response(params, str(exc), ERROR_UPSTREAM, now, DATA_DELAY)

    selected, skipped_events = select_events(listed, request)
    detailed, fallbacks = await _event_details(selected)
    candidates, skipped_contracts = find_candidates(detailed, now)
    chains, chain_errors = await _option_chains({candidate.threshold.asset for candidate in candidates})
    rows, skipped_rows = compare_all(candidates, chains, chain_errors, now)
    skipped = skipped_events + skipped_contracts + skipped_rows

    if not rows:
        response = error_response(params, "no Gemini contract could be compared with options", ERROR_NO_DATA, now, DATA_DELAY)
        response["skipped"] = skipped[:MAX_SKIPPED_REPORTED]
        return response
    event_inputs = {
        "events_listed": len(listed),
        "events_compared": [event["ticker"] for event in selected],
        "event_detail_fallbacks": fallbacks,
    }
    return build_response(request, rows, skipped, chains, event_inputs, now)
