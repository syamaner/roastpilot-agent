---
name: capture
description: Drive the replay harness + SPA and capture a screenshot of a named page state (dashboard-live, dashboard-recovery, dashboard-fault, roast-detail, roast-detail-selected, history, history-empty) — for ui-reviewer, debugging, or the E12 demo rig. Uses the Playwright MCP; scripted Playwright fallback. (E10+: needs web/ + the replay harness.)
---

Capture a deterministic screenshot of one SPA page state, driven by the replay
harness (never real hardware). Pass the state name as the argument
(default: `dashboard-live`).

## Prerequisites (E10+)

- The replay harness exists (`roastpilot-agent --replay <export>` — E10-S1) and a
  fixture under `tests/fixtures/replay/` that produces the target state.
- The SPA is built/served (Vite dev server proxying `/api`, or the built `web/`).
- The Playwright MCP (`@playwright/mcp`, wired in `.mcp.json`) is available, or
  scripted Playwright via Bash as a fallback.

## State → fixture/route map

| State | How to reach it |
|---|---|
| `dashboard-live` | `/` during preheating — charge guidance band visible |
| `dashboard-recovery` | `/` in `operator_recovery_required` → recovery modal |
| `dashboard-fault` | `/` after a pre-T0 overrun fault → fault banner + safety trail |
| `roast-detail` | `/roasts/:id` of a completed run — curve, timeline, trace |
| `roast-detail-selected` | same, with a CLAMP trace row selected → curve marker |
| `history` / `history-empty` | `/roasts` populated / empty |

Pick (or step the replay to) a fixture that produces the requested state.

## Capture

1. Start replay at speed ≥ 10× (or step deterministically for an exact frame).
2. Via the **Playwright MCP**: `browser_navigate` to the route, `browser_wait_for`
   the state to settle (e.g. the phase badge / banner / modal), then
   `browser_take_screenshot`. (Scripted Playwright via Bash is the fallback.)
3. Save to `web/test-results/captures/<state>.png` (or the path the caller asks
   for). For chrome-only shots that exclude the flaky uPlot canvas, **mask the
   canvas** region (D24) — pixel-correctness of the chart is asserted via data,
   not screenshots.

## Output

The screenshot path(s) + a one-line note on what state was captured. This is a
capture utility — it does not judge (that's `ui-reviewer`) and is not the CI gate
(that's the scripted `toHaveScreenshot()` suite, D24).
