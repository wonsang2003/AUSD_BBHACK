/**
 * TanStack Query cache keys for every backend read, shared by the terminal chrome, the live SSE
 * provider and all pages, so one fetch (or one SSE event) updates every panel showing that data.
 */

export const QUERY_KEYS = {
  health: ["health"],
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
  demoStatus: ["demo-status"],
} as const;

/** Rows kept in the live lists (the SSE provider caps them at these sizes). */
export const LIST_LIMITS = {
  purchases: 100,
  findings: 50,
  alerts: 50,
} as const;
