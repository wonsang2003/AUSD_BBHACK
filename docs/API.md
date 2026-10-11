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
