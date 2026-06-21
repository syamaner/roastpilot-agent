/**
 * Playwright global-setup (D26) — verify every replay harness is reachable.
 *
 * Each agent (one per fixture) boots in `--replay <fixture> --step` mode, paused at
 * tick 0 with the gated `POST /api/replay/{step,advance-to}` control routes
 * mounted. We do NOT pre-advance here: each spec opens the SSE stream FIRST, then
 * steps, so the stepped frames flow to the live browser (window.__lastEventId is
 * set on applied SSE frames, not on hydration). This setup just confirms the
 * control surface is up on EVERY agent so a misconfigured harness fails fast, not
 * mid-spec.
 *
 * MULTI-FIXTURE: the dashboard states need different fixtures, so there are several
 * agents (see playwright.config.ts). The `advanceTo`/`step` helpers take the agent
 * base URL so a spec drives the agent backing the preview it loaded:
 *   - session-2    → AGENTS.session2            (:8000) → dashboard-live + route harnesses
 *   - session-1    → AGENTS.session1            (:8001) → dashboard-fault
 *   - fault-pre-t0 → AGENTS.faultPreT0          (:8002) → dashboard-recovery
 *   - session-2    → AGENTS.session2Developed   (:8003) → dashboard-developed (first_crack)
 *   - session-2    → AGENTS.session2ChargeWindow (:8004) → dashboard-charge-window (#211)
 *
 * `advanceTo` treats any non-2xx as a HARD failure — a 404 means the marker never
 * fired (wrong fixture/marker), which must be loud, not a baseline of the wrong page.
 */

/** Per-fixture agent (FastAPI) origins — the gated step surface lives here. */
export const AGENTS = {
  session2: process.env.ROASTPILOT_API ?? "http://127.0.0.1:8000",
  session1: process.env.ROASTPILOT_API_FAULT ?? "http://127.0.0.1:8001",
  faultPreT0: process.env.ROASTPILOT_API_RECOVERY ?? "http://127.0.0.1:8002",
  // A second session-2 agent for the developed state (advance-to first_crack),
  // separate from the live agent so the two specs don't share monotonic stepping.
  session2Developed: process.env.ROASTPILOT_API_DEVELOPED ?? "http://127.0.0.1:8003",
  // A third session-2 agent for the charge-window state (#211): stepped into
  // preheating until the bean is in the charge band so the persistent ChargeBanner
  // shows. Its own agent — advance-to/step is monotonic-forward per agent.
  session2ChargeWindow: process.env.ROASTPILOT_API_CHARGE ?? "http://127.0.0.1:8004",
} as const;

export interface ReplayStepResult {
  agent_phase: string;
  tick: number;
  elapsed_seconds: number | null;
  finalized: boolean;
  settled: boolean;
  /** LOSSY (#338): the broadcaster sequence — retained for diagnostics, NOT the
   *  settle barrier (a dropped SSE frame leaves the browser permanently short). */
  last_event_id: number;
  /** The replayed run id — used to poll the lossless REST snapshot (#338). */
  run_id: string | null;
  /** LOSSLESS settle target (#338): the store-backed CHARGED telemetry row count
   *  (== the rendered curve point count), which the SPA re-hydrates from REST on
   *  (re)connect (#153), so it never depends on every SSE frame arriving. */
  persisted_point_count: number;
  requested_marker: string;
  marker_reached: boolean;
}

/** Advance the given agent's replay to a marker; throw loud on any non-2xx (e.g. 404). */
export async function advanceTo(agent: string, marker: string): Promise<ReplayStepResult> {
  const res = await fetch(`${agent}/api/replay/advance-to`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ marker }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(`advance-to('${marker}') on ${agent} failed (${res.status}): ${detail}`);
  }
  const result = (await res.json()) as ReplayStepResult;
  if (!result.marker_reached) {
    throw new Error(`advance-to('${marker}') on ${agent} returned 2xx but marker_reached=false`);
  }
  return result;
}

/** Advance the given agent's replay by N ticks (count-based; always 200). Each tick
 *  emits a telemetry frame, so this is how a spec makes SSE frames flow to a
 *  connected browser (advance-to a tick-0 marker like `preheating` emits no new
 *  frames). */
export async function step(agent: string, ticks: number): Promise<ReplayStepResult> {
  const res = await fetch(`${agent}/api/replay/step`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ticks }),
  });
  if (!res.ok) {
    throw new Error(`step(${ticks}) on ${agent} failed (${res.status})`);
  }
  return (await res.json()) as ReplayStepResult;
}

/**
 * Advance the agent's replay to an ABSOLUTE cursor `tick` — the idempotent,
 * retry-safe variant of `step` (#338).
 *
 * `step(N)` is count-based + additive, so under Playwright `retries` a re-run
 * re-issues `step(N)` and advances N MORE frames from where the failed attempt
 * left the stateful (monotonic-forward) replay agent → lands the wrong phase.
 * `stepTo(N)` advances only the delta to the absolute cursor, so a retry on an
 * agent already at the target is a no-op and lands the SAME state every time.
 */
export async function stepTo(agent: string, tick: number): Promise<ReplayStepResult> {
  const res = await fetch(`${agent}/api/replay/step-to`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ tick }),
  });
  if (!res.ok) {
    throw new Error(`step-to(${tick}) on ${agent} failed (${res.status})`);
  }
  return (await res.json()) as ReplayStepResult;
}

/** The downsampled telemetry snapshot shape (the LOSSLESS REST settle source, #338). */
interface TelemetrySnapshot {
  point_count: number;
  points: { charge_elapsed_seconds: number | null }[];
}

/**
 * Read the agent's CHARGED telemetry point count straight from the REST snapshot
 * (#338) — store-backed and lossless, independent of SSE delivery. This is the
 * server-side authority the stepped result's `persisted_point_count` mirrors; a
 * spec polls it (or the browser's rendered curve) rather than the lossy
 * `__lastEventId`. Returns 0 for a run with no charged points yet (pre-charge).
 */
export async function serverChargedPointCount(agent: string, runId: string): Promise<number> {
  const res = await fetch(`${agent}/api/roasts/${runId}/telemetry`);
  if (!res.ok) {
    throw new Error(`telemetry snapshot for ${runId} on ${agent} failed (${res.status})`);
  }
  const series = (await res.json()) as TelemetrySnapshot;
  return series.points.filter((p) => p.charge_elapsed_seconds !== null).length;
}

/** Probe one agent's gated step surface (a 0-tick step is a no-op route probe). */
async function probeStepSurface(agent: string): Promise<void> {
  const res = await fetch(`${agent}/api/replay/step`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ticks: 0 }),
  });
  if (!res.ok) {
    throw new Error(
      `replay step surface not available on ${agent} (${res.status}) — booted with --step?`,
    );
  }
}

export default async function globalSetup(): Promise<void> {
  // Confirm the gated replay control surface is mounted on EVERY fixture agent
  // before any spec runs — fail fast if one was booted without --step.
  await Promise.all(Object.values(AGENTS).map(probeStepSurface));
}
