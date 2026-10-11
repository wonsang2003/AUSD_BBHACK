"""
M2 gate — the first REAL payment on Base Sepolia.

An agent wallet (OptionsEvent, holding AUSD but zero ETH) buys one research item from
a demo vendor over x402. The agent only signs an ERC-3009 authorization; the vendor's
settlement wallet submits `transferWithAuthorization` and pays the gas.

    cd backend && uv run python -m scripts.gate_m2                    # expected_move (needs Deribit)
    cd backend && uv run python -m scripts.gate_m2 --seller perps     # perps_crowding (needs Hyperliquid)
    cd backend && uv run python -m scripts.gate_m2 --seller-url http://localhost:8000/sell/expected-move

Needs in the repo-root .env: RELAYER_PRIVATE_KEY and SELLER_SETTLER_PRIVATE_KEY (both with
Base Sepolia ETH), AGENT_OPTIONSEVENT_KEY, SELLER_PAYTO_* addresses.
Exit code 0 = paid and verified on-chain; anything else = gate failed (reason printed):
    1 payment/receipt problem   2 a gas wallet has too little ETH   3 the agent holds ETH (agents must not)
On success the tx hash and Basescan link are also written to backend/gate_m2_result.json, so
"what is the M2 tx hash?" can be answered later without re-running the gate (`make gate-m2-status`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

import httpx
from eth_account import Account
from fastapi import FastAPI

from app.agents.breaker import CircuitBreaker
from app.chain.ausd import FAUCET_ABI, AusdClient, assert_ausd_matches_spec
from app.chain.tokens import TokenRegistry, verify_registry
from app.chain.web3client import TxSender, get_web3, redact_urls
from app.config import BACKEND_DIR, get_settings
from app.constants import BASESCAN_TX_URL, ONE_AUSD
from app.x402.client import PaymentError, X402Client
from app.x402.server import make_paid_router

SELLERS = {
    "expected_move": ("app.sellers.expected_move", {"asset": "BTC", "expiry": "next"}),
    "perps": ("app.sellers.perps_crowding", {"assets": "BTC,ETH,SOL"}),
}
MIN_AGENT_BALANCE = ONE_AUSD          # top the agent up from the faucet below 1 AUSD
MAX_PRICE = ONE_AUSD // 2             # the agent refuses to pay more than 0.50 AUSD for one item
RESULT_PATH = BACKEND_DIR / "gate_m2_result.json"
# Gas a wallet must be able to pay for: the settler sends one settlement plus at most one retry
# (the client's MAX_PAYMENT_ATTEMPTS); the relayer sends at most one faucet call. 200k gas is a
# generous ceiling for either call, including TxSender's 20 % estimate buffer.
GATE_TXS_PER_GAS_WALLET = 2
GAS_UNITS_PER_GATE_TX = 200_000
EXIT_PAYMENT_FAILED = 1
EXIT_GAS_WALLET_UNDERFUNDED = 2
EXIT_AGENT_HOLDS_ETH = 3


def step(message: str) -> None:
    print(f"\n▶ {message}")


def fail(message: str, code: int = EXIT_PAYMENT_FAILED) -> None:
    print(f"\n✗ GATE FAILED: {message}")
    sys.exit(code)


def check_network_and_token(settings):
    step("Connecting to Base Sepolia and checking the AUSD token")
    w3 = get_web3()
    try:
        chain_id = w3.eth.chain_id
    except Exception as exc:  # noqa: BLE001
        fail(f"cannot reach the Base Sepolia RPC ({redact_urls(settings.rpc_url_base_sepolia)}): "
             f"{type(exc).__name__}: {redact_urls(str(exc))}")
    if chain_id != settings.chain_id:
        fail(f"RPC chain id {chain_id} != expected {settings.chain_id}")
    domain = assert_ausd_matches_spec(w3, settings.ausd_address, settings.chain_id)
    print(f"  AUSD domain OK: name={domain.name!r} version={domain.version!r} chainId={domain.chain_id}")
    return w3


def check_gas_wallets(w3, settings) -> TxSender:
    step("Checking the two gas wallets (the only wallets that hold ETH)")
    relayer = TxSender(w3, settings.relayer_private_key, "relayer")
    settler = TxSender(w3, settings.seller_settler_private_key, "seller-settler")
    for sender in (relayer, settler):
        require_gas_wallet_funded(sender)
    return relayer


def min_gas_wallet_balance(sender: TxSender) -> int:
    """Wei a gas wallet needs: GATE_TXS_PER_GAS_WALLET × GAS_UNITS_PER_GATE_TX × the current maxFeePerGas.

    The node refuses a transaction when balance < gas limit × maxFeePerGas ("insufficient funds").
    For the settler that refusal would only show up as an ambiguous HTTP 504 after minutes of
    waiting, so the gate checks it before anything is signed.
    """
    max_fee_per_gas = sender._fee_fields()["maxFeePerGas"]  # the same fee TxSender will send with
    return GATE_TXS_PER_GAS_WALLET * GAS_UNITS_PER_GATE_TX * max_fee_per_gas


def require_gas_wallet_funded(sender: TxSender) -> None:
    """Rule: a gas wallet must hold at least min_gas_wallet_balance(sender) wei. Otherwise the gate fails."""
    balance = sender.eth_balance()
    required = min_gas_wallet_balance(sender)
    print(f"  {sender.label:<15} {sender.address}  {balance / 1e18:.6f} ETH  (needs ≥ {required / 1e18:.6f})")
    if balance < required:
        fail(f"{sender.label} has {balance / 1e18:.6f} ETH but needs at least {required / 1e18:.6f} ETH "
             f"— fund {sender.address} from a Base Sepolia faucet", EXIT_GAS_WALLET_UNDERFUNDED)


def require_agent_holds_no_eth(token: AusdClient, agent: str) -> None:
    """Rule (SPEC §9): agents hold exactly 0 ETH — the relayer/settler pay all gas. Otherwise the gate fails."""
    eth = token.eth_balance(agent)
    if eth != 0:
        fail(f"agent {agent} holds {eth / 1e18:.6f} ETH — agents must never hold gas money", EXIT_AGENT_HOLDS_ETH)


def ensure_agent_has_ausd(w3, settings, relayer: TxSender, token: AusdClient, agent: str) -> None:
    step("Checking the paying agent (AUSD only, zero ETH)")
    balance = token.balance_of(agent)
    print(f"  OptionsEvent    {agent}  {balance / ONE_AUSD:.4f} AUSD  {token.eth_balance(agent) / 1e18:.5f} ETH")
    require_agent_holds_no_eth(token, agent)
    if balance < MIN_AGENT_BALANCE:
        faucet = w3.eth.contract(address=settings.ausd_faucet, abi=FAUCET_ABI)
        result = relayer.send(faucet.functions.requestFunds(agent))
        print(f"  relayer called the AUSD faucet for the agent: {BASESCAN_TX_URL}{result.tx_hash}")
        print(f"  agent balance now {token.balance_of(agent) / ONE_AUSD:.4f} AUSD")


def build_client(w3, settings, token: AusdClient, seller_app: FastAPI | None) -> X402Client:
    registry = verify_registry(w3, TokenRegistry())
    for t in registry.tokens:
        print(f"  token registry: {t.symbol:<5} {'enabled' if t.enabled else 'disabled: ' + t.disabled_reason}")
    http = (httpx.AsyncClient(transport=httpx.ASGITransport(app=seller_app), base_url="http://demo-vendor.local", timeout=120)
            if seller_app else httpx.AsyncClient(timeout=120))
    return X402Client(
        private_key=settings.agent_optionsevent_key,
        registry=registry,
        token_clients={token.address: token},
        max_amount_atomic=MAX_PRICE,
        agent_name="OptionsEvent",
        breaker=CircuitBreaker(),
        http=http,
    )


async def run(args) -> None:
    settings = get_settings()
    w3 = check_network_and_token(settings)
    relayer = check_gas_wallets(w3, settings)
    token = AusdClient(w3, settings.ausd_address)
    agent = Account.from_key(settings.agent_optionsevent_key).address
    ensure_agent_has_ausd(w3, settings, relayer, token, agent)

    module_name, params = SELLERS[args.seller]
    if args.seller_url:
        url, seller_app = args.seller_url, None
    else:
        module = __import__(module_name, fromlist=["make_offer", "compute"])
        offer = module.make_offer()
        seller_app = FastAPI()
        seller_app.include_router(make_paid_router(offer, module.compute))
        url = f"http://demo-vendor.local{offer.path}"

    step(f"Agent buys one item over x402: GET {url} {params}")
    client = build_client(w3, settings, token, seller_app)
    before = token.balance_of(agent)
    try:
        result = await client.get(url, params=params)
    except PaymentError as exc:
        if exc.tx_hash:
            print(f"  tx: {BASESCAN_TX_URL}{exc.tx_hash}")
        fail(f"payment did not complete ({exc.kind}): {exc.reason}")
    except httpx.HTTPError as exc:
        # Only the first, unpaid GET lets transport errors through (the paid GET handles its own),
        # so at this point nothing has been signed or paid.
        fail(f"could not reach the seller at {url} — nothing was signed or paid: {type(exc).__name__}: {exc}")

    receipt = w3.eth.get_transaction_receipt(result.tx_hash)
    after = token.balance_of(agent)
    step("Result")
    print(f"  PAYMENT-RESPONSE: {result.settlement.model_dump(exclude_none=True)}")
    print(f"  tx: {BASESCAN_TX_URL}{result.tx_hash}  (receipt status {receipt['status']})")
    print(f"  paid {result.price_atomic / ONE_AUSD:.4f} {result.token_symbol} → {result.pay_to}")
    print(f"  agent AUSD {before / ONE_AUSD:.4f} → {after / ONE_AUSD:.4f};  agent ETH {token.eth_balance(agent) / 1e18:.5f}")
    print(f"  data keys: {sorted(result.data)[:12]}")
    if receipt["status"] != 1 or before - after != result.price_atomic:
        fail("receipt or balance change does not match the payment")
    require_agent_holds_no_eth(token, agent)
    write_gate_record(result, receipt["blockNumber"], url)
    print("\n✓ M2 GATE PASSED — real AUSD moved agent → seller on Base Sepolia via x402 + ERC-3009")
    print(f"  recorded in {RESULT_PATH}")


def write_gate_record(result, block_number: int, url: str) -> None:
    """Save the passing payment (tx hash, explorer link, amounts) so the M2 status can be shown later."""
    record = {
        "gate": "M2",
        "passed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tx_hash": result.tx_hash,
        "explorer_url": f"{BASESCAN_TX_URL}{result.tx_hash}",
        "block_number": block_number,
        "payer": result.payer,
        "pay_to": result.pay_to,
        "price_atomic": result.price_atomic,
        "token": result.token_symbol,
        "network": result.network,
        "seller_url": url,
    }
    RESULT_PATH.write_text(json.dumps(record, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seller", choices=sorted(SELLERS), default="expected_move")
    parser.add_argument("--seller-url", help="pay a running PocketDesk server instead of an in-process demo vendor")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
