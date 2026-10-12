/**
 * Labels for the live x402 probe: turn a raw x402 payment requirement into what a person reads
 * ("USDC", "Base mainnet", "0.01", "No — USDC on Base mainnet; AUSD routing = roadmap").
 *
 * Rules:
 *   * v1 vendors send short network names ("base"); v2 sends CAIP-2 ("eip155:8453"). Both map
 *     to the same CAIP-2 id before lookup.
 *   * The asset symbol comes from the web token registry (`lib/tokens.ts`, testnet tokens) or the
 *     known mainnet assets below; anything else shows its short address.
 *   * Only the budget token (AUSD on Base Sepolia) is payable from a PocketDesk budget.
 */

import { BASE_SEPOLIA_CAIP2 } from "@/lib/constants";
import { formatFixedPoint, shortAddress } from "@/lib/format";
import { defaultRegistry, findToken } from "@/lib/tokens";
import type { X402Requirement } from "@/lib/types";

const BASE_MAINNET_CAIP2 = "eip155:8453";

const NETWORK_ALIASES: Record<string, string> = {
  base: BASE_MAINNET_CAIP2,
  "base-sepolia": BASE_SEPOLIA_CAIP2,
};

const NETWORK_NAMES: Record<string, string> = {
  [BASE_MAINNET_CAIP2]: "Base mainnet",
  [BASE_SEPOLIA_CAIP2]: "Base Sepolia",
};

/** Well-known mainnet assets x402 vendors ask for (lower-cased address → symbol, decimals). */
const KNOWN_MAINNET_ASSETS: Record<string, { symbol: string; decimals: number }> = {
  "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": { symbol: "USDC", decimals: 6 }, // USDC on Base
};

export function toCaip2(network: string): string {
  return NETWORK_ALIASES[network] ?? network;
}

export function networkName(network: string): string {
  const caip2 = toCaip2(network);
  return NETWORK_NAMES[caip2] ?? network;
}

interface AssetInfo {
  symbol: string;
  decimals: number | null;
  isBudgetToken: boolean;
}

export function assetInfo(requirement: X402Requirement): AssetInfo {
  const token = findToken(defaultRegistry(), toCaip2(requirement.network), requirement.asset);
  if (token) {
    return { symbol: token.symbol, decimals: token.decimals, isBudgetToken: token.isBudgetToken };
  }
  const known = KNOWN_MAINNET_ASSETS[requirement.asset.toLowerCase()];
  if (known) {
    return { symbol: known.symbol, decimals: known.decimals, isBudgetToken: false };
  }
  return { symbol: shortAddress(requirement.asset), decimals: null, isBudgetToken: false };
}

/** Atomic amount → token units ("10000" USDC → "0.01"); raw atomic when decimals are unknown. */
export function formatAmount(requirement: X402Requirement): string {
  const { decimals } = assetInfo(requirement);
  if (decimals === null || !/^\d+$/.test(requirement.amount)) {
    return `${requirement.amount} (atomic)`;
  }
  return formatFixedPoint(requirement.amount, { decimals, minDecimals: 2, maxDecimals: decimals });
}

/** The honest answer to "Payable from your PocketDesk budget?". */
export function payableAnswer(requirement: X402Requirement): { payable: boolean; text: string } {
  const info = assetInfo(requirement);
  if (info.isBudgetToken) {
    return { payable: true, text: "Yes — AUSD on Base Sepolia" };
  }
  return { payable: false, text: `No — ${info.symbol} on ${networkName(requirement.network)}; AUSD routing = roadmap` };
}
