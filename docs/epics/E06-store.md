# E6 — Store

## Goal

SQLite persistence: schema v1 exactly as specified in the component plan,
WAL + `synchronous=FULL`, per-tick commits during active roasts, and the
recovery reads the restart flow depends on.

## Plan links

- Component plan §5 (SQLite schema v1, indexes), §8 (`test_store.py`):
  `roastpilot-plan/roastpilot-agent/plan.md`
- Orchestration plan § Persistence:
  `roastpilot-plan/roastpilot-agent-orchestration-plan.md`

## Stories

### E6-S1 — Schema v1 and initialization ([#46](https://github.com/syamaner/roastpilot-agent/issues/46))

Acceptance criteria:

- [x] All nine tables from plan §5 verbatim plus the six specified
  indexes; exact table set pinned by test; foreign keys enforced.
- [x] WAL + `synchronous=FULL` PRAGMAs set and asserted in tests
  (synchronous == 2).
- [x] Append-only `PRAGMA user_version` migration mechanism — an appended
  migration applies on re-open, bumps the version, and leaves v1 content
  untouched (tested); re-initialization idempotent.

### E6-S2 — Write paths

Acceptance criteria:

- [ ] Per-tick commit during active roasts; telemetry rows every
  `telemetry_log_interval_seconds`; no forced WAL checkpoint per tick.
- [ ] Typed write APIs for events, safety evaluations, advisor decisions
  (context hash, not raw payload), command log, operator actions.

### E6-S3 — Recovery reads and immutability

Acceptance criteria:

- [ ] Restart recovery reads: last persisted run state + phase recoverable.
- [ ] Restart-during-preheat / development / cooling scenarios tested.
- [ ] Completed runs immutable (rating/notes/sync fields excepted) — tested.

## Status

| Story | Title | Status |
|-------|-------|--------|
| E6-S1 | Schema v1 and initialization | done |
| E6-S2 | Write paths | not started |
| E6-S3 | Recovery reads and immutability | not started |

Epic status: **in progress** (E6-S1 done).
