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
const StreamSmokePage = lazy(() =>
  import("@/pages/harness/StreamSmokePage").then((m) => ({ default: m.StreamSmokePage })),
);
// E10-S5 detail snapshot harness — fixed REST-shaped data for the roast-detail /
// roast-detail-selected baselines (deterministic without the stepped-SSE backend).
const DetailHarnessPage = lazy(() =>
  import("@/pages/detail/DetailHarnessPage").then((m) => ({ default: m.DetailHarnessPage })),
);
// #170 advisor-failure detail snapshot — every consult a provider_error, proving
// the advisor timeline renders failures (not a blank panel).
const DetailHarnessFailedPage = lazy(() =>
  import("@/pages/detail/DetailHarnessFailedPage").then((m) => ({
    default: m.DetailHarnessFailedPage,
  })),
);

export const routes: RouteObject[] = [
  { path: "/", element: <DashboardPage /> },
  { path: "/roasts", element: <HistoryPage /> },
  { path: "/roasts/:runId", element: <DetailPage /> },
  // Foundation harnesses — dev/test only, the snapshot suite's stable targets (D24).
  // __chart-harness: fixed-data LiveCurve/badge/indicator gallery.
  // __stream-smoke: the live SSE path wired to the real replay harness (S1).
  { path: "/__chart-harness", element: <ChartHarnessPage /> },
  { path: "/__stream-smoke", element: <StreamSmokePage /> },
  // __detail-harness: the detail page over fixed REST data (E10-S5 snapshots).
  { path: "/__detail-harness", element: <DetailHarnessPage /> },
  // __detail-harness-failed: advisor-failure detail state (#170 snapshot).
  { path: "/__detail-harness-failed", element: <DetailHarnessFailedPage /> },
];
