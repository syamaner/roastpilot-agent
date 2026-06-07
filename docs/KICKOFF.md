# E1 Kickoff — roastpilot-agent

Hand-off document for the implementation session. Written 7 June 2026 at the
end of the planning phase. Delete this file once E1 is merged (its content
will live on in AGENTS.md, docs/epics/, and the issues).

## Paste-ready kickoff prompt

> Read docs/KICKOFF.md in this repo, then execute it.

Or the explicit version:

> Read ~/git/roastpilot-plan/roastpilot-agent/plan.md and
> ~/git/roastpilot-plan/roastpilot-agent-orchestration-plan.md, plus
> ~/git/roastpilot-plan/00-repository-structure.md for decisions D1–D14.
> Execute epic E1 per plan §9, then set up GitHub tracking as described in
> docs/KICKOFF.md.

## Context (where the truth lives)

The program/spec repo is a sibling checkout: **`~/git/roastpilot-plan`**
(github.com/syamaner/roastpilot-plan, private).

Read before writing any code:

1. `roastpilot-plan/roastpilot-agent/plan.md` — this repo's component plan:
   decisions D5–D9, verified MCP contract, phase mapping, module design,
   SQLite schema v1, REST+SSE API, SPA spec, test plan, epics E1–E12,
   sub-agent definitions.
2. `roastpilot-plan/roastpilot-agent-orchestration-plan.md` — authoritative
   architecture (controller, state machine, safety policy, advisor). Its
   repo-structure section is superseded (banner inside).
3. `roastpilot-plan/00-repository-structure.md` — decisions D1–D14, incl.
   **D14**: AGENTS.md is canonical, CLAUDE.md is exactly `@AGENTS.md`.

UI reference material: `roastpilot-plan/roastpilot-agent/sketches/`
(Figma Make exports + frozen screenshots — reference specs, never seed code)
and `ui-prompts.md` (the prompt pack; also the chart spec of record).

## The plan repo is writable — keep it true

If implementation reveals that a plan is wrong, ambiguous, or needs a new
decision:

- **Update `~/git/roastpilot-plan` in the same work session** — edit the
  relevant plan document, assign the next decision number (D15, …) if it is
  an agreed decision, commit with a clear message, and push.
- Plan-repo rules are in its `AGENTS.md`: plans are source of truth, never
  renumber decisions, `archive/` is read-only, CFP accuracy boundaries apply
  to anything public.
- Resolved open items (plan §11: OpenRouter model slug, `drop_beans` cooling
  behavior, wheel packaging of `web/dist`, Safari/iPad SSE) get their
  resolution recorded in plan §11, not just in code.

## E1 deliverables (plan §9, expanded per D14)

1. `pyproject.toml` — Python 3.11+, `src/roastpilot_agent/` layout, deps per
   plan §4 (FastAPI, Pydantic, pydantic-settings, PydanticAI, MCP client,
   aiosqlite, httpx; dev: ruff, pyright, pytest).
2. Quality gates green from the first commit: `ruff check`,
   `ruff format --check`, `pyright` (strict), `pytest`.
3. Module skeletons per plan §4: `controller.py`, `mcp_client.py`,
   `advisor.py`, `safety.py`, `store.py`, `api.py`, `replay.py`,
   `models.py`, `config.py` — typed stubs only.
4. `tests/conftest.py` — placeholders for fake MCP client, fake advisor,
   temp SQLite store, event-sink test double.
5. GitHub Actions CI running all four gates.
6. **`AGENTS.md`** — canonical repo rules, templated on
   `~/git/coffee-roaster-mcp/AGENTS.md` (quality gates, one PR per story,
   branch `feature/{issue-number}-{slug}`, registry → epic → issue reading
   order), plus this repo's architecture invariants:
   - the advisor never receives MCP write tools;
   - every roaster write passes safety policy; verdicts are typed
     (`ALLOW/CLAMP/REJECT/FAULT/EMERGENCY_STOP` — never string-compared);
   - restart never auto-resumes heat/fan (`operator_recovery_required`);
   - temperatures are Celsius everywhere;
   - the SPA renders from server events, never infers phase locally.
7. `CLAUDE.md` — exactly one line: `@AGENTS.md`.
8. `.claude/agents/` — the four sub-agents from plan §10: safety-reviewer,
   mcp-contract-checker, sim-roast-runner, ui-reviewer.
9. `docs/epics/E01-scaffold.md` … `E12-validation-demo.md` — one spec file
   per epic from plan §9: goal, links to exact plan sections, story
   breakdown with acceptance criteria, status table. Plus
   `docs/state/registry.md` (active-epic pointer, mirroring the
   coffee-roaster-mcp convention).
10. README: extend the existing one with dev setup once the scaffold runs.

## GitHub tracking (after the scaffold PR)

- User-level Project **"RoastPilot"** (`gh project create --owner syamaner`),
  custom fields: `Epic` (E1…E12, C1…C7, LA-1, LA-2), `Milestone` (M1/M2).
- Story issues for E1–E3 in this repo, added to the project; an epic
  tracking issue per epic whose body is a task list of story refs
  (cross-repo refs use the full `owner/repo#n` form).
- Later epics get issues as they approach — don't pre-create all of them.

## Conventions recap

- One PR per story; branch `feature/{issue-number}-{slug}`.
- The PR that completes a story updates the epic spec's status table in the
  same PR (file state and GitHub state never drift).
- E1's own epic file is marked done by the scaffold PR itself.
- After E1: **E2 (models & config) then E3 (safety policy)** — dependency-free,
  test-heavy, the heart of the system and of the September talk.
