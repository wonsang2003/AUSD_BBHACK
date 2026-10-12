"""
Deploy BudgetController(AUSD, keeper = relayer) with web3.py — no forge needed.

    cd backend && uv run python -m scripts.deploy [--force] [--rpc-url URL]

1. Reads RPC / AUSD / RELAYER_PRIVATE_KEY from the app's settings (repo-root .env).
2. Refuses to run on anything but Base Sepolia (84532) or a local anvil (31337); checks the AUSD token.
3. Idempotent: if BUDGET_CONTROLLER_ADDRESS already points at code on this chain, it does nothing
   (pass --force to deploy a new one and overwrite the address).
4. Deploys from the committed artifact (backend/app/chain/abi/BudgetController.json), signed by the
   relayer, which becomes both admin and keeper.
5. Writes BUDGET_CONTROLLER_ADDRESS and NEXT_PUBLIC_BUDGET_CONTROLLER_ADDRESS to .env and prints
   the Basescan link. Private keys are never printed.
"""

from __future__ import annotations

import argparse
import sys

from eth_utils import to_checksum_address
from web3 import Web3

from app.chain.artifacts import load_artifact
from app.chain.ausd import assert_ausd_matches_spec
from app.chain.web3client import TxResult, TxSender, get_web3, redact_urls
from app.constants import BASE_SEPOLIA_CHAIN_ID, BASESCAN_ADDRESS_URL, BASESCAN_TX_URL
from scripts._env import ENV_PATH, read_env, set_env_values

ANVIL_DEFAULT_CHAIN_ID = 31337
ALLOWED_CHAIN_IDS = (BASE_SEPOLIA_CHAIN_ID, ANVIL_DEFAULT_CHAIN_ID)
ADDRESS_SETTINGS = ("BUDGET_CONTROLLER_ADDRESS", "NEXT_PUBLIC_BUDGET_CONTROLLER_ADDRESS")


def deploy_controller(sender: TxSender, ausd_address: str, keeper_address: str) -> tuple[str, TxResult]:
    """Deploy BudgetController(ausd, keeper) from `sender` (who becomes admin). Returns (address, receipt)."""
    abi, bytecode = load_artifact("BudgetController")
    address, result = sender.deploy(abi, bytecode, to_checksum_address(ausd_address), to_checksum_address(keeper_address))
    return to_checksum_address(address), result


def existing_deployment(w3: Web3, env: dict[str, str]) -> str | None:
    """The configured controller address if it has code on this chain, else None."""
    address = env.get("BUDGET_CONTROLLER_ADDRESS", "")
    if address and Web3.is_address(address) and w3.eth.get_code(to_checksum_address(address)):
        return to_checksum_address(address)
    return None


def main(argv: list[str] | None = None) -> int:
    from app.config import get_settings

    parser = argparse.ArgumentParser(description="Deploy PocketDesk's BudgetController")
    parser.add_argument("--force", action="store_true", help="deploy even if one is configured, overwrite the address")
    parser.add_argument("--rpc-url", default=None, help="override RPC_URL_BASE_SEPOLIA")
    args = parser.parse_args(argv)

    settings = get_settings()
    w3 = get_web3(args.rpc_url or settings.rpc_url_base_sepolia)
    if not w3.is_connected():
        print(f"cannot reach RPC {redact_urls(args.rpc_url or settings.rpc_url_base_sepolia)}")
        return 1
    chain_id = w3.eth.chain_id
    if chain_id not in ALLOWED_CHAIN_IDS:
        print(f"refusing to deploy on chain {chain_id}: testnet only {ALLOWED_CHAIN_IDS}")
        return 1
    if not settings.relayer_private_key:
        print("RELAYER_PRIVATE_KEY is not set — run `make bootstrap` first")
        return 1

    existing = existing_deployment(w3, read_env(ENV_PATH))
    if existing and not args.force:
        print(f"BudgetController already deployed at {existing} (pass --force to deploy a new one)")
        print(f"  {BASESCAN_ADDRESS_URL}{existing}")
        return 0

    assert_ausd_matches_spec(w3, settings.ausd_address, chain_id)
    relayer = TxSender(w3, settings.relayer_private_key, "relayer")
    if relayer.eth_balance() == 0:
        print(f"relayer {relayer.address} has no ETH for gas — fund it: {BASESCAN_ADDRESS_URL}{relayer.address}")
        return 1

    address, result = deploy_controller(relayer, settings.ausd_address, relayer.address)
    update = set_env_values({name: address for name in ADDRESS_SETTINGS}, overwrite=True, path=ENV_PATH)
    print(f"BudgetController deployed at {address} (keeper = admin = relayer {relayer.address})")
    print(f"  wrote {', '.join(update.written)} to {ENV_PATH}")
    if chain_id == BASE_SEPOLIA_CHAIN_ID:
        print(f"  contract: {BASESCAN_ADDRESS_URL}{address}")
        print(f"  deploy tx: {BASESCAN_TX_URL}{result.tx_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
