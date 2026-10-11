/**
 * AgentCards: one card per sector agent — AUSD wallet balance, ETH balance (always 0.0000),
 * today's spend vs its cap, and status (paused / backing off).
 *
 * Data: `State.agents` + the breaker's per-agent backoff (`GET /api/breaker`).
 * Zero-gas rule (SPEC §9): agents never hold ETH; they pay with ERC-3009 signatures and the
 * relayer submits every transaction. If any agent shows ETH > 0 a warning chip appears, because
 * that would break the "agents can only spend AUSD within caps" story.
 */

import { Badge } from "@/components/ui/Badge";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { Tooltip } from "@/components/ui/Tooltip";
import { formatAusd, formatFixedPoint, shortAddress } from "@/lib/format";
import type { AgentState, BreakerStatus } from "@/lib/types";

interface AgentCardsProps {
  agents: AgentState[];
  breaker: BreakerStatus | undefined;
}

const ETH_DECIMALS = 18;
const GAS_TOOLTIP = "Gas paid by relayer; agents pay with ERC-3009 signatures only";

/** Wei → "0.0000" (always four decimals so a zero balance reads as a deliberate zero). */
export function formatEth4(wei: string | number | undefined): string {
  return formatFixedPoint(wei ?? "0", { decimals: ETH_DECIMALS, minDecimals: 4, maxDecimals: 4 });
}

function holdsEth(agent: AgentState): boolean {
  return BigInt(agent.eth_balance_wei ?? "0") > BigInt(0);
}

export function AgentCards({ agents, breaker }: AgentCardsProps) {
  const anyEth = agents.some(holdsEth);
  return (
    <div className="flex flex-col gap-2">
      {anyEth && (
        <Badge tone="down" className="self-start">
          warning: an agent holds ETH — agents should be zero-gas
        </Badge>
      )}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {agents.map((agent) => (
          <AgentCard key={agent.name} agent={agent} backoffSeconds={breaker?.agents[agent.name]?.backoff_seconds ?? 0} />
        ))}
      </div>
    </div>
  );
}

function AgentCard({ agent, backoffSeconds }: { agent: AgentState; backoffSeconds: number }) {
  return (
    <div className="rounded-sm border border-line bg-surface-2/50 p-2">
      <div className="flex items-center gap-1.5">
        <span className="text-xs font-semibold">{agent.name}</span>
        <span className="num text-[10px] text-fg-dim">{shortAddress(agent.address)}</span>
        <span className="ml-auto flex gap-1">
          {agent.paused && <Badge tone="warn">paused</Badge>}
          {backoffSeconds > 0 && <Badge tone="warn">backoff {backoffSeconds}s</Badge>}
          {holdsEth(agent) && <Badge tone="down">holds ETH</Badge>}
        </span>
      </div>
      <div className="mt-1 grid grid-cols-2 gap-2">
        <div>
          <div className="text-[10px] uppercase text-fg-dim">AUSD</div>
          <div className="num text-sm font-semibold">{formatAusd(agent.balance)}</div>
        </div>
        <div>
          <Tooltip content={GAS_TOOLTIP}>
            <span className="text-[10px] uppercase text-fg-dim underline decoration-dotted">ETH</span>
          </Tooltip>
          <div className="num text-sm font-semibold text-fg-muted">{formatEth4(agent.eth_balance_wei)}</div>
        </div>
      </div>
      <ProgressBar
        className="mt-1.5"
        value={agent.spent_today}
        max={agent.agent_cap}
        label={`spent ${formatAusd(agent.spent_today)} / cap ${formatAusd(agent.agent_cap)} · alloc ${formatAusd(agent.allocation)}`}
      />
    </div>
  );
}
