# E7 — API

## Goal

The full REST + SSE surface the SPA renders from: roast lifecycle routes,
history/detail/telemetry/timeline reads, the operator action queue, and the
typed SSE event stream. One backend authority — the SPA never calls MCP.

## Plan links

- Component plan §6 (API contract: routes, operator actions, SSE event
  types), §8 (`test_api.py`): `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § API And UI Events:
  `roastpilot-plan/roastpilot-agent-orchestration-plan.md`

## Stories

### E7-S1 — REST routes

Acceptance criteria:

- [ ] All plan §6 routes: health (incl. MCP child status + active run id),
  POST /api/roasts (409 if active), history list, run detail, telemetry
  (with downsample param), timeline (decision trace), log manifest +
  downloads, rating.
- [ ] Typed Pydantic response models in `models.py`; route tests.

### E7-S2 — Operator action queue

Acceptance criteria:

- [ ] Action enum per plan §6 (incl. recovery-only `mark_beans_added` and
  `start_cooling`); every action → `operator_actions` row → controller
  queue → safety policy → MCP.
- [ ] Rejected/failed actions reported with reasons; tested.

### E7-S3 — SSE stream

Acceptance criteria:

- [ ] All plan §6 event types, typed JSON payloads, telemetry every tick,
  15 s heartbeat.
- [ ] Disconnect handling tested; UI disconnect does not trigger cooling or
  block backend safety. (The controller-side rule — operator timeout only
  in true operator-required states, never in normal phases — is owned by
  E4-S4 per D16; this story covers the API/SSE side only.)

## Status

| Story | Title | Status |
|-------|-------|--------|
| E7-S1 | REST routes | done |
| E7-S2 | Operator action queue | not started |
| E7-S3 | SSE stream | not started |

Epic status: **in progress** — depends on E4 ✅, E6 ✅.

## Notes

- **E7-S1 (#67):** REST surface + typed response models (`models.py`):
  `GET /api/health` (liveness + MCP child status + active run id),
  `POST /api/roasts` (409 if active), history list, run detail, telemetry
  (`?downsample=` stride), timeline (decision trace), log manifest + artifact
  downloads, operator rating. Backed by a new `RoastService` in `api.py` and
  read projections in `store.py`. The persisted run starts in `starting`; the
  live controller tick loop + MCP child that *drive* it are wired by the E9
  vertical slice (`RoastService` is the seam). No safety/controller changes.
