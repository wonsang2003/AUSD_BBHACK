/**
 * ModeToggle: how the budget is funded.
 *   Flexible (permit)          — one ERC-2612 permit gives the BudgetController a revocable allowance;
 *                                the contract enforces the daily + per-agent caps.
 *   Maximum safety (vouchers)  — 7 signed ERC-3009 vouchers, one per UTC day; each can only be
 *                                redeemed on its own day, only through our contract, only to your
 *                                registered agents. No standing allowance at all.
 */

import { cx } from "@/lib/ui";

import type { FundingMode } from "./budgetForm";

const OPTIONS: Array<{ mode: FundingMode; title: string; detail: string }> = [
  { mode: "permit", title: "Flexible", detail: "1 permit · revocable allowance · 2 signatures" },
  { mode: "voucher", title: "Maximum safety", detail: "7 daily vouchers · no allowance · 8 signatures" },
];

export function ModeToggle({ mode, onChange }: { mode: FundingMode; onChange: (mode: FundingMode) => void }) {
  return (
    <div className="grid grid-cols-2 gap-2" role="radiogroup" aria-label="Funding mode">
      {OPTIONS.map((option) => (
        <button
          key={option.mode}
          type="button"
          role="radio"
          aria-checked={mode === option.mode}
          onClick={() => onChange(option.mode)}
          className={cx(
            "flex flex-col items-start rounded-sm border px-2 py-1.5 text-left",
            mode === option.mode ? "border-up bg-up/10" : "border-line bg-surface-2 hover:border-fg-dim",
          )}
        >
          <span className="text-xs font-semibold">{option.title}</span>
          <span className="text-[10px] text-fg-muted">{option.detail}</span>
        </button>
      ))}
    </div>
  );
}
