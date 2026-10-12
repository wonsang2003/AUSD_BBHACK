/**
 * Client-side providers wrapped around every page (rendered by `app/layout.tsx`).
 *
 *   WagmiProvider        — wallet signing on Base Sepolia (`lib/appkit.ts`); optional for the demo:
 *                          the local demo funds the budget from the server-side demo wallet
 *   QueryClientProvider  — TanStack Query cache for every API read (keys: `terminal/queryKeys.ts`)
 *   StreamProvider       — the ONE live SSE connection; patches that cache for all pages
 *   DeskCommandProvider  — hands questions from the top-bar command line to ASK THE DESK
 *
 * The AppKit modal (when a Reown project id is configured) is created here at module load,
 * before any component can call an AppKit hook.
 */

"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { WagmiProvider } from "wagmi";

import { DeskCommandProvider } from "@/components/terminal/DeskCommand";
import { StreamProvider } from "@/components/terminal/StreamProvider";
import { initAppKit, wagmiConfig } from "@/lib/appkit";

const QUERY_STALE_TIME_MS = 5_000; // live data also arrives over SSE, so short-lived caching is enough

initAppKit();

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { staleTime: QUERY_STALE_TIME_MS, refetchOnWindowFocus: false, retry: 1 },
      mutations: { retry: 0 },
    },
  });
}

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(makeQueryClient);
  return (
    <WagmiProvider config={wagmiConfig}>
      <QueryClientProvider client={queryClient}>
        <StreamProvider>
          <DeskCommandProvider>{children}</DeskCommandProvider>
        </StreamProvider>
      </QueryClientProvider>
    </WagmiProvider>
  );
}
