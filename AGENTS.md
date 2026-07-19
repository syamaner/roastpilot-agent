# AGENTS.md - roastpilot-agent

Project rules and context for coding agents working in this repository.

## Architecture Invariants

These hold for every change, in every epic. PRs that weaken one are wrong by
definition.

- **The controller owns the loop; the LLM advises.** The advisor never
  receives MCP write tools. It returns typed `RoastDecision` data only.
- **Every roaster write passes safety policy.** No code path delivers
  advisor output (or operator input) to `mcp_client` without a
  `SafetyEvaluation`. Verdicts are typed —
  `ALLOW / CLAMP / REJECT / RECOVERY / FAULT / EMERGENCY_STOP` (six values,
  D15) — and never string-compared in core logic. Shared enums are plain
  `Enum`, not `StrEnum`, so a string comparison is a pyright strict error.
- **Restart never auto-resumes heat or fan.** A restart with a
  possibly-active run enters `operator_recovery_required`; explicit operator
  action is required to resume, drop, cool, or end the run. Emergency stop
  stays available from every phase.
- **Temperatures are Celsius everywhere** — models, schema, API, UI, tests.
- **The SPA renders from server events and snapshots.** It never infers
  roast phase locally and never calls MCP tools directly.

## Rules

- Python 3.11+ with full type hints on all public functions and methods.
- Google-style docstrings for public modules, classes, functions, and methods.
- `ruff check`, `ruff format --check`, `pyright` (strict), and `pytest` must
  pass before marking implementation complete.
- All runtime and dev dependencies must be declared in `pyproject.toml`.
  Never install ad-hoc dependencies without adding them to project metadata.
- Keep roaster hardware control conservative. Heat, fan, drop, cooling, and
  emergency stop behavior require explicit tests or manual validation notes.
- All M1 tests run hardware-free: fake MCP client, or the real
  `coffee-roaster-mcp` in mock-driver mode. Do not mark hardware stories
  complete from mock tests alone.
- Do not commit model weights, audio files, roast logs, SQLite databases,
  serial captures, `.env` files, or local IDE folders. The one exception:
  small contract/validation fixtures under `tests/fixtures/` (plan §8) —
  e.g. the 7 Jun 2026 live-roast JSONL/summary excerpts the MCP mirrors
  validate against.
- One PR per story, branch: `feature/{issue-number}-{slug}`.
- The PR that completes a story updates the epic file's status table in the
  same PR — file state and GitHub state never drift.
- Before starting a task: read `docs/state/registry.md`, open the active
  epic file, then check the GitHub issue.
- The program/spec repo is `~/git/roastpilot-plan`
  (github.com/syamaner/roastpilot-plan). If implementation reveals a plan is
  wrong or ambiguous, update the plan repo in the same work session (next
  decision number, clear commit). Plans are the source of truth.
- Public text (README, docs) follows the plan repo's accuracy boundaries:
  the LLM is advisory-only and never controls hardware; no determinism
  percentages; no "fully autonomous"; no "production-ready" before
  end-to-end hardware validation.

## Quick Commands

### Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . --group dev
```

### Test

```bash
python -m pytest
```

### Lint And Format

```bash
python -m ruff check .
python -m ruff format --check .
```

### Typecheck

```bash
python -m pyright
```

(CI adds `--pythonpath` because the runner has no `./.venv` for pyproject's
`venvPath`/`venv` settings to resolve — do not "simplify" the CI step to the
bare command.)

### CLI Smoke

```bash
roastpilot-agent --help
roastpilot-agent --version
```

## Codebase Architecture

```text
src/roastpilot_agent/
  __init__.py     - package version
  cli.py          - console entrypoint
  controller.py   - transition table, tick() loop, T0 debounce (re-exports
                    RoastPhase from models.py, its home per D15)
  mcp_client.py   - typed wrapper over the 14 coffee-roaster-mcp tools; owns
                    the MCP child process (spawn, health, restart → recovery)
  advisor.py      - RoastAdvisor ABC, AdvisorContext, RoastDecision,
                    PydanticAIAdvisor (OpenRouter), FakeAdvisor
  safety.py       - SafetyVerdict, SafetyEvaluation, rule set, rate limits
  store.py        - aiosqlite store, schema v1, recovery reads
  api.py          - FastAPI: REST + SSE + static web/ mount; replay mode
  replay.py       - ReplaySource: recorded exports through the real SSE
                    pipeline at 1×–60×
  models.py       - shared Pydantic models & enums (RoastPhase, RoastProfile)
  config.py       - ControllerConfig, AdvisorConfig, SafetyLimits, AppConfig
tests/
  conftest.py     - fake MCP client, fake advisor, temp SQLite store,
                    event-sink test double
web/              - Vite + React + TS SPA (E10; built into the wheel at E11)
docs/state/
  registry.md     - active project state pointer
docs/epics/
  E01…E12         - epic spec files: goal, plan links, stories, status table
```

## Key Design Decisions

Authoritative sources: `roastpilot-plan/roastpilot-agent/plan.md` (D5–D9),
`roastpilot-plan/roastpilot-agent-orchestration-plan.md` (architecture),
`roastpilot-plan/00-repository-structure.md` (D1–D14).

- D5: advisor provider is OpenRouter via PydanticAI; model slug is config;
  tests always use a deterministic fake.
- D6: the agent spawns `coffee-roaster-mcp` as a stdio child process; agent
  restart ⇒ clean MCP restart into the recovery flow.
- D7: minimal static roast profiles in M1 — no curve targets.
- D8: M1 SPA scope is dashboard + roast detail + history.
- Controller tick is 1.0 s, set by the Hottop thermocouple response time.
- MCP phases are inputs; agent phases are the operator-facing truth
  (mapping in component plan §3).
- T0 and first crack are accepted from MCP detection; operator marking is
  recovery-only (T0) or an explicit override (FC).
- SQLite runs WAL + `synchronous=FULL`; commit per tick during active roasts.

## Epic State Management

Before starting a story:

1. Read `docs/state/registry.md`.
2. Open the active epic file listed in the registry.
3. Read the GitHub story issue and any comments.
4. Confirm acceptance criteria and current risks.
5. **Write the PR plan** (see PR-Hygiene: "PR-plan the story at KICKOFF") — the ordered
   list of thin PRs (scope / rough size / reviewers / deps), each under the 400-line logic
   cap, *before* writing code. Record it in the story brief / issue.
6. Work on a branch named `feature/{issue-number}-{slug}` for the first planned PR.

After completing a story:

1. Run required checks.
2. Update story status in the active epic file.
3. Update decision notes when behavior changed.
4. Comment on the GitHub story issue with what changed and how it was tested.
5. Open a PR referencing the story issue.
6. Watch the PR to green, then resolve every review finding (see the merge
   policy below), then squash-merge, delete the branch, and flip the story's
   project item to Done.

### PR Merge Policy

Watching a PR, addressing its feedback, and squash-merging it is
pre-authorized — no need to ask before merging your own story PR once it is
clean.

- **Green CI is necessary but not sufficient.** The automated code-review
  bot (and any human reviewer) routinely leaves substantive comments while
  CI is green — correctness edge cases, observability gaps, dead code,
  missing coverage. Read every comment before merging.
- For each finding: fix it (cheap, clearly-correct ones — just do them), or
  state in the thread why it is not being actioned (reviewer marked it "no
  action", cosmetic, or unreachable). Never merge with an un-triaged
  comment.
- **Coverage regressions must be sorted, not waved through.** If
  `codecov/patch` fails, either add the missing test or tag a genuinely
  unreachable defensive/narrowing line `# pragma: no cover` (the repo
  convention — see `store.py`, `mcp_client.py`). Do not lower thresholds to
  pass.
- Re-run the checks after any fix; only merge once CI is green *and* every
  comment is resolved or consciously dismissed.
- **`main` is branch-protected (13 Jun, enforces this policy at the platform).**
  Required: the CI checks + `codecov/patch`, `required_conversation_resolution`
  (every review thread resolved), and `enforce_admins` (no bypass for owner or
  agents); force-push/deletion off; repo auto-merge on. **`claude-review` is
  intentionally NOT a required check** — it fails by design on PRs that edit a
  workflow file (the App's workflow-validation guard) and on Dependabot PRs (no
  secrets), and it passes-on-findings; so the findings gate is its **inline
  comments** (`--comment`) + conversation-resolution, not the check itself. Don't
  re-add it as required (it would deadlock workflow PRs). Green CI alone never
  means mergeable.
- **Independent triage when work is delivered by an agent team (D23).** PR
  review feedback (the review roster below — **Claude Code Review** and any human
  reviewer — plus codecov and a `/review-branch` roster pass) is
  adjudicated by the lead/PM or the `pr-triage` subagent —
  *never* by the author teammate self-dismissing comments on its own PR. The
  author fixes; someone else decides what counts as resolved. (Each review is a
  fresh instance with no authoring context — independent of the author even when
  it's the same model; this rule keeps the *triage decision* independent too.)

### PR Hygiene (size & rework)

PR-flow metrics flag the build for **large PRs** and **high rework**. The first
levers (below) clearly cut size (median churn ~735→~388, the >800 giants gone),
but the gross rework rate stayed flat — because the rework that remains is ~half
**healthy** (real review catches we want) and ~half **preventable** (a
backend-only change reddening a frontend contract test, low-finding folds,
fixture regens). Cut the preventable half; the `pr-preflight` skill runs this
checklist before you open.

- **Separate data from logic.** Fixtures, snapshots, generated files, research
  output, bake-off results go in their OWN PR (or at least their own commit),
  never bundled with logic — they were the size outliers and don't need code
  review the way logic does.
- **PR-plan the story at KICKOFF — a planning step, not an execution-time reaction.**
  Before writing code, decompose the story into an **ordered list of thin PRs**, each
  with its scope, rough size, dependencies, and which reviewers it triggers
  (safety / security / qa). You should know "this story is 8 PRs, and PR3 does exactly
  X" *before* PR1 opens. This lives in the story brief (a lead / `product-pm` activity).
  Reactively splitting a 900-line diff at review time is the failure mode this prevents
  (#587's ~800-line module and #600's ~2,000-line harness were unplanned monoliths — the
  logic in each should have been ~3 and ~6 planned slices under the cap below; the
  shift-left folds then *masked* the size problem instead of fixing it).
- **Keep logic PRs small — the 400-line cap is a HARD STOP.** Measure **logic** lines
  only: `git diff --stat origin/main` minus data/fixtures/generated/doc files (those are
  exempt and go in their own PR per the rule above). If the logic diff exceeds **400**,
  the PR plan was too coarse — split to the planned slice boundary before opening. Enough
  slices that every one is under the cap: the ~2,000-line #600 harness was ~5–6 reviewable
  logic slices (scoring / stats / runner / report), not 4. The number is exact (400), the
  slicing is what flexes.
- **Shift review LEFT — mandatory, not optional.** Before opening: run all gates +
  an adversarial self-critique, AND run the domain reviewer on the BRANCH
  (`safety-reviewer` for safety/controller/enum/recovery, `qa` for tests) and
  resolve its findings. Do not open until that pass is done. Findings folded
  pre-open are not rework; the same findings raised by the bots post-open are.
- **A "backend-only" change is a lie when a contract test spans the boundary.**
  Adding or changing a server event kind / SSE field reddens the FE event-kind
  contract test. Always run the **cross-boundary contract tests** (and regen any
  contract fixtures) PRE-open, regardless of which side you touched — the
  collision graph includes the contract test, not just the files you edited.
- **Don't fold LOW findings as post-open commits.** Per the Code Review Rubric
  lows are non-blocking (summary, not inline); fixing them in post-open commits is
  self-inflicted rework. Fix lows pre-open, or defer/dismiss them in-thread.
- **Kill avoidable churn.** Gates before opening (no post-open lint/format
  commits); flakes are P1 (fix, don't re-run); never add a junk "re-trigger CI"
  commit — re-push cleanly if a push didn't fire CI. Two `pr-preflight` levers close
  the specific misses the Tier-1 retro found: run `pytest --cov-report=term-missing`
  pre-open and cover every changed line (no post-open `codecov/patch` gap, #452), and
  check the diff against `docs/recent-fixes.md` so a batch doesn't reintroduce a
  sibling PR's just-fixed bug (#453, the #409-reintroduced-#404 class).
- **What "good" looks like (the KPI).** Judge hygiene by **avoidable churn → ~0**
  (rebase / CI-retrigger / flake / lint), **preventable post-open rework → ~0**
  (contract drift, low folds, fixture regens), and **findings caught pre-open**
  trending UP — NOT by the gross review-fix rate, which stays non-zero because
  **healthy rework stays**: a reviewer catching a real defect is the system
  working, so never game the metric by skipping review.
- **Lead time: expect time-to-OPEN to rise and time-to-MERGE to fall.** Shift-left
  front-loads the gates + review before the PR opens, so the open→merge window
  shrinks while branch→open grows. That is the intended trade — always run the full
  local gates + the pre-open review before opening; never skip the pre-open review to
  shave a lead-time number.
- **Thin slices off `main`; don't homegrow PR-stacking.** Split a story into
  independent slices off `main`, serialised by one owner (resume-on-merge). The
  measured rebase cost of that is ~nil (1 commit across 30 PRs), so true PR-stacking
  is not worth a fragile hand-rolled restacker under squash-merge + branch protection;
  if real stacks are ever wanted, evaluate a dedicated tool (Graphite / spr) as its
  own decision rather than scripting it.
- **Log prevented work.** The strongest shift-left win is a bad PR that never opens
  (e.g. #346, closed pre-PR on a wrong premise → D56), and PR-flow metrics are blind
  to it. When you close an issue WITHOUT a PR on a wrong-premise / superseded /
  infeasible decision, add the `prevented-pre-pr` label + a one-line decision note,
  so the prevented work stays countable.
- **Link the issue correctly.** Use `Closes #N` only for the issue a PR FULLY
  resolves (it auto-closes on merge — keeps the board honest); use `Refs #N` /
  `Part of #N` for partial or related work so an unfinished issue isn't auto-closed.
  The PR template prompts this.

## Code Review Rubric

**The PR review roster (operator, 30 Jun 2026): `Claude Code Review` + `Codex`
(advisory-but-triaged) + any human reviewer.** The automated **Claude Code Review**
(`.github/workflows/claude-code-review.yml`, running `/code-review --comment`), the
**Codex** connector, and any human reviewer follow this rubric. **CodeRabbit stays
disabled (15 Jun) and the Augment Code trial ENDED (28 Jun).**

**Codex (`chatgpt-codex-connector[bot]`) was RE-ENABLED 30 Jun** after its 15-Jun
disable, because on real PRs it catches bugs the other lenses miss (on the Config UI
build it found a roast-breaking config regression that the Opus `safety-reviewer`
PASSed twice, plus a credential-redirection hole and a cross-request `os.environ`
mutation). But it is **ADVISORY-BUT-TRIAGED, NOT a required gate, and never an
auto-blocker**: it re-reviews the whole diff from scratch and re-posts already-fixed
findings as new inline threads, which would deadlock `required_conversation_resolution`
if its threads gated merge. **So the planned `review-gate` "flip-on-BOTH-reviews" wiring
is CANCELLED — do not make Codex a required check.** Operating rule (the lead, per D23 —
the author never self-triages): Codex auto-reviews at PR creation; **re-trigger it with an
`@codex review` comment ONCE on the final commit**, never on intermediate pushes (the
once-only discipline targets re-litigation across pushes; the silent-fallback below may add
at most one extra trigger on that SAME final commit); the lead
verifies each finding against the *current* code, folds the real ones, and **resolves the
stale re-posts by hand** (GraphQL `resolveReviewThread`). Its signal type is a
`pull_request_review` with inline threads, which DO block via conversation-resolution, so
the lead must clear them consciously. A lean roster is not "fewer eyes": a diverse lens
catches what a strong single lens misses; the constraint Codex must satisfy is *not
re-litigating resolved threads on every push*, which the once-on-final-commit discipline
enforces. (Memory: `claude-review-not-a-required-check`.)

**Draft phase vs ready phase (the shift-left reconciliation, D103-adjacent).** The
once-on-final-commit rule above governs the **post-ready** phase — a marked-ready PR heading
to merge, where re-triggering across pushes is re-litigation churn. It does **not** forbid
iterating with Codex on a **draft** PR *before* it is marked ready: the `pr-preflight` skill's
step 5 runs `@codex review` on the draft and folds by class until clean, which is exactly
where the #587-style rounds belong (pre-ready folds, not post-open rework). Draft = iterate to
clean (re-trigger on settled batches, not every push); ready = clean already, then
once-on-final-commit applies. Opening a draft does run `claude-review` (`on: opened`) — fine,
it is not a required check.

**WAIT for Codex's verdict before merging (operator rule, 12 Jul — the #518 lesson):**
Codex is often DELAYED relative to CI, and green-CI auto-merge can land a PR before its
review posts (#518 merged with 3 real P2s in flight → fix-forward #519). Its lifecycle
signals on the PR are readable (every signal below means a **bot-authored** one — `user.login ==
chatgpt-codex-connector[bot]` on the reaction/comment/review; on a public repo any stranger can
emit a look-alike of ANY of them, including the 👀): **👀 reaction = review started; a posted
`pull_request_review` with inline threads = findings; and a CLEAN verdict is EITHER a 👍 reaction
(after the 👀) OR
a top-level "Codex Review: Didn't find any major issues" comment carrying a
`**Reviewed commit:** <sha>` line** (the clean-comment is the MORE COMMON channel — observed on
#57/#60/#65; a watcher polling only reviews + reactions is blind to it). **Both clean signals are
authoritative ONLY when authored by the Codex bot identity (`chatgpt-codex-connector[bot]`):** the
repo is public, so anyone can add a 👍 OR post a look-alike comment copying the title + the visible
head sha — verify the reaction's / comment's `user.login` is the bot (the reactions API exposes it),
because content or a bare reaction alone is spoofable; and **any verdict carrying a
`Reviewed commit:` sha — clean comment AND findings-review alike — counts only when that sha
EQUALS the current PR head** (a bot-authored verdict from an earlier in-flight review can post
*after* the final-commit trigger yet name a stale sha — identity + recency alone would accept a
verdict about old code; a stale findings-review is still worth triaging, it just does not satisfy
the wait for the current head). The same bot-identity requirement applies to the **findings**
channel: a `pull_request_review` from any other `user.login` is just a comment from a stranger —
triage it as such, but it does NOT satisfy the Codex wait. So: do NOT arm auto-merge at open.
After the final commit + `@codex review`, wait for a bot-authored findings-review naming the head
sha, a bot-authored clean comment naming the head sha, or the bot's own 👍 (a 👍 carries NO
commit line, so it is valid only while the head sha is UNCHANGED since the trigger it answers —
any push since the trigger invalidates it; re-trigger on the new head) — **and the
signal must postdate the final-commit trigger**: a review posted at PR creation against an
earlier commit does not satisfy the wait (that stale-verdict reading would reopen the #518
failure mode). Then triage (if findings), resolve, and only then merge/arm auto-merge.
**A bot-authored 👀 reaction without a verdict is an IN-PROGRESS review, not silence — keep waiting**
(extend in ~10-min increments), **bounded at ~30 min from the 👀**: past that, treat the
in-progress signal as stuck and the lead may merge with an "in-progress review stalled"
note, triaging any late review post-merge. The silent fallback applies only when NO signal
at all (no *valid bot-authored* post-trigger 👀, review, clean comment, or 👍 — an invalid signal
per the rules above, e.g. a stranger's reaction or a stale-sha comment, does NOT count as
Codex activity and must not suppress this fallback) has appeared ~15 min after green CI: re-trigger
once more on the same commit and **wait a full second window (~10 min)**; only if still
nothing is merging allowed — note "Codex silent" in the merge context so a late review is
triaged as post-merge follow-up, not a surprise.

**Inline PR comments are MERGE-BLOCKING** — `main` requires every conversation
resolved (branch protection). So calibrate where findings go:

- **Inline (blocking): only genuine must-fix / should-fix findings,** each tagged
  severity — **blocker** (cannot merge), **medium** (fix or justify in-thread),
  **low** (optional but worth it). Resolving the thread is the conscious triage.
- **Summary comment (non-blocking): nits, praise, questions, observations.** Never
  clog the merge gate with trivia — a nitpick posted inline blocks the merge.
- **Don't duplicate CI.** `ruff` / `pyright` (strict) / `pytest` / `codecov/patch`
  already gate; review for what they can't see, not what they catch.
- **Be concrete:** `file:line` + the specific fix.

**Must-block (the Architecture Invariants above — flag any of these as `blocker`):**

- a roaster write that does not pass through safety policy;
- the advisor handed MCP write tools, or the controller no longer owning the loop;
- a restart path that auto-resumes heat/fan (must enter `operator_recovery_required`);
- any Fahrenheit value or conversion (temperatures are Celsius everywhere);
- a string-compared `SafetyVerdict`/`RoastPhase`, or a `StrEnum` where a plain
  `Enum` is required (string comparison must stay a pyright error);
- the SPA calling MCP directly or inferring roast phase locally.

**Escalate:** any diff touching `safety.py`, `controller.py`, or `models.py` enums —
call it out in the summary so it is routed to `safety-reviewer`.

**Escalate (capability-based, not file-based):** any diff that **fetches or parses
untrusted external input, adds an external-input endpoint, or adds a new LLM-provider
call path** → route to `security-reviewer` with `docs/review/untrusted-input-checklist.md`,
**pre-open**. This fires even when the diff touches none of the safety files — a new
fetch/parse surface is the highest-risk case and the easiest to miss. (The #587 lesson:
the bean-sourcing fetch endpoint took **five** post-open Codex rounds — SSRF, fail-soft,
resource-exhaustion, secret-hygiene, cross-feature-contention — because no pre-open lens
covered web/application security; the file-based routing above never fired since it
touched no safety file. `security-reviewer` + the checklist close that gap.) If the change
adds a provider-calling path that could contend with the roast advisor (checklist class 6),
ALSO run `safety-reviewer`. This capability routing is not limited to `pr-preflight`: the
`review-branch` roster workflow (`.claude/workflows/review-branch.mjs`) should likewise include
`security-reviewer` in its lens set when the branch diff matches the external-input test, so the
roster pass and the pre-open pass agree.

**Also verify:** tests assert real behavior (not smoke); new code is covered or
carries `# pragma: no cover` *with a reason* (repo convention — see `store.py`,
`mcp_client.py`); public functions/methods have type hints + Google docstrings.

> Note: `claude-review` is intentionally **not** a required status check — it fails
> by design on PRs that edit a workflow file (the App's workflow-validation guard),
> and it passes even when it finds bugs. The real findings-gate is these inline
> threads + `required_conversation_resolution`; the required checks are CI + codecov.

## Claude Code

- Sub-agents live under `.claude/agents/`. **Domain reviewers:**
  `safety-reviewer` (PRs touching safety/controller/enums), `security-reviewer`
  (web/application security — server-side fetch, untrusted-input parsing, new
  external-input endpoints or provider-call paths; works
  `docs/review/untrusted-input-checklist.md`), `mcp-contract-checker`
  (dependency bumps), `sim-roast-runner` (mock vertical slice + decision-trace
  summaries), `ui-reviewer` (Playwright against the replay harness). **Team
  roles** (define each once; reuse as an agent-team teammate, standalone, or a
  workflow stage): `product-pm` (product reviewer — audit vs plan, record
  decisions, write the next brief; never edits src/tests), `qa` (test quality
  beyond coverage), `pr-triage` (independent PR-feedback triage — also the
  `triage-pr` skill), `engineer-fe` (web/ SPA), `engineer-be` (Python agent). The
  human is the lead + domain expert/architect, consulted on escalations.
- **Model selection — decide it WITH the topology, every time.** When you pick a
  primitive (sub-agent / agent team / workflow), pick the model in the same breath.
  **Default to Sonnet** for the bulk of the work: scoped implementation (`engineer-be`,
  `engineer-fe`), mechanical checks (`mcp-contract-checker`, `sim-roast-runner`), and
  routine review/audit (`pr-triage`, `qa`, `ui-reviewer`, `product-pm`,
  `security-reviewer`) — all pinned `model: sonnet`. **Reserve Opus** for genuinely
  hard reasoning: `safety-reviewer` is
  pinned `model: opus` (the one always-Opus role — a missed safety bug is the costly
  failure), and you may bump a specific spawn to Opus for gnarly architecture/design
  judgment or subtle correctness triage. Why the pins matter: an agent with no `model:`
  inherits the PARENT, so a careless spawn from the Opus main loop silently runs Opus —
  the per-role defaults stop that. The Opus main loop conserves credits by delegating
  execution to Sonnet.
- **Skills** (`.claude/skills/`): `triage-pr` (→ `pr-triage`), `capture` (drive
  the replay harness + SPA, screenshot a named page state — E10+).
- **Workflows** (`.claude/workflows/`): `review-branch` (cross-checked roster
  review of the branch diff).
- **MCP** (`.mcp.json`): the Microsoft **Playwright MCP** (`@playwright/mcp`) —
  agent-driven browser/screenshots for `ui-reviewer`'s direction-match review
  (D24). Interactive sessions only; the deterministic CI gate is the scripted
  `@playwright/test` snapshot suite, not the MCP.
- **Agent-team worktree isolation:** for a parallel fan-out, the lead creates one
  explicit `git worktree` per teammate and each teammate self-locates every
  command (cwd resets between bash calls) — the Agent tool's `isolation: worktree`
  flag silently no-op'd for background team agents in this environment. Runbook:
  `docs/agent-team-worktrees.md`. Verify `git worktree list`; serialize as the
  fallback for any shared file surface.
- `CLAUDE.md` contains exactly `@AGENTS.md` — rules belong here, never there.

## Hardware Safety Notes

- Hottop command behavior (drop, cooling, emergency stop, temperature units)
  requires explicit validation before any hardware-ready claim; E12 owns the
  supervised validation stories.
- Unsafe or uncertain hardware behavior fails closed: heat off, record a
  fault event, preserve enough state for diagnosis.
- Whether `drop_beans` engages cooling on the real Hottop is an open
  verification story (component plan §3); the controller handles both
  outcomes.
