"""
Shared pytest fixtures for tests that need a real EVM chain.

`anvil` starts a local Foundry node with chain id 84532 (Base Sepolia's id, so every
EIP-712 domain matches production). On it we deploy `MockAUSD` — a local stand-in with
the same EIP-712 domain ("Agora Dollar"/"1"), 6 decimals and ERC-3009 — so payment
tests send real transactions with real signatures; nothing in the payment path is faked.

Tests that need the chain are marked `@pytest.mark.chain` and are skipped (with a clear
reason) when the `anvil` binary is not installed.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from eth_account import Account
from web3 import Web3

from app.chain.artifacts import load_artifact
from app.chain.ausd import AusdClient
from app.chain.web3client import TxSender
from app.constants import BASE_SEPOLIA_CHAIN_ID, ONE_AUSD

# Well-known anvil dev keys (public test keys from the default mnemonic — never use with real funds).
ANVIL_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",
    "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e",
    "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356",
    "0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97",
    "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6",
]


def _find_anvil() -> str | None:
    return shutil.which("anvil") or next(
        (str(p) for p in [Path.home() / ".foundry/bin/anvil", Path("/root/.foundry/bin/anvil")] if p.exists()), None
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def anvil_url():
    binary = _find_anvil()
    if binary is None:
        pytest.skip("anvil not installed (install Foundry: https://getfoundry.sh) — chain tests skipped")
    port = _free_port()
    process = subprocess.Popen(
        [binary, "--chain-id", str(BASE_SEPOLIA_CHAIN_ID), "--port", str(port), "--silent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    w3 = Web3(Web3.HTTPProvider(url))
    for _ in range(100):
        try:
            if w3.is_connected():
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.05)
    yield url
    process.terminate()
    process.wait(timeout=5)


@pytest.fixture(scope="session")
def w3(anvil_url) -> Web3:
    return Web3(Web3.HTTPProvider(anvil_url))


@dataclass(frozen=True)
class Wallets:
    deployer: str
    relayer: str
    settler: str
    user: str
    agent: str
    agent2: str
    seller: str

    def address(self, role: str) -> str:
        return Account.from_key(getattr(self, role)).address


@pytest.fixture(scope="session")
def wallets() -> Wallets:
    """Role → private key. Note the agents are funded with AUSD only (never ETH) in the tests that matter."""
    return Wallets(
        deployer=ANVIL_KEYS[0], relayer=ANVIL_KEYS[1], settler=ANVIL_KEYS[2], user=ANVIL_KEYS[3],
        agent="0x" + "a1" * 32, agent2="0x" + "a2" * 32, seller="0x" + "5e" * 32,
    )


@pytest.fixture(scope="session")
def deployer(w3, wallets) -> TxSender:
    return TxSender(w3, wallets.deployer, "deployer")


@pytest.fixture(scope="session")
def settler(w3, wallets) -> TxSender:
    return TxSender(w3, wallets.settler, "seller-settler")


@pytest.fixture(scope="session")
def relayer(w3, wallets) -> TxSender:
    return TxSender(w3, wallets.relayer, "relayer")


@pytest.fixture(scope="session")
def mock_ausd(w3, deployer) -> AusdClient:
    abi, bytecode = load_artifact("MockAUSD")
    address, _ = deployer.deploy(abi, bytecode)
    return AusdClient(w3, address)


@pytest.fixture(scope="session")
def mock_ausd_admin(w3, mock_ausd):
    """Full MockAUSD contract (mint, requestFunds, setAccountFrozen, pause controls) for test setup."""
    abi, _ = load_artifact("MockAUSD")
    return w3.eth.contract(address=mock_ausd.address, abi=abi)


@pytest.fixture
def fund_ausd(mock_ausd_admin, deployer):
    """fund_ausd(address, amount_atomic): mint test AUSD to an address (sent by the deployer)."""

    def _fund(address: str, amount: int = 10 * ONE_AUSD) -> None:
        deployer.send(mock_ausd_admin.functions.mint(Web3.to_checksum_address(address), amount))

    return _fund


@pytest.fixture
def chain_snapshot(w3, deployer, relayer, settler):
    """Revert the chain to its state before the test (keeps chain tests independent).

    After a revert the on-chain nonces go back, so every session-wide TxSender must forget
    its locally cached nonce — otherwise its next transaction would wait forever.
    """
    snapshot_id = w3.provider.make_request("evm_snapshot", [])["result"]
    yield
    w3.provider.make_request("evm_revert", [snapshot_id])
    for sender in (deployer, relayer, settler):
        sender.reset_nonce()


os.environ.setdefault("SKIP_CHAIN_CHECKS", "true")
