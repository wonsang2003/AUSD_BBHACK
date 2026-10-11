/**
 * ProgressBar: a thin usage meter (spent vs daily cap, agent spend vs agent cap, …).
 *
 * Color rule unless `tone` is given: used/max below 75 % → up (green), below 90 % → warn
 * (amber), otherwise → down (red). Values are clamped to 0–100 % for drawing only; the label
 * (if any) is whatever the caller passes, so the real numbers are never hidden.
 */

import type { ReactNode } from "react";

import { cx, TONE_FILL, type Tone } from "@/lib/ui";

export const WARN_RATIO = 0.75;
export const DANGER_RATIO = 0.9;

interface ProgressBarProps {
  value: number;
  max: number;
  tone?: Tone;
  label?: ReactNode;
  className?: string;
}

/** Fraction used, clamped to [0, 1]; a non-positive max counts as empty. */
export function usedRatio(value: number, max: number): number {
  if (max <= 0 || !Number.isFinite(value) || !Number.isFinite(max)) {
    return 0;
  }
  return Math.min(1, Math.max(0, value / max));
}

/** Tone for a usage ratio: green below 75 %, amber below 90 %, red from 90 %. */
export function toneForRatio(ratio: number): Tone {
  if (ratio < WARN_RATIO) {
    return "up";
  }
  if (ratio < DANGER_RATIO) {
    return "warn";
  }
  return "down";
}

export function ProgressBar({ value, max, tone, label, className }: ProgressBarProps) {
  const ratio = usedRatio(value, max);
  const fillTone = tone ?? toneForRatio(ratio);
  return (
    <div className={cx("flex min-w-0 flex-col gap-1", className)}>
      {label !== undefined && <div className="num text-[11px] text-fg-muted">{label}</div>}
      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value}
        className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2"
      >
        <div className={cx("h-full rounded-full", TONE_FILL[fillTone])} style={{ width: `${ratio * 100}%` }} />
      </div>
    </div>
  );
}
