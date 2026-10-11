/**
 * useDashboardData: every read the F1 DESK dashboard (`/`) needs.
 *
 * Data flow: an initial `GET` per panel through TanStack Query (`lib/api.ts`). Live updates come
 * from the ONE app-wide SSE connection in `components/terminal/StreamProvider.tsx`, which patches
 * the same query cache (keys shared via `components/terminal/queryKeys.ts`). This hook never opens
 * an EventSource of its own. A panel whose GET fails shows its own error line; the rest still works.
 *
 * `QUERY_KEYS` is re-exported here because /fund, /agents and /sources import it from this file.
 */

"use client";

import { useQuery } from "@tanstack/react-query";

import { LIST_LIMITS, QUERY_KEYS } from "@/components/terminal/queryKeys";
import { useStreamStatus } from "@/components/terminal/StreamProvider";
import {
  demoStatus,
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

export { QUERY_KEYS };

const SLOW_REFRESH_MS = 60_000; // panels without an SSE event (events, pm-vs-options)

export function useDashboardData() {
  const state = useQuery({ queryKey: QUERY_KEYS.state, queryFn: getState });
  const config = useQuery({ queryKey: QUERY_KEYS.config, queryFn: getConfig, staleTime: Infinity });
  const purchases = useQuery({ queryKey: QUERY_KEYS.purchases, queryFn: () => getPurchases(LIST_LIMITS.purchases) });
  const findings = useQuery({ queryKey: QUERY_KEYS.findings, queryFn: () => getFindings(LIST_LIMITS.findings) });
  const allocations = useQuery({ queryKey: QUERY_KEYS.allocations, queryFn: getAllocations });
  const events = useQuery({ queryKey: QUERY_KEYS.events, queryFn: getEventsToday, refetchInterval: SLOW_REFRESH_MS });
  const pmVsOptions = useQuery({
    queryKey: QUERY_KEYS.pmVsOptions,
    queryFn: getPmVsOptions,
    refetchInterval: SLOW_REFRESH_MS,
  });
  const market = useQuery({ queryKey: QUERY_KEYS.market, queryFn: getMarket });
  const alerts = useQuery({ queryKey: QUERY_KEYS.alerts, queryFn: () => getAlerts(LIST_LIMITS.alerts) });
  const breaker = useQuery({ queryKey: QUERY_KEYS.breaker, queryFn: getBreaker });
  const demo = useQuery({ queryKey: QUERY_KEYS.demoStatus, queryFn: demoStatus, retry: false, staleTime: 30_000 });
  const voucherMode = state.data?.mode === "voucher";
  const vouchers = useQuery({
    queryKey: QUERY_KEYS.vouchers,
    queryFn: () => listVouchers(state.data?.owner ?? undefined),
    enabled: voucherMode,
  });
  const stream = useStreamStatus();

  return {
    state,
    config,
    purchases,
    findings,
    allocations,
    events,
    pmVsOptions,
    market,
    alerts,
    breaker,
    vouchers,
    demo,
    stream,
  };
}

export type DashboardData = ReturnType<typeof useDashboardData>;
