/**
 * BudgetControls: pause / resume and revoke — the user's kill switches.
 *
 *   Pause / Resume — gasless: sign `SetPaused`, the relayer submits `setPausedWithSig`; while
 *                    paused the BudgetController refuses every recharge.
 *   Revoke         — sign a permit with value 0: the controller's allowance drops to 0, so it
 *                    can never pull another AUSD from the wallet.
 * Data: `State.budget` (paused / revoked / exists) from `GET /api/state`.
 */

"use client";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { formatAusd } from "@/lib/format";
import type { BudgetState } from "@/lib/types";

import { revokeBudget, setBudgetPaused } from "./flows";
import { FlowStatus } from "./FlowStatus";
import { useFlowRunner } from "./useFlowRunner";
import type { SignFn } from "./useTypedDataSigner";

interface BudgetControlsProps {
  owner: string | undefined;
  budget: BudgetState | undefined;
  sign: SignFn;
  onTx: (label: string, hash: string) => void;
}

const REVOKE_CONFIRM = "Revoke the BudgetController's allowance? Agents stop being recharged until you open a new budget.";

export function BudgetControls({ owner, budget, sign, onTx }: BudgetControlsProps) {
  const runner = useFlowRunner();
  if (!owner) {
    return <div className="text-xs text-fg-dim">Connect a wallet to manage the budget.</div>;
  }
  const paused = budget?.paused ?? false;

  async function togglePause(): Promise<void> {
    const hash = await runner.run((progress) => setBudgetPaused(owner as string, !paused, sign, progress));
    if (hash) {
      onTx(paused ? "Budget resumed" : "Budget paused", hash);
    }
  }

  async function revoke(): Promise<void> {
    if (!window.confirm(REVOKE_CONFIRM)) {
      return;
    }
    const hash = await runner.run((progress) => revokeBudget(owner as string, sign, progress));
    if (hash) {
      onTx("Allowance revoked (permit value 0)", hash);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {budget?.exists ? <Badge tone="up">budget open</Badge> : <Badge tone="warn">no budget yet</Badge>}
        {paused && <Badge tone="warn">paused</Badge>}
        {budget?.revoked && <Badge tone="down">revoked</Badge>}
        {budget?.exists && (
          <span className="num text-fg-muted">
            allowance {formatAusd(budget.allowance_remaining, true)} · daily cap {formatAusd(budget.daily_cap, true)}
          </span>
        )}
      </div>
      <div className="flex gap-2">
        <Button variant={paused ? "primary" : "danger"} busy={runner.busy} onClick={() => void togglePause()}>
          {paused ? "Resume budget" : "Pause budget"}
        </Button>
        <Button variant="danger" busy={runner.busy} onClick={() => void revoke()}>
          Revoke allowance
        </Button>
      </div>
      <FlowStatus runner={runner} />
    </div>
  );
}
