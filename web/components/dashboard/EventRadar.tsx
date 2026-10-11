/**
 * EventRadar: today's market-moving events and which agent each one boosts.
 *
 * Data: `GET /api/events-today` (earnings, FOMC, CPI, prediction-market expiries, crypto
 * events). The keeper's allocator gives the boosted agent a larger share of today's budget,
 * which is where the reason chips in the allocation bars come from.
 */

import { Badge } from "@/components/ui/Badge";
import type { EventKind, EventToday } from "@/lib/types";
import type { Tone } from "@/lib/ui";

const KIND_TONE: Record<EventKind, Tone> = {
  earnings: "info",
  fomc: "warn",
  cpi: "warn",
  pm_expiry: "neutral",
  crypto: "up",
};

export function EventRadar({ events }: { events: EventToday[] }) {
  return (
    <ul className="flex flex-col gap-1">
      {events.map((event, index) => (
        <li key={`${event.kind}-${event.title}-${index}`} className="flex items-center gap-2 text-xs">
          <Badge tone={KIND_TONE[event.kind]}>{event.kind.replace("_", " ")}</Badge>
          <span className="min-w-0 flex-1 truncate" title={event.title}>
            {event.title}
          </span>
          {event.asset && <span className="num text-[11px] text-fg-muted">{event.asset}</span>}
          {event.boosted_agent && <span className="text-[10px] text-up">↑ {event.boosted_agent}</span>}
        </li>
      ))}
    </ul>
  );
}
