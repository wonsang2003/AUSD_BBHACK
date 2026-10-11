"""
EIP-712 typed-data builders and signers for every signature PocketDesk uses.

There are exactly four kinds of signatures in the system:

    1. ERC-2612 `Permit`                 — user → lets BudgetController pull AUSD (funding)
    2. ERC-3009 `TransferWithAuthorization` — agent → pays a seller (spending, x402)
    3. ERC-3009 `ReceiveWithAuthorization`  — agent → pays an escrow contract (stretch)
    4. PocketDesk `BudgetConfig` / `SetPaused` — user → configures caps / pauses (gasless)

Each builder returns a plain EIP-712 "full message" dict:
    {"types": ..., "primaryType": ..., "domain": ..., "message": ...}
which is exactly what wallets (`eth_signTypedData_v4`), viem (`signTypedData`) and
`eth_account` all accept, so the same dict can be shown in the UI, signed in
Python, and verified in Solidity.

The web app mirrors these builders in `web/lib/eip712.ts`; the cross-language
test in `tests/test_eip712.py` pins the digests so the two can never drift.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

from app.constants import (
    AUSD_EIP712_NAME,
    AUSD_EIP712_VERSION,
    ERC3009_VALID_AFTER_SKEW_SECONDS,
    POCKETDESK_EIP712_NAME,
    POCKETDESK_EIP712_VERSION,
)

EIP712_DOMAIN_TYPE = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

# ─── type definitions (field order matters: it is part of the type hash) ──────

PERMIT_TYPE = [
    {"name": "owner", "type": "address"},
    {"name": "spender", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "nonce", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
]

ERC3009_AUTHORIZATION_FIELDS = [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]

CANCEL_AUTHORIZATION_TYPE = [
    {"name": "authorizer", "type": "address"},
    {"name": "nonce", "type": "bytes32"},
]

BUDGET_CONFIG_TYPE = [
    {"name": "owner", "type": "address"},
    {"name": "dailyCap", "type": "uint128"},
    {"name": "expiry", "type": "uint64"},
    {"name": "agentAddrs", "type": "address[]"},
    {"name": "agentCaps", "type": "uint128[]"},
    {"name": "sectors", "type": "bytes32[]"},
    {"name": "nonce", "type": "uint256"},
]

SET_PAUSED_TYPE = [
    {"name": "owner", "type": "address"},
    {"name": "paused", "type": "bool"},
    {"name": "nonce", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
]


# ─── domains ──────────────────────────────────────────────────────────────────

def ausd_domain(chain_id: int, ausd_address: str) -> dict:
    """Signing domain of the AUSD token. Note: name is "Agora Dollar", not "AUSD"."""
    return {
        "name": AUSD_EIP712_NAME,
        "version": AUSD_EIP712_VERSION,
        "chainId": chain_id,
        "verifyingContract": to_checksum_address(ausd_address),
    }


def pocketdesk_domain(chain_id: int, controller_address: str) -> dict:
    """Signing domain of our BudgetController contract."""
    return {
        "name": POCKETDESK_EIP712_NAME,
        "version": POCKETDESK_EIP712_VERSION,
        "chainId": chain_id,
        "verifyingContract": to_checksum_address(controller_address),
    }


def _full_message(primary_type: str, type_fields: list, domain: dict, message: dict) -> dict:
    return {
        "types": {"EIP712Domain": EIP712_DOMAIN_TYPE, primary_type: type_fields},
        "primaryType": primary_type,
        "domain": domain,
        "message": message,
    }


# ─── builders ────────────────────────────────────────────────────────────────

def build_permit(
    *,
    chain_id: int,
    ausd_address: str,
    owner: str,
    spender: str,
    value: int,
    nonce: int,
    deadline: int,
) -> dict:
    """ERC-2612 permit. Read `nonces(owner)` from the token right before calling this."""
    message = {
        "owner": to_checksum_address(owner),
        "spender": to_checksum_address(spender),
        "value": int(value),
        "nonce": int(nonce),
        "deadline": int(deadline),
    }
    return _full_message("Permit", PERMIT_TYPE, ausd_domain(chain_id, ausd_address), message)


@dataclass(frozen=True)
class Authorization:
    """The six fields of an ERC-3009 authorization (also the x402 `payload.authorization`)."""

    from_address: str
    to: str
    value: int
    valid_after: int
    valid_before: int
    nonce: str  # 0x-prefixed 32-byte hex string

    def to_message(self) -> dict:
        return {
            "from": to_checksum_address(self.from_address),
            "to": to_checksum_address(self.to),
            "value": int(self.value),
            "validAfter": int(self.valid_after),
            "validBefore": int(self.valid_before),
            "nonce": self.nonce,
        }

    def to_x402_json(self) -> dict:
        """x402 v2 puts integers as decimal strings inside `payload.authorization`."""
        message = self.to_message()
        return {
            "from": message["from"],
            "to": message["to"],
            "value": str(message["value"]),
            "validAfter": str(message["validAfter"]),
            "validBefore": str(message["validBefore"]),
            "nonce": message["nonce"],
        }

    @classmethod
    def from_x402_json(cls, data: dict) -> "Authorization":
        return cls(
            from_address=to_checksum_address(data["from"]),
            to=to_checksum_address(data["to"]),
            value=int(data["value"]),
            valid_after=int(data["validAfter"]),
            valid_before=int(data["validBefore"]),
            nonce=data["nonce"],
        )


def random_nonce() -> str:
    """ERC-3009 nonces are random bytes32 (not sequential), so payments can run in parallel."""
    return "0x" + os.urandom(32).hex()


def new_authorization(
    *,
    from_address: str,
    to: str,
    value: int,
    timeout_seconds: int,
    now: int | None = None,
    nonce: str | None = None,
) -> Authorization:
    """Create a fresh authorization valid from (now - 30s) to (now + timeout_seconds)."""
    now = int(time.time()) if now is None else now
    return Authorization(
        from_address=to_checksum_address(from_address),
        to=to_checksum_address(to),
        value=int(value),
        valid_after=now - ERC3009_VALID_AFTER_SKEW_SECONDS,
        valid_before=now + int(timeout_seconds),
        nonce=nonce or random_nonce(),
    )


def build_transfer_with_authorization(*, chain_id: int, ausd_address: str, auth: Authorization) -> dict:
    """ERC-3009 TransferWithAuthorization — anyone (relayer / seller) may submit it."""
    return _full_message(
        "TransferWithAuthorization",
        ERC3009_AUTHORIZATION_FIELDS,
        ausd_domain(chain_id, ausd_address),
        auth.to_message(),
    )


def build_receive_with_authorization(*, chain_id: int, ausd_address: str, auth: Authorization) -> dict:
    """ERC-3009 ReceiveWithAuthorization — only `to` may submit it (used for escrow contracts)."""
    return _full_message(
        "ReceiveWithAuthorization",
        ERC3009_AUTHORIZATION_FIELDS,
        ausd_domain(chain_id, ausd_address),
        auth.to_message(),
    )


def build_cancel_authorization(*, chain_id: int, ausd_address: str, authorizer: str, nonce: str) -> dict:
    message = {"authorizer": to_checksum_address(authorizer), "nonce": nonce}
    return _full_message("CancelAuthorization", CANCEL_AUTHORIZATION_TYPE, ausd_domain(chain_id, ausd_address), message)


def sector_to_bytes32(sector: str) -> str:
    """Encode a short sector label (e.g. "OptionsEvent") as a right-padded bytes32 hex string."""
    raw = sector.encode("utf-8")
    if len(raw) > 32:
        raise ValueError(f"sector label longer than 32 bytes: {sector!r}")
    return "0x" + raw.ljust(32, b"\x00").hex()


def bytes32_to_sector(value: bytes | str) -> str:
    raw = bytes.fromhex(value[2:]) if isinstance(value, str) else value
    return raw.rstrip(b"\x00").decode("utf-8")


@dataclass(frozen=True)
class BudgetConfig:
    """Mirror of the Solidity `BudgetConfig` struct."""

    owner: str
    daily_cap: int
    expiry: int
    agent_addrs: list[str]
    agent_caps: list[int]
    sectors: list[str]  # bytes32 hex strings (see sector_to_bytes32)
    nonce: int

    def to_message(self) -> dict:
        return {
            "owner": to_checksum_address(self.owner),
            "dailyCap": int(self.daily_cap),
            "expiry": int(self.expiry),
            "agentAddrs": [to_checksum_address(a) for a in self.agent_addrs],
            "agentCaps": [int(c) for c in self.agent_caps],
            "sectors": list(self.sectors),
            "nonce": int(self.nonce),
        }

    def to_contract_tuple(self) -> tuple:
        """Argument shape for web3.py when calling `openBudget(..., cfg, sig)`."""
        m = self.to_message()
        return (
            m["owner"],
            m["dailyCap"],
            m["expiry"],
            m["agentAddrs"],
            m["agentCaps"],
            [bytes.fromhex(s[2:]) for s in m["sectors"]],
            m["nonce"],
        )


def build_budget_config(*, chain_id: int, controller_address: str, config: BudgetConfig) -> dict:
    return _full_message(
        "BudgetConfig", BUDGET_CONFIG_TYPE, pocketdesk_domain(chain_id, controller_address), config.to_message()
    )


def build_set_paused(
    *, chain_id: int, controller_address: str, owner: str, paused: bool, nonce: int, deadline: int
) -> dict:
    """Gasless pause/resume: the user signs, the keeper submits `setPausedWithSig`."""
    message = {
        "owner": to_checksum_address(owner),
        "paused": bool(paused),
        "nonce": int(nonce),
        "deadline": int(deadline),
    }
    return _full_message("SetPaused", SET_PAUSED_TYPE, pocketdesk_domain(chain_id, controller_address), message)


# ─── signing / recovery ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Signature:
    v: int
    r: bytes
    s: bytes
    signature: bytes  # 65-byte r || s || v

    @property
    def hex(self) -> str:
        return "0x" + self.signature.hex()


def sign_typed_data(private_key: str, full_message: dict) -> Signature:
    signed = Account.sign_message(encode_typed_data(full_message=full_message), private_key)
    return Signature(
        v=signed.v,
        r=signed.r.to_bytes(32, "big"),
        s=signed.s.to_bytes(32, "big"),
        signature=bytes(signed.signature),
    )


def recover_typed_data_signer(full_message: dict, signature: bytes | str) -> str:
    sig = bytes.fromhex(signature[2:]) if isinstance(signature, str) else signature
    return Account.recover_message(encode_typed_data(full_message=full_message), signature=sig)


def typed_data_digest(full_message: dict) -> str:
    """The 32-byte EIP-712 digest ("\\x19\\x01" ‖ domainSeparator ‖ structHash) as 0x-hex."""
    signable = encode_typed_data(full_message=full_message)
    return "0x" + keccak(b"\x19" + signable.version + signable.header + signable.body).hex()
