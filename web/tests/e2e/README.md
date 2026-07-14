# SPA visual + e2e testing (D24 + D26)

Two tracks, split by job. **Do not conflate them.**

## 1. The CI gate — scripted `@playwright/test` snapshots

The merge gate is the scripted `toHaveScreenshot()` suite in `*.spec.ts`, run
**inside the pinned Playwright Linux image**
(`mcr.microsoft.com/playwright:v1.55.1-noble`). The committed baseline PNGs under
`__screenshots__/` are generated **in that same image**, so they match the GitHub
runner. **Never commit baselines generated from local macOS / system Chrome** —
their pixels drift and the gate will flap.

What the gate asserts:

- **The whole page** (`toHaveScreenshot`) per replay state — header, connection
  indicator, verdict badges, legend, panels, modals, tables, **AND the uPlot
  canvas** (D26 — see below).
- The **`window.__chart` chart-data hook** (`readChartData`) is asserted as the
  **authoritative correctness layer** alongside the pixels: data-assert green +
  pixel-diff red ⇒ a render/CSS regression, not a data bug.
- A **real-replay SSE smoke** (`stream-smoke.spec.ts`): the server-derived phase
  reaches the SPA over the live SSE path against the actual replay backend.

Determinism kit (D26, viewport/full-page fixed by #530): a 1600×1000 viewport,
`fullPage: true` on every `expect(page).toHaveScreenshot()` (the project's
`devices["Desktop Chrome"]` spread carries its own 1280×720 viewport, which wins
over the top-level `use.viewport` unless re-asserted after the spread — see
`playwright.config.ts`'s `chromium` project — and even a correctly-applied fixed
viewport can't cover pages taller than it, so `fullPage: true` is the actual
"whole page" guarantee, not the viewport size), `deviceScaleFactor: 1` (uPlot
scales its backing store by DPR), the specs wait on the `window.__chart`
point-count (`waitForChartPoints`) before shooting, `fonts.ready` awaited
(`settle`), animations disabled, reduced motion, replay-fixed data, and a small
**non-zero** `maxDiffPixelRatio` (0.01).

### The uPlot canvas is UN-MASKED (D26 — revises D24)

D24 masked the canvas out of the screenshots and asserted only its data. **D26
reverses that**: the canvas — the product's primary visual — is **included** in
every page shot. A 0px-collapsed canvas, a wrong series color, a broken legend, or
a clipped axis all pass a green data-assert; only the pixel snapshot catches them.
The `mask:`/`maskCanvas()` convention is **removed**; `window.__chart` /
`readChartData()` stays as the complementary authoritative data oracle.

> **Why un-masking is safe across environments:** baselines are generated **and**
> diffed only inside the one pinned amd64 Docker image (Docker-vs-Docker, never
> cross-OS), so the canvas rasterizes identically both times — the same envelope
> that already makes the system-font DOM-chrome baselines stable. A bundled axis
> webfont is therefore **not required** under the CI-only-baselines rule; it is an
> OPTIONAL future hardening (it would matter only if baselines were ever compared
> across environments, which this rule forbids).

### Baselines are a CI-Docker-only artifact — NEVER macOS

Baselines are generated + diffed **only** inside the pinned
`mcr.microsoft.com/playwright:v1.55.1-noble` image (`--platform=linux/amd64`).
**Do not run `--update-snapshots` on macOS / system Chrome** — those pixels drift
and would poison the gate. Two ways to produce/refresh them, both in that image:

**(a) The canonical producer — the `web-snapshots-update` CI job
(`workflow_dispatch`).** Trigger it from the Actions tab on your branch; it runs
`npx playwright test --update-snapshots` inside the pinned container and commits
the regenerated `__screenshots__/**/*-linux.png` back to the branch. This is the
source of truth for baselines.

**(b) Locally via Docker** (when iterating, before the CI job). Mount the **repo
root** (the agent + replay fixtures live there) and install both runtimes:

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

> **node_modules gotcha:** the container's `npm ci` writes Linux binaries into the
> host-mounted `web/node_modules`, clobbering your macOS install. After a Docker
> run, recover the host with `rm -rf web/node_modules && (cd web && npm install)`.

Commit the resulting `__screenshots__/**/*-linux.png`. Bump the image tag and the
`@playwright/test` / `playwright-core` versions in `package.json` together.

### Two runtimes — the suite drives the REAL replay harness (S1)

Playwright manages **per-fixture webServer pairs** (see `playwright.config.ts`).
D26 added the multi-fixture states (`dashboard-fault` / `dashboard-recovery`),
which need different replay fixtures than `dashboard-live`. The dashboard at `/`
renders the live SSE stream of whatever fixture its agent runs, so each dashboard
state gets its own agent + `vite preview` pair on distinct ports:

| State | Fixture | Agent | SPA preview | marker → phase |
|---|---|---|---|---|
| `dashboard-live` + route harnesses | `session-2` | :8000 | :4173 | `preheating` → preheating |
| `dashboard-fault` | `session-1` | :8001 | :4174 | `fault` → faulted (real env-ceiling E-STOP) |
| `dashboard-recovery` | `fault-pre-t0` | :8002 | :4175 | `recovery` → operator_recovery_required |
| `dashboard-developed` | `session-2` | :8003 | :4176 | `first_crack` → development (full ramping curve) |

`dashboard-developed` reuses `session-2` but at `first_crack` — a SECOND session-2
agent, because `advance-to` is monotonic-forward per agent and `dashboard-live`
holds its own agent at `preheating`. It is the state that makes the un-mask
**pay off**: the curve has real shape (ramping bean/env/RoR spread across ~1031 s
of elapsed-since-T0 + heat/fan step lines + the FC marker), so a broken full-roast
curve can no longer match a near-empty preheating baseline. Its spec adds a
curve-**shape** data assertion (bean span + x-axis spread + FC marker), not just a
point count. (The x-axis spread depends on **#128** — stepped `elapsed_seconds` is
sim-time, not wall-clock; before that fix an `advance-to` burst collapsed the curve
onto one x.)

Each preview proxies `/api` to its agent (`ROASTPILOT_API` set at preview-start —
no rebuild per fixture; see `playwright.config.ts` `webServer[].env` and the shared
`tests/e2e/urls.ts`). So a replayed roast is byte-identical to live, and the specs
drive deterministic states via `advance-to`/`step` (which now take the agent base
URL — see `global-setup.ts` `AGENTS`). The settle protocol is `window.__lastEventId`
(published by the SSE reducer) ≥ the step's `last_event_id` — no arbitrary sleeps.
`advance-to` failing loud (404) on an unreached marker is the harness's contract (S1).

The route-harness pages (`/__chart-harness`, `/__detail-harness`, `/__stream-smoke`)
and the history page are fixture-independent and use the session-2 preview (the
suite baseURL); history mocks `/api/roasts` via a route intercept (no chart).

Because the suite boots agents, the snapshot job needs **both Python (the agent)
and Node**. Replay mode never spawns the MCP child, so no `libportaudio2`/
`sounddevice` is required (unlike the Python `checks` job). The CI `web-snapshots`
job installs Python 3.11 + `pip install -e .` alongside Node before running the suite.

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
