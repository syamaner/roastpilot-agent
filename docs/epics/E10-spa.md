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
  (+ CodeRabbit, the second-reviewer experiment); `ui-reviewer` on the page
  PRs (S3/S4/S5) against the frozen baselines (direction-match).
- **Independent triage:** GitHub/CodeRabbit feedback is adjudicated by the
  lead/PM or the `pr-triage` subagent — never the author teammate dismissing
  comments on its own PR (AGENTS.md merge policy).

### Open question (resolve at the S2/S3 boundary)

The operator action bar must not hardcode the command×phase matrix
client-side (invariant), yet it needs to know *which actions are enabled in
the current phase*. Decide in S2 (the contract owner): either (a) the server
exposes an `enabled_actions` list on the run snapshot / an event (a small E7
contract addition), or (b) the bar enables optimistically and relies on the
operator-action endpoint's typed reject-with-reason (already implemented) for
invalid attempts. (a) is the better UX; (b) needs no server change. Pick one
before S3 builds the action bar — don't let the dashboard teammate invent it.

## Stories

### E10-S1 — Replay harness

Owner: `replay` teammate (Python; runs ∥ with S2). Acceptance criteria:

- [ ] `replay.py` + `--replay` CLI flag stream a recorded export through the
  real SSE pipeline at 1×–60×; deterministic stepping for Playwright; 1× is
  the screen-recording rig (E12).
- [ ] Replay fixtures copied into `tests/fixtures/replay/` (the 7-Jun
  live-roast exports per the kickoff brief §4) — no cross-repo runtime refs.

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
  trace-row→highlight hook. `ui-prompts.md` is the spec.
- [ ] D15 verdict helper (ALLOW/CLAMP/REJECT badge; RECOVERY/FAULT/E-STOP are
  not badges — brief §3) + the routing shell for the three pages.

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

- [ ] Detail page: full persisted curve (the shared `LiveCurve`), event
  timeline, decision-trace table (all six verdicts in its column — it renders
  history), export downloads, self-rating widget.
- [ ] Trace-row click highlights the timestamp on the curve (toggle-off on
  re-click).

### E10-S6 — SPA tests and SSE behavior

Owner: lead / `ui-reviewer`. Acceptance criteria:

- [ ] Component tests + Playwright against the replay harness; `ui-reviewer`
  sub-agent pass recorded against the frozen baselines.
- [ ] SSE keep-alive/reconnect verified on Safari/iPadOS; resolution recorded
  in plan §11 (closes open item 4).

## Status

| Story | Title | Status |
|-------|-------|--------|
| E10-S1 | Replay harness | not started |
| E10-S2 | SPA foundation (shared substrate) | not started |
| E10-S3 | Dashboard (live) | not started |
| E10-S4 | History page | not started |
| E10-S5 | Roast detail page | not started |
| E10-S6 | SPA tests and SSE behavior | not started |

Epic status: **not started** — depends on E7 ✅. Re-sliced from 4→6 stories
for parallel agent-team delivery (D23).
