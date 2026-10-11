/**
 * FindingsFeed: what the agents concluded from the research they bought, newest first.
 *
 * Data: `GET /api/findings` + the `finding` SSE event. Each finding shows its headline,
 * direction (bullish / bearish / neutral / risk), a confidence gauge (0–1), Δp, and — expanded —
 * the summary plus the receipts: every paid research item behind it with its Basescan tx.
 */

import { Badge } from "@/components/ui/Badge";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { clockTime, formatAusd, formatPercent } from "@/lib/format";
import type { Finding, FindingDirection } from "@/lib/types";
import type { Tone } from "@/lib/ui";

import { formatDeltaP } from "./PurchaseFeed";
import { TxLink } from "./TxLink";

interface FindingsFeedProps {
  findings: Finding[];
  explorerTxUrl?: string;
}

export const DIRECTION_TONE: Record<FindingDirection, Tone> = {
  bullish: "up",
  bearish: "down",
  neutral: "neutral",
  risk: "warn",
};

export function FindingsFeed({ findings, explorerTxUrl }: FindingsFeedProps) {
  if (findings.length === 0) {
    return <div className="px-3 py-4 text-center text-xs text-fg-dim">No findings yet.</div>;
  }
  return (
    <ul className="max-h-[420px] divide-y divide-line/60 overflow-auto">
      {findings.map((finding) => (
        <FindingRow key={finding.id} finding={finding} explorerTxUrl={explorerTxUrl} />
      ))}
    </ul>
  );
}

function FindingRow({ finding, explorerTxUrl }: { finding: Finding; explorerTxUrl?: string }) {
  return (
    <li className="px-3 py-2">
      <details>
        <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2">
          <span className="num text-[11px] text-fg-dim">{clockTime(finding.ts)}</span>
          <Badge tone={DIRECTION_TONE[finding.direction]}>{finding.direction}</Badge>
          <span className="text-[11px] text-fg-muted">
            {finding.agent} · {finding.asset}
          </span>
          <span className="min-w-0 flex-1 text-xs font-medium text-fg">{finding.headline}</span>
          <span className="flex w-28 items-center gap-1" title="confidence">
            <ProgressBar value={finding.confidence} max={1} tone="info" className="flex-1" />
            <span className="num text-[10px] text-fg-muted">{formatPercent(finding.confidence, 0)}</span>
          </span>
          <span className="num w-14 text-right text-[11px]">Δp {formatDeltaP(finding.delta_p)}</span>
        </summary>
        <div className="mt-2 flex flex-col gap-1.5 border-l border-line pl-3">
          <p className="text-xs text-fg-muted">{finding.summary}</p>
          {finding.decision && <p className="text-[11px] text-fg-dim">decision: {finding.decision}</p>}
          <ReceiptList receipts={finding.receipts} explorerTxUrl={explorerTxUrl} />
        </div>
      </details>
    </li>
  );
}

export function ReceiptList({
  receipts,
  explorerTxUrl,
}: {
  receipts: Finding["receipts"];
  explorerTxUrl?: string;
}) {
  if (receipts.length === 0) {
    return <span className="text-[11px] text-fg-dim">no paid receipts (free inputs only)</span>;
  }
  return (
    <ul className="flex flex-col gap-0.5 text-[11px]">
      {receipts.map((receipt) => (
        <li key={receipt.tx_hash} className="flex gap-2">
          <span className="text-fg-muted">{receipt.seller_id}</span>
          <span className="num">{formatAusd(receipt.price_atomic, true)}</span>
          <TxLink hash={receipt.tx_hash} explorerTxUrl={explorerTxUrl} />
        </li>
      ))}
    </ul>
  );
}
