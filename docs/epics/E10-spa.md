# E10 — SPA

## Goal

The device SPA in `web/` (D1, D8): live dashboard (the demo centerpiece),
roast detail, and history — Vite + React + TS, Tailwind + shadcn/ui, uPlot,
TanStack Query + native EventSource. Plus the replay harness that makes UI
development and the talk's screen capture hardware-free.

## Plan links

- Component plan §7 (full SPA spec — pages, five-series chart, advisory
  panel, replay harness), §11.4 (Safari/iPad SSE open item):
  `roastpilot-plan/roastpilot-agent/plan.md`
- **UI kickoff brief** (prototype→component mapping, tokens, replay fixtures,
  verdict rendering, demo wiring, out-of-scope):
  `roastpilot-plan/roastpilot-agent/e10-ui-kickoff.md`
- UI reference: `roastpilot-plan/roastpilot-agent/sketches/` (Figma Make
  exports + frozen screenshot baselines — **reference specs, never seed
  code**); `ui-prompts.md` is the chart spec of record.
- Delivery model + the 6-story re-slice: plan decision **D23**.

## Agent-team delivery (D23)

E10 is sliced into 6 single-owner stories so each is a clean branch/PR and the
fan-out is unambiguous. **Foundation first, then one teammate per page.**

- **Ownership:** the lead/PM builds (or assigns) S1 + S2. After S2 merges,
  one teammate per page owns S3/S4/S5 — each editing only
  `web/src/pages/<page>/` and consuming `web/src/{lib,hooks,components/shared,styles}/`
  **read-only** (need a shared change? message the lead — don't edit shared).
- **Dependencies:** S2 blocks S3/S4/S5; S1 ∥ S2; S3 ∥ S4 ∥ S5; S6 last.
- **Independent review** on every PR: GitHub Claude Code Review + codecov
  (+ a `/review-branch` roster pass, a second independent review); `ui-reviewer`
  on the page PRs (S3/S4/S5) against the frozen baselines (direction-match).
- **Independent triage:** that review feedback is adjudicated by the
  lead/PM or the `pr-triage` subagent — never the author teammate dismissing
  comments on its own PR (AGENTS.md merge policy).

### Open question (RESOLVED at the S2/S3 boundary → option (a))

The operator action bar must not hardcode the command×phase matrix
client-side (invariant), yet it needs to know *which actions are enabled in
the current phase*. **Decision (S2, lead-confirmed): option (a)** — the server
exposes an `enabled_actions: list[OperatorAction]` on the run snapshot
(`RoastDetail`) and re-emits it on `phase_changed`, derived read-only from the
existing `SafetyPolicy` command×phase matrix (no new safety logic). It is the
literal expression of the "action bar mirrors server state" invariant and the
better UX; (b) tempts a hidden client-side matrix.

S2 ships the SPA types forward-compatible with `enabled_actions?` (optional).
The server-side contract change lands as a **separate small E7-contract PR**
(`models.py` field + `phase_changed` payload + `api.py` derivation), routed
through `safety-reviewer` (touches the command×phase surface); it must merge
**before S3 builds the action bar**, not before S2. Until then the action bar
falls back to the operator-action endpoint's typed reject-with-reason.

### Playwright is core (set it up early, not at S6)

Playwright backs four things, so treat it as foundation: the `ui-reviewer`
visual review, the component/E2E tests, the screenshot baselines, and the E12
demo screen-recording rig. It must be working **by the end of S2** so
`ui-reviewer` can run on the page PRs (S3–S5) — not deferred to S6. Reuse the
established pattern from `roastpilot-plan/.../sketches/`: **`playwright-core` +
system Google Chrome** (no heavy download) + the `capture.mjs` screenshot script
(port it into `web/`). It runs **headless in CI** against the replay harness
(S6); the prototype baselines are **direction-match, not pixel-match**.

### Snapshot & visual testing — two tracks, split by job (D24)

1. **CI gate = scripted `@playwright/test` `toHaveScreenshot()`** in a *pinned*
   Playwright Docker image (`mcr.microsoft.com/playwright:vX.Y.Z`,
   `--platform=linux/amd64` to match GitHub CI; baselines generated **inside**
   it). Deterministic via the replay harness + fixed viewport + `fonts.ready` +
   animations off + small tolerance (keep non-zero). Snapshots the **DOM chrome**
   (header, advisory panel, badges, modals, tables) per replay state. These are
   the SPA's **own** baselines (committed PNGs), distinct from the prototype
   direction-match baselines.
2. **The uPlot canvas IS snapshotted (D26 revises D24).** ~~`mask:` it~~ — the
   canvas is included in the page screenshots. Baselines are a **CI-only artifact**
   (generated + diffed only inside the pinned amd64 Docker image, never on a dev
   machine — that rule is also the macOS-Docker friction fix). Determinism kit:
   `deviceScaleFactor: 1` (uPlot scales its backing store by DPR), wait on the
   `window.__chart` point-count hook before shooting, `fonts.ready`, animations off,
   replay-fixed data, residual jitter absorbed by `maxDiffPixelRatio ≈ 0.01`. The
   **chart-data assertion stays as a complementary layer, not a replacement** — it
   is the authoritative correctness oracle (data-assert green + pixel-diff red ⇒ a
   render/CSS regression, not a data bug); the snapshot is a visual-smoke layer over
   it. uPlot is 2D canvas (Skia CPU raster) — no GPU runner needed. *(Was: masked +
   data-only, on a since-corrected "canvas pixels are unavoidably flaky" premise.)*
3. **Vitest** snapshots only as sparse `toMatchInlineSnapshot` on small stable
   mappers (SSE-event→view-model, verdict→badge) — never full-DOM shadcn/Radix.
4. **`ui-reviewer` uses the Microsoft Playwright MCP** (`@playwright/mcp`, wired
   in `.mcp.json`) for exploratory *direction-match* judgment — **kept off the
   merge gate** (the scripted suite is the gate). The `/capture` skill captures a
   named state for the reviewer / debugging / the E12 demo.

## Stories

### E10-S1 — Replay harness

Owner: `replay` teammate (Python; runs ∥ with S2). Acceptance criteria:

- [x] `replay.py` + `--replay` CLI flag stream a recorded export through the
  real SSE pipeline at 1×–60×; deterministic stepping for Playwright; 1× is
  the screen-recording rig (E12). Replay drives the real
  `RoastService`/`RoastRunner`/`RoastController` via a `ReplayRoasterControl`
  (no parallel event path; agent phase is server-derived). The deterministic
  step API is the gated HTTP control surface `POST /api/replay/{step,advance-to}`
  (markers: preheating/t0/first_crack/clamp/drop/cooling/recovery/fault/end),
  mounted **only** in `--step` mode (a test pins it off the live app); each
  call returns `{agent_phase, tick, elapsed_seconds, finalized, settled,
  last_event_id}` for a sleepless Playwright settle.
- [x] Replay fixtures copied into `tests/fixtures/replay/` (the 7-Jun
  live-roast `session-1`/`session-2` exports per kickoff §4) — no cross-repo
  runtime refs. Plus a synthetic `fault-pre-t0/` track (clearly labelled) that
  drives the **real** `SafetyPolicy` past the pre-T0 bound for the
  fault/recovery baselines — the real roasts never fault. The talk's CLAMP key
  frame is synthesized demo trace (`source: replay_overlay`) whose verdict is
  computed by the real `SafetyPolicy.evaluate_command`, persisted to the
  timeline + emitted on SSE.

### E10-S2 — SPA foundation (the shared substrate, single-owned)

Owner: lead / `platform` teammate. Acceptance criteria:

- [ ] Vite + React + TS scaffold in `web/`; dev proxies `/api`; Tailwind +
  shadcn/ui; design tokens in `web/src/styles/tokens.css` from the sketch
  theme (defined **unconditionally** — dark is the only M1 theme); tabular
  figures for numerics.
- [ ] Typed API client + event types mirroring E7's `models.py` SSE/REST
  contract; TanStack Query for REST.
- [ ] SSE hook (native `EventSource`): hydrate from `GET /api/roasts/{id}` on
  (re)connect then apply events; capped-backoff reconnect; a
  **live/reconnecting/stale** header indicator. **Phase from server events
  only — never inferred locally.**
- [ ] Shared **`LiveCurve`** (uPlot) consumed by dashboard + detail: five
  series (bean, env on left °C axis; RoR on right; heat %/fan % step-after on
  a hidden 0–100 % scale, amber/teal), legend = live cursor readout +
  click-to-toggle, event markers (T0/FC/drop), charge band (preheating only),
  trace-row→highlight hook. `ui-prompts.md` is the spec. Expose a **chart-data
  test hook** (e.g. `window.__chart` / a `data-*`) so tests assert the series
  data (D24). *(Under D26 the canvas is also pixel-snapshotted at S6; this hook
  stays as the complementary authoritative correctness layer.)*
- [ ] D15 verdict helper (ALLOW/CLAMP/REJECT badge; RECOVERY/FAULT/E-STOP are
  not badges — brief §3) + the routing shell for the three pages.
- [ ] **Playwright snapshot + capture harness** (D24): the scripted
  `@playwright/test` `toHaveScreenshot()` setup against the replay harness, the
  chart-data-assert convention (canvas masked at S2; **D26 un-masks it at S6** so
  the canvas is snapshotted too), the `.mcp.json` wiring the Playwright
  MCP for `ui-reviewer`, and the `/capture` skill — so `ui-reviewer` and the
  snapshot suite can run on the page PRs (S3–S5). See "Playwright is core" +
  "Snapshot & visual testing" above. **Verify the Playwright MCP tool-grant on
  first use**: `ui-reviewer` lists `mcp__playwright` (whole server); if Claude
  Code doesn't honor the server-level grant, replace it with the explicit tool
  names (`mcp__playwright__browser_navigate` / `_snapshot` / `_take_screenshot`).

### E10-S3 — Dashboard (live)

Owner: `dashboard` teammate. The demo centerpiece. Acceptance criteria:

- [ ] Dashboard per plan §7 consuming the foundation: header (phase badge,
  dev %, FC pipeline status + diagnostics drawer), `LiveCurve`, control row
  (ghost markers = advisor targets), advisory panel (verdict badge + reason),
  operator action bar (confirm-press e-stop, enabled every phase; per-phase
  enablement of the rest from server state — never hardcoded client-side),
  recovery modal ("no auto-resume" copy), fault banner (+ safety event
  trail), add-beans toast.
- [ ] Phase comes from server events only — never inferred locally.

### E10-S4 — History page

Owner: `history` teammate. Acceptance criteria:

- [x] History table per plan §7 (date, bean, outcome, FC time, dev %, rating) +
  filter + empty state. FC time deferred at S4 (the `RoastSummary` list payload
  didn't carry it) and landed post-E10 via **#111** — `first_crack_at_utc`
  projected from the earliest persisted `first_crack` roast event, rendered as
  the history FC column (UTC `HH:MM`, em-dash when no FC). Profile name is out
  (D7: minimal profiles, no named profiles); sparklines stay optional.

### E10-S5 — Roast detail page

Owner: `detail` teammate. Acceptance criteria:

- [x] Detail page: full persisted curve (the shared `LiveCurve`), event
  timeline, decision-trace table (all six verdicts in its column — it renders
  history), export downloads, self-rating widget.
- [x] Trace-row click highlights the timestamp on the curve (toggle-off on
  re-click).

### E10-S6 — SPA tests and SSE behavior

Owner: lead / `ui-reviewer`. Acceptance criteria:

- [x] Component tests + the **scripted `toHaveScreenshot()` snapshot suite**
  (D24/**D26**) running **headless in the pinned Playwright Docker image in CI**
  against the replay harness (set up in S2). DOM chrome **and the uPlot canvas**
  per state — the canvas is **no longer masked** (D26); baselines are a **CI-only
  artifact** (generated + diffed only inside the pinned amd64 image). The
  chart-data assertion stays as the authoritative correctness layer alongside the
  pixel snapshot.
- [x] **Existing snapshots un-masked + baselines regenerated in Docker.** The
  shipped states that masked the canvas — `dashboard-live` (S3) plus the
  foundation / history / detail states — drop the `mask:` on the canvas selector
  and have their baselines regenerated inside the pinned image. Remove the
  canvas-mask convention from the S2 harness helper; keep the `window.__chart`
  chart-data hook + assertion.
- [x] **New multi-fixture states, canvas un-masked from the start:**
  `dashboard-fault`, `dashboard-recovery`, and the detail states deferred from
  S3/S5 (their components already have component tests; S6 builds the multi-fixture
  replay states they need). Each asserts chart data **and** snapshots the canvas.
  The `dashboard-developed` curve-shape state (operator review on #125)
  **shipped (#132)** once #128/#130 (replay stepped-elapsed = sim-time) landed.
  Re-adding it surfaced **#131** — a `LiveCurve` bug where every uPlot scale was
  built unranged from the empty mount, so the curve never drew for ANY consumer
  (the canvas-mask hid it); #132 fixes that (a `self.data` range callback, charge
  band folded into the °C scale) + adds a scale-covers-data guard.
- [x] **Canvas determinism kit applied** so the un-masked snapshots are stable in
  CI: `deviceScaleFactor: 1`, wait on the `window.__chart` point-count hook before
  shooting, `fonts.ready`, animations off, replay-fixed data, `maxDiffPixelRatio ≈
  0.01` (kept non-zero). Baselines are **never** generated/diffed on macOS — CI
  Docker only (+ the `web-snapshots-update.yml` producer). **D26 addendum:** the
  bundled axis webfont is **optional**, not required — baselines are generated AND
  diffed only inside the one pinned amd64 image (Docker-vs-Docker), so the system
  font renders identically; the webfont only matters cross-environment, which the
  CI-only-baselines rule already eliminates. (Roster-confirmed on #125.)
- [ ] `ui-reviewer` (Playwright MCP, direction-match) pass recorded against the
  frozen prototype baselines — not a merge gate. **Deferred** — API-fragile;
  stable-API session.
- [ ] SSE keep-alive/reconnect verified on Safari/iPadOS; resolution recorded
  in plan §11 (closes open item 4). **Deferred** — real-device/manual.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E10-S1 | Replay harness | done (#101) |
| E10-S2 | SPA foundation (shared substrate) | done (#100) |
| E10-S3 | Dashboard (live) | done (#113) |
| E10-S4 | History page | done (#114) |
| E10-S5 | Roast detail page | done (#116) |
| E10-S6 | SPA tests and SSE behavior | **deterministic close done** — contract drift guard (#120), `useRoastStream` frame-loss fix (#122 → #123), D26 canvas un-mask matrix (#125), replay stepped-elapsed sim-time (#128 → #130), `dashboard-developed` curve snapshot + `LiveCurve` scale-collapse fix (#131 → #132). **Deferred:** `ui-reviewer` MCP pass (API-fragile), Safari/iPad SSE (real devices) |

Post-E10 observability follow-up (D35 #220 cluster, not an E10 story): **#217 —
`LiveCurve` FIXED Y-axis scales** (operator decision, 14 Jun). Both value axes are
pinned (`scales.ts` `FIXED_SCALE_RANGES`): temperature `c` → 0–210 °C, RoR `ror` →
−20..+30 °C/min — so the live curve reads against an unchanging frame and never
auto-zooms to the current sensor reading, and the 170–200 charge band is always in
frame without stretching the domain. The x (time) axis stays data-driven (#131
scale-covers-data guard preserved). A fixed scale is also snapshot-stable; the D26
Playwright baselines that shifted were regenerated in the pinned image
(`dashboard-live`, `dashboard-charge-window`, `foundation-chrome`, `mic-green`,
`mic-error`). Replaces the previous charge-band-driven °C auto-fit and the RoR
data-range auto-fit. Delivered on PR #232.

Post-E10 observability follow-up (D35 #220 cluster, not an E10 story): **#220 —
live development time + DTR on the dashboard** (ROAST-CRITICAL, operator 14 Jun;
needed before the first roast and by the post-FC LLM loop #223). Two DISTINCT
server-authoritative readouts now ride the per-tick `telemetry` SSE frame:
`development_elapsed_seconds` (time since first crack) and `development_percent`
(DTR — that duration as a share of the WHOLE, CHARGE-referenced roast,
`development_elapsed / charge_elapsed * 100`, the same DTR the advisor reasons on,
#219). The controller already computed both clocks for the advisor context; #220
is read-only display plumbing — `ControllerSnapshot` projects them, `api.py` copies
them onto `TelemetryEventData`, and the dashboard `RoastHeader` renders the
`Development` timer + a new `DTR` readout (both hidden pre-FC). This CLOSES the
#112 gap (the dashboard previously showed a client-derived dev timer and omitted
the %; it is now server-authoritative, no client-side derivation). The chart
x-origin is UNCHANGED — re-referencing the curve to charge (Artisan-style 0:00) is
a separate operator UX decision still held. SPA contract mirror (`lib/types.ts`)
and the #236/#121 contract fixtures regenerated for the new fields. The existing
`dashboard-developed` Playwright baseline was EXTENDED (its post-FC state now shows
the dev-time + DTR readouts; no new snapshot state added — the developed state
already reaches `development`), and its test gained data-level assertions on both
readouts.

Epic status: **core + S6 deterministic work done** — the page fan-out is complete:
S1–S5 are all merged to `main` (replay #101, foundation #100, E7 `enabled_actions`
contract #107/D25, S2 foundation follow-up #115 = phase_changed fix + types audit
+ bean token, dashboard #113, history #114, detail #116). S6 (#98) shipped as five
PRs: the **D26 canvas snapshot matrix** (un-mask the uPlot canvas so screenshots
capture the whole page including the chart; #125), the **contract-fixture drift
guard** (pins the SPA's hand-mirrored types against real server frames so the
`phase_changed`-class drift can't recur; #120), the **`useRoastStream` frame-loss
fix** (#122 → #123), the **replay stepped-elapsed sim-time fix** (#128 → #130), and
the **`dashboard-developed` curve snapshot + `LiveCurve` scale-collapse fix**
(#131 → #132). The un-mask + the developed curve surfaced four foundation bugs from
the consumer side (#115-class, #122, #128, #131). **Deferred** to a stable-API /
real-device session: the consolidated `ui-reviewer` visual pass (MCP-heavy) and
Safari/iPad SSE (plan §11.4). Re-sliced from 4→6 stories for parallel agent-team
delivery (D23).

S3 notes: the dashboard renders the live curve, header (phase badge / roast +
development timers / FC status / diagnostics drawer), control row (ghost markers =
advisor targets), advisory panel (ALLOW/CLAMP/REJECT badges), operator action bar
(enablement from the server `enabled_actions` mirror; confirm-press e-stop; hides
permitted-but-meaningless toggles on terminal phases), recovery modal, fault
banner + safety trail, and add-beans toast. Two contract gaps surfaced (tracked as
#112): live `development_percent` is not on `TelemetryEventData` (show a
development timer, omit %) — **RESOLVED by #220** (both `development_elapsed_seconds`
and `development_percent`/DTR now ride the live frame, server-authoritative); no
live FC-audio pipeline health signal (render real FC
state — "listening" → detection — not a mock dot). `dashboard-live` snapshot ships
here (canvas masked at S3; **D26 un-masks + regenerates its baseline at S6**);
`dashboard-fault` / `dashboard-recovery` snapshots deferred to S6 (their
components are covered by component tests) — they need the multi-fixture replay
harness S6 builds once, and ship with the canvas un-masked (D26). A foundation
`phase_changed` field drift
(`agent_phase`→`phase`) was caught during S3 and routed to platform.
