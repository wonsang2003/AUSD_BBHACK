/**
 * VoucherCalendar: the week of daily vouchers ("Maximum safety" mode), one cell per UTC day,
 * with status pending / redeemed / cancelled / failed and the redemption tx.
 *
 * Data: `GET /api/vouchers`. Future days that are still pending can be selected and cancelled:
 * the backend prepares one ERC-3009 `CancelAuthorization` per voucher nonce, the user signs
 * each, the relayer submits them (`flows.ts` → cancelVoucherDays). A cancelled nonce can never
 * be used again, so that day's money can no longer leave the wallet.
 */

"use client";

import type { UseQueryResult } from "@tanstack/react-query";
import { useState } from "react";

import { QueryState } from "@/components/dashboard/QueryState";
import { voucherValue } from "@/components/dashboard/SafetyCard";
import { TxLink } from "@/components/dashboard/TxLink";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { SECONDS_PER_DAY } from "@/lib/constants";
import { utcDayIndex } from "@/lib/eip712";
import { formatAusd } from "@/lib/format";
import type { Voucher } from "@/lib/types";
import { cx, type Tone } from "@/lib/ui";

import { cancelVoucherDays } from "./flows";
import { FlowStatus } from "./FlowStatus";
import { useFlowRunner } from "./useFlowRunner";
import type { SignFn } from "./useTypedDataSigner";

interface VoucherCalendarProps {
  owner: string | undefined;
  query: UseQueryResult<Voucher[]>;
  sign: SignFn;
  explorerTxUrl?: string;
  onTx: (label: string, hash: string) => void;
  onChanged: () => void;
}

const STATUS_TONE: Record<string, Tone> = { pending: "info", redeemed: "up", cancelled: "neutral", failed: "down", expired: "neutral" };

/** UTC day number → "Mon 12 Oct". */
function dayLabel(dayIndex: number): string {
  return new Date(dayIndex * SECONDS_PER_DAY * 1000).toLocaleDateString("en-GB", {
    weekday: "short",
    day: "2-digit",
    month: "short",
    timeZone: "UTC",
  });
}

export function VoucherCalendar({ owner, query, sign, explorerTxUrl, onTx, onChanged }: VoucherCalendarProps) {
  const [selected, setSelected] = useState<number[]>([]);
  const runner = useFlowRunner();
  const today = utcDayIndex(Math.floor(Date.now() / 1000));

  const toggle = (day: number) =>
    setSelected((current) => (current.includes(day) ? current.filter((d) => d !== day) : [...current, day]));

  async function cancelSelected(): Promise<void> {
    if (!owner || selected.length === 0) {
      return;
    }
    const hashes = await runner.run((progress) => cancelVoucherDays(owner, selected, sign, progress));
    if (hashes) {
      hashes.forEach((hash) => onTx("Voucher cancelled (CancelAuthorization)", hash));
      setSelected([]);
      onChanged();
    }
  }

  return (
    <QueryState query={query} isEmpty={(rows) => rows.length === 0} emptyText="No vouchers signed yet.">
      {(rows) => (
        <div className="flex flex-col gap-2">
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4 xl:grid-cols-7">
            {[...rows]
              .sort((a, b) => a.day_index - b.day_index)
              .map((voucher) => {
                const cancellable = voucher.status === "pending" && voucher.day_index > today;
                return (
                  <label
                    key={voucher.day_index}
                    className={cx(
                      "flex flex-col gap-0.5 rounded-sm border p-1.5 text-[11px]",
                      voucher.day_index === today ? "border-info" : "border-line",
                      selected.includes(voucher.day_index) && "bg-down/10",
                    )}
                  >
                    <span className="flex items-center justify-between gap-1">
                      <span className="font-semibold">{dayLabel(voucher.day_index)}</span>
                      {cancellable && (
                        <input
                          type="checkbox"
                          checked={selected.includes(voucher.day_index)}
                          onChange={() => toggle(voucher.day_index)}
                          aria-label={`select ${dayLabel(voucher.day_index)} for cancellation`}
                        />
                      )}
                    </span>
                    <span className="num">{formatAusd(voucherValue(voucher))}</span>
                    <Badge tone={STATUS_TONE[voucher.status] ?? "neutral"} className="self-start">
                      {voucher.status}
                    </Badge>
                    {voucher.tx_hash && <TxLink hash={voucher.tx_hash} explorerTxUrl={explorerTxUrl} />}
                  </label>
                );
              })}
          </div>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="danger"
              busy={runner.busy}
              disabled={selected.length === 0}
              onClick={() => void cancelSelected()}
            >
              Cancel future days ({selected.length})
            </Button>
            <span className="text-[10px] text-fg-dim">only future, still-pending days can be cancelled</span>
          </div>
          <FlowStatus runner={runner} />
        </div>
      )}
    </QueryState>
  );
}
