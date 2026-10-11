"""
Tests for app/chain/tokens.py — the token registry the x402 client signs for.

Lookup rules are tested in memory; `verify_registry` is tested against MockAUSD on a local anvil
chain: an entry matching the token's real decimals / EIP-712 domain stays enabled, every kind of
mismatch is disabled with a reason.
"""

from __future__ import annotations

import pytest

from app.chain.tokens import TokenInfo, TokenRegistry, verify_registry, verify_token
from app.constants import AUSD_DECIMALS, AUSD_EIP712_NAME, AUSD_EIP712_VERSION, BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_CHAIN_ID

AUSD_ADDRESS = "0xa9012a055bd4e0eDfF8Ce09f960291C09D5322dC"
USDC_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
NO_CODE_ADDRESS = "0x000000000000000000000000000000000000dEaD"


def _token(symbol: str, address: str, **overrides) -> TokenInfo:
    """A Base Sepolia registry entry with AUSD's signing facts unless overridden."""
    fields = dict(
        symbol=symbol, chain_id=BASE_SEPOLIA_CHAIN_ID, caip2=BASE_SEPOLIA_CAIP2, address=address,
        eip712_name=AUSD_EIP712_NAME, eip712_version=AUSD_EIP712_VERSION, decimals=AUSD_DECIMALS,
    )
    fields.update(overrides)
    return TokenInfo(**fields)


# ─── lookups (no chain) ──────────────────────────────────────────────────────

def test_find_matches_network_and_asset_case_insensitively():
    # Arrange
    registry = TokenRegistry(tokens=[_token("AUSD", AUSD_ADDRESS, is_budget_token=True)])

    # Act
    found = registry.find(BASE_SEPOLIA_CAIP2, AUSD_ADDRESS.lower())

    # Assert
    assert found is not None and found.symbol == "AUSD"
    assert registry.find("eip155:8453", AUSD_ADDRESS) is None


def test_find_ignores_disabled_tokens():
    # Arrange
    registry = TokenRegistry(tokens=[_token("USDC", USDC_ADDRESS)])

    # Act
    registry.disable("USDC", "test")

    # Assert
    assert registry.find(BASE_SEPOLIA_CAIP2, USDC_ADDRESS) is None
    assert registry.enabled() == []
    assert registry.tokens[0].disabled_reason == "test"


def test_budget_token_is_the_ausd_entry():
    # Arrange
    registry = TokenRegistry(tokens=[_token("USDC", USDC_ADDRESS), _token("AUSD", AUSD_ADDRESS, is_budget_token=True)])

    # Act + Assert
    assert registry.budget_token().symbol == "AUSD"
    with pytest.raises(LookupError):
        TokenRegistry(tokens=[_token("USDC", USDC_ADDRESS)]).budget_token()


def test_default_registry_prefers_ausd_and_marks_it_as_budget_token():
    # Act
    registry = TokenRegistry()

    # Assert
    assert [token.symbol for token in registry.tokens] == ["AUSD", "USDC"]
    assert registry.budget_token().eip712_name == "Agora Dollar"


# ─── verify_registry against MockAUSD ────────────────────────────────────────

@pytest.mark.chain
def test_verify_token_accepts_an_entry_matching_mock_ausd(w3, mock_ausd):
    # Act
    reason = verify_token(w3, _token("AUSD", mock_ausd.address))

    # Assert
    assert reason is None


@pytest.mark.chain
def test_verify_registry_keeps_matching_entry_and_disables_mismatches(w3, mock_ausd):
    # Arrange: one correct entry and one entry per kind of mismatch, all pointing at MockAUSD.
    registry = TokenRegistry(tokens=[
        _token("AUSD", mock_ausd.address, is_budget_token=True),
        _token("WRONG_DECIMALS", mock_ausd.address, decimals=18),
        _token("WRONG_NAME", mock_ausd.address, eip712_name="AUSD"),   # name() is "AUSD", the domain is not
        _token("WRONG_VERSION", mock_ausd.address, eip712_version="2"),
        _token("NO_CODE", NO_CODE_ADDRESS),
        _token("OTHER_CHAIN", mock_ausd.address, chain_id=1, caip2="eip155:1"),
    ])

    # Act
    verify_registry(w3, registry)

    # Assert
    reasons = {token.symbol: token.disabled_reason for token in registry.tokens if not token.enabled}
    assert [token.symbol for token in registry.enabled()] == ["AUSD"]
    assert registry.find(BASE_SEPOLIA_CAIP2, mock_ausd.address).symbol == "AUSD"
    assert "decimals 6 != expected 18" in reasons["WRONG_DECIMALS"]
    assert "EIP-712 domain" in reasons["WRONG_NAME"]
    assert "EIP-712 domain" in reasons["WRONG_VERSION"]
    assert "on-chain check failed" in reasons["NO_CODE"]
    assert "not on connected chain" in reasons["OTHER_CHAIN"]
