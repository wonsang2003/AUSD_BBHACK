/**
 * BreakerBanner: full-width red banner shown only while the global circuit breaker is open
 * (the AUSD token is paused → every agent is halted). Data: `GET /api/breaker` / `breaker` SSE.
 */

import { etTime } from "@/components/terminal/marketTime";
import type { BreakerStatus } from "@/lib/types";

export function BreakerBanner({ breaker }: { breaker: BreakerStatus | undefined }) {
  if (!breaker?.global_open) {
    return null;
  }
  return (
    <div role="alert" className="col-span-12 flex items-center gap-3 border border-down bg-down px-3 py-1.5 font-bold text-black">
      <span className="t-blink">■</span>
      <span className="text-[14px] tracking-wide">TOKEN PAUSED — AGENTS HALTED</span>
      <span className="min-w-0 flex-1 truncate text-[12px] font-normal" title={breaker.global_reason}>
        {breaker.global_reason}
      </span>
      {breaker.tripped_at ? <span className="num text-[12px]">SINCE {etTime(breaker.tripped_at)} ET</span> : null}
    </div>
  );
}
