/**
 * QueryState: the loading / error / empty / data switch every API-backed panel goes through.
 *
 * Data: a TanStack Query result (`useQuery(...)`). The backend may be down during the demo, so a
 * failed read shows the real reason ("backend unreachable" or the API's `{detail}`) in one dim
 * line instead of breaking the page. `isEmpty` decides when to show `emptyText`.
 */

import type { UseQueryResult } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { ApiError } from "@/lib/api";

interface QueryStateProps<T> {
  query: UseQueryResult<T>;
  isEmpty?: (data: T) => boolean;
  emptyText?: ReactNode;
  children: (data: T) => ReactNode;
}

/** Human-readable reason for a failed request or wallet action (viem errors carry `shortMessage`). */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail;
  }
  if (error && typeof error === "object" && "shortMessage" in error && typeof error.shortMessage === "string") {
    return error.shortMessage;
  }
  if (error instanceof TypeError) {
    return "backend unreachable";
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

export function QueryState<T>({ query, isEmpty, emptyText = "Nothing yet.", children }: QueryStateProps<T>) {
  if (query.data !== undefined) {
    if (isEmpty?.(query.data)) {
      return <Muted>{emptyText}</Muted>;
    }
    return <>{children(query.data)}</>;
  }
  if (query.isError) {
    return <Muted tone="error">Unavailable: {errorText(query.error)}</Muted>;
  }
  return <Muted>Loading…</Muted>;
}

function Muted({ tone, children }: { tone?: "error"; children: ReactNode }) {
  return (
    <div className={tone === "error" ? "px-3 py-3 text-xs text-down/80" : "px-3 py-3 text-xs text-fg-dim"}>{children}</div>
  );
}
