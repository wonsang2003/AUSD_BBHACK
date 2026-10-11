/**
 * BudgetHeader: the top strip of the dashboard — how much budget is left and who pays for what.
 *
 * Data: `State` (`GET /api/state`, refreshed by the `state` SSE event, at most every 5 s):
 *   remaining allowance · today's spend vs daily cap · month-to-date vs monthly cap · runway ·
 *   worst-case exposure · funding mode chip (PERMIT / VOUCHER) · relayer gas spent today
 *   ("who pays gas": the relayer, never the user or the agents) · session controls.
 */

import { Badge } from "@/components/ui/Badge";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { Stat } from "@/components/ui/Stat";
import { formatAusd, formatEth } from "@/lib/format";
import type { StreamStatus } from "@/lib/sse";
import type { State } from "@/lib/types";

import { SessionControls } from "./SessionControls";

interface BudgetHeaderProps {
  state: State;
  streamStatus: StreamStatus;
  explorerTxUrl?: string;
}

const STREAM_TONE = { open: "up", connecting: "warn", reconnecting: "down" } as const;

/** runway in days with one decimal, "∞" when nothing is being spent. */
function formatRunway(days: number | null): string {
  if (days === null || !Number.isFinite(days)) {
    return "∞";
  }
  return `${days.toFixed(1)} d`;
}

export function BudgetHeader({ state, streamStatus, explorerTxUrl }: BudgetHeaderProps) {
  const { budget, monthly, relayer, session } = state;
  const mode = state.mode ?? (budget.exists ? "permit" : null);
  return (
    <section className="rounded-sm border border-line bg-surface px-3 py-2">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-fg-muted">Budget</span>
        {mode && (
          <Badge tone="info" title={mode === "voucher" ? "7 signed daily vouchers" : "one revocable ERC-2612 permit"}>
            {mode === "voucher" ? "VOUCHER" : "PERMIT"}
          </Badge>
        )}
        {!budget.exists && <Badge tone="warn">no budget — open one on /fund</Badge>}
        {budget.paused && <Badge tone="down">budget paused</Badge>}
        {budget.revoked && <Badge tone="down">revoked</Badge>}
        <Badge tone={session.live ? "up" : "neutral"} dot>
          {session.live ? "session live" : "session idle"}
        </Badge>
        <Badge tone={STREAM_TONE[streamStatus]} dot title="Server-Sent Events stream">
          {streamStatus === "open" ? "live feed" : streamStatus}
        </Badge>
        <div className="ml-auto">
          <SessionControls live={session.live} explorerTxUrl={explorerTxUrl} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-2 md:grid-cols-4 xl:grid-cols-7">
        <Stat
          label="Remaining allowance"
          value={formatAusd(budget.allowance_remaining)}
          sub={`wallet ${formatAusd(budget.wallet_balance)} AUSD`}
          hint="What the BudgetController may still pull from your wallet (permit allowance)."
        />
        <div className="flex flex-col gap-1">
          <Stat label="Today" value={formatAusd(budget.spent_today)} sub={`of ${formatAusd(budget.daily_cap)} daily cap`} />
          <ProgressBar value={budget.spent_today} max={budget.daily_cap} />
        </div>
        <div className="flex flex-col gap-1">
          <Stat label="Month to date" value={formatAusd(monthly.spent_mtd)} sub={`of ${formatAusd(monthly.cap)} monthly cap`} />
          <ProgressBar value={monthly.spent_mtd} max={monthly.cap} />
        </div>
        <Stat
          label="Runway"
          value={formatRunway(state.runway_days)}
          sub="at current burn"
          hint="remaining allowance ÷ average daily spend"
        />
        <Stat
          label="Worst-case exposure"
          value={formatAusd(state.worst_case_exposure)}
          tone="warn"
          sub="AUSD at risk today"
          hint="Max that could leave your wallet today if everything went wrong: capped by the contract."
        />
        <Stat label="Remaining today" value={formatAusd(budget.remaining_today)} tone="up" sub="enforced on-chain" />
        <Stat
          label="Who pays gas"
          value={relayer ? formatEth(relayer.eth_spent_today_wei) : "relayer"}
          sub={relayer ? `relayer · ${relayer.tx_count_today} tx today` : "relayer · stats n/a"}
          hint="Every transaction is submitted by the PocketDesk relayer. You only sign; agents hold 0 ETH."
        />
      </div>
    </section>
  );
}
