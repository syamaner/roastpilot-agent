# AGENTS.md - roastpilot-agent

Project rules and context for coding agents working in this repository.

## Architecture Invariants

These hold for every change, in every epic. PRs that weaken one are wrong by
definition.

- **The controller owns the loop; the LLM advises.** The advisor never
  receives MCP write tools. It returns typed `RoastDecision` data only.
- **Every roaster write passes safety policy.** No code path delivers
  advisor output (or operator input) to `mcp_client` without a
  `SafetyEvaluation`. Verdicts are typed —
  `ALLOW / CLAMP / REJECT / RECOVERY / FAULT / EMERGENCY_STOP` (six values,
  D15) — and never string-compared in core logic. Shared enums are plain
  `Enum`, not `StrEnum`, so a string comparison is a pyright strict error.
- **Restart never auto-resumes heat or fan.** A restart with a
  possibly-active run enters `operator_recovery_required`; explicit operator
  action is required to resume, drop, cool, or end the run. Emergency stop
  stays available from every phase.
- **Temperatures are Celsius everywhere** — models, schema, API, UI, tests.
- **The SPA renders from server events and snapshots.** It never infers
  roast phase locally and never calls MCP tools directly.

## Rules

- Python 3.11+ with full type hints on all public functions and methods.
- Google-style docstrings for public modules, classes, functions, and methods.
- `ruff check`, `ruff format --check`, `pyright` (strict), and `pytest` must
  pass before marking implementation complete.
- All runtime and dev dependencies must be declared in `pyproject.toml`.
  Never install ad-hoc dependencies without adding them to project metadata.
- Keep roaster hardware control conservative. Heat, fan, drop, cooling, and
  emergency stop behavior require explicit tests or manual validation notes.
- All M1 tests run hardware-free: fake MCP client, or the real
  `coffee-roaster-mcp` in mock-driver mode. Do not mark hardware stories
  complete from mock tests alone.
- Do not commit model weights, audio files, roast logs, SQLite databases,
  serial captures, `.env` files, or local IDE folders.
- One PR per story, branch: `feature/{issue-number}-{slug}`.
- The PR that completes a story updates the epic file's status table in the
  same PR — file state and GitHub state never drift.
- Before starting a task: read `docs/state/registry.md`, open the active
  epic file, then check the GitHub issue.
- The program/spec repo is `~/git/roastpilot-plan`
  (github.com/syamaner/roastpilot-plan). If implementation reveals a plan is
  wrong or ambiguous, update the plan repo in the same work session (next
  decision number, clear commit). Plans are the source of truth.
- Public text (README, docs) follows the plan repo's accuracy boundaries:
  the LLM is advisory-only and never controls hardware; no determinism
  percentages; no "fully autonomous"; no "production-ready" before
  end-to-end hardware validation.

## Quick Commands

### Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . --group dev
```

### Test

```bash
python -m pytest
```

### Lint And Format

```bash
python -m ruff check .
python -m ruff format --check .
```

### Typecheck

```bash
python -m pyright
```

(CI adds `--pythonpath` because the runner has no `./.venv` for pyproject's
`venvPath`/`venv` settings to resolve — do not "simplify" the CI step to the
bare command.)

### CLI Smoke

```bash
roastpilot-agent --help
roastpilot-agent --version
```

## Codebase Architecture

```text
src/roastpilot_agent/
  __init__.py     - package version
  cli.py          - console entrypoint
  controller.py   - transition table, tick() loop, T0 debounce (re-exports
                    RoastPhase from models.py, its home per D15)
  mcp_client.py   - typed wrapper over the 13 coffee-roaster-mcp tools; owns
                    the MCP child process (spawn, health, restart → recovery)
  advisor.py      - RoastAdvisor ABC, AdvisorContext, RoastDecision,
                    PydanticAIAdvisor (OpenRouter), FakeAdvisor
  safety.py       - SafetyVerdict, SafetyEvaluation, rule set, rate limits
  store.py        - aiosqlite store, schema v1, recovery reads
  api.py          - FastAPI: REST + SSE + static web/ mount; replay mode
  replay.py       - ReplaySource: recorded exports through the real SSE
                    pipeline at 1×–60×
  models.py       - shared Pydantic models & enums (RoastPhase, RoastProfile)
  config.py       - ControllerConfig, AdvisorConfig, SafetyLimits, AppConfig
tests/
  conftest.py     - fake MCP client, fake advisor, temp SQLite store,
                    event-sink test double
web/              - Vite + React + TS SPA (E10; built into the wheel at E11)
docs/state/
  registry.md     - active project state pointer
docs/epics/
  E01…E12         - epic spec files: goal, plan links, stories, status table
```

## Key Design Decisions

Authoritative sources: `roastpilot-plan/roastpilot-agent/plan.md` (D5–D9),
`roastpilot-plan/roastpilot-agent-orchestration-plan.md` (architecture),
`roastpilot-plan/00-repository-structure.md` (D1–D14).

- D5: advisor provider is OpenRouter via PydanticAI; model slug is config;
  tests always use a deterministic fake.
- D6: the agent spawns `coffee-roaster-mcp` as a stdio child process; agent
  restart ⇒ clean MCP restart into the recovery flow.
- D7: minimal static roast profiles in M1 — no curve targets.
- D8: M1 SPA scope is dashboard + roast detail + history.
- Controller tick is 1.0 s, set by the Hottop thermocouple response time.
- MCP phases are inputs; agent phases are the operator-facing truth
  (mapping in component plan §3).
- T0 and first crack are accepted from MCP detection; operator marking is
  recovery-only (T0) or an explicit override (FC).
- SQLite runs WAL + `synchronous=FULL`; commit per tick during active roasts.

## Epic State Management

Before starting a story:

1. Read `docs/state/registry.md`.
2. Open the active epic file listed in the registry.
3. Read the GitHub story issue and any comments.
4. Confirm acceptance criteria and current risks.
5. Work on a branch named `feature/{issue-number}-{slug}`.

After completing a story:

1. Run required checks.
2. Update story status in the active epic file.
3. Update decision notes when behavior changed.
4. Comment on the GitHub story issue with what changed and how it was tested.
5. Open a PR referencing the story issue.

## Claude Code

- Sub-agents live under `.claude/agents/`: `safety-reviewer` (PRs touching
  safety/controller/enums), `mcp-contract-checker` (dependency bumps),
  `sim-roast-runner` (mock vertical slice + decision-trace summaries),
  `ui-reviewer` (Playwright against the replay harness).
- `CLAUDE.md` contains exactly `@AGENTS.md` — rules belong here, never there.

## Hardware Safety Notes

- Hottop command behavior (drop, cooling, emergency stop, temperature units)
  requires explicit validation before any hardware-ready claim; E12 owns the
  supervised validation stories.
- Unsafe or uncertain hardware behavior fails closed: heat off, record a
  fault event, preserve enough state for diagnosis.
- Whether `drop_beans` engages cooling on the real Hottop is an open
  verification story (component plan §3); the controller handles both
  outcomes.
