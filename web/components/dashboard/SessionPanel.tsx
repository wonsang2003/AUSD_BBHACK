/**
 * SessionPanel (SESS): start / run / stop the trading session, or fund a demo budget.
 *
 * Actions (all relayer transactions; errors are shown inline, never thrown):
 *   START SESSION   POST /api/session/start → keeper allocates today's budget, recharges agents
 *   RUN AGENTS NOW  POST /api/session/tick  → every agent decides and buys once, right now
 *   STOP & SWEEP    POST /api/session/stop  → sweeps agent balances back to the owner
 *   DEMO FUND       POST /api/demo/fund {}  → only when no budget is open and
 *                   GET /api/demo/status says the server-side demo wallet is available
 * Each success refreshes state / purchases / findings / allocations.
 */

"use client";

import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";

import { InlineError, MutedLine, errorText } from "@/components/terminal/InlineError";
import { Pill } from "@/components/terminal/Pill";
import { QUERY_KEYS } from "@/components/terminal/queryKeys";
import { TermButton } from "@/components/terminal/TermButton";
import { TxHash } from "@/components/terminal/TxHash";
import { fmtAusd } from "@/components/terminal/format";
import { etShortTime } from "@/components/terminal/marketTime";
import { demoFund, sessionTick, startSession, stopSession } from "@/lib/api";
import type { AgentTx, DemoFundStep, DemoStatus, SessionTickResponse, State } from "@/lib/types";

interface SessionPanelProps {
  state: State;
  demo: UseQueryResult<DemoStatus>;
}

const REFRESH_KEYS = [
  QUERY_KEYS.state,
  QUERY_KEYS.purchases,
  QUERY_KEYS.findings,
  QUERY_KEYS.allocations,
  QUERY_KEYS.breaker,
  QUERY_KEYS.demoStatus,
];

function tickSummary(result: SessionTickResponse): string {
  if (result.message) {
    return result.message;
  }
  const purchases = result.purchases ?? [];
  const bought = purchases.filter((row) => row.status === "bought").length;
  const findings = result.findings?.length ?? 0;
  return `${purchases.length} decisions · ${bought} bought · ${findings} findings`;
}

function stepLabel(step: DemoFundStep, index: number): string {
  return step.label ?? step.name ?? step.step ?? `STEP ${index + 1}`;
}

function TxList({ txs, verb }: { txs: AgentTx[]; verb: string }) {
  if (txs.length === 0) {
    return null;
  }
  return (
    <div className="flex flex-col">
      {txs.map((tx, index) => (
        <div key={`${tx.tx_hash}-${index}`} className="flex items-center justify-between gap-2 text-[12px]">
          <span className="truncate text-fg-muted">
            {verb} {tx.agent}
          </span>
          <span className="num ml-auto">{fmtAusd(tx.amount_atomic)}</span>
          <TxHash hash={tx.tx_hash} showTag={false} />
        </div>
      ))}
    </div>
  );
}

export function SessionPanel({ state, demo }: SessionPanelProps) {
  const client = useQueryClient();
  const refresh = () => {
    for (const key of REFRESH_KEYS) {
      void client.invalidateQueries({ queryKey: key });
    }
  };
  const start = useMutation({ mutationFn: startSession, onSuccess: refresh });
  const tick = useMutation({ mutationFn: () => sessionTick(), onSuccess: refresh });
  const stop = useMutation({ mutationFn: stopSession, onSuccess: refresh });
  const fund = useMutation({ mutationFn: () => demoFund({}), onSuccess: refresh });

  const live = state.session.live;
  const hasBudget = state.budget.exists;
  const demoAvailable = demo.data?.available === true;
  const steps = fund.data?.steps ?? [];

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        {live ? <Pill tone="green">● LIVE</Pill> : <Pill tone="grey">IDLE</Pill>}
        <span className="num truncate text-[12px] text-fg-muted">
          {live
            ? `since ${etShortTime(state.session.started_at)} ET${state.session.ends_at ? ` · ends ${etShortTime(state.session.ends_at)} ET` : ""}`
            : hasBudget
              ? "budget open · session stopped"
              : "no budget open"}
        </span>
      </div>

      <div className="flex flex-wrap gap-1">
        <TermButton variant="primary" size="sm" busy={start.isPending} disabled={live || !hasBudget} onClick={() => start.mutate()}>
          START SESSION
        </TermButton>
        <TermButton variant="go" size="sm" busy={tick.isPending} disabled={!hasBudget} onClick={() => tick.mutate()}>
          RUN AGENTS NOW
        </TermButton>
        <TermButton variant="danger" size="sm" busy={stop.isPending} disabled={!live} onClick={() => stop.mutate()}>
          STOP &amp; SWEEP
        </TermButton>
      </div>

      {!hasBudget ? (
        demoAvailable ? (
          <div className="flex items-center gap-2">
            <TermButton variant="default" size="sm" busy={fund.isPending} onClick={() => fund.mutate()}>
              DEMO FUND
            </TermButton>
            <span className="truncate text-[12px] text-fg-muted">faucet → permit → open budget (server demo wallet)</span>
          </div>
        ) : (
          <MutedLine className="px-0">
            Fund a budget on F2 FUND.
            {demo.data?.reason ? ` Demo fund unavailable: ${demo.data.reason}` : ""}
            {demo.isError ? ` Demo fund unavailable: ${errorText(demo.error)}` : ""}
          </MutedLine>
        )
      ) : null}

      <InlineError error={start.error} prefix="START SESSION" className="px-0" />
      <InlineError error={tick.error} prefix="RUN AGENTS" className="px-0" />
      <InlineError error={stop.error} prefix="STOP & SWEEP" className="px-0" />
      <InlineError error={fund.error} prefix="DEMO FUND" className="px-0" />

      {tick.data ? <div className="truncate text-[12px] text-up">TICK OK · {tickSummary(tick.data)}</div> : null}
      <TxList txs={start.data?.recharge_txs ?? []} verb="RECHARGE" />
      <TxList txs={stop.data?.sweep_txs ?? []} verb="SWEEP" />

      {fund.data ? (
        <div className="flex flex-col">
          {steps.map((step, index) => (
            <div key={`${stepLabel(step, index)}-${index}`} className="flex items-center gap-2 text-[12px]" title={step.detail ?? undefined}>
              <span className="truncate uppercase text-fg-muted">{stepLabel(step, index)}</span>
              {step.status ? (
                <Pill tone={/fail|error/i.test(step.status) ? "red" : "green"}>{step.status}</Pill>
              ) : null}
              <span className="ml-auto">
                <TxHash hash={step.tx_hash} showTag={false} />
              </span>
            </div>
          ))}
          {steps.length === 0 && fund.data.tx_hash ? (
            <div className="flex items-center gap-2 text-[12px]">
              <span className="text-fg-muted">BUDGET OPENED</span>
              <span className="ml-auto">
                <TxHash hash={fund.data.tx_hash} showTag={false} />
              </span>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
