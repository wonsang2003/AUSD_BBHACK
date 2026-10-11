# PocketDesk — an institutional-style research desk on a retail budget

**Fund a research budget once; a team of AI sector agents buys individual research items per call over x402,
paying in AUSD with single-use ERC-3009 signatures — and can never overspend.**

Built during **B@BHacks 2.0 on Oct 11, 2026** (Berkeley). Track: *Best Use of AUSD* (agentic payments) + *Most Technically Impressive*.

> Demo video: _TBD (≤ 2:00)_ · Live dashboard: _TBD_ · BudgetController (Base Sepolia): _TBD_ · Sample tx: _TBD_

---

## The problem

- Institutions buy research by contract (terminals, annual alt-data licenses, enterprise feeds) and pay analyst teams
  to decide what to look at. Retail traders can afford neither.
- A new buying model exists: **AI agents paying per call over x402** (HTTP 402 Payment Required) in stablecoins.
  Real vendors already sell this way (Nansen smart-money flows, CoinGecko, CoinMarketCap, x402stock — $0.01–$0.75 per call).
- But you can't safely hand an AI agent a credit card: a card is an all-or-nothing credential, the ~$0.30 fixed fee
  kills sub-$1 payments, and every vendor needs its own account.

## The solution

PocketDesk is a **buyer-side** research desk. It only buys; it never resells data.

1. **Non-custodial funding (ERC-2612 permit).** Money stays in your wallet. You sign one permit that lets our
   `BudgetController` pull a capped amount — only to *your own registered agent wallets*, within a contract-enforced
   daily cap and per-agent caps. Revoke any time (permit value 0).
2. **"Maximum safety" mode — daily vouchers (ERC-3009 `receiveWithAuthorization`).** Instead of a permit you sign 7
   daily vouchers. Each is valid only on its own UTC day, only for that day's amount, and only redeemable by our
   contract into your agents. **Even a fully compromised keeper or backend cannot pull more than today's voucher** —
   the limit lives in *your* signatures. Future days can be cancelled with `cancelAuthorization`.
3. **Bounded-loss agent wallets (ERC-3009 `transferWithAuthorization`).** Each agent holds only its daily allocation
   and **zero ETH**. Every payment is a single-use, amount-bound, recipient-bound, time-bound signature. Worst case if
   an agent is prompt-injected or hallucinates: one agent's daily allowance.
4. **Compliance + circuit breaker.** Before every payment the agent checks the AUSD freeze list for both parties and
   **pre-flight simulates** the exact signed transfer with `eth_call`. If it would revert, nothing is sent. A *pause*
   revert trips a global breaker ("TOKEN PAUSED — agents halted") that re-probes every 60 s and resumes automatically.
5. **Value-of-information gate.** An agent buys an item only if its expected value for the decision at hand beats the
   price — and logs every skip ("skipped: EVOI $0.004 < price $0.05 × 1.5").
6. **Budget allocator.** A keeper splits the daily budget across agents by realized value per dollar (Thompson
   sampling), boosts agents with events today (earnings, CPI, FOMC, contract expiries), and sweeps unspent balances back
   to you at session close (agent signs, relayer pays gas).

## Architecture

```
 User wallet (WalletConnect / injected)
   │  signs ERC-2612 Permit + BudgetConfig  — or —  7 daily ReceiveWithAuthorization vouchers   (no gas)
   ▼
 Relayer / keeper (Python, the only wallet with ETH besides the seller settler)
   │  openBudget / openVoucherBudget / recharge / redeemVoucher / sweeps / faucet
   ▼
 BudgetController (Base Sepolia) — daily cap, per-agent caps, pause, expiry; never holds funds
   │  transferFrom(owner → agent)   or   receiveWithAuthorization(owner → controller → agents)
   ▼
 Agent wallets (4 sector agents, AUSD only, 0 ETH)
   │  x402: GET → 402 → freeze check → sign ERC-3009 → eth_call pre-flight → retry with PAYMENT-SIGNATURE
   ▼
 Vendors (x402 sellers; our testnet "demo vendors" settle transferWithAuthorization themselves)

 FastAPI backend: demo vendors · agents · VOI planner · allocator · keeper · breaker · SQLite event log
      └─ SSE → Next.js dashboard          └─ Telegram alerts
 Data inputs: Deribit, Gemini Predictions, Hyperliquid, GDELT, FRED, SEC EDGAR, CoinGecko, yfinance (delayed), Context.dev
```

## How each standard is used

| Standard | Where | What it buys us |
|---|---|---|
| **ERC-2612** `permit` | Flexible funding mode | Non-custodial, revocable budget allowance; user pays no gas |
| **ERC-3009** `transferWithAuthorization` | Every agent payment (x402 `exact` scheme) | Gasless, single-use, amount/recipient/time-bound payments; agents hold 0 ETH |
| **ERC-3009** `receiveWithAuthorization` | Daily vouchers (Maximum-safety mode) | Daily limit enforced by the user's own signature; only our contract can redeem |
| **ERC-3009** `cancelAuthorization` | "Cancel future days" | Revoke remaining vouchers without trusting us |
| **x402 v2** | Agent ↔ vendor | `PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` / `PAYMENT-RESPONSE` headers; v1 bodies parsed too |
| **EIP-712 / ERC-5267** | All signatures | Startup check reads `eip712Domain()` + `decimals()` and refuses to run on mismatch |
| **AUSD freeze list + pause** | Every payment | Compliance check + circuit breaker |

## The value-of-information gate

Each agent keeps a belief `p` for each decision (e.g. "Gemini BTC > $X contract is underpriced"), an action threshold
`τ`, and the user's typical stake `S`. Buying item *i* moves the belief by `ε ~ N(0, σᵢ²)`. Acting is worth
`S·max(p − τ, 0)`, so the expected value of the information is the *expected improvement*:

```
m = p − τ,   z = |m| / σᵢ
EVOI_i = S · σᵢ · [ φ(z) − z·(1 − Φ(z)) ]
buy  iff  EVOI_i ≥ price_i × 1.5   (and balance, monthly cap, cache allow it)
```

EVOI is highest when the belief is near the threshold and the source is informative. `σᵢ` is learned online
(EMA of realized Δp², prior 0.08). The closed form is unit-tested against Monte Carlo (≤ 2 %).

## The allocator

`B_day = min(dailyCap, allowance / days_left, monthly_cap_left) + rollover (≤ 25 % of yesterday's unspent)`.
Per agent, value-per-dollar ~ LogNormal(μ, s²), updated from realized values. Each morning: draw θₖ, weight
`wₖ = θₖ · (1 + 0.5 · eventsₖ)`, normalize, clip to [10 %, 50 %] and renormalize; every allocation carries its
reasons ("OptionsEvent +40 %: NVDA earnings today"). Invariants (Σ ≤ budget, floors/caps, determinism) are tested.

## Safety model

| Threat | Bound |
|---|---|
| Agent prompt-injected / hallucinating | At most that agent's balance today (single-use, bounded ERC-3009 authorizations; price cap per item) |
| Keeper / backend compromised (permit mode) | Contract daily cap + per-agent caps; only registered agents can receive |
| Keeper / backend compromised (voucher mode) | **Today's voucher only** — enforced by the user's signature, not by us |
| Token paused / signature verification paused | Pre-flight `eth_call` → global circuit breaker, nothing sent |
| Frozen counterparty | Freeze-list check before signing; demo vendors check the payer too |
| Overnight exposure | End-of-session sweep returns every agent balance to the owner |
| Gas | Agents hold 0 ETH; the relayer pays and the dashboard shows its spend |

## Data catalog (honest)

| Tier | Data | Source | How it is paid |
|---|---|---|---|
| S | On-chain smart-money flows | Nansen x402 | $0.01–$0.05 USDC on Base mainnet (flagged; live probe shows the 402) |
| S | Options-implied expected move, crossing probabilities | **demo vendor** `expected_move` (Deribit, yfinance delayed) | 0.05 AUSD, x402 on Base Sepolia |
| S | News + event detection with tone | **demo vendor** `news_sentiment` (GDELT, Context.dev) | 0.03 AUSD |
| S | Insider buy clusters (Form 4) | **demo vendor** `insider_cluster` (SEC EDGAR) | 0.03 AUSD |
| S | Social firehose | X API pay-per-use | metered API key (roadmap) |
| A | Prediction-market vs options mispricing | **demo vendor** `pm_edge` (Gemini Predictions vs Deribit) | 0.10 AUSD |
| A | Perps crowding (funding, OI) | **demo vendor** `perps_crowding` (Hyperliquid) | 0.02 AUSD |
| A | Macro pulse (CPI, yields, Fed funds, calendar) | **demo vendor** `macro_pulse` (FRED) | 0.02 AUSD |
| A | Crypto prices / DEX data | CoinGecko x402, CoinMarketCap x402 | $0.01 USDC (flagged) |
| A | Earnings packages, sentiment gauges | x402stock | $0.02–$0.75 USDC (flagged) |
| B | Flights, web monitoring, tick history, weather | FlightAware, Context.dev, Databento, Open-Meteo | metered / free |
| — | **Terminal data, consensus estimates, real-time feeds licensed for redistribution, card panels, satellite, expert calls** | — | **Not sold per call today.** We say so. |

Our demo vendors sell **derived analytics computed live from public data** — testnet stand-ins so the agents have
something real to pay for. Every purchase goes directly from the user's agent to the vendor, so the user is the buyer
and licensee.

## Why AUSD

**The safety model works with any ERC-3009 + ERC-2612 stablecoin. That makes it a standard, not a gimmick.**
PocketDesk runs budgets in AUSD because it uses six AUSD capabilities together:

1. ERC-2612 permit for non-custodial budgets.
2. ERC-3009 for gasless, bounded agent payments.
3. Time-windowed `receiveWithAuthorization` daily vouchers that even our own keeper can't exceed.
4. `cancelAuthorization` to revoke future days.
5. The freeze list and pause controls as compliance checks plus a circuit breaker.
6. The same token address on every EVM chain.

Business model: Agora's partner economics could fund a free retail tier (subject to Agora's partner terms), and its
white-label path could later issue branded desk credits. Neither is built today. AUSD holders do not earn yield, and
PocketDesk makes no yield claims.

## FAQ

- **Isn't this data redistribution? / Do you sell data?** No. PocketDesk only buys. Each purchase goes directly from
  the user's agent to the vendor over x402, so the user is the licensee. Our testnet vendors are demo stand-ins.
- **Couldn't this be USDC?** Yes — the mechanism is token-agnostic, and our client already understands USDC-priced x402
  vendors (see the live probe on `/sources`). The token registry verifies every token on-chain at startup and the client
  never signs for a token outside it. We default to AUSD for the reasons above.
- **What stops a hijacked agent or keeper?** Contract caps (permit mode) or the user's own daily voucher signatures
  (max-safety mode), single-use bounded ERC-3009 authorizations, a pre-flight simulation before every payment, the
  freeze check, and a sweep at session end.
- **Why not just use x402stock or Nansen directly?** They are vendors; PocketDesk is the buyer-side desk that decides
  which vendor to buy from, when, and how much, under a hard budget.
- **Is premium data available?** Some premium data is sold per call today (e.g. Nansen smart money). For equities we
  compute derived analytics from public data. We are explicit about what is *not* sold per call.

## Run it

### Offline (local chain, no testnet funds needed)
```bash
make install
make test                 # Foundry + pytest (chain tests run on a local anvil node with MockAUSD)
make local-stack          # anvil + MockAUSD + BudgetController, writes .env.local-anvil
make server               # FastAPI backend
make web                  # Next.js dashboard on http://localhost:3000
```

### Base Sepolia (real AUSD testnet)
```bash
make bootstrap            # creates testnet wallets in .env (prints addresses, never keys)
# send ~0.01 Base Sepolia ETH to the relayer and seller-settler addresses it prints (agents get none)
make deploy               # deploys BudgetController, writes its address to .env
make gate-m2              # first real x402 payment: agent → demo vendor (prints the Basescan tx)
make gate-m3              # permit + config → openBudget → allocator → recharge (+ a cap violation that reverts)
make dev                  # backend + dashboard
make demo                 # scripted end-to-end run printing every tx hash
```
Code reading order: [`docs/CODE_TOUR.md`](docs/CODE_TOUR.md). Full spec: [`docs/SPEC.md`](docs/SPEC.md). API: [`docs/API.md`](docs/API.md).

## Demo video order

1. **Payments first** — dashboard live: agents buy items over x402 with AUSD, Basescan links appear, agent cards show
   ETH = 0, one item is skipped by the VOI gate.
2. **Problem** — institutions buy by contract; retail can't hand an AI a card; per-call x402 vendors exist (live probe).
3. **Funding + safety** — WalletConnect QR → permit (Flexible) or 7 daily vouchers (Max safety); compliance badge;
   circuit-breaker banner.
4. **Value** — a finding (Gemini contract vs options-implied probability), Ask-box answer with receipts, savings meter.
5. **Close** — "An institutional-style research desk on a retail budget. Agents that pay like an API call, and can
   never overspend."

## Roadmap (not built)

- Vendor onboarding kit: any data vendor sells to retail agents in AUSD with our x402 middleware.
- Paying USDC-only vendors from an AUSD budget through a swap router.
- Cross-chain: AUSD and its faucet have the same address on Arbitrum Sepolia ("one token config, any chain").
