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
  serial captures, `.env` files, or local IDE folders. The one exception:
  small contract/validation fixtures under `tests/fixtures/` (plan §8) —
  e.g. the 7 Jun 2026 live-roast JSONL/summary excerpts the MCP mirrors
  validate against.
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
6. Watch the PR to green, then resolve every review finding (see the merge
   policy below), then squash-merge, delete the branch, and flip the story's
   project item to Done.

### PR Merge Policy

Watching a PR, addressing its feedback, and squash-merging it is
pre-authorized — no need to ask before merging your own story PR once it is
clean.

- **Green CI is necessary but not sufficient.** The automated code-review
  bot (and any human reviewer) routinely leaves substantive comments while
  CI is green — correctness edge cases, observability gaps, dead code,
  missing coverage. Read every comment before merging.
- For each finding: fix it (cheap, clearly-correct ones — just do them), or
  state in the thread why it is not being actioned (reviewer marked it "no
  action", cosmetic, or unreachable). Never merge with an un-triaged
  comment.
- **Coverage regressions must be sorted, not waved through.** If
  `codecov/patch` fails, either add the missing test or tag a genuinely
  unreachable defensive/narrowing line `# pragma: no cover` (the repo
  convention — see `store.py`, `mcp_client.py`). Do not lower thresholds to
  pass.
- Re-run the checks after any fix; only merge once CI is green *and* every
  comment is resolved or consciously dismissed.
- **`main` is branch-protected (13 Jun, enforces this policy at the platform).**
  Required: the CI checks + `codecov/patch`, `required_conversation_resolution`
  (every review thread resolved), and `enforce_admins` (no bypass for owner or
  agents); force-push/deletion off; repo auto-merge on. **`claude-review` is
  intentionally NOT a required check** — it fails by design on PRs that edit a
  workflow file (the App's workflow-validation guard) and on Dependabot PRs (no
  secrets), and it passes-on-findings; so the findings gate is its **inline
  comments** (`--comment`) + conversation-resolution, not the check itself. Don't
  re-add it as required (it would deadlock workflow PRs). Green CI alone never
  means mergeable.
- **Independent triage when work is delivered by an agent team (D23).** PR
  review feedback (GitHub Claude Code Review, codecov, and a `/review-branch`
  roster pass) is adjudicated by the lead/PM or the `pr-triage` subagent —
  *never* by the author teammate self-dismissing comments on its own PR. The
  author fixes; someone else decides what counts as resolved. (Each review is a
  fresh instance with no authoring context — independent of the author even when
  it's the same model; this rule keeps the *triage decision* independent too.)

## Code Review Rubric

The automated **Claude Code Review** (`.github/workflows/claude-code-review.yml`,
running `/code-review --comment`) and any reviewer follow this rubric.

**Inline PR comments are MERGE-BLOCKING** — `main` requires every conversation
resolved (branch protection). So calibrate where findings go:

- **Inline (blocking): only genuine must-fix / should-fix findings,** each tagged
  severity — **blocker** (cannot merge), **medium** (fix or justify in-thread),
  **low** (optional but worth it). Resolving the thread is the conscious triage.
- **Summary comment (non-blocking): nits, praise, questions, observations.** Never
  clog the merge gate with trivia — a nitpick posted inline blocks the merge.
- **Don't duplicate CI.** `ruff` / `pyright` (strict) / `pytest` / `codecov/patch`
  already gate; review for what they can't see, not what they catch.
- **Be concrete:** `file:line` + the specific fix.

**Must-block (the Architecture Invariants above — flag any of these as `blocker`):**

- a roaster write that does not pass through safety policy;
- the advisor handed MCP write tools, or the controller no longer owning the loop;
- a restart path that auto-resumes heat/fan (must enter `operator_recovery_required`);
- any Fahrenheit value or conversion (temperatures are Celsius everywhere);
- a string-compared `SafetyVerdict`/`RoastPhase`, or a `StrEnum` where a plain
  `Enum` is required (string comparison must stay a pyright error);
- the SPA calling MCP directly or inferring roast phase locally.

**Escalate:** any diff touching `safety.py`, `controller.py`, or `models.py` enums —
call it out in the summary so it is routed to `safety-reviewer`.

**Also verify:** tests assert real behavior (not smoke); new code is covered or
carries `# pragma: no cover` *with a reason* (repo convention — see `store.py`,
`mcp_client.py`); public functions/methods have type hints + Google docstrings.

> Note: `claude-review` is intentionally **not** a required status check — it fails
> by design on PRs that edit a workflow file (the App's workflow-validation guard),
> and it passes even when it finds bugs. The real findings-gate is these inline
> threads + `required_conversation_resolution`; the required checks are CI + codecov.

## Claude Code

- Sub-agents live under `.claude/agents/`. **Domain reviewers:**
  `safety-reviewer` (PRs touching safety/controller/enums), `mcp-contract-checker`
  (dependency bumps), `sim-roast-runner` (mock vertical slice + decision-trace
  summaries), `ui-reviewer` (Playwright against the replay harness). **Team
  roles** (define each once; reuse as an agent-team teammate, standalone, or a
  workflow stage): `product-pm` (product reviewer — audit vs plan, record
  decisions, write the next brief; never edits src/tests), `qa` (test quality
  beyond coverage), `pr-triage` (independent PR-feedback triage — also the
  `triage-pr` skill), `engineer-fe` (web/ SPA), `engineer-be` (Python agent). The
  human is the lead + domain expert/architect, consulted on escalations.
- **Skills** (`.claude/skills/`): `triage-pr` (→ `pr-triage`), `capture` (drive
  the replay harness + SPA, screenshot a named page state — E10+).
- **Workflows** (`.claude/workflows/`): `review-branch` (cross-checked roster
  review of the branch diff).
- **MCP** (`.mcp.json`): the Microsoft **Playwright MCP** (`@playwright/mcp`) —
  agent-driven browser/screenshots for `ui-reviewer`'s direction-match review
  (D24). Interactive sessions only; the deterministic CI gate is the scripted
  `@playwright/test` snapshot suite, not the MCP.
- **Agent-team worktree isolation:** for a parallel fan-out, the lead creates one
  explicit `git worktree` per teammate and each teammate self-locates every
  command (cwd resets between bash calls) — the Agent tool's `isolation: worktree`
  flag silently no-op'd for background team agents in this environment. Runbook:
  `docs/agent-team-worktrees.md`. Verify `git worktree list`; serialize as the
  fallback for any shared file surface.
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
