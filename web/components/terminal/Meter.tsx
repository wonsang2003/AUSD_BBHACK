/**
 * Meter: a thin horizontal bar for "used vs limit" (spend vs cap, allocation share, confidence).
 *
 * Props:
 *   value, max   the ratio value / max is clamped to 0–100 %; max ≤ 0 renders an empty bar
 *   tone         a fixed TermTone, or "auto" (default): green below 70 %, yellow below 90 %,
 *                red from 90 % (for spend against a cap)
 *   height       px (default 4)
 *   title        tooltip
 */

import { TONE_FILL, type TermTone } from "./tones";

interface MeterProps {
  value: number;
  max: number;
  tone?: TermTone | "auto";
  height?: number;
  title?: string;
}

const WARN_RATIO = 0.7;
const DANGER_RATIO = 0.9;

/** Fraction filled, clamped to [0, 1]; 0 when max is not positive. */
export function meterRatio(value: number, max: number): number {
  if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0) {
    return 0;
  }
  return Math.min(1, Math.max(0, value / max));
}

function autoTone(ratio: number): TermTone {
  if (ratio >= DANGER_RATIO) {
    return "down";
  }
  if (ratio >= WARN_RATIO) {
    return "warn";
  }
  return "up";
}

export function Meter({ value, max, tone = "auto", height = 4, title }: MeterProps) {
  const ratio = meterRatio(value, max);
  const fillTone = tone === "auto" ? autoTone(ratio) : tone;
  return (
    <div className="w-full bg-surface-3" style={{ height }} title={title}>
      <div className={TONE_FILL[fillTone]} style={{ width: `${ratio * 100}%`, height }} />
    </div>
  );
}
