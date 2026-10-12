/**
 * QueryState: the loading / error / empty / data switch used by /fund, /agents and /sources
 * (the DESK uses `components/terminal/QueryView`, same rules).
 *
 * Data: a TanStack Query result (`useQuery(...)`). The backend may be down during the demo, so a
 * failed read shows the real reason ("backend unreachable" or the API's `{detail}`) in one muted
 * line instead of breaking the page. `isEmpty` decides when to show `emptyText`.
 *
 * Whatever `children` returns (an element, or a list of elements without keys) is passed through
 * `Children.toArray`, which gives every element a stable key — no React "unique key" warning.
 */

import type { UseQueryResult } from "@tanstack/react-query";
import { Children, type ReactNode } from "react";

import { errorText as terminalErrorText } from "@/components/terminal/InlineError";

interface QueryStateProps<T> {
  query: UseQueryResult<T>;
  isEmpty?: (data: T) => boolean;
  emptyText?: ReactNode;
  children: (data: T) => ReactNode;
}

/** Human-readable reason for a failed request or wallet action (viem errors carry `shortMessage`). */
export function errorText(error: unknown): string {
  return terminalErrorText(error);
}

export function QueryState<T>({ query, isEmpty, emptyText = "Nothing yet.", children }: QueryStateProps<T>) {
  if (query.data !== undefined && query.data !== null) {
    if (isEmpty?.(query.data)) {
      return <Muted>{emptyText}</Muted>;
    }
    return <>{Children.toArray(children(query.data))}</>;
  }
  if (query.isError) {
    return <Muted tone="error">ERR {errorText(query.error)}</Muted>;
  }
  return <Muted>LOADING…</Muted>;
}

function Muted({ tone, children }: { tone?: "error"; children: ReactNode }) {
  return <div className={tone === "error" ? "px-2 py-1.5 text-xs text-down" : "px-2 py-1.5 text-xs text-fg-muted"}>{children}</div>;
}
