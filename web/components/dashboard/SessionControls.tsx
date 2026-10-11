/**
 * SessionControls: Start / Stop the trading session from the dashboard header.
 *
 * Data: `POST /api/session/start` (the keeper allocates today's budget and recharges agent
 * wallets → recharge txs) and `POST /api/session/stop` (sweeps agent balances back to the
 * owner → sweep txs). Both are relayer transactions; their tx links are shown after the call.
 */

"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/Button";
import { startSession, stopSession } from "@/lib/api";
import { formatAusd } from "@/lib/format";
import type { AgentTx } from "@/lib/types";

import { errorText } from "./QueryState";
import { TxLink } from "./TxLink";
import { QUERY_KEYS } from "./useDashboardData";

interface SessionControlsProps {
  live: boolean;
  explorerTxUrl?: string;
}

export function SessionControls({ live, explorerTxUrl }: SessionControlsProps) {
  const client = useQueryClient();
  const refresh = () => void client.invalidateQueries({ queryKey: QUERY_KEYS.state });
  const start = useMutation({ mutationFn: startSession, onSuccess: refresh });
  const stop = useMutation({ mutationFn: stopSession, onSuccess: refresh });

  const txs: AgentTx[] = start.data?.recharge_txs ?? stop.data?.sweep_txs ?? [];
  const failure = start.error ?? stop.error;
  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex gap-1.5">
        <Button size="sm" variant="primary" busy={start.isPending} disabled={live} onClick={() => start.mutate()}>
          Start session
        </Button>
        <Button size="sm" variant="danger" busy={stop.isPending} disabled={!live} onClick={() => stop.mutate()}>
          Stop &amp; sweep
        </Button>
      </div>
      {failure && <span className="text-[11px] text-down">{errorText(failure)}</span>}
      {txs.length > 0 && (
        <div className="flex flex-wrap justify-end gap-x-3 text-[11px] text-fg-muted">
          {txs.map((tx) => (
            <span key={tx.tx_hash}>
              {tx.agent} {formatAusd(tx.amount_atomic)} <TxLink hash={tx.tx_hash} explorerTxUrl={explorerTxUrl} />
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
