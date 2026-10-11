/**
 * The /fund signing flows, as plain async functions: prepare (backend) → sign (wallet) → submit
 * (backend relayer, which pays the gas). Each step reports progress so the page can say which
 * signature the wallet is asking for right now.
 *
 *   openPermitBudget   "Flexible":        POST /api/budget/prepare → sign permit + config → POST /api/budget/open
 *   openVoucherBudget  "Maximum safety":  POST /api/vouchers/prepare → sign 7 vouchers + config → POST /api/vouchers/submit
 *   setBudgetPaused    POST /api/budget/prepare-pause → sign SetPaused → POST /api/budget/pause
 *   revokeBudget       POST /api/budget/prepare-revoke → sign permit(value 0) → POST /api/budget/revoke
 *   cancelVoucherDays  POST /api/vouchers/prepare-cancel → sign each CancelAuthorization → POST /api/vouchers/cancel
 *
 * Messages are always signed exactly as the backend built them (never rebuilt here).
 */

import {
  cancelVouchers,
  openBudget,
  pauseBudget,
  prepareBudget,
  prepareCancelVouchers,
  preparePause,
  prepareRevoke,
  prepareVouchers,
  revokeBudget as submitRevoke,
  submitVouchers,
} from "@/lib/api";
import { utcDayIndex } from "@/lib/eip712";
import type {
  BudgetPrepareRequest,
  SignedVoucher,
  TxHash,
  TypedDataJson,
  VoucherCancelResponse,
  VoucherSubmitResponse,
} from "@/lib/types";

import type { SignFn } from "./useTypedDataSigner";

export type ProgressFn = (step: string) => void;

/** An integer field of a prepared message, passed back to the API as-is (number or decimal string). */
function integerField(message: Record<string, unknown>, name: string): string | number {
  const value = message[name];
  if (typeof value === "number" || typeof value === "string") {
    return value;
  }
  if (typeof value === "bigint") {
    return value.toString();
  }
  throw new Error(`prepared message has no integer field "${name}"`);
}

function stringField(message: Record<string, unknown>, name: string): string {
  const value = message[name];
  if (typeof value !== "string") {
    throw new Error(`prepared message has no string field "${name}"`);
  }
  return value;
}

export async function openPermitBudget(
  request: BudgetPrepareRequest,
  sign: SignFn,
  progress: ProgressFn,
): Promise<TxHash> {
  progress("Preparing permit + budget config…");
  const prepared = await prepareBudget(request);
  progress("Wallet: sign 1/2 — AUSD permit (allowance for the BudgetController)");
  const permitSignature = await sign(prepared.permit);
  progress("Wallet: sign 2/2 — budget config (daily cap, agents, per-agent caps)");
  const configSignature = await sign(prepared.config);
  progress("Submitting — the relayer pays the gas…");
  const { tx_hash } = await openBudget({
    owner: request.owner,
    permit: {
      value: integerField(prepared.permit.message, "value"),
      deadline: integerField(prepared.permit.message, "deadline"),
      signature: permitSignature,
    },
    config: prepared.config.message,
    config_signature: configSignature,
  });
  return tx_hash;
}

/** One signed voucher in the submit shape. day_index = the UTC day the window opens on. */
export function toSignedVoucher(voucher: TypedDataJson, signature: string): SignedVoucher {
  const message = voucher.message;
  const validAfter = integerField(message, "validAfter");
  return {
    day_index: utcDayIndex(Number(validAfter) + 1),
    value: integerField(message, "value"),
    valid_after: validAfter,
    valid_before: integerField(message, "validBefore"),
    nonce: stringField(message, "nonce"),
    signature,
  };
}

export async function openVoucherBudget(
  owner: string,
  dailyValuesAtomic: number[],
  sign: SignFn,
  progress: ProgressFn,
): Promise<VoucherSubmitResponse> {
  progress("Preparing 7 daily vouchers + budget config…");
  const prepared = await prepareVouchers({ owner, daily_values_atomic: dailyValuesAtomic });
  const signed: SignedVoucher[] = [];
  for (const [index, voucher] of prepared.vouchers.entries()) {
    progress(`Wallet: sign voucher ${index + 1}/${prepared.vouchers.length}`);
    signed.push(toSignedVoucher(voucher, await sign(voucher)));
  }
  progress("Wallet: sign the budget config (agents + per-agent caps)");
  const configSignature = await sign(prepared.config);
  progress("Storing vouchers — redeemed one per day by the keeper…");
  return submitVouchers({ owner, vouchers: signed, config: prepared.config.message, config_signature: configSignature });
}

export async function setBudgetPaused(owner: string, paused: boolean, sign: SignFn, progress: ProgressFn): Promise<TxHash> {
  progress(paused ? "Preparing pause…" : "Preparing resume…");
  const { typed_data } = await preparePause(owner, paused);
  progress("Wallet: sign SetPaused");
  const signature = await sign(typed_data);
  progress("Submitting — the relayer pays the gas…");
  const { tx_hash } = await pauseBudget({ owner, paused, deadline: integerField(typed_data.message, "deadline"), signature });
  return tx_hash;
}

export async function revokeBudget(owner: string, sign: SignFn, progress: ProgressFn): Promise<TxHash> {
  progress("Preparing revoke (permit with value 0)…");
  const { typed_data } = await prepareRevoke(owner);
  progress("Wallet: sign the revoke permit");
  const signature = await sign(typed_data);
  progress("Submitting — the relayer pays the gas…");
  const { tx_hash } = await submitRevoke({ owner, deadline: integerField(typed_data.message, "deadline"), signature });
  return tx_hash;
}

export async function cancelVoucherDays(
  owner: string,
  dayIndexes: number[],
  sign: SignFn,
  progress: ProgressFn,
): Promise<TxHash[]> {
  progress("Preparing cancellations…");
  const { typed_data } = await prepareCancelVouchers({ owner, day_indexes: dayIndexes });
  const cancels: Array<{ nonce: string; signature: string }> = [];
  for (const [index, message] of typed_data.entries()) {
    progress(`Wallet: sign cancellation ${index + 1}/${typed_data.length}`);
    cancels.push({ nonce: stringField(message.message, "nonce"), signature: await sign(message) });
  }
  progress("Submitting cancellations — the relayer pays the gas…");
  const response: VoucherCancelResponse = await cancelVouchers({ owner, cancels });
  return response.tx_hashes ?? (response.tx_hash ? [response.tx_hash] : []);
}
