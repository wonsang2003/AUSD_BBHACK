/**
 * CatalogTable: every data source PocketDesk knows about, and honestly how (or whether) it can
 * be paid per call from the budget.
 *
 * Data: `GET /api/catalog` (`backend/app/sellers/registry.py`). Rows: our demo vendors (testnet
 * x402 sellers in AUSD — the only ones agents actually pay), real external x402 vendors (flagged:
 * mainnet USDC, never paid), metered APIs (roadmap), free inputs, and the "not sold per call"
 * row for data that only comes as a subscription / contract.
 */

import { Badge } from "@/components/ui/Badge";
import { Table, type Column } from "@/components/ui/Table";
import type { CatalogEntry, CatalogStatus, PayableMode } from "@/lib/types";
import type { Tone } from "@/lib/ui";

const STATUS_TONE: Record<CatalogStatus, Tone> = {
  live_ausd_seller: "up",
  external_x402_flagged: "warn",
  metered_adapter_roadmap: "neutral",
  free_input: "info",
  not_per_call: "down",
};

const PAYABLE_TEXT: Record<PayableMode, string> = {
  x402_ausd_testnet: "x402 · AUSD (testnet)",
  x402_usdc_mainnet: "x402 · USDC mainnet",
  metered_api_key: "API key, metered",
  free: "free",
  subscription_contract: "subscription / contract",
};

const TIER_TONE: Record<CatalogEntry["trader_value"], Tone> = { S: "up", A: "info", B: "neutral" };

const COLUMNS: Column<CatalogEntry>[] = [
  {
    key: "tier",
    header: "Tier",
    render: (row) => (
      <Badge tone={TIER_TONE[row.trader_value]} title="trader value">
        {row.trader_value}
      </Badge>
    ),
  },
  {
    key: "source",
    header: "Source",
    render: (row) => (
      <div className="flex max-w-[420px] flex-col">
        <span className="font-semibold text-fg">{row.name}</span>
        <span className="text-[11px] text-fg-muted">{row.description}</span>
        <span className="text-[10px] text-fg-dim">
          {row.source} · {row.sector}
          {row.notes ? ` · ${row.notes}` : ""}
        </span>
      </div>
    ),
  },
  {
    key: "pricing",
    header: "Pricing",
    render: (row) => (
      <div className="flex flex-col">
        <span className="num">{row.price}</span>
        <span className="text-[10px] text-fg-dim">{row.pricing_model}</span>
      </div>
    ),
  },
  { key: "payable", header: "Payable mode", render: (row) => <span className="text-[11px]">{PAYABLE_TEXT[row.payable_mode] ?? row.payable_mode}</span> },
  {
    key: "status",
    header: "Status",
    render: (row) => (
      <Badge tone={STATUS_TONE[row.status] ?? "neutral"} className="normal-case">
        {row.label}
      </Badge>
    ),
  },
];

export function CatalogTable({ rows }: { rows: CatalogEntry[] }) {
  return <Table columns={COLUMNS} rows={rows} rowKey={(row) => row.id} empty="Catalog is empty." />;
}
