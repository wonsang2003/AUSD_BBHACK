/**
 * TxLink: a short tx hash linking to Basescan (Base Sepolia). Shared by every page that shows a
 * transaction (purchase feed, findings receipts, /fund results, session recharge/sweep).
 *
 * Data: just the hash; the explorer prefix defaults to Basescan Sepolia (`lib/constants.ts`),
 * or pass `explorerTxUrl` from `GET /api/config`. A missing hash renders an em dash.
 */

import { EMPTY, shortHash, txUrl } from "@/lib/format";
import { cx } from "@/lib/ui";

interface TxLinkProps {
  hash: string | null | undefined;
  explorerTxUrl?: string;
  label?: string;
  className?: string;
}

export function TxLink({ hash, explorerTxUrl, label, className }: TxLinkProps) {
  if (!hash) {
    return <span className="text-fg-dim">{EMPTY}</span>;
  }
  return (
    <a
      href={txUrl(hash, explorerTxUrl)}
      target="_blank"
      rel="noopener noreferrer"
      title={`View ${hash} on Basescan`}
      className={cx("num text-info underline-offset-2 hover:underline", className)}
    >
      {label ?? shortHash(hash)} ↗
    </a>
  );
}
