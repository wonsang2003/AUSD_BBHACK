/**
 * Dashboard: F1 DESK, the live trading-terminal view at `/` (below the global TerminalChrome).
 *
 * Layout: a 12-column grid from 1280 px up (stacks below), panels with amber title bars:
 *   [breaker banner — only while the token is paused]
 *   Row 1  BUDGET MONITOR (4) | SESSION (4) | SAFETY (4)
 *   Row 2  AGENT BLOTTER (8) | ALLOCATION (4)
 *   Row 3  PURCHASE BLOTTER (8) | FINDINGS WIRE (4)
 *   Row 4  EVENT CALENDAR (3) | PRED MKT vs OPTIONS (3) | ALERTS + SAVINGS (3) | ASK THE DESK (3)
 *
 * Data: `useDashboardData()` — one GET per panel; live updates come from the single app-wide SSE
 * connection (`terminal/StreamProvider`). Each panel handles its own loading / error / empty
 * state through `QueryView`, so one failing endpoint never blanks the page.
 */

"use client";

import { Panel } from "@/components/terminal/Panel";
import { Pill } from "@/components/terminal/Pill";
import { QueryView } from "@/components/terminal/QueryView";

import { AgentBlotter } from "./AgentBlotter";
import { AllocationPanel } from "./AllocationPanel";
import { AskBox } from "./AskBox";
import { BreakerBanner } from "./BreakerBanner";
import { BudgetMonitor, budgetMode } from "./BudgetMonitor";
import { AlertsLog, EventCalendar, PmVsOptions, SavingsMeter } from "./DeskPanels";
import { FindingsWire } from "./FindingsWire";
import { PurchaseBlotter } from "./PurchaseBlotter";
import { SafetyCard } from "./SafetyCard";
import { SessionPanel } from "./SessionPanel";
import { useDashboardData } from "./useDashboardData";

const ROW_1 = "col-span-12 xl:col-span-4";
const WIDE = "col-span-12 xl:col-span-8";
const NARROW = "col-span-12 xl:col-span-4";
const QUARTER = "col-span-12 md:col-span-6 xl:col-span-3";

export function Dashboard() {
  const data = useDashboardData();
  const state = data.state.data;
  const breaker = data.breaker.data ?? state?.breaker;
  const mode = state ? budgetMode(state) : null;
  const purchaseCount = data.purchases.data?.length ?? 0;
  const findingCount = data.findings.data?.length ?? 0;

  return (
    <div className="grid grid-cols-12 gap-2">
      <BreakerBanner breaker={breaker} />

      {/* Row 1 */}
      <Panel
        code="BMON"
        title="Budget monitor"
        className={ROW_1}
        actions={
          state ? (
            <>
              {mode ? <Pill tone="white">{mode === "voucher" ? "VOUCHER MODE" : "PERMIT MODE"}</Pill> : <Pill>NO BUDGET</Pill>}
              {state.budget.paused ? <Pill tone="red">PAUSED</Pill> : null}
              {state.budget.revoked ? <Pill tone="red">REVOKED</Pill> : null}
            </>
          ) : null
        }
      >
        <QueryView query={data.state} label="STATE">
          {(snapshot) => <BudgetMonitor state={snapshot} />}
        </QueryView>
      </Panel>

      <Panel code="SESS" title="Session" className={ROW_1}>
        <QueryView query={data.state} label="STATE">
          {(snapshot) => <SessionPanel state={snapshot} demo={data.demo} />}
        </QueryView>
      </Panel>

      <Panel code="SAFE" title="Safety" className={ROW_1}>
        <QueryView query={data.state} label="STATE">
          {(snapshot) => <SafetyCard state={snapshot} vouchers={data.vouchers.data} breaker={breaker} />}
        </QueryView>
      </Panel>

      {/* Row 2 */}
      <Panel code="AGNT" title="Agent blotter" className={WIDE} flush>
        <QueryView query={data.state} label="AGENTS">
          {(snapshot) => <AgentBlotter agents={snapshot.agents ?? []} breaker={breaker} live={snapshot.session.live} />}
        </QueryView>
      </Panel>

      <Panel code="ALOC" title="Allocation" className={NARROW}>
        <QueryView query={data.allocations} label="ALLOCATION">
          {(payload) => <AllocationPanel allocations={payload} />}
        </QueryView>
      </Panel>

      {/* Row 3 */}
      <Panel
        code="BLTR"
        title="Purchase blotter"
        className={WIDE}
        flush
        actions={<span className="num text-[11px] font-bold">{purchaseCount} ROWS</span>}
      >
        <QueryView query={data.purchases} label="PURCHASES">
          {(rows) => <PurchaseBlotter purchases={rows} />}
        </QueryView>
      </Panel>

      <Panel
        code="WIRE"
        title="Findings wire"
        className={NARROW}
        flush
        actions={<span className="num text-[11px] font-bold">{findingCount}</span>}
      >
        <QueryView query={data.findings} label="FINDINGS">
          {(rows) => <FindingsWire findings={rows} />}
        </QueryView>
      </Panel>

      {/* Row 4 */}
      <Panel code="EVTS" title="Event calendar" className={QUARTER} flush>
        <QueryView query={data.events} label="EVENTS">
          {(rows) => <EventCalendar events={rows} />}
        </QueryView>
      </Panel>

      <Panel code="PMVO" title="Pred mkt vs options" className={QUARTER} flush>
        <QueryView query={data.pmVsOptions} label="PM VS OPTIONS">
          {(rows) => <PmVsOptions rows={rows} />}
        </QueryView>
      </Panel>

      <div className={`${QUARTER} flex min-w-0 flex-col gap-2`}>
        <Panel code="ALRT" title="Alerts" flush actions={<span className="text-[11px] font-bold">→ TELEGRAM</span>}>
          <QueryView query={data.alerts} label="ALERTS">
            {(rows) => <AlertsLog alerts={rows} />}
          </QueryView>
        </Panel>
        <Panel code="SAVE" title="Savings">
          <QueryView query={data.state} label="STATE">
            {(snapshot) => <SavingsMeter savings={snapshot.savings} />}
          </QueryView>
        </Panel>
      </div>

      <Panel code="ASK" title="Ask the desk" className={QUARTER}>
        <AskBox />
      </Panel>
    </div>
  );
}
