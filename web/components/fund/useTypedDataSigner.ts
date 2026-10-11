/**
 * useTypedDataSigner: sign a backend-built EIP-712 message with the connected wallet.
 *
 * The backend's "prepare" endpoints return full messages (`lib/eip712.ts` FullMessage); this
 * hook converts one with `toViemTypedData` (integers → bigint, EIP712Domain dropped) and asks
 * the wallet to sign it UNCHANGED. Every PocketDesk signature is bound to Base Sepolia
 * (chainId 84532 in the domain), so the wallet is switched there first if needed.
 */

"use client";

import { useCallback } from "react";
import { useAccount, useSignTypedData, useSwitchChain } from "wagmi";

import { TARGET_CHAIN } from "@/lib/appkit";
import { toViemTypedData } from "@/lib/eip712";
import type { TypedDataJson } from "@/lib/types";

export type SignFn = (message: TypedDataJson) => Promise<string>;

export function useTypedDataSigner(): SignFn {
  const { chainId } = useAccount();
  const { switchChainAsync } = useSwitchChain();
  const { signTypedDataAsync } = useSignTypedData();

  return useCallback(
    async (message: TypedDataJson) => {
      if (chainId !== TARGET_CHAIN.id) {
        await switchChainAsync({ chainId: TARGET_CHAIN.id });
      }
      const typed = toViemTypedData(message) as unknown as Parameters<typeof signTypedDataAsync>[0];
      return signTypedDataAsync(typed);
    },
    [chainId, switchChainAsync, signTypedDataAsync],
  );
}
