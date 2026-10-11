/**
 * MarketStrip: one-line ticker of watchlist prices (BTC, ETH, SOL, NVDA, COIN, …).
 *
 * Data: `GET /api/market` and the `market` SSE event. These come from FREE sources only (never
 * paid), so each quote carries its source and a "delayed" tag when the source is delayed.
 */

import type { UseQueryResult } from "@tanstack/react-query";

import { formatSignedPercent } from "@/lib/format";
import type { MarketRow } from "@/lib/types";
import { cx } from "@/lib/ui";

import { errorText } from "./QueryState";

interface MarketStripProps {
  query: UseQueryResult<MarketRow[]>;
}

/** Prices above 1000 get no decimals, small prices get 4 (so SOL / tokens stay readable). */
function formatPrice(price: number): string {
  const decimals = price >= 1000 ? 0 : price >= 1 ? 2 : 4;
  return price.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

export function MarketStrip({ query }: MarketStripProps) {
  const rows = query.data ?? [];
  return (
    <div className="flex min-h-8 items-center gap-4 overflow-x-auto rounded-sm border border-line bg-surface px-3 py-1.5">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-dim">Market</span>
      {rows.length === 0 && (
        <span className="text-[11px] text-fg-dim">
          {query.isError ? `Unavailable: ${errorText(query.error)}` : query.isLoading ? "Loading…" : "No quotes yet."}
        </span>
      )}
      {rows.map((row) => (
        <MarketQuote key={row.symbol} row={row} />
      ))}
    </div>
  );
}

function MarketQuote({ row }: { row: MarketRow }) {
  const change = row.change_24h_pct;
  const changeTone = change === null ? "text-fg-dim" : change >= 0 ? "text-up" : "text-down";
  return (
    <span className="flex shrink-0 items-baseline gap-1.5" title={`source: ${row.source}`}>
      <span className="text-[11px] font-semibold text-fg-muted">{row.symbol}</span>
      <span className="num text-xs text-fg">{formatPrice(row.price)}</span>
      <span className={cx("num text-[11px]", changeTone)}>{formatSignedPercent(change)}</span>
      {row.delayed && <span className="text-[9px] uppercase text-warn/80">delayed</span>}
    </span>
  );
}
