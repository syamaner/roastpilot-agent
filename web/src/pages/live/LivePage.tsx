/**
 * Single live-roast home at `/live` (#423, D81, updated #523).
 *
 * `/live` is the state of the roaster, always — NEVER a form (#523 IA). Three
 * server-state-driven states (all phase comes from the server):
 *
 * 1. Loading   — holds until health AND history (the idle path's persistent
 *    fallback) each produce a GENUINELY FRESH read, so nothing flashes before
 *    the active-run status — and the last-completed summary it falls back
 *    to — are known. Also holds while a just-finished run's terminal-outcome
 *    fetch is in flight, so an OLDER completed run's summary never flashes
 *    before the just-finished run's own gate resolves (#523 Codex follow-up
 *    on #532).
 * 2. Active run — the full live dashboard (DashboardPage). A reload on this
 *    URL re-hydrates from the server snapshot + SSE — the reload-safe guarantee.
 * 3. No active run — the last completed run's summary (`LiveFinishedView`),
 *    PERSISTENT across reload: sourced from the history API
 *    (`GET /api/roasts`, newest-first), not session state. The just-finished
 *    run from THIS session (`stickyCompletedRunId`) is preferred while set —
 *    history can lag the terminal write by a beat — but a reload always falls
 *    through to the same history-derived id, so the summary survives it
 *    (unlike the pre-#523 session-only sticky). If the operator has never
 *    completed a roast, `LiveNoRoastsView` shows a neutral "no roasts yet"
 *    state — still not a form — with a link to `/start`. A persistent history
 *    read failure gets its OWN neutral state (`LiveHistoryUnknownView`) —
 *    never `LiveNoRoastsView`, which would otherwise assert a false "this
 *    roaster has never completed a roast" on a network/server error.
 *
 * `/start` (StartRoastView.tsx) is the ONLY start-form surface under the
 * #523 IA; `/live` never renders one.
 *
 * INVARIANTS: active-run presence comes from the SERVER's `/health` snapshot
 * (`active_run_id`) — never inferred client-side (D8). Phase is not read here;
 * DashboardPage owns phase via SSE + snapshot. Operator actions and MCP access
 * live entirely in DashboardPage.
 */

import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";

import { AppFrame, LiveCurve } from "@/components/shared";
import {
  roastKeys,
  useFreshHealthGate,
  useFreshHistoryGate,
  useRoast,
  useTelemetry,
} from "@/hooks/queries";
import { api } from "@/lib/api";
import { DashboardPage } from "@/pages/dashboard/DashboardPage";
import { headlineStats, toCurveMarkers, toCurvePoints } from "@/pages/detail/traceModel";

/**
 * Fetch the terminal RoastDetail snapshot for a just-ended run and return its
 * outcome. Always re-fetches (staleTime: 0) so the cache reflects the server's
 * FINAL state, not the stale in-progress snapshot that had `outcome: null` while
 * the run was live (P2-3). Populates the TanStack Query cache so a subsequent
 * `useRoast(runId)` in LiveFinishedView resolves synchronously from the fresh data.
 * Returns `null` on any network error (safe no-op: the session-sticky summary is
 * silently suppressed for this transition — the persistent history-derived
 * fallback, #523, still applies on the next render/reload).
 */
async function fetchTerminalOutcome(
  queryClient: QueryClient,
  runId: string,
): Promise<string | null> {
  try {
    const detail = await queryClient.fetchQuery({
      queryKey: roastKeys.detail(runId),
      queryFn: () => api.roast(runId),
      staleTime: 0,
    });
    return detail.outcome ?? null;
  } catch {
    return null;
  }
}

export function LivePage(): React.JSX.Element {
  // #513 Codex follow-up: `useHealth()`'s shared 30s `staleTime` means a
  // remount within that window would render a CACHED `active_run_id` with
  // `isSuccess: true` and no network request at all — a second process/tab
  // could have started a run in that window, and the summary/no-roasts view
  // would flash on stale "idle" data before the gate below ever fires. Gate on
  // `useFreshHealthGate` instead of `useHealth` here (see its doc for the
  // full empirically-verified rationale); non-gating consumers elsewhere
  // (header/nav) keep using plain `useHealth` unchanged.
  const health = useFreshHealthGate();
  const activeRunId = health.data?.active_run_id ?? null;
  const queryClient = useQueryClient();

  // Persistent idle fallback (#523): the roast history, newest-first
  // (`GET /api/roasts` — store.py orders `started_at_utc DESC`). #523 Codex
  // follow-up on #532: history is now an AUTHORITATIVE source for this idle
  // state (the persistent last-completed-run fallback), so it earns the same
  // `useFreshHealthGate`-class treatment health already has — gate on
  // `useFreshHistoryGate`, not plain `useHistory`, so a within-staleTime
  // remount can't render a cached (possibly empty) history list as proof the
  // roaster has never completed a roast.
  const history = useFreshHistoryGate();
  const lastCompletedRunId =
    history.data?.runs.find((run) => run.outcome === "completed")?.id ?? null;

  // Track the most recent non-null active_run_id across renders. When the id
  // transitions non-null → null we fetch the terminal run snapshot so the
  // session-sticky summary can show IMMEDIATELY on this render, before the
  // history list has necessarily caught up (it can lag the terminal write by
  // a beat). `stickyCompletedRunId` is a same-session convenience layered on
  // top of `lastCompletedRunId`; a reload always falls back to the latter, so
  // the summary survives it (unlike the pre-#523 session-only sticky).
  //   - `completed`: latch as stickyCompletedRunId → LiveFinishedView.
  //   - anything else (faulted, aborted): do NOT latch — the fault flow in
  //     DashboardPage owns that path (P2-4 / P2-3 / #423).
  //
  // Fetching with staleTime:0 ensures we get the SERVER'S TERMINAL snapshot, not
  // the stale in-progress cache that had outcome:null while the run was live (P2-3).
  //
  // #523 Codex follow-up on #532 (transition-flash): while THIS fetch is in
  // flight, `stickyCompletedRunId` is still `null` — without a guard, the
  // render in that window would fall through to `lastCompletedRunId`, which
  // (if an OLDER completed run exists in history) flashes that older run's
  // summary before swapping to the just-finished one a moment later.
  // `terminalFetchPending` holds the idle branch through that window, so the
  // just-finished run's own gate always resolves before ANY summary renders.
  const prevRunIdRef = useRef<string | null>(null);
  const [stickyCompletedRunId, setStickyCompletedRunId] = useState<string | null>(null);
  const [terminalFetchPending, setTerminalFetchPending] = useState(false);

  useEffect(() => {
    if (activeRunId !== null) {
      // A run is active — remember it.
      prevRunIdRef.current = activeRunId;
    } else if (prevRunIdRef.current !== null) {
      // Transition: active_run_id was non-null, is now null. Fetch the terminal
      // snapshot to gate on outcome (P2-3 + P2-4). fetchQuery populates the cache
      // so LiveFinishedView's useRoast sees the terminal data immediately on mount.
      const finishedId = prevRunIdRef.current;
      prevRunIdRef.current = null;
      setTerminalFetchPending(true);
      void fetchTerminalOutcome(queryClient, finishedId).then((outcome) => {
        if (outcome === "completed") {
          setStickyCompletedRunId(finishedId);
        }
        // Non-completed outcomes (faulted, aborted, null) don't show the summary.
        // DashboardPage retains the faulted run via stickyFaultedRunId for ack.
        setTerminalFetchPending(false);
      });
      // The history list is also invalidated on `run_completed` /
      // fault-acknowledge (see queries.ts) so `lastCompletedRunId` picks up
      // the new run independently of this session-sticky path.
    }
  }, [activeRunId, queryClient]);

  // Health error (#513 medium): active-run status is UNKNOWN — never fall
  // through to a state that implies "no run" (a run could genuinely be active
  // and the operator would have no path to the dashboard/e-stop, the exact
  // hazard this PR fixes elsewhere). `useHealth`'s default `retry: 1` already
  // rides out a single blip before `isError` is true, so this is a persistent
  // failure, not noise. Show a neutral "can't confirm" state instead.
  if (health.isError) {
    return <LiveStatusUnknownView />;
  }

  // Hold until health has produced a GENUINELY FRESH read (same pattern as
  // the old HomeGate hold, extended #513 Codex follow-up). `health.isFresh`
  // (`useFreshHealthGate`) is false both while genuinely pending AND while
  // `isSuccess` is true only from stale cache with a forced refetch still in
  // flight — a within-staleTime remount would otherwise let a cached "idle"
  // snapshot render as proof no run is active, when another tab/process
  // could have started one in the last 30s. The `health.isError` branch
  // above already handles the persistent-failure case (which `isFresh`
  // treats as settled, not pending), so this check is a single condition.
  if (!health.isFresh) {
    return (
      <AppFrame>
        <div data-testid="live-page-loading" />
      </AppFrame>
    );
  }

  // Active run: the full live dashboard.
  if (activeRunId !== null) {
    return <DashboardPage />;
  }

  // No active run: never a form (#523). Hold while a terminal-outcome fetch
  // for the just-finished run is in flight — see the transition-flash note
  // above — so an older completed run's summary never renders as a flash
  // before the just-finished run's own gate resolves. This check does NOT
  // depend on `stickyCompletedRunId` being null (unlike the history hold
  // below): the fetch itself, not just its outcome, is what must finish
  // before any fallback is allowed to render.
  if (terminalFetchPending) {
    return (
      <AppFrame>
        <div data-testid="live-page-loading" />
      </AppFrame>
    );
  }

  // History error (#523 Codex follow-up on #532): never render
  // `LiveNoRoastsView` on a history read failure — that would assert a false
  // "this roaster has never completed a roast" on what might just be a
  // network/server blip. A session-sticky id (this session's own just-
  // finished run) does not depend on history at all and may still render.
  if (history.isError && stickyCompletedRunId === null) {
    return <LiveHistoryUnknownView />;
  }

  // No active run: never a form (#523). Hold briefly for history to settle
  // too, so a reload doesn't flash "no roasts yet" before the persistent
  // fallback has had a chance to resolve — the session-sticky id (if any) is
  // already known synchronously and doesn't need this hold. `history.isFresh`
  // (not `isPending`) closes the #532 staleness gap: a within-staleTime
  // remount must not render a CACHED history list — possibly empty — as
  // proof no roast has ever completed.
  if (stickyCompletedRunId === null && !history.isFresh) {
    return (
      <AppFrame>
        <div data-testid="live-page-loading" />
      </AppFrame>
    );
  }

  const summaryRunId = stickyCompletedRunId ?? lastCompletedRunId;
  if (summaryRunId !== null) {
    return <LiveFinishedView runId={summaryRunId} />;
  }

  // No active run, and no completed run exists (ever) — a neutral, still-not-
  // a-form state pointing to /start, the only start-form surface (#523).
  return <LiveNoRoastsView />;
}

// --- LiveStatusUnknownView: shown at /live when /health persistently errors. ---

/**
 * Neutral "can't confirm roaster status" state (#513 medium). Shown when
 * `useHealth()` errors persistently (after its own retry budget) — active-run
 * status is genuinely UNKNOWN, so this must never fall through to a state
 * that implies no run is active (the last-completed summary or the no-roasts
 * view, #523): a run could be active and heating, and the operator would have
 * no path to the dashboard/emergency stop. `useHealth` is refetched on-focus
 * disabled but still observed here, so a manual reload or the browser's own
 * retry is the recovery path; this view offers an explicit reload link too.
 */
function LiveStatusUnknownView(): React.JSX.Element {
  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Status unknown
        </span>
      }
    >
      <div
        className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-roast-fault/50 bg-roast-fault/10 p-8 text-center"
        data-testid="live-status-unknown"
      >
        <h2 className="text-lg font-bold uppercase tracking-wide">Can&apos;t confirm roaster status</h2>
        <p className="text-sm text-muted-foreground">
          This page could not reach the agent to check whether a roast is active. If
          one is running, it is still live and heating — reload to reconnect before
          assuming it is safe to start a new one.
        </p>
        <a
          href="/live"
          data-testid="live-status-unknown-reload"
          className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
        >
          Reload
        </a>
      </div>
    </AppFrame>
  );
}

// --- LiveHistoryUnknownView: shown at /live when history persistently errors. ---

/**
 * Neutral "can't load roast history" state (#523 Codex follow-up on #532).
 * Shown when `useFreshHistoryGate()` errors persistently (after its own
 * retry budget) and no session-sticky summary is available to render
 * instead. Distinct from `LiveStatusUnknownView`: THIS run's active-run
 * status is known (health resolved fine, or `activeRunId` is null) — what's
 * unknown is whether a completed roast exists to summarise. Falling through
 * to `LiveNoRoastsView` here would assert a false "this roaster has never
 * completed a roast" on what might be a transient network/server error, the
 * exact isSuccess≠current hazard class `useFreshHealthGate` already guards
 * against for health. A manual reload is the recovery path.
 */
function LiveHistoryUnknownView(): React.JSX.Element {
  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          History unavailable
        </span>
      }
    >
      <div
        className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-roast-fault/50 bg-roast-fault/10 p-8 text-center"
        data-testid="live-history-unknown"
      >
        <h2 className="text-lg font-bold uppercase tracking-wide">Can&apos;t load roast history</h2>
        <p className="text-sm text-muted-foreground">
          This page could not reach the agent to check for a completed roast to
          summarise. This does not mean none exists — reload to try again.
        </p>
        <a
          href="/live"
          data-testid="live-history-unknown-reload"
          className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
        >
          Reload
        </a>
      </div>
    </AppFrame>
  );
}

// --- LiveFinishedView: persistent last-completed-run summary at /live. ---

interface LiveFinishedViewProps {
  /** The last completed run's id — either this session's just-finished run
   *  (session-sticky, immediate) or the history-derived fallback (#523,
   *  persistent — survives reload). Either way it is a genuine `completed`
   *  outcome; LivePage never passes a faulted/aborted run here. */
  runId: string;
}

/**
 * Persistent "roaster's last completed roast" summary shown at `/live` when no
 * run is active (#523) — survives reload, sourced from the history API, not
 * session state. Immediately after a roast ends in the CURRENT session, the
 * RoastDetail snapshot was already fetched (with staleTime:0) by LivePage's
 * `fetchTerminalOutcome` call before this view mounts, so `useRoast` resolves
 * synchronously from the cache — the terminal snapshot with the final outcome
 * (P2-3). On a reload, or once `lastCompletedRunId` takes over, `useRoast`
 * fetches normally.
 *
 * Headline stats (drop temp / dev% / total time) come from the FULL-RESOLUTION
 * telemetry series (`downsample=1`), guaranteeing that the drop/terminal rows are
 * included regardless of stride position (P2-2). The mini curve uses the
 * downsampled series (`downsample=5`) to keep the fetch lightweight.
 *
 * 'Start next roast' links to `/start` — the only start-form surface under the
 * #523 IA. This view itself is never a form.
 */
function LiveFinishedView({ runId }: LiveFinishedViewProps): React.JSX.Element {
  const roast = useRoast(runId);
  // Full-resolution telemetry for accurate headline stats (P2-2): downsample=1
  // ensures the drop/terminal rows are included regardless of stride position.
  const telemetryFull = useTelemetry(runId, 1);
  // Downsampled telemetry for the mini curve only — lightweight fetch for display.
  const telemetryCurve = useTelemetry(runId, 5);

  const stats = headlineStats(undefined, telemetryFull.data);
  const points = toCurvePoints(telemetryCurve.data);
  const markers = toCurveMarkers(undefined, telemetryCurve.data);

  const beanOrigin = roast.data?.profile.bean_origin ?? null;
  const outcome = roast.data?.outcome ?? null;

  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Roast complete
        </span>
      }
    >
      <div className="mx-auto max-w-3xl" data-testid="live-finished-view">
        {/* Run identity */}
        <header className="mb-6">
          <h2 className="font-mono text-2xl text-foreground">
            {beanOrigin ?? "Roast complete"}
          </h2>
          {outcome !== null && (
            <p
              className="mt-1 text-sm capitalize text-muted-foreground"
              data-testid="live-finished-outcome"
            >
              {outcome}
            </p>
          )}
        </header>

        {/* Headline stats */}
        <div
          className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4"
          data-testid="live-finished-stats"
        >
          <StatTile
            testId="stat-drop-temp"
            label="Drop temp"
            value={stats.dropTempC !== null ? `${Math.round(stats.dropTempC)} °C` : "—"}
          />
          <StatTile
            testId="stat-dev-percent"
            label="Dev %"
            value={
              stats.developmentPercent !== null
                ? `${stats.developmentPercent.toFixed(1)} %`
                : "—"
            }
          />
          <StatTile
            testId="stat-total-time"
            label="Total time"
            value={stats.totalSeconds !== null ? formatDuration(stats.totalSeconds) : "—"}
          />
          <StatTile
            testId="stat-weight-loss"
            label="Weight loss"
            value={
              roast.data?.weight_loss_percent != null
                ? `${roast.data.weight_loss_percent.toFixed(1)} %`
                : "—"
            }
          />
        </div>

        {/* Mini curve — only when we have data */}
        {points.length > 0 && (
          <div className="mb-6 rounded-lg border border-border bg-card p-4" data-testid="live-finished-curve">
            <LiveCurve points={points} markers={markers} height={180} />
          </div>
        )}

        {/* Actions */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <Link
            to={`/roasts/${runId}`}
            className="flex-1 rounded-md border border-border bg-card px-5 py-3 text-center text-sm font-medium text-foreground transition-colors hover:bg-accent/40"
            data-testid="live-finished-view-detail"
          >
            View full detail
          </Link>
          <Link
            to="/start"
            className="flex-1 rounded-md bg-primary px-5 py-3 text-center text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
            data-testid="live-finished-start-next"
          >
            Start next roast
          </Link>
        </div>
      </div>
    </AppFrame>
  );
}

/** A single headline-stat card. */
function StatTile({
  label,
  value,
  testId,
}: {
  label: string;
  value: string;
  testId: string;
}): React.JSX.Element {
  return (
    <div
      className="flex flex-col gap-1 rounded-lg border border-border bg-card px-4 py-3"
      data-testid={testId}
    >
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="font-mono text-xl font-semibold text-foreground">{value}</span>
    </div>
  );
}

/** Format total seconds as `M:SS`. */
function formatDuration(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.round(totalSeconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

// --- LiveNoRoastsView: shown at /live when there is no active run and no
// completed run has EVER been recorded (a fresh install, or every past run
// faulted/aborted). Never a form (#523) — a neutral state pointing to /start,
// the only start-form surface under the new IA. ---

function LiveNoRoastsView(): React.JSX.Element {
  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          No roasts yet
        </span>
      }
    >
      <div
        className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-border bg-card p-8 text-center"
        data-testid="live-no-roasts-view"
      >
        <h2 className="text-lg font-bold uppercase tracking-wide">No roasts yet</h2>
        <p className="text-sm text-muted-foreground">
          This roaster hasn&apos;t completed a roast. Start one to see its live status
          and, afterward, its summary here.
        </p>
        <Link
          to="/start"
          data-testid="live-no-roasts-start-link"
          className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
        >
          Start a new roast
        </Link>
      </div>
    </AppFrame>
  );
}
