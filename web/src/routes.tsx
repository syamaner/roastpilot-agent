/**
 * Routing shell for the three pages (E10 plan §7) + the dev/test harness.
 *
 * S2 owns the route table; the page bodies are filled by S3 (dashboard), S4
 * (history), and S5 (detail). The harness route is foundation-only.
 *
 * #324 adds the home / landing hub + a persistent nav: the operator-facing routes
 * nest under `RootLayout` (which mounts the nav above every page), and `/` is
 * state-aware via `HomeGate` (active run → dashboard, idle → home). The dev/test
 * snapshot harnesses stay OUTSIDE the layout so their baselines remain nav-free
 * and deterministic.
 */

import { lazy } from "react";
import type { RouteObject } from "react-router-dom";


const RootLayout = lazy(() =>
  import("@/pages/home/RootLayout").then((m) => ({ default: m.RootLayout })),
);
const HomeGate = lazy(() =>
  import("@/pages/home/HomeGate").then((m) => ({ default: m.HomeGate })),
);
const StartRoastView = lazy(() =>
  import("@/pages/home/StartRoastView").then((m) => ({ default: m.StartRoastView })),
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
// #271 long-roast detail snapshot — the advisor-decisions list + decision-trace
// table both exceed the inline cap, proving the last-5 cap + "View all" modal.
const DetailHarnessLongPage = lazy(() =>
  import("@/pages/detail/DetailHarnessLongPage").then((m) => ({
    default: m.DetailHarnessLongPage,
  })),
);
// #351 dry-end detail snapshot — the base detail data PLUS a persisted drying_end
// timeline event, so the reload/persisted path places the dry-end chart marker
// (asserted via the window.__chart DATA hook, not a new pixel baseline).
const DetailHarnessDryEndPage = lazy(() =>
  import("@/pages/detail/DetailHarnessDryEndPage").then((m) => ({
    default: m.DetailHarnessDryEndPage,
  })),
);
// #303 bean-profile library snapshot — the idle Start-Roast form with the saved-
// profile dropdown + add/edit modals over fixed fixture data (the idle page is not
// reachable via the replay agents, which always carry an active run).
const StartRoastHarnessPage = lazy(() =>
  import("@/pages/harness/StartRoastHarnessPage").then((m) => ({
    default: m.StartRoastHarnessPage,
  })),
);
// #324 home snapshot — the persistent nav + the landing hub over a seeded idle
// `/health` snapshot (no active run), the deterministic `home` baseline target.
const HomeHarnessPage = lazy(() =>
  import("@/pages/home/HomeHarnessPage").then((m) => ({ default: m.HomeHarnessPage })),
);
// #403: stable, reload-safe /live route — the running roast's permanent address.
const LivePage = lazy(() =>
  import("@/pages/live/LivePage").then((m) => ({ default: m.LivePage })),
);
// #419: /config view — config snapshot from GET /api/config + save model.
const ConfigPage = lazy(() =>
  import("@/pages/config/ConfigPage").then((m) => ({ default: m.ConfigPage })),
);

export const routes: RouteObject[] = [
  // Operator-facing routes nest under RootLayout → the persistent nav (#324) is
  // mounted above each page. `/` is a PURE launcher (always `HomePage`, D81 /
  // #423) — no server-state read, no branching. `/live` is the SINGLE live-roast
  // home with three server-state-driven states: active → DashboardPage, just-
  // finished this session → LiveFinishedView, idle → LiveStartView (the start
  // form). `/start` is kept as an alias entry from the old home tile; it routes
  // to `/live` after a start POST (#403). Phase is never inferred client-side —
  // all active-run decisions read the server's `/health` snapshot.
  {
    element: <RootLayout />,
    children: [
      { path: "/", element: <HomeGate /> },
      { path: "/live", element: <LivePage /> },
      { path: "/start", element: <StartRoastView /> },
      // /config view — #419 S2. Renders from GET /api/config (AppConfigSnapshot).
      // Category rail + per-field controls + save model (PUT /api/config).
      { path: "/config", element: <ConfigPage /> },
      { path: "/roasts", element: <HistoryPage /> },
      { path: "/roasts/:runId", element: <DetailPage /> },
    ],
  },
  // Foundation harnesses — dev/test only, the snapshot suite's stable targets (D24).
  // __chart-harness: fixed-data LiveCurve/badge/indicator gallery.
  // __stream-smoke: the live SSE path wired to the real replay harness (S1).
  { path: "/__chart-harness", element: <ChartHarnessPage /> },
  { path: "/__stream-smoke", element: <StreamSmokePage /> },
  // __detail-harness: the detail page over fixed REST data (E10-S5 snapshots).
  { path: "/__detail-harness", element: <DetailHarnessPage /> },
  // __detail-harness-failed: advisor-failure detail state (#170 snapshot).
  { path: "/__detail-harness-failed", element: <DetailHarnessFailedPage /> },
  // __detail-harness-long: long-roast detail — capped lists + "View all" (#271).
  { path: "/__detail-harness-long", element: <DetailHarnessLongPage /> },
  // __detail-harness-dry-end: detail data + a persisted drying_end event, so the
  // reload path places the dry-end chart marker (#351 D24 data assertion).
  { path: "/__detail-harness-dry-end", element: <DetailHarnessDryEndPage /> },
  // __start-roast-harness: idle Start form + bean-profile library (#303 snapshot).
  { path: "/__start-roast-harness", element: <StartRoastHarnessPage /> },
  // __home-harness: persistent nav + landing hub over a seeded idle health snapshot.
  { path: "/__home-harness", element: <HomeHarnessPage /> },
];
