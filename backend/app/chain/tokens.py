"""
Token registry: which ERC-3009 stablecoins PocketDesk agents are allowed to sign for.

PocketDesk is token-agnostic by design — the safety model only needs ERC-3009
(gasless, bounded agent payments) and ERC-2612 (non-custodial budgets). AUSD is the
default and the only token used for the *budget*; the registry exists so the x402
client can understand any standard x402 requirement and so we never sign for an
unknown token.

Rules:
  * The x402 client picks the first `accepts[]` requirement whose (network, asset)
    matches an ENABLED registry entry, preferring AUSD.
  * At startup, `verify_registry()` checks every entry on-chain (`decimals()`,
    `eip712Domain()`); an entry that fails is disabled and the reason is logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from eth_utils import to_checksum_address

from app.constants import (
    AUSD_DECIMALS,
    AUSD_EIP712_NAME,
    AUSD_EIP712_VERSION,
    AUSD_FAUCET_ADDRESS,
    AUSD_TESTNET_ADDRESS,
    BASE_SEPOLIA_CAIP2,
    BASE_SEPOLIA_CHAIN_ID,
)

log = logging.getLogger(__name__)

USDC_BASE_SEPOLIA_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_DECIMALS = 6

# AUSD (and its faucet) live at the same address on every EVM testnet Agora supports.
ARBITRUM_SEPOLIA_CHAIN_ID = 421614
ARBITRUM_SEPOLIA_CAIP2 = "eip155:421614"


@dataclass(frozen=True)
class TokenInfo:
    symbol: str
    chain_id: int
    caip2: str
    address: str
    eip712_name: str
    eip712_version: str
    decimals: int
    faucet: str | None = None
    is_budget_token: bool = False   # True only for AUSD: the token budgets/permits/vouchers use
    enabled: bool = True
    disabled_reason: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """Lookup key used when matching an x402 requirement: (network, lower-cased asset address)."""
        return (self.caip2, self.address.lower())


def default_registry() -> list[TokenInfo]:
    """The registry in preference order (AUSD first). The AUSD address comes from settings
    (defaults to the official testnet address; a local anvil run points it at MockAUSD)."""
    from app.config import get_settings  # local import: keeps this module importable without .env

    return [
        TokenInfo(
            symbol="AUSD",
            chain_id=BASE_SEPOLIA_CHAIN_ID,
            caip2=BASE_SEPOLIA_CAIP2,
            address=to_checksum_address(get_settings().ausd_address or AUSD_TESTNET_ADDRESS),
            eip712_name=AUSD_EIP712_NAME,
            eip712_version=AUSD_EIP712_VERSION,
            decimals=AUSD_DECIMALS,
            faucet=to_checksum_address(AUSD_FAUCET_ADDRESS),
            is_budget_token=True,
        ),
        TokenInfo(
            symbol="USDC",
            chain_id=BASE_SEPOLIA_CHAIN_ID,
            caip2=BASE_SEPOLIA_CAIP2,
            address=to_checksum_address(USDC_BASE_SEPOLIA_ADDRESS),
            eip712_name="USDC",       # expected; verified on-chain at startup
            eip712_version="2",
            decimals=USDC_DECIMALS,
        ),
    ]


@dataclass
class TokenRegistry:
    tokens: list[TokenInfo] = field(default_factory=default_registry)

    def enabled(self) -> list[TokenInfo]:
        return [t for t in self.tokens if t.enabled]

    def find(self, network: str, asset: str) -> TokenInfo | None:
        """Return the enabled token matching an x402 (network, asset) pair, or None."""
        wanted = (network, asset.lower())
        for token in self.enabled():
            if token.key == wanted:
                return token
        return None

    def budget_token(self) -> TokenInfo:
        for token in self.tokens:
            if token.is_budget_token:
                return token
        raise LookupError("registry has no budget token (AUSD)")

    def disable(self, symbol: str, reason: str) -> None:
        self.tokens = [replace(t, enabled=False, disabled_reason=reason) if t.symbol == symbol else t for t in self.tokens]
        log.warning("token %s disabled: %s", symbol, reason)


ERC5267_AND_DECIMALS_ABI = [
    {"type": "function", "name": "decimals", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
    {
        "type": "function",
        "name": "eip712Domain",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "fields", "type": "bytes1"},
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
            {"name": "salt", "type": "bytes32"},
            {"name": "extensions", "type": "uint256[]"},
        ],
    },
]


def verify_token(w3, token: TokenInfo) -> str | None:
    """Check one token on-chain. Returns None if it matches the registry, else the reason it does not."""
    try:
        contract = w3.eth.contract(address=token.address, abi=ERC5267_AND_DECIMALS_ABI)
        decimals = contract.functions.decimals().call()
        _, name, version, chain_id, verifying_contract, _, _ = contract.functions.eip712Domain().call()
    except Exception as exc:  # noqa: BLE001 — any RPC/ABI failure disables the entry
        from app.chain.web3client import redact_urls  # local import: web3client is heavier and imports config

        return f"on-chain check failed: {type(exc).__name__}: {redact_urls(str(exc))}"
    if decimals != token.decimals:
        return f"decimals {decimals} != expected {token.decimals}"
    if (name, version) != (token.eip712_name, token.eip712_version):
        return f"EIP-712 domain {name!r}/{version!r} != expected {token.eip712_name!r}/{token.eip712_version!r}"
    if chain_id != token.chain_id:
        return f"EIP-712 chainId {chain_id} != expected {token.chain_id}"
    if verifying_contract.lower() != token.address.lower():
        return "EIP-712 verifyingContract does not match token address"
    return None


def verify_registry(w3, registry: TokenRegistry) -> TokenRegistry:
    """Verify every entry on the connected chain; disable (and log) any entry that fails.

    Only entries whose chain_id equals the connected chain are checked; others are disabled
    because we cannot verify them with this connection.
    """
    connected_chain_id = w3.eth.chain_id
    for token in list(registry.tokens):
        if token.chain_id != connected_chain_id:
            registry.disable(token.symbol, f"not on connected chain {connected_chain_id}")
            continue
        reason = verify_token(w3, token)
        if reason:
            registry.disable(token.symbol, reason)
    return registry
