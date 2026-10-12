/**
 * QueryView: the loading / error / empty / data switch every API-backed panel goes through.
 *
 * Props:
 *   query     a TanStack Query result (`useQuery(...)`)
 *   label     what is loading, for the messages ("PURCHASES")
 *   isEmpty   decides when to show `empty` instead of the data
 *   empty     the one muted line shown when there is nothing yet
 *   children  render function for the data
 *
 * Once data has arrived it stays on screen even if a later refresh fails (the top bar LED shows
 * the connection). A failed first load shows the backend's `detail` as a red one-liner.
 */

import type { UseQueryResult } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { InlineError, MutedLine } from "./InlineError";

interface QueryViewProps<T> {
  query: UseQueryResult<T>;
  label?: string;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  children: (data: T) => ReactNode;
}

export function QueryView<T>({ query, label, isEmpty, empty = "Nothing yet.", children }: QueryViewProps<T>) {
  if (query.data !== undefined && query.data !== null) {
    if (isEmpty?.(query.data)) {
      return <MutedLine>{empty}</MutedLine>;
    }
    return <>{children(query.data)}</>;
  }
  if (query.isError) {
    return <InlineError error={query.error} prefix={label} />;
  }
  return <MutedLine>LOADING{label ? ` ${label}` : ""}…</MutedLine>;
}
