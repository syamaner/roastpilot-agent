# E2 — Models & Config

## Goal

The full typed vocabulary of the system: shared enums and Pydantic models,
RoastProfile (D7), the configuration surface (ControllerConfig,
AdvisorConfig, SafetyLimits, AppConfig), and the typed safety handshake
(SafetyVerdict, SafetyEvaluation). Dependency-free and test-heavy — the
foundation E3 (safety) and everything else builds on.

## Plan links

- Component plan §1 (D7), §4 (module design, advisor specifics):
  `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § Configuration Model, § First Code Checklist (typed
  safety handshakes): `roastpilot-plan/roastpilot-agent-orchestration-plan.md`

## Stories

### E2-S1 — Shared enums and event vocabulary ([#3](https://github.com/syamaner/roastpilot-agent/issues/3))

Acceptance criteria:

- [x] `RoastPhase` (9 phases per plan §3) and agent event kinds (plan §5
  `roast_events.kind` vocabulary) as typed enums — `RoastEventKind` (14
  kinds) and `RoastEventSource` in models.py.
- [x] `SafetyVerdict` + `SafetyEvaluation` finalized (six verdicts, nullable
  adjusted command per D15); no string comparison possible without a type
  error (plain `Enum`, pinned by tests).
- [x] Round-trip serialization tests for every enum (`tests/test_models.py`).

### E2-S2 — RoastProfile (D7) ([#4](https://github.com/syamaner/roastpilot-agent/issues/4))

Acceptance criteria:

- [x] Minimal static profile: name, bean origin/varietal/weight, charge
  guidance range (default 170–200 °C), initial heat/fan, target drop temp,
  target development %. No curve targets.
- [x] Validation tests: bounds, defaults, rejected nonsense values
  (whitespace-only strings, zero/negative weight, out-of-range percents,
  inverted/empty guidance band), plus JSON round trip.

### E2-S3 — Configuration surface ([#5](https://github.com/syamaner/roastpilot-agent/issues/5))

Acceptance criteria:

- [x] `ControllerConfig` with documented timing defaults (1.0 s tick, 3-tick
  T0 debounce, advisory thresholds, 5 s telemetry interval, 3 s staleness) —
  all validated positive.
- [x] `AdvisorConfig` per D5 (OpenRouter base URL, api_key_env, model_slug,
  timeout, temperature, prompt_version); model slug stays empty until E8
  (plan §11.1).
- [x] `SafetyLimits` with the full limit set E3 needs: max bean (230 °C) /
  env (240 °C) temps, pre-T0 bound 200 °C, overrun safe-fan + severity
  (recovery|fault), command rate limit (2.0 s) — conservative software
  ceilings justified in docstrings, flagged for E12-S1 hardware validation.
- [x] `AppConfig` loads from env (`ROASTPILOT_*`, `__` nesting) with tests
  (defaults, nested overrides, invalid-value rejection).

## Status

| Story | Title | Status |
|-------|-------|--------|
| E2-S1 | Shared enums and event vocabulary | done |
| E2-S2 | RoastProfile (D7) | done |
| E2-S3 | Configuration surface | done |

Epic status: **done** — all three stories complete; next epic is E3
(safety policy) per the kickoff order.
