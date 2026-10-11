"""
Bootstrap: create the testnet wallets PocketDesk needs and check they are ready.

    cd backend && uv run python -m scripts.bootstrap

1. Creates the repo-root `.env` from `.env.example` if it does not exist (file mode 0600:
   it holds hot private keys).
2. Fills in any MISSING keys/addresses and never overwrites a value you already set:
   relayer, seller-settler, 4 agents, demo user (private keys) and 6 demo-vendor payout addresses.
   ".env" is read with python-dotenv, the same parser the app's settings use (last assignment wins;
   `export`, quotes, spaces and lower-case names all count as "set").
3. Prints ADDRESSES only (private keys are never printed) — taken from the settings the app
   really uses, and warns when backend/.env or an environment variable overrides the repo-root .env.
4. If Base Sepolia is reachable: checks the AUSD token, shows ETH balances of the two gas
   wallets, fails (exit 1) if any agent holds ETH, and calls the AUSD faucet for the demo user.

Only the relayer and the seller-settler need Base Sepolia ETH. Agents never get ETH.
"""

from __future__ import annotations

import io
import os
import secrets
import stat
import sys
from pathlib import Path

from dotenv.parser import parse_stream
from eth_account import Account

from app.config import REPO_ROOT
from app.constants import BASESCAN_ADDRESS_URL, BASESCAN_TX_URL, ONE_AUSD

ENV_PATH = REPO_ROOT / ".env"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
PRIVATE_FILE_MODE = 0o600            # owner read/write only: .env holds private keys

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
GAS_WALLET_SETTINGS = ("RELAYER_PRIVATE_KEY", "SELLER_SETTLER_PRIVATE_KEY")


# ─── reading / writing .env ──────────────────────────────────────────────────
def env_assignments(text: str) -> dict[str, list[str]]:
    """NAME (upper-cased) → every value assigned to it, in file order. The app uses the LAST one."""
    assignments: dict[str, list[str]] = {}
    for binding in parse_stream(io.StringIO(text)):
        if binding.key is not None:
            assignments.setdefault(binding.key.upper(), []).append((binding.value or "").strip())
    return assignments


def set_empty_assignments(text: str, name: str, value: str) -> str:
    """Write NAME=value over every EMPTY assignment of NAME (any case / `export` form), or append it.

    Only called when no assignment of NAME has a value, so no user value can be lost.
    """
    lines, replaced = [], False
    for binding in parse_stream(io.StringIO(text)):
        is_target = binding.key is not None and binding.key.upper() == name
        if is_target and not (binding.value or "").strip():
            lines.append(f"{name}={value}\n")
            replaced = True
        else:
            lines.append(binding.original.string)
    new_text = "".join(lines)
    if not replaced:
        new_text = new_text + ("" if new_text.endswith("\n") or not new_text else "\n") + f"{name}={value}\n"
    return new_text


def fill_missing_keys(text: str) -> tuple[str, list[str], list[str]]:
    """Generate missing private keys / payout addresses. Returns (new_text, names_created, warnings).

    Rules per name: last assignment non-empty → keep. All assignments empty (or none) → generate.
    An earlier non-empty value hidden by a later empty line → keep the file as is and warn.
    """
    created, warnings = [], []
    for name in PRIVATE_KEY_SETTINGS + PAYOUT_ADDRESS_SETTINGS:
        values = env_assignments(text).get(name, [])
        if values and values[-1]:
            continue
        if any(values):
            warnings.append(f"{name} is set on an earlier line but a later empty line hides it — fix .env by hand")
            continue
        generated = "0x" + secrets.token_hex(32) if name in PRIVATE_KEY_SETTINGS else Account.create().address
        text = set_empty_assignments(text, name, generated)
        created.append(name)
    return text, created, warnings


def write_private_file(path: Path, text: str) -> None:
    """Atomically replace `path` with `text`, readable by the owner only (mode 0600)."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)
    path.chmod(PRIVATE_FILE_MODE)


def is_readable_by_others(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


# ─── which wallets the app really uses ───────────────────────────────────────
def addresses_in_use(settings) -> dict[str, str]:
    """NAME → address derived from the app's own settings (repo-root .env, backend/.env, environment)."""
    addresses = {}
    for name in PRIVATE_KEY_SETTINGS:
        key = getattr(settings, name.lower())
        addresses[name] = Account.from_key(key).address if key else ""
    return addresses


def overridden_settings(env_text: str, settings) -> list[str]:
    """Names whose value in the app differs from the repo-root .env (backend/.env or an env var wins)."""
    file_values = {name: values[-1] for name, values in env_assignments(env_text).items()}
    names = PRIVATE_KEY_SETTINGS + PAYOUT_ADDRESS_SETTINGS
    return [name for name in names if (getattr(settings, name.lower()) or "") != file_values.get(name, "")]


def print_addresses(addresses: dict[str, str]) -> None:
    print("\nWallets the app uses (addresses only — keys stay in .env):")
    for name, address in addresses.items():
        needs_eth = "  ← needs Base Sepolia ETH" if name in GAS_WALLET_SETTINGS else ""
        print(f"  {name:<28} {address or '(not set)'}{needs_eth}")


# ─── on-chain checks ─────────────────────────────────────────────────────────
def check_on_chain(settings, addresses: dict[str, str]) -> None:
    """Best effort: needs network access to Base Sepolia. Exits 1 if any agent holds ETH."""
    from app.chain.ausd import FAUCET_ABI, AusdClient, assert_ausd_matches_spec
    from app.chain.web3client import TxSender, get_web3, redact_urls

    w3 = get_web3(settings.rpc_url_base_sepolia)
    if not w3.is_connected():
        print(f"\n(no RPC access to {redact_urls(settings.rpc_url_base_sepolia)})")
        print("  ⚠ on-chain checks did NOT run — including the 'agents hold 0 ETH' check. Re-run with network access.")
        return

    domain = assert_ausd_matches_spec(w3, settings.ausd_address, settings.chain_id)
    print(f"\nAUSD token OK (EIP-712 name={domain.name!r}, version={domain.version!r}, 6 decimals)")
    token = AusdClient(w3, settings.ausd_address)
    for name in GAS_WALLET_SETTINGS:
        eth = w3.eth.get_balance(addresses[name]) / 1e18
        hint = "" if eth > 0 else f"   → fund it: {BASESCAN_ADDRESS_URL}{addresses[name]}"
        print(f"  {name:<28} {eth:.5f} ETH{hint}")
    agents_with_eth = [name for name in AGENT_SETTINGS if w3.eth.get_balance(addresses[name]) != 0]
    if agents_with_eth:
        print(f"  ✗ {', '.join(agents_with_eth)} hold ETH — agents must never hold gas money")
        sys.exit(1)
    print("  all agent wallets hold 0 ETH ✓")

    user = addresses["DEMO_USER_PRIVATE_KEY"]
    relayer = TxSender(w3, settings.relayer_private_key, "relayer")
    if relayer.eth_balance() > 0 and token.balance_of(user) < ONE_AUSD:
        faucet = w3.eth.contract(address=settings.ausd_faucet, abi=FAUCET_ABI)
        result = relayer.send(faucet.functions.requestFunds(user))
        print(f"  faucet → demo user: {BASESCAN_TX_URL}{result.tx_hash}")
    print(f"  demo user AUSD: {token.balance_of(user) / ONE_AUSD:.2f}")


def main() -> None:
    from app.config import get_settings

    if ENV_PATH.exists():
        if is_readable_by_others(ENV_PATH):
            print(f"note: {ENV_PATH} was readable by other users; tightening it to 0600")
        text = ENV_PATH.read_text()
    else:
        text = ENV_EXAMPLE_PATH.read_text()
        print(f"creating {ENV_PATH} from .env.example (mode 0600)")
    text, created, warnings = fill_missing_keys(text)
    write_private_file(ENV_PATH, text)
    print(f"generated: {', '.join(created) if created else 'nothing (all keys already set)'}")
    for warning in warnings:
        print(f"  ⚠ {warning}")

    get_settings.cache_clear()
    settings = get_settings()
    for name in overridden_settings(text, settings):
        print(f"  ⚠ {name}: the app uses a value from backend/.env or an environment variable, not the repo-root .env")
    addresses = addresses_in_use(settings)
    print_addresses(addresses)
    check_on_chain(settings, addresses)


if __name__ == "__main__":
    main()
