/**
 * AskBox (ASK): ask the desk a question, command-line style ("> IS NVDA'S IMPLIED MOVE TOO CHEAP? <GO>").
 *
 * Data: `POST /api/ask` → `{answer, agents_used, findings, receipts}`. The backend routes the
 * question to the relevant agents, which may BUY research (same caps and VOI gate) before
 * answering; every paid item comes back as a receipt with its tx hash.
 * Questions typed in the top-bar command line arrive through `useDeskCommand()` and are asked
 * once each (the handled id is kept at module level, so a remount never re-sends one).
 */

"use client";

import { useMutation } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";

import { useDeskCommand } from "@/components/terminal/DeskCommand";
import { InlineError } from "@/components/terminal/InlineError";
import { TermButton } from "@/components/terminal/TermButton";
import { ask } from "@/lib/api";

import { ReceiptRows } from "./FindingsWire";

let lastHandledQuestionId = 0;

export function AskBox() {
  const [question, setQuestion] = useState("");
  const { mutate, isPending, error, data, variables } = useMutation({ mutationFn: ask });
  const { pending } = useDeskCommand();

  useEffect(() => {
    if (pending && pending.id !== lastHandledQuestionId && pending.text.trim()) {
      lastHandledQuestionId = pending.id;
      mutate(pending.text.trim());
    }
  }, [pending, mutate]);

  function submit(event: FormEvent): void {
    event.preventDefault();
    const trimmed = question.trim();
    if (trimmed) {
      mutate(trimmed);
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <form onSubmit={submit} className="flex items-center gap-1">
        <span className="font-bold text-amber">&gt;</span>
        <input
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="IS NVDA'S IMPLIED MOVE TOO CHEAP?"
          aria-label="Ask the desk"
          className="t-input flex-1"
        />
        <TermButton type="submit" variant="go" size="sm" busy={isPending} disabled={!question.trim()}>
          &lt;GO&gt;
        </TermButton>
      </form>
      <InlineError error={error} prefix="ASK" className="px-0" />
      {variables && (isPending || data) ? (
        <div className="truncate text-[11px] text-fg-muted" title={variables}>
          Q: {variables}
        </div>
      ) : null}
      {data ? (
        <div className="flex max-h-[220px] flex-col gap-1 overflow-auto border-l-2 border-amber bg-black px-2 py-1">
          <p className="whitespace-pre-wrap text-[12px] leading-snug">{data.answer}</p>
          {(data.agents_used ?? []).length > 0 ? (
            <span className="text-[11px] text-fg-muted">AGENTS: {data.agents_used.join(", ")}</span>
          ) : null}
          <ReceiptRows receipts={data.receipts} />
        </div>
      ) : null}
    </div>
  );
}
