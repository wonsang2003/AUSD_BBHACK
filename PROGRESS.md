# PocketDesk — progress / handoff

The cloud build was stopped on request; work continues locally in `AUSD-HACK`.
Branch: `claude/pocketdesk-research-desk-jequn7`. Read first: `docs/SPEC.md` (full spec + **§9 Update #1** rules) and
`docs/API.md` (backend ↔ web contract).

## Test status at handoff
- `cd contracts && forge test` → **82 passed** (BudgetController + MockAUSD ERC-3009).
- `cd backend && uv run pytest -q` → **370 passed** (math/decision modules only; see below).

## Done and tested
| Area | Files |
|---|---|
| Foundation | `backend/app/constants.py`, `config.py`, `chain/eip712.py` (permit, ERC-3009, BudgetConfig, SetPaused typed data), `chain/tokens.py` (token registry, AUSD default + USDC, on-chain verification), `x402/offer.py`, `data/http.py` (timeout/retry/cache/rate-limit) |
| Contracts | `contracts/src/BudgetController.sol` (permit funding, daily + per-agent caps, day rollover, sig-based config/pause, `hashBudgetConfig`/`hashSetPaused` for cross-checks), `contracts/src/mocks/MockAUSD.sol` (local-test mock: "Agora Dollar"/"1", 6 decimals, ERC-3009, faucet, `isAccountFrozen`), tests, `script/Deploy.s.sol`, `script/export_abi.sh`; ABIs exported to `backend/app/chain/abi/` |
| Math / decisions | `sellers/optionmath.py`, `agents/voi.py` (EVOI + σ learning), `agents/beliefs.py`, `agents/planner.py`, `keeper/allocator.py` (Thompson sampling, floors/caps), `keeper/recharge_policy.py`, each with tests |

## Written but NOT yet tested or reviewed (the build was stopped mid-way)
- Data adapters: `backend/app/data/{deribit,gemini,yfin,hyperliquid,gdelt,fred,edgar,coingecko,contextdev}.py`
- Sellers (demo vendors): `backend/app/sellers/{expected_move,perps_crowding,news_sentiment,insider_cluster}.py`
- `backend/app/keeper/data/macro_calendar.json` (verify the 2026 FOMC/CPI dates against official schedules)
- `backend/tests/fixtures/` is empty: the adapter tests and fixtures were not written yet.

## Not started (remaining work, in order)
1. **Chain layer:** `chain/web3client.py` (TxSender), `chain/ausd.py` (+ startup assertion), `chain/controller.py`, `chain/faucet.py`, `chain/artifacts.py`, `tests/conftest.py` (anvil fixtures, chain id 84532), EIP-712 Python↔Solidity digest test.
2. **x402:** `x402/models.py`, `verify.py`, `settlement.py`, `server.py` (`make_paid_router`), `client.py` (registry-based requirement choice), **`preflight.py` + `agents/breaker.py`** (Update #1 C: freeze check + eth_call pre-flight circuit breaker), round-trip test on anvil, `scripts/gate_m2.py`.
3. MockAUSD still needs `pause()/unpause()` and `freeze()/unfreeze()` so the breaker is testable (it has `isAccountFrozen` only).
4. Remaining sellers: `pm_edge.py`, `macro_pulse.py`, `registry.py` (label ours "demo vendor"), `sellers/routers.py`.
5. App: `db.py`, `events.py`, `agents/llm.py`, `agents/base.py` + `sectors.py`, keeper scheduler/recharge executor/sweep, `alerts/telegram.py`, `main.py` (per `docs/API.md`), `x402/probe.py` (Update #1 E).
6. Scripts: `bootstrap.py` (never funds agents with ETH), `deploy.py`, `gate_m3.py`, `demo_e2e.py`; `Makefile`.
7. Web (`web/`, Next.js + wagmi/viem/AppKit installed): `/` dashboard (incl. agent ETH = 0, relayer gas spent), `/fund`, `/agents`, `/sources`.
8. Optional: daily vouchers (Update #1 D), README per §19 + Update #1 I.

## Local setup
```bash
cd backend && uv sync && uv run pytest -q          # Python tests
cd contracts && forge test                         # Solidity tests
cd web && npm install                              # web deps
```
Known environment note: `backend/pyproject.toml` sets `pythonpath = ["."]` so tests can import `app`.
