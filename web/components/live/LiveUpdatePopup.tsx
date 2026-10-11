/**
 * LiveUpdatePopup: Bloomberg-style alert cards for live `finding` / `alert` stream events.
 *
 * Bottom-right stack, at most 3 cards, each auto-dismissed after 12 s (paused while hovered),
 * with a close button. Clicking a card jumps to the FINDINGS panel on F1 DESK.
 * A finding card shows direction, headline, confidence, Δp, agent, receipts (tx hashes in cyan)
 * and a sparkline of the asset (`/api/market/history`, else the session's ticker-tape quotes)
 * with the update moment marked in amber. Alerts (no asset) show level-colored, without a chart.
 * Mounted once, globally, by `TerminalChrome`. Alt+L injects a TEST card (see useLiveUpdates).
 */

"use client";

import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState, type MouseEvent } from "react";

import { fetchHistory, normalizeSymbol, sessionSeries, type PricePoint } from "@/lib/history";
import type { Alert, Finding } from "@/lib/types";

import { TxHash } from "../terminal/TxHash";
import { Sparkline } from "./Sparkline";
import { useLiveUpdates, type LiveItem } from "./useLiveUpdates";

const AUTO_DISMISS_MS = 12_000;
const TICK_MS = 250;
const GREEN = "#3ddc84";
const RED = "#ff4d4d";
const AMBER = "#ffa028";
const CYAN = "#4fc3f7";
const WARN = "#ffcc00";
const MUTED = "#9a9a9a";
const FINDINGS_SCROLL_RETRIES = 15;
const FINDINGS_SCROLL_RETRY_MS = 200;

function timeEt(iso: string | number): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "--:--:-- ET";
  }
  const text = date.toLocaleTimeString("en-US", {
    timeZone: "America/New_York",
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  return `${text} ET`;
}

function direction(finding: Finding): { glyph: string; color: string; label: string } {
  switch (finding.direction) {
    case "bullish":
      return { glyph: "▲", color: GREEN, label: "BULLISH" };
    case "bearish":
      return { glyph: "▼", color: RED, label: "BEARISH" };
    case "risk":
      return { glyph: "◆", color: AMBER, label: "RISK" };
    default:
      return { glyph: "●", color: MUTED, label: "NEUTRAL" };
  }
}

function levelColor(level: Alert["level"]): string {
  return level === "critical" ? RED : level === "warn" ? WARN : CYAN;
}

function signed(value: number, decimals: number): string {
  if (!Number.isFinite(value)) {
    return "—";
  }
  return `${value > 0 ? "+" : ""}${value.toFixed(decimals)}`;
}

/** Find the FINDINGS panel on the page (id, data attribute, or a title text) and scroll to it. */
function scrollToFindings(): boolean {
  let target: Element | null = document.getElementById("findings") ?? document.querySelector('[data-panel="findings"]');
  if (!target) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (!parent || parent.closest("[data-live-popup], header")) {
        continue;
      }
      if (/^\s*findings\b/i.test(node.textContent ?? "")) {
        target = parent.closest("section, article, [class*='panel'], [class*='Panel']") ?? parent;
        break;
      }
    }
  }
  if (!target) {
    return false;
  }
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  const element = target as HTMLElement;
  const previous = element.style.outline;
  element.style.outline = `2px solid ${AMBER}`;
  window.setTimeout(() => {
    element.style.outline = previous;
  }, 1_500);
  return true;
}

function useJumpToFindings(): () => void {
  const router = useRouter();
  const pathname = usePathname();
  return () => {
    if (pathname !== "/") {
      router.push("/");
    }
    let tries = 0;
    const attempt = () => {
      if (scrollToFindings() || tries >= FINDINGS_SCROLL_RETRIES) {
        return;
      }
      tries += 1;
      window.setTimeout(attempt, FINDINGS_SCROLL_RETRY_MS);
    };
    window.setTimeout(attempt, pathname === "/" ? 0 : FINDINGS_SCROLL_RETRY_MS);
  };
}

function PriceChart({ asset, markMs }: { asset: string; markMs: number }) {
  const symbol = normalizeSymbol(asset);
  const history = useQuery({
    queryKey: ["market-history", symbol],
    queryFn: () => fetchHistory(symbol),
    staleTime: 60_000,
    enabled: symbol.length > 0,
  });
  if (history.isLoading) {
    return (
      <div className="flex h-[90px] w-[280px] items-center justify-center border border-[#262626] text-[11px] text-[#9a9a9a]">
        LOADING {symbol} …
      </div>
    );
  }
  let points: PricePoint[] = history.data?.points ?? [];
  let label = history.data?.source ? `${symbol} · ${history.data.source}${history.data.delayed ? " · DLY" : ""}` : symbol;
  if (points.length < 2) {
    points = sessionSeries(symbol);
    label = `${symbol} · session tape`;
  }
  return <Sparkline points={points} markT={Math.floor(markMs / 1000)} label={label} />;
}

function FindingBody({ finding, markMs }: { finding: Finding; markMs: number }) {
  const dir = direction(finding);
  const receipts = Array.isArray(finding.receipts) ? finding.receipts : [];
  return (
    <div className="space-y-1.5 px-2.5 py-2">
      <div className="flex items-start gap-2">
        <span className="text-[15px] leading-5" style={{ color: dir.color }}>
          {dir.glyph}
        </span>
        <span className="text-[12px] leading-5 text-[#e6e6e6]">{finding.headline}</span>
      </div>
      <div className="grid grid-cols-3 gap-1 text-[11px] tabular-nums">
        <span>
          <span className="text-[#9a9a9a]">DIR </span>
          <span style={{ color: dir.color }}>{dir.label}</span>
        </span>
        <span>
          <span className="text-[#9a9a9a]">CONF </span>
          <span className="text-[#e6e6e6]">{Number.isFinite(finding.confidence) ? `${Math.round(finding.confidence * 100)}%` : "—"}</span>
        </span>
        <span>
          <span className="text-[#9a9a9a]">Δp </span>
          <span style={{ color: finding.delta_p >= 0 ? GREEN : RED }}>{signed(finding.delta_p, 3)}</span>
        </span>
      </div>
      <div className="flex items-center justify-between text-[11px]">
        <span>
          <span className="text-[#9a9a9a]">AGENT </span>
          <span className="text-[#e6e6e6]">{finding.agent}</span>
        </span>
        <span className="text-[#9a9a9a]">
          RECEIPTS <span className="tabular-nums text-[#e6e6e6]">{receipts.length}</span>
        </span>
      </div>
      {receipts.length > 0 && (
        <div className="flex flex-wrap gap-x-3 text-[11px]" style={{ color: CYAN }} onClick={(event) => event.stopPropagation()}>
          {receipts.slice(0, 2).map((receipt) => (
            <span key={receipt.tx_hash} title={receipt.seller_id}>
              <TxHash hash={receipt.tx_hash} showTag={false} />
            </span>
          ))}
          {receipts.length > 2 && <span className="text-[#9a9a9a]">+{receipts.length - 2}</span>}
        </div>
      )}
      <PriceChart asset={finding.asset} markMs={markMs} />
    </div>
  );
}

function AlertBody({ alert }: { alert: Alert }) {
  const color = levelColor(alert.level);
  return (
    <div className="space-y-1 px-2.5 py-2 text-[12px]">
      <div className="flex items-center gap-2 text-[11px]">
        <span className="px-1 font-bold uppercase text-black" style={{ background: color }}>
          {alert.level}
        </span>
        <span className="uppercase text-[#9a9a9a]">{alert.kind}</span>
      </div>
      <div style={{ color }}>{alert.message}</div>
    </div>
  );
}

function LiveCard({ item, onClose, onJump }: { item: LiveItem; onClose: () => void; onJump: () => void }) {
  const [remaining, setRemaining] = useState(AUTO_DISMISS_MS);
  const hovered = useRef(false);
  const closeRef = useRef(onClose);

  useEffect(() => {
    closeRef.current = onClose;
  });

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (hovered.current) {
        return;
      }
      setRemaining((left) => {
        const next = left - TICK_MS;
        if (next <= 0) {
          window.setTimeout(() => closeRef.current(), 0);
        }
        return Math.max(next, 0);
      });
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, []);

  const isFinding = item.kind === "finding";
  const subject = isFinding ? normalizeSymbol(item.finding.asset) || "—" : `ALERT · ${item.alert.kind}`;
  const ts = isFinding ? item.finding.ts : item.alert.ts;
  const border = isFinding ? AMBER : levelColor(item.alert.level);

  const close = (event: MouseEvent) => {
    event.stopPropagation();
    onClose();
  };

  return (
    <div
      role="alert"
      onMouseEnter={() => {
        hovered.current = true;
      }}
      onMouseLeave={() => {
        hovered.current = false;
      }}
      onClick={onJump}
      title="Jump to FINDINGS on F1 DESK"
      className="pointer-events-auto w-[304px] cursor-pointer bg-black font-mono shadow-[0_6px_24px_rgba(0,0,0,0.7)]"
      style={{ border: `1px solid ${border}`, animation: "pdLiveIn 180ms ease-out" }}
    >
      <div className="flex items-center justify-between px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide text-black" style={{ background: AMBER }}>
        <span className="truncate tabular-nums">
          {item.test && <span className="mr-1 bg-black px-1 text-[#ffa028]">TEST</span>}
          LIVE · {subject} · {timeEt(ts || item.arrivedAt)}
        </span>
        <button
          type="button"
          aria-label="Close live update"
          onClick={close}
          className="ml-2 px-1 leading-none text-black hover:bg-black hover:text-[#ffa028]"
        >
          ✕
        </button>
      </div>
      {isFinding ? <FindingBody finding={item.finding} markMs={item.arrivedAt} /> : <AlertBody alert={item.alert} />}
      <div className="h-[2px] bg-[#1a1a1a]">
        <div className="h-full" style={{ width: `${(remaining / AUTO_DISMISS_MS) * 100}%`, background: border }} />
      </div>
    </div>
  );
}

export function LiveUpdatePopup() {
  const { items, dismiss } = useLiveUpdates();
  const jump = useJumpToFindings();
  if (items.length === 0) {
    return null;
  }
  return (
    <div
      data-live-popup
      aria-live="polite"
      className="pointer-events-none fixed right-4 bottom-4 z-50 flex flex-col-reverse gap-2"
    >
      <style>{"@keyframes pdLiveIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}"}</style>
      {items.map((item) => (
        <LiveCard key={item.key} item={item} onClose={() => dismiss(item.key)} onJump={jump} />
      ))}
    </div>
  );
}
