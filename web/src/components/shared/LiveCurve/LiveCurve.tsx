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
        x: { time: false },
        c: {},
        ror: {},
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
        draw: [(u: uPlot) => drawOverlays(u, markers, highlightTime, chargeBandVisible, chargeBand)],
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

  // Expose the test hook — assert DATA, not pixels (D24).
  useEffect(() => {
    const hook: ChartTestHook = {
      columns,
      visible,
      markers,
      highlightTime,
      chargeBandVisible,
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
