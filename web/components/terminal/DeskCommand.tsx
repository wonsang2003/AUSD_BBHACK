/**
 * DeskCommand: hands a question typed in the top-bar command line to the ASK THE DESK panel.
 *
 * The command line calls `askDesk(text)` (and navigates to the DESK if needed); the panel reads
 * `pending` ({id, text}) and runs it once, using `id` so a question is never sent twice (React
 * may run effects twice in development).
 */

"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export interface PendingQuestion {
  id: number;
  text: string;
}

interface DeskCommandValue {
  pending: PendingQuestion | null;
  askDesk: (text: string) => void;
}

const DeskCommandContext = createContext<DeskCommandValue>({ pending: null, askDesk: () => undefined });

export function DeskCommandProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<PendingQuestion | null>(null);
  const askDesk = useCallback((text: string) => {
    setPending((previous) => ({ id: (previous?.id ?? 0) + 1, text }));
  }, []);
  const value = useMemo(() => ({ pending, askDesk }), [pending, askDesk]);
  return <DeskCommandContext.Provider value={value}>{children}</DeskCommandContext.Provider>;
}

export function useDeskCommand(): DeskCommandValue {
  return useContext(DeskCommandContext);
}
