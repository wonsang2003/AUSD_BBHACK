/**
 * Stat and KeyValue: how the terminal shows a single figure.
 *
 *   <Stat label="Remaining allowance" value="48.50" unit="AUSD" tone="up" sub="wallet 52.00" hint="…" />
 *     amber uppercase label, a large (18px) tabular value, optional unit and a muted sub-line.
 *     `size="lg"` makes the value 20px. `children` render under the value (e.g. a Meter).
 *
 *   <KeyValue label="Custody" value="YOUR WALLET" tone="up" hint="…" />
 *     one dense row: muted label left, value right-aligned (for key/value lists).
 */

import type { ReactNode } from "react";

import { cls, TONE_TEXT, type TermTone } from "./tones";

interface StatProps {
  label: string;
  value: ReactNode;
  unit?: string;
  tone?: TermTone;
  sub?: ReactNode;
  hint?: string;
  size?: "md" | "lg";
  children?: ReactNode;
}

export function Stat({ label, value, unit, tone = "neutral", sub, hint, size = "md", children }: StatProps) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5" title={hint}>
      <span className="t-label truncate">{label}</span>
      <span className={cls("num truncate font-bold leading-tight", size === "lg" ? "text-[20px]" : "text-[18px]", TONE_TEXT[tone])}>
        {value}
        {unit ? <span className="ml-1 text-[11px] font-normal text-fg-muted">{unit}</span> : null}
      </span>
      {children}
      {sub ? <span className="truncate text-[11px] text-fg-muted">{sub}</span> : null}
    </div>
  );
}

interface KeyValueProps {
  label: string;
  value: ReactNode;
  tone?: TermTone;
  hint?: string;
}

export function KeyValue({ label, value, tone = "neutral", hint }: KeyValueProps) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-[#1c1c1c] py-[3px]" title={hint}>
      <span className="shrink-0 text-[12px] text-fg-muted">{label}</span>
      <span className={cls("num min-w-0 truncate text-right font-bold", TONE_TEXT[tone])}>{value}</span>
    </div>
  );
}
