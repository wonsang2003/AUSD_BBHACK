"""
Seller `insider_cluster` (demo vendor, 0.03 AUSD): are several insiders buying together?

`GET /sell/insider-cluster?ticker=NVDA[&days=90]` (SPEC §4.5). Data: SEC EDGAR
(`app/data/edgar.py`) — ticker → CIK, the company's recent Form 4 filings, and the
transactions inside each Form 4 XML.

Rule (SPEC): a **cluster** is ≥ 2 distinct insiders making open-market purchases
(Form 4 transaction code "P") within 10 days of each other.

How it is computed:
    1. Keep Form 4s filed in the last `days` (default 90), newest first, at most 40.
    2. From each, keep non-derivative transactions with code "P" and shares > 0, dated
       inside the look-back. Value of a purchase = shares · price per share.
    3. Slide a 10-day window over the purchases (each purchase date starts a window
       [d, d + 10 days]); the best window has the most distinct insiders, then the
       largest dollar total, then the latest end date.
    4. cluster_detected = best window has ≥ 2 distinct insiders.

Insiders are identified by their SEC CIK (falling back to name). A joint filing is
attributed to its first reporting owner, so one economic buyer is not counted twice.

Agents read `distinct_insiders` and `total_purchase_usd` — see
`app/agents/beliefs.update_from_insider_cluster`.

This module never imports the x402 server; `app/sellers/routers.py` wraps `compute`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from app.config import get_settings
from app.data import edgar
from app.data.edgar import EdgarError, Filing, InsiderTransaction
from app.data.http import UpstreamError
from app.x402.offer import SellerOffer

# ─── offer ───────────────────────────────────────────────────────────────────

SELLER_ID = "insider_cluster"
SELLER_PATH = "/sell/insider-cluster"
PRICE_ATOMIC = 30_000  # 0.03 AUSD (6 decimals)
DESCRIPTION = "Insider cluster (demo vendor): SEC Form 4 open-market purchases by ≥2 insiders within 10 days"

# ─── rules ───────────────────────────────────────────────────────────────────

OPEN_MARKET_PURCHASE_CODE = "P"
CLUSTER_WINDOW_DAYS = 10
MIN_CLUSTER_INSIDERS = 2

DEFAULT_LOOKBACK_DAYS = 90
MAX_LOOKBACK_DAYS = 365
MAX_FORM4_FILINGS = 40          # newest first; keeps one call to a few seconds at ≤ 10 req/s
TICKER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")

COMPLETE_DATA_CONFIDENCE = 0.9  # official filings; capped below 1 for parsing edge cases
TRUNCATED_CONFIDENCE_FACTOR = 0.8
DATA_DELAY = "Form 4 is due within 2 business days of the trade; EDGAR posts filings as they are accepted"


def make_offer() -> SellerOffer:
    """What this demo vendor charges (0.03 AUSD) and where the AUSD goes (SELLER_PAYTO_INSIDER)."""
    return SellerOffer(
        seller_id=SELLER_ID,
        path=SELLER_PATH,
        description=DESCRIPTION,
        price_atomic=PRICE_ATOMIC,
        pay_to=get_settings().seller_payto_insider,
    )


# ─── data types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Purchase:
    """One open-market purchase by an insider."""

    insider_id: str            # SEC CIK of the reporting owner (or name if missing)
    insider_name: str
    role: str                  # e.g. "Director", "CEO", "10% owner"
    on: date
    shares: float
    price_per_share: float
    value_usd: float
    accession_number: str


@dataclass(frozen=True)
class PurchaseWindow:
    """Purchases falling inside one sliding window [start, start + 10 days]."""

    start: date
    end: date                  # date of the last purchase inside the window
    purchases: list[Purchase]

    @property
    def distinct_insiders(self) -> int:
        """Number of different insiders who bought in this window."""
        return len({purchase.insider_id for purchase in self.purchases})

    @property
    def total_usd(self) -> float:
        """Sum of purchase values in the window."""
        return sum(purchase.value_usd for purchase in self.purchases)


# ─── pure logic ──────────────────────────────────────────────────────────────

def insider_role(transaction: InsiderTransaction) -> str:
    """Readable role: officer title (or "Officer"), "Director", "10% owner", joined by " / "."""
    roles: list[str] = []
    if transaction.is_officer:
        roles.append(transaction.officer_title or "Officer")
    if transaction.is_director:
        roles.append("Director")
    if transaction.is_ten_percent_owner:
        roles.append("10% owner")
    return " / ".join(roles) or "Insider"


def open_market_purchases(transactions: list[InsiderTransaction], since: date) -> list[Purchase]:
    """Transactions with code "P", shares > 0 and a date on/after `since`, as Purchases."""
    purchases: list[Purchase] = []
    for transaction in transactions:
        if transaction.code != OPEN_MARKET_PURCHASE_CODE or transaction.shares <= 0:
            continue
        if transaction.transaction_date < since:
            continue
        purchases.append(
            Purchase(
                insider_id=transaction.owner_cik or transaction.owner_name,
                insider_name=transaction.owner_name,
                role=insider_role(transaction),
                on=transaction.transaction_date,
                shares=transaction.shares,
                price_per_share=transaction.price_per_share,
                value_usd=transaction.value_usd,
                accession_number=transaction.accession_number,
            )
        )
    return purchases


def _window_starting_at(start: date, purchases: list[Purchase], window_days: int) -> PurchaseWindow:
    """All purchases dated in [start, start + window_days]."""
    window_end = start + timedelta(days=window_days)
    members = [purchase for purchase in purchases if start <= purchase.on <= window_end]
    return PurchaseWindow(start=start, end=max(p.on for p in members), purchases=members)


def _window_rank(window: PurchaseWindow) -> tuple[int, float, date]:
    """Sort key: more distinct insiders, then more dollars, then more recent."""
    return (window.distinct_insiders, window.total_usd, window.end)


def best_purchase_window(purchases: list[Purchase], window_days: int = CLUSTER_WINDOW_DAYS) -> PurchaseWindow | None:
    """
    Slide a `window_days` window starting at each purchase date and return the best one
    (most distinct insiders, then largest total, then latest). None if there are no purchases.
    """
    if not purchases:
        return None
    start_dates = sorted({purchase.on for purchase in purchases})
    windows = [_window_starting_at(start, purchases, window_days) for start in start_dates]
    return max(windows, key=_window_rank)


def is_cluster(window: PurchaseWindow | None) -> bool:
    """SPEC rule: ≥ 2 distinct insiders bought within the window."""
    return window is not None and window.distinct_insiders >= MIN_CLUSTER_INSIDERS


def confidence_for(filings_attempted: int, filings_parsed: int, truncated: bool) -> float:
    """
    0.9 · (parsed / attempted), × 0.8 more if the look-back held more Form 4s than we read.
    With no Form 4s in the window the answer ("no buying") is complete → 0.9.
    """
    if filings_attempted == 0:
        return COMPLETE_DATA_CONFIDENCE
    confidence = COMPLETE_DATA_CONFIDENCE * filings_parsed / filings_attempted
    if truncated:
        confidence *= TRUNCATED_CONFIDENCE_FACTOR
    return confidence


def _purchase_json(purchase: Purchase) -> dict:
    """JSON form of a purchase (dates as ISO strings)."""
    row = asdict(purchase)
    row["on"] = purchase.on.isoformat()
    return row


def build_response(
    *,
    ticker: str,
    cik: int,
    purchases: list[Purchase],
    lookback_days: int,
    filings_in_lookback: int,
    filings_attempted: int,
    filings_parsed: int,
    errors: list[str],
    now: datetime,
) -> dict:
    """Assemble the JSON body from parsed purchases (pure, so tests can use fixtures)."""
    window = best_purchase_window(purchases)
    detected = is_cluster(window)
    truncated = filings_in_lookback > filings_attempted
    return {
        "seller_id": SELLER_ID,
        "ticker": ticker,
        "cik": cik,
        "cluster_detected": detected,
        "distinct_insiders": window.distinct_insiders if window else 0,
        "total_purchase_usd": window.total_usd if window else 0.0,
        "cluster": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "purchases": [_purchase_json(p) for p in window.purchases],
        } if detected and window else None,
        "purchases": [_purchase_json(p) for p in sorted(purchases, key=lambda p: p.on, reverse=True)],
        "method": (
            f"SEC Form 4 non-derivative transactions with code P in the last {lookback_days} days; "
            f"cluster = ≥{MIN_CLUSTER_INSIDERS} distinct insiders (by CIK) buying within "
            f"{CLUSTER_WINDOW_DAYS} days; value = shares × price per share"
        ),
        "inputs_used": {
            "source": "SEC EDGAR submissions + Form 4 XML",
            "lookback_days": lookback_days,
            "form4_in_lookback": filings_in_lookback,
            "form4_read": filings_attempted,
            "form4_parsed": filings_parsed,
            "errors": errors,
        },
        "computed_at": now.isoformat(timespec="seconds"),
        "confidence": confidence_for(filings_attempted, filings_parsed, truncated),
        "data_delay": DATA_DELAY,
    }


# ─── request handling ────────────────────────────────────────────────────────

def parse_params(params: dict) -> tuple[str, int]:
    """Validate `ticker` (required) and `days` (1–365, default 90)."""
    ticker = str(params.get("ticker") or "").strip().upper()
    if not TICKER_PATTERN.match(ticker):
        raise ValueError("ticker is required, e.g. ticker=NVDA")
    raw_days = params.get("days")
    days = int(raw_days) if raw_days not in (None, "") else DEFAULT_LOOKBACK_DAYS
    if not 1 <= days <= MAX_LOOKBACK_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_LOOKBACK_DAYS}")
    return ticker, days


def filings_in_lookback(filings: list[Filing], since: date) -> list[Filing]:
    """Form 4s filed on/after `since` (input is newest first, output keeps that order)."""
    return [filing for filing in filings if filing.filing_date >= since]


async def compute(params: dict) -> dict:
    """Resolve the ticker, read recent Form 4s from EDGAR, and look for a buying cluster."""
    ticker, lookback_days = parse_params(params)
    now = datetime.now(timezone.utc)
    since = now.date() - timedelta(days=lookback_days)
    try:
        cik = await edgar.fetch_cik_for_ticker(ticker)
    except EdgarError as exc:
        raise ValueError(str(exc)) from exc
    recent = filings_in_lookback(await edgar.fetch_recent_form4_filings(cik), since)
    to_read = recent[:MAX_FORM4_FILINGS]
    transactions: list[InsiderTransaction] = []
    errors: list[str] = []
    for filing in to_read:
        try:
            transactions.extend(await edgar.fetch_form4_transactions(cik, filing))
        except (UpstreamError, EdgarError) as exc:
            errors.append(f"{filing.accession_number}: {exc}")
    return build_response(
        ticker=ticker,
        cik=cik,
        purchases=open_market_purchases(transactions, since),
        lookback_days=lookback_days,
        filings_in_lookback=len(recent),
        filings_attempted=len(to_read),
        filings_parsed=len(to_read) - len(errors),
        errors=errors,
        now=now,
    )
