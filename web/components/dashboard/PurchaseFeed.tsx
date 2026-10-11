/**
 * PurchaseFeed: every buy decision the agents made, newest first — including the ones they
 * decided NOT to make (greyed, with the reason), which is the point of the VOI gate.
 *
 * Data: `GET /api/purchases` + the `purchase` SSE event. Columns: time, agent, seller (our
 * sellers are "demo vendor" testnet stand-ins), params, price in AUSD, EVOI (expected value of
 * the information, USD), status, Δp (how much the result moved the agent's belief), the Basescan
 * payment tx, and a "✓ compliance check" badge when payer and payTo were checked with
 * `isAccountFrozen` before paying.
 */

import { Badge } from "@/components/ui/Badge";
import { Table, type Column } from "@/components/ui/Table";
import { Tooltip } from "@/components/ui/Tooltip";
import { clockTime, formatAusd, formatUsd } from "@/lib/format";
import type { Purchase, PurchaseStatus } from "@/lib/types";
import type { Tone } from "@/lib/ui";

import { TxLink } from "./TxLink";

interface PurchaseFeedProps {
  purchases: Purchase[];
  explorerTxUrl?: string;
}

const STATUS_TONE: Record<PurchaseStatus, Tone> = { bought: "up", skipped: "neutral", failed: "down" };
const MAX_PARAM_CHARS = 48;

/** {asset: "NVDA", expiry: "2026-10-17"} → "asset=NVDA expiry=2026-10-17" (truncated). */
export function formatParams(params: Record<string, unknown>): string {
  const text = Object.entries(params)
    .map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : String(value)}`)
    .join(" ");
  return text.length > MAX_PARAM_CHARS ? `${text.slice(0, MAX_PARAM_CHARS - 1)}…` : text;
}

/** Signed belief change with 3 decimals, e.g. "+0.042". */
export function formatDeltaP(deltaP: number | null): string {
  if (deltaP === null || !Number.isFinite(deltaP)) {
    return "—";
  }
  return `${deltaP > 0 ? "+" : ""}${deltaP.toFixed(3)}`;
}

function columns(explorerTxUrl?: string): Column<Purchase>[] {
  return [
    { key: "ts", header: "Time", render: (p) => <span className="num text-fg-muted">{clockTime(p.ts)}</span> },
    { key: "agent", header: "Agent", render: (p) => p.agent },
    {
      key: "seller",
      header: "Seller",
      render: (p) => (
        <span className="flex items-center gap-1">
          {p.seller_id}
          {!p.live_vendor && <Badge>demo vendor</Badge>}
        </span>
      ),
    },
    {
      key: "params",
      header: "Params",
      render: (p) => (
        <span className="num text-[11px] text-fg-muted" title={JSON.stringify(p.params)}>
          {formatParams({ asset: p.asset, ...p.params })}
        </span>
      ),
    },
    { key: "price", header: "Price AUSD", align: "right", render: (p) => <span className="num">{formatAusd(p.price_atomic)}</span> },
    { key: "evoi", header: "EVOI", align: "right", render: (p) => <span className="num">{formatUsd(p.evoi_usd, 3)}</span> },
    { key: "status", header: "Status", render: (p) => <StatusCell purchase={p} /> },
    { key: "dp", header: "Δp", align: "right", render: (p) => <span className="num">{formatDeltaP(p.delta_p)}</span> },
    {
      key: "tx",
      header: "Tx",
      render: (p) => (
        <span className="flex items-center gap-1">
          <TxLink hash={p.tx_hash} explorerTxUrl={explorerTxUrl} />
          {p.compliance_checked && (
            <Tooltip content="payer and payTo checked with isAccountFrozen() before paying">
              <Badge tone="up">✓ compliance check</Badge>
            </Tooltip>
          )}
        </span>
      ),
    },
  ];
}

function StatusCell({ purchase }: { purchase: Purchase }) {
  return (
    <span className="flex flex-col">
      <Badge tone={STATUS_TONE[purchase.status]} className="self-start">
        {purchase.status}
      </Badge>
      {purchase.status !== "bought" && purchase.reason && (
        <span className="max-w-[220px] text-[10px] text-fg-dim">{purchase.reason}</span>
      )}
    </span>
  );
}

export function PurchaseFeed({ purchases, explorerTxUrl }: PurchaseFeedProps) {
  return (
    <Table
      columns={columns(explorerTxUrl)}
      rows={purchases}
      rowKey={(p) => p.id}
      rowClassName={(p) => (p.status === "skipped" ? "opacity-50" : undefined)}
      empty="No purchase decisions yet — start a session."
      maxHeight={360}
    />
  );
}
