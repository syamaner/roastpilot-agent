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
| `session-1` | 7-Jun live roast, manual-T0 | 273 | alternate full roast |
| `fault-pre-t0` | **synthetic** (hand-authored, labelled) | 9 | drives the real `SafetyPolicy` past the 200 °C pre-T0 bound → `operator_recovery_required` (fault/recovery baselines) |

The 7-Jun roasts never fault (pre-T0 bean temp peaks at 186 °C < the 200 °C
bound), so the fault/recovery baselines use the synthetic `fault-pre-t0` track.
Only its telemetry is synthetic — the RECOVERY verdict is produced by the real
policy through the real controller.

## CLI

```bash
# Free-running (1× = the E12 screen-recording rig; up to 60× for dev):
roastpilot-agent --replay tests/fixtures/replay/session-2 --speed 1

# Deterministic Playwright stepping (paused at tick 0; gated control routes):
roastpilot-agent --replay tests/fixtures/replay/session-2 --step
#   POST /api/replay/step        {"ticks": N}
#   POST /api/replay/advance-to  {"marker": "first_crack"}
# → {agent_phase, tick, elapsed_seconds, finalized, settled, last_event_id}
```

The `/api/replay/*` routes are mounted **only** in `--step` mode (never on the
live app — a control hole otherwise; `test_step_routes_mounted_only_in_step_mode`
pins this). `last_event_id` is the broadcaster sequence after the synchronous
step drains, so a Playwright setup waits until the browser's `lastEventId >= N`
before screenshotting — no arbitrary sleeps.

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

## Tests

`tests/test_replay.py` (19 tests, hardware-free): fixture parsing, real-pipeline
phase progression, typed SSE frame surface, populated REST snapshots, the CLAMP
in `/timeline` + SSE (emitted exactly once), deterministic `step`/`advance_to`,
the speed clamp, the synthetic fault → real recovery, and the gated-route safety
boundary. `ruff` / `ruff format --check` / `pyright` (strict) / `pytest` green.
