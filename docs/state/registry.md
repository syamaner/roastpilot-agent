# RoastPilot Agent Project State Registry

## Active Epic

> **STATUS UPDATE — 27 Jul 2026 (supersedes every framing below).** Read this block
> first; everything under it is retained history, and several of its forward-looking
> statements (June hardware target, "harness complete target: July 2026", #327 as the
> open P1) are now spent. Nothing below this line should be treated as a live plan
> without checking the GitHub issue.
>
> **Control loop.** The D35 split shipped and is hardware-validated across roasts
> 3–15. **#327 (deterministic anticipatory trim) and #228 (pre-FC advisory layer) are
> both CLOSED** — the "trim is the P1 control priority" line below is spent. The live
> control thread is **#559** (D96 recovery law): merged **dormant** and awaiting the
> operator's flag-on hardware validation roast. It is the only control work that is
> ready-but-unproven; the watch-list is on the issue, and an agent analyses the trace
> after the operator runs it. **#567** (reference curves) shipped disabled and is
> **PARKED** after a negative offline bake-off (#578) — if revisited, fix the data
> representation, not the c9 prose. **#396** (prompt/model A/B) and **#653** (visual
> tolerance) are operator decisions: propose, never decide.
>
> **Bean sourcing (the July build).** Draft-a-BeanProfile-from-a-vendor-URL shipped as
> a second, human-gated LLM surface outside the safety envelope: backend #587, the
> #590 containment gates (the D1 free-text gate is LIVE — `_draft_from_identity`
> applies whole-phrase containment to `name`/`country`/`bean_origin`/`farm`/
> `bean_varietal`, demoting anything not code-verified on the page; only the four
> typed-field lexical certification gates are parked, and per #617 no lexical
> hardening will be attempted on them again), the #601 reasoning-arm bake-off and
> spend-integrity stack (results in `docs/advisor/bean-sourcing-bakeoff-2026-07-22.md`),
> and the #637 draft-from-URL UI. Residual hardening #596/#597 closed 26–27 Jul.
> **#588 / D119 closes the runtime-monitoring gap:** schema v14 records every
> admitted extraction attempt (including failures/preemption), qualified usage,
> latency and provenance counts, then atomically correlates at most one explicit
> save to a bounded, server-held baseline without retaining URLs/evidence/error
> text. The scoring/small-N bake-off harness already shipped as
> `scripts/bakeoff_bean_sourcing.py`. **#573 / D121 catalogue recommendations is
> SHIPPED:** backend slice #696 provides bounded discovery, one-call extraction,
> deterministic local ranking, roast-start pre-emption, and aggregate schema-v15
> telemetry; the story-closing UI adds a collection-page form, explainable result
> cards, and an exact server-owned product-URL handoff into the existing editable
> draft flow. Recommendations never order or save a bean — the operator must select,
> review, edit, and explicitly save the profile.
> **#591 socket-level SSRF pinning is SHIPPED:** HTTPX now retains hostname URLs and
> natural cookie/pooling/Host/TLS semantics while the httpcore TCP backend resolves,
> validates, and dials only public IP literals at connect time; the URL-rewrite,
> explicit Host/SNI, and disabled-keepalive workarounds are retired. Remaining open
> follow-up: **#595** (rate-limit/auth on the billable endpoint).
>
> **Review + process.** PR size is a **reviewability guide, not a hard cap** (#661,
> 26 Jul) — a cohesive single-slice story stays one PR. `security-reviewer` is now
> wired into the `review-branch` roster (#598/#678); **#680** tracks two fail-opens
> found in that workflow post-merge. **#663 / D108-D118 is ACTIVE (31 Jul):** the
> PR-scoped Claude approval bridge and bounded solo-maintainer privileged override are
> on `main`; live adversarial proofs passed; `main` now requires one stale-dismissing
> approval. The temporary 25-Jul Claude outage exception is closed. The retired
> SHA-scoped `review-gate` must not be restored.
>
> **Milestone reality.** M1 build continues; the "July 2026 harness complete" and
> "first supervised hardware session in June" targets below are historical. E11
> (packaging) is operator-gated and IN PROGRESS — E11-S1 is split, with SPA bundling
> shipped and the `[pi]` extra still blocked on D27 Phase 2, while E11-S2 and E11-S3
> are not started. E12 (validation/demo) is operator-gated and not started. (Codex P2,
> #681: this line previously called E11 "not started" and then described its shipped
> half in the same sentence, which would have a cold-start session plan E11 from
> scratch.)
> Next free plan decision number: **D122** (D121 records catalogue recommendations).

> **STATUS UPDATE — 21 Jun 2026 (superseded by the 27 Jul block above):** the D35 control work
> is BUILT and **hardware-validated by roast 3** (first clean end-to-end roast). Pre-FC
> deterministic floor (#222) + post-FC LLM loop (#223/#226/#276) + model pin (gpt-4o + c1, D43)
> all SHIPPED. The floor shipped FLAT (heat 100 → FC); the §3 anticipatory **trim** was never
> built and the floor overshoots (D46) → **#327 deterministic anticipatory trim is the P1
> control priority, ahead of #228.** Full roast-3 detail + decisions D46–D50 in the Active
> Context section below. The historical PREP-phase note (14 Jun) is kept for the record:
>
> **ACTIVE DIRECTION — D35 (14 Jun 2026): the deterministic-control-loop advisor
> redesign is the priority, in a PREP phase (NO build until engineers are brought in;
> operator decision).** The supervised hardware roasts (#134 attempts 2–4) surfaced that
> the free-form LLM advisor mishandles in-roast control — it baked a batch (#218: fan
> overkill, heat 70→40→20→0, self-contradiction). D35 splits the control path at first
> crack: **deterministic pre-FC** (controller owns the levers; no LLM), **LLM advises
> heat+fan+drop post-FC** inside a safety box. Grounded in the operator's working n8n
> system, where the LLM merely *follows* a deterministic decision tree (tool-calling),
> rather than freely controlling the levers — the controller still owns the loop. Plan:
> `roastpilot-plan/roastpilot-agent/deterministic-control-loop-plan.md` + `plan.md` §1 D35.
> Tracking: **epic #221** (phases #222 pre-FC determinism / #223 post-FC LLM+box / #224
> test-harness+corpus+eval). **Superseded:** #214, #172 (prompt work, closed). **Subsumes:**
> #209 (settle window → no-op). **Promoted load-bearing:** #205, #219 (**#219 charge-references
> the advisor DTR clock, server-side only; the SPA chart/readout origin stays run-referenced —
> charge-origin chart deferred to #220**). **§7 forward goals:**
> parameterised plan interface (a future learning loop → anticipatory pre-FC) + outcome-
> labelled roast logs as a fine-tune training corpus (learning brain → roastpilot-cloud, D29).
> Forward-looking #209/#211 (charge cluster) are MERGED. **Observability slice DONE on `main`**
> (#217/#219/#220 + #205, plus the #235/#239 DTR-correctness fixes — shipped ahead of the first
> roast, NOT deferred; see the 15–16 Jun session note below and plan D35a). #210/#212 (operability)
> remain deferred to after the next roast. **The D35 keystone #222/#223/#224 (epic #221) is still
> OPEN — the critical path to the next #134 roast.** **E11 (packaging) remains gated** behind D35 +
> the D28/D27 gates below.
>
> **D36 (14 Jun, operator) — refines D35 §7.1.** (a) The post-FC loop's context (#223) gains a
> **windowed telemetry series** (5 s samples; recent full-res window + milestone summary —
> turning point / recovery / drying-end / FC) + **derived features** (predicted-FC ETA off the
> *profile* FC band not a hardcoded 180 — extrapolated off **raw/low-lag** RoR; RoR
> curvature/crash-flick; control-signal entropy as the anti-thrash signal) — **folded into
> #223, part of the first roast.** **TP+recovery and distance-from-reference are computed but
> remain validate-first CANDIDATES** (the 15 Jun research found no external evidence they
> predict the roast) — display them, but do **not** feed them to the post-FC advisor as trusted
> context on the first roast until #229 validates them on our `.alog` set. (b) **Deferred after the
> first good roast:** **#228** an anticipatory pre-FC LLM **advisory layer** over the
> deterministic floor (late-Maillard→FC; advisory-only, same safety box + deadband +
> execute-or-not, fails closed to the deterministic trim; must clear the baked-roast negative
> cases on #224 first) + **#229** a curve-insight feature spike. The deterministic pre-FC trim
> (#222) stays the always-on floor, unchanged for the first roast. Cadence stays ~10 s pre-FC /
> ~5 s post-FC (decoupled from the 5 s sample resolution). Plan: D36 in `roastpilot-plan/roastpilot-agent/plan.md` §1; research note `docs/research/2026-06-14-roast-curve-features.md`.
>
> **D40/D41 (16 Jun) — design-review round 2 + the model-roster screen.** D40 settled five
> operator answers (validate on our data not shadow-mode; **#223 split into #273–#277**;
> "reference curve" = the roast-so-far telemetry; full-roster bake-off; deadband = a short
> dwell + history-in-every-prompt). The bake-off then RAN (8 models, 28 roasts, prompt v4):
> **a SCREEN, not a pin** — the **FC latency gate is decisive** (only gpt-4o / gpt-4o-mini /
> gemini-3.1-flash-lite clear ~5 s; sonnet-4.6 / gpt-5.5 / opus-4.8 / even gpt-5-mini@low all
> bust), recall 1.0 for all but gpt-5-mini (0.96), and the **pre-FC heat-cut/fan-into-crack pattern showed
> systematically** (worst heat-dir: gpt-5-mini 0.26, frontier ~0.40; survivors 0.82–0.84) —
> a **v4 prompt-gap, not a model fault**, captured as `docs/advisor/negative-cases/`. **No
> model pinned** (incumbent `gemini-3.1-flash-lite` D33 stands; gpt-4o confirmed latency-viable);
> the real pin comes from a **capture-enabled re-run with the #274 teaching prompt**, paired
> before→after (**D41**). Summary: `docs/advisor/bakeoff-summary-2026-06-16.md`. **Harness
> hardening from the run, all on `main`:** #280/#283 (observability + checkpoint/resume + cost
> guard), #284/#285 (persist prompt+response+**reasoning** + most-interesting-cells surfacing) —
> *the scores find the symptom, only the reasoning found the cause*; #281 (bounded-concurrency
> replay) queued on top. Plan: D40/D41 in `plan.md` §1.

- Epic file: `docs/epics/E11-packaging.md` — **D28 gate ✅ CLEARED (28 Jun 2026); now
  gated only on D27 (torch-free chain).** E10 closed 11 Jun 2026. E11 is next in order;
  S1/S2 contract-buildable scaffolding is startable on operator opt-in — do **not** begin
  S3 or pin/ship the `[pi]` extra until the torch-free `coffee-roaster-mcp` (**D27**:
  `coffee-first-crack-detection#54` → `coffee-roaster-mcp#157`, cross-repo) lands. See
  **D28**.
- Project: RoastPilot (GitHub user project, owner `syamaner`)
- Repository: `syamaner/roastpilot-agent`
- Package: `roastpilot-agent`
- Import package: `roastpilot_agent`
- Console entrypoint: `roastpilot-agent`
- Current phase: M1 build. **The July-2026 "harness complete" target below WAS met as
  written** (Codex P2, #681, correcting this line): the definition is exactly three
  conditions — E9 vertical slice green in CI, E10 dashboard usable for a live roast,
  and one supervised real-hardware roast end-to-end — and all three hold (roasts 3–15).
  The definition itself says E11/E12 polish may run into August, so their being
  operator-gated is the milestone working as specified, not a miss. See the 27 Jul
  status block at the top for the live position.
- **July milestone (D17)** — "harness complete" = (1) E9 vertical slice
  green in CI + (2) E10 dashboard usable for a live roast + (3) one
  supervised real-hardware roast end-to-end. E11/E12 polish may run into
  August; demo assets recorded by end of August. Every session optimizes
  for this finish line. *(Historical: the first supervised hardware session was
  targeted for June and happened — roasts 3 onward; roast 15 is the most recent,
  tracked in #559. The end-of-August demo-asset date is still live.)*

## Working Rules

- Before starting implementation, read this registry, then the active epic
  file, then the GitHub issue for the story.
- One PR per SLICE (a story is PR-planned at kickoff into coherent review
  units, normally targeting about 400 changed logic lines — see AGENTS.md
  PR-Hygiene); branch
  `feature/{issue-number}-{slug}-{slice}`; the PR that completes a story updates
  the epic file's status table in the same PR.
- Plans live in `~/git/roastpilot-plan` and are the source of truth; record
  resolved open items in component plan §11.
- Closing an epic = create the next epic's story issues from its spec
  file, update this registry, and flip the epic's project item to Done;
  an epic's project item goes In Progress when its first story does.
- Epic order: E1 ✅ → E2 ✅ → E3 ✅ → E4 ✅ → E5 ✅ → E6 ✅ → **E8** (advisor), then E7 →
  E9 (vertical slice) → E10 (SPA) → E11 (packaging) → E12 (validation/demo).

## Active Context

**31 Jul 2026 — CLAUDE PR-SCOPED APPROVAL RESTORED (#663 / D108-D118).** The
25-Jul operator-authorized outage exception is closed. The trusted bridge merged in
#679 and the bounded privileged-PR override merged in #688. The pre-activation
adversarial proofs passed before protection changed; the ordinary-PR enforcement
proof and final protection verification passed immediately after activation.
Shift-left security, QA, and independent triage rejected draft PR #662's attempted
suspension protocol:
GitHub commit statuses are SHA-scoped while review findings and conversation
resolution are PR-scoped; two PRs may share a SHA, a draft success can remain
visible during `ready_for_review`, and `concurrency.queue: max` serializes without
guaranteeing dispatch order. #662 was closed unmerged rather than shipping a known
fail-open gate. Live `main` protection was then verified after removing only
`review-gate`: required status checks are `Checks`, `Web (lint + typecheck + unit)`,
`Web (Playwright snapshots)`, and `codecov/patch`, all app-pinned; strict mode,
`required_conversation_resolution`, and `enforce_admins` remain enabled. Codecov
ingestion recovered on 26 Jul and #646 restored its patch gate. #676 and #678
then live-proved that authenticated zero-Python diffs receive a normal
`codecov/patch` check-run. Dependabot #675 also received `codecov/patch` SUCCESS
at 11:54:20 after its tokenless public-repository upload completed at 11:53:33;
#677's apparent permanent deadlock was a timing misdiagnosis and is not an
activation prerequisite. Merge policy still requires full
local gates, applicable local domain reviewers, an exact-head authenticated
Codex trigger/wait/triage pass (including the documented bounded silent/stalled
fallback), and independent lead/`pr-triage` adjudication.
**Restoration design (D108 amended by D109-D118):** retire the SHA status and use a trusted
default-branch bridge to turn a successful exact-PR/head/base Claude run into
an exact-commit PR approval. That evidence is monotonic for unchanged identity:
later same-head runs are additive and cannot revoke it; an intentional replacement
requires recording the reason, preventing auto-merge, dismissing first, and only
then rerunning. That dismissal creates a durable exact-identity evidence epoch:
approval bodies carry versioned workflow/run-number/attempt order, and only a
strictly newer run may approve. Approval/exemption bodies embed the full PR/head/base-branch
identity and retain the reviewed base-tip SHA as audit metadata. A push or base
retarget invalidates stale evidence. Retarget reconciliation snapshots bridge
reviews before reloading the live PR, removes stale identities only from that
snapshot, and preserves the live identity. This handles delayed A-to-B-to-C and
A-to-B-to-A delivery without touching evidence created after the snapshot;
draft approval survives ready/reopen when the bytes do not change, while both
lifecycle events start a fresh additive review so failed/missing history recovers.
Every normal-PR metadata edit likewise starts an additive review because GitHub
orders its workflow run before job conditions; only a base retarget dismisses
identity-bound evidence. Dependabot exclusion follows PR authorship.
An exact incoming run newer than stale inventory is authoritative; temporarily
empty run association receives bounded exact-run refresh plus one retry. Approval
publication creates a non-counting pending review, revalidates identity and the
dismissal epoch, submits the known review as `APPROVE`, then revalidates both
again. Lost create responses cannot publish active evidence; exact full-body/run
pending evidence is adopted and resumed, while different-run pending work is
never deleted by a concurrent handler. Pending reviews are snapshotted before
the live PR is reloaded; only superseded-head/base evidence in that snapshot is
deleted and the stale handler aborts, so later-created current work is safe.
Known owned pending evidence is deleted on pre-submit identity or epoch abort.
Lost submit responses are state-checked:
exact approved evidence proceeds to validation and exact pending evidence is
retained for explicit retry. Any raced approval is dismissed; cleanup or
validation failure blocks activation pending operator audit.
GitHub review `body` is nullable; collection scanners normalize only `null` to
empty text before marker filtering. A missing member or any other non-string
value remains malformed and fails closed.
Bridge handlers run without workflow concurrency because GitHub can replace a
pending same-group event; their identity checks and tombstones are deliberately
order-safe. JSON mutations declare their media type. Dependabot author routing
is enforced in both event channels, removes false normal evidence, and leaves
privileged changes for explicit maintainer approval.
Because `syamaner` is the only maintainer and GitHub forbids author self-approval,
D118 supplies one capability-scoped operator path for privileged PRs: a typed
`repository_dispatch` that always executes trusted default-branch code, rechecks
the sender has `maintain`/`admin`, requires an affirmative workflow/helper file
match (ordinary and 3,000-file-indeterminate inventories are rejected), and records
the exact identity, actor, and reason in a labelled bot approval. It is not available
to ordinary PRs and never checks out PR code.
Because REST cannot make that sequence atomic, slice 2 had to live-prove that an
approval bound to a non-current commit never satisfies merge. Dependabot uses a
trusted labelled
exemption, while workflow or privileged-helper edits require an explicit recorded
maintainer approval because their upstream review result is untrusted. Slice 1 shipped
the mechanism and operating policy without changing protection. Slice 2 live-proved
same-SHA/different-PR, draft→ready, stale-push, rerun, privileged-path, and ordinary-path
behavior, then enabled Actions PR approvals plus one required stale-dismissing approval.
PR #689 proved a labelled operator override on exact head `4469208`, dismissal after
the head advanced to `f744af6`, and a fresh exact-head approval; documentation-only
PR #686 was rejected by the privileged path. After enforcement, ordinary PR #690 was
`REVIEW_REQUIRED` with no reviews, then received a normal bridge approval bound to exact
head `aaa8d95` only after Claude run 30635910889 completed successfully. All disposable
proof PRs #686/#687/#689/#690 were closed unmerged. Live protection was re-read after
activation: strict app-pinned `Checks`, both Web checks, and `codecov/patch`; one approval
with stale dismissal; conversation resolution and admin enforcement on; protected-base
force-push/deletion off. Claude's draft approval survives a no-code ready
transition, but Codex independently re-reviews that transition: mark ready before
the final Codex round and accept only a fully-paginated, bot-authenticated
exact-head verdict posted after ready. Board: #663 closes with the activation-record
PR. The broader month-stale registry refresh remains a separate doc-only slice; this
entry records only state that changes #663.
Strict required-status mode, stale-review dismissal, admin enforcement, and
disabled protected-base force-push/deletion are load-bearing for D110: a normal
base-tip advance cannot merge a behind head, and the required head update
dismisses approval and starts fresh review.

**25 Jul 2026 — #665 MCP/pytest shutdown hardening completed (D104).** Three CI runs reached
the complete pytest summary and then failed to exit, exposing lifecycle ownership bugs rather than
test failures. The fix keeps a cancellation-resistant MCP owner referenced until it finishes,
refuses to spawn a replacement owner meanwhile, requires ``stop`` to finalize a completed retained
owner without reusing its now-stale PID kill hook, and explicitly joins the test-bounded
bean-sourcing parse executor at pytest session end. Linux thread tracing also exposed three replay
CLI stores whose cleanup had relied only on Uvicorn entering lifespan; the CLI now retains
idempotent source-cleanup ownership across early server return and failure. A fresh-process
regression covers real mock-driver MCP start/stop plus parse-pool use and verifies both the probe
and captured MCP child exit. Final evidence: an unprivileged Linux full suite returned with only
``MainThread`` live, and GitHub CI completed its contract-drift, coverage-upload, and CLI-smoke
steps after pytest. This is cross-cutting process/CI hardening, not a new E11 story row; issue #665
and plan decisions D104/D105 carry its acceptance and ownership contract. Residual boundaries are
explicit: P1 #667 owns a live-mode event-loop policy for a pathological MCP owner that suppresses
cancellation indefinitely. P1 #668 adds D120's audited, incident-bound in-process operator
acknowledgement after unconfirmed teardown, including the `/start` UI's explicit physical-verification
checkbox, required reason, incident-bound submission, and double-submit/stale-incident handling. The
incident and acknowledgement gate are process-local: physical verification plus
a controlled full agent restart remains the legacy recovery boundary and is not represented by an
acknowledgement audit row. Restart recovery still never auto-resumes heat or fan.

**18 Jul 2026 (later — D102 plant-model experiment + the discard-roast feature).** After #567
parked, the operator's diagnosis that the system lacks RoR PROJECTION (only the pre-FC FC-ETA
projects; post-FC is reactive) opened a new control-theoretic track: **D102 — a learned plant
model (heat/fan → RoR) → predictive (MPC) post-FC control**, the process-model half of the D42
learning loop, captured in `roastpilot-plan/roastpilot-agent/plant-model-mpc-plan.md` + issue #580.
**Phase-1 ARX feasibility MERGED (PR #581): verdict NEEDS-MORE-DATA — the blocker is EXCITATION,
not sample count** (heat is pinned ~65 %, the advisor moves fan not heat, so the heat→RoR channel is
barely stimulated; ARX barely beats persistence in the BT≥150 control regime). Corpus = 47 Artisan
`.alog` + 14 store runs (the WAL-safe copy fix caught a completed run hiding in the WAL, 13→14),
same Hottop + room; reproducible via committed harness + a data-manifest fingerprint (no raw roast
data in the repo). Path to GO: designed excitation (safe heat staircase/PRBS) + a grey-box FOPDT +
a control-regime acceptance gate. **Discard-roast feature (#582, PR #583):** a reversible `excluded`
flag that filters a bad-data roast out of history + the learning corpus WITHOUT deleting — motivated
by the 18 Jul Brazil second batch (FC detector didn't fire, operator marked ~7 °C late → bogus DTR
6.7 %). Schema V13, NO immutability-trigger change (safety-reviewer PASS: `excluded` mutable while
real-field UPDATE + DELETE still abort); `list_runs` + reference retrieval + the fixture exporter all
filter `excluded=0`; `count_completed_runs_for_origin` deliberately NOT filtered (recording-slot
counter — excluding would collide the discarded run's preserved audio). The FC-miss audio + a
labelled `fc-miss-label.json` are preserved under `~/roasts/captures/` for FC-detector fine-tuning.
Roasts this day: Brazil Santos baseline (dev 14, dropped ~188/15.3 %) + a second batch (discarded).
Follow-up tracked: the plant-model harness (`scripts/plant_model_arx_study.py`) also reads the store
unfiltered and should skip discarded runs once #582 lands.

**18 Jul 2026 — #567 REFERENCE CURVES: offline bake-off ran NEGATIVE → feature PARKED
(PR #578, data). The build stays DISABLED; no hardware roast warranted.** Operator picked the
3-arm bake-off (design §6.4) as the validation gate, then chose **park** on the result. New harness
`scripts/bakeoff_reference_567.py` (store-roast replay + self-excluding reference retrieval + 3 arms
+ spend cap/resume/dry-run); 10 held-out runs / gpt-4o / 0 errors / ~$2; result
`docs/advisor/reference-curve-bakeoff-2026-07-18.md` (raw per-tick JSON deliberately
NOT committed — AGENTS.md forbids checked-in roast logs). **Two clean findings:** (1) the
reference DATA untaught is INERT — arm2==arm1 on 8/10, aggregate unchanged (12.4→12.3 % DTR), model
never cites it (the 30-pt curve is ignored without teaching, cutting against the design's "data beats
prose" hope); (2) the c9 TEACHING moves the drop EARLIER/shorter (12.4→10.6 % DTR, toward
under-development, worst case DTR 6.5 %) and cites the reference only 3/38 — the fragile-prose
perturbation the whole arc warned of, not reasoning from the reference. Caveats: n=3 beans /
replay-only; corpus references are mostly 3★ (weak teachers). **Revisit direction (if ever): the DATA
REPRESENTATION (surface the reference as labelled comparison numbers — the arithmetic form the model
reasons on), NOT more c9 prose — a redesign, hence parked not iterated.** Gate did its job (caught a
regression pre-hardware). Ops gotcha recorded: the advisor reads `OPENROUTER_API_KEY` from
`os.environ`, so a stale shell-profile export SHADOWS a valid `.env` value → `401 User not found`;
pass the `.env` value explicitly to bake-off scripts. **The [[567-reference-curves-v1-shipped]] code
(3 slices) stays on `main`, disabled — do not re-build; #567 remains OPEN, now parked.**

**17 Jul 2026 — #567 REFERENCE CURVES: full code build SHIPPED across three slices
(PRs #574/#575/#576), feature ships DISABLED. Operator picked #567 to build (said they'll do the
D96 validation roast after it lands).** The designed-but-empty `reference_roasts` is now a working
advisor-context input — the operator's own best-rated past roast of the SAME bean, retrieved at
roast start and shown to the advisor as read-only context — gated OFF by default. Serial stack off
`main`, lead-orchestrated (one `engineer-be` per slice + `safety-reviewer`/`qa` pre-open reviews +
Codex post-open); ~13 reviewer findings folded across the stack, zero un-triaged threads:
- **Slice A (#574, `46c5922`) — retrieval + representation, INERT (no wiring).**
  `ReferenceCurveSample`/`ReferenceLandmarks`/`ReferenceRoast` models + `RoastStore.find_reference_run`
  / `load_reference_roast` (private `_build_reference_roast`): best-rated (≥3) completed prior run of
  the same `recording_origin_slug` within ±10 % charge weight, tie-break recency, best-USABLE
  fall-through, `outcome='completed'` filter, clock-safe drop/FC landmarks off the development-phase
  telemetry rows (no cooling-tail bug, §6.4a), ≤30-pt curve trimmed at drop. qa (3 test gaps) + Codex
  (5: raw-string `RoastPhase`→typed `is`, faulted-run exclusion, unbuildable fall-through, curve trim,
  private builder) all folded.
- **Slice B (#575, `02b6626`) — plumbing, flag-gated default OFF.**
  `controller.reference_curve.enabled` (default `False` = zero store reads, empty context, identical
  CONTROL behaviour); `AdvisorContext.reference_curve`/`reference_landmarks` (read-only, shape-only, no
  levers); fail-soft flag-gated retrieval retrieved ONCE in the common `_build_runner` (covers fresh
  start + operator-resume; the just-created run excluded by hard SQL); replay pins retrieval OFF
  (`use_live_reference_retrieval` hatch, mirrors the post-FC pin — no lookahead leak). **safety-reviewer
  PASS** (six invariants traced: read-only, no control authority, restart-no-auto-resume, fail-soft);
  qa folded (retrieve-once `calls==1` proof, end-to-end replay no-leak, doc precision).
- **Slice C (#576, `c544667`) — c9 teaching + Config selector.** c9 = c8 + one minimal, non-imperative
  reference section ("deviations are information, not commands"; names NO numbers; subordinate to the
  profile's own targets and the joint-objective teaching); selectable-only, **c3 stays the live
  default**; c9 added to the SPA prompt-version selector. qa PASS; Codex folded (expose c9 in the UI
  selector) + one reasoned dismissal (`reference_curve.enabled` is env/launch-toggled like every
  post-FC sibling flag — `post_first_crack_control.enabled`/`ceiling_guard`/`recovery_enabled` are all
  absent from `/config` too — not a piecemeal UI decision for a prompt PR).
**#567 stays OPEN** — feature ships DISABLED; promotion is gated on (1) the 3-arm offline bake-off
(design §6.4: c8-no-ref / c8-ref-untaught / c9-ref-taught) as the FIRST gate, THEN (2) a hardware roast
with a live reference (El Durazno / Colombia / Sumatra all qualify in the corpus). **The 3-arm bake-off
is the next validation step — paid OpenRouter, produces DATA; offered, operator-gated.** Process notes:
LSP diagnostics were STALE all session (phantom `build_reference_roast`/`tz` flags — real `pyright`
always authoritative); the auto-mode classifier blocked `git commit --amend`/force-push in the main
loop → used normal commits (squash-merge collapses them).

**16 Jul 2026 — BATCH 4 COMPLETE: four tracks, three PRs merged + one design note
(PRs #568/#570/#569; note on #567). D96 recovery law now FULLY BUILT on main.** Operator: "file
it and run with it. Any other autonomous tasks, update the boards and get teams on them." Four
parallel tracks, lead-orchestrated (per-track engineer + Opus safety reviewers), all landed:
- **#563 → #570 (told-ceiling separation) MERGED** — the highest-leverage advisor fix; full
  detail in the entry directly below. C4 bake-off PASS (9/15 vs 14/15 rationale-conflation, 0
  regression).
- **#561 → #569 (post-failure heat-to-base clamp, D96 slice 1.5) MERGED — completes the D96
  recovery law on main** (slice 1 #560 + slice 1.5 #561 + slice 2 c8 #562, all flag-gated OFF).
  Most-scrutinised path of the batch: TWO Opus safety reviews (core PASS + a focused re-review
  PASS) + TWO Codex rounds, every finding real — a fourth uncovered drop path (operator_drop), a
  stale-advisor-output gap after the forced recovery exit, a fail-safe-down hole (forcing HOLDING
  re-enabled a base-seeking raise) → the `_post_fc_raise_suppressed_after_clamp` latch; then a
  failed-corrective-write asymmetry + the latch stranding fan → `self_healing` param + heat-only
  suppression via a threaded `actuated_heat`. **CI caught a real sibling interaction:** the branch,
  built pre-#570, used `ceiling_guard_temp_c=220/230` fixtures that #570's told-ceiling change now
  feeds into `PhaseControlLimits` → tripped its `emergency>bitter` validator → test-only fix
  mirroring #563's `_ISOLATED_CEILING_GUARD_LIMITS` (the #453 build-before-a-sibling-merges class).
- **#566 → #568 (rating + tasting merged into one entry gesture) MERGED — 7 Codex rounds, every
  finding real, three async-state-machine classes converged at the ROOT** (not point-patched):
  (1) cache-seed races → surgical merge-only `setQueryData` + `exact` cancel; (2) partial-retry/
  lock → frozen partial-failure mode + full-payload capture + a module-scoped `useSyncExternalStore`
  lock on the full `isFrozen` lifecycle (throughline: UI-coordination state does NOT belong in the
  server cache); (3) component keying (RoastRating keyed by runId like its sibling). A durable
  codex-wait specimen: green CI passed at round 1; the diff-read caught real cross-widget data-loss
  across 7 rounds. Stopping-line judgment applied (effort-vs-value, not a mechanical severity gate).
- **#567 (reference curves) — design note FINALIZED + attached to the issue; build PARKED at the
  operator gate** (new-feature scope). n=3 corpus, replay-pinned-off, 3-arm bake-off, ship-disabled
  flag; store re-verification fixed two stale facts (n=1→n=3 beans; a cooling-tail DTR artifact →
  authoritative 190°C/715s/15.1% + 191°C/687s/12.4% off `development_percent`). **Open follow-on
  (operator-raised):** a deterministic bean-SIMILARITY retrieval (altitude/density/moisture/
  processing/size) as #567 v2 fallback for beans with no exact-slug match — assessed as sound but
  gated on a schema gap (density + moisture not captured; altitude is a weak density proxy) and the
  n=3 validation limit; a scoped research pass offered.

**Process ledger:** the two-lens review (Opus safety-reviewer + Codex diff-read) repeatedly
surfaced real interaction bugs neither caught alone (sharpest on the #569 clamp); author-fixes /
lead-triages held throughout (D23); auto-merge never armed on the safety PRs; #570/#569/#568 all
merged on the Codex-silent fallback (well-vetted). New runtime bean added same session: **Brazil
Santos (Natural)** — 250 g / drop 195 / dev 14 % (de-risked first roast; extend to 15-16 % for
deeper chocolate). **Next:** the D96 validation roast (c3 + /config trim 60 + Sumatra dev 19 +
`recovery_enabled` flip — operator-gated hardware); the #567 build decision + the similarity-v2
research; #396 full-corpus advisor A/B (optional).

**16 Jul 2026 — #563 TOLD-CEILING SEPARATION SHIPPED (design → Opus safety review
PASS → C4 bake-off PASS → PR).** The highest-leverage advisor fix the D96 prompt-testing arc
found: `RoastControlPolicy._bitter_ceiling_temp_c()` capped the told bitter ceiling at
`min(196, target_drop_temp_c)`, making the told number IDENTICAL to the drop target on every
seeded profile (195 == 195) — a told≠enforced violation the c7/c8 prompt teaching's
ceiling-minus-bean-temperature gap arithmetic then read as "no overshoot flexibility, drop
early" (a correct inference from a false premise). Fix: the told ceiling now reflects what is
ACTUALLY enforced — `ceiling_guard_temp_c` when the post-FC ceiling guard is enabled (the
number that fires the real deterministic drop), or the hard `SafetyLimits.bitter_ceiling_temp_c`
when the guard is disabled (an accepted, documented told≠enforced gap in that configuration —
only the 230 °C e-stop is truly enforced there, but teaching 230 °C would license far more
overshoot than the operator's empirical bitter line). Neither branch reads `target_drop_temp_c`
any more — the target and ceiling are independent numbers. **Process, in full:** design note
(consumer trace: `safety.py` never reads either temp field, so the only consumer is the advisor
context; replay is definitionally immune, `advisor=None`/`run_loop=False`) → Opus safety review
(`safety-563`) PASS with 5 conditions (keyword-only `post_fc_control` param mirroring
`pre_fc_levers`; a semantically-flipping test rewritten + a guard-value-mismatch test added so
the wiring is proven, not a numeric coincidence; the told≠enforced gap commented at the code
site; C4 merge-gate bake-off; no target>ceiling validator — that configuration is pre-existing/
tested, unaffected by this change) → implementation (found + fixed 5 `test_controller.py`
D96/#560 recovery-law tests whose `ceiling_guard_temp_c=220.0` isolation fixture became
inconsistent with the box's own validator once the guard temp started feeding the told ceiling
directly — a `SafetyLimits` override restored their original intent; also surfaced a fail-closed
hardening: an inconsistent guard config now raises `ValidationError` at construction instead of
silently misinforming the model) → the Opus reviewer's own masked-regression check (nearly
false-alarmed on a pytest worktree-resolution artifact, re-ran with isolation, confirmed the 5
edited tests still go RED without the real fix) → C4 mini bake-off (~30 calls gpt-4o+c3, ~$0.60,
roast-12's real trajectory + 4 constructed probe ticks 191-194 °C): **the mechanism-level read
was the real evidence** — 9/15 (60%) of old-scheme rationales explicitly stated the conflated
"ceiling equals target" premise (one reasoning off a ceiling the bean had already EXCEEDED) vs
14/15 (93%) of new-scheme rationales correctly citing the real 196 °C line with genuine
headroom; raw drop-rate table noisier (the conflation mostly changes the STATED REASON, not
always the decision, once DTR is in-window near either number); zero regression. Cross-refs:
issue #563, D96 (the arc that found the root cause), memory `told-vs-enforced-bitter-ceiling`.

**16 Jul 2026 — D96 BUILT END-TO-END OVERNIGHT (PRs #558/#560/#562/#564); THE
PROMPT-TESTING ARC FOUND THE TOLD-CEILING ROOT CAUSE (#563).** Slice 1 (#560): the
bounded-bidirectional recovery law, merged DORMANT (`recovery_enabled=False`; validators require
the ceiling guard AND the master flag) after five review rounds — entry/exit hysteresis with a
ceiling glide, the drop-tick raise suppression (actuated-level-compared after a P1 showed the
skip could freeze LOWERING writes on failed drops), #561 filed as slice 1.5 (post-failure
heat-to-base clamp, before promotion). Slice 2 (#562): c8 (pace-not-edges + bottom-edge
target-seeking + fan→RoR coupling) + AdvisorContext gains the loop's setpoint and a
PostFcHeatAuthorityState enum (told==enforced, mutation-tested). #564: the teaching revision —
numeric-comparison ceiling framing, an anticipatory per-minute-arithmetic drop trigger
(bean_ror_c_per_min as the unit, bitter_ceiling_temp_c explicitly named), confidence-scale pin.
**Four live bake-off runs (192 calls, $1.56, operator's standing OpenRouter approval) drove the
revisions and found the structural root cause:** c7/c8's original ceiling emphasis hallucinated
"bean at ceiling" 8 °C early; post-fix the failure MOVED to inference ("ceiling==target ⇒ no
overshoot room ⇒ drop early") — a CORRECT reading of a misleading input, because
`_bitter_ceiling_temp_c()` caps the told ceiling at min(196, target_drop) → 195==195 on every
profile, a told≠enforced violation vs the real 196 guard → **#563 (likely the highest-leverage
advisor fix)**. Design fact recorded: joint-objective PATIENCE architecturally outweighs the
anticipatory projection when DTR is short (models hold ~50% even at a sub-actuation gap, without
miscomputing) — #563's true-ceiling separation may dissolve the contest. #396 provisional:
prompt >> model; gpt-4.1-mini decision-competitive at 1/6 cost. c3 stays the default AND the
live selection; c7/c8 selectable-only with the full record on #499. **Validation-roast recipe,
all merged: c3 + /config trim 60 + Sumatra dev 19 + `recovery_enabled` flip; watch the first
raise event, exit re-trigger, real plant gain.** Also merged: #558 (the roast-day UI P1 pair —
mismatch-view reload brick, stale-card false positive). Proposed next tracks: #563 design pass;
reference curves (the designed-but-empty `reference_roasts` — the D95 bean-aware input; the
operator's own context question surfaced it).

**15 Jul 2026 (latest) — D94 AFFORDABILITY LAW FALSIFIED PRE-MERGE (D95); PR #551 CLOSED
UNMERGED; D88's VALIDATED STACK STANDS.** The operator's flat-cup verdict on roasts 13/14
ratified the #521 affordability-anchored post-FC setpoint (D94 + same-day m=0.1 hysteresis
amendment). It survived an Opus design review, a store-exact literal derivation, an Opus diff
review, and a 10-case fail-then-pass test matrix — and was then FALSIFIED by a Codex P2:
the remaining-dwell budget omitted the time already elapsed at the decision instant, a
near-CONSTANT ~22.0 s (the FC detector's 20 s confirmation window + processing; measured
21.99-22.02 s across all four D88-era roasts). Corrected, the law fires on NOTHING — its only
validating cut (roast 14) was a <0.4 °C/min artifact of the omitted term cancelling the
60 s-window RoR's lag. No corrected form is constructible with current instrumentation (no
shorter-window RoR exists; pre-crack ramps converge; the 12-vs-14 quality difference lives in
the BEAN — identical realized drop signatures, opposite cups). Full record: PR #551 (closed,
branch `feature/521-affordability-anchor` preserved), plan D95, issue #521 (closed,
`prevented-pre-pr`). **Standing:** D88's hold-at-engagement + never-add-heat + 196 guard (the
9/10 stack, byte-unchanged). **SHIPPED same day (constructive follow-through):** #499 part 2 as
**c7** — the roast-13 fix as prompt TEACHING, not arithmetic (PR #554; selectable, c3 stays
default; promotion gated on hardware validation per the bar on #499); and the **Sumatra seed
dev target 17→19** (PR #553 — guard floor 16 now rejects the failed 15.1 % repeat; the first
proposal's `pre_fc_heat=60` mechanism was ALSO Codex-falsified — the field governs the whole
pre-FC ramp, so the per-roast momentum lever is the /config late-Maillard trim depth ~60,
documented in the seed comment; runtime rows corrected operator-side incl. the El Durazno
13→16 drift). Next-roast recipe: Sumatra + /config trim ~60 + c7 selected. **Enablers filed:** coffee-roaster-mcp#196
(short-window RoR field), #380 (more motivated — it shrinks this exact error class at the
source). Durable lessons in D95: validate control-law arithmetic against input CLOCK SEMANTICS
before ratifying; knife-edge validation margins are the tell; n=2 different-bean evidence fits
bean identity, not physics (bean-aware laws belong to the E14/D42 corpus trajectory, not the
controller).

**14 Jul 2026 — BATCH 3 COMPLETE: #525 + #530 + E11 KICKED OFF (PRs #546–#548).**
Three tracks, three teammates in explicit worktrees. **#548 (#525 stale-session clear, D92):**
the #523 S4 gap closed as a pure store write behind a three-guard design — (a) own-active-run
409, (b) atomic unfinalised WHERE, (c) a **two-clause per-run-budgeted liveness gate** (recent
telemetry OR recent start; effective window = `max(answerer_window, owner_window)` where each
window is `max(20.0, 4 * telemetry_log_interval_seconds)` — the owner's interval read from the
target run's frozen `config_json`, the answerer's from its own live config; wider wins,
fail-closed) — with zero MCP writes pinned by a call-count test and audit rows on every
path incl. 404s. The full arc: design note → Opus design review PASS-WITH-CONDITIONS (found the
shadowed-live-run kill chain: a clear could abort a ticking roast AND 410 its API e-stop) → D92
(the issue's "MCP-idle gate" formally replaced by DB write-recency) → Opus impl review PASS →
Codex rounds 1-2 each finding a REAL hole (the pre-first-telemetry actuation window → the
start-recency clause; answerer-vs-owner window config provenance → per-run json_extract) →
round 3 silent. Load-bearing facts recorded in code + D92: a same-process bare orphan is
unreachable by construction (start_roast critical section + recover_on_start's two-bucket
funnel — the stale card and clear action are inherently multi-process tools);
`roast_runs.config_json` freezes controller config at creation (per-run provenance pattern).
**#547 (#137, D93): E11 is now IN PROGRESS** — S1's wheel/SPA half shipped (hatchling build
hook scoped to the wheel target, rebuild-always-when-npm, package-data resolution + clean-venv
install proof, CI package job); **the `[pi]` extra is HELD** — coffee-roaster-mcp 0.1.13 still
hard-requires transformers, so #137 stays open blocked on coffee-roaster-mcp#157 (D27 Phase 2,
Pi-5-gated); base wheel verified lean. **#546 (#530):** the devices-spread viewport override +
missing `fullPage` fixed; the bottom third of every page (ratings, weights, tastings, export,
config safety/recovery pane) is in the visual baselines for the first time. 16 verified review
findings folded + 1 evidence-based refutation across the batch; zero un-triaged threads.
Remaining backlog unchanged: #521 (tasting-gated), #396 (credits-gated), #137's extra half +
MCP#194/#157 (Pi-5-gated), E11-S2/S3, E12 (operator-supervised), #380 (wrong-premise candidate
vs the fc-detector-lag no-magic-offset decision — flagged, not closed).

**12–13 Jul 2026 — OVERNIGHT TEAM BATCH + FOLLOW-UP BATCH 2 COMPLETE (18 PRs, 3 repos,
MCP v0.1.13).** Batch 1 (four tracks: fe-523 / be-signals / mcp-audio / fc-docs — 13 PRs: agent
#524/#527/#528/#529/#532/#534/#535/#536/#538, MCP #192/#193/#195, FC #67): the #523 UX/IA
restructure (/live = roast state, /start = only start surface, / = links hub, nav always visible;
PRs #528/#529/#532/#535, closes #517), #522 tasting-signal capture for E14 (#527, SCHEMA_V11),
#520 charge correction (#534, SCHEMA_V12, atomic cross-endpoint bounds), #516 health instance_id
defence for the port-impostor class (#536), the AGENTS.md **Codex-wait rule** (#524 — operator-
instituted after #518 merged mid-review; the rule prevented a real 11-test coverage regression
the same night), and the MCP #190/#191 audio-overflow arc (MCP PRs #192/#193/#195: reader-thread
split, capture-time stamping, milestone recovery with earliest-eligible cutoffs) → **v0.1.13
released** + agent pin/mirror bump (#538). Batch 2 (operator: "launch the five"; serial
be-signals track, PRs #540–#544): #531 flake→poll-until-condition, #526 unmount guard (+ the
React-18 finding: unmount-guard tests on local-state-only paths are PROVABLY vacuous — recorded
in recent-fixes), #539 overflow diagnostics in MicStatus/drawer, #533 tasting UX debt (+ Codex
caught the new completed-gate violating the batch's own freshness class → 5s live-only poll),
#537 mismatch-view field-protocol copy (3 fold rounds incl. a real P1: e-stop-first before the
launcher-restart step — the launcher force-kills without heat-off). 7 verified findings folded in
batch 2, zero dismissed, zero un-triaged threads across both batches. Deferred deliberately:
#525 (stale-session clear — needs its safety design pass), #530 (snapshot viewport story), #521
(tasting-gated), #396 (credits-gated), MCP#194 Pi-5 soak (hardware-gated). Roasts 13 (El
Salvador — first merged-stack validation; advisor fan actuation + joint drop 190 °C / 20.9 %)
and 14 (Sumatra — late FC at 186 °C, dev 15.1 %; operator steer → #521 RoR-aware decisive heat
cuts) logged same weekend; tastings pending.

**12 Jul 2026 (PM) — PAPER-RIGOUR BATCH COMPLETE: #495 CLOSED (3 parallel tracks, PRs
agent #508/#509/#510 + FC #64/#65).** Every pre-submission gate item is now committed, reviewed,
reproducible evidence: **McNemar p=0.0039 REPRODUCED exactly** from the committed bake-off
artifacts (`scripts/advisor_significance.py`, PR #508; Wilcoxon p=0.0098 explicitly marked
newly-computed — qa caught the retroactive-embellishment risk against the original D34 commit);
**FP discrepancy reconciled with forensics** (FC PR #64: README's "6 FP" scored a 13-min-stale
split — gitignored experiments/ was the systemic cause; the citable number is baseline_v5 int8
= 91.1 % P / 97.6 % R / F1 0.943 on the current 303-set); **int8 re-benchmarked on the deployed
checkpoint** (no quality loss — marginally ahead — ~2× latency); **RF+MFCC comparator** (FC PR
#65: pre-registered protocol, NOT competitive — F1 0.474, recall 42.9 % disqualifying; the
transparency check itself made reproducible after qa's catch); **plan-repo Dependabot triaged**
(all 30 alerts = Figma-sketch-only deps, dismissed not_used). **Bonus find: #507** — assigned
MCP#189, the engineer PROVED it wrong-premise (the agent's controller mirrors, never reset on
any drop/e-stop path, were the real source of roast-12's stale 91 % readout through cooling)
→ fixed via typed AppliedRoasterState adoption from the command's own result payload at all
five call sites (PRs #509+#510; safety review split the verdict with precision — e-stop paths
correct, drop paths failed closed on observability → Optional-return at one choke point;
Codex caught the out-of-range ValidationError escape post-merge, closed same-day). FC repo
process note: NO required status checks there — --auto merges instantly; lead merges manually
on verified green until the operator decides on protection. Next: paper WRITING (rigour done);
D89 Tier 2 ratification when wanted; #396 c1-vs-c3 A/B remains the one open experiment thread.

**12 Jul 2026 — D89 TIER-1 BATCH COMPLETE + D88 FLAGS PROMOTED (plan D90; PRs #501–#505).**
Tasting ratified the validation (taper cup 9/10, "like sugar" — the program's best-rated roast;
the 9/10-Jul cups "a bit flat" → El Durazno seed dev target 13→16 %). Shipped in order: **#497**
(advisor context carries ACTUATED heat/fan + loop-mode flag via a shared helper the actuation
gate also calls; Codex extension folded — all four offline builders populate the fields), **#498**
(advisor fan actuates in loop mode via COALESCED SINGLE-WRITER — pre-open safety review caught a
BLOCKER before the PR existed: two per-tick writers colliding on the command rate limit silently
dropped the advisor's fan exactly when heat moved; redesigned, re-verified by the same reviewer
instance; intended one-tick fan lag documented), **#499** (joint drop-window prompt + DTR window
[target ± drop_dev_margin_percent] from the SAME constant the drop-coherence guard enforces;
style NAME only — option A ruling on the #499 thread; 3 post-merge Codex prompt catches fixed in
#504 incl. the assembled-prompt CONTRADICTION class → new assemble-then-assert test class +
recent-fixes.md entry), **#505** (both D88 flags default-ON + launch-script `=0` opt-outs +
runbook + **the replay-pins-baseline invariant**: `build_replay_service` pins post-FC flags OFF
unless `use_live_post_fc_control=True` — without it the promoted guard default injected a
policy/ceiling_guard drop into a recorded 206 °C fixture, pre-empting its operator drop;
measured per-fixture, Playwright baselines unaffected). Next roast runs the merged stack BY
DEFAULT. Open on **#495**: paper-rigour items, the #499 offline prompt comparison (blocked on
the .env key refresh), plan-repo Dependabot triage. D89 Tier 2 needs its own ratification.

**11 Jul 2026 — D88 VALIDATION A/B PASSED + D89 MERGED-CONTROL DIRECTION RATIFIED.**
Roasts 11/12 (runs `d55b0fce` baseline / `edbe9a76` treatment, Guatemala El Durazno 2×250 g,
dev target 15 %): the D88 taper's first hardware run **held every structural property** — heat
anchored at its 65 % engagement value and NEVER rose (trace-verified benign: measured RoR
7.0→5.0 sat at/below the declining setpoint 7→4, so the never-add-heat ceiling was the binding
constraint; nothing to cut), the 196 °C guard stayed correctly silent, and the convergence
thesis validated: **treatment dropped 194 °C @ 13.4 % vs baseline 190 °C @ 15.0 %** (the
baseline's heat-0 over-brake stalled temp while DTR accrued — targets diverged; the taper kept
them together). Weights 215 g / 218 g. Both drops advisor-called (earlier-only authority).
**Both flags remain default OFF** — promotion + taper-constant tuning gate on the operator's
tasting (roasts 9–12). Same-day pre-roast tooling: **PR #496** (CEILING_GUARD launch toggle +
independent per-flag banner lines; pre-open qa caught the untested call-site wiring — mutation
kill verified). **Findings filed:** #497 (AdvisorContext carries NO actuated heat/fan — the
advisor reasoned from an imagined heat-0 in loop mode), #498 (loop-mode fan stayed pinned 40 by
D88(5) design; operator steer = dynamic fan up to 100 %), #499 (drop decisions are
first-past-the-post — both arms sacrificed one target, in opposite directions). **D89 ratified
(plan repo): merged post-FC control — Tier 1 division-by-lever via #497→#498→#499 in order;
Tier 2 bounded advisor nudge (±1.5 °C/min proposal) on the taper SETPOINT, never the actuator,
own ratification before code.** Next: operator tasting verdict → the Tier-1 batch. Also this
session: conference research pack DONE (blog-sources/24-*, 109 verified citations + grounded
method inventory; pre-submission gate = #495); tracking issue **#495** holds all remaining
rigour tasks. Memory `roast11-12-d88-validation-ab`.

**9–10 Jul 2026 (overnight) — AUTONOMOUS BATCH COMPLETE: 7 PRs merged, the roast-night
findings all actioned. #405 REWORKED per D88 (both flags OFF), #484 fixed with real-child tests,
the config-UI trio closed.** One lead + 4 worktree tracks + a persistent review roster; operator
away (MCP release approval delegated — none needed).

- **#405 rework (PRs #490/#493, plan D88 — supersedes D83's law):** trace analysis of the A/B
  (`docs/analysis/2026-07-09-roast9-10-postfc-ab.md`, PR #487) measured the flaw exactly — the
  fixed 8.0 target sat ABOVE the measured 6.1 engagement RoR; the loop actuated 72→91 % from the
  FIRST post-FC tick while the advisor said 0; ALL verdicts ALLOW (a control-law gap, not a safety
  gap). **Corrected record: roast 2's drop was ADVISOR-triggered at 194 °C / 16.51 % — the
  earlier-only authority WORKED and ended the runaway.** The D88 law (safety-reviewer ratified
  with 7 amendments, then verified the implementation incl. mutation tests): setpoint tapers from
  the MEASURED engagement RoR (clamped [end, start-max]) down to 4.0 over 90 s; output can NEVER
  exceed heat-at-engagement (the 1 % anti-stall floor wins); taper clock on the actuation clock
  with gap-resume dt capped (a Codex catch — the integrator was also exposed); PLUS a DECOUPLED
  196 °C ceiling-guard drop on its OWN flag (fires regardless of the loop flag and after
  recovery-resume; the first cross-section AppConfig validator; typed `DropReason`). **Both flags
  default OFF — the flip is the operator's conscious decision at the validation roast (D88/A2).**
- **#484 fixed (PR #492):** the stdio session lives in ONE owner task (respawn = a request, never a
  cross-task teardown); the fail-closed surface covers EVERY exit — wedged-timeout, raising aclose
  (a safety-reviewer-DISPROVEN "unreachable" pragma → policy: force-terminate +
  `stop_unconfirmed=True`), cancelled stop (mark-then-re-raise), startup-abort overrun. NEW
  real-child (mock-driver) respawn suite incl. the exact operator repro — both tests fail pre-fix.
  The fake-only respawn coverage gap that let the crash through is closed.
- **Config-UI trio closed:** #483 (PR #486 — save rebaselines from the PUT response; + 2 Codex race
  fixes: disable-while-pending, cancelQueries-before-setQueryData), #482 (PR #491 — inherit fields
  show the SOURCE-yaml value "Inherit from yaml (audio)", never a bogus "Disabled"/"0";
  restore-to-yaml; shared resolver so the GET + spawn paths can't drift; ui-reviewer drove the UI
  to confirm the scare dead).
- **Review-system ledger:** Codex 7 real catches (2 save races, sibling-pressure on a pragma, the
  taper gap-swallow incl. the integrator, 3 lifecycle-cancellation bugs) — every one in code the
  other lenses had passed. safety-reviewer EMPIRICALLY disproved a second false "unreachable"
  pragma (the night's recurring class: 4 comments/docstrings-that-LIE caught + fixed). qa
  mutation-testing killed 4 weak tests. **Incident:** a reviewer's `git checkout --` wiped
  uncommitted work in a shared worktree → recovered from its captured diff + author line-by-line
  verification → the worktree runbook now mandates lead safety-commits before reviews + a
  cp-snapshot mutation protocol (no tree-mutating git in shared worktrees). **pr-preflight
  refined:** the coverage check now runs `--cov-branch` (#492's patch failure was 3 diff
  branch-partials that line-coverage missed).
- **Next:** the beans (ratings feed the corpus + the taste verdict); the NEXT supervised roast
  validates D88 (flip taper + ceiling-guard consciously, re-A/B); then #323/#380 tuning. E11/E12
  remain the post-validation arc.

---

**9 Jul 2026 (evening) — FIRST SUPERVISED HARDWARE A/B of #405. The A/B did its job: it
CAUGHT A REAL #405 DESIGN FLAW. #460 STAYS OFF (plan D87).** Guatemala El Durazno (White Honey),
2×250 g, baseline (advisor-driven) vs #405 deterministic post-FC loop.
- **Roast 1 baseline (#405 OFF, `bf85c77a`):** clean, on-target — drop 189 °C, **DTR 13.6 %**, advisor
  ramped fan + called the drop at 189/13.1 %.
- **Roast 2 #405 ON (`a4299aea`):** the flaw. Post-FC RoR declined to ~5 °C/min; the loop's fixed
  ~8 °C/min target made it **crank heat to 89 %** to chase the band → bean racing the 196 ceiling →
  **potential overroast**. The advisor CORRECTLY wanted heat 0 (brake) but was overridden. Operator
  caught it + manually dropped ~193. **Both extremes fail** (loop over-heats / a hard-0 advisor
  over-brakes) → **post-FC needs a DECLINING-RoR taper + a hard ceiling-guard drop** (drop on bean ≥
  ceiling regardless of dev %). Plan **D87** supersedes D83's fixed-band; #405 has the full brief.
- **Big validation night:** ambient probe end-to-end (Yocto → MCP → agent → **live UI triad** + on
  both roast_run rows), the Guatemala seed, `POST_FC_LOOP=1` toggle + banner, accurate T0, weight
  entry, dual-mic recording — all proven on real hardware in one session.
- **Bugs surfaced live:** **#484 (HIGH)** — changing a `/config` device setting MID-SESSION crashes
  the app (the respawn's anyio cancel-scope bug on the real stdio child; only tested vs a fake MCP
  before). Workaround: set config then RESTART, never mid-session. Plus **#482/#483** (config UI
  inherit-render + stuck save-warning). Ratings pending (taste tomorrow) → full trace+taste A/B then.
- **Next priorities:** (1) rework #405 post-FC (declining-RoR taper + ceiling-guard) → then re-A/B;
  (2) fix #484; (3) #482/#483. Memory `roast9-10-postfc-ab-hardware`.

---

**9 Jul 2026 (daytime) — CONFIG-SCREEN audit → 2 gaps closed (#473/#474 merged), 1 filed (#475).**
Operator asked: is `/config` functional, where's the link, are temp-sensor settings exposed? Audit
(read-only investigation): **functional YES** for its scope (real GET/PUT round-trip, safety
read-only, applies-next-roast via MCP respawn, strong component tests); **link = NONE** (URL-only —
no home tile, no nav link); **temp-sensor = NOT exposed** (ambient shipped after the Config UI, so
`ambient.mode`/`device`/`poll` had zero representation in the config model/overlay/schema). Shipped:
**#473** (Settings home tile + nav link — the screen was built but unreachable; the "data-complete is
not done / last-mile" class, memory `data-complete-is-not-done-last-mile`) + **#474** (ambient/
temp-sensor mode/device/poll editable in `/config`, extends the D78/D80 MCP-device pattern 1:1 —
tri-state inherit, passthrough-merge overlay matching the MCP `AmbientConfig` keys, applies next-roast
via respawn). Both `qa`-PASS pre-open, merged CLEAN. **#475** (config Playwright e2e) filed, low-pri
Todo. Context: a GitHub Actions `pull_request`-trigger outage mid-session blocked CI for ~1h (repo
workflows didn't fire, only managed CodeQL) — the #472 registry PR needed a one-off operator
admin-override (enforce_admins toggled + RESTORED); CI recovered and #473/#474 merged normally. See
memory `baseline-regen-skip-ci-retrigger` for the `[skip ci]` re-trigger gotcha.

---

**8 Jul 2026 — AMBIENT-UI SHIPPED (#464 + follow-ups #467/#463, D86) + an AUTONOMOUS
HOUSEKEEPING BATCH. Everything not gated on the hardware roast is now done and consistent across all
three repos.** Two threads this session:

- **Ambient probe now SHOWN in the UI (#464/D86 — closed) + both follow-ups closed.** #342 captured/
  stored the triad and exposed it on `RoastDetail`/`RoastSummary`, but the frontend rendered none of
  it (grep of `web/src` = zero refs) and it was deliberately off the SSE frame (D85 — corpus-only).
  Operator asked "are we showing this when roasting?" → we weren't. **D86 revises D85's SSE-exclusion
  point** (operator chose live=latest, option A): the readout rides the SSE telemetry frame, mirroring
  `mic_status` 1:1 — **no MCP change** (the MCP already ~30s-caches + reports `ambient_status`; the
  agent already mirrors it per tick). Shipped: **#466** (BE — `project_live_ambient` → `RoastTelemetry`
  → `TelemetryEventData`, built in api.py not the controller → no safety surface) + **#468** (FE —
  live "Room" readout on the dashboard, charge-time card on detail, history column; null→"—"). Two
  follow-ups also DONE: **#467** (replay SYNTHESIZES a representative ambient — 21/45/1013 via
  `_synthesized_ambient`, mirroring `_synthesized_mic_status`, since exports carry no ambient — so
  `dashboard-live` + demos show real values not "—") and **#463** (explicit `ambient_captured`
  store-V10 latch replaces the `temp IS NOT NULL` inference so a status=ok-with-null-temp capture
  never re-fires post-restart). Review roster earned its keep: `qa` caught the #241 "fixtures test
  nothing" anti-pattern + the baseline-regen Playwright run caught a testid-on-container bug (unit
  tests don't run `.spec.ts`) + Codex caught a `formatAmbientCell` non-finite gap and a
  `.playwright-mcp/` artifact sweep-in. CI-plumbing lesson captured: the `web-snapshots-update`
  `[skip ci]` re-trigger trap (memory `baseline-regen-skip-ci-retrigger`).
- **Cross-repo autonomous batch (sibling repos, all merged):** `coffee-first-crack-detection` went
  from ZERO CI to full CI (#57 gates + torch-free-import guard, #59 ruff/format/pyright cleanup +
  wire), gained `record_mics.py` streaming disk writes + verify (#49), an HF card/Space **sync
  workflow** (#34 — secret-gated; operator added `HF_TOKEN`, so it's LIVE and validated end-to-end on
  a real card change), and a **model-card correctness fix** (#63 — Pi/ONNX examples updated to the
  D27 torch-free API, proven runnable; an INT8-parity overclaim corrected: the mel-diff test proves
  FRONT-END equivalence, not full/quantized-model parity). #55 precision analysis posted (the "4 FP"
  headline is unreproducible from committed data; saved matrices show 5–6; the `pi_inference` 0.90 +
  confirmation gate is the likely near-free fix; sweep on request). `coffee-roaster-mcp` gained the
  pre-open logic-churn size-check discipline (#187/#188).

**Unchanged:** the **supervised hardware roast** is still the one high-leverage unlock — it flips on
the #405 post-FC RoR loop (#460) and lights up #323 (ceiling-drop) + #380 (FC-lag, deferred) tuning.
MCP #157 (D27 Phase 2) needs a Pi 5. #396 (advisor A/B) is available on request (paid).

---

**7 Jul 2026 — #342 AMBIENT PROBE SHIPPED, both repos, hardware-validated. The Yoctopuce
Yocto-Meteo-V2-C (temp/humidity/pressure) is now captured per-roast as corpus metadata. On the
operator's re-raised architecture question the integration home moved from agent-direct to
MCP-OWNED (plan D85 revises D60) — the MCP owns the roaster rig's other USB sensor (the FC mic), so
ambient follows the same pattern and the agent stays hardware-free.** Two-repo build, both merged:

- **coffee-roaster-mcp 0.1.12 (PR #186, PUBLISHED to PyPI + MCP Registry)** — reads the Yocto-Meteo
  over USB (new `yoctopuce` dep, lazy-loaded), fail-soft `AmbientSessionRuntime` (absent/erroring
  probe → `unavailable`, never blocks a roast) with a staleness cache (USB read ≤ once per
  `poll_interval_seconds`, default 30 s, never on the 1 Hz state read), exposing the triad as
  `ambient_status` on `get_roast_state` — mirroring the FC-mic pipeline, riding existing state (no
  new tool). Default `mode: disabled` ⇒ zero change for existing configs. 31 tests, qa PASS.
- **roastpilot-agent (#342, PR #461, merged `699883b`)** — `AmbientStatus(MCPMirror)` (byte-for-byte
  the MCP dataclass) + pin bump 0.1.11→0.1.12; `roast_run` schema V9 (3 nullable columns, outside the
  completion-immutability trigger); capture-at-charge via `_persist_ambient_if_charged` (a fail-soft,
  once-latched, restart-latch-restored sibling of `_persist_t0_if_charged` — reads the already-mirrored
  raw MCP state, **no controller/safety surface**); optional fields on RoastDetail/RoastSummary.
  mcp-contract-checker + qa + Claude-review all PASS.

**Hardware-validated end-to-end** against the plugged-in probe through the real production factory
path: **29.7 °C / 41.2 % / 1008.3 hPa**. **To enable on the next roast:** set `ambient.mode: yoctopuce`
in the MCP yaml (see `docs/examples/coffee-roaster-mcp.known-good.yaml`); the final live-roast
confirmation (agent spawns MCP mode=yoctopuce → charge → triad on the `roast_run`) happens naturally
at the next supervised roast — non-blocking, default-off corpus metadata. Two noted non-blocking
follow-ups: `ambient_captured` derives from `temp IS NOT NULL` (a status=ok-with-null-temp is not
constructible per the MCP contract); the plan §2 "13-tool" baseline is stale (14 since 0.1.9). **This
does not change #405 — the enable-flip (#460) still awaits its supervised hardware roast.**

---

**2 Jul 2026 — #405 POST-FC CONTROL REDESIGN: BUILD COMPLETE, behind a flag (default OFF).
All four slices merged; the only remaining step is the ENABLE-FLIP, which is gated on a supervised
hardware roast. Design is plan-repo D82/D83/D84.** (registry synced 7 Jul.) #405 replaces the
advisor-driven post-FC brake (which over-cut in roasts 7/8 → under-temp drops) with a deterministic
post-FC control regime, all gated behind `controller.post_first_crack_control.enabled` (**default
`False` = byte-for-byte today's fully-advisor post-FC — nothing is live**). What merged to `main`:

- **Slice A (#455)** — `RoastStyle` roast-style vocabulary (plain `Enum` LIGHT/MEDIUM/DARK →
  drop_temp_c/dtr_target seeds; optional `roast_style` profile field). Additive, back-compat.
- **Slice B1 (#456)** — the pure PI RoR-target controller (`post_fc_control.py`): EMA-smoothed RoR,
  deadband, conditional-integration anti-windup, bumpless-transfer reset, clamp `[floor≥1, ceiling]`
  (crash-to-0 structurally impossible), deterministic (dt passed in). INERT until B2.
- **Slice B2 (#457)** — wires the loop into the DEVELOPMENT tick: the controller owns heat (PI loop) +
  fan (pinned), the advisor's post-FC levers are gated off when the loop is active (`should_drop`
  still honored), safety box floor/target built FROM the actuated PI output (#412 told==enforced),
  loop state advances only on an actually-executed write, bumpless `reset()` at the true FC edge.
- **Slice C (#458)** — the deterministic drop anchor: fires `drop_beans` when
  `bean_temp ≥ target_drop_temp_c AND system_dev% ≥ target_development_percent` (the SYSTEM
  development %, never the advisor's claimed number), gated behind the same flag+engaged bundle.
  Precedence (D84): the profile's EXPLICIT drop targets are authoritative; `roast_style` is a UI
  seed/fallback, so existing profiles are unchanged. LLM-earlier-only: the advisor `should_drop` +
  #313 coherence path is the earlier window; the anchor caps the drop at target, so the LLM can only
  pull the drop earlier, never delay.

**Remaining (the sole open #405 work): the ENABLE-FLIP** — flip `post_first_crack_control.enabled`
default → `True` and tune the conservative PI gains (kp 3.0 / ki 0.1 / target 8 °C·min⁻¹ / floor 25 /
fan 40) on real thermal response. **Gated on a supervised hardware roast** — a replay/bake-off cannot
validate a closed loop that changes the trajectory (D34/D35a), so hardware is the tuning + sign-off
gate. Tracked as its own issue; #405 stays **In Progress** until the flip lands and validates. Route
the flip PR through safety-reviewer (Opus) + the full roster. A non-blocking follow-up noted on #457:
under a misconfigured `min_seconds_between_commands > control_interval_seconds` the loop retries +
re-persists a REJECT every 1 s (DB-write volume only, not reachable with defaults).

**Process note — the layered review earned its keep:** the independent roster caught a real
control-path bug on 3 of the 4 slices, each the told≠enforced/#412 class, all fixed pre-merge, each
missed by the *other* lenses (B1: safety-reviewer fuzzed a mislabeled `# pragma: no cover`; B2:
safety-reviewer caught a resume-edge phantom seed + false-invariant docstring; B2: **Codex** caught an
actuator-failure phantom advance that safety-reviewer + qa + sim + Claude-review all missed). Run the
same full roster on the enable-flip (the riskiest step). **This does not change other priorities.**

---

**1 Jul 2026 — TIER-1 AUTONOMOUS FIX BATCH COMPLETE. All 7 follow-up issues merged
(#404, #439, #426, #423, #412, #409, #443) across two parallel tracks — config cluster (Track A)
and live-view (Track B) — each in its own git worktree, lead-plus-subagents.** One PR per issue,
domain reviewer + qa run on the branch BEFORE opening (shift-left), codex advisory-but-triaged
post-open. What merged:

- **#404 (PR #444)** — T0 chart marker anchors to the telemetry-recovered charge origin
  (`elapsed − charge_elapsed`), not the detection-fire frame; fixes the roast-7/8 wrong-position bug.
- **#439 (PR #445)** — `/config` `mcp_device` tri-state inherit/override (null = keep hand-authored
  yaml / value = override); fixes boolean null→off collapse, un-clearable overrides, blank-driver
  writing `driver:""`. Codex caught 2 real P2s post-open (override unclearable on device-enumeration
  error; blank-`""`→yaml crash class on `serial_port`/`audio_input_device`, same class on the API surface).
- **#426 (PR #447)** — a saved-file value no longer shadows a top-level JSON-section env var
  (`ROASTPILOT_ADVISOR='{...}'`); PER-FIELD injection so a PARTIAL blob keeps the other saved fields
  (qa caught the whole-section-skip regression pre-open; codex caught uppercase-key + nested-blob P2s).
- **#423 (PR #446)** — `/live` is the single live-roast home (control view → sticky last-roast summary
  on finalise, NOT snap-to-start) and `/` collapses to a pure launcher (revises #403 → plan **D81**).
  Codex caught 4 real finished-view data-flow P2s (health not refetched on completion so the summary
  never triggered; "Roast complete" shown for FAULTED runs; stale in-progress snapshot; stats from
  lossy downsampled telemetry) — all folded to drive the view from the terminal server-outcome record.
- **#412 (PR #448)** — deterministic deadband+slew damping for the #386 adaptive-trim thrash; opt-in,
  OFF by default (byte-for-byte the fixed 65% cut). **KEYSTONE / control-path**: the first cut bounded
  only the RETURNED trim value; codex caught (and the Opus safety-reviewer CONFIRMED on re-examination)
  that the ACTUATED roaster command was NOT bounded — the safety box floor was built from the undamped
  target (clamped the damped value back UP on the heat-up leg, 12pp jump) and state advanced on
  rate-limited/rejected ticks (2×slew between real commands). Reworked to feed the damped depth into the
  box floor+target and advance state only after an accepted write; tests now assert on the actuated MCP
  command stream. qa AND the first safety pass both missed it because the tests asserted the returned
  value — the layered/independent review is what caught it.
- **#409 (PR #449)** — turning-point (post-charge bean-temp minimum) landmark event + chart marker,
  mirroring drying_end (new `RoastEventKind.TURNING_POINT`, store schema V8 with a data-preservation
  test, live + detail markers, FE contract parity same PR). Observability-only, threshold OUT of
  `/config`. Codex caught 3 clock/correctness P2s (marker one tick early — the same class as the #404
  T0 fix, reintroduced; timeline showing the debounce-relative clock; false landmark on first-sample-≥0
  RoR) — all folded (payload-anchored marker, charge-referenced timeline clock, negative-RoR witness gate).
- **#443 (PR #450)** — exposes the #412 damping knobs (deadband/slew) in the `/config` Late-Maillard
  Trim category (revealed when adaptive depth is on); removes the "not in schema" note. Server enforces
  the `deadband < slew` cross-field constraint (422 + hint).

**Process notes:** Codex earned its seat hard — it caught real defects on nearly every PR that qa +
the Opus domain reviewers missed, most notably the #412 actuated-command control bug. Independent
triage held throughout (author never self-adjudicated; the lead + safety-reviewer + codex as separate
lenses). One process slip (a teammate started the #443 dependency early, causing a worktree branch
switch mid-review) was caught and untangled with zero work lost. Shift-left worked: most rework was
folded pre-open; the post-open rework that remained was healthy (real codex catches). **This does not
change roast priorities — #405 (deterministic post-FC heat floor) remains the top control priority.**

---

**30 Jun 2026 — CONFIG UI (#413) IS DONE. The device-selection UI shipped and saved
device/hardware config now applies next-roast; the Config UI epic is complete.** A second
lead-plus-subagents session closed out the remaining PR3 + S4 work (the block below was the
"functionally complete for the agent-settings path" state; device selection + hardware
apply-next-roast were still pending then). Nine PRs merged this session:

- **#433 (#431)** — respawn the MCP child when saved `mcp_device` config changes, so DEVICE/hardware
  config (serial port, driver, audio input, recording, FC detection) applies next-roast via a safe
  **between-roasts** respawn (under `_start_lock`, behind the active-run guard; **never** auto-resumes
  heat/fan). Independent safety-reviewer PASS — but **Codex then caught two real P1s the Opus safety
  review passed**: a stale force-terminate hook that would SIGKILL a recycled pid after a respawn, and
  an unconfirmed-stop being masked. Both fixed + re-verified. Closes #431.
- **#434 / #435 / #436 / #437 (#419 PR3)** — DeviceSelect (single) + `/api/config/devices` wiring,
  DeviceMultiSelect (recording devices), the env-override badge, and the Hardware/Audio/First-Crack
  category panels + mic-test "not available in M1" placeholder. Backend-enumerated dropdowns (never
  free-text), wired to `mcp_device` via PUT. **#419 closed.**
- **#438** — device endpoint emits the audio device **NAME** (not the sounddevice index) as the
  selectable value, so a picked mic saves the name substring the MCP yaml matches (Codex P2 catch).
- **#440 / #441 (#421 S4)** — a11y (arrow-key listbox nav + trigger accessible-name + open-to-list
  focus), group subheadings, category reorder, responsive `<900px` (outer + per-row single-column),
  `valuesEqual` array dirty-guard, help-copy accuracy. **#421 closed.**

**Deferred follow-up filed: #439** — the `mcp_device` tri-state inherit/override semantics
(null = keep the hand-authored MCP yaml / value = override) are not yet modelled in the UI (booleans
collapse null→off; can't clear an override back to inherit; blank `roaster_driver` → `driver:""`).
All non-safety; device selection itself works. Earlier-deferred follow-ups still open: **#426**
(top-level-JSON env shadow), **#423** (sticky `/live` summary), an on-demand mic-test backend sample.

**Process notes (this session):** the layered review kept earning its keep — Codex caught P1 safety
bugs no other lens did, and `qa` caught real test-quality + a11y gaps (a malformed `checkbox`-in-`listbox`
role nesting; smoke-tests-masquerading-as-behaviour). Ran FE + BE tracks in parallel via **explicit
git worktrees** (one per track, per the runbook) after an initial shared-checkout collision was caught
and untangled with no work lost. **This does not change roast priorities — #405 (deterministic post-FC
heat floor) remains the top control priority.**

---

**30 Jun 2026 — CONFIG UI (#413) FUNCTIONALLY COMPLETE for the agent-settings path. The agent now owns one unified
config and renders the MCP yaml from it (D76/D78), and saved settings drive the next roast. Plan
decision D79 (`roastpilot-plan` `fb59c10`).** A single long lead-plus-subagents session built the
operator's settings surface end to end. Seven PRs on `main`:

- **#422** routing shell — reload-safe `/live` + home launcher + `/config` route (also fixed the
  roast-7 reload-loses-the-live-roast bug). **Closes #403.**
- **#424 / #425 / #427 (S1)** — unified config model + env-overrides-file persistence + per-field
  metadata; `GET`/`PUT /api/config` (PUT excludes safety); read-only `/api/config/devices`
  (agent-direct `pyserial` + `sounddevice`). Two Opus-safety BLOCKERS caught + fixed in #424 (a
  `Safety:` casing bypass + a 3-field coverage hole → a wholesale `ROASTPILOT_SAFETY__` prefix skip).
- **#429 (S3)** — MCP-yaml passthrough-merge + `mcp_device` through `/api/config` + apply-on-respawn;
  preserves the pinned model `revision` / `onnx_threads` / profile **by construction**.
- **#430** — reload `load_app_config()` at `start_roast` so AGENT settings (advisor model/prompt,
  pre-FC levers, trim) apply next-roast (the D78 apply-next-roast guarantee).
- **#428 (S2)** — the `/config` view: category rail + per-field validation + save model; Safety read-only.

**Apply-next-roast is SPLIT:** AGENT config applies next-roast now (#430); DEVICE config (serial/audio/FC
→ MCP yaml) still needs an MCP respawn → **DEFERRED to #431.** **Slice status:** S1 (#418) ✅ closed ·
S3 (#420) ✅ closed · reload (#430) ✅ · S2 view (#428) ✅. **REMAINING:** S2 **PR3** (env badges + device
dropdowns + the Hardware/Audio/FC categories now backed by #429 + a mic-test "not available in M1"
placeholder, under **#419**) and **S4** polish (**#421**). Other deferred follow-ups: **#426**
(top-level-JSON env shadow), **#423** (sticky `/live` summary), an on-demand mic-test backend sample
endpoint.

**Governance — Codex earned its review seat (reverses the 15-Jun D37 disable).** Re-enabled 30 Jun, and
on its first real outing it caught a run of bugs the Opus safety-reviewer (PASSed twice), the contract
checker, and the author all missed: a roast-breaking render-source regression (a normal `roast-live.sh`
roast would spawn the MCP on defaults, dropping the Hottop config), a credential-redirection hole, a
cross-request `os.environ` mutation, and the apply-next-roast gap itself. **Operator decision (30 Jun): Codex is ADVISORY-BUT-TRIAGED, NOT a required gate — the
planned `review-gate` flip-on-BOTH-reviews wiring is CANCELLED** (its re-post-on-every-trigger
churn would deadlock conversation-resolution if its inline threads gated merge). Operating rule:
auto-reviews at PR creation, re-trigger with `codex review` only ONCE on the final commit **[SUPERSEDED 27 Jul 2026 by plan D142: Codex auto-reviews at the READY transition, not at creation; this line records the 30 Jun operating rule and is kept as history, see AGENTS.md for the current one]**; the lead
verifies each finding vs current code, folds the real ones, resolves the stale re-posts by hand
(D23 — author never self-triages). AGENTS.md roster updated to match (this PR). Memories:
`agent-config-ui-story`, `claude-review-not-a-required-check`. **This does not change roast priorities —
#405 (deterministic post-FC heat floor) remains the top control priority; the block below is unchanged.**

---

**28 Jun 2026 — ROAST 8 + the c5/c6 control prompts. Roast 8 = the FIRST fully
autonomous LLM-driven drop on this stack that landed a proper roast. c5/c6 prompts merged
(selectable; c3 stays default). The deterministic post-FC heat floor (#405) is now the
ACTIVE PRIORITY (In Progress).** Plan decisions **D72/D73/D74** (`roastpilot-plan`).
What landed on `main` / happened tonight:

- **c5 prompt MERGED (#406; D72).** Post-FC heat-floor control-teaching ("keep the bean
  climbing"). Additive + selectable; **c3 stays the live default**. Refs #396.
- **c6 prompt MERGED (#407; D72).** c5 + an explicit heat-RECOVERY action ("if heat = 0 and
  the bean is below drop temp, RESTORE heat"). Additive + selectable; c3 stays default. Refs #396.
- **roast-live.sh banner MERGED (#408).** The runner now reads out the resolved advisor model
  + prompt, tagged ⚠ EXPERIMENT when non-default. Refs #396.
- **Bake-off (mini+c6 round; D73).** gpt-4o vs **gpt-4.1-mini** × c3/c4/c5/c6 × 3 Colombia +
  Artisan. **gpt-4.1-mini WINS the Artisan heat-fidelity reference** (heat-MAE ~18–22 vs gpt-4o
  ~30–40, ~2× closer; higher drop-F1; ~5× cheaper) — the EXACT metric D43/D69 pinned gpt-4o on.
  c6 recovers the post-FC floor (strong for mini, late for gpt-4o). **Pin NOT overturned** (the
  replay eval scores RECOVERY not PREVENTION; mini was never hardware-run until roast 8).
  Recommendation = add a **mini arm** (mini+c4 / mini+c6 vs gpt-4o+c1) to the #396 A/B. Commented on #396.
- **ROAST 8 (hardware, 28 Jun; D74) = FIRST FULLY AUTONOMOUS LLM-DRIVEN DROP that landed a
  proper roast.** Config = gpt-4.1-mini + c6 (the experiment), pre-FC trim fixed 65 %. The
  controller executed mini's `should_drop` through the safety box (operator did not touch the
  drop) at **bean 193 °C / DTR 21.1 % / RoR 3, t+13:01 from charge** — a solid slightly-developed
  medium for washed Colombia Huila, far better than roast 7's under-temp 188 °C / 16 %.
  **c6 hardware verdict: does NOT prevent the over-brake** (heat still crashed 40→0 one tick
  post-FC) **but DOES recover from it** (~30 s later, 0→30→40, fast enough the bean never
  stalled — roast 7 / gpt-4o+c3 never recovered). So c6 = a real improvement, but the wobble
  persists → **#405 (deterministic post-FC heat floor + LLM-drop-only) is the robust fix and is
  now the priority** (board: In Progress).
- **Filed: #409** — tag the turning point (post-charge bean-temp minimum) as a landmark event +
  chart marker, mirroring drying_end (open; board To Do). (Plan **D74**.)

**Open / next:** **#405** (deterministic post-FC redesign — TOP priority, carries the roast-8 +
c6 evidence), **#396** (the operator-gated prompt A/B — now wants a mini arm; do NOT close),
**#404** (T0 chart marker mis-anchored to the detection-fire point, not the backdated charge —
open; supersedes the #387 line), **#409** (turning-point landmark), **#386** (pre-FC adaptive-trim
thrash — already closed via #402's interim toggle / config-gated depth), **#342** (ambient probe),
**MCP E11-S3** (Pi soak), **#178** / **#179**.

---

**28 Jun 2026 (later) — BAKE-OFF RE-VALIDATION + ADVISOR/CORPUS CLOSEOUT. The post-FC pin HOLDS
(no re-pin), c4 added selectable (c3 stays default), the store corpus + finalists report shipped,
Colombia dev stepped to 16 %. #277 and #224 are CLOSED; the open follow-up is the operator-gated
#396 prompt A/B.** Plan decisions **D69/D70/D71** (`roastpilot-plan` `8b23178`). What landed on `main`
tonight (PRs #395 / #397 / #394):

- **#277 post-FC loop re-validation — DONE; the gpt-4o + c1 pin HOLDS (D69).** A fresh finalists
  bake-off (17 known-good mediums × 3 finalists, scored on the c3 live prompt;
  `docs/advisor/bakeoff-finalists-2026-06-28.md` + `docs/advisor/postfc-validation-2026-06-28.md`, shipped in #395)
  gave gemini-3.1-flash-lite the drop-F1 lead (0.931 vs gpt-4o 0.765 / 0.611 on the 6-roast screen)
  — but that gap is a **c1→c3 PROMPT confound, not a model verdict**: gpt-4o recovered to drop-F1
  0.833 on c1 (both c3 never-drops, artisan-01/-12, returned F1=1.0), and at TRACE level it stated
  the drop conditions (dev at target, bean at drop temp) then BRAKED (heat-0 + fan-up, the c3
  fan-brake) instead of dropping. The D43 pin's deciding axis was heat-magnitude fidelity (gpt-4o
  ≈7.5 pp on c1 vs gemini ≈22 pp), unchanged. **NO re-pin.** **grok-4.3 REMOVED from the finalist
  roster** (6.12 s median FC latency > the 2.5 s gate AND confidence > 1.0 on 3 ticks =
  `AdvisorUnsafeOutputError`); screen-coverage only (`finalist=False`).
- **c4 control prompt MERGED (#397; D70).** c4 = c3 + a brake-vs-drop decisiveness section (the
  fan-brake shapes the APPROACH while behind target; once IN the drop window, `should_drop=TRUE`,
  not more braking) — targeting the exact c3 overshoot D69 found. **Additive + selectable; `c3`
  STAYS the live default** (`AdvisorConfig.prompt_version` default `c3`). The **c1-vs-c3-vs-c4 A/B
  is #396** (operator-gated, full 17-roast set / all three prompts / one session) — **any
  prompt-default change is gated on #396**, no flip ships off this analysis alone.
- **#224 corpus — DONE.** Roasts 3–6 → replay fixtures (two-clock reconciliation + the
  `phase_changed`→cooling drop clock) + a labelled-corpus manifest
  (`docs/advisor/store-roast-corpus-manifest.json`); `store_to_fixture` v6+v7 compat. Shipped in #395.
- **Colombia Huila seed dev 13 → 16 % (#394; D71).** Auto-drop now has room to land ~192–193 °C; the
  195 °C ceiling is UNCHANGED (it caps bitter regardless). The planned de-risk step-up toward the
  bean's ~18 % research DTR, safe after roast 6 validated the full stack.

**Closed by this session:** #277 (post-FC loop re-validation — pin holds), #224 (consolidate roast
logs → labelled replay corpus). **Open follow-up:** **#396** — the operator-gated c1-vs-c3-vs-c4
prompt A/B that gates any prompt-default change. Everything else open before the next roast (roast 7)
is unchanged from the block below (#386 / #342 / MCP E11-S3 / #178 / #179).

---

**28 Jun 2026 — BEFORE-NEXT-ROAST BATCH SHIPPED. The two MCP releases + the agent batch all landed;
#387 re-diagnosed as a NON-BUG (T0 is correct); #134 validated by roast 6 → E11 unblocked. Roast 7
is ready. This supersedes every "P0 = run roast 4" / "0.1.10 bundle still open" framing below.** The
recording-flywheel + accuracy gaps from the roast-5/6 session are now closed on `main`; `coffee-roaster-mcp`
is pinned at **0.1.11** (`pyproject.toml:131`). What shipped tonight:

- **MCP 0.1.10 SHIPPED** (published — PyPI + MCP Registry): **#180** (numpy-vectorised WAV flush — the
  roast-5 recording overflow that starved FC detection, now 0.28 ms / 13× / byte-identical PCM16) +
  **#162** (per-request SDK log flood; fix = the guard reads `getEffectiveLevel() <= WARNING`, so it no
  longer no-ops before the SDK raises the level). Agent pin bumped 0.1.9 → 0.1.10 (#390).
- **MCP 0.1.11 SHIPPED** (published): **#181** (the recorder finalises at session **STOP**, not first
  crack → recordings now span charge→drop; auggie-confirmed the FC training pipeline `chunk_audio.py`
  slides across the FULL recording, so truncating at FC was discarding ~80 % of the **negative-class**
  training data) + **#178** (live in-session mic peak/RMS dBFS readout on the FC-status). Agent pin
  bumped 0.1.10 → 0.1.11 (#391).
- **Agent batch merged (PR #391):**
  - **#387 — RE-DIAGNOSED as NOT a bug; the T0-accuracy question is CLOSED.** The roast-6 store trace
    shows T0 is stamped at the turning point within ~2 s (sampling granularity), NOT ~11 s late. The
    earlier "~11 s late" was a **chart-read artifact** — the 170 °C *rising* crossing was misread as the
    turning point, vs the true 174 °C *peak* ~14 s later. Auggie-confirmed against `coffee-roaster-mcp`
    `session.py:1590-1630`: the MCP backdates `beans_added` to the running-max bean-temp peak;
    `auto_t0_drop_threshold_c` only controls detection latency / the backdate *delta*, NEVER the stamped
    origin. Resolution = a turning-point regression test + lowering the *example* threshold 25→15
    (robustness only; it does not move the origin). **The #174 T0 fix is correct.** (Plan **D66**.)
  - **#385** — store-backed per-origin recording `roast_num` (the counter syncs forward so a fallback
    can't collide).
  - **#388** — capture roasted weight + compute `weight_loss_percent`, an objective D42 corpus label
    alongside the operator rating; the API + FE reject `roasted > charge`. (Plan **D68**.)
- **#181 implements D65's recorder-lifecycle decouple** (recording spans the full roast; recorder
  lifecycle = the SESSION, not FC) — the negative-class-data rationale is recorded as plan **D67**.
- **#134 validated by roast 6** (auto-FC detection + advisor drop + full recording, supervised, clean
  light roast) → **E11 (Pi packaging) is unblocked** on the D28 hardware-roast gate. (E11's other gate,
  the D27 torch-free chain, is independent and unchanged.)

**Remaining OPEN before the next roast (roast 7):**
- **#386** — adaptive pre-FC trim DEPTH from curve / RoR / FC-ETA (keep it deterministic).
- **#342** — ambient probe (Yoctopuce Yocto-Meteo-V2-C, plan D60); agent-startable on probe arrival.
- **MCP E11-S3** — Pi 5 dual-mic + FC-detection CPU soak (the overflow bites harder on the Pi 5: RP1,
  `onnx_threads=2`, int8; #180 necessary but maybe not sufficient — logged in the E11 epic).
- **#178** — live mic peak/RMS levels validated at the next roast (shipped in 0.1.11; confirm on hardware).
- **#179** — pin pyright (unpinned caused 2 CI failures).

**Roast 7 readiness:** full charge→drop recordings (#181) + accurate T0 (#387 closed) + live mic levels
(#178) + roasted-weight/loss-% label (#388) + per-origin roast numbering (#385), all on `main` @ 0.1.11.

---

**27 Jun 2026 (later) — ROASTS 5 + 6 + MCP 0.1.9 + the recording-overflow saga. Roast 6 is the
FIRST FULL-STACK SUCCESS with auto-FC; roast 5 aborted on a recording regression, since fixed. All
agent work stays on `feature/134-roast4-colombia-huila-seed` (NO PR yet — still the roast branch).**
The session built the FC training-data flywheel onto live hardware and shook out the decoupling
invariant (D65) the hard way.

- **MCP 0.1.9 RELEASED** (operator-approved publish; PyPI + MCP Registry): **#175** (per-window FC
  confidence observability — `fc_window` log rows + a `first_crack_detection` summary) + **#176**
  (config-driven roast audio capture: mono + **multi-device "option A" = two INDEPENDENT USB-mic
  streams, NOT sample-locked** — research-confirmed fine for FC training; + a record-check CLI + the
  **14th tool `set_recording_metadata(origin, roast_num)`** + `record_mics`-compatible naming
  `mic{N}-{origin}-roast{N}.wav` + a session JSON).
- **Agent-side 0.1.9 integration DONE** (the local "task #9"), all on `feature/134`, safety-reviewer
  **PASS:** pin bump 0.1.8 → 0.1.9, the `mcp_client` mirror for the 14th tool, and
  `controller.start_run` now derives a deduped `recording_origin_slug` + a per-process `roast_num`
  and calls `set_recording_metadata` **BEFORE `start_roast_session`** (HARD ordering — after, the
  MCP silently falls back to no metadata). Live config gained a `recording` section (both mics @
  16 kHz, autocapture → `~/roasts/captures`).
- **ROAST 5 = ABORTED by a recording regression (coffee-roaster-mcp #180).** The multi-device
  capture's WAV flush packed each 16 k-sample block one-by-one via a `struct.pack` Python loop (GIL
  held ~3.6 ms) **inside the detector's capture-read worker AND the 2nd-mic thread** → stalled the
  detector read → **30 consecutive mic overflows → audio faulted, FC dead** → operator stopped
  pre-FC. **#180 FIXED tonight** (numpy-vectorised flush: 0.28 ms, 13×, byte-identical PCM16) on
  `feature/180-vectorise-recording-flush` (`fe1a9c6`), **editable-installed into the agent .venv**
  (operator's "local MCP release without CI") + **soak-validated** (2.5 min @ onnx_threads=8, both
  mics: max 1 consecutive overflow, no fault). Proper **0.1.10** PR/release still pending.
- **ROAST 6 = FULL-STACK SUCCESS.** Deterministic trim engaged (heat 100 → 65 at ~155 °C),
  **auto-FC detected on a real roast for the FIRST time** (R4 missed, R5 faulted), the advisor drove
  a clean dev%-gated drop (~190 °C / ~13.9 %, under the 195 ceiling, confidence 0.98), both mics
  captured. The lowered USB-PnP gain (operator set it after a clip-headroom concern) did NOT starve
  detection. **#134 flagged validated by roast 6** (recommend close → unblocks E11; operator's gate).
- **Open 0.1.10 bundle + follow-ups filed:** coffee-roaster-mcp **#180** (the flush fix, done,
  awaiting release) + **#181** (recorder lifecycle stops at FC — it is owned by the FC-detection
  pipeline → must capture the FULL roast) + **#162 REOPENED** (per-request log flood: the guard reads
  `getEffectiveLevel()` = inherited default WARNING at startup before the SDK's `.run()` sets INFO, so
  the guard skips; fix = guard on `.level`/NOTSET; **#173 closed as a dup**) + **#178** (live
  mic-levels check at roaster start) + **#179** (pin pyright — unpinned caused 2 CI failures). Agent:
  **#385** (store-backed roast_num), **#386** (adaptive pre-FC trim DEPTH from curve/RoR/FC-ETA — keep
  it deterministic), **#387** (T0 STILL ~11 s late after #174 — backdate not reaching the turning
  point; suspects: `auto_t0_drop_threshold_c=25` too coarse / MCP backdate short / agent delta),
  **#388** (capture roasted weight + compute weight-loss % as an objective D42 corpus label). **E11
  gained E11-S3** (Pi dual-mic + FC CPU soak — the overflow bites harder on the Pi 5: RP1,
  onnx_threads=2, int8; #180 necessary but maybe not sufficient).
- **Stale issues closed:** MCP #175 + #176 (shipped in 0.1.9), #173 (dup of #162).
- **Plan (source of truth):** the recording/training-data-capture architecture is **D65** (revises
  D64) — option A two-independent-mic streams + the best-effort-and-decoupled-from-the-FC-read-loop-
  AND-lifecycle invariant + the 0.1.10 bundle + the weight-loss-% label (#388).
- **Branch state:** `feature/134` now carries the 0.1.9 wiring (pin / mirror / `set_recording_metadata`
  ordering / `recording` config) **on top of** the prior roast-4 seed + #379 timeline fix + the E11-S3
  doc edit, and **still needs PR(s) to `main`** once the roast season pauses.

**27 Jun 2026 (later) — ROAST 4 IN PROGRESS. New bean (Colombia Huila) seeded + de-risked; one FE
bug found+fixed; two follow-ups filed. Work is on `feature/134-roast4-colombia-huila-seed` (NO PR
yet — operator's deliberate mode for the roast session).** Branch = 4 commits off `main`: Colombia
seed → dev 15→18 → dev 18→13 → #379 timeline fix.

- **Roast 4 bean = Colombia Excelso Huila (Washed)** (Redber GRE-COEX-BE250), seeded as the second
  built-in `BeanProfile` (`seed.py`, idempotent at `serve` startup). Charge 170–200, **drop 195**,
  250 g (1 kg / 4 batches), washed, pre-FC levers at config default.
- **Dev guide DE-RISKED to 13 %** for this first roast on the bean (operator). Research (our `.alog`
  corpus + a deep-research pass) put the *eventual* medium at **~18 % DTR** for a washed high-grown
  bean (Costa Rica analog 17.7 %; same-machine Roast Rebels Hottop page 19 %) — but for roast 1 the
  operator guides LIGHT (13 %) to avoid a dark roast, knowing audio FC lags ~30 s so true development
  runs longer than the number. Memory `per-origin-dtr-washed-highgrown`. **Priority: watch the
  AUTO-drop fire** (should land light ~188–193 before the 195 ceiling); **the un-gated manual drop is
  the backstop** (confirmed in code: `controller.py` — the #313 coherence guard gates only the advisor
  drop, never the operator DROP BEANS). Residual risk = the #323 guard-vs-ceiling hold on a fast
  (40 °C-ambient) roast.
- **Preflight done, all green:** Hottop serial present (`cu.usbserial-DN016OJ3`, was just powered off),
  USB-PnP mic present, real driver (not mock), FC audio, advisor **REACHABLE** (gpt-4o, key good —
  the attempt-1 trap avoided), Colombia profile loaded (dev 13 / drop 195).
- **Roast 3 rated 2★ "too dark / bitter"** by the operator (the D42 corpus label) — its detail page
  shows the overshoot live: 5× advisor-drop REJECT while the bean climbed 197→203 °C (the #323
  guard-vs-ceiling conflict). Direct evidence for why roast 4 is de-risked.

**Follow-ups filed this session (all sequenced POST-roast-4):**
- **#379 — detail Timeline milestone times blank — FIXED on-branch (`35fd2ff`, no PR).** Root cause:
  `EventTimeline.tsx` placed milestones via `event.payload.tick`, but controller milestone events
  (first_crack/run_completed/logs_exported) carry no tick — only `monotonic_seconds`. Fix rebases each
  event's monotonic to the T0 event's. FE-only, lint+typecheck+513 unit tests green. Detail Playwright
  baselines need regen at PR time. The Decision-trace `RECOMMENDED/EXECUTED = —/—` and pre-T0 `TIME = —`
  are **working-as-intended** (passive telemetry rows / pre-charge ticks), not bugs.
- **#380 — FC-lag offset → re-pointed to an MCP-config architecture.** Originally an agent-side dev%
  offset; operator's better framing: put it in `coffee-roaster-mcp` (single source of truth for FC
  timing, already onset-backdates via #337) as **optional config** (`first_crack.onset_offset_seconds`),
  MCP reports the corrected FC, the agent computes dev% from it, UI follows — no agent offset, no
  double-correction. Corrected dev% releases the auto-drop earlier ⇒ mitigates #323. **Revises D49/D57**
  (needs a plan D-number) + safety review; tune to roast-4 data first.
- **coffee-roaster-mcp #173 — console-flood regression.** The 1 Hz `CallToolRequest` INFO flood is back
  on 0.1.8: `quiet_sdk_per_request_log()` guards on `getEffectiveLevel() < WARNING` but runs *before*
  logging is configured, so the guard is False and it no-ops; the SDK then sets INFO. **Cosmetic only**
  (no roast/control/safety impact). Fix = unconditional `setLevel` (or post-`.run()`), 0.1.9 + pin bump.

**27 Jun 2026 — STATE SYNC (no code). `main` @ `92cdab3`; the autonomous/no-hardware backlog is
fully cleared and roast 4 (#134) is re-validated as the sole next operator gate.** Two housekeeping
items this session, no roaster behaviour touched:

- **#318 is CLOSED + merged** (the D59 read-out slice #374/#375/#376 landed) — the "Untouched: #318"
  lines in the 24 Jun / 22 Jun blocks below are SUPERSEDED; there is no open #318 work.
- **#342 ambient covariate — sensor decision synced across all surfaces (plan D60).** The operator
  ordered the **Yoctopuce Yocto-Meteo-V2-C USB probe** (in transit, 27 Jun); it supersedes the
  issue's original Home-Assistant-REST plan. Plan `D60` was already committed (`roastpilot-plan`
  `c3bad82`); this session re-synced the **#342 issue title + Integration section**, the
  `operator-decisions-318-342-176` memory, and this registry to match. **#342 becomes agent-startable
  the moment the probe arrives** — a single no-roast build (read the triad over USB at charge, fail
  soft to null, store on `roast_run`, add `yoctopuce` to `pyproject.toml`). NOT on the GitHub project
  board (project 5 tracks only a subset; #134 isn't on it either — board reconciliation is a separate
  operator call).

Everything else remains exactly as the 24 Jun block records: **roast 4 (#134) is the keystone gate**
(validates the #336 trim + c3 prompt + MCP 0.1.8 on hardware; gates E11/E12/#323/#228/#277/M2). The
config that roast 4 must validate is confirmed live on `main`: MCP pin `0.1.8` (`pyproject.toml:115`),
`late_maillard_trim` default 65 %/60 s/155 °C (`config.py` `LateMaillardTrim`, default fields ~L126–138), advisor `prompt_version="c3"`
(`config.py:479`). Operator prerequisite unchanged: a **fresh, non-expiring OpenRouter key** (the
13 Jun attempt-1 failure mode). Open operator action carried over: arm the `review-gate` required
check (#159 / D58).

**24 Jun 2026 — PRE-ROAST-4 RELIABILITY + CLEANUP BATCH COMPLETE (the measured "after-v2"
PR-hygiene sample). The next gate remains the operator running roast 4.** Four issues closed as
lead/PM with engineer/reviewer teammates; every PR through `pr-preflight` (gates + self-critique +
the pre-open domain reviewer on the branch) and independent D23 triage (author never self-triages):

- **#212 — Ctrl-C hangs the server on a live roast — FIXED (PR #365, `f6c0c52`).** `MCPServerProcess.stop()`
  was an unbounded `_stack.aclose()` that could hang forever on a wedged MCP child (the thing that
  bit roast 3). Now bounded by `MCPConfig.stop_timeout_seconds` (10 s) via `asyncio.timeout`; on
  timeout it force-terminates the child process group (`os.killpg(SIGKILL)`; SDK spawns with
  `start_new_session=True` so pgid==pid) and sets a `stop_unconfirmed` seam. Teardown order
  unchanged — heat-off (`safe_shutdown_heat_off`, already bounded) still lands BEFORE `mcp.stop`.
  safety-reviewer PASS; a folded low turned out to be a real test-not-collected coverage gap.
- **#177 — harden the wedged-child shutdown — FIXED (PR #367, `2fc1d91`).** Single retry of the
  heat-off write on timeout before giving up (the first attempt's `CancelledError` propagates out
  before the FAULTED transition, so the retry hits a still-hot run); persist a 'stop unconfirmed'
  marker for BOTH signals — heat-off-unconfirmed and the force-killed child (`COMMAND_FAILED` reuse →
  no FE-contract change), making #212's flag *read* not dead code; multi-start reset of
  `_stop_unconfirmed` in `start()`. Markers are diagnosis-only — restart still enters
  `operator_recovery_required`. safety-reviewer PASS.
- **#159 — auto-merge-before-`claude-review` race — FIXED + plan D58 (PR #366, `12d14e1`).** A
  skip-aware `review-gate` commit-status workflow (pending on open → success-only/fail-closed when
  Claude Code Review completes; Dependabot + workflow-editing PRs auto-pass at stamp time).
  **Validated end-to-end on three real PRs this batch** (#366/#367/#368). **OPERATOR ACTION
  OUTSTANDING:** mark `review-gate` a REQUIRED status check on `main` to arm it (build gate + operator
  activates; the mechanism is inert until required). See plan D58.
- **#306 — per-tick MCP console flood — CLOSED via the MCP release lockstep.** Root cause is
  child-side (the MCP SDK's `mcp.server.lowlevel.server` per-request INFO, piped as raw stderr — the
  agent can't filter another process's logger), so NO agent code change. Fixed in
  **coffee-roaster-mcp#162 → v0.1.8** (raise that logger to WARNING only when more verbose, never
  lowering a stricter user setting — observability-only), RELEASED to PyPI + the MCP Registry
  (operator-approved publish); agent pin bumped 0.1.7 → 0.1.8 (PR #368, `4a4e03f`), mcp-contract-checker
  PASS (13-tool surface / 11 model mirrors / the #337 backdating fields all unchanged). Filed **#369**
  (non-blocking: the round-trip test skip-gate keys off the homebrew binary, not the venv-resolved one).

**PR-hygiene after-v2 sample (`--since 364`, n=3 logic PRs #365/#366/#367):** churn median **389**
(>800 **0%**), **avoidable churn = 0** (no rebase/CI-retrigger/flake/lint commits), **preventable
rework = 0%** — the 2 rework commits both HEALTHY (real Augment correctness catches the pre-open
review missed: the #365 force-terminate-hook re-raise + the #366 fail-closed flip). Lead 0.3 h
open→merge. v2 targets hit on this small sample; n=3 is not a trend (re-measure at n≥15). Honest
caveat: an independent post-open lens (Augment) still earned its keep twice; shift-left's biggest wins
are metric-blind (#306 resolved with zero agent code; the folded coverage gap). Memory
`pr-flow-improvement-experiment`.

**Still open / next:** **operator activates the `review-gate` required check (#159 / D58)**; everything
else is OPERATOR-GATED — **roast 4 (#134)** + device SSE (#135), then the post-roast-4 sequence (#323
ceiling-override → #228 LAST → M2/D42). Untouched: #318. Nothing else agent-startable until roast 4.

**#300 — roast-data pipeline (D44) — DONE (store → labelled replay fixture).** The store-side
sibling of `alog_to_fixture.py`: `scripts/store_to_fixture.py` reads a completed roast out of the
agent SQLite store (read-only) and emits the same `roast.jsonl` + `summary.json` the bake-off scores,
mapping store event kinds → the three fixture kinds (`t0_detected`→`beans_added` /
`first_crack`→`first_crack_detected` / `run_completed`→`beans_dropped`). Adds the #300/D42 outcome
label on `summary.json` — `operator_rating` / `operator_notes` (from `roast_runs`) + a `degree`
(`core_medium` ≤195 / `soft_medium` (195,197] / `over` >197) from the shared `scripts/roast_degree.py`
helper, which `alog_to_fixture.py` now emits too (parity). Privacy invariant held: real stores are
never committed; the unit test builds a synthetic store via the store write API and asserts the
output parses via `bakeoff_replay.load_roast` with the label fields present. Registering a real
fixture into the bake-off set stays a LOCAL operator action (gitignored). Extends #224 (consolidation
half); feeds D42. Real roast-2/3 ingestion is a manual operator validation, not a committed test.

---

**23 Jun 2026 (later) — MCP v0.1.7 BACKDATING LOCKSTEP COMPLETE. The only remaining gate is the
operator running roast 4.** `coffee-roaster-mcp` **v0.1.7 released** (operator-approved publish): #169
(auto-T0 → turning point) + #170 (FC → crack onset) backdating, on PyPI + the MCP Registry.
The agent-side consumer **#337 merged (#362, `7f70fd1`; plan D57)**: bumps the pin 0.1.6 → 0.1.7 and
honours the backdated timestamps by subtracting the MCP-domain backdating *delta* from the agent's
receive clock (never the non-comparable absolute cross-process value), failing closed to receive-tick
when fields are absent/invalid. Independent D23 review caught that the author's "display-only"
self-assessment was wrong: the backdated clocks feed `_development_percent`, which the #313
drop-coherence guard reads → the advisor drop releases ~1-2 pp earlier on a truer dev% (fails safe;
the #327 trim's FC-ETA is charge-clock-independent, so the trim is unaffected). **OPERATOR-CONFIRMED
decision:** accept the ~1-2 pp-earlier release with `drop_dev_margin_percent` UNCHANGED at the 3 pp
default, and **RE-CHECK where the advisor drop releases on roast 4**, tuning the margin only if needed
(memory `roast4-drop-release-watch-337`). Reviews: safety not-blocking, mcp-contract-checker PASS,
claude-review no-blockers. This supersedes the "open/gated" framings for #169/#170/#337 below.

**23 Jun 2026 — ROAST-4 PREP CYCLE COMPLETE (first run under the new PR-hygiene discipline,
#356). Before-roast-4 reliability/readability shipped; one cleanup closed as superseded; the
DRYING_END landmark added end-to-end. The next gate is the operator running roast 4.** Worked the
post-roast-3 follow-up order as lead/PM with engineer teammates; every PR through `pr-preflight`
(gates + self-critique + domain reviewer on the branch) and independent triage (D23, the author
never self-triages):

- **#339 SSE Last-Event-ID resume — MERGED (#357, `b1155fd`).** Broadcaster ring buffer +
  replay-on-subscribe + endpoint Last-Event-ID header/query + a consumer-layer drain id-guard. The
  live SPA no longer drops fault/recovery/CLAMP events on reconnect. Memory `sse-resume-needs-consumer-id-guard`.
- **#344 centred Savitzky–Golay smoothing — MERGED (#358, `cc690e7`).** SG on bean + RoR,
  display-only, centred (no net lag), live-tail-to-raw fallback, fitted centre clamped to the
  window's raw range. Window pinned **21 s**, validated vs a roast-3 replay.
- **#346 freeze `_roast_elapsed_seconds` server-side — CLOSED as superseded by #330 (plan D56).**
  `elapsed_seconds` is one multi-purpose field (curve x-axis + persistence throttle + readout); a
  blanket server freeze regresses the cooling curve and stops cooling rows persisting (caught
  18→14 points pre-push). The readout-freeze belongs at the presentation layer, already shipped by
  #330. No code change. (Engineer caught the regression before opening a PR.)
- **#351 server DRYING_END signal + dry-end chart marker — COMPLETE (BE #359 `1d40733`, FE #360
  `ac0fcb3`).** Bean-temp threshold approach, validated against the operator's 47 .alog roasts (all
  47 carry a computed Dry-End temp, ~150 °C, median 149 / mean 150.5 / σ 4.9), pinned **150.0 °C**.
  BE: turning-point-gated first-cross latch pre-FC, `drying_end` SSE event + persisted timeline,
  deliberately NOT a RoastMilestone so it never reaches the advisor (observability-only). FE: the
  `dry_end` marker on both the live dashboard and the reload/detail path (placed from the server's
  own threshold cross against persisted telemetry). Independent qa caught a test-oracle coincidence
  + two coverage gaps before merge. Memories `artisan-roast-logs-dataset` (the DRY_BT validation),
  `event-kind-be-fe-contract-parity` (a backend event-kind change reds the FE contract gate).

**PR-hygiene "after" sample (this cycle = the measured after for the #356 experiment):** 3 logic
PRs, churn median 735→504, PRs >800 → 0 %, but >400 and the ~42 % review-fix share stayed flat —
the win is size *composition* + zero avoidable churn, plus the metric-blind catch (#346 closed
before any PR opened). Written up in blog post 17 + the `pr-flow-improvement-experiment` memory.

**Still open / next:** everything remaining is OPERATOR-GATED — roast 4 (#134) + device SSE (#135);
the `coffee-roaster-mcp` #169/#170 v0.1.7 backdating release (CI-green/review-clean, MERGEABLE) with
the agent **#337** (honour the backdated T0/FC timestamp) + the MCP pin bump **in lockstep**; then
the post-roast-4 sequence (#323 ceiling-override → #228 LAST → M2/D42). Untouched: #318. Nothing
else agent-startable until roast 4.

---

**22 Jun 2026 — POST-ROAST-3 BACKLOG CLEARED (15 PRs merged; agent-team session). The
P0 deterministic trim, the full control+safety serial, the chart sub-track, and the
independent FE are all DONE. Roast 4 (supervised validation of the trim + c3) is the
next operator gate.** A large autonomous agent-team session cleared the operator's
pre-triaged post-roast-3 backlog. State recorded:

- **P0 deterministic anticipatory heat trim SHIPPED + ENABLED (#336/#327).** Late-Maillard
  → FC trim, **65 % / 60 s / 155 °C** defaults (operator-approved), hysteresis latch, fails
  closed to the flat deterministic floor; a NaN-latch bug was caught pre-merge.
  **safety-reviewer PASS ×4.** Shipped **enabled** for roast-4 supervised validation — this
  is the fix for the roast-3 overshoot (D46). #327 is now CLOSED.
- **Control + safety serial — DONE:** #343/#210 (DROP BEANS allowed from faulted — dump beans
  off a hot drum), #348/#332 (acknowledge_fault latency — FAULT-latched wedged-child re-read
  no longer starves the action queue), #350/#331 (auto-finalize a stale prior-session faulted
  run on restart, no longer strands boot; safety-reviewer endorsed #331 auto-finalize-over-surface),
  #349/#328 (**c3 control prompt** — fan as an active post-FC brake; **c3 is now the live
  default for roast 4**, advisory-only).
- **Chart sub-track — DONE:** #334/#326 (chart x-axis + cursor = charge-referenced ROAST TIME,
  preheat negative), #341/#307 (live-chart UX: temp auto-range + hysteresis so no clip/collapse,
  RoR fixed, heat/fan as a subordinate overlay, charge-band in frame), #352/#309 (chart event
  markers — charge/T0, FC, drop, cooling; all server-sourced), #353/#325 (advisory-history rows
  show roast-time + bean-temp).
- **Independent FE — DONE:** #335/#324 (home/menu landing hub + persistent nav + state-aware `/`),
  #347/#315 (bean-profile product/source URL + hardened validation), #345/#330 (preheat clock
  freezes on a terminal/fault phase), #354/#329 (restored/reloaded fault renders ACKNOWLEDGE from
  the hydrated snapshot).
- **e2e reliability — DONE:** #340/#338 (lossless e2e settle barrier + idempotent replay stepping —
  the flaky-gate fix that unblocked the merge pipeline).

**Operator decisions captured this session:** trim defaults 65 %/60 s/155 °C shipped ENABLED for
roast-4 supervised validation (operator-approved); **c3 prompt is the live default** (operator
flagged); **centred Savitzky-Golay chosen for chart smoothing** (#344 — approved, not yet built);
#331 auto-finalize-over-surface (safety-reviewer endorsed). Two operator-facing semantics notes,
both safety-PASS'd: (a) **#348** — acknowledging a fault while the bean is still above the hard
ceiling FINALISES that tick rather than auto-e-stopping (heat is already off; e-stop stays
reachable); (b) **#350/#329** — a hot fault surviving a restart no longer auto-offers in-app
cooling for that run (a future "cool from idle / history" button is the enhancement). **Plan-repo
D-numbers still want the operator's plan commit:** the #327 trim (plan §3 / §8.4) and the c3 default.

**Follow-ups filed this session (open, NOT in the original backlog):**
- **#344** — chart smoothing (centred Savitzky-Golay; approved, build pending).
- **#346** — server-side `_roast_elapsed_seconds` freeze on terminal phases (makes the #330 client
  hold redundant).
- **#351** — server DRYING_END signal → then the chart dry-end marker (deferred from #309).
- **#337** — honours MCP v0.1.7 backdated T0/FC (IN PR #362). NOT display-only: the backdated
  clocks raise `_development_percent` (~+1.8 pp), which the #313 drop-coherence guard reads →
  advisor-drop-release ~1-2 pp earlier (truer dev%, fails safe — a guard REJECT is still a held
  drop, no roaster write). **MERGED #362 (D57); OPERATOR-CONFIRMED decision:** keep
  `drop_dev_margin_percent` unchanged at the 3 pp default, RE-CHECK on roast 4 and tune only if
  needed. Mechanism: the controller subtracts the MCP-domain backdating *delta* (`confirmed_at −
  onset`, surfaced on `RoastTelemetry`) from its own receive-tick clock — never the cross-process
  absolute MCP `monotonic_seconds`; manual marks / pre-0.1.7 payloads / negative deltas fall back to
  receive-tick (future-rejection holds).
- Unfiled control idea: the deterministic floor should step fan up toward ~50 at FC.

**Operator-gated / untouched (leave as-is):** coffee-roaster-mcp **#169/#170** (T0/FC backdating —
CI-green + review-clean, awaiting the operator's gated merge + v0.1.7 release); **#323** (drop-ceiling
override — the guard-vs-ceiling conflict from roast 3); **#134/#135** (supervised hardware roast + device
SSE); **#228** (pre-FC anticipatory LLM advisory layer — deferred LAST, D50).

**#318 RESOLVED as option C + read-out UI (24 Jun, D59):** pre-FC heat/fan are now per-bean
deterministic values the controller owns off the bean profile (BE slice PR #374/#375, merged). FE
slice (this PR) makes the dashboard control row render pre-FC heat/fan as READ-OUTS, not dials
(server-phase-gated) so nothing silently reverts. Closes #318.

**Next gate:** roast 4 — supervised validation of the deterministic trim (#336, enabled) + the c3
prompt (live default), targeting a clean ≤195 °C drop. Then the operator's call on marking #134 Done.

---

**21 Jun 2026 — FIRST CLEAN END-TO-END HARDWARE ROAST (roast 3). #134 substantively
cleared on the harness; the deterministic control floor is hardware-validated; the
anticipatory trim is now the P1 control priority.** Roast 3 (Ethiopia Yirgacheffe Koke
natural, ceiling 195 °C) completed: charged, roasted, dropped, cooled, **NO end-of-roast
segfault** (the thing that aborted roasts 1 & 2). Beans over-developed (drop 203 °C);
taste pending. Plan decisions **D46–D50** (plan repo, commit 7e45402). What this session
recorded:

- **Deterministic floor VALIDATED but OVERSHOOTS (D46).** The D35 floor (heat 100 / fan 30
  → FC) is safe and works, but **#222 shipped it FLAT** — the plan's §3 phase table specs a
  late-Maillard anticipatory heat **trim** that was never built (plan ↔ impl drift). Roast 3:
  flat 100 → late FC → env 239 °C → at FC the post-FC loop (c2) cut heat correctly (10→0 by
  bean ~193) but drum/bean momentum **coasted the bean 193 → 203 even at heat 0**; the #313
  drop-coherence guard then held the drop for dev% (5.9 % < 10 % floor) until bean 203 — **8 °C
  over the 195 ceiling** (the guard-vs-ceiling conflict, #323). **→ the deterministic
  anticipatory heat trim (#327) is the P1 control priority, AHEAD of #228.**
- **Merged this session:** #319 (advisory trail cap + always-build), #320 (fault-latch +
  escalation — tamed the roast-2 fault-event tick-spam), #321 (c2 prompt + Koke drop 195),
  #322 (pin `coffee-roaster-mcp==0.1.6` + in-venv spawn, closes the homebrew trap).
- **coffee-roaster-mcp v0.1.6 shipped (D47):** the audio-teardown UAF (#165) was the
  roast-1/2 end-of-roast SIGSEGV (NOT a pydantic ABI mismatch); + drum (#163) + mic (#160).
  Roast 3 completing clean validates #165 on hardware. The agent's fail-closed caught the
  segfault on both prior roasts — invariant validated on hardware.
- **Chart/UX decided (D48):** #307 (temp axis auto-range/no-clip; RoR axis fixed/may-clip;
  heat/fan as a 0–100 % step-line overlay; smoothing still open), #326 (chart x = roast-time
  charge-referenced, preheat negative), #324 (home/menu landing page), #325 (advisory-history
  rows show roast-time + bean-temp). **A local chart-regression fix (#316 blank-preheat-chart)
  is uncommitted in `web/src/pages/dashboard/useDashboardEvents.ts` — needs its own engineer
  PR (9 guard tests must flip to serve-referenced); NOT part of this docs sync.**
- **Detection backdating (D49):** T0 + FC should backdate to onset, not confirmation —
  coffee-roaster-mcp #167 (T0) + #168 (FC sibling, filed this session).
- **#228 sequenced LAST (D50):** the pre-FC anticipatory LLM advisory layer ships only after
  the deterministic floor + trim are hardware-validated; it refines the trim and fails closed
  to it.

**Issues filed this session:** agent **#327** (P1 deterministic anticipatory trim), **#329**
(restored/reloaded fault doesn't render ACKNOWLEDGE), **#330** (preheat clock keeps ticking
after e-stop), **#331** (prior-session faulted run restored as ACTIVE, blocks UI), **#332**
(investigate: acknowledge_fault slow while latched — #320 starving the action queue?); mcp
**#168** (FC backdating). Earlier today: agent **#323/#324/#325/#326** + #307 decision
comments; mcp **#167**.

**Next-roast priority order (operator):** **P1 #327 deterministic anticipatory trim** (roast
quality) + **#324 home page** (FE/BE parallelizable); then the P2 UI bugs (#329/#330/#331/#332
+ the #316 chart-fix PR first); **#228 LAST** (post-trim, hardware-validated).

---

**15–16 Jun 2026 — two batches of autonomous agent-execution work (dev orchestration,
NOT roast autonomy — the advisor stays advisory-only and never controls hardware) +
safety-operability + observability merged to `main`; D35 keystone still the critical
path.** The latest session-cluster cleared a large pre-roast backlog without starting the
D35 build. Honest state:

- **Observability slice — DONE** (was D35a-listed "deferred", now shipped ahead of the
  roast): live curve fixed Y-axis (#217 → #232), charge-referenced DTR/drop clock
  (#219 → #234), live development time + DTR on the dashboard (#220 → #238), display-only
  RoR smoothing (#205 → #243). The post-FC loop's DTR inputs and the operator's post-FC
  readouts are now in place before #223.
- **Safety-operability / DTR-correctness — DONE:** charge clock (T0) survives restart
  (#235 → #259), development time / DTR freeze at the drop (#239 → #261), `acknowledge_fault`
  FE-gating + audit-only guard (#117 → #264; substance shipped earlier in #206).
- **E10 follow-up housekeeping — DONE** (all merged; see `docs/epics/E10-spa.md` "E10
  follow-ups — closed"): #102 (pin pyright → #231), #104 (recovery-lifespan test → #233),
  #121 (#236) + #237 (#255) contract-drift gate, #111 (FC-time on history → #240), #184
  (advisor stats on summary, kills the history N+1 → #245), #244 (#247) + #105 (#249)
  refactors, #133 (#250) + #126 (#256) + #241 (#252) + #253 (#258) test-hardening, #155
  (#254) curve hydration, #242 (#248) name-keyed rows, #103 (#251) replay-harness
  hardening, #257 Dependabot. **#112 closed** (GAP A shipped via #220; GAP B = live
  FC-detector health is M2/MCP scope, out of E10).
- **Governance / CI — DONE:** PR review roster = Claude Code Review (+ human reviewers);
  the Augment Code trial ended 28 Jun 2026, and Codex + CodeRabbit were disabled (#246,
  **plan D37**) — a conversation-resolution gate + a re-review-on-every-push bot was a
  non-terminating merge loop. CI Playwright
  image mirrored to GHCR off MCR's anonymous-pull hot path; `mirror-playwright.yml`
  self-refreshes weekly + on bump (#262/#263, **plan D38**).

**Remaining backlog:**
- **D35 keystone (epic #221) — pre-FC floor + post-FC loop SHIPPED + hardware-validated
  (roast 3, 21 Jun):** **#222** pre-FC deterministic policy (DONE, but shipped FLAT — the
  §3 anticipatory trim is missing, see below) → **#223/#226/#276** post-FC LLM control loop
  (DONE; c2 cut heat correctly at FC) → **#224/#277** replay harness + corpus + LLM eval
  (#277 ran, model PINNED gpt-4o + c1, D43; #224 consolidation open). **The roast-3 overshoot
  was a pre-FC floor problem, not a post-FC loop problem.**
- **P0 control priority: #327 deterministic anticipatory heat trim — DONE (22 Jun, #336,
  enabled, safety-reviewer PASS ×4).** Fixes the roast-3 overshoot; awaiting roast-4
  validation. See the 22 Jun block at the top for the full session.
- **D36/D50 (post-trim, LAST):** #228 pre-FC anticipatory LLM advisory layer (refines the
  #327 trim, fails closed to it); #229 curve-feature spike (DONE).
- **Advisor behavior:** #218 (fan/heat over-adjust — the attempt-3 evidence for D35).
  Post-FC fan-as-brake addressed by the c3 prompt (#328/#349, DONE 22 Jun).
- **Safety-operability follow-ups:** #210 (DROP from faulted — **DONE 22 Jun, #343**),
  #212 (Ctrl+C hangs on a live roast), #177 (wedged-MCP-child shutdown timeout), #176
  (cooling on graceful shutdown). #159 (auto-merge-vs-review governance race; interim:
  don't `--auto` substantive PRs).
- **22 Jun follow-ups (open):** #344 (Savitzky-Golay chart smoothing, approved), #346
  (server-side roast-elapsed freeze), #351 (server DRYING_END + chart marker), #337
  (agent honors MCP backdated T0/FC timestamp, gated on the MCP release).
- **Operator-gated:** #323 (drop-ceiling override), mcp #169/#170 (T0/FC backdating,
  awaiting operator merge + v0.1.7). (#318 resolved as option C + read-out UI, D59 — BE
  #374/#375 merged, FE read-out slice in PR; Closes #318.)
- **E11 (packaging, BLOCKED — D28 + D27):** #136/#137/#138, gated on #134 (supervised
  hardware roast) + the torch-free chain.
- **E12 (validation/demo):** #139/#140/#141, #134.

E1–E6 are complete: safety policy, deterministic controller, typed MCP
client, and SQLite persistence (schema v2 with trigger-enforced
completed-run immutability, typed write paths with per-tick commits,
recovery reads proven across the E4/E6 seam).

**E8 (Advisor) is complete** — all four stories: `FakeAdvisor` (#53), the
provider-agnostic `PydanticAIAdvisor` (#54, D18), the change-based
call-frequency policy (#55), and the operator-judged bake-off (#57, D20).
The bake-off set the default to `anthropic/claude-opus-4.8` via OpenRouter
with the electric-roaster prompt `v1`, resolving plan §11 item 1 — the last
M1 open item. Captured in `docs/advisor-bakeoff-2026-06-08.md` (reproducible
via `scripts/advisor_bakeoff.py`); default is config-swappable (D18).

**Advisor re-evaluated on real roasts (14–15 Jun).** A 28-roast bake-off built
from the operator's Artisan `.alog` history **re-pinned the model to
`google/gemini-3.1-flash-lite` (D33, merged #195)** — the only model that
reliably calls the drop; opus + frontier/slow models over-hold. A prompt sweep
(#194) found **`v4`** (profile-anchored drop) closes a recall gap in `v2`
(recall 0.68→1.0); validated on 19 held-out roasts (generalizes 19/19) + a
target-sensitivity addendum. **`v4` re-pinned (D34, operator-approved 15 Jun)** on
branch `feature/194-advisor-prompt-v4-v8` — PR open. Full record + tables:
`docs/advisor/experiment.md`. Pre-roast deps remaining before #134: #189
(phase-resolved slug), #191 (D32 cadence), allowed_bots; mic-check shipped
(`coffee-roaster-mcp==0.1.4`).

**E7 (API + SSE) is complete** — #67 (REST routes), #68 (operator action
queue), #69 (SSE stream) all merged; epic tracking #70 closed. The full
REST + SSE surface the SPA renders from: one backend authority, the SPA
never calls MCP. `RoastService` in `api.py` is the backend authority and
the E9 wiring seam (store + active-run guard + operator queue +
`EventBroadcaster`).

**M1 stories are all closed through E8.** **E9 (vertical slice) is now
active** — the active epic. It is a deliberate single-session, sequential,
same-file (`controller.py`/`api.py`) safety-critical integration; parallel
implementation is the documented anti-pattern. E10 (SPA) follows E9 and
*is* the positive fan-out case.

**E7's SSE contract is ready.** `models.SseEventType` / `SseEvent` /
`TelemetryEventData` + `api.EventBroadcaster` are the typed event surface
E9 (vertical slice) and E10 (SPA) render from.

**E9 wires the live controller tick loop + MCP child into `RoastService`**
(drain the operator queue through full safety; emit events + per-tick
telemetry into the broadcaster) and adds controller handlers for the four
operator actions without one today (`mark_beans_added`, `start_cooling`,
`pause_advisory`, `resume_advisory` — see `docs/epics/E07-api.md` E9 notes,
D19). E9 green in CI realizes D17 criterion (1). The E10 UI kickoff brief is
waiting in the plan repo.

**E9 is complete** — both stories merged. **E9-S1 (#80):** the E7 handoff
wiring (`RoastRunner` live loop, 4 new D19 handlers, `RoasterControlAdapter`,
queue bound + 410 guard, restart recovery via the app lifespan) and the green
12-step mock slice (`tests/test_milestone1.py`). **E9-S2 (#81):** the same flow
against the real `coffee-roaster-mcp==0.1.3` spawned as a stdio subprocess in
mock-driver mode (`tests/test_milestone1_real_mcp.py`); added the dev dep +
`libportaudio2` to CI and wired `MCPConfig.env` forwarding. Traces in
`docs/e9-decision-trace-2026-06-09.md` (fake) and
`docs/e9-decision-trace-real-mcp-2026-06-09.md` (real). **E9 green in CI
realizes D17 criterion (1).**

**E9 is complete (criterion (1) realized).** Both stories merged; epic #82
closed.

**E10 (SPA) — fan-out complete; S6 deterministic close + curve fast-follow DONE
(ui-reviewer/Safari deferred).** The deliberate positive fan-out case (the contrast to E9's
single-session anti-pattern), delivered as a **supervised agent team** (D23):
foundation-first, then one teammate per page. **S1–S5 all merged to `main`** —
S1 replay harness (#101), S2 foundation (#100), the E7 `enabled_actions` contract
(#107, D25) + its S2 foundation follow-up (#115: a `phase_changed` field drift a
page *consumer* caught that two review passes had missed), and the genuine 3-way
page fan-out S3 dashboard (#113) / S4 history (#114) / S5 detail (#116), built in
parallel in isolated git worktrees after the `isolation:worktree` flag silently
no-op'd (runbook: `docs/agent-team-worktrees.md`). **D17 criterion (2) met** —
the dashboard is usable for a live roast.

**S6 (tests + SSE behavior) — DONE (five PRs merged).** The post-fan-out close-out
preceded it (status sync #118/#119, the `product-pm` audit PASS, the revert audit).
S6's PRs on `main`:
1. **PR2 #120** — contract-fixture drift guard: pins the SPA's hand-mirrored types
   (both `lib/types.ts` and `pages/dashboard/events.ts`) against REAL server frames
   so the `phase_changed`-class drift can't recur; must-fail-on-rename proven.
2. **PR3 #123** — `useRoastStream` frame-loss fix (#122): the single-slot
   `lastEvent` dropped page-local frames under a burst, so the dashboard could lose
   the fault/recovery/**CLAMP** frame. Now a non-lossy append buffer + cursor drain.
3. **PR1 #125** — the **D26 canvas un-mask matrix**: the uPlot chart is now IN the
   snapshots (dashboard-live/fault/recovery, detail, history, foundation), the
   determinism kit, CI-Docker-only baselines + the `web-snapshots-update.yml`
   producer; the fault baseline shows the real `SafetyPolicy` reason.
4. **#130** — replay `--step` stepped-elapsed = sim-time (#128): a `_SimClock` off
   the recorded frame timestamps, so a developed-state curve spreads across the real
   roast duration instead of collapsing onto one x.
5. **#132** — the **`dashboard-developed` curve snapshot** (#131) + the `LiveCurve`
   scale-collapse fix: the chart built every uPlot scale unranged from the empty
   mount, so the curve never drew for ANY consumer (the canvas-mask hid it); a
   `self.data` range callback (charge band folded into °C) + a scale-covers-data
   guard fix it. ALL baselines regenerated — every curve now renders.

**The fan-out's signature result: FOUR real foundation bugs surfaced by
consumer-side work that review had missed — #115 (contract drift), #122 (frame
loss), #128 (replay stepped-elapsed curve collapse), #131 (LiveCurve scales never
ranged → curve never drew).**

**Deferred (recorded, not dropped):**
- **`ui-reviewer` MCP pass** (API-fragile) + **Safari/iPad SSE** (§11.4,
  real-device) — genuinely deferred.

Open follow-ups: #124 (active_run_id→null fault teardown), #126 (detail
CLAMP-highlight above viewport), #121 (contract continuous-auto-catch), #133 (S6
review defers: ror scale in hook, scale-covers asserts on live/fault),
#103/#104/#105/#111/#112/#117/#102. Kickoff brief:
`roastpilot-plan/roastpilot-agent/e10-ui-kickoff.md`.

**Advisor decision trace, surfaced (#167 + #170).** #167 (merged) persists
`advisor_decisions` on both controller paths + exposes them on
`GET /api/roasts/{id}/timeline` (`TimelineAdvisorDecision`). #170 (SPA) renders that
trace durably: the **roast detail** page gains an advisor-spined **decision
timeline** (one row per consult incl. preheat + failures — provider_error/timeout/
malformed show the failure, never a blank panel — with provider/model, latency, the
recommended heat/fan + rationale on `ok`, and the linked safety verdict joined by
tick via the shared `VerdictBadge`), plus a summary-chip header; the **history** list
gains a per-roast advisor summary column (consults / clamped / rejected / failed),
derived client-side from each row's cached `/timeline` (the summary contract carries
no advisor stats; backend untouched). New Playwright state
`roast-detail-advisor-failed`. SPA renders from server data only.

**E11 (Packaging) is next in epic order but BLOCKED — do not start (D28 + D27).**
Two independent gates must clear first:
1. **Operator manual tests (D28)** — human-owned (@syamaner): **#135** (E10-S6 manual
   Safari/iPadOS SSE on real devices) is **✅ DONE/CLOSED** (validated on iPad + iPhone
   Safari, latest iPadOS/iOS — connects/streams + reconnect + the critical GET-only
   check); **#134** (E12-S1 supervised hardware roast through the agent harness, D17
   criterion 3) is the **sole remaining gate** (operator running it 13 Jun). *Both*
   must be Done before E11 implementation begins. Rationale: prove the harness on real
   hardware + devices before packaging/distributing it.
2. **Torch-free chain (D27)** — Phase 1 **`coffee-first-crack-detection#54`** (FC-repo
   librosa filterbank + accuracy gate ≥96.86 % acc / 96.9 % precision on the 191-sample
   v2 set) → Phase 2 **`coffee-roaster-mcp#157`** (torch-free MCP release); E11's `[pi]`
   extra pins `coffee-roaster-mcp#157`. (Both are CROSS-REPO — NOT this repo's #54 [E8-S2
   advisor] or #157 [the hardware-readout PR].)
E11 stories #136/#137/#138 are staged (Todo). Contract-buildable scaffolding may be
pre-staged **only on explicit operator opt-in**. (D28 was recorded 13 Jun 2026 after
this gate was lost across a session — agreed verbally, written nowhere durable.)

**13 Jun bridge (E10→E11) — landed on `main`, #143–#162.** Pre-E11 enabler +
hardening, NOT a start on E11 implementation (still gated):
- **Live-serve entrypoint (#143):** `roastpilot-agent serve [--host --port --spa-dir]`
  builds the live stack (`live.build_live_service` → `MCPServerProcess` →
  `RoasterControlAdapter` → `RoastService`, recovery lifespan, fail-closed) + a
  **static SPA mount** in `create_app`. The missing glue to run the supervised roast
  THROUGH the agent (#134); early-completes **E11-S1's "api.py serves the SPA"** (the
  wheel build-hook + `[pi]` extra still remain). `serve` forwards `COFFEE_*` to the MCP
  child; **Ctrl-C now safely stops** — graceful shutdown commands heat→0 through the
  safety path (controller e-stop) BEFORE stopping the MCP child, bounded + fail-closed
  (#142). A hard kill (SIGKILL/power loss) is uncatchable → still restart →
  `operator_recovery_required`, never auto-resume.
- **Governance-as-code:** `main` is branch-protected (required checks + codecov +
  `required_conversation_resolution` + `enforce_admins`, no bypass; auto-merge on).
  `claude-review` posts **blocking inline** findings (`--comment`) but is intentionally
  **NOT a required check** (fails by design on workflow-editing + Dependabot PRs; the
  gate is the inline threads + conversation-resolution). AGENTS.md gained a **Code
  Review Rubric** (#147).
- **Demo/operator ergonomics:** one-command launch scripts `scripts/roast-replay.sh`
  (#135 device test) + `scripts/roast-live.sh` (#134), wait-for-ready + rebuild-stale-SPA
  (#150–#152); the **known-good MCP config** at
  `docs/examples/coffee-roaster-mcp.known-good.yaml` (#149) — uses the FC
  **library-default** profile (0.6/5/20s/overlap0.7), distinct from the Pi `pi_inference`
  profile (0.90/3/30/0.3) E11 will bundle.
- **Device-test fix (#154):** the dashboard live curve now backfills from `/telemetry`
  on (re)connect + renders an **M:SS** time axis (was blank on late-join, raw seconds).
  Low follow-ups → #155.
- **Hardware-sensing startup readout (#157):** `serve` prints the MCP child's resolved
  runtime (real Hottop vs `mock` driver, port, FC mode/model) before uvicorn serves —
  a can't-miss "right hardware + FC on?" console block for the #134 roast.
- **Start Roast button (#158/#160):** the dashboard idle state renders a **Start Roast**
  form (`POST /api/roasts`) — operate the roast without `curl`. Celsius 100–240 °C bounds
  on the temp inputs. The ONLY operator entry-point added; per the **automated-roaster
  minimal-UX** principle, heat/fan stay read-outs, not dials (no manual `set_heat`/`set_fan`).
- **Persistent live decision trace (#161/#162):** `serve` now writes the agent store to a
  **persistent** path (`--db` > `ROASTPILOT_DB` > `$XDG_STATE_HOME/roastpilot/…`), not a
  tempdir — the per-tick telemetry + every CLAMP/REJECT `SafetyEvaluation` + advisor
  decisions survive shutdown and a restart can read prior run state for recovery. Found
  *during* the #134 roast (operator asked where logs go). `roast-live.sh` defaults the
  trace to `~/roasts/roastpilot.sqlite3` and shows it in the READY banner. Replay stays
  ephemeral. (`--db` + `--replay` together now errors, not silently ignored.)

**Profile UX (D29, 13 Jun):** profiles stay inline-per-roast (D7) — no saved-profile
library / dropdown / clone / post-start edit. Profile *selection / reuse / rating-matched
recommendation* is deferred to **roastpilot-cloud** (feedback-learning), not yet fully
planned. Keeps the appliance UX minimal; the cloud owns the profile/feedback brain.

**Gate status (21 Jun update):** **#134 (supervised hardware roast) substantively CLEARED on
the harness** — roast 3 ran end-to-end (charge → roast → drop → cool, no segfault) on the
agent harness; the harness, safety fail-closed, deterministic floor, post-FC loop, and
persistent trace all worked. Residual on #134 is roast *quality* (the #327 trim, not a harness
gate) — operator to confirm whether to mark #134 Done now or after a clean ≤195 drop. **#135**
(device SSE) is DONE/CLOSED. **E11 still blocked on the independent D27 torch-free chain**
regardless. Historical context below.

**Gate status (13 Jun):** E11 still blocked. Of the two D28 operator manual tests, **#135**
(device SSE) is **DONE/CLOSED**; **#134** (supervised hardware roast) was the **sole remaining
operator gate** — operator running it 13 Jun, now with a persistent trace (#161). The
torch-free chain (D27: `coffee-first-crack-detection#54` → `coffee-roaster-mcp#157`,
cross-repo) is the other, independent gate. Bridge follow-ups still
open: **#155** (curve-hydration lows), **#159** (auto-merge-vs-review governance race;
interim rule: don't `--auto` substantive PRs). **#142** (graceful-shutdown → heat off) is
now **DONE** — see the build order below.

**#134 FIRST ATTEMPT (13 Jun 2026) — harness worked, run did NOT complete; gate still
OPEN.** The supervised roast was attempted live: `serve` + MCP child + FC model load +
tick loop + telemetry + dashboard + safety (537 evals: 428 ALLOW / 108 REJECT / 1
EMERGENCY_STOP) + UI Emergency Stop **all worked**. The **only** failure: the OpenRouter
key was a **1-day key, expired** → advisor `provider_error` every tick (401, never reached
OpenRouter) → operator e-stopped in **preheat** before charging beans. So **no beans
charged, no roast completed — #134 NOT passed.** Unblock = a fresh non-expiring key
(operator). The attempt also surfaced real gaps (below). Trace persisted at
`~/roasts/roastpilot.sqlite3`.

**LOCKED PLAN (13 Jun): do ALL of the below BEFORE the next #134 attempt** (operator
agreed). Mostly core-file (controller/advisor/config/safety/api/cli) → **sequential
single-owner**, NOT a parallel team (shared-surface; safety-critical — the "when not to
fan out" rule). Safety items route through **safety-reviewer**; tests via **qa**. Build
order:
1. ✅ **#142** Ctrl-C/SIGINT → heat off on shutdown (the safety hole hit live) — **DONE**
   (PR #175; safety-reviewer **PASS**; reuse `operator_emergency_stop`, bounded +
   fail-closed before `mcp.stop`). Follow-ups: #177 (wedged-child timeout), #176 (cooling).
2. 🟢 **#168** validate advisor reachability at startup + surface errors live ("advisor
   configured" only checks the key is *present* — would've caught the expired key).
   **Startup half DONE** (PR feature/168): a bounded, best-effort reachability probe
   (`RoastAdvisor.healthcheck()` → `AdvisorHealth`; `PydanticAIAdvisor` runs a cheap
   completion, `FakeAdvisor` is deterministic) prints **"advisor REACHABLE (provider/
   model)"** vs a loud **"⚠️ advisor UNREACHABLE: <error>"** at `serve` startup — as
   prominent as the mock-driver warning, never blocks serve (advisory-paused is valid).
   The result is exposed on `GET /api/health` (`advisor` field) so the dashboard can
   render an ADVISOR-OFFLINE state. Remaining: the **in-roast** dashboard ADVISOR-OFFLINE
   render (FE task — data is now available) + the distinct in-roast error indicator.
3. 🟢 **#167** persist `advisor_decisions` (table had ZERO call sites — trace dropped).
   **DONE** (PR feature/167): the controller now records an advisor row on **both** paths
   — success (`status='ok'`, the `RoastDecision`, latency, provider/model/prompt) and
   failure (`timeout`/`malformed`/`provider_error`; `unsafe`→`malformed`, `decision=None`)
   — each linked to the `safety_evaluation_id` of the verdict it produced (`persist_evaluation`
   now returns the row id; new `SnapshotSink.persist_advisor_decision`; advisor exposes a
   provider-agnostic `descriptor`). The timeline route already returns the rows, now enriched
   with the linked `safety_evaluation_id`. Controller diff is persistence-wiring only — no
   transition/verdict/call-policy change. Next: **#170** surface the advisor timeline in
   detail/history (FE).
4. 🟠 **#171** phase-dependent advisor cadence: preheat 30 s / **charged** 10 s / FC
   as-fast-as-it-returns (~5–7 s floor). "beans dropped" = **charged**. Keep change-based
   early-fire. **PR open** (`feature/171-phase-cadence`): `ControllerConfig.advisory_min_interval_seconds`
   is now a `dict[RoastPhase, float]` consult-floor map (default preheat 30 / pre-FC 10 /
   development 0); `advisory_interval_for(phase)` resolves it, 0/absent = unthrottled so the
   `AdvisoryCallPolicy` heartbeat fires every tick once the prior serial call returns. Delta
   triggers still short-circuit sooner. **Call-frequency only — no verdict/transition/execution
   change.** Awaiting CI + claude-review triage; not yet merged.
5. 🟢 **#172** stage-tuned prompt — **Option 2** (one prompt, per-stage sections:
   PREHEAT / DRYING-MAILLARD / FC-DEV) + fill `AdvisorContext` gaps
   (`target_development_percent`, charge band); **bake-off-gated** (not safety).
   **SCAFFOLD DONE, content pending bake-off (#173).** PR `feature/172-stage-tuned-prompt`:
   added the `v3` prompt (Option 2, one prompt with explicit PREHEAT / DRYING-MAILLARD /
   FC-DEVELOPMENT sections, first draft carrying v2's electric-Hottop framing) selectable
   but NOT default — **`AdvisorConfig.prompt_version` stays `v2`** until the bake-off
   validates v3; populated `AdvisorContext.target_development_percent` + the charge
   guidance band (`charge_guidance_min_c`/`max_c`) from the frozen profile in
   `controller._build_advisor_context`. **Prompt + context-population additive only — no
   verdict / transition / target-execution change**; advisor stays advisory-only.
6. 🟢 **#173** phase-dependent MODEL — fast model post-FC (haiku-4.5 / gemini-flash /
   gpt-5-mini-reasoning-off); **re-run bake-off weighting latency post-FC**; new D-number.
   **MECHANISM DONE** (PR `feature/173-per-phase-model`): `AdvisorConfig` gains a per-phase
   override map `model_slug_by_phase: dict[RoastPhase, str]` + a `model_for(phase)` resolver
   (falls back to base `model_slug`); `DEFAULT_ADVISOR_MODEL` (`anthropic/claude-opus-4.8`)
   is fanned across preheat/pre-FC/development so **the default is Opus everywhere — zero
   behavior change**. `PydanticAIAdvisor` selects the model by `context.phase` at
   `get_recommendation` time via a per-slug agent cache (`build_model(config, model_slug=…)`);
   the injected-model test seam still pins all phases; descriptor/healthcheck keep the base
   slug. **Additive config + selection only — no verdict/transition/call-policy change**;
   advisor stays advisory-only. **The bake-off SETS THE VALUES** (operator-gated: needs a
   valid key + operator judgment + API cost, weighting latency post-FC) → new D-number — the
   one piece that cannot be fully autonomous.
7. ✅ **#166** advisor unavailable → **fail-closed after N** (operator's call) — **DONE**
   (PR #185, **D30**, safety-reviewer **PASS**). N consecutive *availability* failures
   (`provider_error`/`timeout`, default N=3) → heat 0 % + overrun-safe fan +
   `operator_recovery_required` via a **RECOVERY** verdict; `malformed`/`unsafe` are
   **transparent** (don't count or reset the streak); paused advisor never accrues; resets
   on `ok`/`start_run`/`operator_resume`.
8. 🟢 **#165** RoR display (from preheat, mark charge) — **DONE** (PR #186): RoR was already
   plumbed bean RoR → telemetry/SSE → SPA and plotted by the shared `LiveCurve` (right-axis
   green series + legend °C/min + cursor) on dashboard AND detail; finished with an
   operator-facing live RoR readout in the dashboard `RoastHeader` + confirmed the charge
   (T0) marker; pre-charge RoR **shown, not hidden** (13 Jun operator clarification). Display
   only. · ✅ **#164** richer bean identity — **DONE** (PR #188): `RoastProfile` gains
   optional/defaulted `country` / `farm` / `description` / `bean_species` (a constrained
   `Literal`, **not** a `models.py` Enum — no safety-reviewer escalation) / `is_blend`,
   keeping `bean_origin` + `bean_varietal` (cultivar). Blend model = `is_blend` flag, primary
   carries the structured fields, secondaries in `description` (no structured component list).
   Back-compat: pre-#164 frozen `profile_json` still deserializes (test). Surfaced on the Start
   Roast form (identity vs roast-targets fieldsets + blend toggle), detail TitleBlock
   (provenance line + species/blend tags + description), and history (country + Blend badge);
   summary projection carries country/species/is_blend. **Identity metadata only — no
   safety/controller/enum behavior change.** Plan: D7 extension wants a new D-number (lead).

**Operator-dependencies for "all before next roast":** every code fix is buildable/testable
with the **FakeAdvisor** (no key). Only the **#173 / #172 bake-off model+prompt selection**
needs a live key + operator judgment + API cost — that step is operator-gated, done as a
focused session once the key is in hand.
