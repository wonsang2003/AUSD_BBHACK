# PocketDesk — technical spec (source of truth for builders)

PocketDesk: an institutional-style research desk on a retail budget. A retail user funds a research budget once
(ERC-2612 permit → our `BudgetController`); sector **agents** buy individual research items per call over **x402**
paying **AUSD** with **ERC-3009** signed authorizations; a **VOI gate** decides what is worth buying; a **keeper**
allocates the daily budget across agents and recharges/sweeps agent wallets. Live dashboard + Telegram alerts.

Hackathon rules: no hard-coded core functionality, real testnet transactions, no mocks in the payment path.
**Readability is a hard requirement**: module docstring at the top of every file explaining what it does and how it
fits the flow; type hints; named constants (`app/constants.py`); short functions with docstrings that state the formula
or rule; no clever one-liners. Every backend module has a matching `backend/tests/test_<module>.py` (pytest,
Arrange/Act/Assert, descriptive test names such as `test_recharge_reverts_when_daily_cap_exceeded`).

Already written (reuse, do not duplicate): `backend/app/constants.py`, `backend/app/config.py` (`get_settings()`),
`backend/app/chain/eip712.py` (all typed-data builders: `build_permit`, `Authorization`, `new_authorization`,
`build_transfer_with_authorization`, `build_receive_with_authorization`, `BudgetConfig`, `build_budget_config`,
`build_set_paused`, `sector_to_bytes32`, `sign_typed_data`, `recover_typed_data_signer`, `typed_data_digest`).

Tooling in this container: `uv` (run `cd backend && uv run pytest`), Foundry at `/root/.foundry/bin` (`forge`, `anvil`,
`cast`; add to PATH), solc 0.8.24 already cached at `/root/.svm/bin/solc-0.8.24` (if forge tries to download solc,
set `solc = "/root/.svm/bin/solc-0.8.24"` via `FOUNDRY_SOLC` env var). Node 22 + npm. **No network** to Base Sepolia or
market-data APIs from this container — data adapters are tested with recorded fixture payloads; chain code is tested
against a local `anvil` started with `--chain-id 84532`.

---------------------------------------------------------------------------------------------------------------------

## 1. Verified chain facts

- Base Sepolia, chainId 84532, CAIP-2 `eip155:84532`. Explorer https://sepolia.basescan.org
- AUSD testnet `0xa9012a055bd4e0eDfF8Ce09f960291C09D5322dC`, **decimals 6**, EIP-712 domain **name "Agora Dollar"**,
  version "1" (`name()` returns "AUSD" but signing uses "Agora Dollar"). Read `eip712Domain()` (ERC-5267) and
  `decimals()` at backend startup and refuse to start on mismatch.
- AUSD supports ERC-20, ERC-3009 (`transferWithAuthorization`, `receiveWithAuthorization`, `cancelAuthorization`,
  `authorizationState(address,bytes32)`; both `(v,r,s)` and `bytes signature` overloads), ERC-2612 (`permit`,
  `nonces`), ERC-1271, ERC-5267. Accounts can be frozen (`isAccountFrozen(address)`); handle reverts.
- Faucet `0xd236c18D274E54FAccC3dd9DDA4b27965a73ee6C`: `requestFunds(address recipient)` mints 10,000 test AUSD to
  `recipient`; caller pays gas → the relayer calls it for users.
- ERC-3009: random bytes32 nonces; valid iff `validAfter < block.timestamp < validBefore`; use
  `validAfter = now - 30`, `validBefore = now + maxTimeoutSeconds`. `transferWithAuthorization` can be submitted by
  anyone; `receiveWithAuthorization` requires `msg.sender == to`.
- ERC-2612: `Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)`; read `nonces(owner)`
  right before signing; revoke = sign a new permit with value 0.
- Gas: only the relayer/keeper wallet and the seller-settler wallet hold Base Sepolia ETH. Users and agents never do.

## 2. Contract: `contracts/src/BudgetController.sol` (Solidity 0.8.24, OZ v5.1, Foundry)

Purpose: a user grants a capped, revocable allowance (ERC-2612) that can only flow to that user's registered agent
wallets, within a daily cap and per-agent daily caps. The contract never holds funds.

```solidity
IERC20 public immutable ausd;           // also IERC20Permit
address public keeper;                  // relayer allowed to call openBudget/recharge/...WithSig
address public admin;                   // deployer; can setKeeper
struct Budget { uint128 dailyCap; uint128 spentToday; uint64 dayIndex; uint64 expiry; bool paused; bool exists; uint256 cfgNonce; }
struct Agent  { bool active; uint128 dailyCap; uint128 spentToday; uint64 dayIndex; bytes32 sector; }
mapping(address => Budget) public budgets;
mapping(address => mapping(address => Agent)) public agents;   // owner => agent => cfg
mapping(address => address[]) internal agentList;               // owner => registered agents (for deactivation + views)
```

EIP-712 domain: name "PocketDesk", version "1". Typed structs (must match `backend/app/chain/eip712.py` exactly):
```
BudgetConfig(address owner,uint128 dailyCap,uint64 expiry,address[] agentAddrs,uint128[] agentCaps,bytes32[] sectors,uint256 nonce)
SetPaused(address owner,bool paused,uint256 nonce,uint256 deadline)
```
Array members are hashed as `keccak256(abi.encodePacked(array))` (each element padded to 32 bytes — `abi.encodePacked`
of a dynamic array of 32-byte-padded static types does this). `SetPaused.nonce` uses the same `cfgNonce` counter.

Functions:
- `constructor(address ausd, address keeper)`; admin = msg.sender.
- `openBudget(address owner, uint256 permitValue, uint256 permitDeadline, uint8 v, bytes32 r, bytes32 s, BudgetConfig calldata cfg, bytes calldata cfgSig)` — onlyKeeper. Call `IERC20Permit(ausd).permit(owner, address(this), permitValue, permitDeadline, v, r, s)` inside try/catch (front-run safe); afterwards require `allowance(owner, this) >= cfg.dailyCap` (custom error `InsufficientAllowance`). Verify cfgSig signer == owner == cfg.owner (ECDSA, OZ `EIP712` + `SignatureChecker` so ERC-1271 smart wallets also work), `cfg.nonce == budgets[owner].cfgNonce`, then increment. Validate arrays same length, agent != 0, agentCap <= dailyCap. Store budget + agents (deactivate previously registered agents not listed). Emit `BudgetOpened(owner, dailyCap, expiry, agentCount)`.
- `updateConfigWithSig(BudgetConfig calldata cfg, bytes calldata sig)` — onlyKeeper; same verification; updates caps/agents (deactivate not-listed); keeps spentToday. Emit `ConfigUpdated`.
- `recharge(address owner, address agent, uint128 amount)` — onlyKeeper, nonReentrant. Require budget exists, not paused, `block.timestamp < expiry`, agent active. Roll the day (`dayIndex = uint64(block.timestamp / 1 days)`; reset spentToday on new day for budget and agent). Require `spentToday + amount <= dailyCap` and `agent.spentToday + amount <= agent.dailyCap`. `SafeERC20.safeTransferFrom(ausd, owner, agent, amount)`. Emit `Recharged(owner, agent, amount, spentToday, agentSpentToday)`.
- `setPaused(bool paused)` — called by owner directly. `setPausedWithSig(address owner, bool paused, uint256 deadline, bytes calldata sig)` — onlyKeeper, uses `SetPaused` typed data + cfgNonce. Emit `Paused(owner, paused)`.
- `setKeeper(address)` — admin only. Emit `KeeperChanged`.
- Views: `remainingToday(owner)`, `agentRemainingToday(owner, agent)` (account for day rollover in the view), `getAgents(owner) returns (address[] memory)`, `domainSeparator()`, `hashBudgetConfig(BudgetConfig) returns (bytes32 digest)` (the full EIP-712 digest — used by the cross-language test).
- Custom errors for every revert (NotKeeper, NotAdmin, BadSignature, BadNonce, NoBudget, BudgetPaused, BudgetExpired, AgentInactive, DailyCapExceeded, AgentCapExceeded, LengthMismatch, ZeroAddress, AgentCapAboveDailyCap, InsufficientAllowance, SignatureExpired).
- Revoke: the frontend has the user sign a permit with value 0; the keeper submits `ausd.permit(...)` directly.

Test token: `contracts/src/mocks/MockAUSD.sol` — OZ `ERC20` + `ERC20Permit` with name "AUSD" but **EIP-712 domain name
"Agora Dollar"**, version "1", decimals 6, plus a faithful ERC-3009 implementation (`transferWithAuthorization` and
`receiveWithAuthorization` with both `(v,r,s)` and `bytes signature` overloads, `cancelAuthorization`,
`authorizationState`, events `AuthorizationUsed`/`AuthorizationCanceled`, strict `validAfter < now < validBefore`,
`receive` requires `msg.sender == to` (error `InvalidPayee`)), `mint(address,uint256)` open to anyone (testnet mock),
and `requestFunds(address)` minting 10,000 AUSD (so the same faucet code path works locally). ERC-5267 `eip712Domain()`
comes from OZ EIP712. Used only on the local anvil chain in tests — never deployed as "AUSD" on a public chain.

Foundry tests (`contracts/test/BudgetController.t.sol`): daily cap enforced; per-agent cap enforced; day rollover
resets both; unregistered agent reverts; non-keeper reverts; paused reverts; expired reverts; bad config signature
reverts; replayed nonce reverts; permit flow (6 decimals); contract never holds a balance; front-run permit still
opens; setPausedWithSig; updateConfigWithSig deactivates dropped agents. Also `contracts/test/MockAUSD.t.sol` for 3009.

Deploy: `contracts/script/Deploy.s.sol` (constructor `(AUSD, keeper)`; reads `AUSD_ADDRESS`, `KEEPER_ADDRESS`, uses
`--private-key` / env `RELAYER_PRIVATE_KEY`). Also `backend/scripts/deploy.py` (deploy from the committed artifact
with web3.py) so the user can deploy without forge. ABI + bytecode are exported to
`backend/app/chain/abi/{BudgetController,MockAUSD}.json` as `{"abi": [...], "bytecode": "0x..."}` by
`contracts/script/export_abi.sh` and committed (backend + tests then need only anvil, not forge).

## 3. x402 v2 (HTTP transport) — `backend/app/x402/`

1. Unpaid request → **402** with header `PAYMENT-REQUIRED`: base64(JSON)
   `{x402Version:2, error, resource:{url,description,mimeType}, accepts:[{scheme:"exact", network:"eip155:84532", amount:"<atomic>", asset:"<AUSD>", payTo:"<seller>", maxTimeoutSeconds, extra:{name:"Agora Dollar", version:"1", assetTransferMethod:"eip3009"}}]}`.
   Also put the same JSON in the 402 response body.
2. Client retries with header `PAYMENT-SIGNATURE`: base64(JSON)
   `{x402Version:2, resource, accepted:<chosen requirement>, payload:{signature:"0x…", authorization:{from,to,value,validAfter,validBefore,nonce}}}`
   (integers as decimal strings).
3. Server verifies: signature recovers `from`; `to == payTo`; `value >= amount`; `validAfter < now < validBefore` with
   ≥ 6 s of headroom before validBefore; nonce unused (`authorizationState`); balance ≥ value; asset + network +
   scheme match; account not frozen. Then computes the response, **settles** (self-settlement: submit
   `transferWithAuthorization(from,to,value,validAfter,validBefore,nonce,signature)` from the settler key and wait for
   the receipt), and only if the receipt has status 1 returns 200 with the data and header `PAYMENT-RESPONSE`:
   base64(JSON) `{success:true, transaction:"0x…", network, payer}`. On failure: 402 with
   `{success:false, errorReason, transaction?, network, payer}` in `PAYMENT-RESPONSE` and no data.
- Optionally accept legacy v1 `X-PAYMENT` on the server (respond with `X-PAYMENT-RESPONSE` too).
- Facilitator mode (`X402_SETTLEMENT=facilitator`): POST `{x402Version, paymentPayload, paymentRequirements}` to
  `/verify` and `/settle` at `X402_FACILITATOR_URL`. Default is self.
- Client rule: **never retry a payment with the same nonce after an ambiguous result** — check
  `authorizationState(from, nonce)` first; if used, the payment went through (fetch receipt / report); if not, sign a
  fresh authorization (new nonce) for the retry.

### Interfaces (other modules code against these — keep the names)

`app/x402/models.py`
```python
class ResourceInfo(BaseModel): url: str; description: str; mimeType: str = "application/json"
class PaymentRequirements(BaseModel): scheme="exact"; network: str; amount: str; asset: str; payTo: str; maxTimeoutSeconds: int; extra: dict
class PaymentRequired(BaseModel): x402Version: int = 2; error: str | None; resource: ResourceInfo; accepts: list[PaymentRequirements]
class ExactEvmPayload(BaseModel): signature: str; authorization: dict   # x402 JSON form of eip712.Authorization
class PaymentPayload(BaseModel): x402Version: int = 2; resource: ResourceInfo | None; accepted: PaymentRequirements; payload: ExactEvmPayload
class SettlementResponse(BaseModel): success: bool; transaction: str | None; network: str; payer: str | None; errorReason: str | None = None
def encode_header(model: BaseModel) -> str     # base64(JSON, exclude_none)
def decode_header(value: str, model_cls)        # inverse
```
`app/x402/server.py`
```python
@dataclass(frozen=True)
class SellerOffer:
    seller_id: str            # e.g. "expected_move"
    path: str                 # e.g. "/sell/expected-move"
    description: str
    price_atomic: int         # AUSD atomic units (6 decimals)
    pay_to: str               # seller payout address
    max_timeout_seconds: int = 120
    cache_ttl_seconds: int = 120

def make_paid_router(offer: SellerOffer, compute: Callable[[dict], Awaitable[dict]]) -> APIRouter
    # GET offer.path → x402 gate → compute(query_params) → settle → JSON + PAYMENT-RESPONSE.
    # compute results are cached per (seller_id, sorted params) for cache_ttl_seconds (cache hits still require payment).
class PaymentVerifier  / class SelfSettler / class FacilitatorSettler   # injected; tests can pass a local anvil web3
```
`app/x402/client.py`
```python
@dataclass
class PaidResult: data: dict; price_atomic: int; tx_hash: str | None; payer: str; pay_to: str; seller_url: str; settlement: SettlementResponse
class PaymentError(Exception): reason: str; tx_hash: str | None
class X402Client:
    def __init__(self, *, private_key: str, chain_id: int, asset: str, max_amount_atomic: int, http: httpx.AsyncClient | None = None, w3=None)
    async def get(self, url: str, params: dict | None = None) -> PaidResult
```

## 4. Sellers (`backend/app/sellers/`) — real computation from public data

Each seller module exposes `OFFER: SellerOffer` (price, pay_to from settings, path) built by `make_offer()`,
`async def compute(params: dict) -> dict`, and `router = make_paid_router(OFFER, compute)`. Pure math/parse functions
are separate from fetching so they can be unit-tested with fixture payloads in `backend/tests/fixtures/*.json`.
Every response includes `inputs_used`, `computed_at` (ISO UTC), `method`, `confidence` (0–1), and `data_delay` where
relevant. Upstream calls: timeout, retries with backoff, in-process cache (60–300 s), per-source rate limit.

1. `expected_move` — 0.05 AUSD. `GET /sell/expected-move?asset=BTC|ETH|SOL|<equity>&expiry=<YYYY-MM-DD|next>&strikes=K1,K2`
   Crypto: Deribit `public/get_book_summary_by_currency?currency=BTC&kind=option` + `public/get_index_price?index_name=btc_usd`; parse instrument names `BTC-27DEC26-100000-C`; pick the expiry, ATM strike nearest the index, `mark_iv` (percent → /100). Expected move = `S·σ·√T` (T in years, ACT/365). `P(S_T > K) = N(d2)`, `d2 = (ln(S/K) − σ²T/2)/(σ√T)`, r = 0. Equities: yfinance option chain (unofficial, delayed): straddle-implied move = (ATM call mid + ATM put mid)/S; implied σ from ATM `impliedVolatility`; flag `data_delay: "15m+ delayed (yfinance)"`.
2. `pm_edge` — 0.10 AUSD. `GET /sell/pm-edge?category=crypto`. Gemini Predictions public API (no key): `GET https://api.gemini.com/v1/prediction-markets/events?status=active&category=crypto`, then `/v1/prediction-markets/events/{ticker}` for contracts and prices (prices are strings; take instrument symbols from the API, never construct them). Parse strike/expiry/asset from crypto contracts ("BTC above $X on <date>"). Compare YES price (market probability) with options-implied `N(d2)` from Deribit at a matching expiry (interpolate total variance σ²T linearly in time between the two bracketing expiries). Edge after Gemini fees: taker fee = `ceil_to_cent(0.07 · C · P · (1−P))` per contract (C = contracts, P = price). Return list sorted by |edge|.
3. `perps_crowding` — 0.02 AUSD. `GET /sell/perps-crowding?assets=BTC,ETH,SOL`. Hyperliquid `POST https://api.hyperliquid.xyz/info {"type":"metaAndAssetCtxs"}` → funding, openInterest, markPx per asset (zip `universe` names with ctxs). Crowding score = z-score of funding and of OI change over a rolling in-memory window; on cold start, cross-sectional z-scores across all listed assets. Label: crowded long / crowded short / neutral.
4. `news_sentiment` — 0.03 AUSD. `GET /sell/news-sentiment?q=<asset or ticker>`. GDELT DOC 2.0 API (`https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=artlist&format=json&timespan=24h&maxrecords=50` and `mode=timelinetone`). Optional Context.dev search if `CONTEXT_DEV_API_KEY` (mode "fast" only). Return top headlines, tone trend (mean tone first half vs second half of window), event flags by keyword (hack, exploit, listing, delisting, lawsuit, SEC, guidance, downgrade, upgrade, ETF, halt, bankruptcy), and a short summary (LLM via `app/agents/llm.py` if available, else template).
5. `insider_cluster` — 0.03 AUSD. SEC EDGAR Form 4 (`https://data.sec.gov/submissions/CIK##########.json`, ticker→CIK via `https://www.sec.gov/files/company_tickers.json`). **EDGAR requires `User-Agent` from `SEC_USER_AGENT`** and ≤ 10 req/s. Cluster = ≥ 2 distinct insiders with open-market purchases (code P) within 10 days, with dollar totals (parse Form 4 XML).
6. `macro_pulse` — 0.02 AUSD. FRED CSV without key `https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL` (+ FEDFUNDS, DGS2, DGS10, UNRATE): latest values, YoY CPI, 2s10s slope; next scheduled release dates from `backend/app/keeper/data/macro_calendar.json` (static official 2026 FOMC/CPI schedule); Gemini economics-category odds where available.

`registry.py`: the full data catalog (ours + external x402 + metered + free + "not per call") with fields `id, sector,
description, trader_value (S/A/B), source, pricing_model, payable_mode (x402_ausd_testnet | x402_usdc_mainnet |
metered_api_key | free), price, status (live_ausd_seller | external_x402_flagged | metered_adapter_roadmap | free_input |
not_per_call)`. Rows: Nansen smart money ($0.01/$0.05 USDC), our expected_move, Databento OPRA, X API pay-per-use
($0.005/post), our news_sentiment, our insider_cluster, x402stock earnings ($0.05/$0.75), CoinGecko x402 $0.01,
CoinMarketCap x402 $0.01 beta, our perps_crowding, x402stock sentiment $0.02, EDGAR filings free, our macro_pulse, our
pm_edge, FlightAware AeroAPI, Context.dev web data, Databento ticks, Open-Meteo weather free, and the "not per call" row
(terminal data, consensus estimates, real-time feeds for redistribution, card panels, satellite, expert calls).
External x402 vendors (`app/data/x402_external.py`) are behind `LIVE_VENDOR_MODE=false`, use a separate wallet with
hard caps (≤ $1 total, ≤ $0.10 per call) enforced in code, and must never run without explicit user approval.

## 5. Agents, beliefs, VOI (`backend/app/agents/`)

Sector agents (one EOA each): OnchainPerps (perps_crowding, news_sentiment), OptionsEvent (expected_move, pm_edge),
NewsSentiment (news_sentiment), MacroFilings (macro_pulse, insider_cluster).

Belief state per (agent, asset, decision): probability `p`, threshold `τ` (default 0.5), stake `S_usd` (user's typical
position size). Update rules (one module `beliefs.py`, unit tested): pm_edge sets `p` from model-vs-market gap;
perps_crowding shifts `p` by logistic of crowding z; news_sentiment shifts by tone delta; expected_move sets p from
N(d2) etc. Record realized `|Δp|` per purchase.

EVOI (`voi.py`): post-purchase belief `p' = p + ε`, `ε ~ N(0, σ_i²)`. `m = p − τ`, `z = |m|/σ_i`,
`EVOI_i = S · σ_i · [φ(z) − z·(1 − Φ(z))]` (expected improvement). Buy iff `EVOI_i ≥ price_i × margin` (margin 1.5),
balance available, monthly cap not hit, not cached (same seller+params within TTL). Rank by `EVOI_i/price_i`. Log every
decision (bought and skipped) with numbers. σ learning: EMA of Δp² per (seller, decision type), prior σ₀ = 0.08, floor
0.01. Realized value per $ of an agent = Σ(EVOI ex post using realized |Δp|) / Σ spend. Test EVOI vs Monte Carlo ≤ 2%.

Agent loop: runs during session hours; each tick refresh free inputs, generate candidates (sellers × watchlist), compute
EVOI, buy greedily within balance via `X402Client`, update beliefs, emit a Finding when a belief crosses τ or moves > δ
(0.05). Finding = asset, headline, direction, confidence p, Δp, receipts (tx hashes), plain-language summary (LLM phrases
only; numbers come from code; template fallback). Ask box: question → keyword router (+ optional LLM) picks agents and
assets → one focused tick → answer composed from findings with receipts. Payment failures → agent backoff + alert.

## 6. Allocator + keeper (`backend/app/keeper/`)

Daily budget `B_day = min(dailyCap, allowance_remaining / days_remaining, monthly_cap_remaining)` + capped rollover
(≤ 25 % of yesterday's unspent). Thompson sampling: per agent value-per-$ ~ LogNormal(μ_k, s_k²), prior μ=log(1), s=1,
updated from realized values (conjugate normal update on log values with known noise). Draw θ_k, weight
`w_k = θ_k · (1 + β·events_k)` (β = 0.5), normalize, enforce floor 10 % / cap 50 % by iterative clipping +
renormalization, `a_k = B_day · w_k`; record reasons ("OptionsEvent +40%: NVDA earnings today"). Invariants (tested):
Σa_k ≤ B_day; floors and caps hold; deterministic under a fixed seed.

Recharge policy: session start `top_up = max(0, a_k − balance_k)` → `controller.recharge`; intraday refill if
`balance_k < 20 %·a_k` AND realized value/$ today ≥ threshold AND headroom → refill `min(0.25·a_k, headroom)` with
reason; end of session sweep: agent signs ERC-3009 `transferWithAuthorization(to=owner)`, keeper submits it.

Alerts: runway < 3 days (`allowance_remaining / avg_daily_spend`), agent paused after failed payments, daily cap
reached, revocation detected, daily brief at session close (spend by agent, top findings, VOI skips, savings meter).

Event calendar (`calendar.py`): earnings for watchlist equities (yfinance), FOMC/CPI dates (static JSON of official 2026
schedules), Gemini event expiries for watchlist assets.

## 7. Backend app

FastAPI `app/main.py`: mounts seller routers; REST `/api/state` (budget, allocations, balances, runway, safety),
`/api/stream` (SSE of events), `/api/purchases`, `/api/findings`, `/api/catalog`, `/api/events-today`,
`/api/faucet` (relayer calls faucet for a user), `/api/budget/open` (permit + config sigs → relayer `openBudget`),
`/api/budget/pause` (signed SetPaused), `/api/budget/revoke` (signed permit value 0), `/api/ask`, `/api/session/start`,
`/api/session/stop` (sweep), `/api/settings` (watchlist, stakes, subscriptions for savings meter). SQLite via SQLModel
(`db.py`: events, purchases incl. skipped decisions, findings, allocations, alerts). `events.py`: in-process pub/sub → SSE.
Startup: assert AUSD decimals + domain unless `SKIP_CHAIN_CHECKS=true`.

## 8. Gotchas

AUSD signing domain "Agora Dollar"/"1"; decimals 6. ERC-3009 nonces random; strict window; check
`authorizationState` before resubmitting. Permit: read `nonces(owner)` immediately before signing; try/catch in
openBudget. EIP-712 arrays per spec, cross-language tested. Never treat a payment as final without a receipt; show
failures honestly. Keys: hot keys server-side, testnet only; never logged; never sent to the browser.
