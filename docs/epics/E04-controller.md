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

### E4-S1 — Transition table ([#27](https://github.com/syamaner/roastpilot-agent/issues/27))

Acceptance criteria:

- [x] Explicit transition table for the 9 phases per plan §3 (completeness
  pinned by a test); `complete → idle` reset edge added as a documented
  refinement (a long-running service needs it; plan §3 leaves `complete`
  exit-less); recovery exits cover operator resume/cool/end only —
  `starting` is never a recovery target. Advisor cannot trigger
  transitions: no transition API accepts advisor types (pinned by an
  introspection test).
- [x] Tests: valid normal path, invalid transition rejection (phase
  unchanged after rejection), self-transitions rejected, `* → faulted`
  and `* → operator_recovery_required` from every phase.

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
  heat/fan never auto-resumed; e-stop available. Specifically: the E4-S1
  resume edges (recovery → active phases) must pair with an explicit
  operator gate, and heat stays 0 after a resume until separately
  commanded — the E3-S5 phase matrix alone permits SET_HEAT in resumed
  phases (safety-reviewer carry-forward, E4-S1 PR).
- [ ] Operator-timeout policy (D16) applies only in true operator-required
  states — manual confirmation, manual hold, recovery — and never in normal
  phases. UI disconnect during normal phases changes nothing: backend
  safety continues without the UI (API-side disconnect behavior is E7-S3;
  this story owns the controller-side policy).
- [ ] Entry into `faulted`/`operator_recovery_required` guarantees
  hardware-off (heat 0, safe fan) via the controller's own path — the
  E3-S2 telemetry-validity rule is deliberately silent in those phases
  (safety-reviewer carry-forward, E3-S2 PR).
- [ ] A failed MCP `emergency_stop` call lands in fail-closed handling
  (FAULT + heat-0/safe-fan write attempts), never silent continuation —
  the e-stop evaluation deliberately carries no adjusted values
  (safety-reviewer carry-forward, E3-S4 PR).
- [ ] The start command is serialized: a stale `starting` phase can never
  accept a second `start_roast_session` — the E3-S5 matrix allows the
  command in `starting` and delegates uniqueness to the API 409 + this
  controller guarantee (safety-reviewer carry-forward, E3-S5 PR).
- [ ] Relayed T0/FC transitions preserve the true detection source (MCP
  detection or operator action) — the controller never re-stamps an
  advisor-origin event as its own or as MCP/operator, or the E3-S5
  source-validity allowlist could be bypassed (safety-reviewer
  carry-forward, E3-S5 PR).

## Status

| Story | Title | Status |
|-------|-------|--------|
| E4-S1 | Transition table | done |
| E4-S2 | Tick loop and scheduler | not started |
| E4-S3 | T0 debounce and add-beans guidance | not started |
| E4-S4 | Fake-MCP harness and restart recovery | not started |

Epic status: **in progress** (E4-S1 done).
