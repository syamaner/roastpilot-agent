# RoastPilot Agent Project State Registry

## Active Epic

- Epic file: `docs/epics/E07-api.md`
- Project: RoastPilot (GitHub user project, owner `syamaner`)
- Repository: `syamaner/roastpilot-agent`
- Package: `roastpilot-agent`
- Import package: `roastpilot_agent`
- Console entrypoint: `roastpilot-agent`
- Current phase: M1 build (harness complete target: July 2026)
- **July milestone (D17)** — "harness complete" = (1) E9 vertical slice
  green in CI + (2) E10 dashboard usable for a live roast + (3) one
  supervised real-hardware roast end-to-end. E11/E12 polish may run into
  August; demo assets recorded by end of August. Every session optimizes
  for this finish line; the first supervised hardware session is targeted
  for **June**.

## Working Rules

- Before starting implementation, read this registry, then the active epic
  file, then the GitHub issue for the story.
- One PR per story; branch `feature/{issue-number}-{slug}`; the PR that
  completes a story updates the epic file's status table in the same PR.
- Plans live in `~/git/roastpilot-plan` and are the source of truth; record
  resolved open items in component plan §11.
- Closing an epic = create the next epic's story issues from its spec
  file, update this registry, and flip the epic's project item to Done;
  an epic's project item goes In Progress when its first story does.
- Epic order: E1 ✅ → E2 ✅ → E3 ✅ → E4 ✅ → E5 ✅ → E6 ✅ → **E8** (advisor), then E7 →
  E9 (vertical slice) → E10 (SPA) → E11 (packaging) → E12 (validation/demo).

## Active Context

E1–E6 are complete: safety policy, deterministic controller, typed MCP
client, and SQLite persistence (schema v2 with trigger-enforced
completed-run immutability, typed write paths with per-tick commits,
recovery reads proven across the E4/E6 seam). E8-S1–S3 are complete (the
advisor layer behind RoastAdvisor); E8-S4 (the advisor bake-off, #57) is
the human-operator §11.1 resolution path and stays open — it does not
block E7.

**E7 (API + SSE) is complete** — #67 (REST routes), #68 (operator action
queue), #69 (SSE stream) all merged; epic tracking #70 closed. The full
REST + SSE surface the SPA renders from: one backend authority, the SPA
never calls MCP. `RoastService` in `api.py` is the backend authority and
the E9 wiring seam (store + active-run guard + operator queue +
`EventBroadcaster`). E8-S4 (advisor bake-off, #57) remains the only open
M1 story and is human-operator-owned.

**E7's SSE contract is ready.** `models.SseEventType` / `SseEvent` /
`TelemetryEventData` + `api.EventBroadcaster` are the typed event surface
E9 (vertical slice) and E10 (SPA) render from.

**Next: E9 and E10 run in parallel** (vertical slice + SPA), to be set up
deliberately — not yet started. E9 wires the live controller tick loop +
MCP child into `RoastService` (drain the operator queue through full
safety; emit events + per-tick telemetry into the broadcaster) and adds
controller handlers for the four operator actions without one today
(`mark_beans_added`, `start_cooling`, `pause_advisory`, `resume_advisory`
— see `docs/epics/E07-api.md` E9 notes). The E10 UI kickoff brief is
waiting in the plan repo.
