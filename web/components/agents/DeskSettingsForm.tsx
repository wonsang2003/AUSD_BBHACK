/**
 * DeskSettingsForm: the desk-wide inputs every agent reads — watchlist, session hours and the
 * stake per asset (the position size a piece of research protects; it scales each purchase's
 * expected value of information, so a bigger stake justifies buying more).
 * Pure inputs: the parent (`AgentsPage`) owns the values and saves them.
 */

import { Field, TextInput } from "@/components/fund/Field";
import { parseWatchlist } from "@/components/fund/budgetForm";

import { stakeFor, type AgentSettingsValues } from "./agentSettings";

interface DeskSettingsFormProps {
  values: AgentSettingsValues;
  onChange: (values: AgentSettingsValues) => void;
}

export function DeskSettingsForm({ values, onChange }: DeskSettingsFormProps) {
  const set = <K extends keyof AgentSettingsValues>(key: K, value: AgentSettingsValues[K]) => onChange({ ...values, [key]: value });
  const watchlist = parseWatchlist(values.watchlist);
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <Field label="Watchlist" className="col-span-2" hint="space or comma separated">
          <TextInput value={values.watchlist} onChange={(e) => set("watchlist", e.target.value)} />
        </Field>
        <Field label="Session start">
          <TextInput type="time" value={values.sessionStart} onChange={(e) => set("sessionStart", e.target.value)} />
        </Field>
        <Field label="Session end">
          <TextInput type="time" value={values.sessionEnd} onChange={(e) => set("sessionEnd", e.target.value)} />
        </Field>
        <Field label="Time zone" className="col-span-2">
          <TextInput value={values.sessionTz} onChange={(e) => set("sessionTz", e.target.value)} />
        </Field>
      </div>
      <div className="flex flex-col gap-1">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-dim">Stake per asset</span>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
          {watchlist.map((asset) => (
            <Field key={asset} label={<span className="normal-case text-fg-muted">{asset}</span>}>
              <TextInput
                value={stakeFor(values, asset)}
                onChange={(e) => set("stakesUsd", { ...values.stakesUsd, [asset]: e.target.value })}
                suffix="USD"
              />
            </Field>
          ))}
        </div>
      </div>
    </div>
  );
}
