/**
 * The small row-4 panels of the F1 DESK:
 *   EventCalendar  GET /api/events-today — kind pill, title, asset, the agent it boosts
 *   PmVsOptions    GET /api/pm-vs-options — prediction-market price vs the options-implied
 *                  probability for the latest pm_edge purchase; edge after fees green / red
 *   AlertsLog      GET /api/alerts + `alert` SSE event (also sent to Telegram)
 *   SavingsMeter   State.savings — month-to-date research spend vs the prorated subscriptions
 *                  it replaces
 */

import { DataTable, type Column } from "@/components/terminal/DataTable";
import { MutedLine } from "@/components/terminal/InlineError";
import { Meter } from "@/components/terminal/Meter";
import { Pill, type PillTone } from "@/components/terminal/Pill";
import { KeyValue } from "@/components/terminal/Stat";
import { ausdNumber, fmtPct, fmtUsd, toNumberSafe } from "@/components/terminal/format";
import { etShortTime } from "@/components/terminal/marketTime";
import { signTone, TONE_TEXT } from "@/components/terminal/tones";
import type { Alert, AlertLevel, EventKind, EventToday, PmVsOptionsRow, SavingsState } from "@/lib/types";

// ─── event calendar ──────────────────────────────────────────────────────────

const EVENT_TONE: Record<EventKind, PillTone> = {
  earnings: "amber",
  fomc: "red",
  cpi: "yellow",
  pm_expiry: "cyan",
  crypto: "white",
};

const EVENT_COLUMNS: Column<EventToday>[] = [
  {
    key: "kind",
    header: "Kind",
    width: 82,
    render: (e) => <Pill tone={EVENT_TONE[e.kind] ?? "grey"}>{String(e.kind).replace("_", " ")}</Pill>,
  },
  { key: "title", header: "Event", render: (e) => e.title, title: (e) => e.title },
  { key: "asset", header: "Asset", width: 56, render: (e) => <span className="font-bold">{e.asset ?? "—"}</span> },
  {
    key: "boost",
    header: "Boosts",
    width: 78,
    headerTitle: "Agent whose allocation this event boosts",
    render: (e) => <span className="text-fg-muted">{e.boosted_agent ?? "—"}</span>,
    title: (e) => e.boosted_agent ?? undefined,
  },
];

export function EventCalendar({ events }: { events: EventToday[] }) {
  return (
    <DataTable
      columns={EVENT_COLUMNS}
      rows={events}
      rowKey={(e, index) => `${e.kind}-${e.title}-${index}`}
      maxHeight={240}
      empty="No scheduled events today."
    />
  );
}

// ─── prediction market vs options ────────────────────────────────────────────

function signedPct(fraction: number): string {
  return `${fraction > 0 ? "+" : ""}${fmtPct(fraction)}`;
}

const PM_COLUMNS: Column<PmVsOptionsRow>[] = [
  {
    key: "contract",
    header: "Contract",
    render: (r) => (
      <span>
        <span className="font-bold">{r.asset}</span> {toNumberSafe(r.strike)?.toLocaleString("en-US") ?? "—"}{" "}
        <span className="text-fg-muted">{r.expiry}</span>
      </span>
    ),
    title: (r) => `${r.title} (${r.ticker})`,
  },
  { key: "mkt", header: "Mkt p", width: 58, align: "right", render: (r) => <span className="num">{fmtPct(r.market_p)}</span> },
  { key: "model", header: "Model p", width: 64, align: "right", render: (r) => <span className="num">{fmtPct(r.model_p)}</span> },
  {
    key: "edge",
    header: "Edge",
    width: 60,
    align: "right",
    render: (r) => <span className={`num ${TONE_TEXT[signTone(toNumberSafe(r.edge))]}`}>{signedPct(Number(r.edge) || 0)}</span>,
  },
  {
    key: "net",
    header: "Net fees",
    width: 68,
    align: "right",
    headerTitle: "Edge after fees",
    render: (r) => (
      <span className={`num font-bold ${TONE_TEXT[signTone(toNumberSafe(r.edge_after_fees))]}`}>
        {signedPct(Number(r.edge_after_fees) || 0)}
      </span>
    ),
  },
];

export function PmVsOptions({ rows }: { rows: PmVsOptionsRow[] }) {
  return (
    <DataTable
      columns={PM_COLUMNS}
      rows={rows}
      rowKey={(r, index) => `${r.ticker}-${r.strike}-${index}`}
      maxHeight={240}
      empty="No pm_edge purchase yet."
    />
  );
}

// ─── alerts ──────────────────────────────────────────────────────────────────

const ALERT_TONE: Record<AlertLevel, PillTone> = { info: "cyan", warn: "yellow", critical: "red" };

const ALERT_COLUMNS: Column<Alert>[] = [
  { key: "ts", header: "ET", width: 46, render: (a) => <span className="num text-fg-muted">{etShortTime(a.ts)}</span>, title: (a) => a.ts },
  { key: "level", header: "Lvl", width: 50, render: (a) => <Pill tone={ALERT_TONE[a.level] ?? "grey"}>{a.level === "critical" ? "CRIT" : a.level}</Pill> },
  { key: "msg", header: "Message", render: (a) => a.message, title: (a) => `${a.kind}: ${a.message}` },
];

export function AlertsLog({ alerts }: { alerts: Alert[] }) {
  return (
    <DataTable columns={ALERT_COLUMNS} rows={alerts} rowKey={(a, index) => `${a.id}-${index}`} maxHeight={150} empty="No alerts." />
  );
}

// ─── savings ─────────────────────────────────────────────────────────────────

export function SavingsMeter({ savings }: { savings: SavingsState | undefined }) {
  if (!savings || (savings.subscriptions ?? []).length === 0) {
    return <MutedLine className="px-0">Add the subscriptions you would otherwise pay for on F2 FUND.</MutedLine>;
  }
  const spentUsd = ausdNumber(savings.mtd_spend_atomic);
  const subscriptionsUsd = toNumberSafe(savings.prorated_subscriptions_usd) ?? 0;
  const saved = toNumberSafe(savings.savings_usd) ?? subscriptionsUsd - spentUsd;
  return (
    <div className="flex flex-col">
      <KeyValue label="Saved this month" value={fmtUsd(saved)} tone={saved >= 0 ? "up" : "down"} />
      <KeyValue label="Research spend MTD" value={fmtUsd(spentUsd)} />
      <KeyValue label="Prorated subscriptions" value={fmtUsd(subscriptionsUsd)} />
      <div className="py-1">
        <Meter value={spentUsd} max={subscriptionsUsd} title="Spend vs the subscriptions it replaces" />
      </div>
      <div
        className="truncate text-[11px] text-fg-muted"
        title={savings.subscriptions.map((sub) => `${sub.name} ${fmtUsd(sub.monthly_usd, 0)}/mo`).join("\n")}
      >
        {savings.subscriptions.map((sub) => `${sub.name} ${fmtUsd(sub.monthly_usd, 0)}/mo`).join(" · ")}
      </div>
    </div>
  );
}
