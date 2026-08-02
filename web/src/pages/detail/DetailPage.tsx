/**
 * Roast detail page (E10-S5, plan §7 / ui-prompts Prompt C / kickoff §2).
 *
 * Post-roast analysis: the full persisted curve (the shared `LiveCurve`), the
 * decision-trace table (all six verdicts — it renders history), the event
 * timeline, export downloads, and the self-rating widget. Driven entirely by the
 * REST contract (`GET /api/roasts/{id}` + `/telemetry` + `/timeline`) — no SSE, no
 * MCP, no client-side phase inference. The page renders the server's persisted
 * truth.
 *
 * The data-fetching shell; the layout lives in `DetailView` (kept query-free so
 * the snapshot harness can feed it fixed data).
 */

import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { AppFrame } from "@/components/shared";
import { roastKeys, useRoast, useTelemetry, useTimeline } from "@/hooks/queries";
import type { RoastDetail, TelemetrySeries } from "@/lib/types";
import { DetailView } from "./DetailView";

interface CompletionState {
  runId: string | null;
  wasLive: boolean;
  phase:
    | "idle"
    | "refreshing_after_live_transition"
    | "refreshing_aborted"
    | "retrying_confirmation"
    | "done";
  dataUpdateCountBeforeFetch: number;
  errorUpdateCountBeforeFetch: number;
}

/** Whether persisted telemetry already proves the run's terminal D96 boundary. */
function hasTerminalTelemetryEvidence(
  outcome: RoastDetail["outcome"],
  telemetry: TelemetrySeries | undefined,
): boolean {
  // An administratively aborted orphan has no runner left to append another
  // telemetry row; its first settled series is necessarily the final series.
  if (outcome === "aborted") return true;
  const phase = telemetry?.points.at(-1)?.agent_phase;
  if (outcome === "faulted") return phase === "faulted";
  return phase === "cooling" || phase === "complete";
}

export function DetailPage(): React.JSX.Element {
  const { runId = null } = useParams<{ runId: string }>();

  const detail = useRoast(runId);
  const telemetry = useTelemetry(runId);
  const timeline = useTimeline(runId);
  const queryClient = useQueryClient();
  const completedAtUtc = detail.data?.completed_at_utc;
  const outcome = detail.data?.outcome;
  const completionStateRef = useRef<CompletionState>({
    runId: null,
    wasLive: false,
    phase: "idle",
    dataUpdateCountBeforeFetch: 0,
    errorUpdateCountBeforeFetch: 0,
  });

  // A detail route may be opened while its roast is still live. `useRoast`
  // observes completion on its slow poll, but telemetry has a separate cache
  // key and otherwise remains the mid-roast series indefinitely. Refresh it
  // once at the terminal boundary so the post-roast D96 summary is complete.
  useEffect(() => {
    if (completionStateRef.current.runId !== runId) {
      completionStateRef.current = {
        runId,
        wasLive: false,
        phase: "idle",
        dataUpdateCountBeforeFetch: 0,
        errorUpdateCountBeforeFetch: 0,
      };
    }
    const state = completionStateRef.current;
    if (runId === null || completedAtUtc === undefined) return;
    if (completedAtUtc === null) {
      state.wasLive = true;
      return;
    }
    if (state.phase === "done") return;

    const refresh = (
      phase: CompletionState["phase"],
    ): void => {
      const queryKey = roastKeys.telemetry(runId, 1);
      const queryState = queryClient.getQueryState(queryKey);
      state.phase = phase;
      state.dataUpdateCountBeforeFetch = queryState?.dataUpdateCount ?? 0;
      state.errorUpdateCountBeforeFetch = queryState?.errorUpdateCount ?? 0;
      void queryClient.invalidateQueries({
        queryKey,
        exact: true,
      });
    };

    if (state.wasLive && state.phase === "idle") {
      state.wasLive = false;
      refresh("refreshing_after_live_transition");
      return;
    }

    // Cold terminal reads race detail and telemetry. Completion is persisted
    // before the runner's final telemetry row, so a first terminal detail
    // response can accompany a partial telemetry SELECT. Wait for that request
    // to settle, then confirm from server-owned phase evidence; retry at most
    // once when the boundary row is absent. Query timestamps cannot prove
    // server SELECT order, so they are deliberately not used as freshness proof.
    if (outcome === undefined) return;
    if (telemetry.fetchStatus !== "idle") return;

    if (state.phase !== "idle") {
      // Query-core increments one of these generation counters on every settled
      // request. Unlike timestamps/fetchStatus, this detects an exhausted error,
      // a same-millisecond success, and a request that starts/settles in one batch.
      const queryState = queryClient.getQueryState(roastKeys.telemetry(runId, 1));
      const settled =
        (queryState?.dataUpdateCount ?? 0) > state.dataUpdateCountBeforeFetch ||
        (queryState?.errorUpdateCount ?? 0) > state.errorUpdateCountBeforeFetch;
      if (!settled) return;
      // An administrative abort has no runner left to append a terminal row,
      // but the telemetry request that mounted beside the detail request may
      // have completed before the abort became authoritative. One post-outcome
      // refresh makes the persisted series current; there is no evidence row to
      // wait for, so never schedule a confirmation retry after it settles.
      if (state.phase === "refreshing_aborted") {
        state.phase = "done";
        return;
      }
      if (hasTerminalTelemetryEvidence(outcome, telemetry.data)) {
        state.phase = "done";
        return;
      }
      if (state.phase === "refreshing_after_live_transition") {
        refresh("retrying_confirmation");
        return;
      }
      // The one evidence-confirmation retry is deliberately bounded. A third
      // request could loop forever if a legacy/partial series never gains a
      // terminal row; remount remains the recovery path.
      state.phase = "done";
      return;
    }

    if (outcome === "aborted") {
      refresh("refreshing_aborted");
    } else if (hasTerminalTelemetryEvidence(outcome, telemetry.data)) {
      state.phase = "done";
    } else {
      refresh("retrying_confirmation");
    }
  }, [
    completedAtUtc,
    outcome,
    queryClient,
    runId,
    telemetry.data,
    telemetry.dataUpdatedAt,
    telemetry.errorUpdatedAt,
    telemetry.fetchStatus,
  ]);

  return (
    <AppFrame>
      <DetailBody
        runId={runId}
        detail={detail}
        telemetry={telemetry}
        timeline={timeline}
      />
    </AppFrame>
  );
}

interface DetailBodyProps {
  runId: string | null;
  detail: ReturnType<typeof useRoast>;
  telemetry: ReturnType<typeof useTelemetry>;
  timeline: ReturnType<typeof useTimeline>;
}

function DetailBody({ runId, detail, telemetry, timeline }: DetailBodyProps): React.JSX.Element {
  if (runId === null) {
    return <Message testId="detail-no-run">No roast selected.</Message>;
  }
  if (detail.isPending) {
    return <Message testId="detail-loading">Loading roast…</Message>;
  }
  if (detail.isError || detail.data === undefined) {
    return <Message testId="detail-error">Roast not found.</Message>;
  }

  return (
    <DetailView
      detail={detail.data}
      telemetry={telemetry.data}
      timeline={timeline.data}
    />
  );
}

function Message({ children, testId }: { children: React.ReactNode; testId: string }): React.JSX.Element {
  return (
    <p data-testid={testId} className="text-sm text-muted-foreground">
      {children}
    </p>
  );
}
