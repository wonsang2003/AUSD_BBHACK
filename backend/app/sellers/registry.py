"""
Data catalog: every research source PocketDesk knows about, and how (or whether) it can be bought.

Served by `GET /api/catalog` and shown on the `/sources` page (SPEC §4 registry + §9 Update #1).
PocketDesk is BUYER-SIDE ONLY, so the catalog is honest about what each row really is:

    status                    payable_mode          meaning
    live_ausd_seller          x402_ausd_testnet     our DEMO VENDORS: testnet stand-ins on this backend
                                                    that agents really pay per call in test AUSD
    external_x402_flagged     x402_usdc_mainnet     real x402 vendors on mainnet USDC — PocketDesk never
                                                    pays them; the live probe only reads their 402 price
    metered_adapter_roadmap   metered_api_key       usage-billed APIs behind an API key (roadmap adapters)
    free_input                free                  public data our demo vendors / dashboard read for free
    not_per_call              subscription_contract data that is NOT sold per call at all (terminals,
                                                    consensus, redistribution licences, panels, ...)

Demo-vendor rows take their price, path, description and payout address from each seller
module's `make_offer()` — the single source of truth — so the catalog can never show a
different price from the one the x402 gate charges. Everything else is a static row.

`trader_value` is a rough S / A / B rating of how much the data matters to a trader
(S = moves decisions on its own, A = strong supporting input, B = context).
"""

from __future__ import annotations

import importlib
from dataclasses import asdict, dataclass

from app.constants import ONE_AUSD
from app.x402.offer import SellerOffer

# ─── vocabularies ────────────────────────────────────────────────────────────

STATUS_LIVE_AUSD_SELLER = "live_ausd_seller"
STATUS_EXTERNAL_X402_FLAGGED = "external_x402_flagged"
STATUS_METERED_ROADMAP = "metered_adapter_roadmap"
STATUS_FREE_INPUT = "free_input"
STATUS_NOT_PER_CALL = "not_per_call"

PAYABLE_X402_AUSD_TESTNET = "x402_ausd_testnet"
PAYABLE_X402_USDC_MAINNET = "x402_usdc_mainnet"
PAYABLE_METERED_API_KEY = "metered_api_key"
PAYABLE_FREE = "free"
PAYABLE_SUBSCRIPTION = "subscription_contract"   # only for the honest "not per call" row

LABEL_DEMO_VENDOR = "demo vendor"
LABEL_EXTERNAL_FLAGGED = "external x402 vendor (flagged: mainnet USDC, never paid by PocketDesk)"
LABEL_METERED = "metered API (roadmap)"
LABEL_FREE = "free input"
LABEL_NOT_PER_CALL = "not sold per call"

TRADER_VALUE_S = "S"
TRADER_VALUE_A = "A"
TRADER_VALUE_B = "B"

SECTOR_ONCHAIN_PERPS = "OnchainPerps"
SECTOR_OPTIONS_EVENT = "OptionsEvent"
SECTOR_NEWS_SENTIMENT = "NewsSentiment"
SECTOR_MACRO_FILINGS = "MacroFilings"
SECTOR_DASHBOARD = "Dashboard"     # used by the market strip, not by an agent
SECTOR_ALL = "All"

PRICING_PER_CALL_X402 = "per call (x402)"
PRICING_USAGE_BASED = "usage-based (API key)"
PRICING_FREE = "free"
PRICING_CONTRACT = "subscription / contract"

SELLERS_PACKAGE = "app.sellers"
EXTERNAL_X402_NOTE = (
    "Real x402 vendor on mainnet USDC. PocketDesk never signs or pays it; the live probe "
    "(GET /api/x402-probe) sends an UNPAID request and shows the price from its 402 answer."
)


@dataclass(frozen=True)
class CatalogEntry:
    """One row of the data catalog (the `/api/catalog` JSON row)."""

    id: str
    name: str
    sector: str                 # agent that would use it (or Dashboard / All)
    description: str
    trader_value: str           # S / A / B
    source: str                 # who provides the data
    pricing_model: str
    payable_mode: str
    price: str                  # human-readable price
    status: str
    label: str                  # what the UI badge says, e.g. "demo vendor"
    price_atomic: int | None = None   # AUSD atomic units (6 decimals) — demo vendors only
    path: str | None = None           # HTTP path on this backend — demo vendors only
    pay_to: str | None = None         # payout address — demo vendors only
    notes: str = ""

    def to_dict(self) -> dict:
        """Plain JSON form of this row."""
        return asdict(self)


# ─── demo vendors (ours, priced by make_offer) ───────────────────────────────

@dataclass(frozen=True)
class DemoVendorInfo:
    """Catalog facts about a demo vendor that its SellerOffer does not carry."""

    seller_id: str
    name: str
    sector: str
    trader_value: str
    source: str


DEMO_VENDORS: dict[str, DemoVendorInfo] = {
    info.seller_id: info
    for info in (
        DemoVendorInfo("expected_move", "Expected move", SECTOR_OPTIONS_EVENT, TRADER_VALUE_S,
                       "Deribit options (crypto); yfinance option chains (equities, 15m+ delayed)"),
        DemoVendorInfo("news_sentiment", "News sentiment", SECTOR_NEWS_SENTIMENT, TRADER_VALUE_A,
                       "GDELT DOC 2.0 (+ Context.dev when a key is set)"),
        DemoVendorInfo("insider_cluster", "Insider buying cluster", SECTOR_MACRO_FILINGS, TRADER_VALUE_A,
                       "SEC EDGAR Form 4 filings"),
        DemoVendorInfo("perps_crowding", "Perps crowding", SECTOR_ONCHAIN_PERPS, TRADER_VALUE_A,
                       "Hyperliquid public API (funding, open interest)"),
        DemoVendorInfo("macro_pulse", "Macro pulse", SECTOR_MACRO_FILINGS, TRADER_VALUE_B,
                       "FRED, official FOMC/CPI schedule, Gemini economics odds"),
        DemoVendorInfo("pm_edge", "Prediction-market edge", SECTOR_OPTIONS_EVENT, TRADER_VALUE_S,
                       "Gemini Predictions vs Deribit options-implied probability"),
    )
}


def load_offer(seller_id: str) -> SellerOffer | None:
    """
    The demo vendor's live offer from `app.sellers.<seller_id>.make_offer()`.
    Returns None only when that seller module does not exist (not built yet); any
    other import error is a bug and is raised.
    """
    module_name = f"{SELLERS_PACKAGE}.{seller_id}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise
    return module.make_offer()


def format_ausd(price_atomic: int) -> str:
    """50_000 → "0.05 AUSD (testnet)"."""
    return f"{price_atomic / ONE_AUSD:.2f} AUSD (testnet)"


def demo_vendor_entry(info: DemoVendorInfo) -> CatalogEntry:
    """A demo-vendor row priced from its SellerOffer (or marked "not built yet" if the module is missing)."""
    offer = load_offer(info.seller_id)
    if offer is None:
        return CatalogEntry(
            id=info.seller_id,
            name=info.name,
            sector=info.sector,
            description=f"{info.name} (demo vendor)",
            trader_value=info.trader_value,
            source=info.source,
            pricing_model=PRICING_PER_CALL_X402,
            payable_mode=PAYABLE_X402_AUSD_TESTNET,
            price="not built yet",
            status=STATUS_LIVE_AUSD_SELLER,
            label=LABEL_DEMO_VENDOR,
            notes=f"seller module {SELLERS_PACKAGE}.{info.seller_id} is not installed",
        )
    return CatalogEntry(
        id=offer.seller_id,
        name=info.name,
        sector=info.sector,
        description=offer.description,
        trader_value=info.trader_value,
        source=info.source,
        pricing_model=PRICING_PER_CALL_X402,
        payable_mode=PAYABLE_X402_AUSD_TESTNET,
        price=format_ausd(offer.price_atomic),
        status=STATUS_LIVE_AUSD_SELLER,
        label=LABEL_DEMO_VENDOR,
        price_atomic=offer.price_atomic,
        path=offer.path,
        pay_to=offer.pay_to or None,
        notes="Testnet stand-in so agents have something real to buy; paid per call in test AUSD via x402.",
    )


# ─── static rows ─────────────────────────────────────────────────────────────

def _external_x402(
    row_id: str, name: str, sector: str, value: str, description: str, source: str, price: str
) -> CatalogEntry:
    """A real mainnet x402 vendor: flagged, never paid."""
    return CatalogEntry(
        id=row_id,
        name=name,
        sector=sector,
        description=description,
        trader_value=value,
        source=source,
        pricing_model=PRICING_PER_CALL_X402,
        payable_mode=PAYABLE_X402_USDC_MAINNET,
        price=price,
        status=STATUS_EXTERNAL_X402_FLAGGED,
        label=LABEL_EXTERNAL_FLAGGED,
        notes=EXTERNAL_X402_NOTE,
    )


def _metered(
    row_id: str, name: str, sector: str, value: str, description: str, source: str, price: str, notes: str = ""
) -> CatalogEntry:
    """A usage-billed API behind an API key (adapter on the roadmap)."""
    return CatalogEntry(
        id=row_id,
        name=name,
        sector=sector,
        description=description,
        trader_value=value,
        source=source,
        pricing_model=PRICING_USAGE_BASED,
        payable_mode=PAYABLE_METERED_API_KEY,
        price=price,
        status=STATUS_METERED_ROADMAP,
        label=LABEL_METERED,
        notes=notes,
    )


def _free(
    row_id: str, name: str, sector: str, value: str, description: str, source: str, notes: str = ""
) -> CatalogEntry:
    """A public, key-less input."""
    return CatalogEntry(
        id=row_id,
        name=name,
        sector=sector,
        description=description,
        trader_value=value,
        source=source,
        pricing_model=PRICING_FREE,
        payable_mode=PAYABLE_FREE,
        price="free",
        status=STATUS_FREE_INPUT,
        label=LABEL_FREE,
        notes=notes,
    )


NOT_PER_CALL_ENTRY = CatalogEntry(
    id="not_per_call",
    name="Institutional data not sold per call",
    sector=SECTOR_ALL,
    description=(
        "Terminal data, consensus estimates, real-time feeds licensed for redistribution, "
        "card-spend panels, satellite imagery, expert-network calls"
    ),
    trader_value=TRADER_VALUE_S,
    source="Terminal vendors, exchanges, alternative-data and expert networks",
    pricing_model=PRICING_CONTRACT,
    payable_mode=PAYABLE_SUBSCRIPTION,
    price="annual contracts; no per-call price",
    status=STATUS_NOT_PER_CALL,
    label=LABEL_NOT_PER_CALL,
    notes="Listed honestly: PocketDesk cannot buy these per call, so its agents do not use them.",
)


def _static_rows() -> dict[str, CatalogEntry]:
    """Every non-demo row, keyed by id."""
    rows = [
        _external_x402("nansen_smart_money", "Nansen smart money", SECTOR_ONCHAIN_PERPS, TRADER_VALUE_S,
                       "Smart-money wallet flows and labels", "Nansen x402 API", "$0.01 / $0.05 USDC per call"),
        _metered("databento_opra", "Databento OPRA options", SECTOR_OPTIONS_EVENT, TRADER_VALUE_S,
                 "Consolidated US options quotes and trades (OPRA)", "Databento", "usage-based"),
        _metered("x_api_posts", "X API pay-per-use", SECTOR_NEWS_SENTIMENT, TRADER_VALUE_A,
                 "Posts and search from X", "X API", "$0.005 per post"),
        _external_x402("x402stock_earnings", "x402stock earnings", SECTOR_OPTIONS_EVENT, TRADER_VALUE_A,
                       "Earnings data and calendar", "x402stock", "$0.05 / $0.75 USDC per call"),
        _external_x402("coingecko_x402", "CoinGecko x402", SECTOR_ONCHAIN_PERPS, TRADER_VALUE_B,
                       "Crypto prices (/x402/simple/price)", "CoinGecko", "$0.01 USDC per call"),
        _external_x402("coinmarketcap_x402", "CoinMarketCap x402 (beta)", SECTOR_ONCHAIN_PERPS, TRADER_VALUE_B,
                       "Crypto quotes (/x402/v3/cryptocurrency/quotes/latest)", "CoinMarketCap",
                       "$0.01 USDC per call (beta)"),
        _external_x402("x402stock_sentiment", "x402stock sentiment", SECTOR_NEWS_SENTIMENT, TRADER_VALUE_B,
                       "Stock sentiment scores", "x402stock", "$0.02 USDC per call"),
        _free("edgar_filings", "SEC EDGAR filings", SECTOR_MACRO_FILINGS, TRADER_VALUE_A,
              "Company filings incl. Form 4 insider trades", "U.S. SEC (data.sec.gov)",
              "Input to insider_cluster; needs a contact User-Agent, ≤ 10 requests/s."),
        _metered("flightaware_aeroapi", "FlightAware AeroAPI", SECTOR_MACRO_FILINGS, TRADER_VALUE_B,
                 "Flight tracking (e.g. corporate-jet activity)", "FlightAware", "per query"),
        _metered("contextdev_web", "Context.dev web data", SECTOR_NEWS_SENTIMENT, TRADER_VALUE_B,
                 "Fresh web search results", "Context.dev", "metered credits",
                 notes="Adapter exists: optional extra headlines for news_sentiment when CONTEXT_DEV_API_KEY is set."),
        _metered("databento_ticks", "Databento tick data", SECTOR_OPTIONS_EVENT, TRADER_VALUE_A,
                 "Equity and futures tick-by-tick data", "Databento", "usage-based"),
        _free("open_meteo_weather", "Open-Meteo weather", SECTOR_MACRO_FILINGS, TRADER_VALUE_B,
              "Weather forecasts (commodity and event context)", "Open-Meteo"),
        _free("deribit_public", "Deribit public options", SECTOR_OPTIONS_EVENT, TRADER_VALUE_A,
              "Crypto option marks and implied vols", "Deribit public API", "Input to expected_move and pm_edge."),
        _free("hyperliquid_public", "Hyperliquid public perps", SECTOR_ONCHAIN_PERPS, TRADER_VALUE_A,
              "Perp funding, open interest and mids", "Hyperliquid info API",
              "Input to perps_crowding and the dashboard market strip."),
        _free("gdelt_news", "GDELT news", SECTOR_NEWS_SENTIMENT, TRADER_VALUE_B,
              "Global news headlines and tone", "GDELT DOC 2.0 API", "Input to news_sentiment."),
        _free("fred_macro", "FRED macro series", SECTOR_MACRO_FILINGS, TRADER_VALUE_B,
              "CPI, fed funds, Treasury yields, unemployment", "Federal Reserve Bank of St. Louis",
              "Input to macro_pulse."),
        _free("gemini_predictions", "Gemini Predictions (public)", SECTOR_OPTIONS_EVENT, TRADER_VALUE_A,
              "Prediction-market contracts and prices", "Gemini public API", "Input to pm_edge and macro_pulse."),
        _free("coingecko_public", "CoinGecko public prices", SECTOR_DASHBOARD, TRADER_VALUE_B,
              "Spot crypto prices", "CoinGecko keyless API", "Dashboard market strip only."),
        _free("yfinance_delayed", "yfinance (delayed)", SECTOR_OPTIONS_EVENT, TRADER_VALUE_B,
              "Equity quotes and option chains, 15m+ delayed", "Yahoo Finance via unofficial yfinance",
              "Input to expected_move (equities) and the market strip; unofficial and delayed."),
    ]
    return {row.id: row for row in rows}


# Display order: SPEC §4's catalog order, then the other free inputs, then the honest last row.
CATALOG_ORDER = (
    "nansen_smart_money", "expected_move", "databento_opra", "x_api_posts", "news_sentiment",
    "insider_cluster", "x402stock_earnings", "coingecko_x402", "coinmarketcap_x402", "perps_crowding",
    "x402stock_sentiment", "edgar_filings", "macro_pulse", "pm_edge", "flightaware_aeroapi",
    "contextdev_web", "databento_ticks", "open_meteo_weather", "deribit_public", "hyperliquid_public",
    "gdelt_news", "fred_macro", "gemini_predictions", "coingecko_public", "yfinance_delayed",
    "not_per_call",
)


def list_catalog() -> list[CatalogEntry]:
    """The full catalog in display order (demo-vendor prices read live from make_offer())."""
    static_rows = _static_rows()
    static_rows[NOT_PER_CALL_ENTRY.id] = NOT_PER_CALL_ENTRY
    catalog: list[CatalogEntry] = []
    for row_id in CATALOG_ORDER:
        if row_id in DEMO_VENDORS:
            catalog.append(demo_vendor_entry(DEMO_VENDORS[row_id]))
        else:
            catalog.append(static_rows[row_id])
    return catalog


def catalog_json() -> list[dict]:
    """`list_catalog()` as JSON rows for `GET /api/catalog`."""
    return [entry.to_dict() for entry in list_catalog()]
