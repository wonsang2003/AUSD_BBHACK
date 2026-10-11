/**
 * DemoWalletPanel: fund the budget from the server-side demo wallet (testnet / local key) —
 * works with NO browser wallet extension.
 *
 * Data: `GET /api/demo/status` → {available, reason, owner, chain_id, local_chain};
 * `POST /api/demo/fund` {mode, total_budget_atomic, daily_cap_atomic, plan_days, agent_caps_atomic}
 * → {owner, mode, steps: [{label, tx_hash}]}. Every failure shows the backend `detail` inline.
 */

"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { InlineError, MutedLine } from "@/components/terminal/InlineError";
import { Pill } from "@/components/terminal/Pill";
import { QUERY_KEYS } from "@/components/terminal/queryKeys";
import { TermButton } from "@/components/terminal/TermButton";
import { TxHash } from "@/components/terminal/TxHash";
import { shortHex } from "@/components/terminal/format";
import { demoFund, demoStatus } from "@/lib/api";
import type { DemoFundRequest } from "@/lib/types";

import { parseBudgetForm, type BudgetFormValues } from "./budgetForm";

type DemoMode = "permit" | "voucher";

interface DemoStatusInfo {
  available?: boolean;
  reason?: string | null;
  owner?: string | null;
  user_address?: string | null;
  chain_id?: number;
  local_chain?: boolean;
}

interface DemoStep {
  label?: string;
  step?: string;
  name?: string;
  tx_hash?: string | null;
}

interface DemoFundResult {
  owner?: string;
  mode?: string;
  steps?: DemoStep[];
}

interface DemoFundBody extends DemoFundRequest {
  mode: DemoMode;
}

interface DemoWalletPanelProps {
  values: BudgetFormValues;
  onTx: (label: string, hash: string) => void;
}

export function DemoWalletPanel({ values, onTx }: DemoWalletPanelProps) {
  const client = useQueryClient();
  const status = useQuery({
    queryKey: QUERY_KEYS.demoStatus,
    queryFn: async () => (await demoStatus()) as unknown as DemoStatusInfo,
    retry: false,
  });

  const fund = useMutation({
    mutationFn: async (mode: DemoMode): Promise<DemoFundResult> => {
      const parsed = parseBudgetForm(values);
      if (Array.isArray(parsed)) {
        throw new Error(parsed.join("; "));
      }
      const body: DemoFundBody = {
        mode,
        total_budget_atomic: parsed.totalAtomic,
        daily_cap_atomic: parsed.dailyCapAtomic,
        plan_days: parsed.planDays,
        agent_caps_atomic: parsed.agentCapsAtomic,
      };
      return (await demoFund(body)) as unknown as DemoFundResult;
    },
    onSuccess: (result) => {
      for (const step of result.steps ?? []) {
        if (step.tx_hash) {
          onTx(`DEMO: ${step.label ?? step.step ?? step.name ?? "step"}`, step.tx_hash);
        }
      }
      void client.invalidateQueries({ queryKey: QUERY_KEYS.state });
      void client.invalidateQueries({ queryKey: QUERY_KEYS.vouchers });
      void client.invalidateQueries({ queryKey: QUERY_KEYS.demoStatus });
      void client.invalidateQueries({ queryKey: ["settings"] });
    },
  });

  const info = status.data;
  const owner = info?.owner ?? info?.user_address ?? null;
  const available = info?.available === true;
  const steps = fund.data?.steps ?? [];

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="t-label">DEMO WALLET</span>
        {status.isLoading ? <Pill tone="grey">CHECKING</Pill> : null}
        {info ? <Pill tone={available ? "green" : "red"}>{available ? "READY" : "UNAVAILABLE"}</Pill> : null}
        {info?.local_chain ? <Pill tone="amber" outline>LOCAL CHAIN</Pill> : null}
        {info?.chain_id ? <span className="num text-[12px] text-fg-muted">chain {info.chain_id}</span> : null}
        <span className="num text-[12px] text-info" title={owner ?? undefined}>
          {owner ? shortHex(owner, 6) : "—"}
        </span>
      </div>
      <InlineError error={status.error} prefix="DEMO STATUS" />
      {info && !available && info.reason ? <MutedLine>{info.reason}</MutedLine> : null}
      <div className="flex flex-wrap gap-2">
        <TermButton
          variant="primary"
          busy={fund.isPending && fund.variables === "permit"}
          disabled={fund.isPending || !available}
          onClick={() => fund.mutate("permit")}
        >
          FUND FLEXIBLE (PERMIT)
        </TermButton>
        <TermButton
          variant="go"
          busy={fund.isPending && fund.variables === "voucher"}
          disabled={fund.isPending || !available}
          onClick={() => fund.mutate("voucher")}
        >
          FUND MAX-SAFETY (7 VOUCHERS)
        </TermButton>
      </div>
      <InlineError error={fund.error} prefix="DEMO FUND" />
      {steps.length > 0 ? (
        <div className="flex flex-col">
          <span className="t-label">
            FUNDED {fund.data?.mode ? `· ${String(fund.data.mode).toUpperCase()}` : ""}{" "}
            {fund.data?.owner ? `· OWNER ${shortHex(fund.data.owner, 4)}` : ""}
          </span>
          {steps.map((step, index) => (
            <div
              key={`${index}-${step.tx_hash ?? "none"}`}
              className="flex items-center justify-between gap-3 border-b border-[#1c1c1c] py-[3px] text-[12px]"
            >
              <span className="min-w-0 truncate">
                <span className="text-fg-muted">{index + 1}.</span> {step.label ?? step.step ?? step.name ?? "step"}
              </span>
              <TxHash hash={step.tx_hash} />
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
