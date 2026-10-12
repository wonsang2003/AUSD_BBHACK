/**
 * AllocationPanel (ALOC): how the keeper split today's budget across the sector agents.
 *
 * Data: `GET /api/allocations` / the `allocation` SSE event. One fixed-height row per agent:
 * name · thin weight bar · amount + % (right-aligned) · ONE short reason tag. All reasons are in
 * the row tooltip, so nothing wraps or overlaps.
 */

import { Meter } from "@/components/terminal/Meter";
import { MutedLine } from "@/components/terminal/InlineError";
import { fmtAusd, fmtPct } from "@/components/terminal/format";
import type { Allocations } from "@/lib/types";

const REASON_TAG_CHARS = 22;

function shortReason(reason: string | undefined): string {
  if (!reason) {
    return "";
  }
  return reason.length > REASON_TAG_CHARS ? `${reason.slice(0, REASON_TAG_CHARS - 1)}…` : reason;
}

export function AllocationPanel({ allocations }: { allocations: Allocations }) {
  const rows = allocations.allocations ?? [];
  if (rows.length === 0) {
    return <MutedLine>No allocation yet — start a session.</MutedLine>;
  }
  return (
    <div className="flex flex-col">
      <div className="num flex justify-between border-b border-line pb-1 text-[11px] text-fg-muted">
        <span>{allocations.date}</span>
        <span>BUDGET {fmtAusd(allocations.budget_atomic)} AUSD</span>
      </div>
      {rows.map((row, index) => {
        const reasons = row.reasons ?? [];
        const tooltip = reasons.length > 0 ? `${row.agent}:\n• ${reasons.join("\n• ")}` : row.agent;
        return (
          <div
            key={`${row.agent}-${index}`}
            title={tooltip}
            className="grid h-[24px] grid-cols-[88px_minmax(40px,1fr)_118px_minmax(0,140px)] items-center gap-2 border-b border-[#1c1c1c]"
          >
            <span className="truncate font-bold">{row.agent}</span>
            <Meter value={row.weight} max={1} tone="info" height={4} />
            <span className="num truncate text-right">
              {fmtAusd(row.amount_atomic)} <span className="text-fg-muted">{fmtPct(row.weight, 0).padStart(4, " ")}</span>
            </span>
            {reasons.length > 0 ? (
              <span className="truncate border border-line px-1 text-[10.5px] uppercase leading-[14px] text-fg-muted">
                {shortReason(reasons[0])}
                {reasons.length > 1 ? ` +${reasons.length - 1}` : ""}
              </span>
            ) : (
              <span />
            )}
          </div>
        );
      })}
    </div>
  );
}
