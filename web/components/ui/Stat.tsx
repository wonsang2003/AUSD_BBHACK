/**
 * Stat: one headline number (label above, big monospace value, optional sub-line below).
 *
 * Used for the dashboard's top strip: remaining today, runway, spent MTD, relayer gas, …
 * `tone` colors the value (e.g. "down" when a cap is hit); `hint` becomes a tooltip on the label.
 */

import type { ReactNode } from "react";

import { cx, TONE_TEXT, type Tone } from "@/lib/ui";

import { Tooltip } from "./Tooltip";

interface StatProps {
  label: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
  tone?: Tone;
  hint?: ReactNode;
  className?: string;
}

export function Stat({ label, value, sub, tone = "neutral", hint, className }: StatProps) {
  const labelNode = <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-dim">{label}</span>;
  return (
    <div className={cx("flex min-w-0 flex-col gap-0.5", className)}>
      {hint === undefined ? labelNode : <Tooltip content={hint}>{labelNode}</Tooltip>}
      <span className={cx("num truncate text-lg font-semibold leading-tight", TONE_TEXT[tone])}>{value}</span>
      {sub !== undefined && <span className="num truncate text-[11px] text-fg-muted">{sub}</span>}
    </div>
  );
}
