# SPA visual + e2e testing (D24)

Two tracks, split by job. **Do not conflate them.**

## 1. The CI gate — scripted `@playwright/test` snapshots

The merge gate is the scripted `toHaveScreenshot()` suite in `*.spec.ts`, run
**inside the pinned Playwright Linux image**
(`mcr.microsoft.com/playwright:v1.55.1-noble`). The committed baseline PNGs under
`__screenshots__/` are generated **in that same image**, so they match the GitHub
runner. **Never commit baselines generated from local macOS / system Chrome** —
their pixels drift and the gate will flap.

What the gate asserts:

- **DOM chrome** (`toHaveScreenshot`) per replay state — header, connection
  indicator, verdict badges, legend, panels, modals, tables.
- The **uPlot canvas is masked** (`maskCanvas`) out of those shots. Its
  correctness is asserted via the **chart-data hook** (`readChartData` →
  `window.__chart`), not pixels.
- ≤1 loose canvas smoke shot ("did it draw / not blank").
- A **real-replay SSE smoke** (`stream-smoke.spec.ts`): the server-derived phase
  reaches the SPA over the live SSE path against the actual replay backend.

Determinism: fixed 1600×1000 viewport, `fonts.ready` awaited (`settle`),
animations disabled, reduced motion, a small non-zero pixel tolerance.

### Two runtimes — the suite drives the REAL replay harness (S1)

Playwright manages **two webServers** (see `playwright.config.ts`):

1. **The agent** in `roastpilot-agent --replay <fixture> --step` mode — the real
   backend (REST + SSE + the gated `POST /api/replay/{step,advance-to}` routes).
2. **The built SPA** via `vite preview`, proxying `/api` to the agent.

So a replayed roast is byte-identical to live, and the specs drive deterministic
states via `advance-to`/`step` (see `global-setup.ts`). The settle protocol is
`window.__lastEventId` (published by the SSE reducer) ≥ the step's `last_event_id`
— no arbitrary sleeps. `advance-to` failing loud (404) on an unreached marker is
the harness's contract (S1).

Because of (1), the snapshot job needs **both Python (the agent) and Node**.
Replay mode never spawns the MCP child, so no `libportaudio2`/`sounddevice` is
required (unlike the Python `checks` job). The CI `web-snapshots` job installs
Python 3.11 + `pip install -e .` alongside Node before running the suite.

**S2 scope:** the harness drives **one** real state (session-2 → `preheating`)
to prove the webServer + deterministic stepping end-to-end via the dev-only
`/__stream-smoke` route. The full fixture→marker state matrix (dashboard-live/
fault/recovery, detail, history) lands with the pages (S3–S5) / S6, reusing this
exact path and the `tests/e2e/global-setup.ts` helpers.

### Generating / updating baselines

Always inside the pinned image, `--platform=linux/amd64` to match CI. Mount the
**repo root** (the agent + replay fixtures live there) and install both runtimes:

```bash
# from the repo root
docker run --rm --platform=linux/amd64 \
  -v "$PWD":/work -w /work/web \
  mcr.microsoft.com/playwright:v1.55.1-noble \
  bash -c '
    apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip
    python3 -m venv /tmp/venv && . /tmp/venv/bin/activate
    pip install -q -e /work
    export PATH="/tmp/venv/bin:$PATH"
    npm ci && npm run build && npx playwright test --update-snapshots
  '
```

Commit the resulting `__screenshots__/**/*-linux.png`. Bump the image tag and the
`@playwright/test` / `playwright-core` versions in `package.json` together.

## 2. Direction-match review — `ui-reviewer` + `/capture` (NON-gating)

`ui-reviewer` uses the Microsoft **Playwright MCP** (`@playwright/mcp`, wired in
the repo `.mcp.json`) for exploratory *direction-match* judgment against the
frozen prototype baselines — **kept off the merge gate**. The `/capture` skill
captures a named page state for the reviewer / debugging / the E12 demo via
`scripts/capture.mjs` (local `playwright-core` + system Google Chrome, no heavy
download). These local pixels are for human/agent judgment only — never the gate.

**MCP tool-grant (verify on first use).** `ui-reviewer` should list
`mcp__playwright` (the whole server). If Claude Code does not honor the
server-level grant on the page PRs (S3–S5), replace it with the explicit tool
names: `mcp__playwright__browser_navigate`, `mcp__playwright__browser_snapshot`,
`mcp__playwright__browser_take_screenshot`. The server itself is confirmed
resolvable (`npx @playwright/mcp@latest --help`); the grant is the only open
item, checked the first time `ui-reviewer` runs.

## S2 scope vs S3–S6

S2 (the foundation) ships the harness wired to the deterministic
`/__chart-harness` route (fixed data) so the suite is **green before any page
exists** — proving the conventions. S3–S6 add the product page-state snapshots
(dashboard-live, dashboard-recovery, dashboard-fault, roast-detail,
roast-detail-selected, history, history-empty) backed by the **replay harness**
(S1): point `playwright.config.ts`'s `webServer` at
`roastpilot-agent --replay <fixture>` + the SPA once S1 lands.
