/**
 * Terminal color tones, shared by every `components/terminal/*` piece.
 *
 *   up     green  #3ddc84  positive change, bought, live, OK
 *   down   red    #ff4d4d  negative change, failed, paused token, ETH where there must be none
 *   warn   yellow #ffcc00  warning
 *   info   cyan   #4fc3f7  links, tx hashes
 *   amber  amber  #ffa028  labels, terminal accent
 *   muted  grey   #9a9a9a  secondary text
 *   neutral white #e6e6e6  plain values, unchanged numbers
 *
 * The class names are written out in full so Tailwind finds them when scanning sources.
 */

export type TermTone = "up" | "down" | "warn" | "info" | "amber" | "muted" | "neutral";

/** Text color per tone. */
export const TONE_TEXT: Record<TermTone, string> = {
  up: "text-up",
  down: "text-down",
  warn: "text-warn",
  info: "text-info",
  amber: "text-amber",
  muted: "text-fg-muted",
  neutral: "text-fg",
};

/** Solid fill per tone (meters, LEDs). */
export const TONE_FILL: Record<TermTone, string> = {
  up: "bg-up",
  down: "bg-down",
  warn: "bg-warn",
  info: "bg-info",
  amber: "bg-amber",
  muted: "bg-fg-muted",
  neutral: "bg-fg",
};

/** Rule: > 0 → up (green), < 0 → down (red), 0 / missing → neutral (white). */
export function signTone(value: number | null | undefined): TermTone {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) {
    return "neutral";
  }
  return value > 0 ? "up" : "down";
}

/** Join class names, skipping falsy ones. */
export function cls(...names: Array<string | false | null | undefined>): string {
  return names.filter(Boolean).join(" ");
}
