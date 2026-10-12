/**
 * FindingsWire (WIRE): what the agents concluded from the research they bought, newest first.
 *
 * Data: `GET /api/findings` + the `finding` SSE event. One line per finding: ET time, asset,
 * direction (▲ bullish green, ▼ bearish red, ■ neutral, ! risk yellow), headline (ellipsis),
 * confidence %, Δp. Clicking a line expands the summary and the paid receipts (tx hashes).
 */

"use client";

import { useState } from "react";

import { MutedLine } from "@/components/terminal/InlineError";
import { TxHash } from "@/components/terminal/TxHash";
import { fmtAusd, fmtPct, fmtSigned, toNumberSafe } from "@/components/terminal/format";
import { etShortTime } from "@/components/terminal/marketTime";
import { cls, signTone, TONE_TEXT, type TermTone } from "@/components/terminal/tones";
import type { Finding, FindingDirection, Receipt } from "@/lib/types";

const DIRECTION: Record<FindingDirection, { glyph: string; tone: TermTone }> = {
  bullish: { glyph: "▲", tone: "up" },
  bearish: { glyph: "▼", tone: "down" },
  neutral: { glyph: "■", tone: "muted" },
  risk: { glyph: "!", tone: "warn" },
};

export function ReceiptRows({ receipts }: { receipts: Receipt[] | undefined }) {
  const rows = receipts ?? [];
  if (rows.length === 0) {
    return <div className="text-[11px] text-fg-muted">no paid receipts (free inputs only)</div>;
  }
  return (
    <div className="flex flex-col">
      {rows.map((receipt, index) => (
        <div key={`${receipt.tx_hash}-${index}`} className="flex items-center gap-2 text-[12px]">
          <span className="min-w-0 flex-1 truncate text-fg-muted" title={receipt.seller_id}>
            {receipt.seller_id}
          </span>
          <span className="num">{fmtAusd(receipt.price_atomic)} AUSD</span>
          <TxHash hash={receipt.tx_hash} showTag={false} />
        </div>
      ))}
    </div>
  );
}

function FindingLine({ finding, open, onToggle }: { finding: Finding; open: boolean; onToggle: () => void }) {
  const direction = DIRECTION[finding.direction] ?? DIRECTION.neutral;
  return (
    <div className="border-b border-[#1c1c1c] even:bg-surface-2">
      <button
        type="button"
        onClick={onToggle}
        title={finding.headline}
        className="grid w-full grid-cols-[40px_52px_14px_minmax(0,1fr)_38px_52px] items-center gap-1.5 px-2 py-[3px] text-left hover:bg-[#1d1d1d]"
      >
        <span className="num text-fg-muted">{etShortTime(finding.ts)}</span>
        <span className="truncate font-bold">{finding.asset}</span>
        <span className={cls("font-bold", TONE_TEXT[direction.tone])} title={finding.direction}>
          {direction.glyph}
        </span>
        <span className="truncate">{finding.headline}</span>
        <span className="num text-right text-fg-muted" title="confidence">
          {fmtPct(finding.confidence, 0)}
        </span>
        <span className={cls("num text-right", TONE_TEXT[signTone(toNumberSafe(finding.delta_p))])}>{fmtSigned(finding.delta_p, 3)}</span>
      </button>
      {open ? (
        <div className="flex flex-col gap-1 border-l-2 border-amber bg-black px-2 py-1.5">
          <div className="text-[11px] text-fg-muted">
            {finding.agent} · {finding.direction.toUpperCase()}
            {finding.decision ? ` · ${finding.decision}` : ""}
          </div>
          <p className="whitespace-pre-wrap text-[12px] leading-snug">{finding.summary}</p>
          <ReceiptRows receipts={finding.receipts} />
        </div>
      ) : null}
    </div>
  );
}

export function FindingsWire({ findings }: { findings: Finding[] }) {
  const [openId, setOpenId] = useState<string | null>(null);
  if (findings.length === 0) {
    return <MutedLine>No findings yet.</MutedLine>;
  }
  return (
    <div className="max-h-[360px] overflow-auto">
      {findings.map((finding, index) => {
        const id = `${finding.id}-${index}`;
        return (
          <FindingLine key={id} finding={finding} open={openId === id} onToggle={() => setOpenId(openId === id ? null : id)} />
        );
      })}
    </div>
  );
}
