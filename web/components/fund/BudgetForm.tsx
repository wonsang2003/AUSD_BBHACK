/**
 * BudgetForm: step 2 on /fund — the budget the agents must stay inside.
 *
 * Fields: total budget, plan days, daily cap, monthly cap, session hours, watchlist, per-agent
 * caps, stake per asset (sizes the value of information), the subscriptions this replaces (for
 * the savings meter), and the funding mode. The parent saves them to `POST /api/settings` and
 * runs the signing flow (`flows.ts`); this component only edits values.
 */

"use client";

import { Button } from "@/components/ui/Button";

import type { BudgetFormValues, FundingMode } from "./budgetFormModel";
import { Field, TextInput } from "./Field";
import { ModeToggle } from "./ModeToggle";

interface BudgetFormProps {
  values: BudgetFormValues;
  onChange: (values: BudgetFormValues) => void;
  mode: FundingMode;
  onModeChange: (mode: FundingMode) => void;
  onSubmit: () => void;
  busy: boolean;
  disabled: boolean;
  problems: string[];
}

export function BudgetForm({ values, onChange, mode, onModeChange, onSubmit, busy, disabled, problems }: BudgetFormProps) {
  const set = <K extends keyof BudgetFormValues>(key: K, value: BudgetFormValues[K]) => onChange({ ...values, [key]: value });
  const setRecord = (key: "agentCapsAusd" | "stakesUsd", name: string, text: string) =>
    set(key, { ...values[key], [name]: text });

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <Field label="Total budget">
          <TextInput value={values.totalAusd} onChange={(e) => set("totalAusd", e.target.value)} suffix="AUSD" />
        </Field>
        <Field label="Plan">
          <TextInput value={values.planDays} onChange={(e) => set("planDays", e.target.value)} suffix="days" />
        </Field>
        <Field label="Daily cap" hint="enforced on-chain">
          <TextInput value={values.dailyCapAusd} onChange={(e) => set("dailyCapAusd", e.target.value)} suffix="AUSD" />
        </Field>
        <Field label="Monthly cap">
          <TextInput value={values.monthlyCapAusd} onChange={(e) => set("monthlyCapAusd", e.target.value)} suffix="AUSD" />
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
        <Field label="Watchlist" className="col-span-2 md:col-span-4" hint="space or comma separated">
          <TextInput value={values.watchlist} onChange={(e) => set("watchlist", e.target.value)} />
        </Field>
      </div>

      <RecordInputs
        title="Per-agent daily cap"
        record={values.agentCapsAusd}
        suffix="AUSD"
        onChange={(name, text) => setRecord("agentCapsAusd", name, text)}
      />
      <RecordInputs
        title="Stake per asset (position size the research protects)"
        record={values.stakesUsd}
        suffix="USD"
        onChange={(name, text) => setRecord("stakesUsd", name, text)}
      />
      <SubscriptionInputs values={values} onChange={onChange} />

      <ModeToggle mode={mode} onChange={onModeChange} />
      {problems.length > 0 && (
        <ul className="text-[11px] text-down">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      <Button type="submit" variant="primary" busy={busy} disabled={disabled} className="self-start">
        {mode === "permit" ? "Sign permit + config & open budget" : "Sign 7 daily vouchers + config"}
      </Button>
    </form>
  );
}

interface RecordInputsProps {
  title: string;
  record: Record<string, string>;
  suffix: string;
  onChange: (name: string, text: string) => void;
}

function RecordInputs({ title, record, suffix, onChange }: RecordInputsProps) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-dim">{title}</span>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        {Object.entries(record).map(([name, text]) => (
          <Field key={name} label={<span className="normal-case text-fg-muted">{name}</span>}>
            <TextInput value={text} onChange={(e) => onChange(name, e.target.value)} suffix={suffix} />
          </Field>
        ))}
      </div>
    </div>
  );
}

function SubscriptionInputs({ values, onChange }: { values: BudgetFormValues; onChange: (values: BudgetFormValues) => void }) {
  const subscriptions = values.subscriptions;
  const update = (index: number, name: string, monthlyUsd: number) =>
    onChange({
      ...values,
      subscriptions: subscriptions.map((sub, i) => (i === index ? { name, monthly_usd: monthlyUsd } : sub)),
    });
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-dim">
        Subscriptions this replaces (savings meter)
      </span>
      {subscriptions.map((sub, index) => (
        <div key={index} className="grid grid-cols-[1fr_120px_auto] gap-2">
          <TextInput value={sub.name} placeholder="name" onChange={(e) => update(index, e.target.value, sub.monthly_usd)} />
          <TextInput
            value={String(sub.monthly_usd)}
            suffix="USD/mo"
            onChange={(e) => update(index, sub.name, Number(e.target.value) || 0)}
          />
          <Button
            size="sm"
            variant="ghost"
            onClick={() => onChange({ ...values, subscriptions: subscriptions.filter((_, i) => i !== index) })}
          >
            remove
          </Button>
        </div>
      ))}
      <Button
        size="sm"
        variant="ghost"
        className="self-start"
        onClick={() => onChange({ ...values, subscriptions: [...subscriptions, { name: "", monthly_usd: 0 }] })}
      >
        + add subscription
      </Button>
    </div>
  );
}
