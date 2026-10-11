/**
 * PmVsOptions: prediction-market price vs the options-implied probability for the same event.
 *
 * Data: `GET /api/pm-vs-options` — the rows of the latest paid `pm_edge` purchase. market_p is
 * the prediction market's price, model_p the probability implied by the options chain; edge =
 * model_p − market_p, and edge after fees is what would remain after trading costs. The tx is
 * the payment for that research item.
 */

import { Table, type Column } from "@/components/ui/Table";
import { formatPercent } from "@/lib/format";
import type { PmVsOptionsRow } from "@/lib/types";
import { cx } from "@/lib/ui";

import { TxLink } from "./TxLink";

function signedPercent(fraction: number): string {
  return `${fraction > 0 ? "+" : ""}${formatPercent(fraction)}`;
}

function columns(explorerTxUrl?: string): Column<PmVsOptionsRow>[] {
  return [
    {
      key: "market",
      header: "Market",
      render: (row) => (
        <span title={row.title}>
          {row.asset} {row.strike.toLocaleString("en-US")} <span className="text-fg-dim">{row.expiry}</span>
        </span>
      ),
    },
    { key: "pm", header: "PM", align: "right", render: (row) => <span className="num">{formatPercent(row.market_p)}</span> },
    { key: "opt", header: "Options", align: "right", render: (row) => <span className="num">{formatPercent(row.model_p)}</span> },
    {
      key: "edge",
      header: "Edge net",
      align: "right",
      render: (row) => (
        <span className={cx("num", row.edge_after_fees > 0 ? "text-up" : "text-fg-muted")} title={`gross ${signedPercent(row.edge)}`}>
          {signedPercent(row.edge_after_fees)}
        </span>
      ),
    },
    { key: "tx", header: "Tx", render: (row) => <TxLink hash={row.tx_hash} explorerTxUrl={explorerTxUrl} /> },
  ];
}

export function PmVsOptions({ rows, explorerTxUrl }: { rows: PmVsOptionsRow[]; explorerTxUrl?: string }) {
  return <Table columns={columns(explorerTxUrl)} rows={rows} rowKey={(row) => `${row.ticker}-${row.strike}`} maxHeight={220} />;
}
