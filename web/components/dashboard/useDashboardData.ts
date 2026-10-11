/**
 * useDashboardData: every read the dashboard (`/`) needs, kept live by Server-Sent Events.
 *
 * Data flow: an initial `GET` per panel through TanStack Query (`lib/api.ts`), then the SSE
 * stream (`lib/sse.ts`) patches the same query cache:
 *   purchase / finding / alert → prepended to their list (deduplicated by id, capped)
 *   state / allocation / market / breaker → replace the cached value
 *   recharge / sweep → refetch state (balances changed)
 * A panel whose GET fails keeps rendering its own error line; the rest of the page still works.
 */

"use client";

import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import {
  getAlerts,
  getAllocations,
  getBreaker,
  getConfig,
  getEventsToday,
  getFindings,
  getMarket,
  getPmVsOptions,
  getPurchases,
  getState,
  listVouchers,
} from "@/lib/api";
import { useEventStream, type EventStreamState } from "@/lib/sse";
import type { Alert, Finding, Purchase } from "@/lib/types";

export const QUERY_KEYS = {
  state: ["state"],
  config: ["config"],
  purchases: ["purchases"],
  findings: ["findings"],
  allocations: ["allocations"],
  events: ["events-today"],
  pmVsOptions: ["pm-vs-options"],
  market: ["market"],
  alerts: ["alerts"],
  breaker: ["breaker"],
  vouchers: ["vouchers"],
} as const;

const MAX_PURCHASES = 100;
const MAX_FINDINGS = 50;
const MAX_ALERTS = 50;
const SLOW_REFRESH_MS = 60_000; // panels without an SSE event (events, pm-vs-options)

interface WithId {
  id: number | string;
}

/** Put `item` first, drop any older copy with the same id, keep at most `max` rows. */
export function prependUnique<T extends WithId>(list: T[] | undefined, item: T, max: number): T[] {
  const rest = (list ?? []).filter((existing) => existing.id !== item.id);
  return [item, ...rest].slice(0, max);
}

function prependTo<T extends WithId>(client: QueryClient, key: readonly string[], item: T, max: number): void {
  client.setQueryData<T[]>(key, (list) => prependUnique(list, item, max));
}

export function useDashboardData() {
  const client = useQueryClient();

  const state = useQuery({ queryKey: QUERY_KEYS.state, queryFn: getState });
  const config = useQuery({ queryKey: QUERY_KEYS.config, queryFn: getConfig, staleTime: Infinity });
  const purchases = useQuery({ queryKey: QUERY_KEYS.purchases, queryFn: () => getPurchases(MAX_PURCHASES) });
  const findings = useQuery({ queryKey: QUERY_KEYS.findings, queryFn: () => getFindings(MAX_FINDINGS) });
  const allocations = useQuery({ queryKey: QUERY_KEYS.allocations, queryFn: getAllocations });
  const events = useQuery({ queryKey: QUERY_KEYS.events, queryFn: getEventsToday, refetchInterval: SLOW_REFRESH_MS });
  const pmVsOptions = useQuery({
    queryKey: QUERY_KEYS.pmVsOptions,
    queryFn: getPmVsOptions,
    refetchInterval: SLOW_REFRESH_MS,
  });
  const market = useQuery({ queryKey: QUERY_KEYS.market, queryFn: getMarket });
  const alerts = useQuery({ queryKey: QUERY_KEYS.alerts, queryFn: () => getAlerts(MAX_ALERTS) });
  const breaker = useQuery({ queryKey: QUERY_KEYS.breaker, queryFn: getBreaker });
  const voucherMode = state.data?.mode === "voucher";
  const vouchers = useQuery({
    queryKey: QUERY_KEYS.vouchers,
    queryFn: () => listVouchers(state.data?.owner ?? undefined),
    enabled: voucherMode,
  });

  const stream: EventStreamState = useEventStream({
    purchase: (row: Purchase) => prependTo(client, QUERY_KEYS.purchases, row, MAX_PURCHASES),
    finding: (row: Finding) => prependTo(client, QUERY_KEYS.findings, row, MAX_FINDINGS),
    alert: (row: Alert) => prependTo(client, QUERY_KEYS.alerts, row, MAX_ALERTS),
    state: (snapshot) => {
      client.setQueryData(QUERY_KEYS.state, snapshot);
      if (snapshot.breaker) {
        client.setQueryData(QUERY_KEYS.breaker, snapshot.breaker);
      }
    },
    allocation: (payload) => client.setQueryData(QUERY_KEYS.allocations, payload),
    market: (rows) => client.setQueryData(QUERY_KEYS.market, rows),
    breaker: (status) => client.setQueryData(QUERY_KEYS.breaker, status),
    recharge: () => void client.invalidateQueries({ queryKey: QUERY_KEYS.state }),
    sweep: () => void client.invalidateQueries({ queryKey: QUERY_KEYS.state }),
  });

  return { state, config, purchases, findings, allocations, events, pmVsOptions, market, alerts, breaker, vouchers, stream };
}

export type DashboardData = ReturnType<typeof useDashboardData>;
