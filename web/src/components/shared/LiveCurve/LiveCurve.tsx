/**
 * LiveCurve — the shared five-series roast chart (component plan §7,
 * ui-prompts.md prompts A & C). Consumed read-only by the dashboard (live, SSE
 * append) and the detail page (full persisted curve). uPlot, not Recharts.
 *
 * Five series:
 *   - bean °C, env °C   → left temperature axis
 *   - RoR °C/min        → right axis
 *   - heat %, fan %      → a HIDDEN 0–100 % scale, drawn as step-after lines
 *                          (amber `--roast-heat` / teal `--roast-fan`), so control
 *                          changes correlate with the temperature response.
 *
 * The legend doubles as a live cursor readout (value at the hovered time) AND a
 * click-to-toggle control (hide/show a series). Vertical markers label T0 /
 * first crack / drop; a shaded charge band shows in `preheating` only; a
 * controlled `highlightTime` draws the trace-row → curve highlight.
 *
 * Tests assert the chart's DATA via `window.__chart` + `data-chart-*`, never the
 * canvas pixels (D24).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

import { cn } from "@/lib/cn";
import { formatElapsed, formatSeriesValue, toColumns } from "./chartData";
import { makeAutoRange } from "./scales";
import {
  type ChartTestHook,
  type CurveMarker,
  type LiveCurveProps,
  type SeriesKey,
} from "./types";

interface SeriesMeta {
  key: SeriesKey;
  label: string;
  /** uPlot scale key — temps share "c", RoR is "ror", heat/fan share "pct". */
  scale: string;
  stroke: string;
  /** Step-after rendering for the control lines (heat/fan). */
  step: boolean;
}

// CSS custom properties resolve to the roast palette; uPlot needs concrete
// colors, so we read them off the document at mount.
function token(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function seriesMeta(): SeriesMeta[] {
  return [
    { key: "bean", label: "Bean", scale: "c", stroke: token("--roast-coffee", "#d97706"), step: false },
    { key: "env", label: "Env", scale: "c", stroke: token("--muted-foreground", "#d4d4d8"), step: false },
    { key: "ror", label: "RoR", scale: "ror", stroke: token("--roast-nominal", "#34d399"), step: false },
    { key: "heat", label: "Heat", scale: "pct", stroke: token("--roast-heat", "#fbbf24"), step: true },
    { key: "fan", label: "Fan", scale: "pct", stroke: token("--roast-fan", "#22d3ee"), step: true },
  ];
}

export function LiveCurve({
  points,
  markers = [],
  phase = null,
  chargeBand = { minC: 170, maxC: 200 },
  highlightTime = null,
  initialHidden = [],
  className,
  height = 420,
}: LiveCurveProps): React.JSX.Element {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const plotRef = useRef<uPlot | null>(null);
  const meta = useMemo(seriesMeta, []);

  const [visible, setVisible] = useState<Record<SeriesKey, boolean>>(() => {
    const base: Record<SeriesKey, boolean> = {
      bean: true,
      env: true,
      ror: true,
      heat: true,
      fan: true,
    };
    for (const key of initialHidden) base[key] = false;
    return base;
  });
  // Cursor readout: the values at the hovered index (null when not hovering →
  // the legend shows the latest point).
  const [cursorIdx, setCursorIdx] = useState<number | null>(null);

  const columns = useMemo(() => toColumns(points), [points]);
  const chargeBandVisible = phase === "preheating";

  // The uPlot `draw` hook is created once in the plot-build effect (keyed on
  // [height, meta]); if it closed over markers/highlightTime/chargeBand directly
  // it would repaint STALE values when those props change without a rebuild (the
  // redraw effect below re-invokes the same closure). Read them through a ref
  // that every render keeps current instead.
  const overlayRef = useRef({ markers, highlightTime, chargeBandVisible, chargeBand });
  overlayRef.current = { markers, highlightTime, chargeBandVisible, chargeBand };

  // (Re)build the plot when structural inputs change. Data-only updates go
  // through setData below to avoid tearing down the canvas every tick.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    // The value axes are FIXED (#217), so the °C range no longer depends on the live
    // charge band (the 0–210 range always contains the 170–200 band). The charge-band
    // getter is passed only for API symmetry — `makeAutoRange` ignores it — and the
    // band is still read live off `overlayRef` by the `draw` overlay, not the range.
    const rangeFn = makeAutoRange(() => ({
      visible: overlayRef.current.chargeBandVisible,
      minC: overlayRef.current.chargeBand.minC,
      maxC: overlayRef.current.chargeBand.maxC,
    }));

    const opts: uPlot.Options = {
      width: host.clientWidth || 800,
      height,
      // Cursor drives the legend readout; we mirror the hovered index to state.
      cursor: {
        focus: { prox: 16 },
        sync: { key: "roast" },
      },
      legend: { show: false }, // we render our own legend (readout + toggle)
      scales: {
        // `rangeFn` (#217) pins the two VALUE scales to fixed ranges (c → 0–210 °C,
        // ror → −20..+30 °C/min) so they never auto-zoom to the current sensor reading,
        // and re-ranges only the x (time) scale to the loaded data on each setData.
        // The plot is built once (on [height, meta]) while the dashboard's live series
        // is still EMPTY (it mounts before SSE frames arrive), so uPlot leaves x
        // unranged — {min:null,max:null}, which collapsed the series to a single point
        // at index 0 (invisible). `setData` was not re-ranging it; the explicit `range`
        // callback recomputes x's min/max from the data uPlot is about to draw, so x
        // always covers the loaded elapsed-time range (the fixed c/ror always cover the
        // roast).
        x: { time: false, range: rangeFn },
        c: { auto: true, range: rangeFn },
        ror: { auto: true, range: rangeFn },
        // Hidden 0–100 % scale for the control lines — fixed range, no axis.
        pct: { range: [0, 100] },
      },
      axes: [
        {
          stroke: token("--muted-foreground", "#a1a1aa"),
          grid: { show: true, stroke: token("--border", "#3f3f46") },
          // The x is roast-elapsed SECONDS; render the tick labels as M:SS
          // (720 → 12:00) like roasting tools — display-only, the data stays
          // seconds (#153). `time:false` on the x scale, so uPlot passes raw
          // numeric splits here.
          values: (_self, splits) => splits.map((v) => formatElapsed(v)),
        },
        { scale: "c", stroke: token("--muted-foreground", "#a1a1aa"), grid: { show: false } },
        { scale: "ror", side: 1, stroke: token("--roast-nominal", "#34d399"), grid: { show: false } },
        // No axis for the hidden pct scale.
      ],
      series: [
        {},
        ...meta.map((m) => ({
          label: m.label,
          scale: m.scale,
          stroke: m.stroke,
          width: m.step ? 1.5 : 2,
          paths: m.step ? uPlot.paths.stepped?.({ align: 1 }) : undefined,
          show: visible[m.key],
          points: { show: false },
        })),
      ],
      hooks: {
        setCursor: [
          (u: uPlot) => {
            setCursorIdx(u.cursor.idx ?? null);
          },
        ],
        draw: [
          (u: uPlot) => {
            const o = overlayRef.current;
            drawOverlays(u, o.markers, o.highlightTime, o.chargeBandVisible, o.chargeBand);
          },
        ],
      },
    };

    const plot = new uPlot(opts, columns as unknown as uPlot.AlignedData, host);
    plotRef.current = plot;

    const onResize = () => plot.setSize({ width: host.clientWidth || 800, height });
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      plot.destroy();
      plotRef.current = null;
    };
    // Markers/highlight/charge changes re-run draw via the data effect below by
    // forcing a redraw; structural rebuild only depends on these:
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [height, meta]);

  // Data updates without tearing down the plot.
  useEffect(() => {
    plotRef.current?.setData(columns as unknown as uPlot.AlignedData);
  }, [columns]);

  // Toggle series visibility on the live plot.
  useEffect(() => {
    const plot = plotRef.current;
    if (!plot) return;
    meta.forEach((m, i) => plot.setSeries(i + 1, { show: visible[m.key] }));
  }, [visible, meta]);

  // Redraw overlays (markers / highlight) when they change — a plain repaint.
  useEffect(() => {
    plotRef.current?.redraw();
  }, [markers, highlightTime]);

  // The °C range is fixed (#217), so the band overlay no longer re-ranges the scale —
  // but when it appears/disappears or moves, the canvas overlay (drawn in the `draw`
  // hook) must repaint. A plain `redraw()` fires that draw hook (same mechanism as the
  // markers/highlight effect above); no `setData` round-trip is needed now the range
  // is fixed.
  useEffect(() => {
    plotRef.current?.redraw();
  }, [chargeBandVisible, chargeBand]);

  // Expose the test hook — assert DATA + the rendered scale ranges (D24 / #131).
  // The `setData` effect above re-ranges the scales synchronously before this
  // effect runs (effects fire in declaration order), so `plotRef.scales` here
  // reflects the just-drawn ranges — letting a test assert the scale COVERS the
  // data (catching the collapsed/unranged-scale bug a blank snapshot can't).
  useEffect(() => {
    const plot = plotRef.current;
    const sx = plot?.scales.x;
    const sc = plot?.scales.c;
    const sror = plot?.scales.ror;
    const hook: ChartTestHook = {
      columns,
      visible,
      markers,
      highlightTime,
      chargeBandVisible,
      scales: {
        x: { min: sx?.min ?? null, max: sx?.max ?? null },
        c: { min: sc?.min ?? null, max: sc?.max ?? null },
        ror: { min: sror?.min ?? null, max: sror?.max ?? null },
      },
    };
    if (typeof window !== "undefined") window.__chart = hook;
  }, [columns, visible, markers, highlightTime, chargeBandVisible]);

  const toggle = (key: SeriesKey) =>
    setVisible((prev) => ({ ...prev, [key]: !prev[key] }));

  const readoutIdx = cursorIdx ?? (points.length > 0 ? points.length - 1 : null);
  const readoutPoint = readoutIdx === null ? null : points[readoutIdx] ?? null;

  return (
    <div className={cn("flex flex-col gap-2", className)} data-testid="live-curve">
      <Legend
        meta={meta}
        visible={visible}
        readout={readoutPoint}
        readoutTime={readoutPoint?.t ?? null}
        onToggle={toggle}
      />
      <div
        ref={hostRef}
        data-chart-ready="true"
        data-charge-band={chargeBandVisible ? "true" : "false"}
        data-marker-count={markers.length}
        data-highlight={highlightTime ?? ""}
        className="w-full"
        style={{ height }}
      />
    </div>
  );
}

interface LegendProps {
  meta: SeriesMeta[];
  visible: Record<SeriesKey, boolean>;
  readout: { bean: number | null; env: number | null; ror: number | null; heat: number | null; fan: number | null } | null;
  /** Elapsed seconds at the readout index (cursor, else latest point); rendered M:SS. */
  readoutTime: number | null;
  onToggle: (key: SeriesKey) => void;
}

/** Color-keyed legend that doubles as the cursor value readout + toggle control. */
function Legend({ meta, visible, readout, readoutTime, onToggle }: LegendProps): React.JSX.Element {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" data-testid="live-curve-legend">
      {/* The cursor/latest-point time, M:SS (#153) — same format as the x-axis ticks. */}
      <span className="numeric font-medium text-muted-foreground" data-testid="legend-time">
        {formatElapsed(readoutTime)}
      </span>
      {meta.map((m) => {
        const value = readout ? readout[m.key] : null;
        return (
          <button
            key={m.key}
            type="button"
            data-testid={`legend-${m.key}`}
            data-series={m.key}
            data-visible={visible[m.key] ? "true" : "false"}
            aria-pressed={visible[m.key]}
            onClick={() => onToggle(m.key)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded px-1 py-0.5 transition-opacity",
              visible[m.key] ? "opacity-100" : "opacity-40",
            )}
          >
            <span className="size-2 rounded-sm" style={{ background: m.stroke }} aria-hidden />
            <span className="font-medium">{m.label}</span>
            <span className="numeric text-muted-foreground">
              {formatSeriesValue(m.key, value)}
            </span>
          </button>
        );
      })}
    </div>
  );
}

/** Draw the charge band, event markers, and the trace highlight onto the canvas. */
function drawOverlays(
  u: uPlot,
  markers: CurveMarker[],
  highlightTime: number | null,
  chargeBandVisible: boolean,
  chargeBand: { minC: number; maxC: number },
): void {
  const ctx = u.ctx;
  ctx.save();

  if (chargeBandVisible) {
    const yTop = u.valToPos(chargeBand.maxC, "c", true);
    const yBot = u.valToPos(chargeBand.minC, "c", true);
    ctx.fillStyle = "rgba(148, 163, 184, 0.12)"; // --roast-phase-preheat @ low alpha
    ctx.fillRect(u.bbox.left, yTop, u.bbox.width, yBot - yTop);
  }

  for (const marker of markers) {
    const x = u.valToPos(marker.t, "x", true);
    ctx.strokeStyle = "rgba(212, 212, 216, 0.6)";
    ctx.lineWidth = 1;
    line(ctx, x, u.bbox.top, x, u.bbox.top + u.bbox.height);
    // Label the marker (T0 / FIRST CRACK / DROP) at the top of its line —
    // ui-prompts.md Prompt A requires labeled event markers, not bare lines.
    ctx.fillStyle = "rgba(212, 212, 216, 0.9)";
    ctx.font = "10px ui-sans-serif, system-ui, sans-serif";
    ctx.textBaseline = "top";
    ctx.textAlign = "left";
    ctx.fillText(marker.label, x + 3, u.bbox.top + 2);
  }

  if (highlightTime !== null) {
    const x = u.valToPos(highlightTime, "x", true);
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    line(ctx, x, u.bbox.top, x, u.bbox.top + u.bbox.height);
  }

  ctx.restore();
}

function line(ctx: CanvasRenderingContext2D, x0: number, y0: number, x1: number, y1: number): void {
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x1, y1);
  ctx.stroke();
}
