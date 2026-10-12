/**
 * useLiveUpdates: the queue behind the LIVE UPDATE popup.
 *
 * It does NOT open a second SSE connection. `StreamProvider` already patches the shared TanStack
 * Query cache on every `finding` / `alert` event with `setQueryData` (a "manual" success), so this
 * hook subscribes to the query cache and pops a card for each row that arrives that way and was
 * not seen before (normal fetches and polls only mark rows as seen). It also records every
 * `/api/market` snapshot into the in-memory session series (`recordMarketRows`), the popup
 * chart's fallback when `/api/market/history` has no points.
 *
 * Dev shortcut: Alt+L injects a sample BTC finding labelled TEST (for demo recordings).
 */

"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { recordMarketRows } from "@/lib/history";
import type { Alert, Finding, MarketRow } from "@/lib/types";

import { QUERY_KEYS } from "../terminal/queryKeys";

export const MAX_VISIBLE = 3;
const MAX_POPS_PER_EVENT = 3;

export type LiveItem =
  | { key: string; kind: "finding"; finding: Finding; arrivedAt: number; test: boolean }
  | { key: string; kind: "alert"; alert: Alert; arrivedAt: number; test: boolean };

interface WithId {
  id: number | string;
}

function idsOf(list: unknown): WithId[] {
  return Array.isArray(list) ? (list.filter((row) => row && typeof row === "object" && "id" in row) as WithId[]) : [];
}

/** A sample finding for BTC, clearly labelled TEST (Alt+L). */
export function sampleFinding(nowMs: number = Date.now()): Finding {
  return {
    id: `test-${nowMs}`,
    ts: new Date(nowMs).toISOString(),
    agent: "OnchainPerps",
    asset: "BTC",
    decision: "bought",
    headline: "TEST · BTC perp funding flipped negative while OI climbs — short squeeze risk",
    direction: "bullish",
    confidence: 0.72,
    delta_p: 0.084,
    summary: "Sample finding injected with Alt+L for demo recording.",
    receipts: [
      { seller_id: "perps_crowding", tx_hash: `0x${"4fc3".repeat(16)}`, price_atomic: 20000 },
      { seller_id: "pm_edge", tx_hash: `0x${"a1b2".repeat(16)}`, price_atomic: 20000 },
    ],
  };
}

export function useLiveUpdates() {
  const client = useQueryClient();
  const [items, setItems] = useState<LiveItem[]>([]);
  const [total, setTotal] = useState(0);
  const seen = useRef<{ findings: Set<string>; alerts: Set<string> }>({ findings: new Set(), alerts: new Set() });

  const push = useCallback((item: LiveItem) => {
    setItems((current) => [item, ...current.filter((existing) => existing.key !== item.key)].slice(0, MAX_VISIBLE));
    setTotal((count) => count + 1);
  }, []);

  const dismiss = useCallback((key: string) => {
    setItems((current) => current.filter((item) => item.key !== key));
  }, []);

  useEffect(() => {
    const cache = client.getQueryCache();
    for (const row of idsOf(client.getQueryData(QUERY_KEYS.findings))) {
      seen.current.findings.add(String(row.id));
    }
    for (const row of idsOf(client.getQueryData(QUERY_KEYS.alerts))) {
      seen.current.alerts.add(String(row.id));
    }
    recordMarketRows(client.getQueryData<MarketRow[]>(QUERY_KEYS.market));

    return cache.subscribe((event) => {
      if (event.type !== "updated" || event.action.type !== "success") {
        return;
      }
      const head = event.query.queryKey[0];
      const data: unknown = event.query.state.data;
      if (head === QUERY_KEYS.market[0]) {
        recordMarketRows(data as MarketRow[]);
        return;
      }
      if (head !== QUERY_KEYS.findings[0] && head !== QUERY_KEYS.alerts[0]) {
        return;
      }
      const kind = head === QUERY_KEYS.findings[0] ? "findings" : "alerts";
      const seenIds = seen.current[kind];
      const fresh = idsOf(data).filter((row) => !seenIds.has(String(row.id)));
      for (const row of fresh) {
        seenIds.add(String(row.id));
      }
      // Only rows pushed by the live stream (setQueryData = "manual") pop a card.
      if (!event.action.manual || fresh.length === 0) {
        return;
      }
      const now = Date.now();
      // setState during a cache notification can land mid-render of another component: defer.
      queueMicrotask(() => {
        for (const row of fresh.slice(0, MAX_POPS_PER_EVENT).reverse()) {
          if (kind === "findings") {
            push({ key: `f-${row.id}`, kind: "finding", finding: row as Finding, arrivedAt: now, test: false });
          } else {
            push({ key: `a-${row.id}`, kind: "alert", alert: row as Alert, arrivedAt: now, test: false });
          }
        }
      });
    });
  }, [client, push]);

  useEffect(() => {
    function onKey(event: KeyboardEvent): void {
      if (event.altKey && !event.ctrlKey && !event.metaKey && event.code === "KeyL") {
        event.preventDefault();
        const finding = sampleFinding();
        push({ key: `f-${finding.id}`, kind: "finding", finding, arrivedAt: Date.now(), test: true });
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [push]);

  return { items, total, dismiss };
}
