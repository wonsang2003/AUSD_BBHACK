"""
Tests for app/chain/eip712.py — the typed-data builders every PocketDesk signature goes through.

Three kinds of checks:
  1. Python ↔ Solidity: `typed_data_digest(build_budget_config(...))` equals the deployed
     BudgetController's own `hashBudgetConfig` for several configs (no agents, one, four).
  2. Signatures recover: permit, ERC-3009 transfer / receive and BudgetConfig signatures recover
     to the signer.
  3. Python ↔ TypeScript: `tests/fixtures/eip712_vectors.json` pins fixed inputs (fixed key, fields
     and verifyingContract) with the digests Python computes; `web/scripts/test-eip712.mjs` checks
     the web builders against the same file. Regenerate after an intentional change with
         cd backend && uv run python -m tests.test_eip712
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_account import Account

from app.chain import eip712
from app.chain.vouchers import compute_voucher_nonce, day_bounds
from app.constants import AUSD_TESTNET_ADDRESS, BASE_SEPOLIA_CHAIN_ID, ONE_AUSD

VECTORS_PATH = Path(__file__).parent / "fixtures" / "eip712_vectors.json"

# Public example key from the eth-account documentation — a dummy test key, never funded anywhere.
VECTOR_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
VECTOR_SIGNER = Account.from_key(VECTOR_PRIVATE_KEY).address

# Fixed verifyingContracts / counterparties (any valid addresses work; these never change).
VECTOR_CONTROLLER = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
VECTOR_SELLER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
VECTOR_AGENTS = [
    "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
    "0x90F79bf6EB2c4f870365E785982E1f101E93b906",
    "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65",
    "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
]
VECTOR_SECTORS = ["OnchainPerps", "OptionsEvent", "NewsSentiment", "MacroFilings"]
VECTOR_TIMESTAMP = 1_790_000_000
VECTOR_DAY_INDEX = VECTOR_TIMESTAMP // 86_400
VECTOR_SALT = "0x" + "5a" * 32
JS_MAX_SAFE_INTEGER = 2**53 - 1  # the web test reads plain JSON numbers


def _config(agent_count: int, *, owner: str = VECTOR_SIGNER, nonce: int = 0) -> eip712.BudgetConfig:
    """A BudgetConfig with the first `agent_count` fixed agents (0 → all three arrays empty)."""
    return eip712.BudgetConfig(
        owner=owner,
        daily_cap=10 * ONE_AUSD,
        expiry=VECTOR_TIMESTAMP + 30 * 86_400,
        agent_addrs=VECTOR_AGENTS[:agent_count],
        agent_caps=[(index + 2) * ONE_AUSD for index in range(agent_count)],
        sectors=[eip712.sector_to_bytes32(sector) for sector in VECTOR_SECTORS[:agent_count]],
        nonce=nonce,
    )


def _voucher_authorization() -> eip712.Authorization:
    valid_after, valid_before = day_bounds(VECTOR_DAY_INDEX)
    return eip712.Authorization(
        from_address=VECTOR_SIGNER, to=VECTOR_CONTROLLER, value=8 * ONE_AUSD,
        valid_after=valid_after, valid_before=valid_before,
        nonce=compute_voucher_nonce(VECTOR_SIGNER, VECTOR_DAY_INDEX, VECTOR_SALT),
    )


def vector_messages() -> dict[str, dict]:
    """name → full EIP-712 message, built from fixed inputs with the Python builders."""
    chain, ausd = BASE_SEPOLIA_CHAIN_ID, AUSD_TESTNET_ADDRESS
    payment = eip712.Authorization(
        from_address=VECTOR_SIGNER, to=VECTOR_SELLER, value=50_000,
        valid_after=VECTOR_TIMESTAMP - 30, valid_before=VECTOR_TIMESTAMP + 120, nonce="0x" + "11" * 32,
    )
    permit_fields = dict(chain_id=chain, ausd_address=ausd, owner=VECTOR_SIGNER, spender=VECTOR_CONTROLLER,
                         nonce=0, deadline=VECTOR_TIMESTAMP + 3_600)
    return {
        "permit_300_ausd": eip712.build_permit(value=300 * ONE_AUSD, **permit_fields),
        "permit_revoke_zero": eip712.build_permit(value=0, **permit_fields),
        "transfer_with_authorization": eip712.build_transfer_with_authorization(
            chain_id=chain, ausd_address=ausd, auth=payment),
        "receive_with_authorization_daily_voucher": eip712.build_receive_with_authorization(
            chain_id=chain, ausd_address=ausd, auth=_voucher_authorization()),
        "cancel_authorization": eip712.build_cancel_authorization(
            chain_id=chain, ausd_address=ausd, authorizer=VECTOR_SIGNER, nonce=_voucher_authorization().nonce),
        "budget_config_no_agents": eip712.build_budget_config(
            chain_id=chain, controller_address=VECTOR_CONTROLLER, config=_config(0)),
        "budget_config_two_agents": eip712.build_budget_config(
            chain_id=chain, controller_address=VECTOR_CONTROLLER, config=_config(2, nonce=1)),
        "budget_config_four_agents": eip712.build_budget_config(
            chain_id=chain, controller_address=VECTOR_CONTROLLER, config=_config(4, nonce=7)),
        "set_paused": eip712.build_set_paused(
            chain_id=chain, controller_address=VECTOR_CONTROLLER, owner=VECTOR_SIGNER, paused=True, nonce=3,
            deadline=VECTOR_TIMESTAMP + 3_600),
    }


def build_vectors() -> dict:
    """The full vectors document: fixed inputs, Python digest and the fixed key's signature per message."""
    vectors = []
    for name, full_message in vector_messages().items():
        vectors.append({
            "name": name,
            "typed_data": full_message,
            "digest": eip712.typed_data_digest(full_message),
            "signature": eip712.sign_typed_data(VECTOR_PRIVATE_KEY, full_message).hex,
        })
    return {
        "description": "EIP-712 cross-language vectors: Python (backend/app/chain/eip712.py) digests that "
                       "web/lib/eip712.ts must reproduce. Regenerate: cd backend && uv run python -m tests.test_eip712",
        "signer": VECTOR_SIGNER,
        "vectors": vectors,
    }


def _all_integers(value) -> list[int]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, dict):
        return [n for item in value.values() for n in _all_integers(item)]
    if isinstance(value, list):
        return [n for item in value for n in _all_integers(item)]
    return []


def write_vectors(path: Path = VECTORS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_vectors(), indent=2) + "\n")


# ─── 1. Python ↔ Solidity ────────────────────────────────────────────────────

@pytest.mark.chain
@pytest.mark.parametrize("agent_count", [0, 1, 4])
def test_python_digest_matches_contract_hash_budget_config(controller, agent_count):
    # Arrange
    config = _config(agent_count, nonce=agent_count)
    typed = eip712.build_budget_config(
        chain_id=BASE_SEPOLIA_CHAIN_ID, controller_address=controller.address, config=config
    )

    # Act
    on_chain = controller.hash_budget_config(config)

    # Assert
    assert eip712.typed_data_digest(typed) == on_chain


@pytest.mark.chain
def test_python_digest_matches_contract_for_large_values(controller):
    # Arrange: uint128 / uint64 / uint256 maxima exercise the full field widths.
    config = eip712.BudgetConfig(
        owner=VECTOR_SIGNER, daily_cap=2**128 - 1, expiry=2**64 - 1, agent_addrs=VECTOR_AGENTS,
        agent_caps=[2**128 - 1] * 4, sectors=[eip712.sector_to_bytes32("x" * 32)] * 4, nonce=2**256 - 1,
    )
    typed = eip712.build_budget_config(
        chain_id=BASE_SEPOLIA_CHAIN_ID, controller_address=controller.address, config=config
    )

    # Act + Assert
    assert eip712.typed_data_digest(typed) == controller.hash_budget_config(config)


# ─── 2. signatures recover ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "name", ["permit_300_ausd", "transfer_with_authorization", "receive_with_authorization_daily_voucher",
             "budget_config_four_agents", "set_paused"],
)
def test_signature_recovers_to_signer(name):
    # Arrange
    full_message = vector_messages()[name]

    # Act
    signature = eip712.sign_typed_data(VECTOR_PRIVATE_KEY, full_message)
    recovered = eip712.recover_typed_data_signer(full_message, signature.hex)

    # Assert
    assert recovered == VECTOR_SIGNER
    assert signature.signature == signature.r + signature.s + bytes([signature.v])


def test_signature_does_not_recover_to_signer_after_tampering():
    # Arrange
    full_message = vector_messages()["permit_300_ausd"]
    signature = eip712.sign_typed_data(VECTOR_PRIVATE_KEY, full_message).hex
    full_message["message"]["value"] += 1

    # Act
    recovered = eip712.recover_typed_data_signer(full_message, signature)

    # Assert
    assert recovered != VECTOR_SIGNER


# ─── 3. Python ↔ TypeScript vectors file ─────────────────────────────────────

def test_eip712_vectors_fixture_is_up_to_date():
    # Arrange
    expected = build_vectors()

    # Act
    committed = json.loads(VECTORS_PATH.read_text())

    # Assert (on failure: regenerate with `uv run python -m tests.test_eip712` and review the diff)
    assert committed == expected


def test_eip712_vectors_use_only_js_safe_integers():
    # Act
    integers = _all_integers(build_vectors())

    # Assert
    assert integers and all(abs(n) <= JS_MAX_SAFE_INTEGER for n in integers)


def test_eip712_vectors_digests_and_signatures_are_consistent():
    # Arrange
    document = json.loads(VECTORS_PATH.read_text())

    # Act + Assert
    assert len(document["vectors"]) == len(vector_messages())
    for vector in document["vectors"]:
        assert eip712.typed_data_digest(vector["typed_data"]) == vector["digest"], vector["name"]
        assert eip712.recover_typed_data_signer(vector["typed_data"], vector["signature"]) == document["signer"]


if __name__ == "__main__":
    write_vectors()
    print(f"wrote {VECTORS_PATH}")
