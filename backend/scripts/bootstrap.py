"""
Bootstrap: create the testnet wallets PocketDesk needs and check they are ready.

    cd backend && uv run python -m scripts.bootstrap

1. Creates the repo-root `.env` from `.env.example` if it does not exist.
2. Fills in any MISSING keys/addresses (never overwrites a value you already set):
   relayer, seller-settler, 4 agents, demo user (private keys) and 6 demo-vendor payout addresses.
3. Prints ADDRESSES only (private keys are never printed) with Basescan links.
4. If Base Sepolia is reachable: checks the AUSD token, shows ETH balances of the two gas
   wallets, asserts every agent holds 0 ETH, and calls the AUSD faucet for the demo user.

Only the relayer and the seller-settler need Base Sepolia ETH. Agents never get ETH.
"""

from __future__ import annotations

import re
import secrets
import shutil
from pathlib import Path

from eth_account import Account

from app.config import REPO_ROOT
from app.constants import BASESCAN_ADDRESS_URL, ONE_AUSD

ENV_PATH = REPO_ROOT / ".env"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"

PRIVATE_KEY_SETTINGS = [
    "RELAYER_PRIVATE_KEY", "SELLER_SETTLER_PRIVATE_KEY",
    "AGENT_ONCHAINPERPS_KEY", "AGENT_OPTIONSEVENT_KEY", "AGENT_NEWSSENTIMENT_KEY", "AGENT_MACROFILINGS_KEY",
    "DEMO_USER_PRIVATE_KEY",
]
PAYOUT_ADDRESS_SETTINGS = [
    "SELLER_PAYTO_EXPECTED_MOVE", "SELLER_PAYTO_PM_EDGE", "SELLER_PAYTO_PERPS",
    "SELLER_PAYTO_NEWS", "SELLER_PAYTO_INSIDER", "SELLER_PAYTO_MACRO",
]
AGENT_SETTINGS = PRIVATE_KEY_SETTINGS[2:6]


def read_env_value(text: str, key: str) -> str:
    match = re.search(rf"^{key}=([^\s#]*)", text, re.M)
    return match.group(1).strip('"') if match else ""


def write_env_value(text: str, key: str, value: str) -> str:
    """Set KEY=value, replacing an existing (empty) line or appending a new one."""
    if re.search(rf"^{key}=", text, re.M):
        return re.sub(rf"^{key}=[^\n]*", f"{key}={value}", text, count=1, flags=re.M)
    return text.rstrip("\n") + f"\n{key}={value}\n"


def fill_missing_keys(text: str) -> tuple[str, list[str]]:
    """Generate any missing private keys / payout addresses. Returns (new_text, names_created)."""
    created = []
    for key in PRIVATE_KEY_SETTINGS:
        if not read_env_value(text, key):
            text = write_env_value(text, key, "0x" + secrets.token_hex(32))
            created.append(key)
    for key in PAYOUT_ADDRESS_SETTINGS:
        if not read_env_value(text, key):
            text = write_env_value(text, key, Account.create().address)
            created.append(key)
    return text, created


def print_addresses(text: str) -> dict[str, str]:
    addresses = {key: Account.from_key(read_env_value(text, key)).address for key in PRIVATE_KEY_SETTINGS}
    print("\nWallets (addresses only — keys stay in .env):")
    for key, address in addresses.items():
        needs_eth = "  ← needs Base Sepolia ETH" if key in ("RELAYER_PRIVATE_KEY", "SELLER_SETTLER_PRIVATE_KEY") else ""
        print(f"  {key:<28} {address}{needs_eth}")
    return addresses


def check_on_chain(addresses: dict[str, str]) -> None:
    """Best effort: needs network access to Base Sepolia. Skipped with a note otherwise."""
    from app.chain.ausd import FAUCET_ABI, AusdClient, assert_ausd_matches_spec
    from app.chain.web3client import TxSender, get_web3
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    w3 = get_web3()
    try:
        w3.eth.chain_id
    except Exception as exc:  # noqa: BLE001
        print(f"\n(no RPC access to {settings.rpc_url_base_sepolia}: {type(exc).__name__}) — on-chain checks skipped")
        return

    domain = assert_ausd_matches_spec(w3, settings.ausd_address, settings.chain_id)
    print(f"\nAUSD token OK (EIP-712 name={domain.name!r}, version={domain.version!r}, 6 decimals)")
    token = AusdClient(w3, settings.ausd_address)
    for key in ("RELAYER_PRIVATE_KEY", "SELLER_SETTLER_PRIVATE_KEY"):
        eth = w3.eth.get_balance(addresses[key]) / 1e18
        hint = "" if eth > 0 else f"   → fund it: {BASESCAN_ADDRESS_URL}{addresses[key]}"
        print(f"  {key:<28} {eth:.5f} ETH{hint}")
    for key in AGENT_SETTINGS:
        eth = w3.eth.get_balance(addresses[key])
        assert eth == 0, f"{key} holds ETH — agents must never hold gas money"
    print("  all agent wallets hold 0 ETH ✓")

    relayer_key = settings.relayer_private_key
    user = addresses["DEMO_USER_PRIVATE_KEY"]
    if w3.eth.get_balance(addresses["RELAYER_PRIVATE_KEY"]) > 0 and token.balance_of(user) < ONE_AUSD:
        relayer = TxSender(w3, relayer_key, "relayer")
        faucet = w3.eth.contract(address=settings.ausd_faucet, abi=FAUCET_ABI)
        result = relayer.send(faucet.functions.requestFunds(user))
        print(f"  faucet → demo user: https://sepolia.basescan.org/tx/{result.tx_hash}")
    print(f"  demo user AUSD: {token.balance_of(user) / ONE_AUSD:.2f}")


def main() -> None:
    if not ENV_PATH.exists():
        shutil.copy(ENV_EXAMPLE_PATH, ENV_PATH)
        print(f"created {ENV_PATH} from .env.example")
    text, created = fill_missing_keys(ENV_PATH.read_text())
    ENV_PATH.write_text(text)
    print(f"generated: {', '.join(created) if created else 'nothing (all keys already set)'}")
    check_on_chain(print_addresses(text))


if __name__ == "__main__":
    main()
