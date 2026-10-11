"""
The data catalog and the live x402 probe (SPEC §4 + §9 Update #1 E):

    GET /api/catalog              every research source and how (or whether) it can be bought;
                                  demo vendors are priced live from their `make_offer()`
    GET /api/x402-probe[?refresh=1]
                                  UNPAID requests to real mainnet x402 vendors; their 402 answers
                                  are parsed with the client's own parser. Never signs or pays.
                                  Cached 10 minutes by `x402/probe.py`; `refresh=1` skips the cache.

The catalog is returned as a plain list (what `web/lib/api.ts:getCatalog` expects first).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from app.api.deps import HTTP_UNAVAILABLE, api_errors
from app.sellers import registry

router = APIRouter(tags=["catalog"])


def _entry_json(entry: Any) -> dict[str, Any]:
    """One CatalogEntry as JSON (`to_api()` when the entry has it, else its dataclass dict)."""
    to_api = getattr(entry, "to_api", None)
    return to_api() if to_api is not None else entry.to_dict()


def catalog_rows() -> list[dict[str, Any]]:
    """The full catalog in display order."""
    return [_entry_json(entry) for entry in registry.list_catalog()]


@router.get("/api/catalog")
async def catalog() -> list[dict[str, Any]]:
    """`[CatalogEntry]` — demo vendors (labeled "demo vendor"), flagged real x402 vendors, free inputs."""
    with api_errors():
        return await run_in_threadpool(catalog_rows)


def probe_payload(vendors: Any, refreshed: bool) -> dict[str, Any]:
    """
    Wrap `probe_all()`'s vendor rows as `{probed_at, cached, vendors}` (web `X402ProbeResult`).
    Rule: `probed_at` is the newest per-vendor probe time when rows carry one, else now;
    `cached` is what the rows say, else "not refreshed on request".
    """
    if isinstance(vendors, dict) and "vendors" in vendors:
        return vendors
    rows = list(vendors)
    times = [row["probed_at"] for row in rows if isinstance(row, dict) and row.get("probed_at")]
    flags = [bool(row["cached"]) for row in rows if isinstance(row, dict) and "cached" in row]
    newest = max(times) if times else datetime.now(UTC).isoformat()
    if isinstance(newest, int | float):            # unix seconds → ISO-8601 (web `IsoTime`)
        newest = datetime.fromtimestamp(newest, UTC).isoformat()
    return {
        "probed_at": newest,
        "cached": all(flags) if flags else not refreshed,
        "vendors": rows,
    }


@router.get("/api/x402-probe")
async def x402_probe(refresh: bool = Query(False)) -> dict[str, Any]:
    """Live 402 prices of real x402 vendors (unpaid requests only)."""
    try:
        from app.x402.probe import probe_all  # imported per call: the probe module pulls in the x402 client stack
    except ImportError as exc:
        raise HTTPException(HTTP_UNAVAILABLE, f"x402 probe is not available: {exc}") from exc
    with api_errors():
        vendors = await probe_all(refresh=refresh)
    return probe_payload(vendors, refresh)
