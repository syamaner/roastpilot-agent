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
2. **One worktree per occupant** — its own directory and intended revision:
   - **Implementer / teammate doing new work:** create its branch from
     `origin/main`:
     ```bash
     git worktree add /Users/<you>/git/rp-<slug> -b feature/<n>-<slug> origin/main
     ```
   - **Read-only occupant (reviewer, triage, QA):** detach at the exact commit
     under review:
     ```bash
     git worktree add --detach <path> <reviewed-sha>
     ```
     `<reviewed-sha>` must be the reviewed commit, not `origin/main` and not a
     branch tip that has moved since the review was requested. A reviewer in a
     base-branch worktree renders a verdict about bytes nobody asked it to
     review; see `docs/agent-topology.md` §8 item 6.
   Each worktree has its own index and HEAD, so git operations (branch, commit —
   the things that actually collided) never cross even though they share one
   `.git`.
   Provisioning also includes the gate environment described below, created by
   the teammate as its first act or by the lead for a read-only occupant that
   cannot mutate the tree.
3. **Verify before trusting:** `git worktree list` shows each occupant on its
   own directory and intended branch or detached reviewed SHA. The flag failed
   *silently*; manual creation + this check fails *loud*.
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
- Run every Python gate via `<abs worktree>/.venv/bin/python -m ...` per the
  gate-environment section below; never use the main repository's `.venv`.
- Read-only peeks at the main repo are fine via `git -C <main repo> ...`; never
  *write* there.
- Push from your worktree (push works normally from one) and REPORT the sha to
  the lead; by default the LEAD opens the PR and owns the review cycle (see
  "Branch freeze on PR-open" below). Open the PR yourself only when the lead
  has explicitly delegated it — and then the freeze rule applies to you: the
  moment YOUR PR is open, the branch is frozen and further pushes need the
  cycle-owner's go-ahead.

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

## Per-worktree gate environment (venv, pyright, pytest) — added Aug 2026 (#738, #733)

This is the same family of "isolation is not as isolated as it looks" failure as
the worktree-flag incident above. A separate index and HEAD do not isolate Python
import paths, installed dependency versions, or pytest's temporary directory.

There are two failure directions:

1. **Too permissive.** When the worktree has **no independently created `.venv`
   of its own**, either passing `--pythonpath` with another checkout's interpreter
   or reusing/symlinking another checkout's venv can follow that editable
   install's `.pth` file and resolve `roastpilot_agent` from the **OTHER checkout**.
   The dangerous outcome is a **false PASS**: a clean or near-clean report about
   another tree. Simply invoking the main interpreter without `--pythonpath` or a
   borrowed venv instead fails loudly with `venv .venv subdirectory not found`
   and a flood of missing-import errors. The worktree's `venvPath = "."` /
   `venv = ".venv"` settings are relative to its `pyproject.toml`: once a genuine
   per-worktree venv exists, it wins and imports the worktree copy.
   `PYTHONPATH=<abs worktree>/src` is a fallback for diagnosing the no-venv case,
   not the primary gate recipe.
2. **Too strict.** A worktree venv created with `--system-site-packages` inherits
   machine-global packages. In the observed incident it resolved numpy 2.3.4
   instead of the repository venv's 2.4.6 and fabricated 12 type errors in a file
   the branch had not touched.

The per-worktree venv is the fix for the first direction and the vector for the
second, so this recipe is a single unit: **do not follow only half of it.** Create
and validate the environment before trusting any gate:

```bash
cd <abs worktree> && python3.11 -m venv .venv
cd <abs worktree> && .venv/bin/python -c 'import sys; from pathlib import Path; p = Path(sys.prefix).resolve(); print(p); print(p.is_relative_to(Path("<abs worktree>").resolve()))'
cd <abs worktree> && grep -Fx 'include-system-site-packages = false' .venv/pyvenv.cfg
cd <abs worktree> && .venv/bin/python -m pip list
cd <abs worktree> && .venv/bin/python -m pip install --upgrade pip
cd <abs worktree> && .venv/bin/python -m pip install -e . --group dev
cd <abs worktree> && .venv/bin/python -c 'import roastpilot_agent as r; from pathlib import Path; p = Path(r.__file__).resolve(); print(p); print(p.is_relative_to(Path("<abs worktree>/src").resolve()))'
cd <abs worktree> && .venv/bin/python -c 'import numpy, sys; from pathlib import Path; p = Path(numpy.__file__).resolve(); print(p); print(p.is_relative_to(Path(sys.prefix).resolve()))'
cd <abs worktree> && env -u PYTHONPATH .venv/bin/python -m ruff check .
cd <abs worktree> && env -u PYTHONPATH .venv/bin/python -m ruff format --check .
cd <abs worktree> && env -u PYTHONPATH .venv/bin/python -m pyright
cd <abs worktree> && env -u PYTHONPATH -u OPENROUTER_API_KEY .venv/bin/python -m pytest --basetemp "$(mktemp -d)" --cov=roastpilot_agent --cov-branch --cov-report=term-missing
```

**Never create this venv with `--system-site-packages`.** Immediately after venv
creation, the prefix command must print the resolved path and then `True`, proving
component-aware containment within `<abs worktree>`; `pyvenv.cfg` must contain
the exact false setting above, and the first, pre-install `pip list` should be
short. `False` identifies a borrowed or symlinked venv and means this is
**not a valid gate environment — discard the gate result and rebuild the venv**.
The prefix realpath assertion catches whole-venv borrowing — a `.venv` that is
itself a symlink or resolves outside the worktree — but cannot detect a symlinked
subdirectory inside an otherwise local venv. A config mismatch or a long
inherited package list (for example, Django or torch is already present) has the
same fail-closed result; never report gates from that environment. After
dependency installation, the **primary assertion** is the first-party path
command: it must print the resolved path and then `True`, proving component-aware
containment within `<abs worktree>/src/`. `False` means the gates are about to
describe the wrong tree and this is **not a valid gate environment — discard the
gate result and reinstall from the worktree or rebuild the venv**. An editable
install pointed at the wrong directory triggers this; that is what happens when
`pip install -e .` runs without self-locating, the cwd-resets rule above biting
inside the gate recipe itself. This first-party assertion also catches
subdirectory-level borrowing; it directly checks where the package under test
comes from. Numpy remains the third-party provenance canary, including for
`--system-site-packages` contamination: the recipe must print the resolved
package path and then `True`, proving component-aware containment within the
resolved `sys.prefix`. `False` means contamination, so this is **not a valid gate
environment — discard the gate result and rebuild the venv**. The prefix,
first-party, and third-party path assertions cover different borrowing depths;
none is individually sufficient. Every gate unsets inherited `PYTHONPATH`, which
can silently outrank the venv for tooling imports even when both package probes
pass. **A containment or provenance check is only as good as the comparison it
performs and the environment it performs it in.** Version comparison is
informational only: dependencies are ranged and
the repository has no lockfile, so a fresh venv may legitimately resolve a newer
version than an older repository venv; that difference must **not** trigger a
rebuild. It is worth noticing while diagnosing an odd type error, but is not
evidence of contamination. In the Aug 2026 incident, the repository venv had
numpy 2.4.6 while inherited machine-global site-packages supplied 2.3.4.

The pytest details are part of the gate recipe too. Unsetting
`OPENROUTER_API_KEY` prevents an exported provider key from silently greening
tests that fail in CI. Pytest's default basetemp, `/tmp/pytest-of-$USER/`, is
shared across worktrees; concurrent runs collide there and produce phantom
failures that have twice triggered real P1 investigations. Every worktree run
therefore passes its own `--basetemp "$(mktemp -d)"`. It is unique per run,
leaves nothing in the tree, and needs no `.gitignore` change. Every changed line
and branch arm must be covered before opening, because `codecov/patch` counts
partial branches.

### Manual pyright isolation verification

Use this manual mutation check when provisioning or investigating resolution;
it is intentionally not an automated test. It tests an import, because passing
pyright an explicit source-file path bypasses the resolution behavior at issue.

1. Make a scratch directory outside every worktree, then snapshot both files by
   copy:

   ```bash
   cd <abs worktree> && cp src/roastpilot_agent/models.py <abs scratchpad>/models.py
   cd <abs worktree> && cp scripts/advisor_smoke.py <abs scratchpad>/advisor_smoke.py
   ```

2. In the worktree's `models.py`, append a uniquely named, correctly typed
   symbol such as `WORKTREE_GATE_PROBE: int = 738`. In the worktree's
   `advisor_smoke.py`, import that symbol from `roastpilot_agent.models`, then
   append the deliberate type error
   `worktree_gate_error: str = WORKTREE_GATE_PROBE`. These edits exist only in
   the worktree; the second file is the probe module.
3. Run the worktree's own configured typecheck:

   ```bash
   cd <abs worktree> && .venv/bin/python -m pyright
   ```

   It **MUST** report that an `int` cannot be assigned to `str` at the probe
   assignment, and must not report that `WORKTREE_GATE_PROBE` is an unknown
   import. That pair of observations proves the import resolved to the
   worktree's package rather than an editable `.pth` target elsewhere.
4. Restore both snapshots by file copy — never `git checkout --`, `git restore`,
   `git stash`, `git reset`, or `git clean`:

   ```bash
   cd <abs worktree> && cp <abs scratchpad>/models.py src/roastpilot_agent/models.py
   cd <abs worktree> && cp <abs scratchpad>/advisor_smoke.py scripts/advisor_smoke.py
   cd <abs worktree> && .venv/bin/python -m pyright
   ```

   On the untouched worktree, pyright **MUST** report 0 errors. The first run
   proves it saw the worktree-only mutation; the clean run proves the environment
   did not fabricate errors.

For delegated sandboxed implementers specifically, lack of network access can
make a faithful dependency install structurally impossible. Their gate output is
**unverified evidence — the lead re-runs the full gates lead-side before review**.
An implementer that cannot install must say so, never improvise with
`--system-site-packages`.

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
   `git restore`, `git stash`, `git reset`, `git clean`) in a worktree you don't
   own.** Claims about a commit's contents must use `git show HEAD:path`, not the
   working tree.
   Mutation tests are encouraged — they caught real test gaps all night — but
   the revert mechanism must be file-scoped, never git-scoped.

---

*Provenance: E10 agent-team experiment. Failure + recovery narrated in the blog
source `career/.../blog-sources/05-when-not-to-fan-out.md`; smoke-test validation
9 Jun 2026. Reviewer rules: the 9 Jul 2026 overnight batch incident.
Gate-environment rules: #732 phantom-error and #756 delegation incidents (Aug
2026), #738/#733.*

## Branch freeze on PR-open (added 14 Jul 2026, after the third FIFO-lag crossing)

The teammate mailbox delivers FIFO **on idle only**, so the lead and a teammate
can cross: a ruling arrives after the teammate proceeded, or a teammate's late
fold arrives after the lead already opened the PR and fired the review trigger
(PR #547 hit both in one evening — absorbed only because the trigger hadn't
fired yet).

Rule: **opening the PR is the branch-ownership handoff — WHOEVER opens it.**

1. When the lead opens a PR on a teammate's branch, the accompanying message
   includes an explicit **FREEZE** — no further pushes without a lead
   go-ahead. When a teammate opens their own PR (lead-delegated — see the
   teammate rules above), the same freeze binds them from the moment it opens:
   the review-cycle owner (the lead) controls all further pushes.
2. Every teammate "pushed sha X" report re-freezes by default; unfreeze is
   always an explicit lead instruction naming what to fold.
3. If a push lands after the review trigger fired, the verdict is stale by
   definition — the lead re-runs the cycle on the final head and never merges
   on a verdict predating it (the AGENTS.md codex-wait clause).
