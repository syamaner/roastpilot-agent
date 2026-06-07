# E3 — Safety Policy

## Goal

The full deterministic safety rule set with exhaustive tests. This is the
heart of the system (and of the September talk): every roaster write is
validated, clamped, or rejected here; the LLM never bypasses it.

## Plan links

- Component plan §8 (`test_safety.py` suite):
  `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § Safety Policy, § Milestone 1 Module Blueprint
  (safety.py ownership, pre-roast overrun rule):
  `roastpilot-plan/roastpilot-agent-orchestration-plan.md`

## Stories

### E3-S1 — Temperature and overrun rules

Acceptance criteria:

- [ ] Max bean temp and max env temp rules with tests.
- [ ] Pre-T0 overrun rule: applies only in `preheating` with no confirmed
  T0; bean temp > configured bound (default 200 °C) ⇒ heat clamped to 0 %,
  safe fan preserved/set, controller moved to `operator_recovery_required`
  or `faulted` per configured severity. Both severities tested.

### E3-S2 — Telemetry validity rules

Acceptance criteria:

- [ ] Stale telemetry (> `max_stale_telemetry_seconds`) and missing
  telemetry during an active roast produce the configured verdicts.
- [ ] MCP read/write failure handling verdicts tested.

### E3-S3 — Command validation rules

Acceptance criteria:

- [ ] Heat/fan bounds (0–100) with clamping semantics tested.
- [ ] Command rate limiting tested.
- [ ] Unsafe drop recommendation rejection tested (drop eligibility).
- [ ] Malformed/timeout advisor output ⇒ rejected recommendation ⇒
  deterministic fallback (hold current targets).

### E3-S4 — Emergency stop and verdict plumbing

Acceptance criteria:

- [ ] `emergency_stop` reachable from every phase; never gated on advisor,
  UI, or cloud state.
- [ ] Every evaluation produces a persisted-ready `SafetyEvaluation` (rule
  name, verdict, input/adjusted values, reason).
- [ ] safety-reviewer sub-agent run recorded on the closing PR.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E3-S1 | Temperature and overrun rules | not started |
| E3-S2 | Telemetry validity rules | not started |
| E3-S3 | Command validation rules | not started |
| E3-S4 | Emergency stop and verdict plumbing | not started |

Epic status: **not started** — follows E2.
