# E7 — API

## Goal

The full REST + SSE surface the SPA renders from: roast lifecycle routes,
history/detail/telemetry/timeline reads, the operator action queue, and the
typed SSE event stream. One backend authority — the SPA never calls MCP.

## Plan links

- Component plan §6 (API contract: routes, operator actions, SSE event
  types), §8 (`test_api.py`): `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § API And UI Events:
  `roastpilot-plan/roastpilot-agent-orchestration-plan.md`

## Stories

### E7-S1 — REST routes

Acceptance criteria:

- [ ] All plan §6 routes: health (incl. MCP child status + active run id),
  POST /api/roasts (409 if active), history list, run detail, telemetry
  (with downsample param), timeline (decision trace), log manifest +
  downloads, rating.
- [ ] Typed Pydantic response models in `models.py`; route tests.

### E7-S2 — Operator action queue

Acceptance criteria:

- [ ] Action enum per plan §6 (incl. recovery-only `mark_beans_added` and
  `start_cooling`); every action → `operator_actions` row → controller
  queue → safety policy → MCP.
- [ ] Rejected/failed actions reported with reasons; tested.

### E7-S3 — SSE stream

Acceptance criteria:

- [ ] All plan §6 event types, typed JSON payloads, telemetry every tick,
  15 s heartbeat.
- [ ] Disconnect handling tested; UI disconnect does not trigger cooling or
  block backend safety. (The controller-side rule — operator timeout only
  in true operator-required states, never in normal phases — is owned by
  E4-S4 per D16; this story covers the API/SSE side only.)

## Status

| Story | Title | Status |
|-------|-------|--------|
| E7-S1 | REST routes | done |
| E7-S2 | Operator action queue | done |
| E7-S3 | SSE stream | done |

Epic status: **complete** — depends on E4 ✅, E6 ✅.

## Notes

- **E7-S1 (#67):** REST surface + typed response models (`models.py`):
  `GET /api/health` (liveness + MCP child status + active run id),
  `POST /api/roasts` (409 if active), history list, run detail, telemetry
  (`?downsample=` stride), timeline (decision trace), log manifest + artifact
  downloads, operator rating. Backed by a new `RoastService` in `api.py` and
  read projections in `store.py`. The persisted run starts in `starting`; the
  live controller tick loop + MCP child that *drive* it are wired by the E9
  vertical slice (`RoastService` is the seam). No safety/controller changes.
- **E7-S2 (#68):** Operator action queue. `OperatorAction` enum (plan §6, 9
  actions incl. recovery-only `mark_beans_added`/`start_cooling`) + request/
  result models in `models.py`. `POST /api/roasts/{id}/operator-actions`:
  action → phase-validity pre-check via the existing
  `SafetyPolicy.evaluate_command_phase` → `operator_actions` row → controller
  queue (`asyncio.Queue` on `RoastService`). Accepted actions are enqueued;
  rejected (wrong phase) are recorded with the policy reason and not queued;
  `emergency_stop` is accepted from every phase. The queue performs **no MCP
  write** and does not reimplement safety. Passed an adversarial
  safety-reviewer pass. No `safety.py`/`controller.py` changes.
  - **For E9 (plan §6 decision D19; `docs/epics/E09-vertical-slice.md` "E7
    handoff").** Plan §6 lists 9 operator actions; E4 shipped handlers for 5
    (`mark_first_crack`, `drop_beans`, `stop_cooling`, `emergency_stop`,
    `acknowledge_recovery`). The controller drains `operator_queue` each tick
    and runs the **full** safety policy (rate limits, bounds, drop eligibility,
    phase) before any MCP write — the queue's phase pre-check verdict is
    advisory (last-persisted phase), not authoritative. E9 also adds the
    controller handlers for the 4 actions without one today: `mark_beans_added`,
    `start_cooling`, `pause_advisory`, `resume_advisory` (the queue accepts and
    records all 9; execution of these four is wired in E9).
  - **For E9:** `operator_queue` is unbounded (`asyncio.Queue()`); with no
    controller drain in E7 it cannot grow. When E9 wires the drain, bound it
    (or dedup at drain) so a spammy operator cannot grow it without limit. A
    terminal-run guard (409/410 on actions to a COMPLETE/FAULTED run) is also
    a reasonable E9 addition — today the phase matrix rejects the harmful ones.
- **E7-S3 (#69):** Typed SSE event stream — **E7's contract for E9/E10**.
  `SseEventType` (the `event:` discriminator: every `RoastEventKind` +
  transport-only `telemetry`/`heartbeat`), the `SseEvent` envelope (typed,
  `render()` to wire format, monotonic `id`), and `TelemetryEventData` in
  `models.py`. `EventBroadcaster` in `api.py` implements the controller's
  `EventEmitter` protocol (the E9 sink) + `emit_telemetry`; it fans out to
  bounded per-connection queues, non-blocking (`put_nowait`, drop-on-overflow)
  so a slow client can never back-pressure the tick. `GET
  /api/roasts/{id}/events` streams typed frames with a 15 s heartbeat and
  unsubscribes on disconnect. **A UI/SSE disconnect triggers no cooling and no
  state change** — the broadcaster holds no controller/executor/MCP reference;
  backend safety continues with no client. Passed an adversarial
  safety-reviewer pass. No `safety.py`/`controller.py` changes.
  - **For E9:** construct the controller with `service.events` as its
    `event_emitter`, and call `service.events.emit_telemetry(...)` each tick.
    The broadcaster is global (one active run, per the 409 invariant); add
    per-run filtering if concurrent runs ever land.

## SSE contract ready

E7's typed SSE event contract (`models.SseEventType` / `SseEvent` /
`TelemetryEventData` + `EventBroadcaster`) is the surface E9 (vertical slice)
and E10 (SPA) render from — both can now be set up against it.
