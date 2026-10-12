"""
Live x402 probe (SPEC §9 Update #1 E): what do REAL mainnet x402 vendors charge right now?

PocketDesk only reads their price. For each vendor it sends ONE UNPAID GET (no payment
header), expects HTTP 402, and parses the answer with the SAME parser the agent client uses
(`app.x402.models.parse_payment_required`: v2 `PAYMENT-REQUIRED` header, v1 JSON-body
fallback). It NEVER signs, never builds a payment and never pays — these vendors want USDC
on Base mainnet, which PocketDesk's AUSD testnet budget cannot route (roadmap).

Flow:  probe_all(refresh) → cached 10 min → probe_vendor(name, url) per target → ProbeResult
       → `GET /api/x402-probe` rows (`ProbeResult.to_api()`).

Every failure (timeout, non-402 status, unparseable 402) becomes that vendor's `error`;
`probe_all` never raises.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass

import httpx

from app.x402.models import PaymentRequired, parse_payment_required

# ─── targets ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProbeTarget:
    """One vendor endpoint to probe with an unpaid GET."""

    name: str
    url: str
    verified: bool        # False: URL is our best guess, not confirmed against the vendor's docs
    note: str = ""


PROBE_TARGETS: tuple[ProbeTarget, ...] = (
    ProbeTarget("CoinGecko", "https://pro-api.coingecko.com/api/v3/x402/simple/price?ids=bitcoin&vs_currencies=usd",
                verified=True),
    ProbeTarget("CoinMarketCap",
                "https://pro-api.coinmarketcap.com/x402/v3/cryptocurrency/quotes/latest?symbol=BTC", verified=True),
    ProbeTarget("Nansen", "https://api.nansen.ai/api/v1/smart-money/holdings", verified=False,
                note="unverified for GET: Nansen documents this x402 endpoint as POST; the probe only sends GETs"),
    ProbeTarget("x402stock", "https://x402stock.xyz/api/quote?ticker=AAPL", verified=False,
                note="unverified: path inferred from a third-party directory listing of x402stock.xyz"),
)

# ─── asset symbols and display rules ─────────────────────────────────────────

KNOWN_ASSETS: dict[str, str] = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC (Base mainnet)",
    "0x036cbd53842c5426634e7929541ec2318f3dcf7e": "USDC (Base Sepolia)",
    "0xa9012a055bd4e0edff8ce09f960291c09d5322dc": "AUSD (testnet)",
}
TOKEN_DECIMALS = 6               # USDC and AUSD both use 6 decimals
HTTP_PAYMENT_REQUIRED = 402
PROBE_TIMEOUT_SECONDS = 8.0
PROBE_CACHE_TTL_SECONDS = 600.0  # 10 minutes
PAYABLE_NOTE = "No — USDC on Base mainnet; AUSD routing = roadmap"
SHORT_ADDRESS_CHARS = 6

_cache: dict[str, object] = {"expires_at": 0.0, "rows": []}


@dataclass
class ProbeResult:
    """What one unpaid GET revealed about a vendor's x402 price."""

    name: str
    url: str
    verified: bool
    status_code: int | None = None
    x402_version: int | None = None
    network: str | None = None
    asset: str | None = None
    asset_symbol: str | None = None
    amount_atomic: str | None = None
    amount_human: float | None = None
    pay_to: str | None = None
    pay_to_short: str | None = None
    description: str = ""
    payable_from_budget: bool = False
    payable_note: str = PAYABLE_NOTE
    note: str = ""
    error: str | None = None
    probed_at: float = 0.0

    def to_api(self) -> dict:
        """JSON row for `GET /api/x402-probe`."""
        return asdict(self)


# ─── pure helpers ────────────────────────────────────────────────────────────

def asset_symbol(address: str) -> str:
    """Known token address → symbol label; anything else → "unknown token"."""
    return KNOWN_ASSETS.get((address or "").lower(), "unknown token")


def amount_human(amount_atomic: str) -> float:
    """Atomic units (6 decimals) → token units: "10000" → 0.01."""
    return int(amount_atomic) / 10**TOKEN_DECIMALS


def short_address(address: str) -> str:
    """0x1234567890abcdef… → "0x1234…cdef"."""
    if not address or len(address) <= 2 * SHORT_ADDRESS_CHARS:
        return address
    return f"{address[:SHORT_ADDRESS_CHARS]}…{address[-4:]}"


def result_from_402(target: ProbeTarget, offer: PaymentRequired, status_code: int) -> ProbeResult:
    """Fill a ProbeResult from the first `accepts[]` requirement of a parsed 402."""
    result = ProbeResult(target.name, target.url, target.verified, status_code=status_code,
                         x402_version=offer.x402Version, note=target.note, probed_at=time.time())
    if not offer.accepts:
        result.error = "402 without any accepts[] requirement"
        return result
    first = offer.accepts[0]
    result.network = first.network
    result.asset = first.asset
    result.asset_symbol = asset_symbol(first.asset)
    result.amount_atomic = first.amount
    result.amount_human = amount_human(first.amount)
    result.pay_to = first.payTo
    result.pay_to_short = short_address(first.payTo)
    result.description = offer.resource.description
    return result


# ─── network (unpaid GET only) ───────────────────────────────────────────────

async def probe_vendor(name: str, url: str, *, verified: bool = True, note: str = "",
                       transport: httpx.AsyncBaseTransport | None = None) -> ProbeResult:
    """
    Send ONE unpaid GET to `url` and parse its 402. Never signs or pays; never raises —
    any failure becomes `ProbeResult.error`.
    """
    target = ProbeTarget(name, url, verified, note)
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return ProbeResult(name, url, verified, note=note, error=f"{type(exc).__name__}: {exc}", probed_at=time.time())
    if response.status_code != HTTP_PAYMENT_REQUIRED:
        return ProbeResult(name, url, verified, status_code=response.status_code, note=note,
                           error=f"expected HTTP 402, got {response.status_code}", probed_at=time.time())
    try:
        offer = parse_payment_required(response.headers, response.content, url)
    except Exception as exc:  # malformed vendor answer: report it, never crash the dashboard
        return ProbeResult(name, url, verified, status_code=402, note=note,
                           error=f"unparseable 402: {type(exc).__name__}: {exc}", probed_at=time.time())
    return result_from_402(target, offer, response.status_code)


async def probe_all(refresh: bool = False, transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """All targets probed concurrently, cached 10 minutes (`refresh=True` bypasses the cache)."""
    if not refresh and _cache["expires_at"] > time.monotonic():
        return list(_cache["rows"])
    results = await asyncio.gather(*(
        probe_vendor(target.name, target.url, verified=target.verified, note=target.note, transport=transport)
        for target in PROBE_TARGETS
    ))
    rows = [result.to_api() for result in results]
    _cache["rows"] = rows
    _cache["expires_at"] = time.monotonic() + PROBE_CACHE_TTL_SECONDS
    return list(rows)


def clear_cache() -> None:
    """Forget the cached probe rows (tests)."""
    _cache["rows"] = []
    _cache["expires_at"] = 0.0
