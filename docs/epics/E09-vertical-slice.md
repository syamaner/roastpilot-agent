# E9 — Vertical Slice

## Goal

The 12-step mock milestone test wiring E4–E8 together: a full roast from
service start to recoverable completed state, first against the fake MCP
client, then against the real coffee-roaster-mcp server in mock-driver mode.
No hardware, no microphone, no model download.

## Plan links

- Component plan §8 (`test_milestone1.py`):
  `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § First Milestone (the 12 steps):
  `roastpilot-plan/roastpilot-agent-orchestration-plan.md`

## Stories

### E9-S1 — 12-step slice against the fake MCP client

Acceptance criteria:

- [ ] All 12 steps pass end to end: start service → start roast (mock) →
  stream state → mock auto-T0 → FC (override or mock status) → one advisory
  decision through the fake adapter → safety validation → heat/fan command
  → drop → stop cooling → export logs → restart proves completed state
  recoverable.
- [ ] Decision trace (advisory → verdict → command) persisted and readable
  via the timeline route.

### E9-S2 — Slice against real MCP server (mock driver, subprocess)

Acceptance criteria:

- [ ] Same flow with the agent spawning the real `coffee-roaster-mcp` as a
  stdio child (D6) in mock-driver mode.
- [ ] Runs in CI (no hardware/audio/model deps).
- [ ] sim-roast-runner sub-agent produces a markdown decision-trace summary
  of a slice run.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E9-S1 | 12-step slice, fake MCP | not started |
| E9-S2 | Slice against real MCP (mock mode) | not started |

Epic status: **not started** — depends on E4–E8.
