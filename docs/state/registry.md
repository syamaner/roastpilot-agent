# RoastPilot Agent Project State Registry

## Active Epic

- Epic file: `docs/epics/E11-packaging.md` — **BLOCKED, do not start (D28 + D27)**.
  E10 closed 11 Jun 2026. E11 is next in order but **gated**: do **not** begin E11
  implementation (#136/#137/#138) until **both** operator manual tests are Done —
  **#134** (supervised hardware roast, D17 criterion 3) and **#135** (real-device
  Safari/iPad SSE) — **and** the torch-free chain is green (**D27**: #54 → #157).
  See **D28**. Until then there is no agent-startable story; the next session should
  verify those gates before touching E11.
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

**E11 (Packaging) is next in epic order but BLOCKED — do not start (D28 + D27).**
Two independent gates must clear first:
1. **Operator manual tests (D28)** — human-owned (@syamaner): **#134** (E12-S1
   supervised hardware roast through the agent harness, D17 criterion 3) and
   **#135** (E10-S6 manual Safari/iPadOS SSE on real devices). *Both* must be Done
   before E11 implementation begins. Rationale: prove the harness on real hardware +
   devices before packaging/distributing it.
2. **Torch-free chain (D27)** — Phase 1 **#54** (FC-repo librosa filterbank +
   accuracy gate ≥96.86 % acc / 96.9 % precision on the 191-sample v2 set) → Phase 2
   **#157** (torch-free `coffee-roaster-mcp` release); E11's `[pi]` extra pins #157.
E11 stories #136/#137/#138 are staged (Todo). Contract-buildable scaffolding may be
pre-staged **only on explicit operator opt-in**. (D28 was recorded 13 Jun 2026 after
this gate was lost across a session — agreed verbally, written nowhere durable.)
