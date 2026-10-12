"""
Mount the six demo vendors (testnet stand-ins that PocketDesk's agents really pay over x402).

    build_seller_routers() ──for each seller module──▶ make_paid_router(module.make_offer(), module.compute,
                                                                         on_settled=log the sale)

Each seller lives in `app/sellers/<seller_id>.py` and exposes `make_offer()` (price, path,
payout address) and `async compute(params)`. A seller whose payout address (`SELLER_PAYTO_*`)
is not configured is SKIPPED with a clear log line instead of breaking the whole server, so
the dashboard still runs while a vendor is being set up.

The on_settled hook records the SELLER-side view of a sale (who paid how much, tx hash) in the
log; the buyer-side record (Purchase row, receipts) is written by the agents.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter

from app.constants import BASE_SEPOLIA_CAIP2, ONE_AUSD
from app.sellers.expected_move import SellerConfigError
from app.x402.models import PaymentPayload, SettlementResponse
from app.x402.offer import SellerOffer
from app.x402.server import make_paid_router

log = logging.getLogger(__name__)

# Seller modules under app/sellers/, in catalog order.
DEMO_VENDOR_MODULES = (
    "expected_move",
    "pm_edge",
    "perps_crowding",
    "news_sentiment",
    "insider_cluster",
    "macro_pulse",
)
SELLERS_PACKAGE = "app.sellers"

SaleHook = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class MountedSeller:
    """One demo vendor that is being served: its offer and its router."""

    offer: SellerOffer
    router: APIRouter


def sale_record(offer: SellerOffer, payment: PaymentPayload, settlement: SettlementResponse,
                params: dict) -> dict[str, Any]:
    """The seller-side facts of one settled sale (no secrets: addresses, amounts, tx hash)."""
    return {
        "seller_id": offer.seller_id,
        "price_atomic": offer.price_atomic,
        "pay_to": offer.pay_to,
        "payer": settlement.payer,
        "tx_hash": settlement.transaction,
        "network": settlement.network,
        "params": dict(params),
    }


def make_sale_logger(on_sale: SaleHook | None = None) -> Callable[..., None]:
    """An `on_settled` hook: log every sale, then pass the record to `on_sale` if given."""

    def on_settled(offer: SellerOffer, payment: PaymentPayload, settlement: SettlementResponse,
                   params: dict) -> None:
        record = sale_record(offer, payment, settlement, params)
        log.info("demo vendor sale: %s sold %.4f AUSD to %s (tx %s)", offer.seller_id,
                 offer.price_atomic / ONE_AUSD, record["payer"], record["tx_hash"])
        if on_sale is not None:
            on_sale(record)

    return on_settled


def load_seller(seller_id: str) -> tuple[SellerOffer, Callable] | None:
    """(offer, compute) of one seller module, or None (logged) when its payout address is unset."""
    module = importlib.import_module(f"{SELLERS_PACKAGE}.{seller_id}")
    try:
        offer = module.make_offer()
    except SellerConfigError as exc:
        log.warning("demo vendor %s NOT mounted: %s", seller_id, exc)
        return None
    return offer, module.compute


def build_seller_routers(
    *,
    verifier: Any = None,
    settler: Any = None,
    asset: str | None = None,
    network: str = BASE_SEPOLIA_CAIP2,
    on_sale: SaleHook | None = None,
    seller_ids: tuple[str, ...] = DEMO_VENDOR_MODULES,
) -> list[MountedSeller]:
    """
    One paid x402 router per configured demo vendor.

    `verifier` / `settler` / `asset` are passed to `make_paid_router`; left as None they are built
    lazily from settings on the first PAID request, so building the routers needs no RPC.
    """
    mounted: list[MountedSeller] = []
    hook = make_sale_logger(on_sale)
    for seller_id in seller_ids:
        loaded = load_seller(seller_id)
        if loaded is None:
            continue
        offer, compute = loaded
        router = make_paid_router(offer, compute, verifier=verifier, settler=settler, asset=asset,
                                  network=network, on_settled=hook)
        mounted.append(MountedSeller(offer=offer, router=router))
    return mounted
