# Agent-Team Worktree Isolation — operational runbook

How to run a parallel agent-team fan-out (e.g. E10's S3/S4/S5 page teammates)
without the teammates colliding in one working directory.

## Why this exists

During E10 (the agent-team SPA build, 9 Jun 2026), spawning teammates with the
Agent tool's **`isolation: worktree` flag silently created no worktree** —
`git worktree list` showed only the main checkout, and two background teammates
ended up editing the **same** working directory. Their uncommitted work
interleaved across five files on one branch. It was caught before any corrupt
commit reached a real branch, but only after a careful manual untangle.

**Root cause (verified by a smoke test, 9 Jun 2026):** a teammate's shell
**cwd resets between bash calls**. So neither a flag-set initial cwd nor a
one-time `cd` persists — every command defaults back to the repo it was launched
in unless the command *self-locates*. With no separate worktree actually
created, every teammate's commands landed in the shared main repo.

**The fix, validated:** the *lead* creates worktrees **manually**, and each
teammate **self-locates on every command**. In the smoke test a teammate given a
manual worktree confined to it with no effort; the main repo's tree and branch
were completely untouched.

## Lead runbook (per fan-out)

1. **Foundation merged first.** Worktrees branch off `main`, so the shared
   substrate (the single-owned foundation) must already be merged. Parallelize
   only genuinely-disjoint work (separate page directories), never a shared
   file surface.
2. **One worktree per teammate** — its own directory *and* branch:
   ```bash
   git worktree add /Users/<you>/git/rp-<slug> -b feature/<n>-<slug> origin/main
   ```
   Each worktree has its own index and HEAD, so git operations (branch, commit —
   the things that actually collided) never cross even though they share one
   `.git`.
3. **Verify before trusting:** `git worktree list` shows each teammate on its
   own dir + branch. The flag failed *silently*; manual creation + this check
   fails *loud*.
4. **Spawn each teammate** (background, team) with its **absolute** worktree path
   as its sole workdir, plus the teammate rules below.
5. **Verify isolation on the FIRST teammate** before relying on all of them —
   confirm its reported `pwd`/branch and re-check `git worktree list`. Don't
   assume the mechanism; the flag taught us why.
6. **On merge:** `git worktree remove <dir>` and delete the branch.

## Teammate rules (paste into the spawn prompt)

- Your worktree is `<abs path>` on branch `<branch>`. It is the **only**
  directory you write in. The main repo and sibling worktrees are off limits.
- **cwd resets between bash calls — never rely on a one-time `cd`.** Self-locate
  on *every* command:
  - **git:** `git -C <abs worktree> <subcommand>`
  - **file edits:** the Edit/Write tools take absolute paths and are
    cwd-independent — target `<abs worktree>/...` and they're always correct.
  - **builds/tests:** `cd <abs worktree>/web && npm ...` as a single compound
    command (the `cd` and the command in one bash call).
- Read-only peeks at the main repo are fine via `git -C <main repo> ...`; never
  *write* there.
- Push and open your PR from your worktree — push works normally from one.

## Fallback: serialize

If a teammate can't stay confined, or worktrees misbehave in your environment,
**serialize** — run one writing teammate at a time in the main tree. Proven safe
(it's how the E10 foundation/Python phase ran), just no build-parallelism. Use it
for any work that shares a file surface anyway; reserve worktrees for genuinely
independent directories.

## Do not trust the `isolation: worktree` flag for background team agents

In this environment it silently no-op'd. Create worktrees explicitly and verify
`git worktree list`. Treat the flag as unproven until a `git worktree list` check
says otherwise.

## Reviewers in a shared worktree (added 9 Jul 2026, after a live incident)

During the 9 Jul batch a reviewer ran `git checkout -- <file>` in a teammate's
worktree to undo a one-line hypothesis edit — and wiped the teammate's ENTIRE
uncommitted 200-line diff for that file. It was recovered only because the
reviewer had captured the full diff text earlier (then verified line-by-line by
the author). Two binding rules fell out:

1. **The lead safety-commits the worktree state BEFORE reviews run on it.**
   A local `wip` commit costs nothing (squash/amend at PR time) and makes any
   destructive slip recoverable. Uncommitted work under review is fragile.
2. **Mutation-testing protocol for reviewers:** before ANY hypothesis edit,
   `cp` the target file to the scratchpad; restore by `cp`-ing the snapshot
   back. **Never run tree-mutating git commands (`git checkout --`,
   `git restore`, `git stash`, `git reset`) in a worktree you don't own.**
   Mutation tests are encouraged — they caught real test gaps all night — but
   the revert mechanism must be file-scoped, never git-scoped.

---

*Provenance: E10 agent-team experiment. Failure + recovery narrated in the blog
source `career/.../blog-sources/05-when-not-to-fan-out.md`; smoke-test validation
9 Jun 2026. Reviewer rules: the 9 Jul 2026 overnight batch incident.*

## Branch freeze on PR-open (added 14 Jul 2026, after the third FIFO-lag crossing)

The teammate mailbox delivers FIFO **on idle only**, so the lead and a teammate
can cross: a ruling arrives after the teammate proceeded, or a teammate's late
fold arrives after the lead already opened the PR and fired the review trigger
(PR #547 hit both in one evening — absorbed only because the trigger hadn't
fired yet).

Rule: **opening the PR is the branch-ownership handoff.**

1. When the lead opens a PR on a teammate's branch, the accompanying message
   includes an explicit **FREEZE** — no further pushes without a lead
   go-ahead.
2. Every teammate "pushed sha X" report re-freezes by default; unfreeze is
   always an explicit lead instruction naming what to fold.
3. If a push lands after the review trigger fired, the verdict is stale by
   definition — the lead re-runs the cycle on the final head and never merges
   on a verdict predating it (the AGENTS.md codex-wait clause).
