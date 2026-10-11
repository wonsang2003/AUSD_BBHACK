/**
 * Public runtime settings of the web app: the only configuration the browser ever sees.
 *
 * Every value comes from a `NEXT_PUBLIC_*` variable. Next.js inlines these at build time,
 * which is why each one is read with a literal `process.env.NEXT_PUBLIC_…` expression
 * (a dynamic `process.env[name]` lookup would be empty in the browser).
 *
 * Where the values come from (first match wins):
 *   1. `web/.env.local` / the shell environment,
 *   2. the repo-root `.env` (only its `NEXT_PUBLIC_*` lines; see `next.config.ts`),
 *   3. the defaults below.
 *
 * Private keys never belong here: anything in this file ships to every visitor.
 */

import type { Address } from "viem";

import { AUSD_TESTNET_ADDRESS } from "./constants";

const DEFAULT_API_URL = "http://localhost:8000";
const DEFAULT_SITE_URL = "http://localhost:3000";

/** Base URL of the FastAPI backend, without a trailing slash. */
export const API_URL: string = withoutTrailingSlash(process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL);

/** Public URL of this web app (shown to wallets in the WalletConnect metadata). */
export const SITE_URL: string = withoutTrailingSlash(process.env.NEXT_PUBLIC_SITE_URL || DEFAULT_SITE_URL);

/** Reown (WalletConnect) project id. Empty → the app falls back to the injected wallet only. */
export const REOWN_PROJECT_ID: string = process.env.NEXT_PUBLIC_REOWN_PROJECT_ID || "";

/** Optional custom Base Sepolia RPC for the browser. Empty → viem's public default (sepolia.base.org). */
export const BASE_SEPOLIA_RPC_URL: string = process.env.NEXT_PUBLIC_BASE_SEPOLIA_RPC_URL || "";

/** AUSD token address. Defaults to the official Base Sepolia test token. */
export const AUSD_ADDRESS: Address = asAddress(process.env.NEXT_PUBLIC_AUSD_ADDRESS) ?? AUSD_TESTNET_ADDRESS;

/**
 * Our deployed BudgetController, or null before deployment. Pages should prefer the
 * `controller_address` from `GET /api/config`, which is always current.
 */
export const BUDGET_CONTROLLER_ADDRESS: Address | null = asAddress(process.env.NEXT_PUBLIC_BUDGET_CONTROLLER_ADDRESS);

function withoutTrailingSlash(url: string): string {
  return url.endsWith("/") ? url.slice(0, -1) : url;
}

/** Rule: accept only a 0x-prefixed 40-hex-digit string; anything else (incl. empty) → null. */
function asAddress(value: string | undefined): Address | null {
  if (value && /^0x[0-9a-fA-F]{40}$/.test(value)) {
    return value as Address;
  }
  return null;
}
