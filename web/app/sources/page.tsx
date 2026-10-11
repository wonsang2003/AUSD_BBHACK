/**
 * `/sources` — the data catalog (`GET /api/catalog`) and the live x402 probe of real vendors
 * (`GET /api/x402-probe`). See `components/sources/SourcesPage.tsx`.
 */

import { SourcesPage } from "@/components/sources/SourcesPage";

export default function Sources() {
  return <SourcesPage />;
}
