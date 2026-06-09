# E9 — Vertical Slice

## Goal

The 12-step mock milestone test wiring E4–E8 together: a full roast from
service start to recoverable completed state, first against the fake MCP
client, then against the real coffee-roaster-mcp server in mock-driver mode.
No hardware, no microphone, no model download.

## Plan links

- Component plan §8 (`test_milestone1.py`):
  `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § First Milestone (the 12 steps):
  `roastpilot-plan/roastpilot-agent-orchestration-plan.md`

## E7 handoff (do first)

E7 built the API + SSE surface and the operator action queue but deliberately
left the *live wiring* to E9 (the API queues; the controller executes). Before
the 12-step slice, wire these — each routes through the **full** safety policy
before any MCP write (plan §6, decision **D19**; see `docs/epics/E07-api.md`
"For E9" notes):

1. **Event sink + telemetry.** Construct the controller with
   `RoastService.events` (the `EventBroadcaster`) as its `event_emitter`, and
   call `service.events.emit_telemetry(...)` each tick — that is how the SSE
   stream (and the SPA at E10) sees live state.
2. **Drain the operator queue.** The controller drains
   `RoastService.operator_queue` each tick and executes each action through the
   full safety policy (rate limits, bounds, drop eligibility, phase) — the
   queue's phase pre-check is advisory only (last-persisted phase), not
   authoritative.
3. **Add the 4 missing controller handlers (D19).** Plan §6 lists 9 operator
   actions; E4 shipped handlers for 5 (`mark_first_crack`, `drop_beans`,
   `stop_cooling`, `emergency_stop`, `acknowledge_recovery`). The remaining 4 —
   `mark_beans_added`, `start_cooling`, `pause_advisory`, `resume_advisory` —
   are queued-but-not-executable until E9 adds their controller handlers (with
   explicit tests + a safety-reviewer pass, per AGENTS.md).
4. **Bound the queue + guard terminal runs.** `operator_queue` is unbounded
   today (safe only because nothing drains it yet); bound or dedup it at drain,
   and add a 409/410 guard for actions submitted to a COMPLETE/FAULTED run
   (today the phase matrix rejects the harmful ones).

## Stories

### E9-S1 — 12-step slice against the fake MCP client

Acceptance criteria:

- [x] All 12 steps pass end to end: start service → start roast (mock) →
  stream state → mock auto-T0 → FC (override or mock status) → one advisory
  decision through the fake adapter → safety validation → heat/fan command
  → drop → stop cooling → export logs → restart proves completed state
  recoverable. (`tests/test_milestone1.py`, #80.)
- [x] Decision trace (advisory → verdict → command) persisted and readable
  via the timeline route. Captured in `docs/e9-decision-trace-2026-06-09.md`
  (the same-day demo asset, plan §8).

### E9-S2 — Slice against real MCP server (mock driver, subprocess)

Acceptance criteria:

- [x] Same flow with the agent spawning the real `coffee-roaster-mcp` as a
  stdio child (D6) in mock-driver mode.
  (`tests/test_milestone1_real_mcp.py`, #81.)
- [x] Runs in CI (no hardware/audio/model deps): `coffee-roaster-mcp==0.1.3`
  is a dev dependency; CI installs `libportaudio2` for `sounddevice`'s import.
- [x] sim-roast-runner sub-agent produces a markdown decision-trace summary
  of a slice run (`docs/e9-decision-trace-real-mcp-2026-06-09.md`);
  mcp-contract-checker confirms zero drift vs installed 0.1.3.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E9-S1 | 12-step slice, fake MCP | done |
| E9-S2 | Slice against real MCP (mock mode) | done |

Epic status: **complete** — E9-S1 (#80) + E9-S2 (#81) merged. E9 green in CI
realizes D17 criterion (1).

## Notes

- **E9-S1 (#80):** The E7 handoff + the 12-step mock slice. Wired the live
  controller tick loop into `RoastService` via a new `RoastRunner` (drain the
  operator queue through full safety, per-tick telemetry + event/eval
  persistence into the broadcaster + store, log export + run completion,
  restart recovery on startup via a FastAPI lifespan). Added the 4 missing
  controller handlers (D19) — `operator_mark_beans_added`,
  `operator_start_cooling`, `operator_pause_advisory`,
  `operator_resume_advisory` — each MCP-write handler routing through the full
  command×phase matrix before any write, plus a `ControllerSnapshot` post-tick
  read seam. Extended the `CommandExecutor` protocol (+ conftest fakes) with
  `mark_beans_added`/`start_cooling`. Added the `RoasterControlAdapter` +
  `project_session_state` in `mcp_client.py` (set_targets→set_heat+set_fan,
  stalled-clock `age_seconds` so the stale-telemetry fault stays reachable,
  retained raw state for telemetry-row enrichment). Bounded the operator queue
  (`QueueFull`→`failed`, e-stop drained first) and added a 410 terminal-run
  guard. Passed an adversarial `safety-reviewer` pass (no blockers/concerns)
  and the `sim-roast-runner` decision-trace summary
  (`docs/e9-decision-trace-2026-06-09.md`). Decision trace is readable via the
  timeline route (events + safety_evaluations + command_log); the
  `advisor_decisions` table is the provider-call channel for the real
  `PydanticAIAdvisor`, empty under `FakeAdvisor`.
- **E9-S2 (#81):** The same milestone flow against the **real**
  `coffee-roaster-mcp` spawned as a stdio subprocess in mock-driver mode (D6).
  Added `coffee-roaster-mcp==0.1.3` as a dev dependency and `libportaudio2` to
  CI (for `sounddevice`'s import; mock mode never opens an audio device). Wired
  `MCPConfig.env` → `build_server_parameters` (the E9-S2 env-forwarding gap) so
  the child is hardware-/audio-/model-free regardless of ambient env. The slice
  (`tests/test_milestone1_real_mcp.py`) drives the controller closed-loop over
  the real MCP boundary: a context-aware advisor engineers the bean-temp drop
  the server's auto-T0 detector needs (auto-T0 is config-file-only, enabled via
  a temp YAML), first crack is the operator override (audio disabled), and
  drop/stop-cooling are operator actions. Deterministic (mock advances one
  virtual second per state read), ~2.8 s wall-clock, skipif-gated on the binary.
  `mcp-contract-checker` confirmed zero drift vs installed 0.1.3; trace in
  `docs/e9-decision-trace-real-mcp-2026-06-09.md`.
