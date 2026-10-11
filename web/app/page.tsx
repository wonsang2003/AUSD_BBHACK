/**
 * `/` — the live dashboard: budget, agents, purchases (incl. skipped), findings, events,
 * market strip, Ask box, alerts and safety numbers. All data comes from the FastAPI backend
 * (`docs/API.md`) via `components/dashboard/useDashboardData.ts`, kept live over SSE.
 */

import { Dashboard } from "@/components/dashboard/Dashboard";

export default function DashboardPage() {
  return <Dashboard />;
}
