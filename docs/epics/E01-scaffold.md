# E1 — Scaffold

## Goal

A repository where every later epic starts from green: package layout, all
four quality gates passing in CI from the first commit, typed module stubs,
agent rules (AGENTS.md per D14), sub-agents, and the epic spec files
themselves.

## Plan links

- Component plan §9 (epic table), §4 (module design):
  `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § Initial Repository Layout, § First Code Checklist:
  `roastpilot-plan/roastpilot-agent-orchestration-plan.md`
- D14 (AGENTS.md canonical, CLAUDE.md = `@AGENTS.md`):
  `roastpilot-plan/00-repository-structure.md`

## Stories

### E1-S1 — Repository scaffold ([#1](https://github.com/syamaner/roastpilot-agent/issues/1))

Acceptance criteria:

- [x] `pyproject.toml`: Python 3.11+, `src/roastpilot_agent/` layout, runtime
  deps (FastAPI, Pydantic, pydantic-settings, PydanticAI, MCP client,
  aiosqlite, httpx), dev deps (ruff, pyright, pytest).
- [x] `ruff check`, `ruff format --check`, `pyright` (strict), `pytest` all
  green locally and in GitHub Actions CI.
- [x] Nine typed module stubs per plan §4: controller, mcp_client, advisor,
  safety, store, api, replay, models, config.
- [x] `tests/conftest.py` placeholders: fake MCP client, fake advisor, temp
  SQLite store, event-sink test double.
- [x] `AGENTS.md` with architecture invariants; `CLAUDE.md` = `@AGENTS.md`.
- [x] `.claude/agents/`: safety-reviewer, mcp-contract-checker,
  sim-roast-runner, ui-reviewer.
- [x] `docs/epics/E01…E12` + `docs/state/registry.md`.
- [x] README extended with dev setup.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E1-S1 | Repository scaffold | done (scaffold PR) |

Epic status: **done** — completed by the scaffold PR itself (kickoff
convention).
