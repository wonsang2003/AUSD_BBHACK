/**
 * AgentsPage: `/agents` — configure the four sector agents.
 *
 *   Desk settings: watchlist, session hours, stake per asset
 *   Agents: per-agent daily cap (editable), sellers, today's spend vs on-chain cap, pause / resume
 *
 * Data: `GET /api/config` (agents), `GET /api/settings` (saved values), `GET /api/state`
 * (live spend + paused flags). Save → `POST /api/settings`. Note: the on-chain per-agent caps
 * are the ones in the signed BudgetConfig; new caps saved here reach the chain when the budget
 * is re-signed on /fund (the off-chain allocator uses them right away).
 */

"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { errorText, QueryState } from "@/components/dashboard/QueryState";
import { QUERY_KEYS } from "@/components/dashboard/useDashboardData";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { getConfig, getSettings, getState, saveSettings } from "@/lib/api";

import { AgentRow } from "./AgentRow";
import { toSettings, valuesFromSettings, type AgentSettingsValues } from "./agentSettings";
import { DeskSettingsForm } from "./DeskSettingsForm";

const SETTINGS_KEY = ["settings"];

export function AgentsPage() {
  const client = useQueryClient();
  const config = useQuery({ queryKey: QUERY_KEYS.config, queryFn: getConfig, staleTime: Infinity });
  const settings = useQuery({ queryKey: SETTINGS_KEY, queryFn: getSettings });
  const state = useQuery({ queryKey: QUERY_KEYS.state, queryFn: getState, refetchInterval: 10_000 });

  const [values, setValues] = useState<AgentSettingsValues | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const prefilled = useRef(false);
  useEffect(() => {
    if (!prefilled.current && settings.data && config.data) {
      prefilled.current = true;
      setValues(valuesFromSettings(settings.data, config.data.agents.map((agent) => agent.name)));
    }
  }, [settings.data, config.data]);

  const save = useMutation({
    mutationFn: saveSettings,
    onSuccess: (saved) => client.setQueryData(SETTINGS_KEY, saved),
  });

  function submit(): void {
    if (!values || !settings.data) {
      return;
    }
    const next = toSettings(values, settings.data);
    if (Array.isArray(next)) {
      setProblems(next);
      return;
    }
    setProblems([]);
    save.mutate(next);
  }

  const liveByName = new Map((state.data?.agents ?? []).map((agent) => [agent.name, agent]));
  const saveBar = (
    <div className="flex items-center gap-2">
      {save.isSuccess && <span className="text-[11px] text-up">saved</span>}
      {save.isError && <span className="text-[11px] text-down">{errorText(save.error)}</span>}
      <Button size="sm" variant="primary" busy={save.isPending} disabled={!values} onClick={submit}>
        Save settings
      </Button>
    </div>
  );

  return (
    <div className="flex flex-col gap-3">
      <Card title="Desk settings" subtitle="read by every agent" actions={saveBar}>
        <QueryState query={settings}>
          {() => (values ? <DeskSettingsForm values={values} onChange={setValues} /> : <span className="text-xs text-fg-dim">Loading…</span>)}
        </QueryState>
      </Card>
      <Card title="Agents" subtitle="per-agent daily cap · sellers · pause / resume" actions={saveBar} flush>
        <QueryState query={config} isEmpty={(cfg) => cfg.agents.length === 0} emptyText="No agents configured.">
          {(cfg) => (
            <div>
              {cfg.agents.map((agent) => (
                <AgentRow
                  key={agent.name}
                  agent={agent}
                  live={liveByName.get(agent.name)}
                  capText={values?.agentCapsAusd[agent.name] ?? ""}
                  onCapChange={(text) =>
                    values && setValues({ ...values, agentCapsAusd: { ...values.agentCapsAusd, [agent.name]: text } })
                  }
                />
              ))}
              {problems.length > 0 && (
                <ul className="px-3 py-2 text-[11px] text-down">
                  {problems.map((problem) => (
                    <li key={problem}>{problem}</li>
                  ))}
                </ul>
              )}
              <p className="px-3 py-2 text-[11px] text-fg-dim">
                Caps saved here steer the allocator now; the on-chain per-agent caps change when you re-sign the budget on /fund.
              </p>
            </div>
          )}
        </QueryState>
      </Card>
    </div>
  );
}
