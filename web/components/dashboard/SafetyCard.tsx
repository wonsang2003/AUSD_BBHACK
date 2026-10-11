/**
 * SafetyCard (SAFE): the "what can go wrong" numbers, as key/value rows.
 *
 * Data: `State` (+ today's voucher from `GET /api/vouchers` in voucher mode, + breaker status).
 *   * Max loss if an agent is compromised = the largest agent AUSD balance (an agent key can only
 *     spend its own wallet; agents hold no ETH and no allowance; the contract caps recharges).
 *   * Custody: permit mode → funds stay in your wallet behind a revocable allowance;
 *     voucher mode → today's spend is bounded by today's signed voucher.
 *   * Compliance switches (freeze check before paying, pre-flight simulation, zero-ETH agents).
 *   * Circuit breaker: global_open = token paused → all agents halted.
 *
 * `voucherValue` is also used by /fund (VoucherCalendar).
 */

import { Pill } from "@/components/terminal/Pill";
import { KeyValue } from "@/components/terminal/Stat";
import { fmtAusd } from "@/components/terminal/format";
import { utcDayIndex } from "@/lib/eip712";
import type { BreakerStatus, State, Voucher } from "@/lib/types";

import { budgetMode } from "./BudgetMonitor";

interface SafetyCardProps {
  state: State;
  vouchers: Voucher[] | undefined;
  breaker: BreakerStatus | undefined;
}

/** Atomic value of a voucher row (`value` or `value_atomic`, whichever the backend sent). */
export function voucherValue(voucher: Voucher): number {
  const value = Number(voucher.value ?? voucher.value_atomic ?? 0);
  return Number.isFinite(value) ? value : 0;
}

function todaysVoucher(vouchers: Voucher[] | undefined): Voucher | undefined {
  const today = utcDayIndex(Math.floor(Date.now() / 1000));
  return vouchers?.find((voucher) => voucher.day_index === today);
}

function SwitchPill({ on, label, hint }: { on: boolean | undefined; label: string; hint: string }) {
  if (on === undefined) {
    return (
      <Pill tone="grey" outline title={`${hint} (not reported)`}>
        {label} ?
      </Pill>
    );
  }
  return (
    <Pill tone={on ? "green" : "red"} outline title={hint}>
      {on ? "✓" : "✗"} {label}
    </Pill>
  );
}

export function SafetyCard({ state, vouchers, breaker }: SafetyCardProps) {
  const maxAgentBalance = Math.max(0, ...state.agents.map((agent) => Number(agent.balance) || 0));
  const mode = budgetMode(state);
  const today = todaysVoucher(vouchers);
  const compliance = state.compliance ?? {};
  const backingOff = breaker ? Object.values(breaker.agents ?? {}).filter((agent) => agent.backoff_seconds > 0).length : 0;

  return (
    <div className="flex flex-col">
      <KeyValue
        label="Max loss if an agent is compromised"
        value={`${fmtAusd(maxAgentBalance)} AUSD`}
        tone="warn"
        hint="= the largest agent AUSD balance; agents hold no ETH and no allowance"
      />
      {mode === "voucher" ? (
        <>
          <KeyValue label="Custody" value="SIGNED DAILY VOUCHERS" tone="up" hint="Vouchers pay only your own agents, on their own UTC day" />
          <KeyValue
            label="Today's voucher limit"
            value={`${fmtAusd(today ? voucherValue(today) : state.budget.daily_cap)} AUSD`}
            hint="Today's spend can never exceed this, enforced by your signature"
          />
        </>
      ) : mode === "permit" ? (
        <>
          <KeyValue label="Custody" value="YOUR WALLET · REVOCABLE" tone="up" hint="Permit allowance; revoke anytime on F2 FUND" />
          <KeyValue label="Daily cap (on-chain)" value={`${fmtAusd(state.budget.daily_cap)} AUSD`} hint="Enforced by the BudgetController" />
        </>
      ) : (
        <KeyValue label="Custody" value="NO BUDGET OPEN" tone="muted" />
      )}
      <KeyValue
        label="Circuit breaker"
        value={
          breaker?.global_open ? (
            <Pill tone="red" title={breaker.global_reason}>
              OPEN · HALTED
            </Pill>
          ) : breaker ? (
            <Pill tone="green">CLOSED · OK</Pill>
          ) : (
            <Pill tone="grey">UNKNOWN</Pill>
          )
        }
        hint={breaker?.global_reason || "Trips when the AUSD token is paused; all agents halt"}
      />
      {backingOff > 0 ? <KeyValue label="Agents backing off" value={String(backingOff)} tone="warn" /> : null}
      <div className="flex flex-wrap items-center gap-1 pt-1.5">
        <span className="t-label mr-1">Compliance</span>
        <SwitchPill on={compliance.freeze_check_before_payment} label="FREEZE CHECK" hint="payTo and payer checked with isAccountFrozen before paying" />
        <SwitchPill on={compliance.preflight_simulation} label="PRE-FLIGHT" hint="Every payment is simulated before it is sent" />
        <SwitchPill on={compliance.agents_zero_eth} label="0-ETH AGENTS" hint="Agents hold no ETH; the relayer pays all gas" />
      </div>
    </div>
  );
}
