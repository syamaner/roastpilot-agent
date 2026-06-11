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
import { formatSeriesValue, toColumns } from "./chartData";
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

// Column indices per scale (x, bean, env, ror, heat, fan). The x scale is the
// elapsed-seconds column; the °C scale "c" is fed by bean+env; "ror" by the RoR
// column. Used by `autoRange` to recompute a scale's extent from the data uPlot
// currently holds.
const SCALE_COLUMNS: Record<string, number[]> = {
  x: [0],
  c: [1, 2],
  ror: [3],
};

/**
 * uPlot `scales.<key>.range` callback that ALWAYS fits the current data.
 *
 * The plot is built ONCE (on [height, meta]) while the live series is still
 * EMPTY — the dashboard mounts LiveCurve before any SSE frame arrives — so uPlot
 * initialises every scale with no data and leaves the range unset (`x` ended up
 * `{min:null,max:null}`, which made the series draw a single point at index 0:
 * invisible). `setData` streams the real data in but was NOT re-ranging those
 * scales. This callback recomputes each scale's extent from `self.data` on every
 * setData, so x/°C/RoR always cover what is loaded.
 *
 * x is ranged tight (no padding — it is the time axis); the value scales get
 * uPlot's normal soft padding via `rangeNum` so they look like a default auto scale.
 */
function autoRange(self: uPlot, _min: number, _max: number, scaleKey: string): uPlot.Range.MinMax {
  const cols = SCALE_COLUMNS[scaleKey] ?? [];
  let lo = Infinity;
  let hi = -Infinity;
  for (const ci of cols) {
    const series = self.data[ci];
    if (!series) continue;
    for (const v of series) {
      if (v == null || !Number.isFinite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  // No finite data yet (empty mount): let uPlot keep whatever it passed.
  if (lo === Infinity || hi === -Infinity) return [_min, _max];
  // The x (time) axis is ranged tight; value axes get uPlot's soft padding.
  if (scaleKey === "x") return [lo, hi];
  return uPlot.rangeNum(lo, hi, 0.1, true);
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
        // Every data scale re-ranges to the CURRENT data on each setData via
        // `autoRange`. The plot is built once (on [height, meta]) while the
        // dashboard's live series is still EMPTY (it mounts before SSE frames
        // arrive), so uPlot left the scales unranged — x ended up {min:null,max:null},
        // which collapsed the series to a single point at index 0 (invisible), and
        // the °C scale stayed pinned to the early preheating range. `setData` was not
        // re-ranging them; the explicit `range` callback recomputes min/max from the
        // data uPlot is about to draw, so x/°C/RoR always cover what is loaded.
        x: { time: false, range: autoRange },
        c: { auto: true, range: autoRange },
        ror: { auto: true, range: autoRange },
        // Hidden 0–100 % scale for the control lines — fixed range, no axis.
        pct: { range: [0, 100] },
      },
      axes: [
        { stroke: token("--muted-foreground", "#a1a1aa"), grid: { show: true, stroke: token("--border", "#3f3f46") } },
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

  // Redraw overlays (markers / highlight / charge band) when they change.
  useEffect(() => {
    plotRef.current?.redraw();
  }, [markers, highlightTime, chargeBandVisible, chargeBand]);

  // Expose the test hook — assert DATA + the rendered scale ranges (D24 / #131).
  // The `setData` effect above re-ranges the scales synchronously before this
  // effect runs (effects fire in declaration order), so `plotRef.scales` here
  // reflects the just-drawn ranges — letting a test assert the scale COVERS the
  // data (catching the collapsed/unranged-scale bug a blank snapshot can't).
  useEffect(() => {
    const plot = plotRef.current;
    const sx = plot?.scales.x;
    const sc = plot?.scales.c;
    const hook: ChartTestHook = {
      columns,
      visible,
      markers,
      highlightTime,
      chargeBandVisible,
      scales: {
        x: { min: sx?.min ?? null, max: sx?.max ?? null },
        c: { min: sc?.min ?? null, max: sc?.max ?? null },
      },
    };
    if (typeof window !== "undefined") window.__chart = hook;
  }, [columns, visible, markers, highlightTime, chargeBandVisible]);

  const toggle = (key: SeriesKey) =>
    setVisible((prev) => ({ ...prev, [key]: !prev[key] }));

  const readoutIdx = cursorIdx ?? (points.length > 0 ? points.length - 1 : null);

  return (
    <div className={cn("flex flex-col gap-2", className)} data-testid="live-curve">
      <Legend
        meta={meta}
        visible={visible}
        readout={readoutIdx === null ? null : points[readoutIdx] ?? null}
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
  onToggle: (key: SeriesKey) => void;
}

/** Color-keyed legend that doubles as the cursor value readout + toggle control. */
function Legend({ meta, visible, readout, onToggle }: LegendProps): React.JSX.Element {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs" data-testid="live-curve-legend">
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
