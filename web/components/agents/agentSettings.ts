/**
 * The /agents form model: per-agent caps, stake per asset, watchlist and session hours, as the
 * user types them, plus the conversion to and from `Settings` (`GET/POST /api/settings`).
 * AUSD amounts are converted exactly with `ausdToAtomic` / `atomicToInput` (`fund/budgetForm.ts`).
 */

import { atomicToInput, ausdToAtomic, DEFAULT_WATCHLIST, parseWatchlist } from "@/components/fund/budgetForm";
import type { Settings } from "@/lib/types";

const DEFAULT_STAKE_USD = "1000";

export interface AgentSettingsValues {
  watchlist: string;
  sessionStart: string;
  sessionEnd: string;
  sessionTz: string;
  stakesUsd: Record<string, string>;
  agentCapsAusd: Record<string, string>;
}

export function valuesFromSettings(settings: Settings, agentNames: string[]): AgentSettingsValues {
  return {
    watchlist: (settings.watchlist?.length ? settings.watchlist : DEFAULT_WATCHLIST).join(" "),
    sessionStart: settings.session_hours?.start ?? "09:30",
    sessionEnd: settings.session_hours?.end ?? "16:00",
    sessionTz: settings.session_hours?.tz ?? "America/New_York",
    stakesUsd: Object.fromEntries(Object.entries(settings.stakes_usd ?? {}).map(([asset, usd]) => [asset, String(usd)])),
    agentCapsAusd: Object.fromEntries(
      agentNames.map((name) => [name, atomicToInput(settings.agent_caps_atomic?.[name] ?? 0)]),
    ),
  };
}

/** Stake text for one asset (default 1000 USD when the user has not set one). */
export function stakeFor(values: AgentSettingsValues, asset: string): string {
  return values.stakesUsd[asset] ?? DEFAULT_STAKE_USD;
}

/** Merge the form into the saved settings; returns a list of problems when a cap is invalid. */
export function toSettings(values: AgentSettingsValues, previous: Settings): Settings | string[] {
  const problems: string[] = [];
  const agentCaps: Record<string, number> = {};
  for (const [name, text] of Object.entries(values.agentCapsAusd)) {
    const atomic = ausdToAtomic(text);
    if (atomic === null) {
      problems.push(`${name}: enter an AUSD cap like 2 or 1.50`);
    } else {
      agentCaps[name] = atomic;
    }
  }
  if (problems.length > 0) {
    return problems;
  }
  const watchlist = parseWatchlist(values.watchlist);
  return {
    ...previous,
    watchlist,
    session_hours: { start: values.sessionStart, end: values.sessionEnd, tz: values.sessionTz },
    stakes_usd: Object.fromEntries(watchlist.map((asset) => [asset, Number(stakeFor(values, asset)) || 0])),
    agent_caps_atomic: agentCaps,
  };
}
