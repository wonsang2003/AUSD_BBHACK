/**
 * BreakerBanner: the full-width red "TOKEN PAUSED — agents halted" banner.
 *
 * Data: `GET /api/breaker` / the `breaker` SSE event (SPEC §9 C). The global breaker trips when
 * a pre-flight `eth_call` of a payment reverts because the AUSD token is paused; every agent
 * stops buying until the backend's 60 s re-probe sees the token unpaused again. Renders nothing
 * while the breaker is closed.
 */

import type { BreakerStatus } from "@/lib/types";
import { relativeTime } from "@/lib/format";

interface BreakerBannerProps {
  breaker: BreakerStatus | undefined;
}

export function BreakerBanner({ breaker }: BreakerBannerProps) {
  if (!breaker?.global_open) {
    return null;
  }
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 rounded-sm border border-down bg-down/15 px-3 py-2 text-down">
      <span className="text-sm font-bold tracking-wide">TOKEN PAUSED — agents halted</span>
      <span className="text-xs text-down/80">{breaker.global_reason}</span>
      <span className="num ml-auto text-[11px] text-down/70">
        tripped {relativeTime(breaker.tripped_at)} · re-probing every 60 s, auto-resume when unpaused
      </span>
    </div>
  );
}
