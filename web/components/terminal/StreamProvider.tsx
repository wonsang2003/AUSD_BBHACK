/**
 * StreamProvider: ONE live Server-Sent Events connection for the whole app (`lib/sse.ts`).
 *
 * Every event patches the shared TanStack Query cache (keys in `queryKeys.ts`), so any page or
 * panel reading that query updates live:
 *   purchase / finding / alert → prepended to their list (deduplicated by id, capped)
 *   state / allocation / market / breaker → replace the cached value
 *   recharge / sweep → refetch state (balances changed)
 * `useStreamStatus()` exposes the connection status for the top bar LED.
 * Must sit inside QueryClientProvider (see `app/providers.tsx`).
 */

"use client";

import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { createContext, useContext, type ReactNode } from "react";

import { useEventStream, type EventStreamState } from "@/lib/sse";
import type { Alert, Finding, Purchase } from "@/lib/types";

import { LIST_LIMITS, QUERY_KEYS } from "./queryKeys";

interface WithId {
  id: number | string;
}

const StreamContext = createContext<EventStreamState>({ status: "connecting", lastEventAt: null });

/** Put `item` first, drop any older copy with the same id, keep at most `max` rows. */
export function prependUnique<T extends WithId>(list: T[] | undefined, item: T, max: number): T[] {
  const rest = (Array.isArray(list) ? list : []).filter((existing) => existing.id !== item.id);
  return [item, ...rest].slice(0, max);
}

function prependTo<T extends WithId>(client: QueryClient, key: readonly string[], item: T, max: number): void {
  if (item && typeof item === "object" && "id" in item) {
    client.setQueryData<T[]>(key, (list) => prependUnique(list, item, max));
  }
}

export function StreamProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const refreshState = () => void client.invalidateQueries({ queryKey: QUERY_KEYS.state });
  const stream = useEventStream({
    purchase: (row: Purchase) => prependTo(client, QUERY_KEYS.purchases, row, LIST_LIMITS.purchases),
    finding: (row: Finding) => prependTo(client, QUERY_KEYS.findings, row, LIST_LIMITS.findings),
    alert: (row: Alert) => prependTo(client, QUERY_KEYS.alerts, row, LIST_LIMITS.alerts),
    state: (snapshot) => {
      client.setQueryData(QUERY_KEYS.state, snapshot);
      if (snapshot?.breaker) {
        client.setQueryData(QUERY_KEYS.breaker, snapshot.breaker);
      }
    },
    allocation: (payload) => client.setQueryData(QUERY_KEYS.allocations, payload),
    market: (rows) => {
      if (Array.isArray(rows)) {
        client.setQueryData(QUERY_KEYS.market, rows);
      }
    },
    breaker: (status) => client.setQueryData(QUERY_KEYS.breaker, status),
    recharge: refreshState,
    sweep: refreshState,
  });
  return <StreamContext.Provider value={stream}>{children}</StreamContext.Provider>;
}

/** Live-feed connection status ("open" = connected) and the time of the last event. */
export function useStreamStatus(): EventStreamState {
  return useContext(StreamContext);
}
