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

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { AppFrame } from "@/components/shared";
import { roastKeys, useRoast, useTelemetry, useTimeline } from "@/hooks/queries";
import { DetailView } from "./DetailView";

export function DetailPage(): React.JSX.Element {
  const { runId = null } = useParams<{ runId: string }>();

  const detail = useRoast(runId);
  const telemetry = useTelemetry(runId);
  const timeline = useTimeline(runId);
  const queryClient = useQueryClient();
  const completedAtUtc = detail.data?.completed_at_utc;

  // A detail route may be opened while its roast is still live. `useRoast`
  // observes completion on its slow poll, but telemetry has a separate cache
  // key and otherwise remains the mid-roast series indefinitely. Refresh it
  // once at the terminal boundary so the post-roast D96 summary is complete.
  useEffect(() => {
    if (
      runId !== null &&
      completedAtUtc !== undefined &&
      completedAtUtc !== null
    ) {
      void queryClient.invalidateQueries({
        queryKey: roastKeys.telemetry(runId, 1),
        exact: true,
      });
    }
  }, [completedAtUtc, queryClient, runId]);

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
