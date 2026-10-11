/**
 * TxHash: a transaction hash shortened to 0x1234…abcd in cyan.
 *
 * On Base Sepolia it links to Basescan (explorer URL from /api/config). On the local demo chain
 * (see `useChainInfo`) there is no explorer, so it renders plain text plus a "LOCAL" tag; clicking
 * it copies the full hash. A missing hash renders "—". The full hash is always in the tooltip.
 *
 * Props:
 *   hash      the tx hash (null / undefined → "—")
 *   local     force local (no link) or explorer mode; default comes from useChainInfo()
 *   showTag   show the "LOCAL" tag on a local chain (default true; tables with many rows may hide it)
 */

"use client";

import { EMPTY, shortHex } from "./format";
import { useChainInfo } from "./useChainInfo";

interface TxHashProps {
  hash: string | null | undefined;
  local?: boolean;
  showTag?: boolean;
}

function copyToClipboard(text: string): void {
  try {
    void navigator.clipboard?.writeText(text).catch(() => undefined);
  } catch {
    // clipboard unavailable (insecure context): the tooltip still shows the full hash
  }
}

export function TxHash({ hash, local, showTag = true }: TxHashProps) {
  const chain = useChainInfo();
  if (!hash) {
    return <span className="text-fg-muted">{EMPTY}</span>;
  }
  const isLocal = local ?? chain.isLocal;
  const short = shortHex(hash, 4);
  if (isLocal) {
    return (
      <span className="inline-flex items-center gap-1">
        <button
          type="button"
          className="num cursor-copy text-info"
          title={`${hash}\nLocal chain: no explorer. Click to copy.`}
          onClick={() => copyToClipboard(hash)}
        >
          {short}
        </button>
        {showTag ? <span className="text-[10px] font-bold text-fg-muted">LOCAL</span> : null}
      </span>
    );
  }
  return (
    <a
      href={`${chain.explorerTxUrl}${hash}`}
      target="_blank"
      rel="noopener noreferrer"
      title={`${hash}\nOpen on Basescan`}
      className="num text-info hover:underline"
    >
      {short}
    </a>
  );
}
