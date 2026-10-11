/**
 * FundPage: `/fund` — connect, get test AUSD, set the budget, sign, and keep the kill switches
 * at hand.
 *
 *   left:  1. Wallet (connect · on-chain AUSD balance · faucet)
 *          2. Budget form + mode toggle → save settings → sign → open (Flexible) or submit vouchers (Maximum safety)
 *   right: Controls (pause / resume / revoke) · weekly voucher calendar · top-up QR · tx log
 *
 * Data: `GET /api/config` (AUSD address, agent names, explorer), `GET /api/settings` (prefill),
 * `GET /api/state` (budget status), `GET /api/vouchers`; writes go through `flows.ts`.
 * The user only signs EIP-712 messages; every transaction is submitted (and paid) by the relayer.
 */

"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useAccount } from "wagmi";

import { QUERY_KEYS } from "@/components/dashboard/useDashboardData";
import { Card } from "@/components/ui/Card";
import { getConfig, getSettings, getState, listVouchers, saveSettings } from "@/lib/api";
import { AGENT_NAMES } from "@/lib/constants";
import { AUSD_ADDRESS } from "@/lib/env";
import type { Address } from "viem";

import { BudgetControls } from "./BudgetControls";
import { BudgetForm } from "./BudgetForm";
import {
  defaultFormValues,
  formValuesFromSettings,
  parseBudgetForm,
  toPrepareRequest,
  toSettings,
  voucherDailyValues,
  type BudgetFormValues,
  type FundingMode,
} from "./budgetForm";
import { openPermitBudget, openVoucherBudget } from "./flows";
import { FlowStatus } from "./FlowStatus";
import { TopUpQr } from "./TopUpQr";
import { TxLog, type TxLogEntry } from "./TxLog";
import { useFlowRunner } from "./useFlowRunner";
import { useTypedDataSigner } from "./useTypedDataSigner";
import { VoucherCalendar } from "./VoucherCalendar";
import { WalletPanel } from "./WalletPanel";

export function FundPage() {
  const { address } = useAccount();
  const client = useQueryClient();
  const sign = useTypedDataSigner();
  const runner = useFlowRunner();

  const config = useQuery({ queryKey: QUERY_KEYS.config, queryFn: getConfig, staleTime: Infinity });
  const settings = useQuery({ queryKey: ["settings"], queryFn: getSettings });
  const state = useQuery({ queryKey: QUERY_KEYS.state, queryFn: getState, refetchInterval: 10_000 });
  const vouchers = useQuery({ queryKey: [...QUERY_KEYS.vouchers, address], queryFn: () => listVouchers(address) });

  const agentNames = config.data?.agents.map((agent) => agent.name) ?? [...AGENT_NAMES];
  const [values, setValues] = useState<BudgetFormValues>(() => defaultFormValues());
  const [mode, setMode] = useState<FundingMode>("permit");
  const [problems, setProblems] = useState<string[]>([]);
  const [txLog, setTxLog] = useState<TxLogEntry[]>([]);

  // Prefill once from the saved settings and the current funding mode.
  const prefilled = useRef(false);
  useEffect(() => {
    if (!prefilled.current && settings.data && config.data) {
      prefilled.current = true;
      setValues(formValuesFromSettings(settings.data, config.data.agents.map((agent) => agent.name)));
    }
  }, [settings.data, config.data]);
  useEffect(() => {
    if (state.data?.mode) {
      setMode(state.data.mode);
    }
  }, [state.data?.mode]);

  const explorerTxUrl = config.data?.explorer_tx_url;
  const ausdAddress = (config.data?.ausd_address as Address | undefined) ?? AUSD_ADDRESS;
  const addTx = (label: string, hash: string) => setTxLog((log) => [{ label, hash, at: Date.now() }, ...log]);
  const refreshAfterTx = () => {
    void client.invalidateQueries({ queryKey: QUERY_KEYS.state });
    void client.invalidateQueries({ queryKey: QUERY_KEYS.vouchers });
  };

  async function submitBudget(): Promise<void> {
    if (!address) {
      return;
    }
    const parsed = parseBudgetForm(values);
    if (Array.isArray(parsed)) {
      setProblems(parsed);
      return;
    }
    setProblems([]);
    await runner.run(async (progress) => {
      progress("Saving settings…");
      await saveSettings(toSettings(address, parsed, values, settings.data));
      if (mode === "permit") {
        const hash = await openPermitBudget(toPrepareRequest(address, parsed), sign, progress);
        addTx("Budget opened (permit + config)", hash);
      } else {
        const result = await openVoucherBudget(address, voucherDailyValues(parsed), sign, progress);
        if (result.tx_hash) {
          addTx("Voucher budget config registered", result.tx_hash);
        }
      }
      refreshAfterTx();
    });
  }

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
      <div className="flex min-w-0 flex-col gap-3 lg:col-span-7">
        <Card title="1 · Wallet" subtitle="Base Sepolia · you sign, the relayer pays gas">
          <WalletPanel ausdAddress={ausdAddress} explorerTxUrl={explorerTxUrl} onTx={addTx} />
        </Card>
        <Card title="2 · Budget" subtitle={settings.isError ? "settings unavailable — using defaults" : "caps the agents can never exceed"}>
          <BudgetForm
            values={values}
            onChange={setValues}
            mode={mode}
            onModeChange={setMode}
            onSubmit={() => void submitBudget()}
            busy={runner.busy}
            disabled={!address}
            problems={address ? problems : ["Connect a wallet first."]}
          />
          <div className="mt-2">
            <FlowStatus runner={runner} />
          </div>
          <p className="mt-2 text-[11px] text-fg-dim">
            Agents: {agentNames.join(", ")}. Their per-agent caps go into the signed BudgetConfig.
          </p>
        </Card>
      </div>
      <div className="flex min-w-0 flex-col gap-3 lg:col-span-5">
        <Card title="Controls" subtitle="gasless pause · revoke allowance">
          <BudgetControls owner={address} budget={state.data?.budget} sign={sign} onTx={addTx} />
        </Card>
        <Card title="Daily vouchers" subtitle="Maximum safety mode · one voucher per UTC day">
          <VoucherCalendar
            owner={address}
            query={vouchers}
            sign={sign}
            explorerTxUrl={explorerTxUrl}
            onTx={addTx}
            onChanged={refreshAfterTx}
          />
        </Card>
        <Card title="Top up" subtitle="EIP-681 QR · scan with a phone wallet">
          <TopUpQr ausdAddress={ausdAddress} recipient={address} />
        </Card>
        <Card title="Transactions" subtitle="this session">
          <TxLog entries={txLog} explorerTxUrl={explorerTxUrl} />
        </Card>
      </div>
    </div>
  );
}
