/**
 * AllocationBars: how the keeper split today's budget across the four sector agents, and why.
 *
 * Data: `GET /api/allocations` / the `allocation` SSE event (amount + weight + reason chips per
 * agent, e.g. "NVDA earnings today"), joined with each agent's realized value per dollar from
 * `State.agents` (the allocator's learning signal: research value gained per AUSD spent).
 */

import { Badge } from "@/components/ui/Badge";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { formatAusd, formatPercent } from "@/lib/format";
import type { AgentState, Allocations } from "@/lib/types";

interface AllocationBarsProps {
  allocations: Allocations;
  agents: AgentState[];
}

export function AllocationBars({ allocations, agents }: AllocationBarsProps) {
  const valueByAgent = new Map(agents.map((agent) => [agent.name, agent.realized_value_per_dollar]));
  return (
    <div className="flex flex-col gap-2">
      <div className="num text-[11px] text-fg-dim">
        {allocations.date} · budget {formatAusd(allocations.budget_atomic, true)}
      </div>
      {allocations.allocations.map((row) => (
        <div key={row.agent} className="grid grid-cols-[110px_1fr_auto] items-center gap-2">
          <span className="truncate text-xs font-semibold">{row.agent}</span>
          <div className="flex min-w-0 flex-col gap-1">
            <ProgressBar value={row.weight} max={1} tone="info" />
            <div className="flex flex-wrap gap-1">
              {row.reasons.map((reason) => (
                <Badge key={reason} className="normal-case">
                  {reason}
                </Badge>
              ))}
            </div>
          </div>
          <div className="num text-right text-[11px]">
            <div className="text-fg">
              {formatAusd(row.amount_atomic)} <span className="text-fg-dim">({formatPercent(row.weight, 0)})</span>
            </div>
            <div className="text-fg-muted" title="Realized research value per AUSD spent">
              {(valueByAgent.get(row.agent) ?? 0).toFixed(2)} value/$
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
