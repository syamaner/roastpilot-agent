# RoastPilot Agent Project State Registry

## Active Epic

- Epic file: `docs/epics/E04-controller.md`
- Project: RoastPilot (GitHub user project, owner `syamaner`)
- Repository: `syamaner/roastpilot-agent`
- Package: `roastpilot-agent`
- Import package: `roastpilot_agent`
- Console entrypoint: `roastpilot-agent`
- Current phase: M1 build (harness complete target: July 2026)

## Working Rules

- Before starting implementation, read this registry, then the active epic
  file, then the GitHub issue for the story.
- One PR per story; branch `feature/{issue-number}-{slug}`; the PR that
  completes a story updates the epic file's status table in the same PR.
- Plans live in `~/git/roastpilot-plan` and are the source of truth; record
  resolved open items in component plan §11.
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
