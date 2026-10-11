/**
 * BudgetMonitor (BMON): the budget at a glance, as dense key/value rows.
 *
 * Data: `State` (GET /api/state + `state` SSE event).
 *   remaining allowance  budget.allowance_remaining (what the controller may still pull)
 *   today / daily cap    budget.spent_today vs budget.daily_cap, with a Meter (green → yellow → red)
 *   MTD / monthly cap    monthly.spent_mtd vs monthly.cap
 *   runway               runway_days at today's pace
 *   worst-case exposure  worst_case_exposure (the most that could still leave the wallet)
 *   relayer gas today    relayer.eth_spent_today_wei + tx count (agents never pay gas)
 */

import { KeyValue } from "@/components/terminal/Stat";
import { Meter } from "@/components/terminal/Meter";
import { MutedLine } from "@/components/terminal/InlineError";
import { ausdNumber, EMPTY, fmtAusd, fmtEth, toNumberSafe } from "@/components/terminal/format";
import type { State } from "@/lib/types";

export function budgetMode(state: State): "permit" | "voucher" | null {
  if (state.mode) {
    return state.mode;
  }
  if (state.budget.mode) {
    return state.budget.mode;
  }
  return state.budget.exists ? "permit" : null;
}

function runwayText(days: number | null | undefined): string {
  const value = toNumberSafe(days);
  return value === null ? EMPTY : `${value.toFixed(1)} D`;
}

export function BudgetMonitor({ state }: { state: State }) {
  const { budget, monthly, relayer } = state;
  return (
    <div className="flex flex-col">
      {!budget.exists ? <MutedLine className="px-0 pt-0">No budget open — fund it on F2 FUND or use DEMO FUND.</MutedLine> : null}
      <KeyValue
        label="Remaining allowance"
        value={`${fmtAusd(budget.allowance_remaining)} AUSD`}
        tone="up"
        hint="What the BudgetController may still pull from your wallet"
      />
      <KeyValue label="Today spend / daily cap" value={`${fmtAusd(budget.spent_today)} / ${fmtAusd(budget.daily_cap)}`} />
      <div className="py-1">
        <Meter
          value={ausdNumber(budget.spent_today)}
          max={ausdNumber(budget.daily_cap)}
          title="Today's spend against the on-chain daily cap"
        />
      </div>
      <KeyValue label="MTD / monthly cap" value={`${fmtAusd(monthly.spent_mtd)} / ${fmtAusd(monthly.cap)}`} />
      <div className="py-1">
        <Meter value={ausdNumber(monthly.spent_mtd)} max={ausdNumber(monthly.cap)} height={3} title="Month-to-date spend vs monthly cap" />
      </div>
      <KeyValue label="Runway" value={runwayText(state.runway_days)} hint="Days the budget lasts at today's pace" />
      <KeyValue
        label="Worst-case exposure"
        value={`${fmtAusd(state.worst_case_exposure)} AUSD`}
        tone="warn"
        hint="The most that could still leave your wallet under the current caps"
      />
      <KeyValue
        label="Relayer gas today"
        value={relayer ? `${fmtEth(relayer.eth_spent_today_wei, 6)} ETH · ${relayer.tx_count_today ?? 0} TX` : EMPTY}
        tone="info"
        hint="The relayer pays all gas; agents pay with ERC-3009 signatures only"
      />
    </div>
  );
}
