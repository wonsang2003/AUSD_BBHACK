/**
 * Token registry: which ERC-3009 stablecoins PocketDesk knows how to sign for.
 *
 * Mirror of `backend/app/chain/tokens.py`. AUSD is the default and the only token used
 * for the *budget* (permits, BudgetConfig caps, vouchers); USDC is listed so the UI can
 * label any standard x402 requirement it sees (e.g. in the live x402 probe on /sources).
 *
 * Rules (same as the backend):
 *   * A requirement is matched on (CAIP-2 network, lower-cased asset address).
 *   * Preference order is the list order: AUSD first.
 *   * The backend verifies every entry on-chain at startup (`decimals()`, `eip712Domain()`)
 *     and disables entries that fail. The web copy is static and only used for labels,
 *     decimals and signing domains; the backend stays the authority on what is enabled.
 */

import type { Address } from "viem";

import {
  AUSD_DECIMALS,
  AUSD_EIP712_NAME,
  AUSD_EIP712_VERSION,
  AUSD_FAUCET_ADDRESS,
  BASE_SEPOLIA_CAIP2,
  BASE_SEPOLIA_CHAIN_ID,
} from "./constants";
import { AUSD_ADDRESS } from "./env";

export const USDC_BASE_SEPOLIA_ADDRESS: Address = "0x036CbD53842c5426634e7929541eC2318f3dCF7e";
export const USDC_DECIMALS = 6;
export const USDC_EIP712_NAME = "USDC"; // expected; the backend verifies it on-chain at startup
export const USDC_EIP712_VERSION = "2";

// AUSD (and its faucet) live at the same address on every EVM testnet Agora supports.
export const ARBITRUM_SEPOLIA_CHAIN_ID = 421614;
export const ARBITRUM_SEPOLIA_CAIP2 = "eip155:421614";

export interface TokenInfo {
  symbol: string;
  chainId: number;
  caip2: string;
  address: Address;
  eip712Name: string;
  eip712Version: string;
  decimals: number;
  faucet: Address | null;
  /** True only for AUSD: the token budgets, permits and vouchers use. */
  isBudgetToken: boolean;
}

/** The registry in preference order (AUSD first). */
export function defaultRegistry(ausdAddress: Address = AUSD_ADDRESS): TokenInfo[] {
  return [
    {
      symbol: "AUSD",
      chainId: BASE_SEPOLIA_CHAIN_ID,
      caip2: BASE_SEPOLIA_CAIP2,
      address: ausdAddress,
      eip712Name: AUSD_EIP712_NAME,
      eip712Version: AUSD_EIP712_VERSION,
      decimals: AUSD_DECIMALS,
      faucet: AUSD_FAUCET_ADDRESS,
      isBudgetToken: true,
    },
    {
      symbol: "USDC",
      chainId: BASE_SEPOLIA_CHAIN_ID,
      caip2: BASE_SEPOLIA_CAIP2,
      address: USDC_BASE_SEPOLIA_ADDRESS,
      eip712Name: USDC_EIP712_NAME,
      eip712Version: USDC_EIP712_VERSION,
      decimals: USDC_DECIMALS,
      faucet: null,
      isBudgetToken: false,
    },
  ];
}

/** Return the token matching an x402 (network, asset) pair, or null. Asset compare is case-insensitive. */
export function findToken(registry: TokenInfo[], network: string, asset: string): TokenInfo | null {
  const wantedAsset = asset.toLowerCase();
  for (const token of registry) {
    if (token.caip2 === network && token.address.toLowerCase() === wantedAsset) {
      return token;
    }
  }
  return null;
}

/** The budget token (AUSD). Throws if the registry has none, which is a programming error. */
export function budgetToken(registry: TokenInfo[]): TokenInfo {
  for (const token of registry) {
    if (token.isBudgetToken) {
      return token;
    }
  }
  throw new Error("registry has no budget token (AUSD)");
}
