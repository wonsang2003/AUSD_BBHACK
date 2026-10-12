/**
 * useChainInfo: which chain the backend runs on, for the chain chip and for tx-hash links.
 *
 * Data: `GET /api/health` (chain id, AUSD + controller addresses, optional `rpc_url` /
 * `local_chain`) and `GET /api/config` (explorer URL). Both are cached app-wide.
 *
 * Local-chain rule (the anvil demo stack also uses chain id 84532):
 *   local when health.local_chain is true, OR rpc_url points at localhost / 127.0.0.1 / anvil,
 *   OR chain id is 31337, OR a controller is configured on 84532 with an AUSD address other than
 *   the official Base Sepolia AUSD (i.e. the MockAUSD deployed by `make local-stack`).
 * Labels: "LOCAL CHAIN 84532" · "BASE SEPOLIA · TESTNET" · otherwise "CHAIN <id>".
 * On a local chain tx hashes are shown without Basescan links (they would 404).
 */

"use client";

import { useQuery } from "@tanstack/react-query";

import { getConfig, getHealth } from "@/lib/api";
import { AUSD_TESTNET_ADDRESS, BASE_SEPOLIA_CHAIN_ID, BASESCAN_TX_URL } from "@/lib/constants";
import type { Health } from "@/lib/types";

import { QUERY_KEYS } from "./queryKeys";

const ANVIL_CHAIN_ID = 31337;
const LOCAL_RPC = /localhost|127\.0\.0\.1|0\.0\.0\.0|anvil/i;
const HEALTH_REFRESH_MS = 30_000;

export interface ChainInfo {
  /** Null until /api/health answered. */
  chainId: number | null;
  isLocal: boolean;
  label: string;
  explorerTxUrl: string;
  /** /api/health could not be read (backend down). */
  unreachable: boolean;
  health: Health | undefined;
}

/** The local-chain rule from the header. */
export function isLocalChain(health: Health): boolean {
  if (health.local_chain === true) {
    return true;
  }
  if (health.rpc_url && LOCAL_RPC.test(health.rpc_url)) {
    return true;
  }
  if (health.chain_id === ANVIL_CHAIN_ID) {
    return true;
  }
  const hasController = Boolean(health.controller_address);
  const mockToken =
    Boolean(health.ausd_address) && health.ausd_address.toLowerCase() !== AUSD_TESTNET_ADDRESS.toLowerCase();
  return health.chain_id === BASE_SEPOLIA_CHAIN_ID && hasController && mockToken;
}

function chainLabel(health: Health | undefined, isLocal: boolean): string {
  if (!health) {
    return "CHAIN ?";
  }
  if (isLocal) {
    return `LOCAL CHAIN ${health.chain_id}`;
  }
  if (health.chain_id === BASE_SEPOLIA_CHAIN_ID) {
    return "BASE SEPOLIA · TESTNET";
  }
  return `CHAIN ${health.chain_id}`;
}

export function useChainInfo(): ChainInfo {
  const health = useQuery({ queryKey: QUERY_KEYS.health, queryFn: getHealth, refetchInterval: HEALTH_REFRESH_MS });
  const config = useQuery({ queryKey: QUERY_KEYS.config, queryFn: getConfig, staleTime: Infinity });
  const data = health.data;
  const isLocal = data ? isLocalChain(data) : false;
  return {
    chainId: data?.chain_id ?? null,
    isLocal,
    label: chainLabel(data, isLocal),
    explorerTxUrl: config.data?.explorer_tx_url || BASESCAN_TX_URL,
    unreachable: health.isError && data === undefined,
    health: data,
  };
}
