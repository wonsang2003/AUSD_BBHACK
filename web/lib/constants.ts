/**
 * Fixed protocol constants for the PocketDesk web app.
 *
 * Mirror of `backend/app/constants.py`. Everything in here is a *fact about the outside
 * world* (chain ids, explorer URLs, token decimals, signing-domain names). Values that an
 * operator may change (API URL, WalletConnect project id, deployed contract addresses)
 * live in `lib/env.ts` instead.
 *
 * Keep this file in sync with the backend: `npm run test:eip712` recomputes the EIP-712
 * digests from these names/versions and compares them with the backend's vectors, so a
 * drift here fails that test.
 */

import type { Address } from "viem";

// ─── Network: Base Sepolia ────────────────────────────────────────────────────
export const BASE_SEPOLIA_CHAIN_ID = 84532;
export const BASE_SEPOLIA_CAIP2 = "eip155:84532"; // CAIP-2 network id used inside x402 payloads
export const BASESCAN_TX_URL = "https://sepolia.basescan.org/tx/";
export const BASESCAN_ADDRESS_URL = "https://sepolia.basescan.org/address/";

// ─── AUSD token (Agora Dollar) ────────────────────────────────────────────────
export const AUSD_TESTNET_ADDRESS: Address = "0xa9012a055bd4e0eDfF8Ce09f960291C09D5322dC";
export const AUSD_FAUCET_ADDRESS: Address = "0xd236c18D274E54FAccC3dd9DDA4b27965a73ee6C";

export const AUSD_DECIMALS = 6;
/** 1 AUSD in atomic units (10 ** AUSD_DECIMALS). */
export const ONE_AUSD = BigInt(1_000_000);

// IMPORTANT: `name()` on the token returns "AUSD", but the EIP-712 *signing*
// domain name is "Agora Dollar". Signing with "AUSD" fails silently.
export const AUSD_EIP712_NAME = "Agora Dollar";
export const AUSD_EIP712_VERSION = "1";

// ─── PocketDesk BudgetController signing domain ──────────────────────────────
export const POCKETDESK_EIP712_NAME = "PocketDesk";
export const POCKETDESK_EIP712_VERSION = "1";

// ─── ERC-3009 timing ─────────────────────────────────────────────────────────
// validAfter is backdated a little so small clock skew between the browser and the
// chain does not make a fresh authorization "not yet valid".
export const ERC3009_VALID_AFTER_SKEW_SECONDS = 30;
export const DEFAULT_PAYMENT_TIMEOUT_SECONDS = 120;

// ─── Time ────────────────────────────────────────────────────────────────────
export const SECONDS_PER_DAY = 86_400;

// ─── Sectors (stored on-chain as bytes32 labels) ─────────────────────────────
export const AGENT_NAMES = ["OnchainPerps", "OptionsEvent", "NewsSentiment", "MacroFilings"] as const;
export type AgentName = (typeof AGENT_NAMES)[number];
