"""
End-to-end demo on a real chain (Base Sepolia by default), printing every transaction.

    cd backend && uv run python -m scripts.demo_e2e

It drives the real PocketDesk app in-process (same API the dashboard uses) as the demo user:

    1. faucet          relayer asks the AUSD faucet for test AUSD for the demo user (user pays no gas)
    2. sign            demo user signs an ERC-2612 permit + a BudgetConfig (EIP-712, no gas)
    3. openBudget      relayer submits both to BudgetController
    4. session start   allocator splits today's budget; relayer recharges each agent wallet
    5. agent ticks     agents buy research items over x402 (ERC-3009) from the demo vendors,
                       skip items the VOI gate rejects, and emit findings
    6. session stop    every agent signs a sweep back to the owner; relayer submits it

Needs in .env: RELAYER_PRIVATE_KEY + SELLER_SETTLER_PRIVATE_KEY (with Base Sepolia ETH), agent keys,
DEMO_USER_PRIVATE_KEY, SELLER_PAYTO_* and BUDGET_CONTROLLER_ADDRESS (run `make deploy` first).
Exit code 0 only if at least one paid purchase settled on-chain.
"""

from __future__ import annotations

import asyncio
import sys

import httpx
from eth_account import Account

from app.chain.eip712 import sign_typed_data
from app.config import get_settings
from app.constants import BASESCAN_TX_URL, ONE_AUSD

TOTAL_BUDGET = 20 * ONE_AUSD
DAILY_CAP = 5 * ONE_AUSD
AGENT_CAP = 2 * ONE_AUSD
PLAN_DAYS = 7
TICKS_PER_AGENT = 2
API = "http://pocketdesk.local"


class LateASGITransport(httpx.AsyncBaseTransport):
    """ASGI transport whose app is attached after construction (agents need a client before the app exists)."""

    def __init__(self) -> None:
        self.inner: httpx.ASGITransport | None = None

    def attach(self, app) -> None:
        self.inner = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert self.inner is not None, "app not attached yet"
        return await self.inner.handle_async_request(request)


def explorer_prefix() -> str:
    """Basescan link prefix on Base Sepolia; a plain label on a local anvil chain (no public explorer)."""
    rpc = get_settings().rpc_url_base_sepolia
    return "local-chain tx " if ("127.0.0.1" in rpc or "localhost" in rpc) else BASESCAN_TX_URL


def tx(label: str, tx_hash: str | None) -> None:
    print(f"  {label:<34} {explorer_prefix()}{tx_hash}" if tx_hash else f"  {label:<34} (no tx)")


async def call(api: httpx.AsyncClient, method: str, path: str, body: dict | None = None) -> dict:
    response = await api.request(method, path, json=body)
    if response.status_code >= 400:
        print(f"\n✗ {method} {path} → {response.status_code}: {response.text[:400]}")
        sys.exit(1)
    return response.json()


async def run() -> None:
    from app.keeper.runtime import build_runtime  # imported late: needs settings/.env
    from app.main import create_app

    settings = get_settings()
    user = Account.from_key(settings.demo_user_private_key)
    transport = LateASGITransport()
    runtime = build_runtime(seller_http=httpx.AsyncClient(transport=transport, timeout=120))
    app = create_app(runtime=runtime)
    transport.attach(app)
    api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API, timeout=180)

    print("▶ health:", await call(api, "GET", "/api/health"))

    print("\n▶ 1. faucet → demo user", user.address)
    if runtime.token.balance_of(user.address) < TOTAL_BUDGET:
        tx("faucet requestFunds", (await call(api, "POST", "/api/faucet", {"address": user.address})).get("tx_hash"))
    print(f"  demo user AUSD: {runtime.token.balance_of(user.address) / ONE_AUSD:.2f}")

    print("\n▶ 2. user signs permit + BudgetConfig (no gas)")
    agent_names = list(runtime.agents)
    prepared = await call(api, "POST", "/api/budget/prepare", {
        "owner": user.address, "total_budget_atomic": TOTAL_BUDGET, "daily_cap_atomic": DAILY_CAP,
        "plan_days": PLAN_DAYS, "agent_caps_atomic": {name: AGENT_CAP for name in agent_names},
    })
    permit_sig = sign_typed_data(settings.demo_user_private_key, prepared["permit"]).hex
    config_sig = sign_typed_data(settings.demo_user_private_key, prepared["config"]).hex

    print("\n▶ 3. relayer opens the budget")
    opened = await call(api, "POST", "/api/budget/open", {
        "owner": user.address,
        "permit": {"value": prepared["permit"]["message"]["value"], "deadline": prepared["permit"]["message"]["deadline"],
                   "signature": permit_sig},
        "config": prepared["config"]["message"],
        "config_signature": config_sig,
    })
    tx("BudgetController.openBudget", opened.get("tx_hash"))

    print("\n▶ 4. session start: allocate + recharge agents")
    started = await call(api, "POST", "/api/session/start", {})
    for row in started.get("recharge_txs", []):
        tx(f"recharge {row.get('agent')} {row.get('amount_atomic', 0) / ONE_AUSD:.2f} AUSD", row.get("tx_hash"))

    print("\n▶ 5. agents tick (x402 purchases, VOI skips, findings)")
    for _ in range(TICKS_PER_AGENT):
        await runtime.run_tick()
    purchases = await call(api, "GET", "/api/purchases?limit=50")
    rows = purchases if isinstance(purchases, list) else purchases.get("purchases", [])
    paid = [p for p in rows if p.get("status") == "bought" and p.get("tx_hash")]
    for p in rows:
        line = f"{p.get('status'):<7} {p.get('agent')}/{p.get('seller_id')} {p.get('asset') or ''}"
        if p.get("status") == "bought":
            tx(line, p.get("tx_hash"))
        else:
            print(f"  {line:<34} {p.get('reason', '')}")
    findings = await call(api, "GET", "/api/findings?limit=10")
    for f in (findings if isinstance(findings, list) else findings.get("findings", [])):
        print(f"  finding: [{f.get('asset')}] {f.get('headline')}  (p={f.get('confidence')}, Δp={f.get('delta_p')})")

    print("\n▶ 6. session stop: sweep agent balances back to the owner")
    stopped = await call(api, "POST", "/api/session/stop", {})
    for row in stopped.get("sweep_txs", []):
        tx(f"sweep {row.get('agent')} {row.get('amount_atomic', 0) / ONE_AUSD:.4f} AUSD", row.get("tx_hash"))

    if not paid:
        print("\n✗ no paid purchase settled — check the skipped/failed reasons above")
        sys.exit(1)
    print(f"\n✓ DEMO PASSED — {len(paid)} paid x402 purchase(s) settled on-chain")


def main() -> None:
    import argparse
    import os

    from dotenv import dotenv_values

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", help="extra env file layered over .env (e.g. ../.env.local-anvil for the offline stack)")
    args = parser.parse_args()
    if args.env_file:
        os.environ.update({k: v for k, v in dotenv_values(args.env_file).items() if v is not None})
        get_settings.cache_clear()
    asyncio.run(run())


if __name__ == "__main__":
    main()
