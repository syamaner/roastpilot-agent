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
// #205/#344: shared DISPLAY-ONLY curve smoothing (lib/ is the canonical home, #244).
// Raw `bean_temp_c` / `bean_ror_c_per_min` in the contract are untouched — this only
// smooths the rendered bean + RoR lines, identically on the live dashboard and this
// persisted detail curve.
import { smoothCurveForDisplay } from "@/lib/rorSmoothing";
import type { RoastDetail, RoastTimeline, TelemetrySeries } from "@/lib/types";
import { AdvisorSummaryChips } from "./AdvisorSummaryChips";
import { AdvisorTimeline } from "./AdvisorTimeline";
import { advisorSummary, toAdvisorRows } from "./advisorModel";
import { CappedList } from "./CappedList";
import { ChargeWeight } from "./ChargeWeight";
import { DecisionTraceTable } from "./DecisionTraceTable";
import { EventTimeline } from "./EventTimeline";
import { ExportOptions } from "./ExportOptions";
import { RoastConditions } from "./RoastConditions";
import { RoastRating } from "./RoastRating";
import { RoastedWeight } from "./RoastedWeight";
import { RoastTastings } from "./RoastTastings";
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

  // #205/#344: smooth bean + RoR for display only; the persisted raw series is unchanged.
  const points = useMemo(() => smoothCurveForDisplay(toCurvePoints(telemetry)), [telemetry]);
  const markers = useMemo(() => toCurveMarkers(timeline, telemetry), [timeline, telemetry]);
  const rows = useMemo(() => toTraceRows(timeline, telemetry), [timeline, telemetry]);
  const advisorRows = useMemo(() => toAdvisorRows(timeline, telemetry), [timeline, telemetry]);
  const advisor = useMemo(() => advisorSummary(timeline), [timeline]);
  const stats = useMemo(() => headlineStats(timeline, telemetry), [timeline, telemetry]);

  const highlightTime =
    selectedTick === null ? null : tickToSeconds(telemetry, selectedTick);

  const onSelectRow = (tick: number) =>
    setSelectedTick((current) => (current === tick ? null : tick));

  // Selecting a row that lives only in a "View all" modal would point the curve
  // highlight off-screen behind the overlay (#126). When the selection happens
  // inside the modal we close it first, so the highlighted curve (top of the page)
  // is in frame; an inline selection toggles as before.
  const selectFrom =
    (close: () => void, inModal: boolean) =>
    (tick: number) => {
      if (inModal) close();
      onSelectRow(tick);
    };

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
        <CappedList
          rows={advisorRows}
          modalTitle="Advisor decisions — full history"
          testId="advisor"
          modalTableTestId="advisor-timeline-modal"
          renderRows={(slice, ctx) => (
            <AdvisorTimeline
              rows={slice}
              selectedTick={selectedTick}
              onSelect={selectFrom(ctx.close, ctx.inModal)}
              tableTestId={ctx.tableTestId}
            />
          )}
        />
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Decision trace</h2>
        <CappedList
          rows={rows}
          modalTitle="Decision trace — full history"
          testId="trace"
          modalTableTestId="decision-trace-table-modal"
          renderRows={(slice, ctx) => (
            <DecisionTraceTable
              rows={slice}
              selectedTick={selectedTick}
              onSelect={selectFrom(ctx.close, ctx.inModal)}
              tableTestId={ctx.tableTestId}
            />
          )}
        />
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
          <RoastedWeight
            runId={detail.id}
            chargeWeightGrams={detail.profile.bean_weight_grams}
            roastedWeightGrams={detail.roasted_weight_grams ?? null}
            weightLossPercent={detail.weight_loss_percent ?? null}
          />
          <ChargeWeight
            runId={detail.id}
            frozenChargeGrams={detail.profile.bean_weight_grams}
            correctedChargeGrams={detail.corrected_charge_grams ?? null}
            roastedWeightGrams={detail.roasted_weight_grams ?? null}
            weightLossPercent={detail.weight_loss_percent ?? null}
          />
          {/* key={detail.id}: RoastTastings' draft (unlike RoastRating/
              RoastedWeight) has no persisted value to re-sync from on a prop
              change, so without a remount a navigation between two runs would
              carry run A's unsaved draft into a POST against run B — a wrong
              corpus label (#522 Codex P2). */}
          <RoastTastings key={detail.id} runId={detail.id} />
          <RoastConditions
            ambientTempC={detail.ambient_temp_c}
            ambientHumidityPct={detail.ambient_humidity_pct}
            ambientPressureHpa={detail.ambient_pressure_hpa}
          />
          <ExportOptions runId={detail.id} manifest={detail.export_manifest} />
        </div>
      </div>
    </div>
  );
}
