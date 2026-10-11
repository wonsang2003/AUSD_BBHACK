/**
 * TxLog: every transaction started from /fund in this browser session (faucet, budget open,
 * pause / resume, revoke, voucher config, voucher cancellations), newest first, each with its
 * Basescan link. The hashes come from the backend relayer's responses.
 */

import { TxLink } from "@/components/dashboard/TxLink";
import { clockTime } from "@/lib/format";

export interface TxLogEntry {
  label: string;
  hash: string;
  at: number; // browser ms
}

export function TxLog({ entries, explorerTxUrl }: { entries: TxLogEntry[]; explorerTxUrl?: string }) {
  if (entries.length === 0) {
    return <div className="text-xs text-fg-dim">No transactions yet. Every tx you trigger here appears with its Basescan link.</div>;
  }
  return (
    <ul className="flex flex-col gap-1 text-xs">
      {entries.map((entry) => (
        <li key={`${entry.hash}-${entry.at}`} className="flex items-center gap-2">
          <span className="num text-[11px] text-fg-dim">{clockTime(new Date(entry.at))}</span>
          <span className="min-w-0 flex-1 truncate">{entry.label}</span>
          <TxLink hash={entry.hash} explorerTxUrl={explorerTxUrl} />
        </li>
      ))}
    </ul>
  );
}
