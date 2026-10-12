/**
 * InlineError / MutedLine: one-line messages inside a panel.
 *
 *   <InlineError error={mutation.error} prefix="START SESSION" />
 *     → red line "ERR START SESSION: <backend detail>"; renders nothing when `error` is empty.
 *   <MutedLine>No purchases yet — start a session.</MutedLine>
 *     → the standard empty / loading state: one muted line, never a giant empty card.
 *
 * `errorText(error)` turns anything thrown (ApiError with the backend's `detail`, a viem/wagmi
 * error with `shortMessage`, a plain Error or string) into a readable sentence.
 */

import type { ReactNode } from "react";

import { ApiError } from "@/lib/api";

import { cls } from "./tones";

/** Human-readable reason for a failed request or wallet action. */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail;
  }
  if (error && typeof error === "object" && "shortMessage" in error && typeof error.shortMessage === "string") {
    return error.shortMessage;
  }
  if (error instanceof TypeError && /fetch|network|load failed/i.test(error.message)) {
    return "backend unreachable";
  }
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === "string") {
    return error;
  }
  return "unknown error";
}

interface InlineErrorProps {
  /** The caught error (or a message). Null / undefined / false renders nothing. */
  error: unknown;
  /** Short label of what failed, e.g. "STATE" or "DEMO FUND". */
  prefix?: string;
  className?: string;
}

export function InlineError({ error, prefix, className }: InlineErrorProps) {
  if (error === null || error === undefined || error === false || error === "") {
    return null;
  }
  return (
    <div role="alert" className={cls("px-2 py-1 text-[12px] leading-snug text-down", className)}>
      <span className="font-bold">ERR</span>
      {prefix ? <span className="font-bold"> {prefix}:</span> : null} <span>{errorText(error)}</span>
    </div>
  );
}

interface MutedLineProps {
  children: ReactNode;
  className?: string;
}

export function MutedLine({ children, className }: MutedLineProps) {
  return <div className={cls("px-2 py-1.5 text-[12px] text-fg-muted", className)}>{children}</div>;
}
