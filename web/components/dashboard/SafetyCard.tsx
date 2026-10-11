/**
 * SafetyCard: the "what can go wrong" numbers, in plain words.
 *
 * Data: `State` (+ today's voucher from `GET /api/vouchers` in voucher mode).
 *   * Max loss if an agent is compromised today = the largest agent wallet balance (an agent key
 *     can only ever spend what is in its own wallet; the contract caps recharges).
 *   * Custody: permit mode → funds stay in your wallet behind a revocable allowance.
 *   * Voucher mode → today's spend is bounded by today's voucher, which you signed; vouchers pay
 *     only your own registered agents, through our contract, on their own UTC day.
 */

import { formatAusd } from "@/lib/format";
import { utcDayIndex } from "@/lib/eip712";
import type { State, Voucher } from "@/lib/types";

interface SafetyCardProps {
  state: State;
  vouchers: Voucher[] | undefined;
}

/** Atomic value of a voucher row (`value` or `value_atomic`, whichever the backend sent). */
export function voucherValue(voucher: Voucher): number {
  return Number(voucher.value ?? voucher.value_atomic ?? 0);
}

function todaysVoucher(vouchers: Voucher[] | undefined): Voucher | undefined {
  const today = utcDayIndex(Math.floor(Date.now() / 1000));
  return vouchers?.find((voucher) => voucher.day_index === today);
}

export function SafetyCard({ state, vouchers }: SafetyCardProps) {
  const maxAgentBalance = Math.max(0, ...state.agents.map((agent) => agent.balance));
  const voucherMode = state.mode === "voucher";
  const today = todaysVoucher(vouchers);
  const todayLimit = today ? voucherValue(today) : state.budget.daily_cap;
  return (
    <ul className="flex flex-col gap-2 text-xs">
      <li>
        Max loss if an agent is compromised today:{" "}
        <span className="num font-semibold text-warn">{formatAusd(maxAgentBalance, true)}</span>
        <div className="text-[11px] text-fg-dim">= the largest agent wallet balance; agents hold no ETH and no allowance.</div>
      </li>
      {voucherMode ? (
        <>
          <li>
            Today&apos;s spend can never exceed <span className="num font-semibold text-up">{formatAusd(todayLimit, true)}</span>,
            enforced by your signature.
          </li>
          <li className="text-fg-muted">Vouchers can only pay your own registered agents, through our contract, on their own day.</li>
        </>
      ) : (
        <li>
          Custody: <span className="font-semibold text-up">your wallet (permit), revocable</span>
          <div className="text-[11px] text-fg-dim">
            Daily cap {formatAusd(state.budget.daily_cap, true)} enforced by the BudgetController. Revoke anytime on /fund.
          </div>
        </li>
      )}
    </ul>
  );
}
