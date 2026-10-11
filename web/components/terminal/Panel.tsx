/**
 * Panel: the terminal window every block of the UI sits in — a solid amber title bar with black
 * uppercase text, then a #0b0b0b body with a 1px #2a2a2a border and square corners.
 *
 * Props:
 *   title          bar text (rendered uppercase)
 *   code           optional short function code shown before the title, e.g. "BMON"
 *   actions        right side of the bar: small Pills / TermButtons (black-on-amber context)
 *   className      outer classes, typically the grid placement ("col-span-12 xl:col-span-5")
 *   bodyClassName  extra classes for the body (e.g. a max height)
 *   flush          true → no body padding (tables that run edge to edge)
 *
 * The panel fills its grid cell's height, so panels in one row line up.
 */

import type { ReactNode } from "react";

import { cls } from "./tones";

interface PanelProps {
  title: string;
  code?: string;
  actions?: ReactNode;
  className?: string;
  bodyClassName?: string;
  flush?: boolean;
  children: ReactNode;
}

export function Panel({ title, code, actions, className, bodyClassName, flush = false, children }: PanelProps) {
  return (
    <section className={cls("t-panel", className)}>
      <header className="t-panel__bar">
        {code ? <span className="text-[11px] font-bold opacity-70">{code}</span> : null}
        <h2 className="t-panel__title">{title}</h2>
        {actions ? <div className="t-panel__actions">{actions}</div> : null}
      </header>
      <div className={cls("t-panel__body", flush && "t-panel__body--flush", bodyClassName)}>{children}</div>
    </section>
  );
}
