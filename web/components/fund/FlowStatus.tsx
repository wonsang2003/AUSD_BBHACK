/**
 * FlowStatus: one line under a button showing the running step of a signing flow, or why it
 * failed. Data: a `FlowRunner` (`useFlowRunner.ts`).
 */

import type { FlowRunner } from "./useFlowRunner";

export function FlowStatus({ runner }: { runner: FlowRunner }) {
  if (runner.step) {
    return <div className="text-[11px] text-info">{runner.step}</div>;
  }
  if (runner.error) {
    return <div className="text-[11px] text-down">Failed: {runner.error}</div>;
  }
  return null;
}
