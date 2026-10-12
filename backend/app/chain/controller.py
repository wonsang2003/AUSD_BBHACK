"""
BudgetController client: the backend's typed wrapper around `contracts/src/BudgetController.sol`.

Where this fits in the PocketDesk flow:
    * The web app has the user sign a `Permit` (ERC-2612) and a `BudgetConfig` (EIP-712); the
      keeper (relayer) submits both with `open_budget`. In "maximum safety" mode the user signs
      a config plus daily vouchers instead, and the keeper uses `open_voucher_budget`.
    * Every session the keeper tops agents up with `recharge` (permit mode) or redeems the day's
      voucher with `redeem_voucher` (voucher mode, split across agents in one transaction).
    * The dashboard and the allocator read state through `budget`, `agent`, `remaining_today`,
      `agent_remaining_today`, `get_agents` and `cfg_nonce`.
    * Revoking a budget is a permit with value 0 that goes straight to the token:
      `submit_permit` (module-level, because it calls the token, not the controller).

Only the keeper ever sends transactions here (it is the only wallet with gas). A client built
without a keeper is read-only; its write methods raise `ReadOnlyClientError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from eth_utils import to_checksum_address
from web3 import Web3

from app.chain import eip712
from app.chain.artifacts import load_artifact
from app.chain.ausd import AusdClient
from app.chain.web3client import TxResult, TxSender

CONTRACT_NAME = "BudgetController"
BYTES32_LENGTH = 32

SignatureLike = bytes | str | eip712.Signature


class BudgetMode(IntEnum):
    """How a budget is funded (mirrors `BudgetController.BudgetMode`)."""

    PERMIT = 0   # standing ERC-2612 allowance, topped up with recharge()
    VOUCHER = 1  # one signed ERC-3009 voucher per UTC day, pulled with redeemVoucher()


@dataclass(frozen=True)
class BudgetView:
    """One owner's budget as stored on-chain. `spent_today` is the raw value for `day_index`."""

    daily_cap: int
    spent_today: int
    day_index: int
    expiry: int
    paused: bool
    exists: bool
    cfg_nonce: int
    mode: BudgetMode


@dataclass(frozen=True)
class AgentView:
    """One agent wallet registered under an owner (sector decoded from bytes32 to text)."""

    active: bool
    daily_cap: int
    spent_today: int
    day_index: int
    sector: str


class ReadOnlyClientError(RuntimeError):
    """A write method was called on a BudgetControllerClient built without a keeper TxSender."""


# ─── revert reasons ──────────────────────────────────────────────────────────

def _error_selectors(abi: list[dict]) -> dict[str, str]:
    """4-byte selector (0x-hex) → custom error name, for every `error` in the ABI."""
    selectors = {}
    for entry in abi:
        if entry.get("type") == "error":
            signature = f"{entry['name']}({','.join(arg['type'] for arg in entry['inputs'])})"
            selectors["0x" + Web3.keccak(text=signature)[:4].hex()] = entry["name"]
    return selectors


def controller_error_name(revert_reason: str) -> str | None:
    """Name of the BudgetController custom error inside a revert reason (e.g. "DailyCapExceeded"), or None.

    `TxRevertedError` carries the raw selector ("custom error 0x5f3c2b1a..."); this turns it into the
    readable name the dashboard and the keeper's logs show.
    """
    abi, _ = load_artifact(CONTRACT_NAME)
    reason = revert_reason.lower()
    for selector, name in _error_selectors(abi).items():
        if selector in reason:
            return name
    return None


# ─── argument conversion ─────────────────────────────────────────────────────

def signature_bytes(signature: SignatureLike) -> bytes:
    """Accept a 65-byte signature as bytes, 0x-hex or `eip712.Signature`; return raw bytes."""
    if isinstance(signature, eip712.Signature):
        return signature.signature
    if isinstance(signature, str):
        return bytes.fromhex(signature.removeprefix("0x"))
    return bytes(signature)


def bytes32(value: bytes | str) -> bytes:
    """Accept a bytes32 as raw bytes or 0x-hex; reject anything that is not exactly 32 bytes."""
    raw = bytes.fromhex(value.removeprefix("0x")) if isinstance(value, str) else bytes(value)
    if len(raw) != BYTES32_LENGTH:
        raise ValueError(f"expected 32 bytes, got {len(raw)}")
    return raw


# ─── the client ──────────────────────────────────────────────────────────────

class BudgetControllerClient:
    """Reads + keeper transactions for one deployed BudgetController."""

    def __init__(self, w3: Web3, address: str, keeper: TxSender | None = None):
        abi, _ = load_artifact(CONTRACT_NAME)
        self.w3 = w3
        self.address = to_checksum_address(address)
        self.keeper = keeper
        self.contract = w3.eth.contract(address=self.address, abi=abi)

    # ─── keeper transactions ─────────────────────────────────────────────
    def _send(self, contract_fn) -> TxResult:
        """Send one call from the keeper. Raises TxRevertedError on revert (nothing moved)."""
        if self.keeper is None:
            raise ReadOnlyClientError("BudgetControllerClient has no keeper: it can only read")
        return self.keeper.send(contract_fn)

    def open_budget(
        self,
        owner: str,
        permit_value: int,
        permit_deadline: int,
        permit_sig: eip712.Signature,
        config: eip712.BudgetConfig,
        config_sig: bytes | str,
    ) -> TxResult:
        """Submit the owner's permit + signed config in one `openBudget` transaction (PERMIT mode)."""
        return self._send(self.contract.functions.openBudget(
            to_checksum_address(owner),
            int(permit_value),
            int(permit_deadline),
            permit_sig.v,
            permit_sig.r,
            permit_sig.s,
            config.to_contract_tuple(),
            signature_bytes(config_sig),
        ))

    def open_voucher_budget(self, config: eip712.BudgetConfig, config_sig: bytes | str) -> TxResult:
        """Open the budget in VOUCHER mode: signed config only, no permit and no allowance."""
        return self._send(self.contract.functions.openVoucherBudget(
            config.to_contract_tuple(), signature_bytes(config_sig)
        ))

    def update_config_with_sig(self, config: eip712.BudgetConfig, sig: bytes | str) -> TxResult:
        """Change caps / expiry / agents with a newly signed config (nonce = current cfg_nonce)."""
        return self._send(self.contract.functions.updateConfigWithSig(config.to_contract_tuple(), signature_bytes(sig)))

    def recharge(self, owner: str, agent: str, amount: int) -> TxResult:
        """Move `amount` atomic AUSD owner → agent, within today's budget and agent caps (PERMIT mode)."""
        return self._send(self.contract.functions.recharge(
            to_checksum_address(owner), to_checksum_address(agent), int(amount)
        ))

    def redeem_voucher(
        self,
        owner: str,
        value: int,
        valid_after: int,
        valid_before: int,
        nonce: bytes | str,
        signature: SignatureLike,
        agent_addrs: list[str],
        amounts: list[int],
    ) -> TxResult:
        """Pull today's voucher (owner → controller) and split it: agent_addrs[i] gets amounts[i].

        Rule (enforced on-chain): sum(amounts) == value, the window is exactly today's UTC day,
        and the usual daily / per-agent caps hold.
        """
        return self._send(self.contract.functions.redeemVoucher(
            to_checksum_address(owner),
            int(value),
            int(valid_after),
            int(valid_before),
            bytes32(nonce),
            signature_bytes(signature),
            [to_checksum_address(agent) for agent in agent_addrs],
            [int(amount) for amount in amounts],
        ))

    def set_paused_with_sig(self, owner: str, paused: bool, deadline: int, sig: bytes | str) -> TxResult:
        """Gasless pause / resume: submit the owner's `SetPaused` signature (nonce = current cfg_nonce)."""
        return self._send(self.contract.functions.setPausedWithSig(
            to_checksum_address(owner), bool(paused), int(deadline), signature_bytes(sig)
        ))

    # ─── reads ───────────────────────────────────────────────────────────
    def budget(self, owner: str) -> BudgetView:
        """The stored budget. Use `remaining_today` for a figure that already applies the day rollover."""
        daily_cap, spent_today, day_index, expiry, paused, exists, cfg_nonce, mode = (
            self.contract.functions.budgets(to_checksum_address(owner)).call()
        )
        return BudgetView(
            daily_cap=daily_cap,
            spent_today=spent_today,
            day_index=day_index,
            expiry=expiry,
            paused=paused,
            exists=exists,
            cfg_nonce=cfg_nonce,
            mode=BudgetMode(mode),
        )

    def agent(self, owner: str, agent: str) -> AgentView:
        """One agent's stored settings and raw spend counter."""
        active, daily_cap, spent_today, day_index, sector = self.contract.functions.agents(
            to_checksum_address(owner), to_checksum_address(agent)
        ).call()
        return AgentView(
            active=active,
            daily_cap=daily_cap,
            spent_today=spent_today,
            day_index=day_index,
            sector=eip712.bytes32_to_sector(sector),
        )

    def remaining_today(self, owner: str) -> int:
        """max(0, dailyCap − spent today); 0 if the budget is missing, paused or expired."""
        return self.contract.functions.remainingToday(to_checksum_address(owner)).call()

    def agent_remaining_today(self, owner: str, agent: str) -> int:
        """min(agent headroom, budget headroom) — the most `recharge(owner, agent, ·)` accepts now."""
        return self.contract.functions.agentRemainingToday(
            to_checksum_address(owner), to_checksum_address(agent)
        ).call()

    def get_agents(self, owner: str) -> list[str]:
        """Agents listed in the owner's current config (checksummed)."""
        return [to_checksum_address(a) for a in self.contract.functions.getAgents(to_checksum_address(owner)).call()]

    def cfg_nonce(self, owner: str) -> int:
        """Nonce the owner's next BudgetConfig or SetPaused signature must carry."""
        return self.contract.functions.configNonce(to_checksum_address(owner)).call()

    def domain_separator(self) -> str:
        """EIP-712 domain separator ("PocketDesk", "1", chainId, this contract) as 0x-hex."""
        return "0x" + self.contract.functions.domainSeparator().call().hex()

    def hash_budget_config(self, config: eip712.BudgetConfig) -> str:
        """The contract's own EIP-712 digest of `config` (for cross-checking the Python/TS signers)."""
        return "0x" + self.contract.functions.hashBudgetConfig(config.to_contract_tuple()).call().hex()

    def voucher_nonce(self, owner: str, day_index: int, salt: bytes | str) -> str:
        """keccak256(abi.encode("pocketdesk-voucher", owner, dayIndex, salt)) as computed on-chain."""
        nonce = self.contract.functions.voucherNonce(to_checksum_address(owner), int(day_index), bytes32(salt)).call()
        return "0x" + nonce.hex()


# ─── revoking: a permit straight to the token ────────────────────────────────

def submit_permit(
    keeper: TxSender,
    token: AusdClient,
    owner: str,
    spender: str,
    value: int,
    deadline: int,
    sig: eip712.Signature,
) -> TxResult:
    """Submit an owner-signed ERC-2612 permit to the token (the keeper pays gas).

    Used to revoke a budget: the owner signs a permit with value 0 for the controller,
    which sets the allowance to 0, so every later `recharge` reverts.
    """
    return keeper.send(token.permit_fn(owner, spender, value, deadline, sig.v, sig.r, sig.s))
