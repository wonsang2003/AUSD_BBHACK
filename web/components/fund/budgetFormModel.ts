/**
 * The /fund budget form: its values (as the user types them, in AUSD / USD / days), defaults,
 * and the conversions to the API's units (AUSD atomic integers, `Settings`, prepare requests).
 *
 * Rule: amounts typed in AUSD are converted with viem's `parseUnits(text, 6)` (exact decimal →
 * integer, no float rounding); an invalid amount converts to null and the form shows an error.
 */

import { parseUnits } from "viem";

import { AGENT_NAMES, AUSD_DECIMALS } from "@/lib/constants";
import { formatFixedPoint } from "@/lib/format";
import type { BudgetPrepareRequest, Settings, Subscription } from "@/lib/types";

export type FundingMode = "permit" | "voucher";

export const VOUCHER_DAYS = 7;
export const DEFAULT_WATCHLIST = ["BTC", "ETH", "SOL", "NVDA", "COIN"];
const DEFAULT_STAKE_USD = 1000;
const DEFAULT_AGENT_CAP_AUSD = "2";

export interface BudgetFormValues {
  totalAusd: string;
  planDays: string;
  dailyCapAusd: string;
  monthlyCapAusd: string;
  sessionStart: string;
  sessionEnd: string;
  sessionTz: string;
  watchlist: string; // space/comma separated symbols
  agentCapsAusd: Record<string, string>;
  stakesUsd: Record<string, string>;
  subscriptions: Subscription[];
}

export function defaultFormValues(agentNames: readonly string[] = AGENT_NAMES): BudgetFormValues {
  return {
    totalAusd: "100",
    planDays: "30",
    dailyCapAusd: "5",
    monthlyCapAusd: "100",
    sessionStart: "09:30",
    sessionEnd: "16:00",
    sessionTz: "America/New_York",
    watchlist: DEFAULT_WATCHLIST.join(" "),
    agentCapsAusd: Object.fromEntries(agentNames.map((name) => [name, DEFAULT_AGENT_CAP_AUSD])),
    stakesUsd: Object.fromEntries(DEFAULT_WATCHLIST.map((asset) => [asset, String(DEFAULT_STAKE_USD)])),
    subscriptions: [
      { name: "Options analytics", monthly_usd: 50 },
      { name: "Market data terminal", monthly_usd: 30 },
    ],
  };
}

/** Atomic → plain AUSD text for an input ("2500000" → "2.5"). */
export function atomicToInput(atomic: number): string {
  return formatFixedPoint(atomic, { decimals: AUSD_DECIMALS, minDecimals: 0, maxDecimals: AUSD_DECIMALS }).replaceAll(",", "");
}

/** Prefill the form from saved settings (zero / empty fields keep the defaults). */
export function formValuesFromSettings(settings: Settings, agentNames: readonly string[]): BudgetFormValues {
  const defaults = defaultFormValues(agentNames);
  const pick = (atomic: number, fallback: string) => (atomic > 0 ? atomicToInput(atomic) : fallback);
  return {
    totalAusd: pick(settings.total_budget_atomic, defaults.totalAusd),
    planDays: settings.plan_days > 0 ? String(settings.plan_days) : defaults.planDays,
    dailyCapAusd: pick(settings.daily_cap_atomic, defaults.dailyCapAusd),
    monthlyCapAusd: pick(settings.monthly_cap_atomic, defaults.monthlyCapAusd),
    sessionStart: settings.session_hours?.start || defaults.sessionStart,
    sessionEnd: settings.session_hours?.end || defaults.sessionEnd,
    sessionTz: settings.session_hours?.tz || defaults.sessionTz,
    watchlist: settings.watchlist?.length ? settings.watchlist.join(" ") : defaults.watchlist,
    agentCapsAusd: Object.fromEntries(
      agentNames.map((name) => [name, pick(settings.agent_caps_atomic?.[name] ?? 0, defaults.agentCapsAusd[name])]),
    ),
    stakesUsd: Object.keys(settings.stakes_usd ?? {}).length
      ? Object.fromEntries(Object.entries(settings.stakes_usd).map(([asset, usd]) => [asset, String(usd)]))
      : defaults.stakesUsd,
    subscriptions: settings.subscriptions?.length ? settings.subscriptions : defaults.subscriptions,
  };
}

/** "5.25" AUSD → 5250000 atomic; null when the text is not a non-negative decimal. */
export function ausdToAtomic(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d+(\.\d{0,6})?$/.test(trimmed)) {
    return null;
  }
  return Number(parseUnits(trimmed, AUSD_DECIMALS));
}

/** "BTC, eth sol" → ["BTC", "ETH", "SOL"] (deduplicated, upper-case). */
export function parseWatchlist(text: string): string[] {
  const symbols = text
    .split(/[\s,]+/)
    .map((symbol) => symbol.trim().toUpperCase())
    .filter(Boolean);
  return [...new Set(symbols)];
}

/** The validated numbers of the form, in API units. */
export interface ParsedBudget {
  totalAtomic: number;
  planDays: number;
  dailyCapAtomic: number;
  monthlyCapAtomic: number;
  agentCapsAtomic: Record<string, number>;
  stakesUsd: Record<string, number>;
  watchlist: string[];
}

/** Parse + validate; returns the list of problems instead when any field is invalid. */
export function parseBudgetForm(values: BudgetFormValues): ParsedBudget | string[] {
  const problems: string[] = [];
  const amount = (label: string, text: string): number => {
    const atomic = ausdToAtomic(text);
    if (atomic === null) {
      problems.push(`${label}: enter an AUSD amount like 5 or 2.50`);
      return 0;
    }
    return atomic;
  };
  const totalAtomic = amount("Total budget", values.totalAusd);
  const dailyCapAtomic = amount("Daily cap", values.dailyCapAusd);
  const monthlyCapAtomic = amount("Monthly cap", values.monthlyCapAusd);
  const agentCapsAtomic = Object.fromEntries(
    Object.entries(values.agentCapsAusd).map(([name, text]) => [name, amount(`${name} cap`, text)]),
  );
  const planDays = Number.parseInt(values.planDays, 10);
  if (!Number.isFinite(planDays) || planDays <= 0) {
    problems.push("Plan days: enter a whole number of days");
  }
  if (dailyCapAtomic <= 0) {
    problems.push("Daily cap must be above 0");
  }
  const stakesUsd = Object.fromEntries(
    Object.entries(values.stakesUsd).map(([asset, text]) => [asset, Number(text) || 0]),
  );
  if (problems.length > 0) {
    return problems;
  }
  return {
    totalAtomic,
    planDays,
    dailyCapAtomic,
    monthlyCapAtomic,
    agentCapsAtomic,
    stakesUsd,
    watchlist: parseWatchlist(values.watchlist),
  };
}

/** The Settings to save before opening the budget (unknown fields of `previous` are kept). */
export function toSettings(
  owner: string,
  parsed: ParsedBudget,
  values: BudgetFormValues,
  previous: Settings | undefined,
): Settings {
  return {
    ...(previous ?? ({} as Settings)),
    owner,
    watchlist: parsed.watchlist,
    stakes_usd: parsed.stakesUsd,
    session_hours: { start: values.sessionStart, end: values.sessionEnd, tz: values.sessionTz },
    plan_days: parsed.planDays,
    monthly_cap_atomic: parsed.monthlyCapAtomic,
    daily_cap_atomic: parsed.dailyCapAtomic,
    total_budget_atomic: parsed.totalAtomic,
    agent_caps_atomic: parsed.agentCapsAtomic,
    subscriptions: values.subscriptions.filter((sub) => sub.name.trim() !== ""),
  };
}

export function toPrepareRequest(owner: string, parsed: ParsedBudget): BudgetPrepareRequest {
  return {
    owner,
    total_budget_atomic: parsed.totalAtomic,
    daily_cap_atomic: parsed.dailyCapAtomic,
    plan_days: parsed.planDays,
    agent_caps_atomic: parsed.agentCapsAtomic,
  };
}

/** Voucher mode: the same daily cap for each of the next 7 UTC days. */
export function voucherDailyValues(parsed: ParsedBudget): number[] {
  return Array.from({ length: VOUCHER_DAYS }, () => parsed.dailyCapAtomic);
}
