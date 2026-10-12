/**
 * X402ProbePanel: what real x402 vendors ask for RIGHT NOW, fetched live.
 *
 * Data: `GET /api/x402-probe` — the backend sends UNPAID requests to real mainnet x402 vendors
 * (CoinGecko, CoinMarketCap, Nansen, x402stock), parses the 402 Payment Required answer with
 * the same parser our x402 client uses, and never signs or pays. Cached 10 min server-side;
 * "Re-probe" calls `?refresh=1`. Each requirement row answers whether a PocketDesk budget could
 * pay it today (only AUSD on Base Sepolia can).
 */

"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { errorText, QueryState } from "@/components/dashboard/QueryState";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { getX402Probe } from "@/lib/api";
import { relativeTime, shortAddress } from "@/lib/format";
import type { X402ProbeVendor, X402Requirement } from "@/lib/types";

import { assetInfo, formatAmount, networkName, payableAnswer } from "./probeLabels";

const PROBE_KEY = ["x402-probe"];

export function X402ProbePanel() {
  const client = useQueryClient();
  const probe = useQuery({ queryKey: PROBE_KEY, queryFn: () => getX402Probe(false) });
  const reprobe = useMutation({
    mutationFn: () => getX402Probe(true),
    onSuccess: (result) => client.setQueryData(PROBE_KEY, result),
  });

  const actions = (
    <>
      {probe.data && (
        <span className="num text-[10px] text-fg-dim">
          probed {relativeTime(probe.data.probed_at)}
          {probe.data.cached ? " (cached)" : ""}
        </span>
      )}
      <Button size="sm" busy={reprobe.isPending} onClick={() => reprobe.mutate()}>
        Re-probe
      </Button>
    </>
  );

  return (
    <Card title="Live x402 probe" subtitle="unpaid requests to real vendors · never signs or pays" actions={actions} flush>
      {reprobe.isError && <div className="px-3 pt-2 text-[11px] text-down">Re-probe failed: {errorText(reprobe.error)}</div>}
      <QueryState query={probe} isEmpty={(result) => result.vendors.length === 0} emptyText="No vendors probed.">
        {(result) => (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="border-b border-line text-left text-[10px] uppercase tracking-wider text-fg-dim">
                  <th className="px-2 py-1">Vendor</th>
                  <th className="px-2 py-1">x402</th>
                  <th className="px-2 py-1">Scheme</th>
                  <th className="px-2 py-1">Network</th>
                  <th className="px-2 py-1">Asset</th>
                  <th className="px-2 py-1 text-right">Amount</th>
                  <th className="px-2 py-1">payTo</th>
                  <th className="px-2 py-1">Payable from your PocketDesk budget?</th>
                </tr>
              </thead>
              <tbody>
                {result.vendors.map((vendor) => (
                  <VendorRows key={vendor.id} vendor={vendor} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryState>
    </Card>
  );
}

function VendorCell({ vendor }: { vendor: X402ProbeVendor }) {
  return (
    <td className="px-2 py-1 align-top">
      <div className="flex flex-col">
        <span className="font-semibold">{vendor.name}</span>
        <span className="max-w-[260px] truncate text-[10px] text-fg-dim" title={vendor.url}>
          {vendor.url}
        </span>
      </div>
    </td>
  );
}

function VendorRows({ vendor }: { vendor: X402ProbeVendor }) {
  if (!vendor.ok || vendor.accepts.length === 0) {
    return (
      <tr className="border-b border-line/60">
        <VendorCell vendor={vendor} />
        <td colSpan={7} className="px-2 py-1 text-[11px] text-fg-dim">
          <Badge tone="down">no 402</Badge> {vendor.http_status !== null ? `HTTP ${vendor.http_status}` : ""}{" "}
          {vendor.error ?? "no parseable payment requirement"}
        </td>
      </tr>
    );
  }
  return (
    <>
      {vendor.accepts.map((requirement, index) => (
        <RequirementRow key={`${vendor.id}-${index}`} vendor={vendor} requirement={requirement} first={index === 0} />
      ))}
    </>
  );
}

function RequirementRow({ vendor, requirement, first }: { vendor: X402ProbeVendor; requirement: X402Requirement; first: boolean }) {
  const answer = payableAnswer(requirement);
  return (
    <tr className="border-b border-line/60 hover:bg-surface-2">
      {first ? <VendorCell vendor={vendor} /> : <td />}
      <td className="num px-2 py-1">{first && vendor.x402_version !== null ? `v${vendor.x402_version}` : ""}</td>
      <td className="px-2 py-1">{requirement.scheme}</td>
      <td className="px-2 py-1" title={requirement.network}>
        {networkName(requirement.network)}
      </td>
      <td className="px-2 py-1 font-semibold">{assetInfo(requirement).symbol}</td>
      <td className="num px-2 py-1 text-right">{formatAmount(requirement)}</td>
      <td className="num px-2 py-1 text-fg-muted" title={requirement.payTo}>
        {shortAddress(requirement.payTo)}
      </td>
      <td className="px-2 py-1">
        <Badge tone={answer.payable ? "up" : "warn"} className="normal-case">
          {answer.text}
        </Badge>
      </td>
    </tr>
  );
}
