/**
 * Client-side providers wrapped around every page (rendered by `app/layout.tsx`).
 *
 *   WagmiProvider        — wallet connection + signing on Base Sepolia (`lib/appkit.ts`)
 *   QueryClientProvider  — TanStack Query cache, used by wagmi and by pages for API reads
 *
 * The AppKit modal (when a Reown project id is configured) is created here at module load,
 * before any component can call an AppKit hook.
 */

"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { WagmiProvider } from "wagmi";

import { initAppKit, wagmiConfig } from "@/lib/appkit";

const QUERY_STALE_TIME_MS = 5_000; // live data also arrives over SSE, so short-lived caching is enough

initAppKit();

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { staleTime: QUERY_STALE_TIME_MS, refetchOnWindowFocus: false, retry: 1 },
    },
  });
}

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(makeQueryClient);
  return (
    <WagmiProvider config={wagmiConfig}>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </WagmiProvider>
  );
}
