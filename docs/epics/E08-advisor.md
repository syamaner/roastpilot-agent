# E8 — Advisor

## Goal

The advisory layer behind the `RoastAdvisor` interface: deterministic
`FakeAdvisor` for tests/demos, the provider-agnostic PydanticAI
implementation (D5 + D18 — one advisor consuming a config-built model), the
call-frequency policy, and the advisor bake-off that picks the default
provider/model slug (plan §11.1). Advisory-only, always: typed output in,
safety policy after.

## Plan links

- Component plan §1 (D5), §4 (advisor specifics), §8 (`test_advisor.py`),
  §11.1 (open item: model slug; bake-off resolution path recorded at the
  7 Jun 2026 product review): `roastpilot-plan/roastpilot-agent/plan.md`
- Decision **D18** (provider-agnostic advisor via a config-selected
  PydanticAI model factory — supersedes the OpenRouter-only reading of D5):
  `roastpilot-plan/roastpilot-agent/plan.md`
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

### E8-S2 — PydanticAI provider-agnostic implementation (D5, D18) ([#54](https://github.com/syamaner/roastpilot-agent/issues/54))

Acceptance criteria:

- [x] One `PydanticAIAdvisor` consuming a PydanticAI `Model` built by a
  `build_model(config)` factory — **not** one advisor class per provider.
  The factory maps `AdvisorConfig.provider` → a `Model`: native
  `openai` / `anthropic` / `google` via PydanticAI's provider classes;
  `ollama` / `openai_compatible` via an OpenAI-compatible model at
  `provider_base_url` (OpenRouter by default, or a LAN Ollama URL).
- [x] Structured output, prompt versioning, context-hash logging (not raw
  payloads), and the typed-error mapping
  (`AdvisorError` / `AdvisorMalformedOutputError` /
  `AdvisorUnsafeOutputError`) live once in `PydanticAIAdvisor` —
  provider-independent. Only `Model` construction varies per provider.
- [x] The API key is read at build time from the env var named by
  `api_key_env` and handed to the provider — never stored in config or DB.
- [x] Native providers are optional dependency extras
  (`anthropic`, `google`); a minimal install (`openai_compatible` /
  `ollama` / `openai`) stays lean. Each provider value documents the extra
  it needs.
- [x] Each provider path is exercised behind a recorded-response double
  (plan §8) — no live calls in CI. The factory's provider→`Model` mapping
  is unit-tested for every enum value.
- [x] Ships a working provisional default (`openai_compatible` + the
  OpenRouter base URL). The settled default provider/`model_slug` and the
  plan §11 item 1 closure belong to E8-S4 (the bake-off).

Guardrails:

- `FakeAdvisor` stays the test/CI default — no API key and no network in
  CI.
- At most one manual smoke run per provider actually intended for use
  (never in CI).

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
| E8-S2 | PydanticAI provider-agnostic implementation | done |
| E8-S3 | Call-frequency policy | done |
| E8-S4 | Advisor bake-off | not started |

Epic status: **in progress** — E2 ✅; issues minted
([#53](https://github.com/syamaner/roastpilot-agent/issues/53)–[#55](https://github.com/syamaner/roastpilot-agent/issues/55),
[#57](https://github.com/syamaner/roastpilot-agent/issues/57); epic
tracking [#56](https://github.com/syamaner/roastpilot-agent/issues/56)).
