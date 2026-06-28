# design-sync notes — roastpilot-web

Repo-specific context for future syncs of the `roastpilot-web` SPA to
claude.ai/design (project `RoastPilot Design System`,
`a6c9090e-388d-4465-9ee0-1071a14cd585`).

## Shape & build

- This is a **SPA, not a component library** — there is no published package
  `.d.ts` tree and no library `dist` entry. The bundle is built from a hand-
  written entry `web/ds-entry.tsx` that re-exports the four shared components;
  `--entry ./web/ds-entry.tsx` makes `PKG_DIR` resolve to `web/` (its
  package.json name `roastpilot-web` = `cfg.pkg`). Keep that entry committed.
- Run from the repo root:
  ```sh
  node .ds-sync/package-build.mjs --config .design-sync/config.json \
    --node-modules ./web/node_modules --entry ./web/ds-entry.tsx --out ./ds-bundle
  node .ds-sync/package-validate.mjs ./ds-bundle
  ```
  Re-sync driver: same flags via `.ds-sync/resync.mjs` (+ `--remote` once an
  anchor is fetched).
- **Scope is deliberately the 4 reusable shared components**
  (`src/components/shared/`): AppFrame, VerdictBadge, ConnectionIndicator,
  LiveCurve. Page-level UI (dashboard/detail/history/home/harness) is out of
  scope — it's app composition, not a reusable on-brand surface. Re-scope only
  on an explicit request.

## Props & discovery

- Components are added by name via `cfg.componentSrcMap` (no `.d.ts` tree to
  auto-discover from). Each component's `<Name>Props` is hand-written in
  `cfg.dtsPropsFor` with the cross-module unions (SafetyVerdict, ConnectionStatus,
  RoastPhase, CurvePoint/CurveMarker) **inlined** so the contract is self-
  contained. If a component's real props change in source, update `dtsPropsFor`
  to match — it does NOT track the source automatically.

## Styling / fonts

- Dark-only DS; tokens are CSS custom properties on `:root`. The Tailwind-
  compiled CSS is taken from the built `dist/assets/index-<hash>.css` via
  `cfg.cssEntry` and appended into `_ds_bundle.css`; uPlot's base CSS is bundled
  by esbuild from LiveCurve's `import "uplot/dist/uPlot.min.css"`. Both reach
  designs through the `styles.css` import closure.
- **No web fonts** — the DS uses system font stacks (`ui-sans-serif`,
  `ui-monospace`), so no `[FONT_MISSING]` and nothing to ship in `fonts/`.

## Render check

- Chromium is the Playwright 1.55.1 build (`chromium-1193`) at
  `~/Library/Caches/ms-playwright/`. `playwright@1.55.1` is installed into
  `.ds-sync/node_modules` for the validate render check.

## Re-sync risks (watch-list)

- **`cfg.cssEntry` is a HASH-NAMED file** (`dist/assets/index-rn2fByNU.css`).
  `npm run build` in `web/` regenerates the app and **changes the hash**, which
  will break `cssEntry` with `[CSS_IMPORT_MISSING]`/`[CSS_PLACEHOLDER]`. On any
  re-sync that rebuilds the web app: re-point `cfg.cssEntry` at the new
  `dist/assets/index-*.css` (the largest CSS under `dist/assets/`). The current
  bundle reused the pre-existing `dist/`; it was NOT rebuilt this run.
- **`dtsPropsFor` is a manual mirror** of the component props — it can silently
  drift from the source TS. Re-verify against the current `.tsx` interfaces when
  the components change.
- **LiveCurve preview data is synthesized** in `.design-sync/previews/LiveCurve.tsx`
  (a plausible Hottop curve), not a real roast fixture. It's illustrative only.
- The `web/ds-entry.tsx` entry lives in `web/`, NOT under `.design-sync/`
  (required for PKG_DIR resolution). It's part of the durable sync inputs even
  though it sits outside `.design-sync/`.

## Known render warns

- None. All 4 previews render clean (0 bad, 0 floor cards, 0 thin/identical).
