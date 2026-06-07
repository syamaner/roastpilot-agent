# RoastPilot Agent Project State Registry

## Active Epic

- Epic file: `docs/epics/E04-controller.md`
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
- Epic order: E1 ✅ → E2 ✅ → E3 ✅ → **E4** (controller) / E5–E8 →
  E9 (vertical slice) → E10 (SPA) → E11 (packaging) → E12 (validation/demo).

## Active Context

E1–E3 are complete. The full safety policy is merged with exhaustive
tests: temperature ceilings, pre-T0 overrun, telemetry validity, MCP
failure handling, command validation (bounds/rate/drop), unconditional
e-stop, the D16 command×phase matrix, and FC/T0 source validity. Two
safety-reviewer carry-forwards are pinned as E4-S4 criteria (hardware-off
on faulted/recovery entry; failed e-stop lands fail-closed). Next up is
E4 (controller) — story issues to be created when work starts; E5/E6/E8
are also unblocked (depend only on E2).
