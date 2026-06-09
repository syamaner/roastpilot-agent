/**
 * Playwright global-setup (D24) — verify the replay harness is reachable.
 *
 * The agent (webServer 1) boots in `--replay <fixture> --step` mode, paused at
 * tick 0 with the gated `POST /api/replay/{step,advance-to}` control routes
 * mounted. We do NOT pre-advance here: each spec opens the SSE stream FIRST,
 * then steps, so the stepped frames flow to the live browser (window.__lastEventId
 * is set on applied SSE frames, not on hydration). This setup just confirms the
 * control surface is up so a misconfigured harness fails fast, not mid-spec.
 *
 * `advanceTo` is exported for the specs: treat any non-2xx as a HARD failure — a
 * 404 means the marker never fired (wrong fixture/marker), which must be loud,
 * not a baseline of the wrong page.
 */

const AGENT = process.env.ROASTPILOT_API ?? "http://127.0.0.1:8000";

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

/** Advance the replay to a marker; throw loud on any non-2xx (e.g. 404). */
export async function advanceTo(marker: string): Promise<ReplayStepResult> {
  const res = await fetch(`${AGENT}/api/replay/advance-to`, {
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
    throw new Error(`advance-to('${marker}') failed (${res.status}): ${detail}`);
  }
  const result = (await res.json()) as ReplayStepResult;
  if (!result.marker_reached) {
    throw new Error(`advance-to('${marker}') returned 2xx but marker_reached=false`);
  }
  return result;
}

/** Advance the replay by N ticks (count-based; always 200). Each tick emits a
 *  telemetry frame, so this is how the smoke makes SSE frames flow to a connected
 *  browser (advance-to a tick-0 marker like `preheating` emits no new frames). */
export async function step(ticks: number): Promise<ReplayStepResult> {
  const res = await fetch(`${AGENT}/api/replay/step`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ticks }),
  });
  if (!res.ok) {
    throw new Error(`step(${ticks}) failed (${res.status})`);
  }
  return (await res.json()) as ReplayStepResult;
}

export default async function globalSetup(): Promise<void> {
  // Confirm the gated replay control surface is mounted (step mode) before any
  // spec runs — fail fast if the harness was booted without --step. A 0-tick
  // step is a no-op that just probes the route.
  const res = await fetch(`${AGENT}/api/replay/step`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ticks: 0 }),
  });
  if (!res.ok) {
    throw new Error(
      `replay step surface not available (${res.status}) — is the agent booted with --step?`,
    );
  }
}
