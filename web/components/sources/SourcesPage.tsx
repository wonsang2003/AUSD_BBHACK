/**
 * SourcesPage: `/sources` — where the agents' research comes from.
 *
 *   Data catalog (`GET /api/catalog`): tier, source, pricing, payable mode, status. Our sellers
 *   are labelled "demo vendor" (testnet stand-ins); the "not sold per call" row is shown on purpose.
 *   Live x402 probe (`GET /api/x402-probe`): what real x402 vendors ask for today, and why a
 *   PocketDesk budget cannot pay them yet (USDC on Base mainnet vs AUSD on testnet).
 * PocketDesk is buyer-side only, so there is no vendor onboarding here.
 */

"use client";

import { useQuery } from "@tanstack/react-query";

import { QueryState } from "@/components/dashboard/QueryState";
import { Card } from "@/components/ui/Card";
import { getCatalog } from "@/lib/api";

import { CatalogTable } from "./CatalogTable";
import { X402ProbePanel } from "./X402ProbePanel";

export function SourcesPage() {
  const catalog = useQuery({ queryKey: ["catalog"], queryFn: getCatalog, staleTime: Infinity });
  return (
    <div className="flex flex-col gap-3">
      <Card title="Data catalog" subtitle="what agents can buy per call, and what they honestly cannot" flush>
        <QueryState query={catalog}>{(rows) => <CatalogTable rows={rows} />}</QueryState>
      </Card>
      <X402ProbePanel />
    </div>
  );
}
