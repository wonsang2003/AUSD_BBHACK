"""
SEC EDGAR adapter: ticker → CIK, a company's recent Form 4 filings, and the insider
transactions inside each Form 4.

Used by the `insider_cluster` seller (SPEC §4.5). Three public documents:

    1. https://www.sec.gov/files/company_tickers.json
         {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    2. https://data.sec.gov/submissions/CIK0000320193.json
         {"cik": "320193", "name": ..., "filings": {"recent": {"form": [...], "accessionNumber": [...],
          "filingDate": [...], "primaryDocument": [...], ...}}}       (parallel arrays, newest first)
    3. https://www.sec.gov/Archives/edgar/data/<cik>/<accession without dashes>/<form4>.xml
         the raw Form 4 XML (`<ownershipDocument>`)

For Form 4, `primaryDocument` points at the XSL-rendered HTML view, e.g.
"xslF345X05/form4.xml"; the raw XML is the same file name without the "xslF345X05/"
folder, which is what `form4_xml_url` builds.

SEC rules we follow: every request carries the `User-Agent` from `SEC_USER_AGENT`
("Company Name contact@email"), and we stay under 10 requests per second (the shared
HTTP helper spaces "edgar" requests ≥ 0.12 s apart).

Form 4 transaction codes used downstream: "P" = open-market or private purchase,
"S" = open-market sale (full list: SEC Form 4 General Instructions, item 8).
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import date

from app.config import get_settings
from app.data.http import get_json, get_text

SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_ARCHIVES_URL_TEMPLATE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
SOURCE_NAME = "edgar"

FORM_4 = "4"
ISO_DATE_LENGTH = len("2026-01-31")   # Form 4 dates sometimes carry a timezone suffix; we keep the date
TRUE_FLAGS = {"1", "true"}

TICKERS_CACHE_TTL_SECONDS = 24 * 3600.0     # the ticker map changes rarely
SUBMISSIONS_CACHE_TTL_SECONDS = 600.0
FORM4_CACHE_TTL_SECONDS = 24 * 3600.0       # a filed document never changes


class EdgarError(ValueError):
    """EDGAR data was missing or not in the documented shape."""


@dataclass(frozen=True)
class Filing:
    """One row of `filings.recent` in the submissions JSON."""

    accession_number: str      # "0001234567-26-000123"
    filing_date: date
    form: str                  # "4", "4/A", "10-K", ...
    primary_document: str      # e.g. "xslF345X05/form4.xml"


@dataclass(frozen=True)
class InsiderTransaction:
    """One non-derivative transaction from a Form 4."""

    owner_cik: str
    owner_name: str
    is_director: bool
    is_officer: bool
    officer_title: str
    is_ten_percent_owner: bool
    security_title: str
    transaction_date: date
    code: str                  # Form 4 transaction code, "P" = purchase
    shares: float
    price_per_share: float     # 0.0 when the filing gives no price
    acquired_disposed: str     # "A" acquired / "D" disposed
    accession_number: str

    @property
    def value_usd(self) -> float:
        """Dollar value of the transaction = shares · price per share."""
        return self.shares * self.price_per_share


# ─── request headers ─────────────────────────────────────────────────────────

def edgar_headers() -> dict[str, str]:
    """Headers SEC requires: a descriptive User-Agent with contact details."""
    return {"User-Agent": get_settings().sec_user_agent, "Accept-Encoding": "gzip, deflate"}


# ─── pure parsing ────────────────────────────────────────────────────────────

def parse_company_tickers(payload: dict) -> dict[str, int]:
    """Map upper-case ticker → CIK (int) from company_tickers.json."""
    return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in payload.values()}


def parse_recent_filings(submissions: dict, form: str = FORM_4) -> list[Filing]:
    """
    Turn the parallel arrays in `filings.recent` into Filing rows of the given form type,
    newest first (EDGAR already orders them newest first).
    """
    try:
        recent = submissions["filings"]["recent"]
        forms = recent["form"]
        accessions = recent["accessionNumber"]
        filing_dates = recent["filingDate"]
        documents = recent["primaryDocument"]
    except KeyError as exc:
        raise EdgarError(f"submissions JSON is missing {exc}") from exc
    filings: list[Filing] = []
    for index, form_type in enumerate(forms):
        if form_type != form:
            continue
        filings.append(
            Filing(
                accession_number=accessions[index],
                filing_date=date.fromisoformat(filing_dates[index]),
                form=form_type,
                primary_document=documents[index],
            )
        )
    return filings


def form4_xml_url(cik: int, filing: Filing) -> str:
    """
    URL of the raw Form 4 XML: drop the XSL folder from `primaryDocument`
    ("xslF345X05/form4.xml" → "form4.xml") and the dashes from the accession number.
    """
    raw_document = filing.primary_document.rsplit("/", 1)[-1]
    accession_folder = filing.accession_number.replace("-", "")
    return SEC_ARCHIVES_URL_TEMPLATE.format(cik=cik, accession=accession_folder, document=raw_document)


def _text(element: ElementTree.Element | None, path: str, default: str = "") -> str:
    """Stripped text at `path` under `element`, or `default` if missing."""
    if element is None:
        return default
    found = element.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _flag(element: ElementTree.Element | None, path: str) -> bool:
    """Form 4 booleans are written "1"/"0" or "true"/"false"."""
    return _text(element, path).lower() in TRUE_FLAGS


def _number(element: ElementTree.Element, path: str) -> float:
    """A numeric `<value>`; missing (e.g. price only in a footnote) counts as 0.0."""
    text = _text(element, path)
    return float(text) if text else 0.0


def _first_reporting_owner(root: ElementTree.Element) -> dict:
    """
    The first `<reportingOwner>`. Joint filings (e.g. a fund and its general partner)
    list several owners for the same shares; we attribute the trade to the first one
    so one economic buyer is not counted twice.
    """
    owner = root.find("reportingOwner")
    if owner is None:
        raise EdgarError("Form 4 has no reportingOwner")
    relationship = owner.find("reportingOwnerRelationship")
    return {
        "owner_cik": _text(owner, "reportingOwnerId/rptOwnerCik"),
        "owner_name": _text(owner, "reportingOwnerId/rptOwnerName"),
        "is_director": _flag(relationship, "isDirector"),
        "is_officer": _flag(relationship, "isOfficer"),
        "officer_title": _text(relationship, "officerTitle"),
        "is_ten_percent_owner": _flag(relationship, "isTenPercentOwner"),
    }


def parse_form4_xml(xml_text: str, accession_number: str = "") -> list[InsiderTransaction]:
    """Every non-derivative transaction in one Form 4 XML document."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise EdgarError(f"Form 4 XML does not parse: {exc}") from exc
    owner = _first_reporting_owner(root)
    transactions: list[InsiderTransaction] = []
    for row in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        raw_date = _text(row, "transactionDate/value")
        if not raw_date:
            continue
        transactions.append(
            InsiderTransaction(
                **owner,
                security_title=_text(row, "securityTitle/value"),
                transaction_date=date.fromisoformat(raw_date[:ISO_DATE_LENGTH]),
                code=_text(row, "transactionCoding/transactionCode"),
                shares=_number(row, "transactionAmounts/transactionShares/value"),
                price_per_share=_number(row, "transactionAmounts/transactionPricePerShare/value"),
                acquired_disposed=_text(row, "transactionAmounts/transactionAcquiredDisposedCode/value"),
                accession_number=accession_number,
            )
        )
    return transactions


# ─── network ─────────────────────────────────────────────────────────────────

async def fetch_cik_for_ticker(ticker: str) -> int:
    """Resolve a ticker (e.g. "NVDA") to its SEC CIK; raises EdgarError if unknown."""
    payload = await get_json(
        SEC_COMPANY_TICKERS_URL, source=SOURCE_NAME, headers=edgar_headers(), ttl=TICKERS_CACHE_TTL_SECONDS
    )
    cik = parse_company_tickers(payload).get(ticker.upper())
    if cik is None:
        raise EdgarError(f"ticker {ticker!r} not found in SEC company_tickers.json")
    return cik


async def fetch_recent_form4_filings(cik: int) -> list[Filing]:
    """The company's recent Form 4 filings, newest first."""
    submissions = await get_json(
        SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=cik),
        source=SOURCE_NAME,
        headers=edgar_headers(),
        ttl=SUBMISSIONS_CACHE_TTL_SECONDS,
    )
    return parse_recent_filings(submissions, FORM_4)


async def fetch_form4_transactions(cik: int, filing: Filing) -> list[InsiderTransaction]:
    """Download and parse one Form 4."""
    xml_text = await get_text(
        form4_xml_url(cik, filing), source=SOURCE_NAME, headers=edgar_headers(), ttl=FORM4_CACHE_TTL_SECONDS
    )
    return parse_form4_xml(xml_text, filing.accession_number)
