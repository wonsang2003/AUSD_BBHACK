/**
 * Next.js config for the PocketDesk dashboard.
 *
 * One `.env` for the whole repo: the backend reads the repo-root `.env`, and so does the web app,
 * but ONLY its `NEXT_PUBLIC_*` lines. Those are the values that are safe to ship to the browser
 * (API URL, Reown project id, contract addresses). Private keys in the same file are never
 * read here, so they cannot end up in a client bundle.
 *
 * Precedence: a value already in the environment (shell, `web/.env.local`) wins over the
 * repo-root `.env`.
 */

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import type { NextConfig } from "next";

const REPO_ROOT_ENV_FILE = path.join(__dirname, "..", ".env");
const PUBLIC_PREFIX = "NEXT_PUBLIC_";

/** Strip one pair of matching surrounding quotes: "x" or 'x' → x. */
function unquote(value: string): string {
  const quoted = value.length >= 2 && (value[0] === '"' || value[0] === "'") && value.endsWith(value[0]);
  return quoted ? value.slice(1, -1) : value;
}

/** The non-empty `NEXT_PUBLIC_*=value` lines of a dotenv file (comments and other keys ignored). */
function readPublicEnv(file: string): Record<string, string> {
  const result: Record<string, string> = {};
  if (!existsSync(file)) {
    return result;
  }
  for (const rawLine of readFileSync(file, "utf8").split("\n")) {
    const line = rawLine.trim();
    const separator = line.indexOf("=");
    if (!line.startsWith(PUBLIC_PREFIX) || separator === -1) {
      continue;
    }
    const key = line.slice(0, separator).trim();
    const value = unquote(line.slice(separator + 1).trim());
    if (value !== "") {
      result[key] = value;
    }
  }
  return result;
}

/** Repo-root public values for keys the environment does not already set. */
function publicEnvFallbacks(): Record<string, string> {
  const fallbacks: Record<string, string> = {};
  for (const [key, value] of Object.entries(readPublicEnv(REPO_ROOT_ENV_FILE))) {
    if (!process.env[key]) {
      fallbacks[key] = value;
    }
  }
  return fallbacks;
}

/**
 * Optional modules that the WalletConnect / MetaMask SDKs import but a browser build never
 * uses (a pretty-printer for server logs, React Native storage, …). Treating them as external
 * / empty silences "Module not found" build warnings; this is the fix Reown documents.
 */
const UNUSED_WALLET_SDK_EXTERNALS = ["pino-pretty", "lokijs", "encoding"];
const UNUSED_WALLET_SDK_FALLBACKS = { "@react-native-async-storage/async-storage": false };

const nextConfig: NextConfig = {
  env: publicEnvFallbacks(),
  webpack: (config) => {
    config.externals.push(...UNUSED_WALLET_SDK_EXTERNALS);
    config.resolve.fallback = { ...config.resolve.fallback, ...UNUSED_WALLET_SDK_FALLBACKS };
    return config;
  },
};

export default nextConfig;
