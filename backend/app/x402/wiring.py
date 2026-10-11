"""
Builds the real (Base Sepolia) seller dependencies from settings: the AUSD client,
the payment verifier and the settler (self-settlement or facilitator).

Kept separate from server.py so that tests can inject local-chain versions and the
server module never needs RPC access at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.chain.ausd import AusdClient
from app.chain.web3client import TxSender, get_web3
from app.config import get_settings
from app.x402.settlement import FacilitatorSettler, SelfSettler
from app.x402.verify import PaymentVerifier


@dataclass(frozen=True)
class SellerDependencies:
    verifier: PaymentVerifier
    settler: SelfSettler | FacilitatorSettler
    asset: str


@lru_cache
def default_seller_dependencies(network: str) -> SellerDependencies:
    settings = get_settings()
    w3 = get_web3()
    token = AusdClient(w3, settings.ausd_address)
    verifier = PaymentVerifier(token, settings.chain_id, network)
    if settings.x402_settlement == "facilitator":
        settler = FacilitatorSettler(settings.x402_facilitator_url, network)
    else:
        settler = SelfSettler(token, TxSender(w3, settings.seller_settler_private_key, "seller-settler"), network)
    return SellerDependencies(verifier=verifier, settler=settler, asset=token.address)
