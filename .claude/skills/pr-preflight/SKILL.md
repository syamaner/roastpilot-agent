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

Run the gates for what the diff touches. A post-open "fix lint/format/types"
commit is exactly the rework we are cutting, so run them HERE.

- **Python** (`src/` or `tests/` changed):
  `python -m ruff check . && python -m ruff format --check . && python -m pyright && python -m pytest`
- **Web** (`web/` changed), from `web/`:
  `npm run lint && npm run typecheck && npm test && npm run build`
- **Cross-boundary contract — if the diff touches the contract surface, run BOTH sides'
  gates, regardless of which side you edited.** The contract surface = any SSE event
  kind, shared model, or cross-side schema. A "backend-only" change there (e.g. adding
  a server event kind) reddens the FE event-kind contract test, so it must run the
  **web gates too** (and a FE-only contract change must run the Python gates), and
  regenerate any contract fixtures (e.g. `sse_frames`) **here, pre-open** — never as
  post-open commits. If unsure whether your change is contract-surface, run both.

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

## 4. Domain review ON THE BRANCH (shift review left — MANDATORY)

Before opening — not after — run the right reviewer against the branch diff and
**resolve its findings**. This step is NOT optional: gates passing is not a
substitute for review, and skipping it just moves findings to post-open rework
(the measured failure mode — review findings were still landing post-open from the
bots because this pass was skipped).
- touches `safety.py` / `controller.py` / `models.py` enums / the recovery or
  command×phase path → **safety-reviewer** (Agent);
- test quality / coverage / acceptance-criteria coverage → **qa** (Agent);
- otherwise, a general code-review pass over the diff.

Fold every finding in BEFORE opening, and **note in the PR body how many the
pre-open review caught** (e.g. "pre-open review: 3 findings folded"). That count is
shift-left's real output and the only place it is visible — the PR-flow metrics are
structurally blind to it (they can only measure the PRs you open, not the rework you
prevented).

- **Don't fold LOW findings as post-open commits.** Per the Code Review Rubric lows
  are non-blocking; fix them in this pre-open pass or defer/dismiss them in-thread,
  never as a separate post-open commit.
- **Healthy rework stays.** The bots (Claude Code Review, Augment) still run
  post-open; on a cleaner branch they find less, but a reviewer catching a real
  defect is the system working. Remove the *catchable-pre-open* findings, not the
  review itself.

## Only when 1–4 pass

Open the PR (`gh pr create`), then follow the **PR Merge Policy** in AGENTS.md
(independent triage — the author never triages its own PR; every conversation
resolved; `codecov/patch` green; squash-merge; delete the branch).
