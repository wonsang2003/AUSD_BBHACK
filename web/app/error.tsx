/**
 * Route error screen: if a page ever throws while rendering, show the reason in one terminal
 * panel with a RETRY button instead of a blank page. The chrome (top bar, tabs) stays usable.
 */

"use client";

import { InlineError } from "@/components/terminal/InlineError";
import { Panel } from "@/components/terminal/Panel";
import { TermButton } from "@/components/terminal/TermButton";

interface RouteErrorProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function RouteError({ error, reset }: RouteErrorProps) {
  return (
    <Panel title="Page error" actions={<TermButton size="sm" onClick={reset}>Retry</TermButton>}>
      <InlineError error={error} prefix="RENDER" />
      <p className="px-2 text-[12px] text-fg-muted">The rest of the terminal still works: use F1–F4 above.</p>
    </Panel>
  );
}
