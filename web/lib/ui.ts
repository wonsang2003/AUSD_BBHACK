/**
 * Small shared helpers for the UI components in `components/ui/`.
 *
 *   * `cx()`  — joins class names, skipping falsy ones (for conditional Tailwind classes).
 *   * `Tone`  — the five semantic colors of the terminal theme (defined in `app/globals.css`):
 *               up = good / bought / live, down = bad / failed, warn = attention / testnet,
 *               info = neutral highlight, neutral = plain.
 */

export type Tone = "neutral" | "up" | "down" | "warn" | "info";

/** Join class names; false / null / undefined / "" are skipped. */
export function cx(...classNames: Array<string | false | null | undefined>): string {
  return classNames.filter(Boolean).join(" ");
}

/** Text color per tone. */
export const TONE_TEXT: Record<Tone, string> = {
  neutral: "text-fg",
  up: "text-up",
  down: "text-down",
  warn: "text-warn",
  info: "text-info",
};

/** Tinted background + border + text per tone (badges, chips, banners). */
export const TONE_CHIP: Record<Tone, string> = {
  neutral: "border-line bg-surface-2 text-fg-muted",
  up: "border-up/40 bg-up/10 text-up",
  down: "border-down/40 bg-down/10 text-down",
  warn: "border-warn/40 bg-warn/10 text-warn",
  info: "border-info/40 bg-info/10 text-info",
};

/** Solid fill per tone (progress bars, status dots). */
export const TONE_FILL: Record<Tone, string> = {
  neutral: "bg-fg-muted",
  up: "bg-up",
  down: "bg-down",
  warn: "bg-warn",
  info: "bg-info",
};
