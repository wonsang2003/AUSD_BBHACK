/**
 * EIP-712 typed-data builders for every signature the PocketDesk web app asks a user for.
 *
 * Mirror of `backend/app/chain/eip712.py`. The signatures a user makes in the browser:
 *
 *   1. ERC-2612 `Permit`                    — funds the budget (and value 0 = revoke)
 *   2. PocketDesk `BudgetConfig`            — daily cap, expiry, agents and per-agent caps
 *   3. PocketDesk `SetPaused`               — gasless pause / resume
 *   4. ERC-3009 `ReceiveWithAuthorization`  — a daily voucher ("Maximum safety" mode, Update #1 D)
 *   5. ERC-3009 `CancelAuthorization`       — cancels a future voucher
 * (`TransferWithAuthorization` is mirrored too: agents sign it server-side for x402 payments.)
 *
 * Normally the backend builds these messages (`POST /api/budget/prepare`, …) and the web only
 * shows and signs them unchanged. The builders here exist so the UI can rebuild a message and
 * check it against what the backend sent, and so `npm run test:eip712` can pin the digests
 * against the Python side.
 *
 * Two shapes are used:
 *   * `FullMessage` — the JSON "full message" `{types, primaryType, domain, message}` exactly as
 *     the backend sends it: `types` includes `EIP712Domain`, integers are JSON numbers or decimal
 *     strings. The builders below return this shape (integers as decimal strings, lossless).
 *   * `ViemTypedData` — what viem/wagmi `signTypedData` and `hashTypedData` take: integers as
 *     `bigint`, no `EIP712Domain` in `types`. `toViemTypedData()` converts one into the other.
 */

import {
  encodeAbiParameters,
  getAddress,
  hashTypedData,
  keccak256,
  toHex,
  type Hex,
  type TypedDataDomain,
  type TypedDataParameter,
} from "viem";

import {
  AUSD_EIP712_NAME,
  AUSD_EIP712_VERSION,
  ERC3009_VALID_AFTER_SKEW_SECONDS,
  POCKETDESK_EIP712_NAME,
  POCKETDESK_EIP712_VERSION,
  SECONDS_PER_DAY,
} from "./constants";

// ─── shapes ──────────────────────────────────────────────────────────────────

/** An integer as the API may send it: a JS number, a decimal string, or a bigint. */
export type IntegerLike = bigint | number | string;

export interface TypedField {
  name: string;
  type: string;
}

export type TypedDataTypes = Record<string, readonly TypedField[]>;

export interface DomainJson {
  name: string;
  version: string;
  chainId: IntegerLike;
  verifyingContract: string;
}

/** The backend's EIP-712 "full message" (see the module docstring). */
export interface FullMessage {
  types: TypedDataTypes;
  primaryType: string;
  domain: DomainJson;
  message: Record<string, unknown>;
}

/** The viem/wagmi `signTypedData` / `hashTypedData` parameter shape. */
export interface ViemTypedData {
  domain: TypedDataDomain;
  types: Record<string, readonly TypedDataParameter[]>;
  primaryType: string;
  message: Record<string, unknown>;
}

// ─── type definitions (field order matters: it is part of the type hash) ──────

export const EIP712_DOMAIN_TYPE: readonly TypedField[] = [
  { name: "name", type: "string" },
  { name: "version", type: "string" },
  { name: "chainId", type: "uint256" },
  { name: "verifyingContract", type: "address" },
];

export const PERMIT_TYPE: readonly TypedField[] = [
  { name: "owner", type: "address" },
  { name: "spender", type: "address" },
  { name: "value", type: "uint256" },
  { name: "nonce", type: "uint256" },
  { name: "deadline", type: "uint256" },
];

export const ERC3009_AUTHORIZATION_FIELDS: readonly TypedField[] = [
  { name: "from", type: "address" },
  { name: "to", type: "address" },
  { name: "value", type: "uint256" },
  { name: "validAfter", type: "uint256" },
  { name: "validBefore", type: "uint256" },
  { name: "nonce", type: "bytes32" },
];

export const CANCEL_AUTHORIZATION_TYPE: readonly TypedField[] = [
  { name: "authorizer", type: "address" },
  { name: "nonce", type: "bytes32" },
];

export const BUDGET_CONFIG_TYPE: readonly TypedField[] = [
  { name: "owner", type: "address" },
  { name: "dailyCap", type: "uint128" },
  { name: "expiry", type: "uint64" },
  { name: "agentAddrs", type: "address[]" },
  { name: "agentCaps", type: "uint128[]" },
  { name: "sectors", type: "bytes32[]" },
  { name: "nonce", type: "uint256" },
];

export const SET_PAUSED_TYPE: readonly TypedField[] = [
  { name: "owner", type: "address" },
  { name: "paused", type: "bool" },
  { name: "nonce", type: "uint256" },
  { name: "deadline", type: "uint256" },
];

const EIP712_DOMAIN_TYPE_NAME = "EIP712Domain";
const BYTES32_LENGTH = 32;

// ─── domains ─────────────────────────────────────────────────────────────────

/** Signing domain of the AUSD token. Note: name is "Agora Dollar", not "AUSD". */
export function ausdDomain(chainId: number, ausdAddress: string): DomainJson {
  return {
    name: AUSD_EIP712_NAME,
    version: AUSD_EIP712_VERSION,
    chainId,
    verifyingContract: getAddress(ausdAddress),
  };
}

/** Signing domain of our BudgetController contract. */
export function pocketdeskDomain(chainId: number, controllerAddress: string): DomainJson {
  return {
    name: POCKETDESK_EIP712_NAME,
    version: POCKETDESK_EIP712_VERSION,
    chainId,
    verifyingContract: getAddress(controllerAddress),
  };
}

function fullMessage(
  primaryType: string,
  typeFields: readonly TypedField[],
  domain: DomainJson,
  message: Record<string, unknown>,
): FullMessage {
  return {
    types: { [EIP712_DOMAIN_TYPE_NAME]: EIP712_DOMAIN_TYPE, [primaryType]: typeFields },
    primaryType,
    domain,
    message,
  };
}

/** Lossless JSON form of an integer: a decimal string. Throws on fractions / garbage. */
function decimalString(value: IntegerLike): string {
  return BigInt(value).toString();
}

// ─── ERC-2612 permit ─────────────────────────────────────────────────────────

export interface PermitParams {
  chainId: number;
  ausdAddress: string;
  owner: string;
  spender: string;
  value: IntegerLike;
  nonce: IntegerLike;
  deadline: IntegerLike;
}

/** ERC-2612 permit. Read `nonces(owner)` from the token right before building it. Value 0 = revoke. */
export function buildPermit(params: PermitParams): FullMessage {
  const message = {
    owner: getAddress(params.owner),
    spender: getAddress(params.spender),
    value: decimalString(params.value),
    nonce: decimalString(params.nonce),
    deadline: decimalString(params.deadline),
  };
  return fullMessage("Permit", PERMIT_TYPE, ausdDomain(params.chainId, params.ausdAddress), message);
}

// ─── ERC-3009 authorizations ─────────────────────────────────────────────────

/** The six fields of an ERC-3009 authorization (also the x402 `payload.authorization`). */
export interface Authorization {
  from: string;
  to: string;
  value: IntegerLike;
  validAfter: IntegerLike;
  validBefore: IntegerLike;
  nonce: Hex; // 0x-prefixed 32-byte hex string
}

/** The EIP-712 message of an authorization (checksummed addresses, integers as decimal strings). */
export function authorizationMessage(auth: Authorization): Record<string, unknown> {
  return {
    from: getAddress(auth.from),
    to: getAddress(auth.to),
    value: decimalString(auth.value),
    validAfter: decimalString(auth.validAfter),
    validBefore: decimalString(auth.validBefore),
    nonce: auth.nonce,
  };
}

/** ERC-3009 nonces are random bytes32 (not sequential), so payments can run in parallel. */
export function randomNonce(): Hex {
  const bytes = new Uint8Array(BYTES32_LENGTH);
  globalThis.crypto.getRandomValues(bytes);
  return toHex(bytes);
}

export interface NewAuthorizationParams {
  from: string;
  to: string;
  value: IntegerLike;
  timeoutSeconds: number;
  nowSeconds?: number;
  nonce?: Hex;
}

/** A fresh authorization valid from (now − 30 s) to (now + timeoutSeconds). */
export function newAuthorization(params: NewAuthorizationParams): Authorization {
  const now = params.nowSeconds ?? Math.floor(Date.now() / 1000);
  return {
    from: getAddress(params.from),
    to: getAddress(params.to),
    value: BigInt(params.value),
    validAfter: BigInt(now - ERC3009_VALID_AFTER_SKEW_SECONDS),
    validBefore: BigInt(now + params.timeoutSeconds),
    nonce: params.nonce ?? randomNonce(),
  };
}

export interface AuthorizationParams {
  chainId: number;
  ausdAddress: string;
  auth: Authorization;
}

/** ERC-3009 TransferWithAuthorization — anyone (relayer / seller) may submit it. */
export function buildTransferWithAuthorization(params: AuthorizationParams): FullMessage {
  return fullMessage(
    "TransferWithAuthorization",
    ERC3009_AUTHORIZATION_FIELDS,
    ausdDomain(params.chainId, params.ausdAddress),
    authorizationMessage(params.auth),
  );
}

/** ERC-3009 ReceiveWithAuthorization — only `to` may submit it (used for vouchers / escrow). */
export function buildReceiveWithAuthorization(params: AuthorizationParams): FullMessage {
  return fullMessage(
    "ReceiveWithAuthorization",
    ERC3009_AUTHORIZATION_FIELDS,
    ausdDomain(params.chainId, params.ausdAddress),
    authorizationMessage(params.auth),
  );
}

export interface CancelAuthorizationParams {
  chainId: number;
  ausdAddress: string;
  authorizer: string;
  nonce: Hex;
}

/** ERC-3009 CancelAuthorization — makes an unused nonce permanently unusable. */
export function buildCancelAuthorization(params: CancelAuthorizationParams): FullMessage {
  const message = { authorizer: getAddress(params.authorizer), nonce: params.nonce };
  return fullMessage(
    "CancelAuthorization",
    CANCEL_AUTHORIZATION_TYPE,
    ausdDomain(params.chainId, params.ausdAddress),
    message,
  );
}

// ─── daily vouchers (Update #1 D, "Maximum safety" mode) ─────────────────────

export const VOUCHER_NONCE_TAG = "pocketdesk-voucher";

/** UTC day number used by the contract: floor(unixSeconds / 86400). */
export function utcDayIndex(unixSeconds: number): number {
  return Math.floor(unixSeconds / SECONDS_PER_DAY);
}

/**
 * Validity window of the voucher for one UTC day.
 * Rule: validAfter = dayStart − 1, validBefore = dayEnd (= next dayStart), so with ERC-3009's
 * strict `validAfter < now < validBefore` the voucher is usable exactly during that day.
 */
export function voucherWindow(dayIndex: IntegerLike): { validAfter: bigint; validBefore: bigint } {
  const dayStart = BigInt(dayIndex) * BigInt(SECONDS_PER_DAY);
  return { validAfter: dayStart - BigInt(1), validBefore: dayStart + BigInt(SECONDS_PER_DAY) };
}

/**
 * Deterministic voucher nonce.
 * Formula: keccak256(abi.encode("pocketdesk-voucher", owner, dayIndex, salt)), salt a bytes32.
 */
export function voucherNonce(owner: string, dayIndex: IntegerLike, salt: Hex): Hex {
  const encoded = encodeAbiParameters(
    [{ type: "string" }, { type: "address" }, { type: "uint256" }, { type: "bytes32" }],
    [VOUCHER_NONCE_TAG, getAddress(owner), BigInt(dayIndex), salt],
  );
  return keccak256(encoded);
}

export interface DailyVoucherParams {
  chainId: number;
  ausdAddress: string;
  owner: string;
  controllerAddress: string;
  value: IntegerLike;
  dayIndex: IntegerLike;
  salt: Hex;
}

/** One day's voucher: a ReceiveWithAuthorization from the owner to the BudgetController. */
export function buildDailyVoucher(params: DailyVoucherParams): FullMessage {
  const window = voucherWindow(params.dayIndex);
  const auth: Authorization = {
    from: params.owner,
    to: params.controllerAddress,
    value: params.value,
    validAfter: window.validAfter,
    validBefore: window.validBefore,
    nonce: voucherNonce(params.owner, params.dayIndex, params.salt),
  };
  return buildReceiveWithAuthorization({ chainId: params.chainId, ausdAddress: params.ausdAddress, auth });
}

// ─── PocketDesk BudgetConfig / SetPaused ─────────────────────────────────────

/** Encode a short sector label (e.g. "OptionsEvent") as a right-padded bytes32 hex string. */
export function sectorToBytes32(sector: string): Hex {
  const raw = new TextEncoder().encode(sector);
  if (raw.length > BYTES32_LENGTH) {
    throw new Error(`sector label longer than 32 bytes: ${sector}`);
  }
  const hexDigits = toHex(raw).slice(2);
  return `0x${hexDigits.padEnd(BYTES32_LENGTH * 2, "0")}`;
}

/** Inverse of sectorToBytes32: drop the zero padding and decode UTF-8. */
export function bytes32ToSector(value: Hex): string {
  const hexDigits = value.slice(2);
  const bytes: number[] = [];
  for (let index = 0; index < hexDigits.length; index += 2) {
    bytes.push(parseInt(hexDigits.slice(index, index + 2), 16));
  }
  while (bytes.length > 0 && bytes[bytes.length - 1] === 0) {
    bytes.pop();
  }
  return new TextDecoder().decode(new Uint8Array(bytes));
}

/** Mirror of the Solidity `BudgetConfig` struct. */
export interface BudgetConfig {
  owner: string;
  dailyCap: IntegerLike;
  expiry: IntegerLike;
  agentAddrs: string[];
  agentCaps: IntegerLike[];
  sectors: Hex[]; // bytes32 hex strings (see sectorToBytes32)
  nonce: IntegerLike;
}

export function budgetConfigMessage(config: BudgetConfig): Record<string, unknown> {
  return {
    owner: getAddress(config.owner),
    dailyCap: decimalString(config.dailyCap),
    expiry: decimalString(config.expiry),
    agentAddrs: config.agentAddrs.map((address) => getAddress(address)),
    agentCaps: config.agentCaps.map((cap) => decimalString(cap)),
    sectors: [...config.sectors],
    nonce: decimalString(config.nonce),
  };
}

export interface BudgetConfigParams {
  chainId: number;
  controllerAddress: string;
  config: BudgetConfig;
}

export function buildBudgetConfig(params: BudgetConfigParams): FullMessage {
  return fullMessage(
    "BudgetConfig",
    BUDGET_CONFIG_TYPE,
    pocketdeskDomain(params.chainId, params.controllerAddress),
    budgetConfigMessage(params.config),
  );
}

export interface SetPausedParams {
  chainId: number;
  controllerAddress: string;
  owner: string;
  paused: boolean;
  nonce: IntegerLike;
  deadline: IntegerLike;
}

/** Gasless pause/resume: the user signs, the keeper submits `setPausedWithSig`. */
export function buildSetPaused(params: SetPausedParams): FullMessage {
  const message = {
    owner: getAddress(params.owner),
    paused: params.paused,
    nonce: decimalString(params.nonce),
    deadline: decimalString(params.deadline),
  };
  return fullMessage("SetPaused", SET_PAUSED_TYPE, pocketdeskDomain(params.chainId, params.controllerAddress), message);
}

// ─── backend JSON → viem ─────────────────────────────────────────────────────

const INTEGER_TYPE = /^u?int(\d+)?$/; // int, int8 … uint256
const ARRAY_TYPE = /^(.+)\[\d*\]$/;

/**
 * Convert a backend full message into viem's `signTypedData` shape.
 * Rules: drop `EIP712Domain` from `types` (viem derives it from `domain`); every `intN`/`uintN`
 * value (numbers or decimal strings, also inside arrays and nested structs) becomes a bigint;
 * `bool` accepts true/false or "true"/"false"; everything else passes through unchanged.
 */
export function toViemTypedData(full: FullMessage): ViemTypedData {
  const types = withoutDomainType(full.types);
  return {
    domain: toViemDomain(full.domain),
    types,
    primaryType: full.primaryType,
    message: convertStruct(full.primaryType, full.message, types),
  };
}

/** The 32-byte EIP-712 digest ("\x19\x01" ‖ domainSeparator ‖ structHash), same as Python's `typed_data_digest`. */
export function typedDataDigest(full: FullMessage): Hex {
  return hashTypedData(toViemTypedData(full));
}

function withoutDomainType(types: TypedDataTypes): Record<string, readonly TypedDataParameter[]> {
  const result: Record<string, readonly TypedDataParameter[]> = {};
  for (const [typeName, fields] of Object.entries(types)) {
    if (typeName !== EIP712_DOMAIN_TYPE_NAME) {
      result[typeName] = fields;
    }
  }
  return result;
}

function toViemDomain(domain: DomainJson): TypedDataDomain {
  return {
    name: domain.name,
    version: domain.version,
    chainId: Number(domain.chainId),
    verifyingContract: getAddress(domain.verifyingContract),
  };
}

function convertStruct(
  typeName: string,
  value: Record<string, unknown>,
  types: Record<string, readonly TypedDataParameter[]>,
): Record<string, unknown> {
  const fields = types[typeName];
  if (fields === undefined) {
    throw new Error(`unknown EIP-712 type: ${typeName}`);
  }
  const converted: Record<string, unknown> = {};
  for (const field of fields) {
    if (!(field.name in value)) {
      throw new Error(`EIP-712 message for ${typeName} is missing field "${field.name}"`);
    }
    converted[field.name] = convertValue(field.type, value[field.name], types);
  }
  return converted;
}

function convertValue(type: string, value: unknown, types: Record<string, readonly TypedDataParameter[]>): unknown {
  const arrayMatch = ARRAY_TYPE.exec(type);
  if (arrayMatch) {
    const itemType = arrayMatch[1];
    if (!Array.isArray(value)) {
      throw new Error(`expected an array for EIP-712 type ${type}`);
    }
    return value.map((item) => convertValue(itemType, item, types));
  }
  if (type in types) {
    return convertStruct(type, value as Record<string, unknown>, types);
  }
  if (INTEGER_TYPE.test(type)) {
    return toBigInt(value, type);
  }
  if (type === "bool") {
    return value === true || value === "true";
  }
  return value;
}

function toBigInt(value: unknown, type: string): bigint {
  if (typeof value === "bigint") {
    return value;
  }
  if (typeof value === "number" && !Number.isSafeInteger(value)) {
    throw new Error(`${type} value ${value} is not a safe integer: send it as a decimal string`);
  }
  if (typeof value === "number" || typeof value === "string") {
    return BigInt(value);
  }
  throw new Error(`cannot convert ${String(value)} to ${type}`);
}
