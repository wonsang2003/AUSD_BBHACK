"""
`SellerOffer` — what a seller charges for one endpoint, in AUSD.

Kept in its own tiny module so seller modules (app/sellers/*) can declare their
offer without importing the whole x402 server machinery.
"""

from dataclasses import dataclass

from app.constants import DEFAULT_PAYMENT_TIMEOUT_SECONDS, ONE_AUSD


@dataclass(frozen=True)
class SellerOffer:
    seller_id: str      # stable id, e.g. "expected_move"
    path: str           # HTTP path, e.g. "/sell/expected-move"
    description: str    # shown to buyers in the 402 `resource.description`
    price_atomic: int   # price in AUSD atomic units (6 decimals): 50_000 == 0.05 AUSD
    pay_to: str         # seller payout address (receives the AUSD)
    max_timeout_seconds: int = DEFAULT_PAYMENT_TIMEOUT_SECONDS
    cache_ttl_seconds: int = 120

    @property
    def price_ausd(self) -> float:
        return self.price_atomic / ONE_AUSD
