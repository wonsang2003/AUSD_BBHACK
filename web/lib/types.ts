/**
 * JSON types of the PocketDesk backend API, as consumed by the dashboard.
 *
 * Source of truth: `docs/API.md` (backend ↔ web contract) plus SPEC §9 Update #1 (breaker,
 * live x402 probe, zero-gas agents, daily vouchers). Field names are the backend's snake_case
 * JSON names, unchanged, so a payload can be read next to the Python that produced it.
 *
 * Units (same rules as API.md):
 *   * `*_atomic` and other AUSD amounts — integers in AUSD atomic units (6 decimals:
 *     1_000_000 = 1 AUSD), sent as JSON numbers;
 *   * `*_wei` — ETH amounts in wei as DECIMAL STRINGS (wei overflows JS numbers);
 *   * `*_usd` — plain dollar floats;
 *   * timestamps — ISO-8601 UTC strings unless the name says otherwise.
 *
 * Types marked "(proposed)" are not in API.md yet: the web codes against these shapes, so the
 * backend endpoints should return exactly them (or this file changes together with the backend).
 */

import type { FullMessage } from "./eip712";

export type Atomic = number;
export type WeiString = string;
export type IsoTime = string;
export type TxHash = string;
export type AddressString = string;

/** An EIP-712 full message exactly as built by `backend/app/chain/eip712.py`. */
export type TypedDataJson = FullMessage;

// ─── read endpoints ──────────────────────────────────────────────────────────

/** GET /api/health — `chain_checks` is "passed", "skipped" or "failed: <reason>". */
export interface Health {
  ok: boolean;
  chain_checks: string;
  chain_id: number;
  ausd_address: AddressString;
  controller_address: AddressString;
  live_vendor_mode: boolean;
}

export interface AgentConfig {
  name: string;
  address: AddressString;
  sector: string;
  sellers: string[];
}

export interface SellerConfig {
  seller_id: string;
  path: string;
  url: string;
  price_atomic: Atomic;
  pay_to: AddressString;
  description: string;
}

/** GET /api/config */
export interface AppConfig {
  chain_id: number;
  ausd_address: AddressString;
  controller_address: AddressString;
  faucet_address: AddressString;
  explorer_tx_url: string;
  explorer_address_url: string;
  agents: AgentConfig[];
  sellers: SellerConfig[];
}

export interface BudgetState {
  exists: boolean;
  daily_cap: Atomic;
  spent_today: Atomic;
  remaining_today: Atomic;
  expiry: number; // unix seconds
  paused: boolean;
  revoked: boolean;
  allowance_remaining: Atomic;
  wallet_balance: Atomic;
}

export interface AgentState {
  name: string;
  address: AddressString;
  sector: string;
  balance: Atomic;
  allocation: Atomic;
  spent_today: Atomic;
  agent_cap: Atomic;
  agent_remaining_today: Atomic;
  realized_value_per_dollar: number;
  paused: boolean;
  reasons: string[];
  /** Update #1: agents hold zero ETH (the relayer pays gas); the dashboard shows this is 0. */
  eth_balance_wei?: WeiString;
}

/** Update #1: the gas-paying relayer. (proposed location inside State) */
export interface RelayerState {
  address: AddressString;
  eth_balance_wei: WeiString;
  eth_spent_today_wei: WeiString;
  tx_count_today: number;
}

export interface SessionState {
  live: boolean;
  started_at: IsoTime | null;
  ends_at: IsoTime | null;
}

export interface Subscription {
  name: string;
  monthly_usd: number;
}

export interface SavingsState {
  mtd_spend_atomic: Atomic;
  prorated_subscriptions_usd: number;
  subscriptions: Subscription[];
}

/** GET /api/state (also the `state` SSE event). */
export interface State {
  owner: AddressString | null;
  budget: BudgetState;
  monthly: { cap: Atomic; spent_mtd: Atomic };
  runway_days: number | null;
  worst_case_exposure: Atomic;
  agents: AgentState[];
  session: SessionState;
  savings: SavingsState;
  relayer?: RelayerState;
  breaker?: BreakerStatus;
}

export type PurchaseStatus = "bought" | "skipped" | "failed";

/** GET /api/purchases row (also the `purchase` SSE event). Includes skipped decisions. */
export interface Purchase {
  id: number | string;
  ts: IsoTime;
  agent: string;
  seller_id: string;
  asset: string;
  params: Record<string, unknown>;
  price_atomic: Atomic;
  evoi_usd: number;
  status: PurchaseStatus;
  reason: string;
  delta_p: number | null;
  tx_hash: TxHash | null;
  payer: AddressString | null;
  pay_to: AddressString | null;
  live_vendor: boolean;
  /** Update #1: payTo and payer were checked with `isAccountFrozen` before paying. */
  compliance_checked?: boolean;
}

export interface Receipt {
  seller_id: string;
  tx_hash: TxHash;
  price_atomic: Atomic;
}

export type FindingDirection = "bullish" | "bearish" | "neutral" | "risk";

/** GET /api/findings row (also the `finding` SSE event). */
export interface Finding {
  id: number | string;
  ts: IsoTime;
  agent: string;
  asset: string;
  decision: string;
  headline: string;
  direction: FindingDirection;
  confidence: number;
  delta_p: number;
  summary: string;
  receipts: Receipt[];
}

export interface AllocationRow {
  agent: string;
  amount_atomic: Atomic;
  weight: number;
  reasons: string[];
}

/** GET /api/allocations (also the `allocation` SSE event). */
export interface Allocations {
  date: string;
  budget_atomic: Atomic;
  allocations: AllocationRow[];
}

export type EventKind = "earnings" | "fomc" | "cpi" | "pm_expiry" | "crypto";

/** GET /api/events-today row. */
export interface EventToday {
  date: string;
  kind: EventKind;
  title: string;
  asset: string | null;
  boosted_agent: string | null;
}

/** GET /api/pm-vs-options row. */
export interface PmVsOptionsRow {
  ticker: string;
  title: string;
  asset: string;
  strike: number;
  expiry: string;
  market_p: number;
  model_p: number;
  edge: number;
  edge_after_fees: number;
  tx_hash: TxHash | null;
}

/** GET /api/market row (also the `market` SSE event, as a list). */
export interface MarketRow {
  symbol: string;
  price: number;
  change_24h_pct: number | null;
  source: string;
  delayed: boolean;
}

export type AlertLevel = "info" | "warn" | "critical";

/** GET /api/alerts row (also the `alert` SSE event). */
export interface Alert {
  id: number | string;
  ts: IsoTime;
  level: AlertLevel;
  kind: string;
  message: string;
}

export type CatalogStatus =
  | "live_ausd_seller"
  | "external_x402_flagged"
  | "metered_adapter_roadmap"
  | "free_input"
  | "not_per_call";

export type PayableMode =
  | "x402_ausd_testnet"
  | "x402_usdc_mainnet"
  | "metered_api_key"
  | "free"
  | "subscription_contract";

/** GET /api/catalog row — mirror of `CatalogEntry` in `backend/app/sellers/registry.py`. */
export interface CatalogEntry {
  id: string;
  name: string;
  sector: string;
  description: string;
  trader_value: "S" | "A" | "B";
  source: string;
  pricing_model: string;
  payable_mode: PayableMode;
  price: string;
  status: CatalogStatus;
  label: string; // UI badge text, e.g. "demo vendor"
  price_atomic: Atomic | null;
  path: string | null;
  pay_to: AddressString | null;
  notes: string;
}

export interface SessionHours {
  start: string; // "HH:MM"
  end: string; // "HH:MM"
  tz: string;
}

/** GET /api/settings and the POST /api/settings body. */
export interface Settings {
  owner: AddressString | null;
  watchlist: string[];
  stakes_usd: Record<string, number>;
  session_hours: SessionHours;
  plan_days: number;
  monthly_cap_atomic: Atomic;
  daily_cap_atomic: Atomic;
  total_budget_atomic: Atomic;
  agent_caps_atomic: Record<string, Atomic>;
  subscriptions: Subscription[];
}

// ─── Update #1: circuit breaker and live x402 probe ──────────────────────────

export interface AgentBackoffStatus {
  failures: number;
  backoff_seconds: number;
  last_reason: string;
}

/**
 * GET /api/breaker (also the `breaker` SSE event). Mirror of `CircuitBreaker.status()` in
 * `backend/app/agents/breaker.py`. `global_open` = token paused → all agents halted.
 */
export interface BreakerStatus {
  global_open: boolean;
  global_reason: string;
  tripped_at: number | null; // unix seconds (float)
  agents: Record<string, AgentBackoffStatus>;
}

/** One x402 payment requirement as parsed by the backend (x402 v2 field names). */
export interface X402Requirement {
  scheme: string;
  network: string;
  amount: string; // atomic units of `asset`, decimal string
  asset: AddressString;
  payTo: AddressString;
  maxTimeoutSeconds: number;
  extra: Record<string, unknown>;
}

/** One vendor probed with an UNPAID request. (proposed) */
export interface X402ProbeVendor {
  id: string;
  name: string;
  url: string;
  ok: boolean; // true when a parseable 402 came back
  http_status: number | null;
  x402_version: number | null;
  accepts: X402Requirement[];
  error: string | null;
}

/** GET /api/x402-probe (+ ?refresh=1). Never signs or pays. (proposed) */
export interface X402ProbeResult {
  probed_at: IsoTime;
  cached: boolean;
  vendors: X402ProbeVendor[];
}

// ─── write endpoints ─────────────────────────────────────────────────────────

export interface TxResponse {
  tx_hash: TxHash;
}

export interface OkResponse {
  ok: boolean;
}

export interface BudgetPrepareRequest {
  owner: AddressString;
  total_budget_atomic: Atomic;
  daily_cap_atomic: Atomic;
  plan_days: number;
  agent_caps_atomic: Record<string, Atomic>;
}

export interface BudgetPrepareResponse {
  permit: TypedDataJson;
  config: TypedDataJson;
}

export interface BudgetOpenRequest {
  owner: AddressString;
  permit: { value: string | number; deadline: string | number; signature: string };
  config: Record<string, unknown>; // the `config.message` from /api/budget/prepare, unchanged
  config_signature: string;
}

export interface TypedDataResponse {
  typed_data: TypedDataJson;
}

export interface PauseRequest {
  owner: AddressString;
  paused: boolean;
  deadline: string | number;
  signature: string;
}

export interface RevokeRequest {
  owner: AddressString;
  deadline: string | number;
  signature: string;
}

export interface AgentTx {
  agent: string;
  amount_atomic: Atomic;
  tx_hash: TxHash;
}

export interface SessionStartResponse {
  allocations: Allocations;
  recharge_txs: AgentTx[];
}

export interface SessionStopResponse {
  sweep_txs: AgentTx[];
}

export interface AskResponse {
  answer: string;
  agents_used: string[];
  findings: Finding[];
  receipts: Receipt[];
}

// ─── Update #1 D: daily vouchers ("Maximum safety" mode) — all (proposed) ─────

export type VoucherStatus = "pending" | "redeemed" | "canceled" | "expired";

/** POST /api/vouchers/prepare body. */
export interface VoucherPrepareRequest {
  owner: AddressString;
  daily_amount_atomic: Atomic;
  days: number;
}

/** One unsigned voucher: a ReceiveWithAuthorization for one UTC day. */
export interface PreparedVoucher {
  day_index: number;
  typed_data: TypedDataJson;
}

export interface VoucherPrepareResponse {
  vouchers: PreparedVoucher[];
}

export interface SignedVoucher {
  day_index: number;
  message: Record<string, unknown>; // the typed_data.message from prepare, unchanged
  signature: string;
}

/** POST /api/vouchers/submit body. */
export interface VoucherSubmitRequest {
  owner: AddressString;
  vouchers: SignedVoucher[];
}

export interface VoucherSubmitResponse {
  stored: number;
}

/** GET /api/vouchers row. */
export interface Voucher {
  day_index: number;
  value_atomic: Atomic;
  valid_after: number; // unix seconds
  valid_before: number; // unix seconds
  nonce: string;
  status: VoucherStatus;
  tx_hash: TxHash | null;
}

/** POST /api/vouchers/prepare-cancel body → TypedDataResponse (a CancelAuthorization). */
export interface VoucherPrepareCancelRequest {
  owner: AddressString;
  day_index: number;
}

/** POST /api/vouchers/cancel body → TxResponse. */
export interface VoucherCancelRequest {
  owner: AddressString;
  day_index: number;
  signature: string;
}

// ─── SSE ─────────────────────────────────────────────────────────────────────

export interface RechargeEvent {
  agent: string;
  amount_atomic: Atomic;
  tx_hash: TxHash;
  reason: string;
}

export interface SweepEvent {
  agent: string;
  amount_atomic: Atomic;
  tx_hash: TxHash;
}

export interface AskEvent {
  question: string;
  answer: string;
  receipts: Receipt[];
}

/** Every `event: <type>` on GET /api/stream and its `data` JSON. */
export interface StreamEventMap {
  purchase: Purchase;
  finding: Finding;
  allocation: Allocations;
  recharge: RechargeEvent;
  sweep: SweepEvent;
  alert: Alert;
  state: State;
  market: MarketRow[];
  ask: AskEvent;
  breaker: BreakerStatus;
  ping: Record<string, unknown>;
}

export type StreamEventType = keyof StreamEventMap;
