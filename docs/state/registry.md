# RoastPilot Agent Project State Registry

## Active Epic

- Epic file: `docs/epics/E05-mcp-client.md`
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
- Epic order: E1 ✅ → E2 ✅ → E3 ✅ → E4 ✅ → **E5** (MCP client) / E6 / E7 / E8 →
  E9 (vertical slice) → E10 (SPA) → E11 (packaging) → E12 (validation/demo).

## Active Context

E1–E4 are complete: safety policy and the full deterministic controller
(transition table, tick pipeline, T0 debounce, run lifecycle, restart
recovery) are merged — 278 tests. Next up is E5 (MCP client, issues
#38–#40): typed mirrors, stdio child lifecycle with per-call timeouts,
contract fixtures. E6 (store) and E8 (advisor) are also unblocked.
Notable: plan §11 items 1–2 status — item 2 (drop_beans cooling) resolved
7 Jun via coffee-roaster-mcp's live hardware roast; real-hardware export
fixtures now exist for E5-S3.
