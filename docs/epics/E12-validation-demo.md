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
harness-complete definition). Demo assets must be recorded by **end of
August** (D17), leaving September for slides.

**Update 7 Jun 2026**: the `drop_beans` cooling question (plan §11 item 2)
was resolved early by coffee-roaster-mcp's own E7-S6 live Hottop roast —
drop+cooling is atomic (0.37 ms, `cooling_on: true` in the drop payload);
recorded in plan §11. Live audio FC also confirmed working on hardware
(confidence 0.907). The June agent session therefore carries one fewer
unknown; D17 criterion 3 (one supervised roast **through the agent
harness**) still stands.

## Stories

### E12-S1 — Supervised hardware validation

Acceptance criteria:

- [ ] Supervised Hottop runs with explicit validation notes per AGENTS.md
  hardware rules (heat, fan, drop, cooling, e-stop behavior).
- [ ] Controller's post-drop `start_cooling` fallback validated on real
  hardware — the cooling-behavior atomicity itself was confirmed 7 Jun
  via coffee-roaster-mcp E7-S6 and recorded in plan §11 (open item 2
  closed); the fallback is retained as defense-in-depth, so the remaining
  E12-S1 work is confirming it never double-fires.
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
