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

- [ ] All 12 steps pass end to end: start service → start roast (mock) →
  stream state → mock auto-T0 → FC (override or mock status) → one advisory
  decision through the fake adapter → safety validation → heat/fan command
  → drop → stop cooling → export logs → restart proves completed state
  recoverable.
- [ ] Decision trace (advisory → verdict → command) persisted and readable
  via the timeline route.

### E9-S2 — Slice against real MCP server (mock driver, subprocess)

Acceptance criteria:

- [ ] Same flow with the agent spawning the real `coffee-roaster-mcp` as a
  stdio child (D6) in mock-driver mode.
- [ ] Runs in CI (no hardware/audio/model deps).
- [ ] sim-roast-runner sub-agent produces a markdown decision-trace summary
  of a slice run.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E9-S1 | 12-step slice, fake MCP | not started |
| E9-S2 | Slice against real MCP (mock mode) | not started |

Epic status: **not started** — depends on E4–E8.
