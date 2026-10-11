/**
 * SavingsMeter: pay-per-call research vs the data subscriptions it replaces.
 *
 * Data: `State.savings` — month-to-date AUSD spend vs the subscriptions the user listed on /fund,
 * prorated to today. Formula: saved = prorated subscriptions (USD) − MTD spend (AUSD ≈ USD).
 */

import { ProgressBar } from "@/components/ui/ProgressBar";
import { formatUsd } from "@/lib/format";
import { AUSD_DECIMALS } from "@/lib/constants";
import type { SavingsState } from "@/lib/types";

const ATOMIC_PER_AUSD = 10 ** AUSD_DECIMALS;

export function SavingsMeter({ savings }: { savings: SavingsState }) {
  const spentUsd = savings.mtd_spend_atomic / ATOMIC_PER_AUSD;
  const subscriptionsUsd = savings.prorated_subscriptions_usd;
  const saved = subscriptionsUsd - spentUsd;
  if (savings.subscriptions.length === 0) {
    return <div className="text-xs text-fg-dim">Add the subscriptions you would otherwise pay for on /fund.</div>;
  }
  return (
    <div className="flex flex-col gap-1.5 text-xs">
      <div className="flex items-baseline justify-between">
        <span className="text-fg-muted">Saved this month</span>
        <span className={saved >= 0 ? "num text-lg font-semibold text-up" : "num text-lg font-semibold text-down"}>
          {formatUsd(saved)}
        </span>
      </div>
      <ProgressBar
        value={spentUsd}
        max={subscriptionsUsd}
        label={`spent ${formatUsd(spentUsd)} vs ${formatUsd(subscriptionsUsd)} prorated subscriptions`}
      />
      <div className="text-[11px] text-fg-dim">
        {savings.subscriptions.map((sub) => `${sub.name} ${formatUsd(sub.monthly_usd, 0)}/mo`).join(" · ")}
      </div>
    </div>
  );
}
