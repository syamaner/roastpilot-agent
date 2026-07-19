---
name: pr-preflight
description: Run the full pre-open preflight on the current branch BEFORE opening a PR — gates, a size + data/logic split check, an adversarial self-critique, the domain reviewer (incl. security-reviewer for external-input surfaces), and a draft-PR Codex diverse-lens loop before marking ready — so review findings and lint fold into the first push instead of becoming post-open rework. Use before opening any PR.
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
- **Coverage — run `pytest` with BRANCH coverage + missing-line reporting and read the CHANGED lines (#452, refined 9 Jul).**
  `python -m pytest --cov=roastpilot_agent --cov-branch --cov-report=term-missing`. Every line your
  diff ADDS or CHANGES must be covered — **and every BRANCH arm too**: `codecov/patch` counts
  partial branches (`x->y` arrows) against the patch, so a "0 missed lines" run can still fail the
  gate. Cover it, or tag it `# pragma: no cover` **with a reason** (repo convention — see
  `store.py` / `mcp_client.py`) now. Prefer a test over a pragma for "defensive" arms — two false
  "unreachable" pragmas were disproven on 9 Jul; if an arm is provably dead, DELETE it rather than
  suppress it. (On #426 a malformed-env-blob branch shipped uncovered because term-missing ran only
  after the PR opened; on #492 three DIFF branch-partials failed `codecov/patch` post-open because
  the pre-open run lacked `--cov-branch`.)
- **Web** (`web/` changed), from `web/`:
  `npm run lint && npm run typecheck && npm test && npm run build`
- **Cross-boundary contract — if the diff touches the contract surface, run BOTH sides'
  gates, regardless of which side you edited.** The contract surface = any SSE event
  kind, shared model, or cross-side schema. A "backend-only" change there (e.g. adding
  a server event kind) reddens the FE event-kind contract test, so it must run the
  **web gates too** (and a FE-only contract change must run the Python gates), and
  regenerate any contract fixtures (e.g. `sse_frames`) **here, pre-open** — never as
  post-open commits. If unsure whether your change is contract-surface, run both.
- **Real-data validation — converters / replay tooling / anything that ingests recorded
  roast data must be run against a REAL store or log pre-open, not just a synthetic
  fixture.** Green gates and 100 % coverage on synthetic data prove nothing about real
  data: a synthetic fixture is built from the same mental model as the code and inherits
  its blind spots. The #300 converter passed 27 tests at full coverage while silently
  reading the drop temperature off the cooled-down tail, because the synthetic store
  could not reproduce the real store's two time origins (telemetry timestamped relative to
  run start, vs events carrying the raw `time.monotonic()` value with its own arbitrary
  process-uptime origin, never rebased to run start), its missing terminal event, or an
  unfinalised run. Run it on a real recorded roast (e.g.
  `~/.local/state/roastpilot/roastpilot.sqlite3`; outputs are gitignored, never commit
  them) and paste a **sanitised summary** of the real run into the PR body before opening
  — the derived values that prove correctness (drop temp, degree, row counts, truncation),
  not raw paths, usernames, or full telemetry.

If a gate fails, fix it and re-run before continuing.

## 2. Size + data/logic split

- **The 400-line logic cap is a HARD STOP — the number is exact, not "~400".** Measure
  **logic** lines from the branch's MERGE BASE (so an advancing `origin/main` doesn't
  inflate the count): `git diff --stat $(git merge-base origin/main HEAD)` (equivalently
  `git diff --stat origin/main...HEAD`), minus data/fixtures/generated/doc files. If that
  exceeds **400**, STOP — do not open. The story should already have a **PR plan from
  kickoff** (AGENTS.md PR-Hygiene: the ordered list of thin PRs decided *before* writing
  code); over the cap means the plan was too coarse or you merged two planned slices —
  split it back to the planned boundary and open the current slice only. Reactively
  discovering the size here is the failure this catches; plan the slices up front, don't
  split a monolith at review time.
- **Separate data from logic.** Fixtures, snapshots, generated files, research
  output, bake-off results belong in their OWN PR or commit — never bundled with
  logic. They inflate size and don't need code review the way logic does. If the
  diff mixes them, split them out now.

## 3. Adversarial self-critique of the diff

Read your own diff as a hostile reviewer would. Check:
- edge cases + failure modes the change introduces;
- new behaviour has tests that assert **real behaviour, not smoke**;
- observability gaps, dead code, leftover debug;
- **cross-issue regression guard — don't reintroduce a sibling PR's just-fixed bug (#453).**
  Each branch in a batch is written fresh-context and can reintroduce a class of bug a sibling
  PR *just* fixed (on the Tier-1 batch, #409 reintroduced #404's "chart marker one tick early"
  anti-pattern). Before opening, check the diff against **`docs/recent-fixes.md`** (one line per
  recently-fixed anti-pattern + a grep signature); if your diff matches a signature, apply the
  same fix. **When you fix a CLASS of bug, ADD an entry** so the next sibling PR is warned — this
  is how the agent team externalises the shared memory it otherwise lacks;
- **fix the CLASS, sweep the repo — never per-symptom (the round-2..N rework engine).** When a
  finding (yours or a reviewer's) is one instance of a class — a sanitizer that misses one escape,
  one un-normalized value, one un-mapped parse, one un-audited grant target — fix it in one place
  (a shared helper) and `grep` the whole repo for siblings before pushing. A per-symptom patch just
  moves the same finding to the next round on the next line; one categorical fix collapses the loop.
  (This is what turned #587's whack-a-mole into a class-sweep, and what `security-reviewer` does by
  design — sweep the category, not the instance.);
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
- **fetches or parses untrusted external input, adds an external-input endpoint, or
  adds a new LLM-provider call path** → **security-reviewer** (Agent), working
  `docs/review/untrusted-input-checklist.md`. This routing is **capability-based, not
  file-based**: it fires even when the diff touches none of the safety files — a brand-new
  fetch/parse surface is the highest-risk case *and* the easiest to miss (the #587 lesson:
  a fetch endpoint took NINE post-open Codex rounds because no pre-open lens covered it).
  If class 6 (cross-feature contention with the roast loop) applies, ALSO run safety-reviewer;
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
- **Healthy rework stays.** Claude Code Review (and any human reviewer) still runs
  post-open; on a cleaner branch it finds less, but a reviewer catching a real
  defect is the system working. Remove the *catchable-pre-open* findings, not the
  review itself.

## 5. Fold the DIVERSE lens on a DRAFT before "ready" — draft → `@codex review` → fold

Steps 1–4 are the Claude family (author + subagent reviewers); they share blind spots —
a same-family lens co-accepts a bug the author already rationalised. **Codex is a
different model family and catches exactly that class**, and the measured gap is large
(#587: nine Codex rounds, all real; roastpilot-cloud F1-S8: 5 rounds, ~15 real P1s on a
security keystone two Opus safety passes called clean — all post-open). Put Codex in the
loop **while the PR is still a draft**, so its finds fold instead of becoming post-ready
rework:

1. Open the PR as a **draft** (`gh pr create --draft`), and **record the current head sha**.
2. **Codex auto-reviews at PR creation** (AGENTS.md), so opening the draft usually fires it —
   only post `@codex review` if it did *not* fire. Don't double-trigger.
3. **Wait for the verdict on the recorded head sha** — a **posted review** carries a
   `Reviewed commit:` line (match it to the head); a **top-level "Codex Review: Didn't find
   any major issues" COMMENT with a `Reviewed commit:` line matching the head = complete-clean**
   (the MOST COMMON clean channel — don't record a run as "silent" without checking the issue
   comments); a **👍 after 👀 = complete-clean** but
   **carries NO commit line**, so it's only trustworthy against the head sha you recorded at
   trigger *with no push since* — a 👀 reaction = still reviewing (keep waiting). **Every verdict
   signal counts only when authored by the Codex bot identity** (`chatgpt-codex-connector[bot]` —
   public repo, every channel — 👀, findings-review, clean comment, and 👍 alike — is forgeable by any user). (These are the
   AGENTS.md Codex-wait signals — read those, don't invent a timestamp threshold; the
   ~30-min-from-👀 stall + silent-fallback windows there are the only sanctioned time-based
   exits.) **Freeze the head at acceptance:** a verdict only counts if the reviewed/recorded sha
   **still equals the current head**; a concurrent teammate push after the trigger invalidates a
   later 👍 (it describes the old head) — re-check the sha and re-trigger if it moved.
4. Fold every real finding **by CLASS** (step 3 — one categorical fix + repo-sweep, not
   per-symptom). **Triage stays author-independent (D23):** when the work is delivered by an
   agent teammate, the author fixes but does **not** decide which findings are "real" or dismiss
   them on its own draft — route the draft findings through the lead / `pr-triage`, exactly as
   post-open. (A solo human-authored PR self-triages as usual.)
5. **Re-run the branch gates + domain review (steps 1–4) after any code change** — a Codex
   fold can break a gate or reopen a review finding; the pre-ready results only count if
   they reflect the final draft state.
6. Re-trigger Codex only after a **settled** batch of folds (not on every intermediate
   push — the draft phase is where iteration lives, but don't spam the trigger), and
   re-match the new head sha.

**Iterating with Codex here is expected and correct** — this is the pre-ready phase. The
AGENTS.md **"once-on-final-commit, don't re-litigate"** rule governs the **post-ready**
phase (a marked-ready PR heading to merge), where re-triggering across pushes is the churn
we avoid. Draft = iterate to clean; ready = clean already.

> Note on `claude-review`: opening a draft does run `.github/workflows/claude-code-review.yml`
> (it listens to `opened`). That is fine — `claude-review` is **not** a required check and
> passes-on-findings; its draft runs are cheap signal, not a gate. Don't churn on them.

**KPI:** count **pre-ready Codex folds** as their own line in the PR body (alongside the
step-4 pre-open review count). This does NOT redefine AGENTS.md's *pre-open* rework
boundary (findings folded before the PR is *opened*) — a draft PR is open, so draft-phase
folds are a distinct, additional shift-left category. Both counts trending up is the win.

## Only when 1–5 pass

Mark the PR **ready** (`gh pr ready`), then follow the **PR Merge Policy** in AGENTS.md
(independent triage — the author never triages its own PR; every conversation resolved;
`codecov/patch` green; the post-ready once-on-final-commit Codex discipline; squash-merge;
delete the branch).

**No redundant post-ready re-trigger.** If the last draft-phase Codex pass already reviewed the
**exact head sha** you're marking ready (no commit added between the clean draft verdict and
`gh pr ready`), that verdict already satisfies once-on-final-commit — don't re-trigger Codex just
because the PR flipped ready. Re-trigger post-ready only if the head sha actually changed.
