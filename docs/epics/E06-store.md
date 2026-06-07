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

### E6-S2 — Write paths ([#47](https://github.com/syamaner/roastpilot-agent/issues/47))

Acceptance criteria:

- [x] Every writer commits immediately (a second connection sees each
  write — tested); telemetry rows throttled to
  `telemetry_log_interval_seconds` with the cadence proven (5s interval,
  3 of 5 ticks written); no WAL checkpoint is ever forced.
- [x] Typed write APIs: create_run/update_run_phase (frozen profile +
  config JSON), record_event, record_telemetry, record_safety_evaluation
  (returns the row id for trace linking), record_advisor_decision
  (sha256 context hash, never the raw payload — tested), record_command,
  record_operator_action (nullable run_id). All enum values stored as
  lowercase wire forms passing the v1 CHECKs.

### E6-S3 — Recovery reads and immutability ([#48](https://github.com/syamaner/roastpilot-agent/issues/48))

Acceptance criteria:

- [x] `read_latest_run` → typed `PersistedRun` (phase, outcome, frozen
  profile); `complete_run` finalizes outcome/manifest;
  `set_operator_rating` exercises the exception path.
- [x] Restart-during-preheat / development / cooling all tested with a
  fresh store instance (process-death simulation), plus an E4/E6 seam
  test: the persisted phase drives `recover_from_restart` into
  `operator_recovery_required` with zero writes.
- [x] Completed runs immutable — enforced by schema-v2 **triggers**, not
  application discipline: phase/outcome updates and deletes abort;
  operator rating/notes and cloud sync fields stay mutable; active runs
  unaffected. (The BEGIN guard learned to allow trigger bodies.)

## Status

| Story | Title | Status |
|-------|-------|--------|
| E6-S1 | Schema v1 and initialization | done |
| E6-S2 | Write paths | done |
| E6-S3 | Recovery reads and immutability | done |

Epic status: **done** — all three stories complete; E7 (API) is now unblocked (E4 ✅ + E6 ✅); E8 (advisor) is the remaining leaf.
