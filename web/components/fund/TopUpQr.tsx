/**
 * TopUpQr: a QR code to send AUSD to the connected wallet from a phone wallet.
 *
 * The QR encodes an EIP-681 payment request for an ERC-20 transfer on Base Sepolia:
 *   ethereum:<AUSD token>@84532/transfer?address=<your wallet>&uint256=<amount in atomic units>
 * Data: the connected address (wagmi) and the AUSD address from `/api/config` (or the default).
 */

"use client";

import { QRCodeSVG } from "qrcode.react";
import { useState } from "react";

import { BASE_SEPOLIA_CHAIN_ID } from "@/lib/constants";
import { shortAddress } from "@/lib/format";

import { ausdToAtomic } from "./budgetFormModel";

const QR_SIZE_PX = 132;

/** EIP-681 URI for an AUSD transfer of `amountAtomic` to `recipient`. */
export function eip681TransferUri(ausdAddress: string, recipient: string, amountAtomic: number): string {
  return `ethereum:${ausdAddress}@${BASE_SEPOLIA_CHAIN_ID}/transfer?address=${recipient}&uint256=${amountAtomic}`;
}

export function TopUpQr({ ausdAddress, recipient }: { ausdAddress: string; recipient: string | undefined }) {
  const [amount, setAmount] = useState("10");
  if (!recipient) {
    return <div className="text-xs text-fg-dim">Connect a wallet to get a top-up QR code.</div>;
  }
  const atomic = ausdToAtomic(amount) ?? 0;
  const uri = eip681TransferUri(ausdAddress, recipient, atomic);
  return (
    <div className="flex flex-wrap items-start gap-3">
      <div className="rounded-sm bg-white p-1.5">
        <QRCodeSVG value={uri} size={QR_SIZE_PX} />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5 text-xs">
        <label className="flex items-center gap-2">
          <span className="text-fg-muted">Amount</span>
          <input
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
            className="num h-7 w-24 rounded-sm border border-line bg-bg px-2 text-right outline-none focus:border-info"
          />
          <span className="text-fg-dim">AUSD</span>
        </label>
        <span className="text-fg-muted">Scan with a phone wallet to send AUSD to {shortAddress(recipient)}.</span>
        <code className="num break-all text-[10px] text-fg-dim">{uri}</code>
      </div>
    </div>
  );
}
