/**
 * Price history for the LIVE UPDATE popup's sparkline.
 *
 * `fetchHistory(symbol)` calls `GET /api/market/history` (docs/API.md); it never throws — any
 * failure resolves to `{points: [], error}`. When the backend has no points, the popup falls back
 * to `sessionSeries(symbol)`: the ticker-tape `/api/market` quotes collected in memory this
 * session by `recordMarketRows` (fed from the shared query cache in `useLiveUpdates`).
 */

import { apiUrl } from "./api";
import type { MarketRow } from "./types";

export interface PricePoint {
  /** unix seconds */
  t: number;
  p: number;
}

export interface PriceHistory {
  symbol: string;
  source: string | null;
  delayed: boolean;
  points: PricePoint[];
  error?: string;
}

export const DEFAULT_HISTORY_MINUTES = 240;
const MAX_SESSION_POINTS = 600;

const QUOTE_SUFFIXES = ["-USD", "/USD", "USDT", "-PERP"];

/** "btc-usd" → "BTC". */
export function normalizeSymbol(symbol: string | null | undefined): string {
  let upper = (symbol ?? "").trim().toUpperCase();
  for (const suffix of QUOTE_SUFFIXES) {
    if (upper.endsWith(suffix) && upper.length > suffix.length) {
      upper = upper.slice(0, -suffix.length);
    }
  }
  return upper;
}

function isPoint(value: unknown): value is PricePoint {
  const point = value as PricePoint;
  return (
    typeof point === "object" &&
    point !== null &&
    Number.isFinite(point.t) &&
    Number.isFinite(point.p) &&
    point.p > 0
  );
}

export async function fetchHistory(symbol: string, minutes: number = DEFAULT_HISTORY_MINUTES): Promise<PriceHistory> {
  const clean = normalizeSymbol(symbol);
  const empty: PriceHistory = { symbol: clean, source: null, delayed: false, points: [] };
  if (!clean) {
    return { ...empty, error: "no symbol" };
  }
  try {
    const query = new URLSearchParams({ symbol: clean, minutes: String(minutes) });
    const response = await fetch(apiUrl(`/api/market/history?${query.toString()}`), { cache: "no-store" });
    if (!response.ok) {
      return { ...empty, error: `HTTP ${response.status}` };
    }
    const body = (await response.json()) as Partial<PriceHistory>;
    const points = Array.isArray(body.points) ? body.points.filter(isPoint) : [];
    return {
      symbol: body.symbol ?? clean,
      source: body.source ?? null,
      delayed: Boolean(body.delayed),
      points,
      error: body.error,
    };
  } catch (error) {
    return { ...empty, error: error instanceof Error ? error.message : "backend unreachable" };
  }
}

// ─── in-memory session series from the ticker tape ──────────────────────────

const sessionQuotes = new Map<string, PricePoint[]>();

/** Remember one snapshot of `/api/market` rows (called on every market fetch / SSE event). */
export function recordMarketRows(rows: MarketRow[] | undefined, nowMs: number = Date.now()): void {
  if (!Array.isArray(rows)) {
    return;
  }
  const t = Math.floor(nowMs / 1000);
  for (const row of rows) {
    if (!row || !Number.isFinite(row.price) || row.price <= 0) {
      continue;
    }
    const symbol = normalizeSymbol(row.symbol);
    const series = sessionQuotes.get(symbol) ?? [];
    const last = series[series.length - 1];
    if (last && last.t === t) {
      last.p = row.price;
    } else {
      series.push({ t, p: row.price });
    }
    if (series.length > MAX_SESSION_POINTS) {
      series.splice(0, series.length - MAX_SESSION_POINTS);
    }
    sessionQuotes.set(symbol, series);
  }
}

/** The session's collected quotes for `symbol` (copy), oldest first. */
export function sessionSeries(symbol: string): PricePoint[] {
  return [...(sessionQuotes.get(normalizeSymbol(symbol)) ?? [])];
}
