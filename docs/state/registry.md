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
recovery reads proven across the E4/E6 seam).

**E8 (Advisor) is complete** — all four stories: `FakeAdvisor` (#53), the
provider-agnostic `PydanticAIAdvisor` (#54, D18), the change-based
call-frequency policy (#55), and the operator-judged bake-off (#57, D20).
The bake-off set the default to `anthropic/claude-opus-4.8` via OpenRouter
with the electric-roaster prompt `v1`, resolving plan §11 item 1 — the last
M1 open item. Captured in `docs/advisor-bakeoff-2026-06-08.md` (reproducible
via `scripts/advisor_bakeoff.py`); default is config-swappable (D18).

**E7 (API + SSE) is complete** — #67 (REST routes), #68 (operator action
queue), #69 (SSE stream) all merged; epic tracking #70 closed. The full
REST + SSE surface the SPA renders from: one backend authority, the SPA
never calls MCP. `RoastService` in `api.py` is the backend authority and
the E9 wiring seam (store + active-run guard + operator queue +
`EventBroadcaster`).

**M1 stories are all closed through E8.** Next is E9 + E10 (see below) —
not yet started; awaiting a deliberate kickoff.

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
