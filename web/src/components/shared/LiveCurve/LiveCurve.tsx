/**
 * LiveCurve — the shared five-series roast chart (component plan §7,
 * ui-prompts.md prompts A & C). Consumed read-only by the dashboard (live, SSE
 * append) and the detail page (full persisted curve). uPlot, not Recharts.
 *
 * Five series:
 *   - bean °C, env °C   → left temperature axis (controlled-dynamic auto-range, #307)
 *   - RoR °C/min        → right axis (FIXED band, comparable across roasts)
 *   - heat %, fan %      → a dedicated, fixed 0–100 % axis (#307), drawn as
 *                          SUBORDINATE step-after lines (thin / dashed / muted, amber
 *                          `--roast-heat` / teal `--roast-fan`) BEHIND the temperature
 *                          and RoR curves, so the operator reads the control HISTORY
 *                          (when heat was cut, fan moved) without it competing with the
 *                          measurement curves.
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
import { formatRoastTime, formatSeriesValue, toColumns } from "./chartData";
import { type AutoRangeState, makeAutoRange } from "./scales";
import {
  type ChartColumns,
  type ChartTestHook,
  type CurveMarker,
  type LiveCurveProps,
  type SeriesKey,
} from "./types";

// Default charge band, hoisted to module scope so the prop default is a STABLE
// reference. An inline `{ minC, maxC }` default would mint a new object every
// parent render, firing the overlay-redraw effect (keyed on `chargeBand`) on
// every 1 s telemetry tick. Performance-only.
const DEFAULT_CHARGE_BAND: { minC: number; maxC: number } = { minC: 170, maxC: 200 };

interface SeriesMeta {
  key: SeriesKey;
  label: string;
  /** uPlot scale key — temps share "c", RoR is "ror", heat/fan share "pct". */
  scale: string;
  stroke: string;
  /** Step-after rendering for the control lines (heat/fan). */
  step: boolean;
  /** Line width (px). The control lines (#307) are thinner than the measurements. */
  width: number;
  /** Dash pattern for subordinate control lines (heat/fan, #307); solid otherwise. */
  dash?: number[];
  /**
   * Draw order — LOWER draws FIRST (further back). The control lines (#307) sit
   * BEHIND bean/env/RoR so they don't compete with the measurement curves. uPlot
   * paints series in index order, so the plot's series/data arrays are permuted to
   * this order (see {@link PLOT_DRAW_ORDER}); the public column order is unchanged.
   */
  z: number;
}

// CSS custom properties resolve to the roast palette; uPlot needs concrete
// colors, so we read them off the document at mount.
function token(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function seriesMeta(): SeriesMeta[] {
  // The measurement curves (bean/env/RoR) draw on top (higher z); the control lines
  // (heat/fan, #307) draw behind (lower z), thinner + dashed, on the dedicated pct axis.
  return [
    { key: "bean", label: "Bean", scale: "c", stroke: token("--roast-coffee", "#d97706"), step: false, width: 2, z: 4 },
    { key: "env", label: "Env", scale: "c", stroke: token("--muted-foreground", "#d4d4d8"), step: false, width: 2, z: 3 },
    { key: "ror", label: "RoR", scale: "ror", stroke: token("--roast-nominal", "#34d399"), step: false, width: 2, z: 2 },
    { key: "heat", label: "Heat", scale: "pct", stroke: token("--roast-heat", "#fbbf24"), step: true, width: 1.25, dash: [4, 3], z: 0 },
    { key: "fan", label: "Fan", scale: "pct", stroke: token("--roast-fan", "#22d3ee"), step: true, width: 1.25, dash: [4, 3], z: 1 },
  ];
}

/**
 * Permutation from PLOT (draw) order → LOGICAL (column / `SERIES_KEYS`) order, #307.
 *
 * uPlot paints series in their array index order (later = on top) and ties each
 * series to the SAME-index data column. To draw the control lines behind the
 * measurements WITHOUT disturbing the public column order (the test hook, markers,
 * and every `columns[1]===bean` assertion key off the logical order), we permute the
 * series-options AND data arrays handed to uPlot by ascending `z`, then translate the
 * plot-slot index back to the logical series index for visibility toggles.
 *
 * @param meta - series metadata in LOGICAL order.
 * @returns logical series indices, ordered by ascending `z` (plot slot → logical idx).
 */
function plotDrawOrder(meta: SeriesMeta[]): number[] {
  return meta.map((_, i) => i).sort((a, b) => meta[a].z - meta[b].z);
}

/**
 * Permute LOGICAL columns ([x, …series in SERIES_KEYS order]) into PLOT order so the
 * series at plot slot `k` reads data column `order[k]` (#307). Column 0 (x) is shared
 * by all series and stays first; the five series columns follow in `order`.
 */
function toPlotData(columns: ChartColumns, order: number[]): uPlot.AlignedData {
  const [x, ...seriesCols] = columns;
  return [x, ...order.map((logicalIdx) => seriesCols[logicalIdx])] as unknown as uPlot.AlignedData;
}

export function LiveCurve({
  points,
  markers = [],
  phase = null,
  chargeBand = DEFAULT_CHARGE_BAND,
  highlightTime = null,
  originSeconds = null,
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

  // Plot draw order (#307): heat/fan draw BEHIND bean/env/RoR. This permutes the
  // series + data arrays handed to uPlot (later-index = on top) while the public
  // `columns` stay in logical order. Stable for the plot's lifetime (meta is stable).
  const drawOrder = useMemo(() => plotDrawOrder(meta), [meta]);

  // Hysteresis state for the controlled-dynamic temperature axis (#307). One per
  // mounted plot, carried in a ref so it survives data-only setData re-ranges (the
  // range callback reads + updates it across frames). The settled range deliberately
  // carries over if the plot is rebuilt on a height change — the data hasn't changed,
  // so the carried-over range is the right starting point for the new plot.
  const autoRangeRef = useRef<AutoRangeState>({ tempRange: null });

  // The uPlot `draw` hook is created once in the plot-build effect (keyed on
  // [height, meta]); if it closed over markers/highlightTime/chargeBand directly
  // it would repaint STALE values when those props change without a rebuild (the
  // redraw effect below re-invokes the same closure). Read them through a ref
  // that every render keeps current instead.
  const overlayRef = useRef({ markers, highlightTime, chargeBandVisible, chargeBand });
  overlayRef.current = { markers, highlightTime, chargeBandVisible, chargeBand };

  // The x-axis `values` formatter is created once in the plot-build effect (keyed
  // on [height, meta]) but must label ticks against the LIVE charge origin (#326):
  // origin starts null (preheat, serve-elapsed labels) and becomes the T0 serve-
  // elapsed once charge lands. Read it through a ref so the formatter re-labels to
  // roast time on the next redraw without rebuilding the plot; the effect below
  // forces that redraw when `originSeconds` changes.
  const originRef = useRef<number | null>(originSeconds);
  originRef.current = originSeconds;

  // The auto-range callback (built once with the plot) must derive the temp/x extent
  // from the LOGICAL columns (canonical [x, bean, env, ror, heat, fan]), NOT the
  // permuted `self.data` it would otherwise scan (#341 — post-permutation, plot
  // columns 1,2 are heat/fan, so the temp axis would range over the 0–100 % control
  // lines). Read the live logical columns through this ref each frame.
  const columnsRef = useRef(columns);
  columnsRef.current = columns;

  // (Re)build the plot when structural inputs change. Data-only updates go
  // through setData below to avoid tearing down the canvas every tick.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    // The temperature axis is controlled-dynamic with hysteresis (#307) and the RoR
    // axis is fixed; the charge-band getter is passed only for API symmetry (the band
    // overlay reads it live off `overlayRef`, not the range). The hysteresis state ref
    // is threaded in so the temp range only moves when the data leaves the frame.
    const rangeFn = makeAutoRange(
      () => ({
        visible: overlayRef.current.chargeBandVisible,
        minC: overlayRef.current.chargeBand.minC,
        maxC: overlayRef.current.chargeBand.maxC,
      }),
      autoRangeRef.current,
      () => columnsRef.current,
    );

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
        // `rangeFn` policy per scale: x (time) re-ranges to the loaded data; c
        // (temperature) is controlled-dynamic auto-range with hysteresis (#307); ror
        // is FIXED (−20..+30, comparable across roasts).
        // The plot is built once (on [height, meta]) while the dashboard's live series
        // is still EMPTY (it mounts before SSE frames arrive), so uPlot leaves x
        // unranged — {min:null,max:null}, which collapsed the series to a single point
        // at index 0 (invisible). `setData` was not re-ranging it; the explicit `range`
        // callback recomputes x's min/max from the data uPlot is about to draw, so x
        // always covers the loaded elapsed-time range.
        // A `range` callback fires unconditionally, so `auto` has no effect and is
        // omitted (#133).
        x: { time: false, range: rangeFn },
        c: { range: rangeFn },
        ror: { range: rangeFn },
        // Dedicated FIXED 0–100 % scale for the control lines (#307) — its own axis
        // (below), so heat/fan read as percentages and never ride the temp/RoR scale.
        pct: { range: [0, 100] },
      },
      axes: [
        {
          stroke: token("--muted-foreground", "#a1a1aa"),
          grid: { show: true, stroke: token("--border", "#3f3f46") },
          // The x is SERVE-elapsed SECONDS; render the tick labels as CHARGE-
          // referenced ROAST TIME (#326) — 0:00 = charge, negative in preheat —
          // via `formatRoastTime(v, origin)`. Before T0 lands (origin null) this
          // falls back to serve-elapsed M:SS (#153). Display-only; the data stays
          // serve seconds. `time:false` on the x scale, so uPlot passes raw
          // numeric splits here.
          values: (_self, splits) =>
            splits.map((v) => formatRoastTime(v, originRef.current)),
        },
        { scale: "c", stroke: token("--muted-foreground", "#a1a1aa"), grid: { show: false } },
        { scale: "ror", side: 1, stroke: token("--roast-nominal", "#34d399"), grid: { show: false } },
        // Dedicated 0–100 % axis for the control lines (#307), drawn SUBTLY on the
        // right (below the RoR axis) so heat/fan read as their own % scale without
        // competing with the measurement axes: muted stroke, ticks every 25 %, no grid.
        {
          scale: "pct",
          side: 1,
          stroke: token("--muted-foreground", "#71717a"),
          grid: { show: false },
          ticks: { show: false },
          size: 34,
          splits: [0, 25, 50, 75, 100],
          values: (_self, splits) => splits.map((v) => `${v}%`),
          font: "10px ui-sans-serif, system-ui, sans-serif",
        },
      ],
      // Series + data are handed to uPlot in PLOT (draw) order (#307) so the control
      // lines paint BEHIND bean/env/RoR; the public `columns`/test-hook stay logical.
      series: [
        {},
        ...drawOrder.map((logicalIdx) => {
          const m = meta[logicalIdx];
          return {
            label: m.label,
            scale: m.scale,
            stroke: m.stroke,
            width: m.width,
            dash: m.dash,
            paths: m.step ? uPlot.paths.stepped?.({ align: 1 }) : undefined,
            show: visible[m.key],
            points: { show: false },
          };
        }),
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

    const plot = new uPlot(opts, toPlotData(columns, drawOrder), host);
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

  // Data updates without tearing down the plot — permuted into plot/draw order (#307).
  useEffect(() => {
    plotRef.current?.setData(toPlotData(columns, drawOrder));
  }, [columns, drawOrder]);

  // Toggle series visibility on the live plot. The series live in PLOT order (#307),
  // so plot slot `k+1` is the logical series `drawOrder[k]`.
  useEffect(() => {
    const plot = plotRef.current;
    if (!plot) return;
    drawOrder.forEach((logicalIdx, k) =>
      plot.setSeries(k + 1, { show: visible[meta[logicalIdx].key] }),
    );
  }, [visible, meta, drawOrder]);

  // Redraw overlays (markers / highlight) when they change — a plain repaint.
  useEffect(() => {
    plotRef.current?.redraw();
  }, [markers, highlightTime]);

  // When the charge origin lands (or changes), the x-axis tick formatter must
  // re-label preheat negative and 0:00 at charge (#326). The formatter reads
  // `originRef` live, so a plain redraw re-runs the axis `values` callback — no
  // rebuild or setData needed (point positions don't move, only their labels).
  useEffect(() => {
    plotRef.current?.redraw();
  }, [originSeconds]);

  // The band is a canvas overlay drawn in the `draw` hook, not a data series — so
  // `setData` is never needed. When the band appears/disappears or moves, `redraw()`
  // re-fires the `draw` hook (same mechanism as the markers/highlight effect above).
  useEffect(() => {
    plotRef.current?.redraw();
  }, [chargeBandVisible, chargeBand]);

  // Expose the test hook — assert DATA + the rendered scale ranges (D24 / #131).
  //
  // x/ror/pct are read off `plotRef.scales` (x re-ranges to data each setData; ror/pct
  // are fixed, so they're never stale). The °C (`c`) scale, though, is the
  // controlled-dynamic auto-range (#307), and reading `plotRef.scales.c` here proved
  // RACY in CI (#341): it assumes uPlot has SYNCHRONOUSLY committed the new c-range to
  // `scales.c` by the time this effect runs after `setData`. On slower CI render
  // timing that commit lagged, so the hook published a STALE preheat range
  // (c.max ≈ 60) even with the full developed curve loaded — failing the
  // `c.max >= beanMax` assertion deterministically in CI but never locally.
  //
  // The fix: publish `c` from `autoRangeRef.current.tempRange` — the range the
  // auto-range CALLBACK authoritatively computed on the last setData. uPlot is
  // guaranteed to invoke the range callback during `setData` (that's how a `range`
  // function works), and the setData effect above runs before this one (declaration
  // order), so `tempRange` always reflects the current data here — no dependence on
  // when uPlot mirrors it into `scales.c`. This keeps the #131 scale-covers-data
  // guarantee while removing the timing assumption that broke in CI.
  useEffect(() => {
    const plot = plotRef.current;
    const sx = plot?.scales.x;
    const sror = plot?.scales.ror;
    const spct = plot?.scales.pct;
    const tc = autoRangeRef.current.tempRange;
    const hook: ChartTestHook = {
      columns,
      visible,
      markers,
      highlightTime,
      chargeBandVisible,
      scales: {
        x: { min: sx?.min ?? null, max: sx?.max ?? null },
        // °C from the auto-range source of truth, not the possibly-stale uPlot scale.
        c: { min: tc?.[0] ?? null, max: tc?.[1] ?? null },
        ror: { min: sror?.min ?? null, max: sror?.max ?? null },
        pct: { min: spct?.min ?? null, max: spct?.max ?? null },
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
        originSeconds={originSeconds}
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
  /** Serve-elapsed seconds at the readout index (cursor, else latest point);
   *  rendered as roast time via `formatRoastTime` against `originSeconds`. */
  readoutTime: number | null;
  /** Serve-elapsed at the T0/charge moment (#326); null → serve-elapsed display. */
  originSeconds: number | null;
  onToggle: (key: SeriesKey) => void;
}

/** Color-keyed legend that doubles as the cursor value readout + toggle control. */
function Legend({ meta, visible, readout, readoutTime, originSeconds, onToggle }: LegendProps): React.JSX.Element {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" data-testid="live-curve-legend">
      {/* The cursor/latest-point time as CHARGE-referenced roast time (#326) —
          same transform as the x-axis ticks (0:00 = charge, negative in preheat). */}
      <span className="numeric font-medium text-muted-foreground" data-testid="legend-time">
        {formatRoastTime(readoutTime, originSeconds)}
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
