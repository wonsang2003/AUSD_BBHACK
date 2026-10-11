/**
 * AlertsLog: the same alerts that go to Telegram (cap hits, skipped frozen counterparties,
 * breaker trips, recharges), newest first.
 *
 * Data: `GET /api/alerts` + the `alert` SSE event.
 */

import { Badge } from "@/components/ui/Badge";
import { clockTime } from "@/lib/format";
import type { Alert, AlertLevel } from "@/lib/types";
import type { Tone } from "@/lib/ui";

const LEVEL_TONE: Record<AlertLevel, Tone> = { info: "info", warn: "warn", critical: "down" };

export function AlertsLog({ alerts }: { alerts: Alert[] }) {
  return (
    <ul className="flex max-h-[260px] flex-col gap-1 overflow-auto">
      {alerts.map((alert) => (
        <li key={alert.id} className="flex items-start gap-2 text-[11px]">
          <span className="num shrink-0 text-fg-dim">{clockTime(alert.ts)}</span>
          <Badge tone={LEVEL_TONE[alert.level]}>{alert.kind}</Badge>
          <span className="min-w-0 text-fg-muted">{alert.message}</span>
        </li>
      ))}
    </ul>
  );
}
