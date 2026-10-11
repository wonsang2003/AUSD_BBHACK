/**
 * AgentRow: one sector agent on /agents — identity, the demo vendors it buys from, its cap
 * (editable), today's spend, and a pause / resume switch.
 *
 * Data: `GET /api/config` (address, sector, sellers) + `State.agents` (spend, paused).
 * Pause / resume: `POST /api/agents/{name}/pause|resume` — an off-chain switch; the agent
 * stops buying immediately (its on-chain cap stays as signed).
 */

"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { errorText } from "@/components/dashboard/QueryState";
import { QUERY_KEYS } from "@/components/dashboard/useDashboardData";
import { TextInput } from "@/components/fund/Field";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { pauseAgent, resumeAgent } from "@/lib/api";
import { addressUrl, formatAusd, shortAddress } from "@/lib/format";
import type { AgentConfig, AgentState } from "@/lib/types";

interface AgentRowProps {
  agent: AgentConfig;
  live: AgentState | undefined;
  capText: string;
  onCapChange: (text: string) => void;
}

export function AgentRow({ agent, live, capText, onCapChange }: AgentRowProps) {
  const client = useQueryClient();
  const paused = live?.paused ?? false;
  const toggle = useMutation({
    mutationFn: () => (paused ? resumeAgent(agent.name) : pauseAgent(agent.name)),
    onSuccess: () => void client.invalidateQueries({ queryKey: QUERY_KEYS.state }),
  });

  return (
    <div className="grid grid-cols-1 items-start gap-3 border-b border-line/60 px-3 py-2 md:grid-cols-[200px_1fr_140px_200px_auto]">
      <div className="flex flex-col">
        <span className="flex items-center gap-1.5 text-xs font-semibold">
          {agent.name}
          {paused ? <Badge tone="warn">paused</Badge> : <Badge tone="up">active</Badge>}
        </span>
        <a
          className="num text-[11px] text-info hover:underline"
          href={addressUrl(agent.address)}
          target="_blank"
          rel="noopener noreferrer"
        >
          {shortAddress(agent.address)} ↗
        </a>
        <span className="text-[10px] text-fg-dim">sector {agent.sector}</span>
      </div>
      <div className="flex flex-wrap gap-1">
        {agent.sellers.map((seller) => (
          <Badge key={seller} className="normal-case">
            {seller} · demo vendor
          </Badge>
        ))}
      </div>
      <TextInput value={capText} onChange={(event) => onCapChange(event.target.value)} suffix="AUSD/day" aria-label={`${agent.name} daily cap`} />
      <ProgressBar
        value={live?.spent_today ?? 0}
        max={live?.agent_cap ?? 0}
        label={`today ${formatAusd(live?.spent_today)} / ${formatAusd(live?.agent_cap)} on-chain cap`}
      />
      <div className="flex flex-col items-end gap-1">
        <Button size="sm" variant={paused ? "primary" : "danger"} busy={toggle.isPending} onClick={() => toggle.mutate()}>
          {paused ? "Resume" : "Pause"}
        </Button>
        {toggle.isError && <span className="text-[10px] text-down">{errorText(toggle.error)}</span>}
      </div>
    </div>
  );
}
