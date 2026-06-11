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
 * MULTI-FIXTURE: the three dashboard states need three fixtures, so there are three
 * agents (see playwright.config.ts). The `advanceTo`/`step` helpers take the agent
 * base URL so a spec drives the agent backing the preview it loaded:
 *   - session-2   → AGENTS.session2    (:8000) → dashboard-live + route harnesses
 *   - session-1   → AGENTS.session1    (:8001) → dashboard-fault
 *   - fault-pre-t0 → AGENTS.faultPreT0 (:8002) → dashboard-recovery
 *
 * `advanceTo` treats any non-2xx as a HARD failure — a 404 means the marker never
 * fired (wrong fixture/marker), which must be loud, not a baseline of the wrong page.
 */

/** Per-fixture agent (FastAPI) origins — the gated step surface lives here. */
export const AGENTS = {
  session2: process.env.ROASTPILOT_API ?? "http://127.0.0.1:8000",
  session1: process.env.ROASTPILOT_API_FAULT ?? "http://127.0.0.1:8001",
  faultPreT0: process.env.ROASTPILOT_API_RECOVERY ?? "http://127.0.0.1:8002",
} as const;

export interface ReplayStepResult {
  agent_phase: string;
  tick: number;
  elapsed_seconds: number | null;
  finalized: boolean;
  settled: boolean;
  last_event_id: number;
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
