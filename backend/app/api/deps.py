"""
Shared plumbing for the API routers: the app context, the keeper runtime, owners and errors.

    create_app() ──stores──▶ app.state.ctx = AppContext(settings, runtime | factory)
    any handler  ──Depends(get_runtime)──▶ ensure_started(ctx) ──▶ the keeper Runtime

`ensure_started` is idempotent. It runs once at server startup (from the FastAPI lifespan in
`main.py`) and again lazily on the first request when the app is driven without a lifespan
(e.g. `httpx.ASGITransport` in tests). It builds the runtime if none was injected and runs the
on-chain startup checks (AUSD decimals + EIP-712 domain, token registry) unless
`settings.skip_chain_checks`. Nothing here touches the network at import time.

Error rule (docs/API.md "Errors"): every failure leaves as HTTP 4xx/5xx with `{detail: str}`.
`api_errors()` maps the exceptions the chain and keeper layers raise:

    TxRevertedError                    400  "transaction reverted: <reason>"   (nothing moved)
    ValueError / LookupError           400  bad input or unknown name
    PermissionError                    403
    other RuntimeError                 409  the action conflicts with the current state
    ConnectionError / TimeoutError     502  an upstream (RPC, data source) failed
    TxPendingError                     504  broadcast but no receipt yet (tx hash in detail)
    any other Exception                500  "<ErrorType>: <message>" (logged with its traceback)

The last row matters for the dashboard: an HTTPException is answered INSIDE the CORS middleware,
so the browser can read the `{detail}`; an exception that escapes to the app-wide handler is
answered outside it, without CORS headers, and the browser only sees "Failed to fetch".

RPC URLs are redacted from every message (they may embed API keys); private keys never
appear in any message.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from eth_utils import is_address, to_checksum_address
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.chain.ausd import AusdSpecMismatch, assert_ausd_matches_spec
from app.chain.tokens import verify_registry
from app.chain.web3client import TxPendingError, TxRevertedError, redact_urls
from app.config import Settings
from app.db import load_api_settings, session_scope

log = logging.getLogger(__name__)

CHAIN_CHECKS_NOT_RUN = "not run"
CHAIN_CHECKS_PASSED = "passed"
CHAIN_CHECKS_SKIPPED = "skipped"
CHAIN_CHECKS_FAILED_PREFIX = "failed: "

HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_INTERNAL_ERROR = 500
HTTP_BAD_GATEWAY = 502
HTTP_UNAVAILABLE = 503
HTTP_GATEWAY_TIMEOUT = 504

RuntimeFactory = Callable[[Settings], Any]


# ─── app context ─────────────────────────────────────────────────────────────

@dataclass
class AppContext:
    """Everything the routers share: settings, the keeper runtime and the startup outcome."""

    settings: Settings
    runtime: Any | None = None
    runtime_factory: RuntimeFactory | None = None
    chain_checks: str = CHAIN_CHECKS_NOT_RUN
    startup_error: str | None = None     # why the runtime could not be built (reads still work)
    fatal: bool = False                  # the AUSD token contradicts our signing assumptions
    started: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def healthy(self) -> bool:
        """Rule: healthy = the runtime exists and the chain checks did not fail."""
        return self.startup_error is None and not self.chain_checks.startswith(CHAIN_CHECKS_FAILED_PREFIX)


def app_context(request: Request) -> AppContext:
    """The AppContext `create_app` stored on the application."""
    return request.app.state.ctx


def run_chain_checks(ctx: AppContext) -> None:
    """
    Startup safety check (SPEC §1, §9): the token's decimals and EIP-712 domain must match what
    our signers assume, and every token-registry entry is verified on-chain (failures disable
    the entry). A mismatch marks the context fatal; an unreachable RPC is reported as failed.
    """
    settings = ctx.settings
    if settings.skip_chain_checks:
        ctx.chain_checks = CHAIN_CHECKS_SKIPPED
        return
    w3 = ctx.runtime.w3
    try:
        assert_ausd_matches_spec(w3, settings.ausd_address, settings.chain_id)
        verify_registry(w3, ctx.runtime.registry)
    except AusdSpecMismatch as exc:
        ctx.fatal = True
        ctx.chain_checks = CHAIN_CHECKS_FAILED_PREFIX + str(exc)
    except Exception as exc:  # noqa: BLE001 — RPC down, wrong address, ...: report, keep serving reads
        ctx.chain_checks = f"{CHAIN_CHECKS_FAILED_PREFIX}{type(exc).__name__}: {redact_urls(str(exc))}"
    else:
        ctx.chain_checks = CHAIN_CHECKS_PASSED
    log.info("chain checks: %s", ctx.chain_checks)


def ensure_started(ctx: AppContext) -> None:
    """Build the runtime (if not injected) and run the chain checks, exactly once."""
    with ctx._lock:
        if ctx.started:
            return
        ctx.started = True
        if ctx.runtime is None:
            try:
                ctx.runtime = ctx.runtime_factory(ctx.settings) if ctx.runtime_factory else None
            except Exception as exc:  # noqa: BLE001 — e.g. a missing key: report it, serve health
                ctx.startup_error = f"{type(exc).__name__}: {redact_urls(str(exc))}"
                log.error("keeper runtime could not be built: %s", ctx.startup_error)
                return
        if ctx.runtime is None:
            ctx.startup_error = "no keeper runtime configured"
            return
        run_chain_checks(ctx)


def get_runtime(request: Request) -> Any:
    """FastAPI dependency: the started keeper Runtime, or 503 with the reason it is unavailable."""
    ctx = app_context(request)
    ensure_started(ctx)
    if ctx.startup_error is not None:
        raise HTTPException(HTTP_UNAVAILABLE, f"backend not ready: {ctx.startup_error}")
    if ctx.fatal:
        raise HTTPException(HTTP_UNAVAILABLE, f"refusing to operate: {ctx.chain_checks}")
    return ctx.runtime


def get_settings_dep(request: Request) -> Settings:
    """FastAPI dependency: the Settings this app was created with."""
    return app_context(request).settings


# ─── addresses and owners ────────────────────────────────────────────────────

def checksum(address: str | None, field_name: str = "address") -> str:
    """A checksummed address, or 400 naming the field when it is missing or invalid."""
    if not address or not is_address(address):
        raise HTTPException(HTTP_BAD_REQUEST, f"{field_name} is not a valid address: {address!r}")
    return to_checksum_address(address)


def engine_of(runtime: Any) -> Any:
    """The runtime's database engine (None = the process-wide engine from `db.init_db`)."""
    return getattr(runtime, "engine", None)


def stored_owner(runtime: Any) -> str | None:
    """The owner saved in the user's Settings (set when a budget is opened)."""
    with session_scope(engine_of(runtime)) as session:
        return load_api_settings(session).get("owner")


def resolve_owner(runtime: Any, owner: str | None) -> str:
    """
    Rule (docs/API.md): an endpoint uses the `owner` it is given; otherwise the runtime's owner;
    otherwise the owner stored in Settings. None of them → 400.
    """
    candidate = owner or getattr(runtime, "owner", None) or stored_owner(runtime)
    if not candidate:
        raise HTTPException(HTTP_BAD_REQUEST, "No budget owner yet — fund a budget on F2 FUND "
                                            "(or click DEMO FUND), or pass `owner`")
    return checksum(candidate, "owner")


def agent_names(runtime: Any) -> list[str]:
    """Names of the runtime's agents, whether it keeps them as a dict or as a list of objects."""
    agents = getattr(runtime, "agents", None) or {}
    if isinstance(agents, dict):
        return list(agents)
    return [getattr(agent, "name", str(agent)) for agent in agents]


# ─── transactions and errors ─────────────────────────────────────────────────

def tx_hash_of(result: Any) -> str:
    """The 0x tx hash of whatever a chain helper returned (TxResult, dict, str or HexBytes)."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result["tx_hash"])
    tx_hash = getattr(result, "tx_hash", None)
    if tx_hash is not None:
        return str(tx_hash)
    to_hex = getattr(result, "to_0x_hex", None)
    if to_hex is not None:
        return to_hex()
    raise TypeError(f"cannot read a tx hash from {type(result).__name__}")


@contextmanager
def api_errors() -> Iterator[None]:
    """Translate chain / keeper exceptions into `{detail}` HTTP errors (table in the module docstring)."""
    try:
        yield
    except HTTPException:
        raise
    except TxRevertedError as exc:
        suffix = f" (tx {exc.tx_hash})" if exc.tx_hash else ""
        raise HTTPException(HTTP_BAD_REQUEST, f"transaction reverted: {redact_urls(exc.reason)}{suffix}") from exc
    except TxPendingError as exc:
        raise HTTPException(
            HTTP_GATEWAY_TIMEOUT, f"no receipt yet for tx {exc.tx_hash}: {redact_urls(exc.reason)}"
        ) from exc
    except PermissionError as exc:
        raise HTTPException(HTTP_FORBIDDEN, redact_urls(str(exc))) from exc
    except (ValueError, LookupError) as exc:
        raise HTTPException(HTTP_BAD_REQUEST, redact_urls(str(exc))) from exc
    except (ConnectionError, TimeoutError) as exc:
        raise HTTPException(HTTP_BAD_GATEWAY, f"upstream failed: {redact_urls(str(exc))}") from exc
    except RuntimeError as exc:
        raise HTTPException(HTTP_CONFLICT, redact_urls(str(exc))) from exc
    except Exception as exc:  # noqa: BLE001 — anything else still answers a readable {detail}
        log.exception("unexpected error in an API handler")
        raise HTTPException(HTTP_INTERNAL_ERROR, readable_error(exc)) from exc


def readable_error(exc: BaseException) -> str:
    """'<ErrorType>: <message>' with RPC URLs redacted (just the type when the message is empty)."""
    message = redact_urls(str(exc)).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


async def call_blocking(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking (web3 / sqlite) call in the thread pool with `api_errors()` translation."""
    with api_errors():
        return await run_in_threadpool(fn, *args, **kwargs)
