# RoastPilot Agent Project State Registry

## Active Epic

> **STATUS UPDATE — 21 Jun 2026 (supersedes the PREP framing below):** the D35 control work
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

- Epic file: `docs/epics/E11-packaging.md` — **BLOCKED, do not start (D28 + D27)**.
  E10 closed 11 Jun 2026. E11 is next in order but **gated**: do **not** begin E11
  implementation (#136/#137/#138) until **both** operator manual tests are Done —
  **#135** (real-device Safari/iPad SSE) is **✅ DONE**; **#134** (supervised hardware
  roast, D17 criterion 3) is the **sole remaining operator gate** (running 13 Jun) —
  **and** the torch-free chain is green (**D27**: `coffee-first-crack-detection#54` →
  `coffee-roaster-mcp#157` — cross-repo, NOT this repo's #54/#157). See **D28**. Until
  both gates clear there is no agent-startable story; the next session should verify them
  before touching E11.
- Project: RoastPilot (GitHub user project, owner `syamaner`)
- Repository: `syamaner/roastpilot-agent`
- Package: `roastpilot-agent`
- Import package: `roastpilot_agent`
- Console entrypoint: `roastpilot-agent`
- Current phase: M1 build (harness complete target: July 2026)
- **July milestone (D17)** — "harness complete" = (1) E9 vertical slice
  green in CI + (2) E10 dashboard usable for a live roast + (3) one
  supervised real-hardware roast end-to-end. E11/E12 polish may run into
  August; demo assets recorded by end of August. Every session optimizes
  for this finish line; the first supervised hardware session is targeted
  for **June**.

## Working Rules

- Before starting implementation, read this registry, then the active epic
  file, then the GitHub issue for the story.
- One PR per story; branch `feature/{issue-number}-{slug}`; the PR that
  completes a story updates the epic file's status table in the same PR.
- Plans live in `~/git/roastpilot-plan` and are the source of truth; record
  resolved open items in component plan §11.
- Closing an epic = create the next epic's story issues from its spec
  file, update this registry, and flip the epic's project item to Done;
  an epic's project item goes In Progress when its first story does.
- Epic order: E1 ✅ → E2 ✅ → E3 ✅ → E4 ✅ → E5 ✅ → E6 ✅ → **E8** (advisor), then E7 →
  E9 (vertical slice) → E10 (SPA) → E11 (packaging) → E12 (validation/demo).

## Active Context

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
  drop, no roaster write). PM-relayed decision (to confirm with the operator): keep
  `drop_dev_margin_percent` unchanged at the 3 pp default, RE-CHECK on roast 4 and tune only if
  needed. Mechanism: the controller subtracts the MCP-domain backdating *delta* (`confirmed_at −
  onset`, surfaced on `RoastTelemetry`) from its own receive-tick clock — never the cross-process
  absolute MCP `monotonic_seconds`; manual marks / pre-0.1.7 payloads / negative deltas fall back to
  receive-tick (future-rejection holds).
- Unfiled control idea: the deterministic floor should step fan up toward ~50 at FC.

**Operator-gated / untouched (leave as-is):** coffee-roaster-mcp **#169/#170** (T0/FC backdating —
CI-green + review-clean, awaiting the operator's gated merge + v0.1.7 release); **#323** (drop-ceiling
override — the guard-vs-ceiling conflict from roast 3); **#318** (pre-FC manual heat/fan silently
reverts — read-out vs operator-override decision); **#134/#135** (supervised hardware roast + device
SSE); **#228** (pre-FC anticipatory LLM advisory layer — deferred LAST, D50).

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
- **Governance / CI — DONE:** PR review roster curated to Claude Code Review + Augment
  (auggie); Codex + CodeRabbit disabled (#246, **plan D37**) — a conversation-resolution
  gate + a re-review-on-every-push bot was a non-terminating merge loop. CI Playwright
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
- **Operator-gated:** #323 (drop-ceiling override), #318 (pre-FC manual revert), mcp
  #169/#170 (T0/FC backdating, awaiting operator merge + v0.1.7).
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
