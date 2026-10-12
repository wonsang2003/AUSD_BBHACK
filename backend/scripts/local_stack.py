"""
Offline demo stack: a local anvil chain with PocketDesk's contracts, so the whole app runs
without Base Sepolia (no testnet ETH, no RPC limits).

    cd backend && uv run python -m scripts.local_stack              # runs until Ctrl-C
    cd backend && uv run python -m scripts.local_stack --detach     # leaves anvil running in the background

What it does:
    1. starts `anvil --chain-id 84532` (Base Sepolia's id, so every EIP-712 domain matches production);
    2. deploys MockAUSD (the local stand-in: "Agora Dollar"/"1", 6 decimals, ERC-3009, faucet) and
       BudgetController(MockAUSD, keeper = relayer);
    3. funds a fresh demo user with 10,000 test AUSD through the faucet path (`requestFunds`, relayer pays);
    4. writes `.env.local-anvil` (repo root, mode 0600) with the RPC / AUSD / controller / faucet
       overrides, local gas wallets (anvil's public dev keys) and fresh agent + demo-user keys
       (agents and the user hold NO ETH, exactly as on the testnet);
    5. prints how to start the server against it.

The anvil dev keys are public test keys from Foundry's default mnemonic — never use them with real
funds. Private keys are written to the file only, never printed.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from eth_account import Account
from web3 import Web3

from app.chain.artifacts import load_artifact
from app.chain.ausd import AusdClient
from app.chain.faucet import request_test_ausd
from app.chain.web3client import TxSender
from app.config import REPO_ROOT, get_settings
from app.constants import AGENT_NAMES, BASE_SEPOLIA_CHAIN_ID, ONE_AUSD

ENV_FILE = REPO_ROOT / ".env.local-anvil"
LOCAL_DB_PATH = REPO_ROOT / "backend" / "pocketdesk-local.db"   # reset every run: the chain is fresh too
PRIVATE_FILE_MODE = 0o600
DEFAULT_PORT = 8545
ANVIL_START_TIMEOUT_SECONDS = 10.0
ANVIL_POLL_SECONDS = 0.1

# Public anvil dev keys (default mnemonic): deployer, relayer/keeper, seller-settler.
ANVIL_DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_RELAYER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
ANVIL_SETTLER_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"

AGENT_KEY_SETTINGS = {name: f"AGENT_{name.upper()}_KEY" for name in AGENT_NAMES}


def find_anvil() -> str:
    """Path of the anvil binary (PATH or ~/.foundry/bin), or exit with install instructions."""
    candidates = [shutil.which("anvil"), str(Path.home() / ".foundry/bin/anvil"), "/root/.foundry/bin/anvil"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit("anvil not found: install Foundry (https://getfoundry.sh) and run `foundryup`")


def start_anvil(port: int, detach: bool) -> subprocess.Popen:
    """Start anvil with chain id 84532 and wait until it answers JSON-RPC."""
    process = subprocess.Popen(
        [find_anvil(), "--chain-id", str(BASE_SEPOLIA_CHAIN_ID), "--port", str(port), "--silent", "--block-time", "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=detach,
    )
    w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}"))
    deadline = time.monotonic() + ANVIL_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if w3.is_connected():
                return process
        except Exception:  # noqa: BLE001 — not up yet
            pass
        time.sleep(ANVIL_POLL_SECONDS)
    process.terminate()
    sys.exit(f"anvil did not start on port {port} (is the port already in use?)")


def new_key() -> str:
    """A fresh random private key (0x-hex) for a local-only wallet."""
    return "0x" + secrets.token_hex(32)


def deploy(w3: Web3) -> dict[str, str]:
    """Deploy MockAUSD + BudgetController; returns their addresses."""
    deployer = TxSender(w3, ANVIL_DEPLOYER_KEY, "deployer")
    ausd_abi, ausd_bytecode = load_artifact("MockAUSD")
    ausd_address, _ = deployer.deploy(ausd_abi, ausd_bytecode)
    controller_abi, controller_bytecode = load_artifact("BudgetController")
    keeper = Account.from_key(ANVIL_RELAYER_KEY).address
    controller_address, _ = deployer.deploy(controller_abi, controller_bytecode, ausd_address, keeper)
    return {"ausd": ausd_address, "controller": controller_address}


def local_keys() -> dict[str, str]:
    """Fresh random agent keys + demo-user key for this local chain (none of them ever gets ETH).

    Fresh every run, so the testnet keys in .env are never copied into another file."""
    keys = {setting: new_key() for setting in AGENT_KEY_SETTINGS.values()}
    keys["DEMO_USER_PRIVATE_KEY"] = new_key()
    return keys


def payout_addresses() -> dict[str, str]:
    """Demo-vendor payout addresses: the configured ones, else fresh addresses (receiving needs no key)."""
    settings = get_settings()
    configured = {
        "SELLER_PAYTO_EXPECTED_MOVE": settings.seller_payto_expected_move,
        "SELLER_PAYTO_PM_EDGE": settings.seller_payto_pm_edge,
        "SELLER_PAYTO_PERPS": settings.seller_payto_perps,
        "SELLER_PAYTO_NEWS": settings.seller_payto_news,
        "SELLER_PAYTO_INSIDER": settings.seller_payto_insider,
        "SELLER_PAYTO_MACRO": settings.seller_payto_macro,
    }
    return {name: value or Account.create().address for name, value in configured.items()}


def write_env_file(rpc_url: str, addresses: dict[str, str], keys: dict[str, str]) -> None:
    """Write .env.local-anvil (mode 0600: it holds local private keys)."""
    lines = [
        "# Written by scripts/local_stack.py — LOCAL ANVIL ONLY. Overrides the repo-root .env.",
        f"RPC_URL_BASE_SEPOLIA={rpc_url}",
        f"CHAIN_ID={BASE_SEPOLIA_CHAIN_ID}",
        f"AUSD_ADDRESS={addresses['ausd']}",
        f"AUSD_FAUCET={addresses['ausd']}",          # MockAUSD has requestFunds(address) itself
        f"BUDGET_CONTROLLER_ADDRESS={addresses['controller']}",
        f"RELAYER_PRIVATE_KEY={ANVIL_RELAYER_KEY}",
        f"SELLER_SETTLER_PRIVATE_KEY={ANVIL_SETTLER_KEY}",
        "X402_SETTLEMENT=self",
        "SKIP_CHAIN_CHECKS=false",
        f"DATABASE_URL=sqlite:///{LOCAL_DB_PATH}",
    ]
    lines += [f"{name}={value}" for name, value in keys.items()]
    lines += [f"{name}={value}" for name, value in payout_addresses().items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_FILE, PRIVATE_FILE_MODE)


def fund_demo_user(w3: Web3, ausd_address: str, demo_user: str) -> str:
    """The relayer calls the (mock) faucet for the demo user; returns the tx hash."""
    relayer = TxSender(w3, ANVIL_RELAYER_KEY, "relayer")
    result = request_test_ausd(relayer, ausd_address, demo_user)
    balance = AusdClient(w3, ausd_address).balance_of(demo_user)
    print(f"  demo user {demo_user} funded: {balance / ONE_AUSD:,.2f} AUSD (tx {result.tx_hash})")
    return result.tx_hash


def print_next_steps(port: int, addresses: dict[str, str]) -> None:
    print("\nLocal stack ready:")
    print(f"  RPC                 http://127.0.0.1:{port}  (chain id {BASE_SEPOLIA_CHAIN_ID})")
    print(f"  MockAUSD + faucet   {addresses['ausd']}")
    print(f"  BudgetController    {addresses['controller']}")
    print(f"  settings written to {ENV_FILE}")
    print("\nStart the backend against it:")
    print("  make server ENV_FILE=.env.local-anvil")
    print("  # or: cd backend && uv run python -m scripts.run_server --env-file ../.env.local-anvil")
    print("Run the end-to-end demo against it:")
    print("  cd backend && uv run python -m scripts.demo_e2e --env-file ../.env.local-anvil")
    print("Web: set NEXT_PUBLIC_AUSD_ADDRESS / NEXT_PUBLIC_BUDGET_CONTROLLER_ADDRESS to the addresses above "
          "and point your wallet at the local RPC.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--detach", action="store_true", help="leave anvil running after this script exits")
    args = parser.parse_args()

    print(f"▶ starting anvil (chain id {BASE_SEPOLIA_CHAIN_ID}) on port {args.port}")
    process = start_anvil(args.port, args.detach)
    rpc_url = f"http://127.0.0.1:{args.port}"
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    print("▶ deploying MockAUSD + BudgetController")
    addresses = deploy(w3)
    keys = local_keys()
    print("▶ funding a fresh demo user through the faucet (relayer pays gas)")
    fund_demo_user(w3, addresses["ausd"], Account.from_key(keys["DEMO_USER_PRIVATE_KEY"]).address)
    write_env_file(rpc_url, addresses, keys)
    LOCAL_DB_PATH.unlink(missing_ok=True)   # old purchases/owner belong to a chain that no longer exists
    print_next_steps(args.port, addresses)

    if args.detach:
        print(f"\nanvil keeps running (pid {process.pid}); stop it with: kill {process.pid}")
        return
    print("\nanvil is running — press Ctrl-C to stop it.")
    try:
        process.wait()
    except KeyboardInterrupt:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
        print("anvil stopped.")


if __name__ == "__main__":
    main()
