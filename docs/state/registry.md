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

**Active: E7 (API + SSE).** Issues minted #67–#69 (epic tracking #70).
E7 is unblocked (depends on E4 ✅, E6 ✅; no E8 dependency). The full
REST + SSE surface the SPA renders from — one backend authority, the SPA
never calls MCP. The typed SSE event set is E7's most important output:
E9 (vertical slice) and E10 (SPA) both render from it. E7 establishes the
API + SSE contract and the operator action queue; E9 wires the live
controller tick loop + MCP child to drive it.

When E7 closes: E9 and E10 run in parallel next (vertical slice + SPA) —
the E10 UI kickoff brief is waiting in the plan repo.
