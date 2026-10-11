"""
PocketDesk backend entry point: `create_app()` builds the FastAPI application.

    uvicorn app.main:app                 (or: uv run python -m scripts.run_server)

What the app contains:

    /api/*        the dashboard API (docs/API.md), one router per area in `app/api/`
    /sell/*       the six DEMO VENDORS (x402 paid endpoints, testnet AUSD) from `app/sellers/routers.py`

Startup (FastAPI lifespan, or lazily on the first request when no lifespan runs):
    1. build the keeper `Runtime` (chain clients, relayer, agents, breaker, event bus, alerts, db)
       unless one was injected — tests inject an anvil-backed runtime;
    2. unless `settings.skip_chain_checks`: `assert_ausd_matches_spec` (decimals + EIP-712 domain)
       and `verify_registry`; the outcome is reported by GET /api/health. A token that contradicts
       our signing assumptions stops the server (SPEC §1: refuse to start on mismatch).

Importing this module is cheap and offline: the module-level `app = create_app()` reads settings
but makes no RPC call; everything that talks to the chain happens at startup or per request.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    agents,
    ask,
    breaker,
    budget,
    catalog,
    faucet,
    feeds,
    health,
    session,
    state,
    stream,
    vouchers,
)
from app.api import settings as settings_api
from app.api.deps import AppContext, RuntimeFactory, ensure_started
from app.chain.ausd import AusdClient
from app.chain.web3client import TxSender, get_web3, redact_urls
from app.config import Settings, get_settings
from app.constants import (
    BASE_SEPOLIA_CAIP2,
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
)
from app.sellers.routers import build_seller_routers
from app.x402.settlement import FacilitatorSettler, SelfSettler
from app.x402.verify import PaymentVerifier

log = logging.getLogger(__name__)

APP_TITLE = "PocketDesk"
APP_VERSION = "0.1.0"
SETTLEMENT_FACILITATOR = "facilitator"
HTTP_INTERNAL_ERROR = 500

API_ROUTERS = (
    health.router,
    state.router,
    feeds.router,
    budget.router,
    vouchers.router,
    session.router,
    agents.router,
    ask.router,
    catalog.router,
    settings_api.router,
    stream.router,
    faucet.router,
    breaker.router,
)
X402_HEADERS = [HEADER_PAYMENT_REQUIRED, HEADER_PAYMENT_RESPONSE, HEADER_PAYMENT_SIGNATURE]


def default_runtime_factory(settings: Settings) -> Any:
    """Build the production keeper runtime (imported lazily: it is the heaviest module)."""
    from app.keeper.runtime import build_runtime

    return build_runtime(settings)


def seller_dependencies(settings: Settings, runtime: Any | None) -> dict[str, Any]:
    """
    Verifier, settler and token address for the demo vendors, built WITHOUT any RPC call.

    Rule: reuse the runtime's web3 + token when one is injected (tests, local anvil); otherwise
    connect lazily to `settings.rpc_url_base_sepolia`. Without a settler key the settler is left
    None and `make_paid_router` reports the missing key on the first PAID request only.
    """
    w3 = runtime.w3 if runtime is not None else get_web3(settings.rpc_url_base_sepolia)
    token = runtime.token if runtime is not None else AusdClient(w3, settings.ausd_address)
    settler: Any = None
    if settings.x402_settlement == SETTLEMENT_FACILITATOR:
        settler = FacilitatorSettler(settings.x402_facilitator_url, BASE_SEPOLIA_CAIP2)
    elif settings.seller_settler_private_key:
        settler = SelfSettler(token, TxSender(w3, settings.seller_settler_private_key, "seller-settler"),
                              BASE_SEPOLIA_CAIP2)
    return {
        "verifier": PaymentVerifier(token, settings.chain_id, BASE_SEPOLIA_CAIP2),
        "settler": settler,
        "asset": token.address,
    }


def mount_demo_vendors(app: FastAPI, settings: Settings, runtime: Any | None) -> list[str]:
    """Include one paid router per configured demo vendor; returns the mounted seller ids."""
    deps = seller_dependencies(settings, runtime)
    mounted = build_seller_routers(network=BASE_SEPOLIA_CAIP2, **deps)
    for seller in mounted:
        app.include_router(seller.router, tags=["demo vendors"])
    seller_ids = [seller.offer.seller_id for seller in mounted]
    app.state.demo_vendor_offers = [seller.offer for seller in mounted]
    log.info("demo vendors mounted: %s", ", ".join(seller_ids) or "none")
    return seller_ids


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: build the runtime + chain checks (refuse to start on a token mismatch). Shutdown: close it."""
    ctx: AppContext = app.state.ctx
    await run_in_threadpool(ensure_started, ctx)
    if ctx.fatal:
        raise RuntimeError(f"refusing to start: {ctx.chain_checks}")
    yield
    close = getattr(ctx.runtime, "aclose", None)
    if close is not None:
        await close()


async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Any unexpected error still answers `{detail}` (docs/API.md), with RPC URLs redacted."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=HTTP_INTERNAL_ERROR,
                        content={"detail": f"{type(exc).__name__}: {redact_urls(str(exc))}"})


def create_app(
    runtime: Any | None = None,
    settings: Settings | None = None,
    *,
    runtime_factory: RuntimeFactory | None = None,
) -> FastAPI:
    """
    The PocketDesk FastAPI app.

    `runtime`: an already-built keeper Runtime (tests); otherwise `runtime_factory(settings)`
    (default `build_runtime`) runs at startup. `settings`: defaults to the runtime's, else
    `get_settings()`.
    """
    if settings is None:
        settings = runtime.settings if runtime is not None else get_settings()
    app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)
    app.state.ctx = AppContext(settings=settings, runtime=runtime,
                               runtime_factory=runtime_factory or default_runtime_factory)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list(),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=X402_HEADERS,
    )
    app.add_exception_handler(Exception, unhandled_error)
    for router in API_ROUTERS:
        app.include_router(router)
    app.state.demo_vendors = mount_demo_vendors(app, settings, runtime)
    return app


app = create_app()
