/**
 * Safe display formatters for the terminal. They never throw: any odd input (null, NaN, a float
 * where an integer was expected, a huge wei number sent as a JSON number) renders as "—" or as
 * the closest sensible value, so one bad field can never crash a panel.
 *
 * Units: AUSD amounts are atomic integers (6 decimals); ETH amounts are wei (18 decimals) as
 * decimal strings or numbers; `*_usd` are dollar floats; fractions (0–1) for probabilities.
 * Money goes through bigint fixed-point math (`lib/format.ts`), never through floats.
 */

import { EMPTY, formatFixedPoint } from "@/lib/format";

export { EMPTY };

const AUSD_DECIMALS = 6;
const ETH_DECIMALS = 18;
const INTEGER_TEXT = /^-?\d+$/;
const DECIMAL_TEXT = /^-?\d+\.\d+$/;

/** Any integer-ish value → bigint; anything unusable → null. Floats are truncated. */
export function toBigIntSafe(value: unknown): bigint | null {
  if (typeof value === "bigint") {
    return value;
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? BigInt(Math.trunc(value)) : null;
  }
  if (typeof value === "string") {
    const text = value.trim();
    if (INTEGER_TEXT.test(text)) {
      return BigInt(text);
    }
    if (DECIMAL_TEXT.test(text)) {
      return BigInt(text.split(".")[0]);
    }
  }
  return null;
}

/** Any number-ish value → finite number, else null. */
export function toNumberSafe(value: unknown): number | null {
  const parsed = typeof value === "string" ? Number(value) : value;
  return typeof parsed === "number" && Number.isFinite(parsed) ? parsed : null;
}

/** AUSD atomic → "1,234.50" (2–4 decimals). */
export function fmtAusd(atomic: unknown): string {
  const amount = toBigIntSafe(atomic);
  if (amount === null) {
    return EMPTY;
  }
  return formatFixedPoint(amount, { decimals: AUSD_DECIMALS, minDecimals: 2, maxDecimals: 4 });
}

/** AUSD atomic → plain number of AUSD (for ratios and bars only, never for display). */
export function ausdNumber(atomic: unknown): number {
  const amount = toNumberSafe(atomic);
  return amount === null ? 0 : amount / 10 ** AUSD_DECIMALS;
}

/** Wei → "0.0000" with a fixed number of decimals (default 4). */
export function fmtEth(wei: unknown, decimals: number = 4): string {
  const amount = toBigIntSafe(wei);
  if (amount === null) {
    return EMPTY;
  }
  return formatFixedPoint(amount, { decimals: ETH_DECIMALS, minDecimals: decimals, maxDecimals: decimals });
}

/** True when a wei amount is strictly positive. */
export function isPositiveWei(wei: unknown): boolean {
  const amount = toBigIntSafe(wei);
  return amount !== null && amount > BigInt(0);
}

/** Dollars → "$1,234.56". */
export function fmtUsd(value: unknown, decimals: number = 2): string {
  const usd = toNumberSafe(value);
  if (usd === null) {
    return EMPTY;
  }
  const text = Math.abs(usd).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  return usd < 0 ? `-$${text}` : `$${text}`;
}

/** A fraction 0–1 → "12.3%". */
export function fmtPct(fraction: unknown, decimals: number = 1): string {
  const value = toNumberSafe(fraction);
  return value === null ? EMPTY : `${(value * 100).toFixed(decimals)}%`;
}

/** A value already in percent → "+1.23%" / "-0.50%". */
export function fmtSignedPct(percent: unknown, decimals: number = 2): string {
  const value = toNumberSafe(percent);
  if (value === null) {
    return EMPTY;
  }
  return `${value > 0 ? "+" : ""}${value.toFixed(decimals)}%`;
}

/** A signed number → "+0.042" / "-0.013" / "0.000". */
export function fmtSigned(value: unknown, decimals: number = 3): string {
  const number = toNumberSafe(value);
  if (number === null) {
    return EMPTY;
  }
  return `${number > 0 ? "+" : ""}${number.toFixed(decimals)}`;
}

/** Market price: 2 decimals from 1 up, 4 below 1, thousands separators. */
export function fmtPrice(value: unknown): string {
  const price = toNumberSafe(value);
  if (price === null) {
    return EMPTY;
  }
  const decimals = Math.abs(price) >= 1 ? 2 : 4;
  return price.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

/** Plain number with fixed decimals → "12.5". */
export function fmtNumber(value: unknown, decimals: number = 1): string {
  const number = toNumberSafe(value);
  return number === null ? EMPTY : number.toFixed(decimals);
}

/** "0x1234567890…" → "0x1234…abcd". */
export function shortHex(value: string | null | undefined, visible: number = 4): string {
  if (!value) {
    return EMPTY;
  }
  if (value.length <= 2 + visible * 2) {
    return value;
  }
  return `${value.slice(0, 2 + visible)}…${value.slice(-visible)}`;
}
