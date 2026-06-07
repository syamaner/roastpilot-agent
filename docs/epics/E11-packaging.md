# E11 — Packaging

## Goal

One deployable unit: a wheel that includes the built `web/dist`, a systemd
unit (agent spawns MCP as child — one unit total, per D6), and a deployment
document for the Raspberry Pi 5 target.

## Plan links

- Component plan §7 (packaging paragraph), §11.3 (hatchling build-hook open
  item): `roastpilot-plan/roastpilot-agent/plan.md`
- 00-repository-structure D1 (SPA ships inside the wheel):
  `roastpilot-plan/00-repository-structure.md`

## Stories

### E11-S1 — Wheel with bundled SPA

Acceptance criteria:

- [ ] `web/dist` built in CI (Node step) and included in the wheel via a
  hatchling force-include/build hook; `api.py` serves it as static files.
- [ ] Built-wheel smoke test in CI (install into clean venv, CLI + health
  route).
- [ ] Build-hook approach recorded in plan §11 (closes open item 3;
  fallback: commit built dist for the first release).

### E11-S2 — systemd unit and deployment doc

Acceptance criteria:

- [ ] systemd unit: one service, agent spawns MCP stdio child; restart
  lands in the recovery flow (never auto-resumes heat/fan).
- [ ] Deployment doc: Pi 5 install, config (env vars), data location,
  upgrade, log access.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E11-S1 | Wheel with bundled SPA | not started |
| E11-S2 | systemd unit and deployment doc | not started |

Epic status: **not started** — depends on E9, E10.
