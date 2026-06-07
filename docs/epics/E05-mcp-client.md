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

### E5-S3 — Contract fixtures

Acceptance criteria:

- [ ] Fixtures cover one example per tool result shape (not just
  `get_roast_state`), captured from the actual MCP server (mock driver),
  committed under `tests/fixtures/`.
- [ ] The two real live-roast exports
  (`coffee-roaster-mcp/docs/validation/2026-06-07-live-roast/{session,session-2}/`)
  are included as validation fixtures. Note: session 1 has a **manual**
  `beans_added` event with an empty payload while session 2 carries the
  `auto_t0` payload (source, charge/drop metadata) — the mirrors must
  accept both shapes (plan repo f0e9502 extends the Loop A source-marker
  change to cover this).
- [ ] Mirror models validate all fixtures; mcp-contract-checker sub-agent
  documented as the re-validation path on dependency bumps.
- [ ] Read/write failure paths tested.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E5-S1 | Pydantic mirrors and typed tool wrappers | done |
| E5-S2 | Child-process lifecycle (D6) | done |
| E5-S3 | Contract fixtures | not started |

Epic status: **in progress** (E5-S1, E5-S2 done; E5-S3 remains).
