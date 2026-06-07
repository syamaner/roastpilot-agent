# E5 — MCP Client

## Goal

The typed wrapper over the verified 13-tool coffee-roaster-mcp surface,
owning the stdio child process (D6): spawn, health, restart → recovery.
Contract fixtures recorded from the real server keep the mirrors honest.

## Plan links

- Component plan §2 (verified MCP contract), §8 (`test_mcp_client.py`):
  `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § MCP Integration:
  `roastpilot-plan/roastpilot-agent-orchestration-plan.md`

## Stories

### E5-S1 — Pydantic mirrors and typed tool wrappers

Acceptance criteria:

- [ ] Pydantic mirrors of `RoastSessionState`, `T0Status`,
  `FirstCrackStatus`, `ExportRoastLogResult` (all temps Celsius; derived
  metrics passed through, not recomputed).
- [ ] Typed methods for exactly the 13 tools — no arbitrary tool execution
  surface.

### E5-S2 — Child-process lifecycle (D6)

Acceptance criteria:

- [ ] Spawn coffee-roaster-mcp as stdio child; health check; clean shutdown.
- [ ] Crash/restart of the child surfaces as a typed failure the controller
  maps to recovery — never silent reconnect-and-continue.
- [ ] Every MCP call is timeout-bounded — a hung read or write (including
  `emergency_stop`) must raise rather than stall the tick; only the
  advisor call is bounded today (safety-reviewer carry-forward, E4-S2 PR).

### E5-S3 — Contract fixtures

Acceptance criteria:

- [ ] Real `RoastSessionState` JSON captured from the actual MCP server
  (mock driver) committed under `tests/fixtures/`.
- [ ] Mirror models validate all fixtures; mcp-contract-checker sub-agent
  documented as the re-validation path on dependency bumps.
- [ ] Read/write failure paths tested.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E5-S1 | Pydantic mirrors and typed tool wrappers | not started |
| E5-S2 | Child-process lifecycle (D6) | not started |
| E5-S3 | Contract fixtures | not started |

Epic status: **not started** — depends on E2.
