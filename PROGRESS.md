# PocketDesk — progress

Built during B@BHacks 2.0 on Oct 11, 2026. Branch: `claude/pocketdesk-research-desk-jequn7`.

## Status
| Check | Result |
|---|---|
| `cd contracts && forge test --offline` | **112 passed** (BudgetController incl. daily vouchers, MockAUSD ERC-3009/pause/freeze) |
| `cd backend && uv run pytest -q` | **892 passed** (chain tests run real transactions on a local anvil node, chain id 84532) |
| `cd web && npm run build` | passes: `/`, `/fund`, `/agents`, `/sources` |
| Full flow on a local chain (`make local-stack` + `make demo ENV_FILE=.env.local-anvil`) | faucet → permit + config → openBudget → session recharge → agent ticks → sweep: all transactions succeed |
| Real Base Sepolia transactions | **not run yet** — this build container has no network access to Base Sepolia; run on a machine with network (below) |

## What works
- **Contracts:** BudgetController (permit funding, daily + per-agent caps, sig-based config/pause, revoke via permit 0, **daily-voucher mode** with `redeemVoucher` + `receiveWithAuthorization`, never holds funds).
- **Payments:** x402 v2 server (demo vendors) + client: token registry (AUSD default), freeze check, ERC-3009 signing, **eth_call pre-flight + circuit breaker**, settlement only on a status-1 receipt, never charged for a failed computation.
- **Agents:** 4 sector agents, VOI gate (every bought/skipped/failed decision logged with numbers), beliefs + σ learning, findings (LLM phrasing optional), Ask box.
- **Keeper:** Thompson-sampling allocator with event boosts, recharge, voucher redemption, end-of-session sweep (agents hold 0 ETH).
- **Data / demo vendors:** expected_move (Deribit, yfinance), pm_edge (Gemini vs options), perps_crowding (Hyperliquid), news_sentiment (GDELT), insider_cluster (EDGAR), macro_pulse (FRED); honest catalog; **live x402 probe** of real vendors (unpaid requests only).
- **API + dashboard:** every endpoint in `docs/API.md` + vouchers, breaker, probe; SSE live feed; Telegram alerts (optional).

## Run on Base Sepolia (needs network)
```bash
make install
make bootstrap      # creates wallets in .env, prints addresses (never keys)
# fund RELAYER + SELLER_SETTLER with Base Sepolia ETH (agents get none)
make deploy         # BudgetController → .env
make gate-m2        # first real x402 payment (prints Basescan tx)
make demo           # full flow, prints every tx hash
make dev            # backend + dashboard
```

## Known gaps
- No real-network run yet (see above); deployed address and tx hashes to be added to the README.
- Data fixtures were written offline (shape fixtures); live API shapes verified only on a networked machine.
- `gate_m3.py` not written (the `demo` script covers the same budget → recharge flow).
- Voucher status naming differs slightly between `db.py` ("signed") and `chain/vouchers.py` ("pending"); the API maps both to "pending".
- Nansen's x402 endpoint is POST, so the GET-only probe reports it as an error; x402stock URL unverified.
