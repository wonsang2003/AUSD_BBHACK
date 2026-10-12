/**
 * AgentBlotter (AGNT): one dense row per sector agent.
 *
 * Data: `State.agents` + breaker status. Columns: AUSD wallet balance, ETH (must be 0.0000 — the
 * relayer pays gas; red if an agent ever holds ETH), today's allocation / spend / cap / remaining,
 * realized research value per AUSD, and a status pill:
 *   HALTED (token paused, breaker open) > PAUSED > BACKOFF (failed pre-flight) > ACTIVE (session live) > IDLE.
 */

import { DataTable, type Column } from "@/components/terminal/DataTable";
import { Pill } from "@/components/terminal/Pill";
import { fmtAusd, fmtEth, fmtNumber, isPositiveWei } from "@/components/terminal/format";
import type { AgentState, BreakerStatus } from "@/lib/types";

interface AgentBlotterProps {
  agents: AgentState[];
  breaker: BreakerStatus | undefined;
  live: boolean;
}

const ETH_HINT = "Gas paid by relayer; agents pay with ERC-3009 signatures only";

function statusPill(agent: AgentState, breaker: BreakerStatus | undefined, live: boolean) {
  if (breaker?.global_open) {
    return (
      <Pill tone="red" title={breaker.global_reason}>
        HALTED
      </Pill>
    );
  }
  if (agent.paused) {
    return <Pill tone="yellow">PAUSED</Pill>;
  }
  const backoff = agent.backoff ?? breaker?.agents?.[agent.name];
  if (backoff && backoff.backoff_seconds > 0) {
    return (
      <Pill tone="amber" title={`${backoff.last_reason} (${backoff.failures} failures, ${backoff.backoff_seconds}s)`}>
        BACKOFF
      </Pill>
    );
  }
  return live ? <Pill tone="green">ACTIVE</Pill> : <Pill tone="grey">IDLE</Pill>;
}

function columns(breaker: BreakerStatus | undefined, live: boolean): Column<AgentState>[] {
  const ausd = (pick: (agent: AgentState) => unknown) =>
    function renderAusd(agent: AgentState) {
      return <span className="num">{fmtAusd(pick(agent))}</span>;
    };
  return [
    { key: "name", header: "Agent", width: 110, render: (a) => <span className="font-bold">{a.name}</span>, title: (a) => a.address },
    { key: "sector", header: "Sector", width: 96, render: (a) => <span className="text-fg-muted">{a.sector}</span>, title: (a) => a.sector },
    { key: "ausd", header: "AUSD", width: 84, align: "right", render: ausd((a) => a.balance) },
    {
      key: "eth",
      header: "ETH",
      width: 72,
      align: "right",
      headerTitle: ETH_HINT,
      title: () => ETH_HINT,
      render: (a) => (
        <span className={isPositiveWei(a.eth_balance_wei) ? "num font-bold text-down" : "num text-fg-muted"}>
          {fmtEth(a.eth_balance_wei ?? 0, 4)}
        </span>
      ),
    },
    { key: "alloc", header: "Alloc", width: 80, align: "right", render: ausd((a) => a.allocation) },
    { key: "spent", header: "Spent", width: 80, align: "right", render: ausd((a) => a.spent_today) },
    { key: "cap", header: "Cap", width: 80, align: "right", render: ausd((a) => a.agent_cap) },
    { key: "rem", header: "Remaining", width: 88, align: "right", render: ausd((a) => a.agent_remaining_today) },
    {
      key: "vpd",
      header: "Value/$",
      width: 70,
      align: "right",
      headerTitle: "Realized research value per AUSD spent",
      render: (a) => <span className="num">{fmtNumber(a.realized_value_per_dollar, 2)}</span>,
    },
    { key: "status", header: "Status", width: 84, render: (a) => statusPill(a, breaker, live) },
  ];
}

export function AgentBlotter({ agents, breaker, live }: AgentBlotterProps) {
  return (
    <DataTable
      columns={columns(breaker, live)}
      rows={agents}
      rowKey={(agent, index) => `${agent.name}-${index}`}
      empty="No agents configured."
    />
  );
}
