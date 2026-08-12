---
name: pr-preflight
description: Run the pre-open gates, size check, self-critique, and minimum sufficient risk-routed independent review before opening a draft PR.
---

Run this on the PR branch **before** `gh pr create`. The build's PR-flow metrics
flag **large PRs** and **high rework** (most rework is review findings landing
*after* the PR opens). This checklist opens a PR that is already small and clean,
so the post-open review/rework loop shrinks. Work steps 1-3, then apply the
minimum-sufficient review routing in `AGENTS.md` before pushing and opening a
draft PR.

**D158 pilot override:** the Codex-led topology supersedes the legacy fixed
Claude-domain-review plus local-Codex-review sequence in sections 4-5 below.
Those sections are retained temporarily as historical rationale for the draft
and ready-head gates, but they are not the pilot's local-review procedure. The
Codex parent selects one independent, diff-focused review and adds only the
contract- or diff-triggered lenses listed in `AGENTS.md`; it does not run the
full roster. Open as a draft and stop there unless the task explicitly authorizes
the ready transition. The GitHub exact-head Claude approval, ready-head Codex
wait, conversation resolution, CI, CodeQL, and Codecov rules remain unchanged.

## 0. Orient

!`git branch --show-current`
!`git diff --stat $(git merge-base origin/main HEAD)`

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

- **Target about 400 changed logic lines as a reviewability guide.** Measure
  **logic** lines from the branch's MERGE BASE (so an advancing `origin/main` doesn't
  inflate the estimate): `git diff --stat $(git merge-base origin/main HEAD)`
  (equivalently `git diff --stat origin/main...HEAD`), minus separated
  data/fixtures/generated/doc files **and minus test files** (operator ruling,
  21 Jul — #621: test bulk is often spec-corpus data in test form).
- **Large test diffs get a qa pass instead of a count.** If the test-file diff exceeds
  **600** lines (exact threshold), run the `qa` reviewer on the branch pre-open and fold
  its findings — test quality (vacuous assertions, wrong expectations) is policed by the
  qa lens, not by rationing test lines.
- The story should already have a **PR plan from kickoff** (AGENTS.md PR-Hygiene:
  coherent review units decided *before* writing code). Split unrelated or independently
  shippable responsibilities at that planned boundary. Do not create awkward interfaces,
  temporary dead code, or extra PRs solely to hit the size target. If the logic diff
  materially exceeds the target, record why the larger unit is more reviewable than the
  available splits, run the applicable domain reviewers, and obtain independent
  pre-open triage. A large unexplained diff must be replanned.
- **Executable `.md` counts as logic, not exempt docs.** The doc exemption is for
  prose (README, design notes, plan files). Agent/skill/workflow definitions under
  `.claude/` (`agents/*.md`, `skills/**/*.md`, `workflows/*.mjs`) are executable behaviour
  — they count toward the logic-size estimate and get review like code.
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

## Legacy sections 4-5 (superseded locally by D158)

The text below records the pre-D158 fixed-review flow and remains useful for its
GitHub lifecycle rationale. Do not execute its local reviewer fan-out during the
pilot; use the risk routing in `AGENTS.md`.

### 4. Former domain-review flow

Before opening — not after — run the right reviewer against the branch diff and
**resolve its findings**. This step is NOT optional: gates passing is not a
substitute for review, and skipping it just moves findings to post-open rework
(the measured failure mode — review findings were still landing post-open from the
bots because this pass was skipped).
- touches `safety.py` / `controller.py` / `models.py` enums / the recovery or
  command×phase path → **safety-reviewer** (Agent);
- **fetches or parses untrusted external input, adds an external-input endpoint, or
  adds a new LLM/model-provider call path — ANY provider or model service, not only the
  backend the roast advisor uses, and in ANY process (a provider-calling CLI, offline job,
  script or test harness counts; the "on the server" framing does not apply to that
  bullet)** → **security-reviewer** (Agent), working
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

### 5. Former local-Codex and ready-transition flow

Steps 1–4 are the Claude family (author + subagent reviewers); they share blind spots —
a same-family lens co-accepts a bug the author already rationalised. **Codex is a
different model family and catches exactly that class**, and the measured gap is large
(#587: nine Codex rounds, all real; roastpilot-cloud F1-S8: 5 rounds, ~15 real P1s on a
security keystone two Opus safety passes called clean — all post-open). Get Codex in
**before the branch is pushed**, so its finds fold instead of becoming post-ready rework:

> **Diverse-lens inversion on a Codex-authored branch (D152).** When the diff was
> implemented by Codex-MCP (the D152 default for contracted slices), the family roles
> swap: this step's local `codex review` is then the SAME-family lens, and the Claude
> domain reviewers in step 4 are the cross-family adversarial lens — so on such a
> branch step 4 is the one that must never be skipped or thinned, and a clean local
> Codex pass carries correspondingly less independent weight. Run both regardless;
> only the rationale shifts, not the checklist.

1. **Run `codex review --base origin/main` LOCALLY, before pushing anything — and COMMIT your
   implementation work before that first pass, not merely the folds that follow it** (Codex P2,
   #682, caught on this fold). `--base` reviews the committed branch diff, so entering preflight
   with the work still staged or unstaged makes the opening pass certify the pre-work `HEAD`,
   return clean having read none of the actual patch, and license committing and pushing that
   never-reviewed work on the strength of it. `git status` must be clean here for exactly the
   reason it must be clean at every re-run below; the commit-first rule is a property of the
   command, not a courtesy owed only to folds. This is the
   pre-ready diverse lens. Corrected 27 Jul 2026 (D142): Codex does **not** auto-review a
   draft. Its connector fires automatically only at the **ready** transition, so a draft
   cannot converge the Codex lens at all, and a workflow that waits for a clean verdict
   there stalls forever. Fold every real finding by CLASS (step 3, which defines the
   categorical fix and the repository-wide sibling sweep) before pushing. **Triage
   stays author-independent here too (D23):** when an agent teammate produced the diff, it
   fixes but does not decide which local Codex findings are real or dismissable; route them
   through the lead or `pr-triage`, exactly as for post-open findings. **Close the loop before
   pushing** (Codex P2, #682): after folding anything, **COMMIT the fold first**, then re-run
   steps 1-4 AND re-run `codex review --base origin/main`, repeating until a Codex pass comes
   back clean with the gates green on that same tree. Committing first is load-bearing, not
   tidiness (Codex P1, #682): `--base` reviews the COMMITTED branch diff, not the working
   tree, so re-running it over an uncommitted fold certifies the previous HEAD and returns
   clean while the actual fix has been seen by nothing. Use `codex review --uncommitted` if
   you genuinely want the working tree reviewed instead, but do not confuse the two. Only then **push the reviewed branch**. Pushing after the
   re-gate but before the next Codex pass ships a tree the diverse lens has never seen, which
   is the gap this step exists to close: the gates describe the pre-fold tree until they are
   re-run, and Codex describes it until IT is re-run.
2. Open the PR as a **draft** (`gh pr create --draft`), and **record the current head sha**.
   The draft phase exists for the **runner-only gates** (`ci.yml`: `ruff`, `pyright`, `pytest`,
   and the web jobs, plus the non-required CodeQL workflow's Actions / JavaScript-TypeScript /
   Python analyses): they run on draft pushes and catch a class local runs cannot, including
   environment-dependent failures that only reproduce on a runner. Fold those here, while the
   CODEX lens is still unspent: its automatic trigger does not fire until ready, so nothing
   you do in this phase consumes it. That is the only lens the draft phase protects. Note
   this repository's
   `claude-review` DOES run on draft `opened`/`synchronize` (unlike the cloud repository's,
   which is draft-suppressed), so its findings appear during the draft phase. **Those findings
   ARE merge-gating**: they are inline threads, and `main` requires every conversation
   resolved, so they block merge exactly like post-ready ones. Only the workflow's status
   check is non-required, which is a different thing from the findings being optional. Fold
   or explicitly resolve each one. A manual `@codex review` on a draft is not forbidden and does
   post findings, but per D105 it never completes the clean-verdict flow on a draft, so it
   can supplement the local pass and can **never** satisfy the merge wait.

   **Draft-phase exit condition — leaving this phase is gated, not a step you simply reach.**
   (Codex P2 ×2, #682. Both reported instances — a draft fold that re-runs only the gates, and
   a `gh pr ready` that never waits for the draft's own checks — are the same gap: the draft
   phase had no stated exit, so "done" meant "the numbered list ran out".) Do not advance to
   step 3 until ALL of these hold on the **current** draft head, meaning whatever the last fold
   produced, not the sha you recorded at draft-creation time:
   - every draft-phase fold, from **any** source — a runner failure, a `claude-review` finding,
     a manual Codex finding — is **committed**. Same reason as step 1: `--base` reviews the
     committed branch diff, so an uncommitted fold is invisible to every check below;
   - the **step-1 gates and any applicable domain review (steps 1-4) have been re-run** on that
     committed tree. The pre-push results describe the pre-fold tree;
   - **`codex review --base origin/main` has been re-run and comes back clean** on that same
     tree. The draft phase runs BEFORE step 5's own re-run, so at this point nothing later has
     covered this tree yet; without this bullet a draft-phase fold reaches the ready transition
     having been seen by no diverse lens at all, and it then surfaces as post-ready rework,
     which is the precise cost this entire step exists to avoid;
   - the committed fold is **pushed**, and the draft's **required runner checks are GREEN on the
     pushed head**, not merely started: confirm `gh pr view --json headRefOid` matches your local
     `HEAD`, then `gh pr checks --required --watch`. Both halves are load-bearing, and both were
     caught by the local Codex pass on this very fold. Checking before pushing reads green off
     the PREVIOUS remote head while the commit you just validated is absent from the PR
     entirely. Watching UNFILTERED deadlocks a workflow-editing draft permanently: `claude-review`
     **fails by design** on PRs that edit a workflow file (the App's workflow-validation guard —
     AGENTS.md Code Review Rubric note), which is exactly why it is deliberately not a required
     check, so an unfiltered watch treats an expected failure as a permanent block. `ci.yml`
     triggers on `pull_request`, so those jobs are still running while you read this; marking
     ready underneath them fires the automatic Codex review against a head that an
     environment-only failure is about to invalidate, forcing a post-ready fix, push, and
     re-trigger — the draft phase's whole purpose, spent;
   - the latest CodeQL `Analyze (actions)`, `Analyze (javascript-typescript)`, and
     `Analyze (python)` jobs are **successful for that same pushed head**, and the separate
     `CodeQL` check posted by the `github-advanced-security` app is green with a "No new alerts
     in code changed by this pull request" result. The three job successes prove that SARIF
     uploads completed; they do not prove the diff is clean. CodeQL is deliberately not
     branch-required, so `gh pr checks --required --watch` does not wait for these results:
     inspect every CodeQL entry in `gh pr checks`, verify the workflow run's `headSha` equals
     the current `headRefOid`, and inspect/triage any new-alert result before accepting the
     gate. A stale green analysis from an earlier head does not satisfy this exit condition.
     The repository switched from default to advanced setup on 29 Jul 2026; if initialization
     or upload later fails because the setups conflict, restore that prerequisite in Settings
     -> Code security rather than treating the failure as a code defect;
   - every draft `claude-review` inline thread is folded or explicitly resolved.

   A fold made to satisfy any bullet restarts this list from the top.
3. **Mark ready only once the draft-phase exit condition above holds** (`gh pr ready`) — that
   condition is what "the head is expected to hold" actually means, and an earlier draft of
   this step said only the vague half — and **RE-RECORD the
   head sha at that moment** — the sha you noted in step 2 is from draft-creation time, and
   step 2 is precisely where runner-gate fixes land, so it is very likely stale by now. Every
   "recorded head sha" below means this re-recorded one, not step 2's. (Missing this is the
   same stale-sha class the wait rule exists to close, and it was reintroduced by omission in
   an earlier draft of this very step.) That transition is
   what fires the automatic Codex review, and in this repo it also re-runs `claude-review`.
   The verdict you wait on must match the re-recorded head **and postdate the event that
   started the automatic review**: `ready_for_review` for a draft you marked ready, or
   `opened` for a PR that was created ready (which never emits `ready_for_review` at all). a findings-review left on the draft describes the same
   sha, so a head-match alone would let a pre-ready verdict satisfy the wait while the
   automatic review it triggered is still in flight.
   **Wait for the verdict on the recorded head sha** — a **posted review** carries a
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
5. **Re-run the branch gates + domain review (steps 1–4) after any code change, commit it, and
   re-run `codex review --base origin/main` until clean before pushing** — a Codex fold can
   break a gate or reopen a review finding, so the earlier results only count if they reflect
   the final state. The local Codex re-run belongs here for the same reason it belongs in the
   draft-phase exit condition (this bullet was the third, unreported instance of that class,
   swept #682): item 6's GitHub re-trigger will see this tree either way, but it sees it as a
   post-ready round, and a local pass that is free collapses that round before it costs one.
6. If a fold moves the head **after** the automatic review, **push it and confirm
   `gh pr view --json headRefOid` matches your local `HEAD` BEFORE re-triggering** (Codex P1,
   #682) — this is the same push-and-verify clause the draft-phase exit condition carries, and
   it was missing here, which is the identical class in its third location. Re-triggering
   against an unpushed fold makes GitHub review the PREVIOUS remote commit, and since that
   review then legitimately comes back clean and names a real head, nothing downstream can tell
   it apart from a verdict on the tree you actually intend to merge. Only then re-trigger with a
   single `@codex review` on the new final commit — and then **go back to step 3 and wait out a
   valid verdict on THAT head before preflight is complete**. Re-triggering is a
   request, not a result: the verdict already "in" describes the superseded head, so treating
   the re-trigger itself as the end of this step merges while the final-head review is still in
   flight — exactly the #518 race AGENTS.md's wait rule exists to close. Every head-moving fold
   re-enters this loop. Re-trigger only on the new final commit, never on intermediate pushes.

**Iterate LOCALLY, not on the draft.** After folding a Codex finding that changed code, you
**MUST** re-run `codex review --base origin/main` and keep going until a pass is clean, not
merely "can" (Codex P2, #682): a fold routinely introduces or exposes the next
Codex-specific issue, so a single local pass only ever certifies the tree that produced the
finding, never the tree that answered it. It costs nothing
on GitHub, so there is no reason to ration it. This is where iteration now lives. The AGENTS.md **"once-on-final-commit, don't re-litigate"** rule governs the
**post-ready** phase (a marked-ready PR heading to merge), where re-triggering across
pushes is the churn we avoid. Local = iterate to clean; draft = fold the runner gates **and
then iterate locally again until the draft-phase exit condition holds**; ready = commit the
whole review roster to a head you expect to hold. Note that the local loop is not confined to
the pre-push phase: every phase that folds anything re-enters it, because a fold is a new tree
and a tree no diverse lens has read is the one thing none of these phases may hand onward.

> Note on `claude-review`: opening a draft does run `.github/workflows/claude-code-review.yml`
> (it listens to `opened`). Its STATUS CHECK is not required and passes on findings, so a green
> check there proves nothing. Its FINDINGS are a different matter: they are inline threads, and
> `main` requires every conversation resolved, so draft findings block merge exactly like
> post-ready ones. Fold or explicitly resolve each; do not treat them as optional noise.

**KPI:** local `codex review` folds now happen BEFORE the branch is pushed, so they count as
**pre-open** folds on AGENTS.md's own boundary, not as a separate pre-ready bucket. Count
them alongside the step-4 pre-open review count. Keep the draft-phase line only for what is
genuinely folded after the draft exists: runner-gate failures, and this repository's
draft-run `claude-review` findings. Both counts trending up is the win.

## Only when 1–5 pass

Step 5 is where `gh pr ready` is actually run, so this section is the POST-ready gate rather than
a second ready transition (Codex P2, #682: reading it as one made the two sections circular,
since step 5 cannot complete until the ready transition has fired the automatic review). Once
step 5's verdict **on the current final head** is in — a re-trigger from item 6 that you have
not yet waited out is not a verdict — follow the **PR Merge Policy** in AGENTS.md
(independent triage — the author never triages its own PR; every conversation resolved;
`codecov/patch` green; the post-ready once-on-final-commit Codex discipline; squash-merge;
delete the branch).

**A draft-phase verdict does NOT satisfy the post-ready wait** (corrected 27 Jul 2026, Codex P1
on #682). An earlier version of this footer said a draft review of the exact head already
satisfied once-on-final-commit, so no re-trigger was needed when the PR flipped ready. That is a
stale-verdict race: the `ready_for_review` transition starts a NEW automatic review, so merging
on the older draft verdict means merging while that review is still in flight, even though both
name the same sha. The accepted verdict must postdate the boundary event for this PR's shape
(`ready_for_review` for a draft you marked ready, `opened` for a PR created ready). A manual
draft review's findings are still real and worth folding; they simply are not the signal you
merge on.
