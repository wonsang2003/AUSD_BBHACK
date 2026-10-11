/**
 * `/agents` — per-agent caps, stake per asset, watchlist, session hours (`GET/POST /api/settings`)
 * and per-agent pause / resume. See `components/agents/AgentsPage.tsx`.
 */

import { AgentsPage } from "@/components/agents/AgentsPage";

export default function Agents() {
  return <AgentsPage />;
}
