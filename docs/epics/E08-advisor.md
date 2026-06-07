# E8 — Advisor

## Goal

The advisory layer behind the `RoastAdvisor` interface: deterministic
`FakeAdvisor` for tests/demos, the OpenRouter-backed PydanticAI
implementation (D5), the call-frequency policy, and the advisor bake-off
that picks the default model slug (plan §11.1). Advisory-only, always:
typed output in, safety policy after.

## Plan links

- Component plan §1 (D5), §4 (advisor specifics), §8 (`test_advisor.py`),
  §11.1 (open item: model slug; bake-off resolution path recorded at the
  7 Jun 2026 product review): `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § PydanticAI Advisory Layer, § Advisory Call Frequency:
  `roastpilot-plan/roastpilot-agent-orchestration-plan.md`

## Stories

### E8-S1 — FakeAdvisor and failure fixtures ([#53](https://github.com/syamaner/roastpilot-agent/issues/53))

Acceptance criteria:

- [x] Deterministic `FakeAdvisor` with scriptable decisions (absorbs/
  replaces the conftest ScriptedAdvisor split).
- [x] Fixtures: valid / malformed / unsafe / timeout / provider error —
  each produces a rejected recommendation + deterministic fallback (hold
  current targets), every outcome persisted.

### E8-S2 — PydanticAI OpenRouter implementation (D5) ([#54](https://github.com/syamaner/roastpilot-agent/issues/54))

Acceptance criteria:

- [ ] OpenAI-compatible OpenRouter endpoint via PydanticAI; strict output
  models; versioned prompts; context hashes (not raw payloads) logged.
- [ ] Tested behind a recorded-response double — no live calls in CI.
- [ ] Structured-output settings confirmed; ships with a provisional
  default model slug — the final default and the plan §11 item 1 closure
  belong to E8-S4 (the bake-off).

Guardrails:

- `FakeAdvisor` stays the test/CI default — no API key and no network in
  CI.
- The OpenRouter path is exercised behind a recorded-response double
  (plan §8) plus one manual smoke run against the live endpoint.

### E8-S3 — Call-frequency policy ([#55](https://github.com/syamaner/roastpilot-agent/issues/55))

Acceptance criteria:

- [x] Advisor called only on meaningful change: ≥1.0 °C bean temp delta,
  ≥2.0 °C/min RoR delta, phase transition, ≥15 s minimum interval, or
  manual trigger; timeout-bounded; never blocks the tick.
- [x] Replaces the controller's interim manual-trigger-only
  `_advisory_requested` flag (E4-S2 note): `request_advisory()` is now the
  manual override into `AdvisoryCallPolicy`, which the tick consults every
  cycle.
- [x] Policy unit-tested against scripted telemetry sequences.

### E8-S4 — Advisor bake-off (plan §11.1) ([#57](https://github.com/syamaner/roastpilot-agent/issues/57))

Acceptance criteria:

- [ ] Replay the same recorded roast context — the 7 Jun 2026 live-roast
  fixtures (`tests/fixtures/live-roast-2026-06-07/`) — through 3–4
  candidate OpenRouter model slugs.
- [ ] Compare `RoastDecision` quality side by side. The operator judges
  advice quality — the story's output is the comparison document + chosen
  default, not an automated metric.
- [ ] Record the comparison in `docs/` (talk material).
- [ ] Set the winning slug as the config default and resolve plan §11
  item 1 in the plan repo in the same work session.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E8-S1 | FakeAdvisor and failure fixtures | done |
| E8-S2 | PydanticAI OpenRouter implementation | not started |
| E8-S3 | Call-frequency policy | done |
| E8-S4 | Advisor bake-off | not started |

Epic status: **in progress** — E2 ✅; issues minted
([#53](https://github.com/syamaner/roastpilot-agent/issues/53)–[#55](https://github.com/syamaner/roastpilot-agent/issues/55),
[#57](https://github.com/syamaner/roastpilot-agent/issues/57); epic
tracking [#56](https://github.com/syamaner/roastpilot-agent/issues/56)).
