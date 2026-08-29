# E10-S1 replay-harness decision trace (2026-06-09)

A short reproducible record of what the replay harness produces, for the E10
page teammates (S3–S5) and the E12 demo. The harness drives the **real**
`RoastService` / `RoastRunner` / `RoastController` against recorded telemetry,
so the SSE stream + REST snapshots are byte-identical to a live roast; agent
phase is server-derived, never inferred from the export.

## Fixtures (`tests/fixtures/replay/`)

| Fixture | Source | Frames | Demo role |
|---|---|---|---|
| `session-2` | 7-Jun live roast, auto-T0 (the better demo) | 270 | dashboard-live → development → drop → cooling; carries the CLAMP key frame |
| `session-1` | 7-Jun live roast, manual-T0 | 273 | **faults on replay** — see below |
| `fault-pre-t0` | **synthetic** (hand-authored, labelled) | 9 | drives the real `SafetyPolicy` past the 200 °C pre-T0 bound → `operator_recovery_required` (fault/recovery baselines) |
| `cooling-complete` | **synthetic** (hand-authored, labelled) | 20 | a successful roast that stops cooling before its last frame → reaches `complete` (the COMPLETE baseline) |

**Two fault paths, two fixtures.** session-1 *faults on replay*: it carries real
env-temp readings up to 242 °C, which exceed the agent's deliberately conservative
`max_env_temp_c` = 240 °C software ceiling, so the **real** policy trips
EMERGENCY_STOP → FAULTED (faithful replay of a real reading; the Hottop tolerated
it but the agent is more conservative by design — a genuine *validation* of the
conservative policy on real data, not a defect). That trips a *different* rule
than the synthetic `fault-pre-t0` track (pre-T0 overrun → RECOVERY), so both are
needed. No real 7-Jun roast replays cleanly to `complete` under all agent ceilings
— hence the synthetic `cooling-complete` fixture for the COMPLETE baseline. Only
the synthetic fixtures' telemetry is hand-authored; every verdict/phase is produced
by the real controller + policy.

## Capture-state → fixture → marker map (S3/S5/S6 consume this)

The required `ui-reviewer` / snapshot baseline states (kickoff §5), each mapped to
the exact fixture + `advance_to` marker that produces it. Boot with
`--replay <fixture> --step`, then `POST /api/replay/advance-to {marker}` (200 →
screenshot; 404 → wrong fixture/marker, fail loud).

| Baseline state | Fixture | `advance_to` marker | Resulting phase |
|---|---|---|---|
| `dashboard-live` (charge band) | `session-2` | `preheating` | `preheating` |
| advisory CLAMP key frame | `session-2` | `clamp` | `development` |
| `dashboard-fault` (FaultBanner) | **`session-1`** (real, evidence-backed env-ceiling fault) | `fault` | `faulted` |
| `dashboard-recovery` (RecoveryModal) | `fault-pre-t0` | `recovery` | `operator_recovery_required` |
| `roast-detail` / `history` "complete" | `cooling-complete` | `end` | `complete` |
| live drop → cooling | `session-2` | `drop` / `cooling` | `cooling` |

Prefer **session-1 for `dashboard-fault`** — it's a real fault (more authentic +
a strong talk frame) over anything synthetic. `fault-pre-t0` owns the *recovery*
(operator_recovery_required) baseline specifically. For a synthetic *faulted*
variant of the overrun, `ROASTPILOT_SAFETY__PRE_T0_OVERRUN_SEVERITY=fault` flips
`fault-pre-t0`'s `recovery` marker to `fault` — but session-1 is the preferred
fault asset.

## CLI

```bash
# Free-running (1× = the E12 screen-recording rig; up to 60× for dev):
roastpilot-agent --replay tests/fixtures/replay/session-2 --speed 1

# Deterministic Playwright stepping (paused at tick 0; gated control routes):
roastpilot-agent --replay tests/fixtures/replay/session-2 --step
#   POST /api/replay/step        {"ticks": N}      → 200
#   POST /api/replay/advance-to  {"marker": "..."} → 200 if reached, 404 if not
# → {agent_phase, tick, elapsed_seconds, finalized, settled, last_event_id,
#    requested_marker, marker_reached}
```

The `/api/replay/*` routes are mounted **only** in `--step` mode (never on the
live app — a control hole otherwise; `test_step_routes_mounted_only_in_step_mode`
pins this, including against the live `create_app`). `last_event_id` is the
broadcaster sequence after the synchronous step drains, so a Playwright setup
waits until the browser's `lastEventId >= N` before screenshotting — no arbitrary
sleeps. **`advance-to` returns 404** (with a descriptive body) when the requested
marker never fires in the export, so a wrong fixture/marker fails loud instead of
silently screenshotting the terminal state.

## session-2 stepped trace (markers → server-derived phase)

```
run frames 270
preheating   -> phase=preheating                   tick=0
t0           -> phase=preheating  (detected; debounced transition follows)
first_crack  -> phase=development                  tick=199
clamp        -> phase=development                  tick=199
drop         -> phase=cooling                      tick=219
end          -> phase=cooling                      tick=270
```

## The CLAMP key frame (the talk's frame)

Synthesized demo trace, tagged `"source": "replay_overlay"` / `"synthesized":
true` so no reader mistakes it for live output. The recorded exports carry no
advisory records, and a genuine CLAMP cannot arise from a bounded
`RoastDecision` — so the overlay injects a 105 % heat request and lets the
**real** `SafetyPolicy.evaluate_command` compute the verdict:

```
clamp evals in /timeline: 1 | advisory events: 1
reason: requested heat 105 % / fan 40 % outside 0–100: clamped to heat 100 % / fan 40 %
```

It is both emitted on SSE (the live advisory panel) and persisted as a
`roast_events` + `safety_evaluations` row (the detail-page trace table). The SPA
renders it from that recorded trace; it never re-runs safety.

## fault-pre-t0 stepped trace

```
run frames 9
recovery -> phase=operator_recovery_required   (real RECOVERY verdict)
```

The pre-T0 overrun rule's default severity is `recovery`, so the agent enters
`operator_recovery_required` (drives the SPA RecoveryModal + "no auto-resume"
copy). For a true `faulted` baseline, set
`ROASTPILOT_SAFETY__PRE_T0_OVERRUN_SEVERITY=fault`.

## Playwright global-setup (the canonical flow)

Spawn `--step`, open the SSE stream, `advance-to` a marker, wait until the
browser's `lastEventId` has caught up, then screenshot. Treat **any non-2xx from
`advance-to` as a hard failure** — a 404 means the marker never fired (wrong
fixture/marker), and you want that loud, not a baseline of the wrong page.

```ts
// playwright global-setup (Node) — drives the harness over HTTP only.
const BASE = "http://127.0.0.1:8000";

async function advanceTo(marker: string) {
  const res = await fetch(`${BASE}/api/replay/advance-to`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ marker }),
  });
  if (!res.ok) {
    // 404 → marker never fired in this export. Fail loud with the server's body.
    const { detail } = await res.json();
    throw new Error(`advance-to('${marker}') failed (${res.status}): ${detail}`);
  }
  return res.json(); // { agent_phase, tick, last_event_id, marker_reached, ... }
}

// In the page: the SSE reducer tracks lastEventId (it dedups on id <= lastEventId).
async function waitForSettle(page, lastEventId: number) {
  await page.waitForFunction(
    (id) => (window as any).__lastEventId >= id, // exposed by the SSE reducer
    lastEventId,
  );
}

// Usage per baseline state:
const { last_event_id } = await advanceTo("clamp"); // the advisory key frame
await waitForSettle(page, last_event_id);
await page.screenshot({ path: "dashboard-clamp.png" });
```

`step` (count-based) always returns 200 — there's no marker to miss. For the
fault/recovery baselines, point `--replay` at `fault-pre-t0` and `advance-to`
`recovery` (or `fault` with `PRE_T0_OVERRUN_SEVERITY=fault`).
`tests/test_replay.py::test_http_step_routes_drive_the_replay` +
`::test_http_advance_to_unreached_marker_is_404` are working end-to-end
references for both paths.

## Tests

`tests/test_replay.py` + `tests/test_cli.py` (hardware-free): fixture parsing,
real-pipeline phase progression (incl. the `t0` marker landing in `preheating`
before the debounce), typed SSE frame surface, populated REST snapshots, the
CLAMP in `/timeline` + SSE (emitted exactly once), deterministic
`step`/`advance_to` with the 404-on-unreached-marker path, the speed clamp,
session-1 → real env-ceiling FAULT, `cooling-complete` → real COMPLETE (exercising
STOP_COOLING), the synthetic fault → real recovery, and the gated-route safety
boundary (incl. the live `create_app`). `ruff` / `ruff format --check` /
`pyright` (strict) / `pytest` green.

<!-- issue-702 final-head docs-only fast-path proof -->
