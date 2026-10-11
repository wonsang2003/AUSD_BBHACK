/**
 * Card: the bordered panel every dashboard block sits in.
 *
 * Layout: an optional header row (small uppercase title, optional subtitle on the left,
 * optional actions such as buttons or badges on the right) above the body.
 * Pass `flush` for bodies that bring their own padding (tables).
 */

import type { ReactNode } from "react";

import { cx } from "@/lib/ui";

interface CardProps {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  /** No body padding (for tables that run edge to edge). */
  flush?: boolean;
  className?: string;
  children: ReactNode;
}

export function Card({ title, subtitle, actions, flush = false, className, children }: CardProps) {
  const hasHeader = title !== undefined || actions !== undefined;
  return (
    <section className={cx("min-w-0 rounded-sm border border-line bg-surface", className)}>
      {hasHeader && (
        <header className="flex items-center justify-between gap-2 border-b border-line px-3 py-1.5">
          <div className="min-w-0">
            <h2 className="truncate text-[11px] font-semibold uppercase tracking-wider text-fg-muted">{title}</h2>
            {subtitle !== undefined && <p className="truncate text-[11px] text-fg-dim">{subtitle}</p>}
          </div>
          {actions !== undefined && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={flush ? undefined : "p-3"}>{children}</div>
    </section>
  );
}
