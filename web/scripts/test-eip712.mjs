/**
 * Cross-language EIP-712 test: the web builders must produce the same digests as Python.
 *
 * Run:  npm run test:eip712            (uses ../backend/tests/fixtures/eip712_vectors.json)
 *       node scripts/test-eip712.mjs <path-to-vectors.json>
 *
 * The backend writes `eip712_vectors.json` from `backend/app/chain/eip712.py`. Each vector
 * carries a full EIP-712 message `{types, primaryType, domain, message}` and the digest Python
 * computed for it. For every vector this script checks two things:
 *
 *   1. converter: viem `hashTypedData(toViemTypedData(fullMessage))` == the Python digest
 *      (proves the JSON → bigint conversion and dropping `EIP712Domain` are right);
 *   2. builder:   rebuilding the same message with the matching `lib/eip712.ts` builder from
 *      its domain + message fields gives the same digest (proves the TS type definitions and
 *      signing-domain names mirror Python exactly).
 *
 * If the vectors file does not exist yet, the script prints the TS digests of fixed sample
 * inputs (so they can be compared by hand) and exits 0 with a note.
 *
 * `lib/*.ts` is plain TypeScript imported with extensionless relative paths (the Next.js
 * convention). Node 22 strips the types natively; the small resolve hook below only adds the
 * `.ts` extension to those relative imports so Node can find the files.
 */

import { existsSync, readFileSync } from "node:fs";
import { register } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_VECTORS_PATH = path.resolve(SCRIPT_DIR, "../../backend/tests/fixtures/eip712_vectors.json");
const DIGEST_PATTERN = /^0x[0-9a-fA-F]{64}$/;
const EXIT_OK = 0;
const EXIT_FAILED = 1;

const RESOLVE_TS_EXTENSION_HOOK = `
export async function resolve(specifier, context, nextResolve) {
  const isRelative = specifier.startsWith("./") || specifier.startsWith("../");
  const hasExtension = /\\.[cm]?[jt]sx?$/.test(specifier);
  const fromTypeScript = (context.parentURL ?? "").endsWith(".ts");
  if (isRelative && !hasExtension && fromTypeScript) {
    return nextResolve(specifier + ".ts", context);
  }
  return nextResolve(specifier, context);
}
`;

register(`data:text/javascript,${encodeURIComponent(RESOLVE_TS_EXTENSION_HOOK)}`);
const eip712 = await import("../lib/eip712.ts");

// ─── loading vectors (tolerant of the exact file layout) ─────────────────────

/** True if `value` looks like an EIP-712 full message. */
function isFullMessage(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    "types" in value &&
    "primaryType" in value &&
    "domain" in value &&
    "message" in value
  );
}

/** Rule: the digest is the first string field whose key contains "digest" and that is 0x + 64 hex. */
function findDigest(vector) {
  for (const [key, value] of Object.entries(vector)) {
    if (key.toLowerCase().includes("digest") && typeof value === "string" && DIGEST_PATTERN.test(value)) {
      return value.toLowerCase();
    }
  }
  return null;
}

/** The vector's full message: the vector itself, or the first field that looks like one. */
function findFullMessage(vector) {
  if (isFullMessage(vector)) {
    return vector;
  }
  for (const value of Object.values(vector)) {
    if (isFullMessage(value)) {
      return value;
    }
  }
  return null;
}

/** Accepts `[vector]`, `{vectors: [vector]}` (any array-valued key) or `{name: vector}`. */
function listVectors(data) {
  if (Array.isArray(data)) {
    return data.map((vector, index) => ({ name: vector.name ?? vector.id ?? `#${index}`, vector }));
  }
  for (const value of Object.values(data)) {
    if (Array.isArray(value)) {
      return listVectors(value);
    }
  }
  return Object.entries(data).map(([name, vector]) => ({ name, vector }));
}

// ─── rebuilding a message with the TS builders ───────────────────────────────

/** Rebuild `full` with the lib/eip712.ts builder for its primaryType, or null if there is none. */
function rebuildWithTsBuilder(full) {
  const chainId = Number(full.domain.chainId);
  const verifyingContract = full.domain.verifyingContract;
  const message = full.message;
  switch (full.primaryType) {
    case "Permit":
      return eip712.buildPermit({ chainId, ausdAddress: verifyingContract, ...message });
    case "TransferWithAuthorization":
      return eip712.buildTransferWithAuthorization({ chainId, ausdAddress: verifyingContract, auth: message });
    case "ReceiveWithAuthorization":
      return eip712.buildReceiveWithAuthorization({ chainId, ausdAddress: verifyingContract, auth: message });
    case "CancelAuthorization":
      return eip712.buildCancelAuthorization({ chainId, ausdAddress: verifyingContract, ...message });
    case "BudgetConfig":
      return eip712.buildBudgetConfig({ chainId, controllerAddress: verifyingContract, config: message });
    case "SetPaused":
      return eip712.buildSetPaused({ chainId, controllerAddress: verifyingContract, ...message });
    default:
      return null;
  }
}

// ─── checks ──────────────────────────────────────────────────────────────────

function checkVector(name, vector) {
  const full = findFullMessage(vector);
  const expected = findDigest(vector);
  if (full === null || expected === null) {
    return [`${name}: no full message or digest found in this vector`];
  }
  const failures = [];
  const converted = eip712.typedDataDigest(full).toLowerCase();
  if (converted !== expected) {
    failures.push(`${name}: converter digest ${converted} != python ${expected}`);
  }
  const rebuilt = rebuildWithTsBuilder(full);
  if (rebuilt === null) {
    console.log(`  · ${name}: no TS builder for ${full.primaryType}, converter check only`);
  } else {
    const rebuiltDigest = eip712.typedDataDigest(rebuilt).toLowerCase();
    if (rebuiltDigest !== expected) {
      failures.push(`${name}: TS builder digest ${rebuiltDigest} != python ${expected}`);
    }
  }
  if (failures.length === 0) {
    console.log(`  ✓ ${name} (${full.primaryType}) ${expected}`);
  }
  return failures;
}

function runAgainstVectors(vectorsPath) {
  const vectors = listVectors(JSON.parse(readFileSync(vectorsPath, "utf8")));
  console.log(`EIP-712 cross-language test: ${vectors.length} vectors from ${vectorsPath}`);
  const failures = vectors.flatMap(({ name, vector }) => checkVector(name, vector));
  if (vectors.length === 0) {
    failures.push("the vectors file contains no vectors");
  }
  for (const failure of failures) {
    console.error(`  ✗ ${failure}`);
  }
  const passed = vectors.length - new Set(failures.map((failure) => failure.split(":")[0])).size;
  console.log(`\n${passed} passed, ${vectors.length - passed} failed`);
  return failures.length === 0 ? EXIT_OK : EXIT_FAILED;
}

// ─── fallback: print digests of fixed sample inputs ──────────────────────────

const SAMPLE = {
  chainId: 84532,
  ausd: "0xa9012a055bd4e0eDfF8Ce09f960291C09D5322dC",
  controller: "0x5FbDB2315678afecb367f032d93F642f64180aa3",
  owner: "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
  agent: "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
  nonce: "0x" + "ab".repeat(32),
  salt: "0x" + "01".repeat(32),
};

function sampleMessages() {
  const auth = {
    from: SAMPLE.owner,
    to: SAMPLE.agent,
    value: 50_000,
    validAfter: 1_760_000_000,
    validBefore: 1_760_000_120,
    nonce: SAMPLE.nonce,
  };
  return {
    permit: eip712.buildPermit({
      chainId: SAMPLE.chainId,
      ausdAddress: SAMPLE.ausd,
      owner: SAMPLE.owner,
      spender: SAMPLE.controller,
      value: 30_000_000,
      nonce: 0,
      deadline: 1_760_086_400,
    }),
    budget_config: eip712.buildBudgetConfig({
      chainId: SAMPLE.chainId,
      controllerAddress: SAMPLE.controller,
      config: {
        owner: SAMPLE.owner,
        dailyCap: 2_000_000,
        expiry: 1_762_592_000,
        agentAddrs: [SAMPLE.agent],
        agentCaps: [1_000_000],
        sectors: [eip712.sectorToBytes32("OptionsEvent")],
        nonce: 0,
      },
    }),
    set_paused: eip712.buildSetPaused({
      chainId: SAMPLE.chainId,
      controllerAddress: SAMPLE.controller,
      owner: SAMPLE.owner,
      paused: true,
      nonce: 1,
      deadline: 1_760_003_600,
    }),
    transfer_with_authorization: eip712.buildTransferWithAuthorization({
      chainId: SAMPLE.chainId,
      ausdAddress: SAMPLE.ausd,
      auth,
    }),
    receive_with_authorization: eip712.buildReceiveWithAuthorization({
      chainId: SAMPLE.chainId,
      ausdAddress: SAMPLE.ausd,
      auth,
    }),
    cancel_authorization: eip712.buildCancelAuthorization({
      chainId: SAMPLE.chainId,
      ausdAddress: SAMPLE.ausd,
      authorizer: SAMPLE.owner,
      nonce: SAMPLE.nonce,
    }),
    daily_voucher: eip712.buildDailyVoucher({
      chainId: SAMPLE.chainId,
      ausdAddress: SAMPLE.ausd,
      owner: SAMPLE.owner,
      controllerAddress: SAMPLE.controller,
      value: 2_000_000,
      dayIndex: 20_372,
      salt: SAMPLE.salt,
    }),
  };
}

function printSampleDigests(vectorsPath) {
  console.log(`NOTE: ${vectorsPath} does not exist yet (the backend writes it).`);
  console.log("Nothing to compare against; TS digests of fixed sample inputs, for a manual check:\n");
  for (const [name, full] of Object.entries(sampleMessages())) {
    console.log(`  ${name.padEnd(28)} ${eip712.typedDataDigest(full)}`);
  }
  return EXIT_OK;
}

// ─── main ────────────────────────────────────────────────────────────────────

const vectorsPath = process.argv[2] ? path.resolve(process.argv[2]) : DEFAULT_VECTORS_PATH;
const exitCode = existsSync(vectorsPath) ? runAgainstVectors(vectorsPath) : printSampleDigests(vectorsPath);
process.exit(exitCode);
