/**
 * Badge: a small tinted label ("demo vendor", "✓ compliance check", "bought", "TESTNET", …).
 *
 * `tone` picks the color (see `lib/ui.ts`); `dot` adds a status dot in front of the text.
 */

import type { ReactNode } from "react";

import { cx, TONE_CHIP, TONE_FILL, type Tone } from "@/lib/ui";

interface BadgeProps {
  tone?: Tone;
  dot?: boolean;
  title?: string;
  className?: string;
  children: ReactNode;
}

export function Badge({ tone = "neutral", dot = false, title, className, children }: BadgeProps) {
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-sm border px-1.5 py-px text-[10px] font-semibold uppercase tracking-wide",
        TONE_CHIP[tone],
        className,
      )}
    >
      {dot && <span className={cx("h-1.5 w-1.5 rounded-full", TONE_FILL[tone])} aria-hidden />}
      {children}
    </span>
  );
}
