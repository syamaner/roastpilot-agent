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

### E3-S1 — Temperature and overrun rules ([#6](https://github.com/syamaner/roastpilot-agent/issues/6))

Acceptance criteria:

- [x] Max bean temp and max env temp rules with tests — breach ⇒
  EMERGENCY_STOP (hard ceilings; bean checked first), strict bounds
  (exactly-at-limit does not trip).
- [x] Pre-T0 overrun rule: applies only in `preheating` with no confirmed
  T0; bean temp > configured bound (default 200 °C) ⇒ heat clamped to 0 %,
  safe fan preserved/set, controller moved to `operator_recovery_required`
  or `faulted` per configured severity. Both severities tested.
- [x] `SafetyEvaluation` gains the `rule` field (plan §5
  `safety_evaluations.rule`); every outcome carries rule + reason.

### E3-S2 — Telemetry validity rules ([#7](https://github.com/syamaner/roastpilot-agent/issues/7))

Acceptance criteria:

- [x] Stale telemetry (> `max_stale_telemetry_seconds`) and missing
  telemetry during an active roast produce the configured verdicts — both
  fail closed (heat 0 %, safe fan, FAULT) in `ACTIVE_ROAST_PHASES` only;
  strict bound; never enforced in idle/starting/complete/faulted/recovery.
- [x] MCP read/write failure handling verdicts tested — transient failures
  tolerated (ALLOW, controller skips/retries), `max_consecutive_mcp_failures`
  (default 3 ≈ 3 s blind window at the 1.0 s tick) fails closed with FAULT.

### E3-S3 — Command validation rules ([#8](https://github.com/syamaner/roastpilot-agent/issues/8))

Acceptance criteria:

- [x] Heat/fan bounds (0–100) with clamping semantics tested — out-of-range
  requests CLAMP to the nearest bound (intent honored, never rejected);
  ALLOW/CLAMP carry the adjusted values to execute.
- [x] Command rate limiting tested — inside `min_seconds_between_commands`
  ⇒ REJECT (checked before bounds); exactly at the limit allowed.
- [x] Unsafe drop recommendation rejection tested — advisor `should_drop`
  honored only in `development`; REJECTed in all other phases (operator
  drops are E3-S5's command×phase matrix).
- [x] Malformed/timeout/unsafe/provider-error advisor output ⇒ rejected
  recommendation ⇒ deterministic fallback holds current targets (adjusted
  values echo the heat/fan in effect).

### E3-S4 — Emergency stop and verdict plumbing

Acceptance criteria:

- [ ] `emergency_stop` reachable from every phase; never gated on advisor,
  UI, or cloud state.
- [ ] Every evaluation produces a persisted-ready `SafetyEvaluation` (rule
  name, verdict, input/adjusted values, reason).
- [ ] safety-reviewer sub-agent run recorded on the closing PR.

### E3-S5 — Phase and source validity rules (D16)

Acceptance criteria:

- [ ] Command×phase validity matrix: every MCP write command is checked
  against the current agent phase; invalid combinations (e.g. `set_heat`
  during `cooling`, `stop_cooling` during `development`) produce REJECT
  verdicts. The matrix is exhaustively tested (all write commands × all
  nine phases).
- [ ] FC/T0 source validity: first-crack and T0 state transitions are
  accepted only from MCP detection status or explicit operator action;
  any other source is rejected and logged.
- [ ] Tests for both rule groups in `test_safety.py` (plan §8, D16 row).

## Status

| Story | Title | Status |
|-------|-------|--------|
| E3-S1 | Temperature and overrun rules | done |
| E3-S2 | Telemetry validity rules | done |
| E3-S3 | Command validation rules | done |
| E3-S4 | Emergency stop and verdict plumbing | not started |
| E3-S5 | Phase and source validity rules (D16) | not started |

Epic status: **in progress** (E3-S1, E3-S2, E3-S3 done).
