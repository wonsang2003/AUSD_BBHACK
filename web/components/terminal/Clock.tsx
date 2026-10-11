/**
 * Clock: live local time + New York time and the US market session (PRE / OPEN / AFTER / CLOSED).
 *
 * Renders placeholders on the server and starts ticking after mount (avoids a hydration mismatch).
 * Session colors: OPEN green, PRE / AFTER yellow, CLOSED grey.
 */

"use client";

import { useEffect, useState } from "react";

import { localTime, etTime, marketSession, type MarketSession } from "./marketTime";
import { Pill, type PillTone } from "./Pill";

const TICK_MS = 1_000;

const SESSION_TONE: Record<MarketSession, PillTone> = {
  PRE: "yellow",
  OPEN: "green",
  AFTER: "yellow",
  CLOSED: "grey",
};

export function Clock() {
  const [now, setNow] = useState<Date | null>(null);

  useEffect(() => {
    setNow(new Date());
    const timer = setInterval(() => setNow(new Date()), TICK_MS);
    return () => clearInterval(timer);
  }, []);

  const session = now ? marketSession(now) : null;
  return (
    <div className="flex items-center gap-3 whitespace-nowrap text-[12px]">
      <span title="Your local time">
        <span className="text-fg-muted">LCL </span>
        <span className="num font-bold text-fg">{now ? localTime(now) : "--:--:--"}</span>
      </span>
      <span title="New York (ET)">
        <span className="text-fg-muted">NY </span>
        <span className="num font-bold text-amber">{now ? etTime(now) : "--:--:--"}</span>
      </span>
      {session ? (
        <Pill tone={SESSION_TONE[session]} title="US equity session (ET): PRE 04:00, OPEN 09:30, AFTER 16:00, CLOSED 20:00">
          {session}
        </Pill>
      ) : null}
    </div>
  );
}
