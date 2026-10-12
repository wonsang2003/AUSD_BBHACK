/**
 * Pill: a small rectangular status tag (Bloomberg style), uppercase bold 10.5px.
 *
 * Props:
 *   tone     "green" | "red" | "amber" | "yellow" | "grey" | "cyan" | "white"   (default "grey")
 *   outline  true → colored border + colored text on black (e.g. "✓ COMPLIANCE");
 *            false (default) → solid colored fill with black text (e.g. BOUGHT, FAILED, PAUSED)
 *   title    tooltip text
 *
 * Example: <Pill tone="green">BOUGHT</Pill>  <Pill tone="green" outline title="…">✓ COMPLIANCE</Pill>
 */

import type { ReactNode } from "react";

export type PillTone = "green" | "red" | "amber" | "yellow" | "grey" | "cyan" | "white";

interface PillProps {
  tone?: PillTone;
  outline?: boolean;
  title?: string;
  children: ReactNode;
}

interface PillColors {
  solid: { background: string; color: string };
  line: string;
}

const PILL_COLORS: Record<PillTone, PillColors> = {
  green: { solid: { background: "#3ddc84", color: "#000" }, line: "#3ddc84" },
  red: { solid: { background: "#ff4d4d", color: "#000" }, line: "#ff4d4d" },
  amber: { solid: { background: "#ffa028", color: "#000" }, line: "#ffa028" },
  yellow: { solid: { background: "#ffcc00", color: "#000" }, line: "#ffcc00" },
  grey: { solid: { background: "#3a3a3a", color: "#e6e6e6" }, line: "#9a9a9a" },
  cyan: { solid: { background: "#4fc3f7", color: "#000" }, line: "#4fc3f7" },
  white: { solid: { background: "#e6e6e6", color: "#000" }, line: "#e6e6e6" },
};

export function Pill({ tone = "grey", outline = false, title, children }: PillProps) {
  const colors = PILL_COLORS[tone];
  const style = outline
    ? { borderColor: colors.line, color: colors.line, background: "#000" }
    : { ...colors.solid, borderColor: colors.solid.background };
  return (
    <span className="t-pill" style={style} title={title}>
      {children}
    </span>
  );
}
