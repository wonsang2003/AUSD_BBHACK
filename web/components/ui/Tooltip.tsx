/**
 * Tooltip: a hover / keyboard-focus explanation bubble, CSS only (no JS, works in server
 * components). Used for the "why" behind numbers: EVOI, runway formula, compliance check, …
 *
 * The trigger is focusable so keyboard users can open it too; `side` puts the bubble above
 * (default) or below the trigger.
 */

import type { ReactNode } from "react";

import { cx } from "@/lib/ui";

interface TooltipProps {
  content: ReactNode;
  side?: "top" | "bottom";
  className?: string;
  children: ReactNode;
}

const SIDE_CLASSES = {
  top: "bottom-full mb-1",
  bottom: "top-full mt-1",
};

export function Tooltip({ content, side = "top", className, children }: TooltipProps) {
  return (
    <span tabIndex={0} className={cx("group relative inline-flex cursor-help outline-none", className)}>
      {children}
      <span
        role="tooltip"
        className={cx(
          "pointer-events-none absolute left-0 z-30 hidden w-max max-w-xs rounded-sm border border-line bg-surface-2",
          "px-2 py-1 text-[11px] font-normal normal-case tracking-normal text-fg shadow-lg shadow-black/50",
          "group-hover:block group-focus:block",
          SIDE_CLASSES[side],
        )}
      >
        {content}
      </span>
    </span>
  );
}
