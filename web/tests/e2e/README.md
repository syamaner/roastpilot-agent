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

Determinism: fixed 1600×1000 viewport, `fonts.ready` awaited (`settle`),
animations disabled, reduced motion, a small non-zero pixel tolerance.

### Generating / updating baselines

Always inside the pinned image, `--platform=linux/amd64` to match CI:

```bash
# from web/
docker run --rm --platform=linux/amd64 \
  -v "$PWD":/work -w /work \
  mcr.microsoft.com/playwright:v1.55.1-noble \
  bash -c "npm ci && npm run build && npx playwright test --update-snapshots"
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
