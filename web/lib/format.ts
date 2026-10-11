/**
 * Display formatting for the dashboard: money, addresses, explorer links, times.
 *
 * Money is formatted from integers with bigint arithmetic, never through floats, so an AUSD
 * amount shown on screen is exactly the on-chain amount rounded for display:
 *   * AUSD: atomic units, 6 decimals (1_000_000 atomic = 1 AUSD);
 *   * ETH:  wei, 18 decimals (the API sends wei as decimal strings).
 * Missing values (null / undefined) render as an em dash.
 */

import { AUSD_DECIMALS, BASESCAN_ADDRESS_URL, BASESCAN_TX_URL } from "./constants";
import type { IntegerLike } from "./eip712";

export const EMPTY = "—";
const ETH_DECIMALS = 18;
const AUSD_MIN_DECIMALS = 2;
const AUSD_MAX_DECIMALS = 4; // enough to show a 0.005 AUSD micro-payment
const ETH_MAX_DECIMALS = 6;
const ADDRESS_VISIBLE_CHARS = 4;
const HASH_VISIBLE_CHARS = 6;

const MS_PER_SECOND = 1_000;
const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3_600;
const SECONDS_PER_DAY = 86_400;
const JUST_NOW_SECONDS = 5;

type Maybe<T> = T | null | undefined;

// ─── fixed-point numbers ─────────────────────────────────────────────────────

export interface FixedPointOptions {
  decimals: number; // decimals of the integer unit (6 for AUSD, 18 for ETH)
  minDecimals: number; // always show at least this many
  maxDecimals: number; // round half-up to at most this many
}

/**
 * Format an integer amount in base units as a decimal string with thousands separators.
 * Formula: shown = round_half_up(|value| · 10^maxDecimals / 10^decimals), then trailing zeros
 * are trimmed down to minDecimals. Example: 1234500000 (AUSD) → "1,234.50".
 */
export function formatFixedPoint(value: IntegerLike, options: FixedPointOptions): string {
  const amount = BigInt(value);
  const negative = amount < BigInt(0);
  const magnitude = negative ? -amount : amount;
  const unit = BigInt(10) ** BigInt(options.decimals);
  const keep = BigInt(10) ** BigInt(options.maxDecimals);
  const rounded = (magnitude * keep + unit / BigInt(2)) / unit;

  const whole = (rounded / keep).toLocaleString("en-US");
  const fraction = trimFraction((rounded % keep).toString().padStart(options.maxDecimals, "0"), options.minDecimals);
  const sign = negative && rounded > BigInt(0) ? "-" : "";
  return fraction ? `${sign}${whole}.${fraction}` : `${sign}${whole}`;
}

/** Drop trailing zeros from a fraction string, but keep at least `minDigits` digits. */
function trimFraction(fraction: string, minDigits: number): string {
  let end = fraction.length;
  while (end > minDigits && fraction[end - 1] === "0") {
    end -= 1;
  }
  return fraction.slice(0, end);
}

// ─── money ───────────────────────────────────────────────────────────────────

/** AUSD atomic units → "1,234.50" (2–4 decimals). Pass `withSymbol` for "1,234.50 AUSD". */
export function formatAusd(atomic: Maybe<IntegerLike>, withSymbol: boolean = false): string {
  if (atomic === null || atomic === undefined) {
    return EMPTY;
  }
  const text = formatFixedPoint(atomic, {
    decimals: AUSD_DECIMALS,
    minDecimals: AUSD_MIN_DECIMALS,
    maxDecimals: AUSD_MAX_DECIMALS,
  });
  return withSymbol ? `${text} AUSD` : text;
}

/** Wei → "0.000123 ETH" (up to 6 decimals). */
export function formatEth(wei: Maybe<IntegerLike>): string {
  if (wei === null || wei === undefined) {
    return EMPTY;
  }
  const text = formatFixedPoint(wei, { decimals: ETH_DECIMALS, minDecimals: 0, maxDecimals: ETH_MAX_DECIMALS });
  return `${text} ETH`;
}

/** Dollars → "$1,234.56". */
export function formatUsd(usd: Maybe<number>, decimals: number = 2): string {
  if (usd === null || usd === undefined || !Number.isFinite(usd)) {
    return EMPTY;
  }
  const text = Math.abs(usd).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  return usd < 0 ? `-$${text}` : `$${text}`;
}

/** A fraction 0–1 → "12.3%" (probabilities, weights, edges). */
export function formatPercent(fraction: Maybe<number>, decimals: number = 1): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) {
    return EMPTY;
  }
  return `${(fraction * 100).toFixed(decimals)}%`;
}

/** A value already in percent → "+1.23%" / "-0.50%" (24h changes). */
export function formatSignedPercent(percent: Maybe<number>, decimals: number = 2): string {
  if (percent === null || percent === undefined || !Number.isFinite(percent)) {
    return EMPTY;
  }
  const sign = percent > 0 ? "+" : "";
  return `${sign}${percent.toFixed(decimals)}%`;
}

// ─── addresses and transactions ──────────────────────────────────────────────

/** "0x1234567890…" → "0x1234…abcd". */
export function shortAddress(address: Maybe<string>, visibleChars: number = ADDRESS_VISIBLE_CHARS): string {
  if (!address) {
    return EMPTY;
  }
  return `${address.slice(0, 2 + visibleChars)}…${address.slice(-visibleChars)}`;
}

/** "0xabcdef…" tx hash → "0xabcdef…123456". */
export function shortHash(hash: Maybe<string>): string {
  return shortAddress(hash, HASH_VISIBLE_CHARS);
}

/** Explorer link for a transaction. Pass `explorerTxUrl` from /api/config when available. */
export function txUrl(hash: string, explorerTxUrl: string = BASESCAN_TX_URL): string {
  return `${explorerTxUrl}${hash}`;
}

/** Explorer link for an address. */
export function addressUrl(address: string, explorerAddressUrl: string = BASESCAN_ADDRESS_URL): string {
  return `${explorerAddressUrl}${address}`;
}

// ─── time ────────────────────────────────────────────────────────────────────

/** Rule: a number is unix SECONDS (the backend's convention); a string is ISO-8601. */
export function toDate(when: string | number | Date): Date {
  if (when instanceof Date) {
    return when;
  }
  if (typeof when === "number") {
    return new Date(when * MS_PER_SECOND);
  }
  return new Date(when);
}

/** "just now", "42s ago", "5m ago", "3h ago", "2d ago" — or "in 5m" for future times. */
export function relativeTime(when: Maybe<string | number | Date>, nowMs: number = Date.now()): string {
  if (when === null || when === undefined) {
    return EMPTY;
  }
  const timeMs = toDate(when).getTime();
  if (Number.isNaN(timeMs)) {
    return EMPTY;
  }
  const deltaSeconds = Math.round((nowMs - timeMs) / MS_PER_SECOND);
  const magnitude = Math.abs(deltaSeconds);
  if (magnitude < JUST_NOW_SECONDS) {
    return "just now";
  }
  const span = describeSpan(magnitude);
  return deltaSeconds > 0 ? `${span} ago` : `in ${span}`;
}

/** Largest whole unit of a duration: 42 → "42s", 300 → "5m", 7200 → "2h", 172800 → "2d". */
function describeSpan(seconds: number): string {
  if (seconds < SECONDS_PER_MINUTE) {
    return `${seconds}s`;
  }
  if (seconds < SECONDS_PER_HOUR) {
    return `${Math.floor(seconds / SECONDS_PER_MINUTE)}m`;
  }
  if (seconds < SECONDS_PER_DAY) {
    return `${Math.floor(seconds / SECONDS_PER_HOUR)}h`;
  }
  return `${Math.floor(seconds / SECONDS_PER_DAY)}d`;
}

/** "14:05:09" in the viewer's local time (dense tables). */
export function clockTime(when: Maybe<string | number | Date>): string {
  if (when === null || when === undefined) {
    return EMPTY;
  }
  const date = toDate(when);
  if (Number.isNaN(date.getTime())) {
    return EMPTY;
  }
  return date.toLocaleTimeString("en-GB", { hour12: false });
}
