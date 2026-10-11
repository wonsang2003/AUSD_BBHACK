/**
 * PurchaseBlotter (BLTR): every buy decision, newest first — bought, skipped by the VOI gate, failed.
 *
 * Data: `GET /api/purchases` + the `purchase` SSE event. One single-line row per decision; long
 * reasons are cut with an ellipsis and shown in full on hover. EVOI is the expected value of the
 * information in dollars (max 3 decimals); Δp the probability shift it caused. ✓ COMPLIANCE when
 * payer and payTo were checked with `isAccountFrozen` before paying.
 */

import { DataTable, type Column } from "@/components/terminal/DataTable";
import { Pill, type PillTone } from "@/components/terminal/Pill";
import { TxHash } from "@/components/terminal/TxHash";
import { EMPTY, fmtAusd, fmtSigned, toNumberSafe } from "@/components/terminal/format";
import { etTime } from "@/components/terminal/marketTime";
import { signTone, TONE_TEXT } from "@/components/terminal/tones";
import type { Purchase, PurchaseStatus } from "@/lib/types";

const STATUS_TONE: Record<PurchaseStatus, PillTone> = {
  bought: "green",
  skipped: "grey",
  failed: "red",
};

/** Dollars with at most 3 decimals (at least 2): 31.91543 → "$31.915", 0.5 → "$0.50". */
export function fmtEvoi(value: unknown): string {
  const usd = toNumberSafe(value);
  if (usd === null) {
    return EMPTY;
  }
  const text = Math.abs(usd).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 3 });
  return usd < 0 ? `-$${text}` : `$${text}`;
}

/** Newest first, whatever order the API / SSE delivered. */
function newestFirst(rows: Purchase[]): Purchase[] {
  return [...rows].sort((a, b) => (Date.parse(b.ts) || 0) - (Date.parse(a.ts) || 0));
}

const COLUMNS: Column<Purchase>[] = [
  { key: "ts", header: "Time ET", width: 70, render: (p) => <span className="num text-fg-muted">{etTime(p.ts)}</span>, title: (p) => p.ts },
  { key: "agent", header: "Agent", width: 84, render: (p) => p.agent, title: (p) => p.agent },
  {
    key: "vendor",
    header: "Vendor",
    width: 140,
    title: (p) => `${p.seller_id}${p.live_vendor ? "" : " (demo vendor, testnet stand-in)"}`,
    render: (p) => (
      <div className="flex min-w-0 items-center gap-1">
        <span className="truncate">{p.seller_id}</span>
        {p.live_vendor ? null : <Pill tone="grey">DEMO</Pill>}
      </div>
    ),
  },
  { key: "asset", header: "Asset", width: 60, render: (p) => <span className="font-bold">{p.asset}</span>, title: (p) => p.asset },
  { key: "price", header: "Price AUSD", width: 80, align: "right", render: (p) => <span className="num">{fmtAusd(p.price_atomic)}</span> },
  {
    key: "evoi",
    header: "EVOI $",
    width: 72,
    align: "right",
    headerTitle: "Expected value of the information, in dollars",
    render: (p) => <span className="num">{fmtEvoi(p.evoi_usd)}</span>,
  },
  {
    key: "status",
    header: "Decision",
    width: 76,
    render: (p) => <Pill tone={STATUS_TONE[p.status] ?? "grey"}>{p.status}</Pill>,
  },
  {
    key: "reason",
    header: "Reason",
    render: (p) => <span className={p.status === "failed" ? "text-down" : "text-fg-muted"}>{p.reason || EMPTY}</span>,
    title: (p) => p.reason || undefined,
  },
  {
    key: "dp",
    header: "Δp",
    width: 62,
    align: "right",
    headerTitle: "Probability shift caused by the research",
    render: (p) => <span className={`num ${TONE_TEXT[signTone(toNumberSafe(p.delta_p))]}`}>{fmtSigned(p.delta_p, 3)}</span>,
  },
  { key: "tx", header: "Tx", width: 104, render: (p) => <TxHash hash={p.tx_hash} showTag={false} /> },
  {
    key: "cc",
    header: "Checks",
    width: 102,
    render: (p) =>
      p.compliance_checked ? (
        <Pill tone="green" outline title="payer and payTo checked with isAccountFrozen before paying">
          ✓ COMPLIANCE
        </Pill>
      ) : null,
  },
];

export function PurchaseBlotter({ purchases }: { purchases: Purchase[] }) {
  return (
    <DataTable
      columns={COLUMNS}
      rows={newestFirst(purchases)}
      rowKey={(p, index) => `${p.id}-${index}`}
      rowClassName={(p) => (p.status === "skipped" ? "text-fg-muted" : undefined)}
      maxHeight={360}
      empty="No purchases yet — start a session or RUN AGENTS NOW."
    />
  );
}
