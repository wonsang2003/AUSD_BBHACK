/**
 * TickerTape: one row of watchlist quotes under the tabs — SYMBOL  price  ±% (green / red / white)
 * and a "DLY" tag on delayed quotes. Data: `GET /api/market` (free sources only), refreshed every
 * 30 s and live via the `market` SSE event. Errors and empty data collapse to one muted line.
 */

"use client";

import { useQuery } from "@tanstack/react-query";

import { getMarket } from "@/lib/api";
import type { MarketRow } from "@/lib/types";

import { fmtPrice, fmtSignedPct } from "./format";
import { errorText } from "./InlineError";
import { QUERY_KEYS } from "./queryKeys";
import { cls, signTone, TONE_TEXT } from "./tones";

const MARKET_REFRESH_MS = 30_000;

function Quote({ row }: { row: MarketRow }) {
  const tone = signTone(row.change_24h_pct);
  return (
    <span className="flex items-baseline gap-1.5 whitespace-nowrap" title={`${row.symbol} · source ${row.source}${row.delayed ? " · delayed" : ""}`}>
      <span className="font-bold text-amber">{row.symbol}</span>
      <span className="num text-fg">{fmtPrice(row.price)}</span>
      <span className={cls("num", TONE_TEXT[tone])}>{fmtSignedPct(row.change_24h_pct)}</span>
      {row.delayed ? <span className="text-[10px] font-bold text-fg-muted">DLY</span> : null}
    </span>
  );
}

export function TickerTape() {
  const market = useQuery({ queryKey: QUERY_KEYS.market, queryFn: getMarket, refetchInterval: MARKET_REFRESH_MS });
  const rows = Array.isArray(market.data) ? market.data : [];
  let content;
  if (rows.length > 0) {
    content = rows.map((row) => <Quote key={row.symbol} row={row} />);
  } else if (market.isError) {
    content = <span className="text-fg-muted">MARKET DATA UNAVAILABLE: {errorText(market.error)}</span>;
  } else if (market.isLoading) {
    content = <span className="text-fg-muted">LOADING QUOTES…</span>;
  } else {
    content = <span className="text-fg-muted">NO QUOTES (watchlist empty or sources offline)</span>;
  }
  return (
    <div className="flex h-[24px] items-center gap-5 overflow-x-auto overflow-y-hidden border-b border-line bg-[#050505] px-3 text-[12px]">
      {content}
    </div>
  );
}
