/**
 * Wallet connection for the PocketDesk web app (Base Sepolia only).
 *
 * Two modes, picked at build time from `NEXT_PUBLIC_REOWN_PROJECT_ID`:
 *   * project id set   → Reown AppKit (WalletConnect modal: mobile wallets, QR, injected) on top
 *                        of the wagmi adapter. `initAppKit()` must run once on the client
 *                        before any AppKit hook is used (`app/providers.tsx` does this).
 *   * project id empty → a plain wagmi config with the injected connector only (MetaMask,
 *                        Rabby, Coinbase extension…), so local dev works without a Reown account.
 *
 * Either way the rest of the app talks to `wagmiConfig` through wagmi hooks, and users only ever
 * SIGN (EIP-712): the backend relayer submits every transaction and pays the gas.
 */

import { WagmiAdapter } from "@reown/appkit-adapter-wagmi";
import { createAppKit } from "@reown/appkit/react";
import { http, type Transport } from "viem";
import { baseSepolia } from "viem/chains";
import { createConfig, type Config } from "wagmi";
import { injected } from "wagmi/connectors";

import { BASE_SEPOLIA_RPC_URL, REOWN_PROJECT_ID, SITE_URL } from "./env";

/** True when the Reown AppKit modal is available (a project id is configured). */
export const APPKIT_ENABLED: boolean = REOWN_PROJECT_ID !== "";

/** The only chain PocketDesk uses. */
export const TARGET_CHAIN = baseSepolia;

const APP_METADATA = {
  name: "PocketDesk",
  description: "A research desk on a retail budget: agents buy research per call over x402 in AUSD (testnet).",
  url: SITE_URL,
  icons: [] as string[],
};

const ACCENT_GREEN = "#22c55e";

/** Browser transport: the custom RPC if configured, else viem's public Base Sepolia default. */
function baseSepoliaTransport(): Transport {
  return BASE_SEPOLIA_RPC_URL ? http(BASE_SEPOLIA_RPC_URL) : http();
}

function makeAppKitAdapter(): WagmiAdapter {
  return new WagmiAdapter({
    networks: [TARGET_CHAIN],
    projectId: REOWN_PROJECT_ID,
    ssr: true,
    transports: { [TARGET_CHAIN.id]: baseSepoliaTransport() },
  });
}

function makeInjectedOnlyConfig(): Config {
  return createConfig({
    chains: [TARGET_CHAIN],
    connectors: [injected()],
    ssr: true,
    transports: { [TARGET_CHAIN.id]: baseSepoliaTransport() },
  });
}

const appKitAdapter: WagmiAdapter | null = APPKIT_ENABLED ? makeAppKitAdapter() : null;

/**
 * The adapter builds its config with whatever `@wagmi/core` npm resolved for it. Its peer range
 * (">=2.20.3") currently resolves to 3.x while `wagmi` 2.x uses 2.21.2, so the two `Config`
 * types differ. KNOWN ISSUE: pin `"@wagmi/core": "2.21.2"` in package.json dependencies so both
 * use the same runtime; until then this cast only papers over the type mismatch.
 */
function adapterConfig(adapter: WagmiAdapter): Config {
  return adapter.wagmiConfig as unknown as Config;
}

/** The wagmi config the whole app uses (`<WagmiProvider config={wagmiConfig}>`). */
export const wagmiConfig: Config = appKitAdapter ? adapterConfig(appKitAdapter) : makeInjectedOnlyConfig();

let appKitInitialized = false;

/** Create the AppKit modal once. No-op without a project id or when called again. */
export function initAppKit(): void {
  if (appKitAdapter === null || appKitInitialized) {
    return;
  }
  createAppKit({
    adapters: [appKitAdapter],
    networks: [TARGET_CHAIN],
    defaultNetwork: TARGET_CHAIN,
    projectId: REOWN_PROJECT_ID,
    metadata: APP_METADATA,
    themeMode: "dark",
    themeVariables: { "--w3m-accent": ACCENT_GREEN, "--w3m-border-radius-master": "2px" },
    features: { analytics: false, email: false, socials: false, swaps: false, onramp: false },
  });
  appKitInitialized = true;
}
