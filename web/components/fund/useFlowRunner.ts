/**
 * useFlowRunner: runs one signing flow from `flows.ts` at a time and tracks its progress.
 *
 * State: `busy` while the flow runs, `step` = the current progress message ("Wallet: sign
 * voucher 3/7"), `error` = why the last run failed (wallet rejection, API `{detail}`, …).
 */

"use client";

import { useCallback, useState } from "react";

import { errorText } from "@/components/dashboard/QueryState";

import type { ProgressFn } from "./flows";

export interface FlowRunner {
  busy: boolean;
  step: string | null;
  error: string | null;
  run: <T>(flow: (progress: ProgressFn) => Promise<T>) => Promise<T | undefined>;
}

export function useFlowRunner(): FlowRunner {
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async <T,>(flow: (progress: ProgressFn) => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      const result = await flow(setStep);
      setStep(null);
      return result;
    } catch (caught) {
      setStep(null);
      setError(errorText(caught));
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);

  return { busy, step, error, run };
}
