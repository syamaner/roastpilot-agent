/**
 * Presentational detail view (E10-S5).
 *
 * The pure composition of the detail page: given the three REST payloads it
 * derives the trace rows + curve + headline stats and lays out the screen. It
 * owns the `selectedTick` → `LiveCurve.highlightTime` wiring (trace-row click
 * highlights the matching timestamp on the curve; re-clicking the same row
 * toggles it off). Kept free of the query layer so the snapshot harness can feed
 * it fixed REST-shaped data for deterministic Playwright baselines.
 *
 * The curve is the SHARED `LiveCurve`, consumed read-only — no re-implementation.
 * Phase is `complete`/`faulted` from the persisted snapshot (no charge band on a
 * finished roast); this page renders persisted server truth, never live state.
 */

import { useMemo, useState } from "react";

import { LiveCurve } from "@/components/shared";
import type { RoastDetail, RoastTimeline, TelemetrySeries } from "@/lib/types";
// #205: shared DISPLAY-ONLY RoR smoothing (dashboard owns the canonical module).
// Raw `bean_ror_c_per_min` in the contract is untouched — this only smooths the
// rendered line, identically on the live dashboard and this persisted detail curve.
import { smoothRorForDisplay } from "@/pages/dashboard/rorSmoothing";
import { AdvisorSummaryChips } from "./AdvisorSummaryChips";
import { AdvisorTimeline } from "./AdvisorTimeline";
import { advisorSummary, toAdvisorRows } from "./advisorModel";
import { DecisionTraceTable } from "./DecisionTraceTable";
import { EventTimeline } from "./EventTimeline";
import { ExportOptions } from "./ExportOptions";
import { RoastRating } from "./RoastRating";
import { TitleBlock } from "./TitleBlock";
import {
  headlineStats,
  tickToSeconds,
  toCurveMarkers,
  toCurvePoints,
  toTraceRows,
} from "./traceModel";

export interface DetailViewProps {
  detail: RoastDetail;
  telemetry: TelemetrySeries | undefined;
  timeline: RoastTimeline | undefined;
}

export function DetailView({ detail, telemetry, timeline }: DetailViewProps): React.JSX.Element {
  // The single source of truth for the trace-row → curve highlight. A row click
  // sets its tick; clicking the same tick again clears it (toggle-off on
  // re-click). The page owns the toggle; the chart just renders `highlightTime`.
  const [selectedTick, setSelectedTick] = useState<number | null>(null);

  // #205: smooth RoR for display only; the persisted raw series is unchanged.
  const points = useMemo(() => smoothRorForDisplay(toCurvePoints(telemetry)), [telemetry]);
  const markers = useMemo(() => toCurveMarkers(timeline, telemetry), [timeline, telemetry]);
  const rows = useMemo(() => toTraceRows(timeline, telemetry), [timeline, telemetry]);
  const advisorRows = useMemo(() => toAdvisorRows(timeline, telemetry), [timeline, telemetry]);
  const advisor = useMemo(() => advisorSummary(timeline), [timeline]);
  const stats = useMemo(() => headlineStats(timeline, telemetry), [timeline, telemetry]);

  const highlightTime =
    selectedTick === null ? null : tickToSeconds(telemetry, selectedTick);

  const onSelectRow = (tick: number) =>
    setSelectedTick((current) => (current === tick ? null : tick));

  return (
    <div className="flex flex-col gap-6" data-testid="detail-view">
      <TitleBlock detail={detail} stats={stats} />

      <div className="rounded-lg border border-border bg-card p-3">
        <LiveCurve
          points={points}
          markers={markers}
          phase={detail.agent_phase}
          highlightTime={highlightTime}
        />
      </div>

      <section className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold tracking-tight">Advisor decisions</h2>
          <AdvisorSummaryChips summary={advisor} />
        </div>
        <AdvisorTimeline
          rows={advisorRows}
          selectedTick={selectedTick}
          onSelect={onSelectRow}
        />
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Decision trace</h2>
        <DecisionTraceTable rows={rows} selectedTick={selectedTick} onSelect={onSelectRow} />
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <section className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold tracking-tight">Timeline</h2>
          <EventTimeline
            timeline={timeline}
            tickToSeconds={(tick) => tickToSeconds(telemetry, tick)}
          />
        </section>
        <div className="flex flex-col gap-6">
          <RoastRating runId={detail.id} rating={detail.rating} notes={detail.notes} />
          <ExportOptions runId={detail.id} manifest={detail.export_manifest} />
        </div>
      </div>
    </div>
  );
}
