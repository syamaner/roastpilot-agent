# E8 — Advisor

## Goal

The advisory layer behind the `RoastAdvisor` interface: deterministic
`FakeAdvisor` for tests/demos, the OpenRouter-backed PydanticAI
implementation (D5), and the call-frequency policy. Advisory-only, always:
typed output in, safety policy after.

## Plan links

- Component plan §1 (D5), §4 (advisor specifics), §8 (`test_advisor.py`),
  §11.1 (open item: model slug): `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § PydanticAI Advisory Layer, § Advisory Call Frequency:
  `roastpilot-plan/roastpilot-agent-orchestration-plan.md`

## Stories

### E8-S1 — FakeAdvisor and failure fixtures

Acceptance criteria:

- [ ] Deterministic `FakeAdvisor` with scriptable decisions.
- [ ] Fixtures: valid / malformed / unsafe / timeout / provider error —
  each produces a rejected recommendation + deterministic fallback (hold
  current targets), every outcome persisted.

### E8-S2 — PydanticAI OpenRouter implementation (D5)

Acceptance criteria:

- [ ] OpenAI-compatible OpenRouter endpoint via PydanticAI; strict output
  models; versioned prompts; context hashes (not raw payloads) logged.
- [ ] Tested behind a recorded-response double — no live calls in CI.
- [ ] Exact model slug + structured-output settings confirmed and recorded
  in plan §11 (closes open item 1).

### E8-S3 — Call-frequency policy

Acceptance criteria:

- [ ] Advisor called only on meaningful change: ≥1.0 °C bean temp delta,
  ≥2.0 °C/min RoR delta, phase transition, ≥15 s minimum interval, or
  manual trigger; timeout-bounded; never blocks the tick.
- [ ] Policy unit-tested against scripted telemetry sequences.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E8-S1 | FakeAdvisor and failure fixtures | not started |
| E8-S2 | PydanticAI OpenRouter implementation | not started |
| E8-S3 | Call-frequency policy | not started |

Epic status: **not started** — depends on E2.
