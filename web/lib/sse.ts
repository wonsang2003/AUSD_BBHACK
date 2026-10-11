/**
 * `useEventStream`: live dashboard updates from the backend's Server-Sent Events stream.
 *
 * The backend pushes `event: <type>` + `data: <json>` on `GET /api/stream` (types and payloads
 * in `StreamEventMap`, `lib/types.ts`). This hook opens one EventSource, parses each event's
 * JSON and calls the handler registered for that type.
 *
 * Reconnect rule: on any error the stream is closed and reopened after a delay that starts at
 * 1 s and doubles per consecutive failure, capped at 30 s; a successful open resets it to 1 s.
 * (The browser's own EventSource retry gives up for good on some errors, e.g. a 502 while the
 * backend restarts, so the hook always reconnects itself.)
 *
 * Handlers may change on every render; only a change of `url` reconnects.
 */

import { useEffect, useRef, useState } from "react";

import { streamUrl } from "./api";
import type { StreamEventMap, StreamEventType } from "./types";

export const INITIAL_RETRY_MS = 1_000;
export const MAX_RETRY_MS = 30_000;
const RETRY_BACKOFF_FACTOR = 2;

/** Every event type the backend sends; each gets its own listener. */
export const STREAM_EVENT_TYPES: readonly StreamEventType[] = [
  "purchase",
  "finding",
  "allocation",
  "recharge",
  "sweep",
  "alert",
  "state",
  "market",
  "ask",
  "breaker",
  "ping",
];

export type StreamHandlers = { [Type in StreamEventType]?: (data: StreamEventMap[Type]) => void };

export type StreamStatus = "connecting" | "open" | "reconnecting";

export interface EventStreamState {
  status: StreamStatus;
  /** Browser time (ms) of the last event received, including pings; null before the first. */
  lastEventAt: number | null;
}

/** Delay before the next reconnect attempt: double the previous one, capped at MAX_RETRY_MS. */
export function nextRetryDelay(previousMs: number): number {
  return Math.min(previousMs * RETRY_BACKOFF_FACTOR, MAX_RETRY_MS);
}

/** Parse an event's JSON payload; returns undefined (and warns) on malformed data. */
function parseEventData(type: StreamEventType, raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    console.warn(`SSE: ignoring malformed "${type}" event`);
    return undefined;
  }
}

export function useEventStream(handlers: StreamHandlers, url: string = streamUrl()): EventStreamState {
  const handlersRef = useRef<StreamHandlers>(handlers);
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);

  useEffect(() => {
    handlersRef.current = handlers;
  });

  useEffect(() => {
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryDelay = INITIAL_RETRY_MS;
    let stopped = false;

    function dispatch(type: StreamEventType, event: MessageEvent<string>): void {
      setLastEventAt(Date.now());
      const handler = handlersRef.current[type] as ((data: unknown) => void) | undefined;
      if (handler === undefined) {
        return;
      }
      const data = parseEventData(type, event.data);
      if (data !== undefined) {
        handler(data);
      }
    }

    function scheduleReconnect(): void {
      if (stopped) {
        return;
      }
      setStatus("reconnecting");
      retryTimer = setTimeout(connect, retryDelay);
      retryDelay = nextRetryDelay(retryDelay);
    }

    function connect(): void {
      source = new EventSource(url);
      source.onopen = () => {
        retryDelay = INITIAL_RETRY_MS;
        setStatus("open");
      };
      source.onerror = () => {
        source?.close();
        scheduleReconnect();
      };
      for (const type of STREAM_EVENT_TYPES) {
        source.addEventListener(type, (event) => dispatch(type, event as MessageEvent<string>));
      }
    }

    connect();
    return () => {
      stopped = true;
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
      }
      source?.close();
    };
  }, [url]);

  return { status, lastEventAt };
}
