# E4 — Controller

## Goal

The deterministic state machine and monotonic 1.0 s tick loop: explicit
transition table, T0 debounce, add-beans guidance, restart-into-recovery
behavior — all driven against a fake MCP client.

## Plan links

- Component plan §3 (phase mapping), §8 (`test_controller.py`):
  `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § State Machine, § Controller Loop, § Milestone 1
  Module Blueprint: `roastpilot-plan/roastpilot-agent-orchestration-plan.md`

## Stories

### E4-S1 — Transition table

Acceptance criteria:

- [ ] Explicit transition table for the 9 phases per plan §3; advisor cannot
  trigger transitions.
- [ ] Tests: valid normal path, invalid transition rejection, `* → faulted`,
  `* → operator_recovery_required`.

### E4-S2 — Tick loop and scheduler

Acceptance criteria:

- [ ] Monotonic fixed-rate scheduler (no drift accumulation) with
  jitter measurement; tested with a fake clock.
- [ ] Tick order enforced: read state → persist → safety → transitions →
  (advisory?) → validate → execute → persist → emit events.
- [ ] Slow/failed advisor call never blocks safety handling or polling.

### E4-S3 — Preheating branch: T0 debounce and add-beans guidance

Acceptance criteria:

- [ ] Add-beans guidance emitted exactly once when the 170–200 °C range is
  reached; non-blocking.
- [ ] T0 debounce: counter increments on MCP-reported T0, resets when absent,
  transition after `t0_debounce_ticks` (default 3). Tests reflect that
  flapping originates from read faults, not MCP state (plan §2 note).

### E4-S4 — Fake-MCP harness and restart recovery

Acceptance criteria:

- [ ] `FakeMCPClient` scripted full-roast scenarios in conftest.
- [ ] Restart with possibly-active run lands in `operator_recovery_required`;
  heat/fan never auto-resumed; e-stop available.
- [ ] Operator-timeout policy (D16) applies only in true operator-required
  states — manual confirmation, manual hold, recovery — and never in normal
  phases. UI disconnect during normal phases changes nothing: backend
  safety continues without the UI (API-side disconnect behavior is E7-S3;
  this story owns the controller-side policy).

## Status

| Story | Title | Status |
|-------|-------|--------|
| E4-S1 | Transition table | not started |
| E4-S2 | Tick loop and scheduler | not started |
| E4-S3 | T0 debounce and add-beans guidance | not started |
| E4-S4 | Fake-MCP harness and restart recovery | not started |

Epic status: **not started** — depends on E2, E3.
