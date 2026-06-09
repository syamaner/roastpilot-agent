/**
 * Routing shell for the three pages (E10 plan §7) + the dev/test harness.
 *
 * S2 owns the route table; the page bodies are filled by S3 (dashboard), S4
 * (history), and S5 (detail). The harness route is foundation-only.
 */

import { lazy } from "react";
import type { RouteObject } from "react-router-dom";

const DashboardPage = lazy(() =>
  import("@/pages/dashboard/DashboardPage").then((m) => ({ default: m.DashboardPage })),
);
const HistoryPage = lazy(() =>
  import("@/pages/history/HistoryPage").then((m) => ({ default: m.HistoryPage })),
);
const DetailPage = lazy(() =>
  import("@/pages/detail/DetailPage").then((m) => ({ default: m.DetailPage })),
);
const ChartHarnessPage = lazy(() =>
  import("@/pages/harness/ChartHarnessPage").then((m) => ({ default: m.ChartHarnessPage })),
);

export const routes: RouteObject[] = [
  { path: "/", element: <DashboardPage /> },
  { path: "/roasts", element: <HistoryPage /> },
  { path: "/roasts/:runId", element: <DetailPage /> },
  // Foundation harness — the snapshot suite's stable target (D24). Dev/test only.
  { path: "/__chart-harness", element: <ChartHarnessPage /> },
];
