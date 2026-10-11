/**
 * AskBox: ask the desk a question ("Should I worry about NVDA into earnings?").
 *
 * Data: `POST /api/ask` → `{answer, agents_used, findings, receipts}`. The backend routes the
 * question to the relevant agents, which may BUY research (paid from the budget, same caps and
 * VOI gate) before answering; every paid item comes back as a receipt with its Basescan tx.
 */

"use client";

import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/Button";
import { ask } from "@/lib/api";

import { ReceiptList } from "./FindingsFeed";
import { errorText } from "./QueryState";

interface AskBoxProps {
  explorerTxUrl?: string;
}

export function AskBox({ explorerTxUrl }: AskBoxProps) {
  const [question, setQuestion] = useState("");
  const mutation = useMutation({ mutationFn: ask });

  function submit(event: FormEvent): void {
    event.preventDefault();
    const trimmed = question.trim();
    if (trimmed) {
      mutation.mutate(trimmed);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <form onSubmit={submit} className="flex gap-1.5">
        <input
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="e.g. Is NVDA's implied move too cheap?"
          className="h-8 min-w-0 flex-1 rounded-sm border border-line bg-bg px-2 text-xs outline-none focus:border-info"
        />
        <Button type="submit" variant="primary" busy={mutation.isPending} disabled={!question.trim()}>
          Ask
        </Button>
      </form>
      {mutation.isError && <span className="text-[11px] text-down">{errorText(mutation.error)}</span>}
      {mutation.data && (
        <div className="flex flex-col gap-1.5 rounded-sm border border-line bg-bg p-2">
          <p className="whitespace-pre-wrap text-xs text-fg">{mutation.data.answer}</p>
          {mutation.data.agents_used.length > 0 && (
            <span className="text-[11px] text-fg-dim">agents: {mutation.data.agents_used.join(", ")}</span>
          )}
          <ReceiptList receipts={mutation.data.receipts} explorerTxUrl={explorerTxUrl} />
        </div>
      )}
    </div>
  );
}
