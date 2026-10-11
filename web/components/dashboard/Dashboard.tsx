/**
 * Dashboard: the live trading-terminal view at `/`.
 *
 * Layout (laptop width and up; stacks on narrow screens):
 *   breaker banner (only when the token is paused)
 *   market strip
 *   budget header (allowance, caps, runway, exposure, mode, who pays gas, session buttons)
 *   left 2/3: allocation bars · agent cards · purchase feed · findings feed
 *   right 1/3: safety card · ask box · event radar · PM vs options · savings · alerts
 *
 * Data: `useDashboardData()` — one GET per panel, kept live by the SSE stream. Each panel
 * handles its own loading / error / empty state, so a single failing endpoint never blanks the page.
 */

"use client";

import { Card } from "@/components/ui/Card";

import { AgentCards } from "./AgentCards";
import { AlertsLog } from "./AlertsLog";
import { AllocationBars } from "./AllocationBars";
import { AskBox } from "./AskBox";
import { BreakerBanner } from "./BreakerBanner";
import { BudgetHeader } from "./BudgetHeader";
import { EventRadar } from "./EventRadar";
import { FindingsFeed } from "./FindingsFeed";
import { MarketStrip } from "./MarketStrip";
import { PmVsOptions } from "./PmVsOptions";
import { PurchaseFeed } from "./PurchaseFeed";
import { QueryState } from "./QueryState";
import { SafetyCard } from "./SafetyCard";
import { SavingsMeter } from "./SavingsMeter";
import { useDashboardData } from "./useDashboardData";

export function Dashboard() {
  const data = useDashboardData();
  const explorerTxUrl = data.config.data?.explorer_tx_url;
  const breaker = data.breaker.data ?? data.state.data?.breaker;
  const agents = data.state.data?.agents ?? [];

  return (
    <div className="flex flex-col gap-3">
      <BreakerBanner breaker={breaker} />
      <MarketStrip query={data.market} />
      <QueryState query={data.state}>
        {(state) => <BudgetHeader state={state} streamStatus={data.stream.status} explorerTxUrl={explorerTxUrl} />}
      </QueryState>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-3 lg:col-span-8">
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            <Card title="Today's allocation" subtitle="keeper split · reasons · realized value per $">
              <QueryState
                query={data.allocations}
                isEmpty={(payload) => payload.allocations.length === 0}
                emptyText="No allocation yet — start a session."
              >
                {(payload) => <AllocationBars allocations={payload} agents={agents} />}
              </QueryState>
            </Card>
            <Card title="Agents" subtitle="AUSD wallets · 0 ETH (relayer pays gas)">
              <QueryState query={data.state} isEmpty={(state) => state.agents.length === 0} emptyText="No agents configured.">
                {(state) => <AgentCards agents={state.agents} breaker={breaker} />}
              </QueryState>
            </Card>
          </div>
          <Card title="Purchase feed" subtitle="bought · skipped by the VOI gate · failed" flush>
            <QueryState query={data.purchases}>
              {(rows) => <PurchaseFeed purchases={rows} explorerTxUrl={explorerTxUrl} />}
            </QueryState>
          </Card>
          <Card title="Findings" subtitle="click a row for the summary and paid receipts" flush>
            <QueryState query={data.findings}>
              {(rows) => <FindingsFeed findings={rows} explorerTxUrl={explorerTxUrl} />}
            </QueryState>
          </Card>
        </div>

        <div className="flex min-w-0 flex-col gap-3 lg:col-span-4">
          <Card title="Safety">
            <QueryState query={data.state}>{(state) => <SafetyCard state={state} vouchers={data.vouchers.data} />}</QueryState>
          </Card>
          <Card title="Ask the desk">
            <AskBox explorerTxUrl={explorerTxUrl} />
          </Card>
          <Card title="Event radar" subtitle="today" flush>
            <div className="p-3">
              <QueryState query={data.events} isEmpty={(rows) => rows.length === 0} emptyText="No scheduled events today.">
                {(rows) => <EventRadar events={rows} />}
              </QueryState>
            </div>
          </Card>
          <Card title="Prediction market vs options" subtitle="latest pm_edge purchase" flush>
            <QueryState query={data.pmVsOptions} isEmpty={(rows) => rows.length === 0} emptyText="No pm_edge purchase yet.">
              {(rows) => <PmVsOptions rows={rows} explorerTxUrl={explorerTxUrl} />}
            </QueryState>
          </Card>
          <Card title="Savings meter" subtitle="vs subscriptions">
            <QueryState query={data.state}>{(state) => <SavingsMeter savings={state.savings} />}</QueryState>
          </Card>
          <Card title="Alerts" subtitle="also sent to Telegram">
            <QueryState query={data.alerts} isEmpty={(rows) => rows.length === 0} emptyText="No alerts.">
              {(rows) => <AlertsLog alerts={rows} />}
            </QueryState>
          </Card>
        </div>
      </div>
    </div>
  );
}
