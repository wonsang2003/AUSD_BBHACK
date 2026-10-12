/**
 * New York (ET) time helpers for the terminal: clock strings, table timestamps in ET, and the
 * US equity market session.
 *
 * Session rule (America/New_York, weekdays only; holidays are not modelled):
 *   PRE    04:00–09:30     OPEN   09:30–16:00     AFTER  16:00–20:00     CLOSED otherwise / weekends
 */

const NEW_YORK = "America/New_York";
const MINUTES_PER_HOUR = 60;
const PRE_OPEN_MIN = 4 * MINUTES_PER_HOUR;
const OPEN_MIN = 9 * MINUTES_PER_HOUR + 30;
const CLOSE_MIN = 16 * MINUTES_PER_HOUR;
const AFTER_END_MIN = 20 * MINUTES_PER_HOUR;
const WEEKEND = new Set(["Sat", "Sun"]);

export type MarketSession = "PRE" | "OPEN" | "AFTER" | "CLOSED";

const ET_PARTS = new Intl.DateTimeFormat("en-US", {
  timeZone: NEW_YORK,
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const ET_TIME = new Intl.DateTimeFormat("en-GB", {
  timeZone: NEW_YORK,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const ET_SHORT_TIME = new Intl.DateTimeFormat("en-GB", {
  timeZone: NEW_YORK,
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

const ET_DATE = new Intl.DateTimeFormat("en-US", { timeZone: NEW_YORK, month: "short", day: "2-digit" });

const LOCAL_TIME = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

interface EtParts {
  weekday: string;
  minutesOfDay: number;
}

function etParts(date: Date): EtParts {
  const parts = ET_PARTS.formatToParts(date);
  const read = (type: string): string => parts.find((part) => part.type === type)?.value ?? "0";
  return {
    weekday: read("weekday"),
    minutesOfDay: Number(read("hour")) * MINUTES_PER_HOUR + Number(read("minute")),
  };
}

/** The US equity session at `date` (see the rule in the header). */
export function marketSession(date: Date): MarketSession {
  const { weekday, minutesOfDay } = etParts(date);
  if (WEEKEND.has(weekday)) {
    return "CLOSED";
  }
  if (minutesOfDay >= PRE_OPEN_MIN && minutesOfDay < OPEN_MIN) {
    return "PRE";
  }
  if (minutesOfDay >= OPEN_MIN && minutesOfDay < CLOSE_MIN) {
    return "OPEN";
  }
  if (minutesOfDay >= CLOSE_MIN && minutesOfDay < AFTER_END_MIN) {
    return "AFTER";
  }
  return "CLOSED";
}

function parseWhen(when: string | number | Date | null | undefined): Date | null {
  if (when === null || when === undefined || when === "") {
    return null;
  }
  const date = when instanceof Date ? when : new Date(typeof when === "number" ? when * 1000 : when);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "14:05:09" in New York time; "—" for a missing or invalid timestamp (numbers are unix seconds). */
export function etTime(when: string | number | Date | null | undefined): string {
  const date = parseWhen(when);
  return date ? ET_TIME.format(date) : "—";
}

/** "14:05" in New York time. */
export function etShortTime(when: string | number | Date | null | undefined): string {
  const date = parseWhen(when);
  return date ? ET_SHORT_TIME.format(date) : "—";
}

/** "Oct 11" in New York time. */
export function etDate(when: string | number | Date | null | undefined): string {
  const date = parseWhen(when);
  return date ? ET_DATE.format(date) : "—";
}

/** "14:05:09" in the viewer's local time. */
export function localTime(date: Date): string {
  return LOCAL_TIME.format(date);
}
