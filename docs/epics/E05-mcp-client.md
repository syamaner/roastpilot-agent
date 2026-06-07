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

### E5-S1 — Pydantic mirrors and typed tool wrappers ([#38](https://github.com/syamaner/roastpilot-agent/issues/38))

Acceptance criteria:

- [x] Pydantic mirrors of all nine result shapes, derived from the actual
  coffee-roaster-mcp source: `RoastSessionState` (with nested
  `RoasterDeviceState`/`T0Status`/`FirstCrackStatus`/`EventSnapshot`),
  `ExportRoastLogResult`, `StartRoastSessionResult`,
  `ControlCommandResult`, `EventCommandResult`, `ServerInfo`,
  `RuntimeConfigSnapshot`. All temps Celsius; derived metrics passed
  through; `extra="ignore"` so upstream additions never crash the agent
  (drift detection is the contract checker's job).
- [x] Typed methods for exactly the 13 tools, wire names pinned; a test
  asserts the public surface is exactly those 13 — no generic execution.
- [x] Mirrors validated against both 7 Jun live-roast exports from day
  one (committed under `tests/fixtures/live-roast-2026-06-07/`; manual
  empty-payload `beans_added` and `auto_t0` payload both accepted;
  AGENTS.md fixtures exception documented).

### E5-S2 — Child-process lifecycle (D6) ([#39](https://github.com/syamaner/roastpilot-agent/issues/39))

Acceptance criteria:

- [x] `MCPServerProcess` spawns the stdio child via the SDK, initializes
  the session (startup timeout), health-checks through the public surface
  (`get_server_info`), and shuts down cleanly via AsyncExitStack. The
  real-child integration test runs whenever `coffee-roaster-mcp` is on
  PATH (auto-skips until E9 adds the dependency).
- [x] Spawn argv is `coffee-roaster-mcp serve` — pinned by a test
  (server.json packageArguments alignment).
- [x] Every transport fault is a typed `MCPConnectionError` (dead child,
  broken pipe, server-side isError, not-started) — one except-clause for
  the controller's consecutive-failure rules; never silent
  reconnect-and-continue.
- [x] Every MCP call is bounded by `MCPConfig.call_timeout_seconds`
  (5.0 s, justified in config) — a hung call (including `emergency_stop`)
  raises `MCPToolTimeoutError` (E4-S2 carry-forward closed; provenance on
  #39).

### E5-S3 — Contract fixtures ([#40](https://github.com/syamaner/roastpilot-agent/issues/40))

Acceptance criteria:

- [x] One captured fixture per tool — all 13, from the real published
  coffee-roaster-mcp 0.1.3 in bootstrap-safe mock mode, captured through
  the agent's own E5-S2 transport (`scripts/capture_mcp_fixtures.py`;
  re-run on dependency bumps). Committed under
  `tests/fixtures/mcp-tool-results/`; completeness pinned by a test.
- [x] Both 7 Jun live-roast exports included as validation fixtures
  (landed in E5-S1): manual empty-payload `beans_added` and `auto_t0`
  payload both accepted.
- [x] Every fixture validates into its mirror (parametrized over all 13);
  the fixture→mirror map documents the re-validation path together with
  the mcp-contract-checker sub-agent.
- [x] Read/write failure paths tested (E5-S2's typed-failure suite:
  timeout, dead child, server-side isError on a write tool, malformed
  payload, not-started).

## Status

| Story | Title | Status |
|-------|-------|--------|
| E5-S1 | Pydantic mirrors and typed tool wrappers | done |
| E5-S2 | Child-process lifecycle (D6) | done |
| E5-S3 | Contract fixtures | done |

Epic status: **done** — all three stories complete; the agent can drive the real MCP server end to end (capture run doubled as the first real-spawn validation).
