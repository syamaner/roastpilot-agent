# RoastPilot Agent Project State Registry

## Active Epic

- Epic file: `docs/epics/E03-safety-policy.md`
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
- Epic order: E1 ✅ → E2 ✅ → **E3** (safety policy) → E4–E8 →
  E9 (vertical slice) → E10 (SPA) → E11 (packaging) → E12 (validation/demo).

## Active Context

E1 (scaffold) and E2 (models & config) are complete: typed vocabulary
(enums per D15/D16, RoastProfile per D7) and the full configuration surface
(timing, advisor per D5, conservative SafetyLimits pending E12 hardware
validation) are merged with tests. Next up is E3 (safety policy) — the
heart of the system and of the September talk. Stories #6–#9 and #15
(D16) are ready with acceptance criteria.
