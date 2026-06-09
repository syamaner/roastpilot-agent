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
2. **The uPlot canvas is NOT pixel-snapshotted** — `mask:` it and **assert the
   chart's data** via a test hook (the replay harness makes data deterministic);
   ≤1 loose "did it blank/crash" canvas smoke shot. No GPU runner.
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
  data without pixel-diffing the canvas (D24).
- [ ] D15 verdict helper (ALLOW/CLAMP/REJECT badge; RECOVERY/FAULT/E-STOP are
  not badges — brief §3) + the routing shell for the three pages.
- [ ] **Playwright snapshot + capture harness** (D24): the scripted
  `@playwright/test` `toHaveScreenshot()` setup against the replay harness, the
  canvas-mask + chart-data-assert convention, the `.mcp.json` wiring the Playwright
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

- [ ] History table per plan §7 (date, bean, profile, outcome, FC time,
  dev %, rating) + filter + empty state. Sparklines optional (cut first if
  time is tight).

### E10-S5 — Roast detail page

Owner: `detail` teammate. Acceptance criteria:

- [x] Detail page: full persisted curve (the shared `LiveCurve`), event
  timeline, decision-trace table (all six verdicts in its column — it renders
  history), export downloads, self-rating widget.
- [x] Trace-row click highlights the timestamp on the curve (toggle-off on
  re-click).

### E10-S6 — SPA tests and SSE behavior

Owner: lead / `ui-reviewer`. Acceptance criteria:

- [ ] Component tests + the **scripted `toHaveScreenshot()` snapshot suite**
  (D24) running **headless in the pinned Playwright Docker image in CI** against
  the replay harness (harness set up in S2): DOM chrome per state, canvas masked +
  chart data asserted. `ui-reviewer` (Playwright MCP, direction-match) pass
  recorded against the frozen baselines — not a merge gate.
- [ ] SSE keep-alive/reconnect verified on Safari/iPadOS; resolution recorded
  in plan §11 (closes open item 4).

## Status

| Story | Title | Status |
|-------|-------|--------|
| E10-S1 | Replay harness | done (#101) |
| E10-S2 | SPA foundation (shared substrate) | done (#100) |
| E10-S3 | Dashboard (live) | done (#95) |
| E10-S4 | History page | done (#114) |
| E10-S5 | Roast detail page | in review (#116) |
| E10-S6 | SPA tests and SSE behavior | not started |

Epic status: **in progress** — S1 (#101) + S2 (#100) merged to `main`; the E7
`enabled_actions` contract (#107, D25) merged; the S2 foundation follow-up (#115,
phase_changed fix + types audit + bean token) merged; S3 (#95, the dashboard) +
S4 (#114, history) merged; S5 (#116, detail) in review; S6 next. Re-sliced from
4→6 stories for parallel agent-team delivery (D23).

S3 notes: the dashboard renders the live curve, header (phase badge / roast +
development timers / FC status / diagnostics drawer), control row (ghost markers =
advisor targets), advisory panel (ALLOW/CLAMP/REJECT badges), operator action bar
(enablement from the server `enabled_actions` mirror; confirm-press e-stop; hides
permitted-but-meaningless toggles on terminal phases), recovery modal, fault
banner + safety trail, and add-beans toast. Two contract gaps surfaced (tracked as
#112): live `development_percent` is not on `TelemetryEventData` (show a
development timer, omit %); no live FC-audio pipeline health signal (render real FC
state — "listening" → detection — not a mock dot). `dashboard-live` snapshot ships
here; `dashboard-fault` / `dashboard-recovery` snapshots deferred to S6 (their
components are covered by component tests) — they need the multi-fixture replay
harness S6 builds once. A foundation `phase_changed` field drift
(`agent_phase`→`phase`) was caught during S3 and routed to platform.
