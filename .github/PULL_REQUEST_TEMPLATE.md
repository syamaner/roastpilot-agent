## What & why
<!-- one or two lines: what this changes and why -->

## Issue
<!-- Use "Closes #N" ONLY for the issue this PR FULLY resolves — it auto-closes the issue on
     merge (keeps the board honest). For partial or related work use "Refs #N" / "Part of #N"
     instead — do NOT use Closes, or it wrongly auto-closes an unfinished issue. -->
Closes #

## pr-preflight (run the skill BEFORE opening — see AGENTS.md "PR Hygiene")
- [ ] Gates green pre-open — incl. the **cross-boundary contract tests** + fixtures regenerated if the diff touches an SSE event kind / model / any contract surface (a "backend-only" change can still red the frontend gate)
- [ ] **Logic vs data separated** — no fixtures / snapshots / research output / generated files bundled with logic
- [ ] **Pre-open domain review** run on the branch (`safety-reviewer` for safety/controller/enum/recovery, `qa` for tests) — findings folded pre-open: ___
- [ ] **LOW findings** resolved-with-reason in-thread, NOT folded as post-open commits

## Notes for reviewers
<!-- anything they should know: escalations, deliberate trade-offs, what to scrutinise -->
