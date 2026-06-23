---
name: pr-preflight
description: Run the full pre-open preflight on the current branch BEFORE opening a PR — gates, a size + data/logic split check, an adversarial self-critique, and the domain reviewer on the branch — so review findings and lint fold into the first push instead of becoming post-open rework. Use before opening any PR.
---

Run this on the PR branch **before** `gh pr create`. The build's PR-flow metrics
flag **large PRs** and **high rework** (most rework is review findings landing
*after* the PR opens). This checklist opens a PR that is already small and clean,
so the post-open review/rework loop shrinks. Work the four steps in order; fix at
each before moving on; **do not open the PR until all four pass.**

## 0. Orient

!`git branch --show-current`
!`git diff --stat origin/main`

## 1. Gates — green before opening (not after)

Run only the gates for what the diff touches. A post-open "fix lint/format/types"
commit is exactly the rework we are cutting, so run them HERE.

- **Python** (`src/` or `tests/` changed):
  `python -m ruff check . && python -m ruff format --check . && python -m pyright && python -m pytest`
- **Web** (`web/` changed), from `web/`:
  `npm run lint && npm run typecheck && npm test && npm run build`

If a gate fails, fix it and re-run before continuing.

## 2. Size + data/logic split

- **Logic PRs target ≤ ~400 changed lines.** Over that, split the story into
  thinner vertical slices (a story may be several stacked PRs) rather than ship
  one large PR.
- **Separate data from logic.** Fixtures, snapshots, generated files, research
  output, bake-off results belong in their OWN PR or commit — never bundled with
  logic. They inflate size and don't need code review the way logic does. If the
  diff mixes them, split them out now.

## 3. Adversarial self-critique of the diff

Read your own diff as a hostile reviewer would. Check:
- edge cases + failure modes the change introduces;
- new behaviour has tests that assert **real behaviour, not smoke**;
- observability gaps, dead code, leftover debug;
- every **Architecture Invariant** the diff could touch (AGENTS.md): safety policy
  on every roaster write; controller owns the loop (advisor never gets MCP write
  tools); restart never auto-resumes heat/fan; Celsius only; plain `Enum` not
  `StrEnum` and no string-compared verdicts; the SPA renders from server
  events/snapshots and never infers phase or calls MCP directly.

Fix what you find now, before the PR exists.

## 4. Domain review ON THE BRANCH (shift review left)

Before opening — not after — run the right reviewer against the branch diff and
resolve its findings, so review-caught fixes fold into the first push:
- touches `safety.py` / `controller.py` / `models.py` enums / the recovery or
  command×phase path → **safety-reviewer** (Agent);
- test quality / coverage / acceptance-criteria coverage → **qa** (Agent).

The automated reviewers (Claude Code Review, Augment) still run post-open, but on
a cleaner branch they find less. The goal is to remove the *catchable-pre-open*
findings from the rework count — **not** to skip review: a reviewer catching a
real defect is the system working, so keep the genuinely-new findings.

## Only when 1–4 pass

Open the PR (`gh pr create`), then follow the **PR Merge Policy** in AGENTS.md
(independent triage — the author never triages its own PR; every conversation
resolved; `codecov/patch` green; squash-merge; delete the branch).
