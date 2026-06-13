# RoastPilot Agent Project State Registry

## Active Epic

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

**Gate status:** E11 still blocked. Of the two D28 operator manual tests, **#135** (device
SSE) is **DONE/CLOSED**; **#134** (supervised hardware roast) is the **sole remaining
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
   **Operator-gated** (needs a valid key + operator judgment + API cost) — the one piece
   that cannot be fully autonomous.
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
   only. · ✅ **#164** richer bean identity — **DONE** (PR #187): `RoastProfile` gains
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
