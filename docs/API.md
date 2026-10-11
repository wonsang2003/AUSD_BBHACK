# PocketDesk backend ↔ web API contract

The FastAPI backend (`backend/app/main.py`) serves this API; the Next.js dashboard (`web/`) consumes it.
All money amounts are **AUSD atomic units as integers** (6 decimals: `1_000_000` = 1 AUSD) unless the field name ends in
`_usd`. Addresses are checksummed `0x…` strings. Timestamps are ISO-8601 UTC strings. Tx links are built client-side
from `explorer_tx_url + tx_hash`.

One PocketDesk deployment serves **one user ("owner")** for the demo. The owner is set when the budget is opened
(stored in settings); endpoints that need it read it from settings unless `owner` is given.

## Read endpoints

| Method/path | Returns |
|---|---|
| `GET /api/health` | `{ok, chain_checks: "passed"\|"skipped"\|"failed: …", chain_id, ausd_address, controller_address, live_vendor_mode}` |
| `GET /api/config` | `{chain_id, ausd_address, controller_address, faucet_address, explorer_tx_url, explorer_address_url, agents: [{name, address, sector, sellers: [seller_id]}], sellers: [{seller_id, path, url, price_atomic, pay_to, description}]}` |
| `GET /api/state` | see **State** below |
| `GET /api/purchases?limit=100` | `[Purchase]` newest first (bought, skipped and failed decisions) |
| `GET /api/findings?limit=50` | `[Finding]` newest first |
| `GET /api/allocations` | `{date, budget_atomic, allocations: [{agent, amount_atomic, weight, reasons: [str]}]}` (latest) |
| `GET /api/events-today` | `[{date, kind: "earnings"\|"fomc"\|"cpi"\|"pm_expiry"\|"crypto", title, asset, boosted_agent}]` |
| `GET /api/pm-vs-options` | `[{ticker, title, asset, strike, expiry, market_p, model_p, edge, edge_after_fees, tx_hash}]` (latest pm_edge purchase) |
| `GET /api/market` | `[{symbol, price, change_24h_pct, source, delayed: bool}]` free sources only |
| `GET /api/alerts?limit=50` | `[{id, ts, level: "info"\|"warn"\|"critical", kind, message}]` |
| `GET /api/catalog` | `[CatalogEntry]` (see `backend/app/sellers/registry.py`) + `onboarding_snippet` string |
| `GET /api/settings` | `Settings` (below) |
| `GET /api/stream` | Server-Sent Events, see **SSE** |

### State
```json
{
  "owner": "0x…" | null,
  "budget": {"exists": true, "daily_cap": 0, "spent_today": 0, "remaining_today": 0, "expiry": 0,
             "paused": false, "revoked": false, "allowance_remaining": 0, "wallet_balance": 0},
  "monthly": {"cap": 0, "spent_mtd": 0},
  "runway_days": 12.5,
  "worst_case_exposure": 0,
  "agents": [{"name": "OptionsEvent", "address": "0x…", "sector": "OptionsEvent", "balance": 0,
              "allocation": 0, "spent_today": 0, "agent_cap": 0, "agent_remaining_today": 0,
              "realized_value_per_dollar": 1.4, "paused": false, "reasons": ["NVDA earnings today"]}],
  "session": {"live": true, "started_at": "…", "ends_at": "…"},
  "savings": {"mtd_spend_atomic": 0, "prorated_subscriptions_usd": 0.0, "subscriptions": [{"name": "…", "monthly_usd": 0.0}]}
}
```

### Purchase
`{id, ts, agent, seller_id, asset, params, price_atomic, evoi_usd, status: "bought"|"skipped"|"failed", reason, delta_p, tx_hash, payer, pay_to, live_vendor: bool}`

### Finding
`{id, ts, agent, asset, decision, headline, direction: "bullish"|"bearish"|"neutral"|"risk", confidence, delta_p, summary, receipts: [{seller_id, tx_hash, price_atomic}]}`

### Settings (GET and POST body)
`{owner, watchlist: [str], stakes_usd: {asset: number}, session_hours: {start: "HH:MM", end: "HH:MM", tz}, plan_days, monthly_cap_atomic, daily_cap_atomic, total_budget_atomic, agent_caps_atomic: {agent: int}, subscriptions: [{name, monthly_usd}]}`

## Write endpoints (the user signs, the relayer pays gas)

| Method/path | Body → Returns |
|---|---|
| `POST /api/faucet` | `{address}` → `{tx_hash}` (relayer calls `AUSD_FAUCET.requestFunds(address)`) |
| `POST /api/budget/prepare` | `{owner, total_budget_atomic, daily_cap_atomic, plan_days, agent_caps_atomic: {agent: int}}` → `{permit: TypedData, config: TypedData}` — the backend reads `nonces(owner)` and `cfgNonce(owner)` *now* and builds both EIP-712 messages; the web shows and signs them unchanged |
| `POST /api/budget/open` | `{owner, permit: {value, deadline, signature}, config: <config.message>, config_signature}` → `{tx_hash}` |
| `POST /api/budget/prepare-pause` | `{owner, paused}` → `{typed_data}` |
| `POST /api/budget/pause` | `{owner, paused, deadline, signature}` → `{tx_hash}` |
| `POST /api/budget/prepare-revoke` | `{owner}` → `{typed_data}` (permit with value 0) |
| `POST /api/budget/revoke` | `{owner, deadline, signature}` → `{tx_hash}` |
| `POST /api/session/start` | `{}` → `{allocations, recharge_txs: [{agent, amount_atomic, tx_hash}]}` |
| `POST /api/session/stop` | `{}` → `{sweep_txs: [{agent, amount_atomic, tx_hash}]}` |
| `POST /api/agents/{name}/pause` and `/resume` | → `{ok}` |
| `POST /api/ask` | `{question}` → `{answer, agents_used, findings: [Finding], receipts: [{seller_id, tx_hash, price_atomic}]}` |
| `POST /api/settings` | `Settings` → `Settings` |

`TypedData` = the EIP-712 "full message" `{types, primaryType, domain, message}` exactly as built by
`backend/app/chain/eip712.py` (integers may arrive as numbers or decimal strings; the web converts them to `bigint`
before calling `signTypedData`, and drops `EIP712Domain` from `types`).

Errors: HTTP 4xx/5xx with `{detail: "human readable reason"}`.

## SSE (`GET /api/stream`)

`event: <type>` + `data: <json>`. Types:
`purchase` (Purchase, includes skipped), `finding` (Finding), `allocation` (allocations payload), `recharge`
(`{agent, amount_atomic, tx_hash, reason}`), `sweep` (`{agent, amount_atomic, tx_hash}`), `alert` (Alert), `state`
(State snapshot, at most every 5 s), `market` (market rows), `ask` (`{question, answer, receipts}`), `ping` every 15 s.

## Demo wallet + manual tick (no browser wallet; testnet / local anvil only)

The server can play the user with `DEMO_USER_PRIVATE_KEY` (backend `.env`, a testnet/anvil-only key). It runs the same
prepare → sign → open code as the wallet flow; only the signatures come from the server. The key is never returned.

| Method/path | Body → Returns |
|---|---|
| `GET /api/demo/status` | → `{available: bool, reason, owner: address\|null, chain_id, local_chain: bool}` — `available` only when `DEMO_USER_PRIVATE_KEY` is a valid key AND `chain_id` is 84532 or 31337; `local_chain` = the RPC host is `127.0.0.1`/`localhost` |
| `POST /api/demo/fund` | `{mode: "permit"\|"voucher" = "permit", total_budget_atomic = 20 AUSD, daily_cap_atomic = 5 AUSD, plan_days = 7, agent_caps_atomic?: {agent: int}}` (all optional, `{}` works) → `{owner, mode, steps: [{label, tx_hash}]}`. Steps: the relayer faucets the demo user when its AUSD balance is below what the budget needs (total for permit, 7 × daily cap for vouchers), the demo user becomes the stored owner, then **permit**: permit + BudgetConfig signed with fresh nonces → `openBudget`; **voucher**: 7 daily vouchers (each = `daily_cap_atomic`) + BudgetConfig → `openVoucherBudget`, vouchers stored. 400 `{detail: "demo wallet unavailable: …"}` when not available |
| `POST /api/session/tick` | `{agent?: str}` (or no body) → `{decisions: [Purchase]}` — one buying round now for that agent (all agents when omitted), instead of waiting for the scheduler. Unknown agent → 400 |

Session preconditions (`POST /api/session/start`): 409 with a sentence the dashboard can show as-is —
`"No budget yet — fund one on F2 FUND (or click DEMO FUND)"`, `"Budget is paused — …"`, `"Budget has expired — …"`.
`POST /api/session/stop` without an owner answers the same "No budget yet" 409.
Any unexpected error inside an API handler answers 500 `{detail: "<ErrorType>: <message>"}` (still with CORS headers).
