/**
 * Typed client for the PocketDesk backend API (`docs/API.md` + SPEC §9 Update #1).
 *
 * One function per endpoint, named after what it does (`getState`, `openBudget`, …). Every
 * function returns the parsed JSON typed with `lib/types.ts`, or throws an `ApiError` carrying
 * the HTTP status and the backend's `{detail}` message, so pages can show failures honestly.
 *
 * The base URL is `NEXT_PUBLIC_API_URL` (see `lib/env.ts`). Signing never happens here: the
 * "prepare" endpoints return EIP-712 messages that the page signs with wagmi
 * (`signTypedDataAsync(toViemTypedData(typedData))`), then posts the signature back.
 */

import { API_URL } from "./env";
import type {
  Alert,
  Allocations,
  AppConfig,
  AskResponse,
  BreakerStatus,
  BudgetOpenRequest,
  BudgetPrepareRequest,
  BudgetPrepareResponse,
  CatalogEntry,
  EventToday,
  Finding,
  Health,
  MarketRow,
  OkResponse,
  PauseRequest,
  PmVsOptionsRow,
  Purchase,
  RevokeRequest,
  SessionStartResponse,
  SessionStopResponse,
  Settings,
  State,
  TxResponse,
  TypedDataListResponse,
  TypedDataResponse,
  Voucher,
  VoucherCancelRequest,
  VoucherCancelResponse,
  VoucherPrepareCancelRequest,
  VoucherPrepareRequest,
  VoucherPrepareResponse,
  VoucherSubmitRequest,
  VoucherSubmitResponse,
  X402ProbeResult,
} from "./types";

const DEFAULT_PURCHASES_LIMIT = 100;
const DEFAULT_FINDINGS_LIMIT = 50;
const DEFAULT_ALERTS_LIMIT = 50;
const JSON_HEADERS = { "Content-Type": "application/json", Accept: "application/json" };

// ─── transport ───────────────────────────────────────────────────────────────

/** A non-2xx answer from the backend. `detail` is the backend's human-readable reason. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** Absolute URL of an API path, e.g. apiUrl("/api/state") → "http://localhost:8000/api/state". */
export function apiUrl(path: string): string {
  return `${API_URL}${path}`;
}

/** URL of the Server-Sent Events stream (see `lib/sse.ts`). */
export function streamUrl(): string {
  return apiUrl("/api/stream");
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(apiUrl(path), { ...init, headers: JSON_HEADERS, cache: "no-store" });
  if (!response.ok) {
    throw new ApiError(response.status, await errorDetail(response));
  }
  return (await response.json()) as T;
}

/** Rule: prefer the backend's `{detail}`; fall back to the raw body, then the status text. */
async function errorDetail(response: Response): Promise<string> {
  const body = await response.text();
  try {
    const parsed: unknown = JSON.parse(body);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      return typeof detail === "string" ? detail : JSON.stringify(detail);
    }
  } catch {
    // not JSON: use the raw text below
  }
  return body || response.statusText;
}

function get<T>(path: string): Promise<T> {
  return request<T>(path, { method: "GET" });
}

function post<T>(path: string, body: unknown = {}): Promise<T> {
  return request<T>(path, { method: "POST", body: JSON.stringify(body) });
}

// ─── read endpoints ──────────────────────────────────────────────────────────

export function getHealth(): Promise<Health> {
  return get<Health>("/api/health");
}

export function getConfig(): Promise<AppConfig> {
  return get<AppConfig>("/api/config");
}

export function getState(): Promise<State> {
  return get<State>("/api/state");
}

/** Newest first; includes skipped and failed decisions. */
export function getPurchases(limit: number = DEFAULT_PURCHASES_LIMIT): Promise<Purchase[]> {
  return get<Purchase[]>(`/api/purchases?limit=${limit}`);
}

export function getFindings(limit: number = DEFAULT_FINDINGS_LIMIT): Promise<Finding[]> {
  return get<Finding[]>(`/api/findings?limit=${limit}`);
}

export function getAllocations(): Promise<Allocations> {
  return get<Allocations>("/api/allocations");
}

export function getEventsToday(): Promise<EventToday[]> {
  return get<EventToday[]>("/api/events-today");
}

export function getPmVsOptions(): Promise<PmVsOptionsRow[]> {
  return get<PmVsOptionsRow[]>("/api/pm-vs-options");
}

export function getMarket(): Promise<MarketRow[]> {
  return get<MarketRow[]>("/api/market");
}

export function getAlerts(limit: number = DEFAULT_ALERTS_LIMIT): Promise<Alert[]> {
  return get<Alert[]>(`/api/alerts?limit=${limit}`);
}

/**
 * The data catalog. The backend returns a plain list (`registry.catalog_json()`); an object
 * with a `catalog` list is also accepted in case the response gets wrapped later.
 */
export async function getCatalog(): Promise<CatalogEntry[]> {
  const body = await get<CatalogEntry[] | { catalog: CatalogEntry[] }>("/api/catalog");
  return Array.isArray(body) ? body : body.catalog;
}

export function getSettings(): Promise<Settings> {
  return get<Settings>("/api/settings");
}

/** Global circuit breaker + per-agent backoff (Update #1 C). */
export function getBreaker(): Promise<BreakerStatus> {
  return get<BreakerStatus>("/api/breaker");
}

/** Live x402 probe of real vendors: unpaid requests only (Update #1 E). `refresh` skips the 10-min cache. */
export function getX402Probe(refresh: boolean = false): Promise<X402ProbeResult> {
  return get<X402ProbeResult>(refresh ? "/api/x402-probe?refresh=1" : "/api/x402-probe");
}

// ─── write endpoints (the user signs, the relayer pays gas) ──────────────────

/** The relayer calls the AUSD faucet for `address` (users never need ETH). */
export function requestFaucet(address: string): Promise<TxResponse> {
  return post<TxResponse>("/api/faucet", { address });
}

/** Builds the permit + BudgetConfig messages with fresh on-chain nonces; sign them unchanged. */
export function prepareBudget(body: BudgetPrepareRequest): Promise<BudgetPrepareResponse> {
  return post<BudgetPrepareResponse>("/api/budget/prepare", body);
}

export function openBudget(body: BudgetOpenRequest): Promise<TxResponse> {
  return post<TxResponse>("/api/budget/open", body);
}

export function preparePause(owner: string, paused: boolean): Promise<TypedDataResponse> {
  return post<TypedDataResponse>("/api/budget/prepare-pause", { owner, paused });
}

export function pauseBudget(body: PauseRequest): Promise<TxResponse> {
  return post<TxResponse>("/api/budget/pause", body);
}

/** A permit with value 0: revokes the BudgetController's allowance. */
export function prepareRevoke(owner: string): Promise<TypedDataResponse> {
  return post<TypedDataResponse>("/api/budget/prepare-revoke", { owner });
}

export function revokeBudget(body: RevokeRequest): Promise<TxResponse> {
  return post<TxResponse>("/api/budget/revoke", body);
}

export function startSession(): Promise<SessionStartResponse> {
  return post<SessionStartResponse>("/api/session/start");
}

/** Ends the session and sweeps agent balances back to the owner. */
export function stopSession(): Promise<SessionStopResponse> {
  return post<SessionStopResponse>("/api/session/stop");
}

export function pauseAgent(name: string): Promise<OkResponse> {
  return post<OkResponse>(`/api/agents/${encodeURIComponent(name)}/pause`);
}

export function resumeAgent(name: string): Promise<OkResponse> {
  return post<OkResponse>(`/api/agents/${encodeURIComponent(name)}/resume`);
}

export function ask(question: string): Promise<AskResponse> {
  return post<AskResponse>("/api/ask", { question });
}

export function saveSettings(settings: Settings): Promise<Settings> {
  return post<Settings>("/api/settings", settings);
}

// ─── daily vouchers (Update #1 D, "Maximum safety" mode) ─────────────────────

/** 7 ReceiveWithAuthorization messages (one per UTC day) + the BudgetConfig, to be signed unchanged. */
export function prepareVouchers(body: VoucherPrepareRequest): Promise<VoucherPrepareResponse> {
  return post<VoucherPrepareResponse>("/api/vouchers/prepare", body);
}

export function submitVouchers(body: VoucherSubmitRequest): Promise<VoucherSubmitResponse> {
  return post<VoucherSubmitResponse>("/api/vouchers/submit", body);
}

/** The owner's vouchers. Accepts a plain list or `{vouchers: [...]}`. */
export async function listVouchers(owner?: string): Promise<Voucher[]> {
  const query = owner ? `?owner=${encodeURIComponent(owner)}` : "";
  const body = await get<Voucher[] | { vouchers: Voucher[] }>(`/api/vouchers${query}`);
  return Array.isArray(body) ? body : body.vouchers;
}

/** One CancelAuthorization message per future day's voucher nonce. */
export function prepareCancelVouchers(body: VoucherPrepareCancelRequest): Promise<TypedDataListResponse> {
  return post<TypedDataListResponse>("/api/vouchers/prepare-cancel", body);
}

export function cancelVouchers(body: VoucherCancelRequest): Promise<VoucherCancelResponse> {
  return post<VoucherCancelResponse>("/api/vouchers/cancel", body);
}
