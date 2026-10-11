"""
Seller side of x402: `make_paid_router(offer, compute)` turns any async function into an
endpoint that is sold per call for AUSD.

    GET <path>                       → 402 + PAYMENT-REQUIRED (what to pay, to whom, in which token)
    GET <path> + PAYMENT-SIGNATURE   → verify → compute → settle on-chain → 200 + data + PAYMENT-RESPONSE

Data is returned ONLY after settlement succeeded (receipt status 1). Order of a paid request:

    verify → compute → render the JSON body → re-check the authorization has ≥ 6 s left → settle → 200

Rendering BEFORE settling means a result that cannot be serialized is never charged.
Responses for failures:
    402 + PAYMENT-RESPONSE{errorReason}   definite failure, nothing was charged
    502 {charged: false}                  the item could not be produced, nothing was charged
    504 {charged: "unknown"}              settlement outcome unknown (no receipt yet); the buyer
                                          resolves it on-chain via authorizationState

Only x402 v2 (`PAYMENT-SIGNATURE`) is accepted; the legacy v1 `X-PAYMENT` header is not.

In PocketDesk these endpoints are demo vendors: testnet stand-ins so the buyer-side
agents have something real to pay for.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from app.config import get_settings
from app.constants import (
    AUSD_EIP712_NAME,
    AUSD_EIP712_VERSION,
    BASE_SEPOLIA_CAIP2,
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
)
from app.x402.models import (
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettlementResponse,
    decode_header,
    encode_header,
)
from app.x402.offer import SellerOffer
from app.x402.settlement import SettlementUnknown
from app.x402.verify import has_settlement_headroom

log = logging.getLogger(__name__)

ComputeFn = Callable[[dict], Awaitable[dict]]
OnSettled = Callable[[SellerOffer, PaymentPayload, SettlementResponse, dict], None]


class _TtlCache:
    """Per-seller result cache: identical params within the TTL reuse the computed answer (still paid)."""

    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._items: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> dict | None:
        item = self._items.get(key)
        return item[1] if item and item[0] > time.monotonic() else None

    def put(self, key: str, value: dict) -> None:
        self._items[key] = (time.monotonic() + self.ttl, value)


def build_requirements(offer: SellerOffer, asset: str, network: str) -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network=network,
        amount=str(offer.price_atomic),
        asset=asset,
        payTo=offer.pay_to,
        maxTimeoutSeconds=offer.max_timeout_seconds,
        extra={"name": AUSD_EIP712_NAME, "version": AUSD_EIP712_VERSION, "assetTransferMethod": "eip3009"},
    )


def _payment_required_response(resource: ResourceInfo, required: PaymentRequirements, error: str | None,
                               settlement: SettlementResponse | None = None) -> JSONResponse:
    body = PaymentRequired(error=error, resource=resource, accepts=[required])
    headers = {HEADER_PAYMENT_REQUIRED: encode_header(body)}
    if settlement is not None:
        headers[HEADER_PAYMENT_RESPONSE] = encode_header(settlement)
    return JSONResponse(status_code=402, content=json.loads(body.model_dump_json(exclude_none=True)), headers=headers)


def render_json_body(data: dict) -> bytes | None:
    """The exact bytes FastAPI's JSONResponse would send, or None if `data` is not valid JSON (NaN, Decimal, ...)."""
    try:
        return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return None


def _not_charged_response(error: str) -> JSONResponse:
    """502: the seller could not produce the item, so the authorization is simply never settled."""
    return JSONResponse(status_code=502, content={"error": error, "charged": False})


def _settlement_unknown_response(exc: SettlementUnknown, network: str, payer: str | None) -> JSONResponse:
    """504: the settlement may still execute. The buyer must check authorizationState, not assume failure."""
    pending = SettlementResponse(success=False, transaction=exc.tx_hash, network=network, payer=payer,
                                 errorReason=exc.reason)
    return JSONResponse(
        status_code=504,
        content={"error": exc.reason, "charged": "unknown", "transaction": exc.tx_hash},
        headers={HEADER_PAYMENT_RESPONSE: encode_header(pending)},
    )


def make_paid_router(
    offer: SellerOffer,
    compute: ComputeFn,
    *,
    verifier=None,
    settler=None,
    asset: str | None = None,
    network: str = BASE_SEPOLIA_CAIP2,
    on_settled: OnSettled | None = None,
) -> APIRouter:
    """Create a FastAPI router that sells `compute(params)` at `offer.path` for `offer.price_atomic` AUSD.

    `verifier` / `settler` are injected by tests (local anvil chain); otherwise they are built lazily
    from settings on the first paid request.
    """
    router = APIRouter()
    cache = _TtlCache(offer.cache_ttl_seconds)
    deps: dict = {"verifier": verifier, "settler": settler, "asset": asset}

    def _ensure_deps() -> None:
        if deps["verifier"] is not None and deps["settler"] is not None and deps["asset"]:
            return
        from app.x402.wiring import (
            default_seller_dependencies,  # lazy: needs RPC + keys
        )

        built = default_seller_dependencies(network)
        deps["verifier"] = deps["verifier"] or built.verifier
        deps["settler"] = deps["settler"] or built.settler
        deps["asset"] = deps["asset"] or built.asset

    @router.get(offer.path)
    async def paid_endpoint(request: Request):
        params = dict(request.query_params)
        resource = ResourceInfo(url=str(request.url), description=f"{offer.description} (demo vendor)")
        token_address = deps["asset"] or asset or get_settings().ausd_address
        required = build_requirements(offer, token_address, network)

        header = request.headers.get(HEADER_PAYMENT_SIGNATURE)
        if not header:
            return _payment_required_response(resource, required, "payment required")

        try:
            payment = decode_header(header, PaymentPayload)
        except Exception as exc:  # noqa: BLE001 — malformed header
            return _payment_required_response(resource, required, f"malformed payment header: {exc}")

        await run_in_threadpool(_ensure_deps)
        required = build_requirements(offer, deps["asset"], network)
        verdict = await run_in_threadpool(deps["verifier"].verify, payment, required)
        if not verdict.is_valid:
            failed = SettlementResponse(success=False, network=network, payer=verdict.payer, errorReason=verdict.reason)
            return _payment_required_response(resource, required, verdict.reason, failed)

        cache_key = json.dumps(sorted(params.items()))
        data = cache.get(cache_key)
        freshly_computed = data is None
        if freshly_computed:
            data = await compute(params)
            if data.get("error"):
                # Never charge for a failed computation: the authorization is simply not settled.
                return _not_charged_response(data["error"])
        body = render_json_body(data)
        if body is None:
            log.error("%s produced a result that is not valid JSON; not charging", offer.seller_id)
            return _not_charged_response("seller produced a result that is not valid JSON")
        if freshly_computed:
            cache.put(cache_key, data)  # only renderable results are cached

        if not has_settlement_headroom(payment):
            expired = SettlementResponse(success=False, network=network, payer=verdict.payer,
                                         errorReason="authorization expired during compute")
            return _payment_required_response(resource, required, expired.errorReason, expired)

        try:
            settlement: SettlementResponse = await run_in_threadpool(deps["settler"].settle, payment, required)
        except SettlementUnknown as exc:
            log.warning("%s settlement outcome unknown: %s (tx %s)", offer.seller_id, exc.reason, exc.tx_hash)
            return _settlement_unknown_response(exc, network, verdict.payer)
        if not settlement.success:
            return _payment_required_response(resource, required, settlement.errorReason, settlement)

        if on_settled is not None:
            try:
                on_settled(offer, payment, settlement, params)
            except Exception:  # noqa: BLE001 — logging must never break a paid response
                log.exception("on_settled hook failed")

        return Response(content=body, media_type="application/json",
                        headers={HEADER_PAYMENT_RESPONSE: encode_header(settlement)})

    return router
