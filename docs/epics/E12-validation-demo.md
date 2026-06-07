# E12 — Validation & Demo

## Goal

Supervised Hottop validation and the September talk's demo assets. The
recorded traces are **deliverables, not byproducts**: advisory decision
trace with ≥1 CLAMP and ≥1 REJECT, MCP interaction trace, FC detector
probability trace, full-workflow screen capture.

## Plan links

- Component plan §9 (E12 row), §3 (drop_beans cooling verification story),
  §11.2: `roastpilot-plan/roastpilot-agent/plan.md`
- Timeline anchors (talk 17–18 Sept 2026, demo asset list):
  `roastpilot-plan/00-repository-structure.md`

## D17 note — hardware pulled forward

The **first supervised hardware session is targeted for June** (D17 pulls
hardware risk forward; it is also criterion 3 of the July
harness-complete definition). That session should resolve the
`drop_beans` cooling-behavior open item (component plan §11 item 2) —
record the resolution in plan §11 when it happens. Demo assets must be
recorded by **end of August** (D17), leaving September for slides.

## Stories

### E12-S1 — Supervised hardware validation

Acceptance criteria:

- [ ] Supervised Hottop runs with explicit validation notes per AGENTS.md
  hardware rules (heat, fan, drop, cooling, e-stop behavior).
- [ ] `drop_beans` cooling behavior on real hardware verified; controller's
  fallback (`start_cooling` after configured window) confirmed or removed;
  resolution recorded in plan §11 (closes open item 2).
- [ ] Unsafe/uncertain behavior fails closed and is documented.

### E12-S2 — Demo trace recording

Acceptance criteria:

- [ ] Recorded roast(s) containing ≥1 CLAMP and ≥1 REJECT advisory verdict,
  exported and archived as demo assets.
- [ ] MCP interaction trace and FC detector probability trace captured.
- [ ] Full roast workflow screen capture recorded via the replay harness or
  live run.

### E12-S3 — Talk asset review

Acceptance criteria:

- [ ] sim-roast-runner decision-trace summaries reviewed for the talk
  walkthrough.
- [ ] All public-facing assets pass the plan repo's accuracy boundaries
  (advisory-only wording, no determinism percentages, no "fully
  autonomous", no "production-ready" before hardware validation).

## Status

| Story | Title | Status |
|-------|-------|--------|
| E12-S1 | Supervised hardware validation | not started |
| E12-S2 | Demo trace recording | not started |
| E12-S3 | Talk asset review | not started |

Epic status: **not started** — depends on E11. Demo assets must exist before
the 17–18 Sept 2026 talk.
