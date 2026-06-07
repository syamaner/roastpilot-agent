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
- UI reference: `roastpilot-plan/roastpilot-agent/sketches/` (reference
  specs, never seed code); `ui-prompts.md` is the chart spec of record.

## Stories

### E10-S1 — Replay harness

Acceptance criteria:

- [ ] `replay.py` + `--replay` CLI flag stream a recorded export through the
  real SSE pipeline at 1×–60×.
- [ ] Deterministic Playwright runs use it; doubles as the screen-recording
  rig.

### E10-S2 — SPA scaffold and dashboard

Acceptance criteria:

- [ ] Vite + React + TS scaffold in `web/`; dev mode proxies `/api`.
- [ ] Dashboard per plan §7: header (phase badge, dev %, FC pipeline status
  + diagnostics drawer), live uPlot curve with five series (bean, env, RoR,
  heat %/fan % step-after lines, amber/teal), legend with live cursor
  readout + click-to-toggle, event markers, charge guidance band, control
  row, advisory panel with ALLOW/CLAMP/REJECT badge, safety banner +
  recovery modal, operator action bar (confirm-press e-stop), add-beans
  toast.
- [ ] Phase comes from server events only — never inferred locally.

### E10-S3 — History and detail pages

Acceptance criteria:

- [ ] History table per plan §7; detail page with full persisted curve
  (same five series + cursor readout), event timeline, decision trace
  table, export downloads, self-rating widget.
- [ ] Trace-row click highlights the timestamp on the curve.

### E10-S4 — SPA tests and SSE behavior

Acceptance criteria:

- [ ] Component tests + Playwright against the replay harness; ui-reviewer
  sub-agent pass recorded.
- [ ] SSE keep-alive/reconnect verified on Safari/iPadOS; resolution
  recorded in plan §11 (closes open item 4).

## Status

| Story | Title | Status |
|-------|-------|--------|
| E10-S1 | Replay harness | not started |
| E10-S2 | SPA scaffold and dashboard | not started |
| E10-S3 | History and detail pages | not started |
| E10-S4 | SPA tests and SSE behavior | not started |

Epic status: **not started** — depends on E7.
