"""
Daily vouchers ("Maximum safety" mode, docs/SPEC.md §9 Update #1 D): day arithmetic and builders.

Instead of a standing allowance, the user signs one ERC-3009 `ReceiveWithAuthorization` per UTC
day, each paying that day's budget to the BudgetController:

    to          = BudgetController (only it may submit a ReceiveWithAuthorization)
    validAfter  = dayStart − 1      ERC-3009's window is strict (validAfter < now < validBefore),
    validBefore = dayEnd            so the voucher is usable exactly during its UTC day
    nonce       = keccak256(abi.encode("pocketdesk-voucher", owner, dayIndex, salt))

Each day the keeper calls `BudgetControllerClient.redeem_voucher`, which pulls the voucher and
splits it across agents in one transaction. A future day is killed by cancelling its nonce on the
token (`cancelAuthorization`), so at most one day's value is ever exposed.

This module only builds unsigned typed data; signing happens in the user's wallet (or in tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from app.chain import eip712

SECONDS_PER_DAY = 86_400
VOUCHER_NONCE_TAG = "pocketdesk-voucher"
DAYS_PER_WEEK = 7


class VoucherStatus:
    """Lifecycle of one stored voucher."""

    PENDING = "pending"      # signed, waiting for its UTC day
    REDEEMED = "redeemed"    # redeemVoucher succeeded
    CANCELLED = "cancelled"  # cancelAuthorization sent for its nonce
    FAILED = "failed"        # redeem reverted / was rejected

    ALL = (PENDING, REDEEMED, CANCELLED, FAILED)


class VoucherNonceSource(Protocol):
    """Anything that can compute a voucher nonce — `BudgetControllerClient` does, on-chain."""

    def voucher_nonce(self, owner: str, day_index: int, salt: bytes | str) -> str: ...


# ─── day arithmetic ──────────────────────────────────────────────────────────

def day_index(timestamp: int) -> int:
    """UTC day number, as the contract computes it: floor(timestamp / 86400)."""
    return int(timestamp) // SECONDS_PER_DAY


def day_bounds(day: int) -> tuple[int, int]:
    """(valid_after, valid_before) of the voucher for UTC day `day`: (dayStart − 1, dayEnd)."""
    day_start = int(day) * SECONDS_PER_DAY
    return day_start - 1, day_start + SECONDS_PER_DAY


def compute_voucher_nonce(owner: str, day: int, salt: bytes | str) -> str:
    """Off-chain twin of `BudgetController.voucherNonce`: keccak256(abi.encode(tag, owner, day, salt))."""
    salt_bytes = bytes.fromhex(salt.removeprefix("0x")) if isinstance(salt, str) else bytes(salt)
    encoded = abi_encode(
        ["string", "address", "uint256", "bytes32"],
        [VOUCHER_NONCE_TAG, to_checksum_address(owner), int(day), salt_bytes],
    )
    return "0x" + keccak(encoded).hex()


# ─── builders ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DailyVoucher:
    """One unsigned voucher: the day it pays for, its authorization fields and the typed data to sign."""

    day_index: int
    authorization: eip712.Authorization
    typed_data: dict

    def to_api(self) -> dict:
        """Shape of one entry in POST /api/vouchers/prepare (see web/lib/types.ts PreparedVoucher)."""
        return {"day_index": self.day_index, "typed_data": self.typed_data}


def voucher_authorization(
    *, controller_address: str, owner: str, day: int, value: int, nonce: str
) -> eip712.Authorization:
    """The six ERC-3009 fields of the voucher for UTC day `day` (owner → controller)."""
    valid_after, valid_before = day_bounds(day)
    return eip712.Authorization(
        from_address=to_checksum_address(owner),
        to=to_checksum_address(controller_address),
        value=int(value),
        valid_after=valid_after,
        valid_before=valid_before,
        nonce=nonce,
    )


def build_voucher(
    *,
    chain_id: int,
    ausd_address: str,
    controller_address: str,
    owner: str,
    day_index: int,
    value: int,
    nonce: str,
) -> dict:
    """ReceiveWithAuthorization typed data (AUSD "Agora Dollar" domain) for one day's voucher."""
    auth = voucher_authorization(
        controller_address=controller_address, owner=owner, day=day_index, value=value, nonce=nonce
    )
    return eip712.build_receive_with_authorization(chain_id=chain_id, ausd_address=ausd_address, auth=auth)


def build_week(
    *,
    controller: VoucherNonceSource,
    chain_id: int,
    ausd_address: str,
    controller_address: str,
    owner: str,
    first_day_index: int,
    value: int,
    salt: bytes | str,
    days: int = DAYS_PER_WEEK,
) -> list[DailyVoucher]:
    """`days` consecutive daily vouchers starting at `first_day_index`, each for `value` atomic AUSD.

    Nonces come from `controller.voucher_nonce(owner, day, salt)` so they match the contract exactly.
    """
    vouchers = []
    for day in range(int(first_day_index), int(first_day_index) + days):
        nonce = controller.voucher_nonce(owner, day, salt)
        auth = voucher_authorization(
            controller_address=controller_address, owner=owner, day=day, value=value, nonce=nonce
        )
        typed_data = eip712.build_receive_with_authorization(chain_id=chain_id, ausd_address=ausd_address, auth=auth)
        vouchers.append(DailyVoucher(day_index=day, authorization=auth, typed_data=typed_data))
    return vouchers


def build_cancel(*, chain_id: int, ausd_address: str, owner: str, nonce: str) -> dict:
    """CancelAuthorization typed data that kills one (future) voucher; anyone may submit it to the token."""
    return eip712.build_cancel_authorization(
        chain_id=chain_id, ausd_address=ausd_address, authorizer=owner, nonce=nonce
    )
