"""SQLite persistence (component plan §5; orchestration plan § Persistence).

E6-S1: schema v1 — the nine tables from plan §5 verbatim, the specified
indexes, WAL + ``synchronous=FULL`` durability PRAGMAs (active-roast
power-loss protection is the default bias), and a ``PRAGMA user_version``
migration mechanism. Write paths land in E6-S2, recovery reads in E6-S3.
"""

import asyncio
import hashlib
import json
import math
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import aiosqlite
from pydantic import BaseModel, ConfigDict

from roastpilot_agent.advisor import AdvisorContext, RoastDecision
from roastpilot_agent.config import AppConfig, ControllerConfig, SafetyLimits
from roastpilot_agent.models import (
    AdvisorTraceStatus,
    BeanProfile,
    BeanProfileDraft,
    BeanProfileInput,
    BrewMethod,
    CommandTraceSource,
    CommandTraceStatus,
    LogManifest,
    PostFcHeatAuthorityState,
    ProcessingMethod,
    ReferenceCurveSample,
    ReferenceLandmarks,
    ReferenceRoast,
    RoastCommand,
    RoastDetail,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastSummary,
    RoastTasting,
    RoastTelemetry,
    RoastTimeline,
    TastingAttribute,
    TastingDefect,
    TelemetryPoint,
    TimelineAdvisorDecision,
    TimelineCommand,
    TimelineEvent,
    TimelineSafetyEvaluation,
    TimelineVerdict,
    recording_origin_slug,
    weight_loss_percent,
)
from roastpilot_agent.roast_landmarks import (
    earliest_onset_within_event_window,
    interpolate_at,
    is_mcp_first_crack_source,
    utc_to_run_seconds,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict, enabled_operator_actions

SCHEMA_V1 = """
CREATE TABLE roast_runs (
  id TEXT PRIMARY KEY,                      -- uuid4
  mcp_session_id TEXT,
  agent_phase TEXT NOT NULL CHECK (agent_phase IN (
    'idle', 'starting', 'preheating', 'roasting_pre_first_crack',
    'development', 'cooling', 'complete', 'faulted',
    'operator_recovery_required')),         -- models.RoastPhase.value
  profile_json TEXT NOT NULL,               -- frozen RoastProfile
  config_json TEXT NOT NULL,                -- frozen ControllerConfig + SafetyLimits
  started_at_utc TEXT NOT NULL,
  completed_at_utc TEXT,
  outcome TEXT CHECK (outcome IN ('completed', 'aborted', 'faulted')),
  fault_reason TEXT,
  log_dir TEXT,                             -- from ExportRoastLogResult
  export_manifest_json TEXT,                -- jsonl/csv/summary paths + ready
  operator_rating INTEGER CHECK (operator_rating BETWEEN 1 AND 5),
  operator_notes TEXT,
  cloud_sync_status TEXT NOT NULL DEFAULT 'local_only'
    CHECK (cloud_sync_status IN ('local_only', 'pending_sync', 'synced', 'sync_failed')),
  cloud_roast_id TEXT,
  public_slug TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE roast_events (                 -- agent-level event log (superset of MCP events)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES roast_runs(id),
  kind TEXT NOT NULL CHECK (kind IN (
    'run_started', 'phase_changed', 'charge_guidance', 't0_detected',
    'first_crack', 'advisory', 'command_executed', 'command_failed',
    'safety_alert', 'fault', 'recovery_required', 'recovery_acknowledged',
    'logs_exported', 'run_completed')),     -- models.RoastEventKind.value
  source TEXT NOT NULL CHECK (source IN (
    'controller', 'mcp', 'operator', 'advisor', 'safety')),
                                            -- models.RoastEventSource.value
  monotonic_seconds REAL,
  recorded_at_utc TEXT NOT NULL,
  payload_json TEXT
);

CREATE TABLE telemetry_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES roast_runs(id),
  tick INTEGER NOT NULL,
  recorded_at_utc TEXT NOT NULL,
  elapsed_seconds REAL,
  agent_phase TEXT NOT NULL CHECK (agent_phase IN (
    'idle', 'starting', 'preheating', 'roasting_pre_first_crack',
    'development', 'cooling', 'complete', 'faulted',
    'operator_recovery_required')),
  mcp_phase TEXT,
  bean_temp_c REAL, env_temp_c REAL,
  bean_ror_c_per_min REAL, env_ror_c_per_min REAL,
  heat_level_percent INTEGER, fan_level_percent INTEGER,
  cooling_on INTEGER,
  development_percent REAL,
  raw_state_json TEXT                       -- full RoastSessionState dump
);

CREATE TABLE safety_evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES roast_runs(id),
  tick INTEGER NOT NULL,
  rule TEXT NOT NULL,                       -- which rule fired / 'all_clear'
  verdict TEXT NOT NULL CHECK (verdict IN (
    'allow', 'clamp', 'reject', 'recovery', 'fault', 'emergency_stop')),
                                            -- SafetyVerdict.value (lowercase wire form)
  input_heat INTEGER, input_fan INTEGER,
  adjusted_heat INTEGER, adjusted_fan INTEGER,
  reason TEXT NOT NULL,
  recorded_at_utc TEXT NOT NULL
);

CREATE TABLE advisor_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES roast_runs(id),
  tick INTEGER NOT NULL,
  provider TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  context_hash TEXT NOT NULL,               -- hash, not raw payload (plan policy)
  latency_ms INTEGER,
  decision_json TEXT,                       -- RoastDecision or NULL on failure
  status TEXT NOT NULL CHECK (status IN ('ok', 'timeout', 'malformed', 'provider_error')),
  safety_evaluation_id INTEGER REFERENCES safety_evaluations(id),
  recorded_at_utc TEXT NOT NULL
);

CREATE TABLE command_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES roast_runs(id),
  tick INTEGER NOT NULL,
  tool TEXT NOT NULL,                       -- MCP tool name (models.RoastCommand values)
  args_json TEXT,
  source TEXT NOT NULL CHECK (source IN (
    'policy', 'advisor', 'operator', 'safety', 'recovery')),
  safety_evaluation_id INTEGER REFERENCES safety_evaluations(id),
  status TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
  result_json TEXT,
  recorded_at_utc TEXT NOT NULL
);

CREATE TABLE operator_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES roast_runs(id),
  action TEXT NOT NULL,
  payload_json TEXT,
  result TEXT NOT NULL CHECK (result IN ('accepted', 'rejected', 'failed')),
  recorded_at_utc TEXT NOT NULL
);

CREATE TABLE sync_jobs (                    -- M2; table ships in v1 schema
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES roast_runs(id),
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('pending', 'in_flight', 'done', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE reference_roasts (             -- M2 cache; table ships in v1 schema
  id TEXT PRIMARY KEY,                      -- cloud summary id
  bean_origin TEXT NOT NULL,
  roast_level TEXT NOT NULL,
  summary_json TEXT NOT NULL,               -- RoastReference payload
  fetched_at_utc TEXT NOT NULL
);

CREATE INDEX idx_roast_events_run_kind ON roast_events(run_id, kind);
CREATE INDEX idx_telemetry_run_tick ON telemetry_snapshots(run_id, tick);
CREATE INDEX idx_safety_run_tick ON safety_evaluations(run_id, tick);
CREATE INDEX idx_advisor_run_tick ON advisor_decisions(run_id, tick);
CREATE INDEX idx_command_run_tick ON command_log(run_id, tick);
CREATE INDEX idx_roast_runs_sync_status ON roast_runs(cloud_sync_status);
"""

SCHEMA_V2_IMMUTABILITY = """
-- Completed runs are immutable (plan §8) except the operator/cloud
-- fields: operator_rating, operator_notes, cloud_sync_status,
-- cloud_roast_id, public_slug (and updated_at_utc bookkeeping).
CREATE TRIGGER roast_runs_immutable_after_completion
BEFORE UPDATE ON roast_runs
FOR EACH ROW
WHEN OLD.completed_at_utc IS NOT NULL AND (
  NEW.agent_phase != OLD.agent_phase
  OR NEW.profile_json != OLD.profile_json
  OR NEW.config_json != OLD.config_json
  OR NEW.started_at_utc != OLD.started_at_utc
  OR NEW.created_at_utc != OLD.created_at_utc
  OR NEW.completed_at_utc IS NOT OLD.completed_at_utc
  OR NEW.outcome IS NOT OLD.outcome
  OR NEW.fault_reason IS NOT OLD.fault_reason
  OR NEW.log_dir IS NOT OLD.log_dir
  OR NEW.export_manifest_json IS NOT OLD.export_manifest_json
  OR NEW.mcp_session_id IS NOT OLD.mcp_session_id
)
BEGIN
  SELECT RAISE(ABORT, 'completed roast_runs are immutable (operator/cloud fields excepted)');
END;

CREATE TRIGGER roast_runs_undeletable_after_completion
BEFORE DELETE ON roast_runs
FOR EACH ROW
WHEN OLD.completed_at_utc IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'completed roast_runs are immutable (operator/cloud fields excepted)');
END;
"""

SCHEMA_V3_T0_DETECTED_AT = """
-- #235: persist the absolute charge/T0 instant so the advisor's
-- charge-referenced DTR clock (#219) survives a restart→operator-resume.
-- Nullable and advisory/display-only — no safety gate reads it (the safety
-- box keys on temperature, never DTR). Pre-existing rows read back NULL,
-- which the recovery read treats as "charge clock unknown" (the prior
-- behaviour: a resumed run with no stored T0 keeps a None charge clock).
-- Intentionally OUTSIDE the v2 completed-run immutability set: it is written
-- exactly once on an ACTIVE run (the debounced T0 transition), never after
-- completion, so the immutability trigger never needs to guard it.
ALTER TABLE roast_runs ADD COLUMN t0_detected_at_utc TEXT;
"""

SCHEMA_V4_BEAN_PROFILES = """
-- #303 (D45): the saved bean-profile library behind the Start-Roast dropdown.
-- Additive — a NEW table only; no existing table or row is touched, so the
-- frozen roast_runs.profile_json snapshots and corpus integrity are unchanged
-- (a roast still instantiates a RoastProfile and freezes that, never a row here).
-- Stores the reusable BeanProfile template as one JSON column (the same
-- model_dump_json shape RoastProfile uses for profile_json) plus the columns the
-- list query filters/orders on. ``archived`` is a soft-delete flag, not a hard
-- DELETE: a profile may be referenced by a past roast's notes, so deleting only
-- hides it from the dropdown (archived = 1) and never dangles a reference.
CREATE TABLE bean_profiles (
  id TEXT PRIMARY KEY,                       -- uuid4 hex (BeanProfile.id)
  name TEXT NOT NULL,
  profile_json TEXT NOT NULL,                -- full BeanProfile model_dump_json
  archived INTEGER NOT NULL DEFAULT 0
    CHECK (archived IN (0, 1)),
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE INDEX idx_bean_profiles_archived ON bean_profiles(archived, name);
"""

SCHEMA_V5_CHARGE_ELAPSED = """
-- #308: persist the charge-referenced roast clock (seconds since charge/T0) on
-- each telemetry snapshot so the REST telemetry series re-origins the chart
-- x-axis at charge (Artisan 0:00) on a history/reload read, not only live over
-- SSE. Nullable and advisory/display-only — no safety gate reads it (the safety
-- box keys on temperature, never the clock). NULL before charge (and for
-- pre-existing rows), which the SPA renders as no roast-time / pre-charge
-- lead-in. Distinct from the existing serve-referenced elapsed_seconds column,
-- which is retained as the chart's raw x lead-in.
ALTER TABLE telemetry_snapshots ADD COLUMN charge_elapsed_seconds REAL;
"""

SCHEMA_V6_DRYING_END_EVENT = """
-- #351: add the pre-FC 'drying_end' event kind (the .alog-validated ~150 °C
-- drying→browning landmark) to the roast_events.kind CHECK. SQLite cannot ALTER a
-- CHECK constraint in place, so rebuild the table (create-with-new-CHECK → copy →
-- drop → rename → recreate the index). roast_events has no triggers and nothing
-- references it by FK, so the swap is safe; column order/types are preserved
-- exactly. Observability-only event (SSE marker + persisted timeline); it is never
-- fed to the advisor or any safety/control path.
CREATE TABLE roast_events_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES roast_runs(id),
  kind TEXT NOT NULL CHECK (kind IN (
    'run_started', 'phase_changed', 'charge_guidance', 't0_detected',
    'drying_end', 'first_crack', 'advisory', 'command_executed', 'command_failed',
    'safety_alert', 'fault', 'recovery_required', 'recovery_acknowledged',
    'logs_exported', 'run_completed')),     -- models.RoastEventKind.value
  source TEXT NOT NULL CHECK (source IN (
    'controller', 'mcp', 'operator', 'advisor', 'safety')),
                                            -- models.RoastEventSource.value
  monotonic_seconds REAL,
  recorded_at_utc TEXT NOT NULL,
  payload_json TEXT
);
INSERT INTO roast_events_new
  (id, run_id, kind, source, monotonic_seconds, recorded_at_utc, payload_json)
  SELECT id, run_id, kind, source, monotonic_seconds, recorded_at_utc, payload_json
  FROM roast_events;
DROP TABLE roast_events;
ALTER TABLE roast_events_new RENAME TO roast_events;
CREATE INDEX idx_roast_events_run_kind ON roast_events(run_id, kind);
"""

SCHEMA_V7_ROASTED_WEIGHT = """
-- #388 (D42): the operator-entered roasted-OUT weight (grams), captured
-- post-roast after weighing. The green/charge weight is the frozen
-- RoastProfile.bean_weight_grams (the "in" side); this is the "out" side, so the
-- pair yields weight-loss % = (charge - roasted) / charge * 100 (predominantly
-- moisture but also dry-matter loss — CO2, volatiles, chaff — so NOT pure water
-- loss; computed on read, never stored, single source of truth).
--
-- Operator-editable on a completed run, the SAME lifecycle as operator_rating:
-- it is set after cooling/weighing. The v2 completed-run immutability trigger
-- guards a fixed column allow-list (it ABORTs only on the enumerated frozen
-- columns), so a NEW column is permitted to change after completion without
-- editing the shipped trigger. Nullable; pre-existing rows + un-weighed roasts
-- read back NULL (no weight-loss %).
ALTER TABLE roast_runs ADD COLUMN roasted_weight_grams REAL
  CHECK (roasted_weight_grams IS NULL OR roasted_weight_grams > 0);
"""

SCHEMA_V8_TURNING_POINT_EVENT = """
-- #409: add the pre-FC 'turning_point' event kind (the post-charge bean-temp minimum,
-- the tick RoR first crosses zero after the charge dip) to the roast_events.kind
-- CHECK. Mirrors the drying_end landmark (V6). SQLite cannot ALTER a CHECK in place,
-- so rebuild with the same create-copy-drop-rename pattern. Observability-only event
-- (SSE marker + persisted timeline); never fed to the advisor or safety/control path.
CREATE TABLE roast_events_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES roast_runs(id),
  kind TEXT NOT NULL CHECK (kind IN (
    'run_started', 'phase_changed', 'charge_guidance', 't0_detected',
    'turning_point', 'drying_end', 'first_crack', 'advisory',
    'command_executed', 'command_failed', 'safety_alert', 'fault',
    'recovery_required', 'recovery_acknowledged',
    'logs_exported', 'run_completed')),       -- models.RoastEventKind.value
  source TEXT NOT NULL CHECK (source IN (
    'controller', 'mcp', 'operator', 'advisor', 'safety')),
                                              -- models.RoastEventSource.value
  monotonic_seconds REAL,
  recorded_at_utc TEXT NOT NULL,
  payload_json TEXT
);
INSERT INTO roast_events_new
  (id, run_id, kind, source, monotonic_seconds, recorded_at_utc, payload_json)
  SELECT id, run_id, kind, source, monotonic_seconds, recorded_at_utc, payload_json
  FROM roast_events;
DROP TABLE roast_events;
ALTER TABLE roast_events_new RENAME TO roast_events;
CREATE INDEX idx_roast_events_run_kind ON roast_events(run_id, kind);
"""

SCHEMA_V9_AMBIENT = """
-- #342 (D85): mirror + store the MCP-owned ambient reading (temperature/
-- humidity/pressure from an optional Yoctopuce probe) captured ONCE at charge
-- (api._persist_ambient_if_charged). Read-only corpus metadata — nullable,
-- advisory/display-only: no safety gate, transition, or advisor context reads
-- these columns. NULL for a pre-existing row, an ambient-disabled/unavailable
-- MCP config, or a run that never charged. Written on an ACTIVE (not-yet-
-- completed) run, exactly once — like t0_detected_at_utc (SCHEMA_V3) — so it is
-- intentionally OUTSIDE the v2 completed-run immutability set: the immutability
-- trigger never needs to guard it.
ALTER TABLE roast_runs ADD COLUMN ambient_temp_c REAL;
ALTER TABLE roast_runs ADD COLUMN ambient_humidity_pct REAL;
ALTER TABLE roast_runs ADD COLUMN ambient_pressure_hpa REAL;
"""

SCHEMA_V10_AMBIENT_CAPTURED_LATCH = """
-- #463: derive the ambient charge-time capture latch from an EXPLICIT flag
-- rather than inferring "already captured" from ``ambient_temp_c IS NOT
-- NULL`` (SCHEMA_V9). That derivation has a narrow edge: a ``status='ok'``
-- MCP reading with a NULL temperature (not constructible today per the MCP
-- contract, but not ruled out either) would read back as "never captured"
-- and could re-fire the once-only capture post-restart, clobbering a good
-- corpus row. The explicit flag records that the capture WRITE ran, wholly
-- independent of whether the reading itself was null.
--
-- Written in the SAME statement as the ambient triad (``set_ambient``, the
-- single charge-time capture write) — set to 1 whenever the capture runs,
-- regardless of the reading. Existing rows default to 0 (never captured;
-- back-compat, same as a pre-existing NULL ambient_temp_c). Like the V9
-- ambient columns, this is written on an ACTIVE (not-yet-completed) run
-- exactly once, so it stays OUTSIDE the v2 completed-run immutability set —
-- the immutability trigger never needs to guard it.
ALTER TABLE roast_runs ADD COLUMN ambient_captured INTEGER NOT NULL DEFAULT 0
  CHECK (ambient_captured IN (0, 1));
"""

SCHEMA_V11_TASTINGS = """
-- #522 (D91): structured tasting entries — the E14 corpus starts now. A NEW
-- table, like bean_profiles (V4): no existing table or row is touched, so the
-- frozen roast_runs.profile_json snapshots and corpus integrity are unchanged.
-- Multiple rows per run are the whole point (a revisit tasting, e.g. roast 13's
-- same-evening "flat" refined to "grassy" hours later, is an ADDITIONAL row,
-- never an overwrite) — this is why the field lives in its own table rather
-- than as two more roast_runs columns on the rating-lifecycle allow-list.
-- Entirely outside the v2 completed-run immutability trigger: that trigger
-- guards UPDATE/DELETE on roast_runs only, and a tasting is always a fresh
-- INSERT into this table, so completed-run immutability is preserved by
-- construction, not by a trigger exception.
-- attributes_json / defects_json store the small controlled-vocabulary tag
-- lists (models.TastingAttribute / TastingDefect) as JSON arrays rather than
-- a normalized join table — matching the roast_events.payload_json precedent
-- for small optional structured data that is never queried/filtered on.
CREATE TABLE roast_tastings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES roast_runs(id),
  tasted_at_utc TEXT,                        -- operator-supplied; NULL if not given
  recorded_at_utc TEXT NOT NULL,              -- when this entry was saved
  stars INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
  notes TEXT,
  brew_method TEXT,
  grind_note TEXT,
  attributes_json TEXT,
  defects_json TEXT
);

CREATE INDEX idx_roast_tastings_run ON roast_tastings(run_id, id);
"""

SCHEMA_V12_CORRECTED_CHARGE = """
-- #520: the operator-entered CORRECTED charge/green weight, for the case
-- where the start-form default was left unedited but the operator actually
-- weighed a different amount (roast 13: charged 255 g, the form still had
-- the 250 g seed default). The frozen profile_json.bean_weight_grams stays
-- frozen — it is what the controller/advisor actually saw and ran with —
-- so the physical truth gets a NEW column rather than mutating the snapshot.
-- Same lifecycle as roasted_weight_grams (V7): the v2 completed-run
-- immutability trigger guards a fixed column allow-list (it ABORTs only on
-- the enumerated frozen columns), so a NEW column is permitted to change
-- after completion without editing the shipped trigger. Nullable; read
-- paths prefer this value over the frozen charge weight when present (see
-- read_run / list_runs), falling back to profile.bean_weight_grams when
-- NULL — an uncorrected run's derived weight-loss % is unaffected.
ALTER TABLE roast_runs ADD COLUMN corrected_charge_grams REAL
  CHECK (corrected_charge_grams IS NULL OR corrected_charge_grams > 0);
"""

SCHEMA_V13_EXCLUDED = """
-- #582: a reversible soft-exclude flag for a completed roast that produced BAD
-- DATA while the beans themselves were fine (e.g. a detector-missed first
-- crack the operator marked ~66 s / 7 degC late, so the derived DTR reads a
-- bogus value) -- the store is deliberately immutable (no delete-run feature;
-- a BEFORE DELETE trigger aborts; 10 child tables + FK enforcement), so this
-- is the clean way to get a bad-data roast out of history/stats/the learning
-- corpus WITHOUT deleting it -- the raw record, audio, FC-miss label, and BT
-- curve are all retained (a detector-miss is prime FC fine-tuning data).
--
-- Same lifecycle as roasted_weight_grams (V7) / corrected_charge_grams (V12):
-- the v2 completed-run immutability trigger guards a fixed column allow-list
-- (it ABORTs only on the enumerated frozen columns), so this NEW column is
-- permitted to change after completion -- including being TOGGLED BACK
-- (reversible by design) -- without editing the shipped trigger. Verified
-- empirically in test_store.py (test_excluded_flag_is_an_immutability_exception)
-- alongside a same-session proof that the trigger still ABORTS a real-field
-- update on a completed run (test_completed_runs_are_immutable) -- the new
-- column does not weaken immutability for any existing field.
--
-- Default 0 = every existing/new roast stays visible until explicitly
-- discarded (zero behavior change for the whole existing corpus on upgrade).
ALTER TABLE roast_runs ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0
  CHECK (excluded IN (0, 1));
"""

SCHEMA_V14_BEAN_SOURCING_ATTEMPTS = """
-- #588 / D119: durable runtime monitoring for every admitted URL-draft
-- attempt. Sensitive normalized draft values exist only during the bounded
-- operator review window and are cleared on claim/expiry.
CREATE TABLE bean_sourcing_attempts (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  model_slug TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  owner_instance_id TEXT NOT NULL,
  started_at_utc TEXT NOT NULL,
  lease_expires_at_utc TEXT NOT NULL,
  lease_expired_observed_at_utc TEXT,
  completed_at_utc TEXT,
  latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
  outcome TEXT NOT NULL CHECK (outcome IN (
    'in_progress', 'success', 'fetch_error', 'extraction_error',
    'provider_error', 'preempted', 'cancelled')),
  request_tokens INTEGER CHECK (request_tokens IS NULL OR request_tokens >= 0),
  response_tokens INTEGER CHECK (response_tokens IS NULL OR response_tokens >= 0),
  usage_evidence TEXT NOT NULL DEFAULT 'unknown'
    CHECK (usage_evidence IN ('exact', 'partial', 'unknown')),
  timed_out_runs INTEGER NOT NULL DEFAULT 0 CHECK (timed_out_runs >= 0),
  on_page_field_count INTEGER CHECK (
    on_page_field_count IS NULL OR on_page_field_count >= 0),
  origin_estimated_field_count INTEGER CHECK (
    origin_estimated_field_count IS NULL OR origin_estimated_field_count >= 0),
  draft_snapshot_json TEXT,
  claim_expires_at_utc TEXT,
  saved_profile_id TEXT REFERENCES bean_profiles(id),
  changed_fields_json TEXT,
  claimed_at_utc TEXT
);

CREATE INDEX idx_bean_sourcing_attempt_expiry
  ON bean_sourcing_attempts(claim_expires_at_utc)
  WHERE draft_snapshot_json IS NOT NULL AND saved_profile_id IS NULL;
"""

SCHEMA_V15_CATALOGUE_ATTEMPT_COUNTS = """
-- #573 / D121: successful catalogue attempts retain only bounded aggregate
-- discovery/extraction counts, never URLs, HTML, provider output, or result text.
ALTER TABLE bean_sourcing_attempts ADD COLUMN catalogue_discovered_count INTEGER
  CHECK (catalogue_discovered_count IS NULL OR
         catalogue_discovered_count BETWEEN 0 AND 24);
ALTER TABLE bean_sourcing_attempts ADD COLUMN catalogue_extracted_count INTEGER
  CHECK (catalogue_extracted_count IS NULL OR
         (catalogue_extracted_count BETWEEN 0 AND 12 AND
          catalogue_discovered_count IS NOT NULL AND
          catalogue_extracted_count <= catalogue_discovered_count));
"""

SCHEMA_V16_D96_VALIDATION_TRACE = """
-- #699 / D96: retain the controller-owned recovery-authority diagnostics used
-- to validate the dormant post-FC recovery law during supervised roasts.
ALTER TABLE telemetry_snapshots ADD COLUMN post_fc_recovery_enabled INTEGER
  CHECK (post_fc_recovery_enabled IS NULL OR post_fc_recovery_enabled IN (0, 1));
ALTER TABLE telemetry_snapshots ADD COLUMN post_fc_heat_authority_state TEXT
  CHECK (post_fc_heat_authority_state IS NULL OR
         post_fc_heat_authority_state IN ('holding', 'recovering', 'gliding'));
ALTER TABLE telemetry_snapshots ADD COLUMN post_fc_ror_setpoint_c_per_min REAL;
ALTER TABLE telemetry_snapshots ADD COLUMN post_fc_smoothed_ror_c_per_min REAL;
ALTER TABLE telemetry_snapshots ADD COLUMN post_fc_effective_heat_ceiling_percent INTEGER
  CHECK (post_fc_effective_heat_ceiling_percent IS NULL OR
         post_fc_effective_heat_ceiling_percent BETWEEN 0 AND 100);
"""

_BEAN_SOURCING_LEASE_DURATION = timedelta(minutes=2)
_BEAN_SOURCING_LEASE_CONFIRMATION = timedelta(seconds=60)

#: Ordered migration scripts; index+1 is the resulting PRAGMA user_version.
#: Append-only — never edit a shipped migration (plan §8: schema migration
#: is test-covered).
MIGRATIONS: tuple[str, ...] = (
    SCHEMA_V1,
    SCHEMA_V2_IMMUTABILITY,
    SCHEMA_V3_T0_DETECTED_AT,
    SCHEMA_V4_BEAN_PROFILES,
    SCHEMA_V5_CHARGE_ELAPSED,
    SCHEMA_V6_DRYING_END_EVENT,
    SCHEMA_V7_ROASTED_WEIGHT,
    SCHEMA_V8_TURNING_POINT_EVENT,
    SCHEMA_V9_AMBIENT,
    SCHEMA_V10_AMBIENT_CAPTURED_LATCH,
    SCHEMA_V11_TASTINGS,
    SCHEMA_V12_CORRECTED_CHARGE,
    SCHEMA_V13_EXCLUDED,
    SCHEMA_V14_BEAN_SOURCING_ATTEMPTS,
    SCHEMA_V15_CATALOGUE_ATTEMPT_COUNTS,
    SCHEMA_V16_D96_VALIDATION_TRACE,
)


class BeanProfileNotFoundError(Exception):
    """No active bean profile matches the id (#303).

    Raised by :meth:`RoastStore.update_bean_profile` /
    :meth:`RoastStore.delete_bean_profile` for an unknown or already-archived id;
    the API maps it to HTTP 404."""


class BeanDraftAttemptClaimError(Exception):
    """A draft attempt id cannot be attached to a new saved profile (#588)."""


class BeanDraftAttemptAlreadyClaimedError(BeanDraftAttemptClaimError):
    """A claimed draft was replayed with values unlike its saved profile."""


class PhysicallyImpossibleWeightError(Exception):
    """A weight/charge write would violate the roasted<=charge physical
    invariant (#520 round-2 P3), caught ATOMICALLY at the store layer.

    Raised by :meth:`RoastStore.set_roasted_weight` /
    :meth:`RoastStore.set_corrected_charge` when the UPDATE's own WHERE
    clause rejects the write against the CURRENT row (not a value read
    moments earlier by the caller) — closing the race where two concurrent
    corrections could each pass an API-layer pre-check against a stale
    snapshot. The API maps it to HTTP 409, the same status the API-layer
    pre-check already used."""


class RunActivelyDrivenError(Exception):
    """A :meth:`RoastStore.finalize_orphaned_run` target shows recent telemetry
    (#525 guard (c)) — some process is still ticking this run right now.

    This is the shared-durable-state liveness check that closes the
    multi-process gap a single process's own in-memory ``active_run_id``
    cannot see (a second process's live run can look stale from here, since
    ``health``/``active_run`` read the newest unfinalised row DB-wide, not any
    one process's pointer — see :meth:`RoastService.clear_stale_session`'s
    docstring for the full reasoning). The API maps it to HTTP 409 with a
    distinct "actively driven, do not clear" message — never the generic
    "already finalized" conflict, since the two failure modes call for
    opposite operator responses (already finalized: nothing to do; actively
    driven: verify the hardware / use emergency stop, do not clear)."""


class FrozenRunConfig(BaseModel):
    """Controller and safety configuration frozen when a roast starts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    controller: ControllerConfig
    safety: SafetyLimits


class PersistedRun(BaseModel):
    """The recovery read (E6-S3): what restart classification needs —
    feeds controller.recover_from_restart and run resumption context.
    Frozen: a point-in-time snapshot, never mutated downstream."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    agent_phase: RoastPhase
    outcome: Literal["completed", "aborted", "faulted"] | None
    started_at_utc: str
    completed_at_utc: str | None
    profile: RoastProfile
    #: The run-generation controller and safety configuration. Recovery must
    #: restore this pair rather than applying process-current next-roast edits
    #: to a roast that is already in progress.
    frozen_config: FrozenRunConfig | None = None
    #: Absolute UTC instant of the debounced charge/T0 transition (#235), or
    #: ``None`` when the run never charged or predates the v3 column. Recovery
    #: restores the advisory DTR clock from it; nothing safety-gating reads it.
    t0_detected_at_utc: str | None = None
    #: Whether the ambient triad was already persisted for this run pre-restart
    #: (#342, D85; explicit-flag fix #463) — ``True`` iff the ``ambient_captured``
    #: column is set, NOT derived from ``ambient_temp_c IS NOT NULL`` (a
    #: status='ok'-with-null-temperature capture must still latch). Recovery
    #: seeds the runner's once-only latch from this so a resumed
    #: already-charged run never re-captures (and potentially overwrites a good
    #: corpus reading with a transient post-restart probe hiccup). Corpus-only;
    #: nothing safety-gating reads it.
    ambient_captured: bool = False


class RoastStore:
    """aiosqlite-backed persistence and recovery reads.

    Durability bias per the orchestration plan: WAL journal with
    ``synchronous=FULL`` — commit per tick during active roasts (E6-S2)
    without forcing WAL checkpoints; power loss never costs committed
    ticks.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._connection: aiosqlite.Connection | None = None
        self._last_telemetry_elapsed: dict[str, float] = {}
        self._last_telemetry_d96_state: dict[
            str, tuple[bool | None, PostFcHeatAuthorityState | None, bool]
        ] = {}
        self._bean_profile_write_lock = asyncio.Lock()

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file."""
        return self._db_path

    @property
    def connection(self) -> aiosqlite.Connection:
        """The live connection (initialize() first)."""
        if self._connection is None:
            raise RuntimeError("store is not initialized")
        return self._connection

    async def initialize(self) -> None:
        """Open the database, set durability PRAGMAs, apply migrations."""
        if self._connection is not None:
            return
        connection = await aiosqlite.connect(self._db_path)
        # Name-keyed rows everywhere: the read projections map columns by name
        # (``row["col"]``), so adding or reordering a SELECT column never silently
        # shifts a positional ``row[N]`` index into the wrong field (#242). A
        # ``sqlite3.Row`` is still positionally indexable, so the single-column
        # PRAGMA reads below keep using ``row[0]``.
        connection.row_factory = aiosqlite.Row
        try:
            async with connection.execute("PRAGMA journal_mode=WAL") as cursor:
                mode_row = await cursor.fetchone()
            if mode_row is None or str(mode_row[0]).lower() != "wal":  # pragma: no cover
                # Environment-dependent path: WAL activation only fails on
                # filesystems that cannot support it (e.g. some NFS mounts);
                # not reproducible on local/CI filesystems.
                raise RuntimeError(
                    f"could not activate WAL journal mode (got {mode_row}): "
                    f"the durability bias requires it"
                )
            await connection.execute("PRAGMA synchronous=FULL")
            await connection.execute("PRAGMA foreign_keys=ON")
            await self._apply_migrations(connection)
        except BaseException:
            # Never leak the connection (and its worker thread) on a
            # failed open/migration — found when the rollback test hung
            # pytest exit on the leaked aiosqlite thread.
            await connection.close()
            raise
        self._connection = connection

    async def _apply_migrations(self, connection: aiosqlite.Connection) -> None:
        async with connection.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        assert row is not None  # PRAGMA user_version always returns one row
        version = int(row[0])
        for number, script in enumerate(MIGRATIONS, start=1):
            if version < number:
                # Transaction-opening BEGIN only — trigger bodies use
                # BEGIN…END legitimately (e.g. the v2 immutability triggers).
                if re.search(
                    r"\bBEGIN\s*(;|TRANSACTION|DEFERRED|IMMEDIATE|EXCLUSIVE)",
                    script,
                    re.IGNORECASE,
                ):
                    raise ValueError(
                        f"migration {number} embeds its own transaction — "
                        f"_apply_migrations owns BEGIN/COMMIT"
                    )
                # One explicit transaction per migration: SQLite DDL and
                # PRAGMA user_version are both transactional, so a crash
                # or failure anywhere rolls back to the previous version
                # cleanly — never half-applied DDL with a stale version
                # (review finding, E6-S1 PR). Migration scripts must not
                # contain their own BEGIN/COMMIT.
                await connection.executescript(
                    f"BEGIN;\n{script}\nPRAGMA user_version={number};\nCOMMIT;"
                )

    async def schema_version(self) -> int:
        """The applied schema version (PRAGMA user_version)."""
        async with self.connection.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        assert row is not None  # PRAGMA user_version always returns one row
        return int(row[0])

    async def close(self) -> None:
        """Close the connection (safe to call when never initialized)."""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    # --- E6-S2: write paths ---
    #
    # Every writer commits immediately: "commit per tick during active
    # roasts" with no forced WAL checkpoint anywhere — SQLite checkpoints
    # WAL automatically; durability comes from synchronous=FULL.

    async def create_run(
        self,
        *,
        run_id: str,
        profile: RoastProfile,
        config: AppConfig,
        agent_phase: RoastPhase,
        started_at_utc: str | None = None,
    ) -> None:
        """Insert the roast_runs row (the FK parent for everything else).

        The profile and config are frozen as JSON at run start (plan §5).
        """
        now = started_at_utc or _utc_now()
        frozen_config = json.dumps(
            {
                "controller": self._dump(config.controller),
                "safety": self._dump(config.safety),
            },
            sort_keys=True,
        )
        await self.connection.execute(
            "INSERT INTO roast_runs (id, agent_phase, profile_json, config_json,"
            " started_at_utc, created_at_utc, updated_at_utc)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                agent_phase.value,
                profile.model_dump_json(),
                frozen_config,
                now,
                now,
                now,
            ),
        )
        await self.connection.commit()

    async def count_completed_runs_for_origin(self, origin_slug: str) -> int:
        """Count prior *completed* roasts whose origin slug matches (#385).

        The per-origin recording roast number (the MCP ``set_recording_metadata``
        filename counter) is derived as ``this count + 1`` so numbering is stable
        and meaningful across agent restarts, replacing the per-process counter
        that reset to 0 each restart (colliding two roasts of the same bean).

        "Completed" means ``completed_at_utc IS NOT NULL`` — a finalised run,
        whatever its outcome (a faulted-but-finalised roast still consumed a
        recording slot). The active run being started is excluded by definition (it
        is not yet completed). The origin is derived from each completed run's
        frozen ``profile_json`` via the shared :func:`recording_origin_slug`, the
        same slug the controller hands the MCP, so the count and the filename agree.

        Deliberately does NOT filter on ``roast_runs.excluded`` (#582): this
        count feeds a recording FILENAME slot number, not history/stats/corpus
        presentation. A soft-discarded run's audio file still physically
        exists on disk under the slot number it was recorded with (the whole
        point of #582 is that nothing is deleted) — excluding it here would
        double-assign that slot to the next real roast of the same bean and
        collide with the discarded run's still-present recording.

        Args:
            origin_slug: The recording-origin slug to match (from
                :func:`recording_origin_slug` on the new run's profile).

        Returns:
            The number of completed runs with the same origin slug (``0`` when this
            is the first roast of the bean).
        """
        cursor = await self.connection.execute(
            "SELECT profile_json FROM roast_runs WHERE completed_at_utc IS NOT NULL"
        )
        rows = await cursor.fetchall()
        count = 0
        for row in rows:
            try:
                profile = RoastProfile.model_validate_json(str(row["profile_json"]))
            except ValueError:  # pragma: no cover - a frozen profile is always valid
                continue
            if recording_origin_slug(profile) == origin_slug:
                count += 1
        return count

    async def update_run_phase(self, run_id: str, agent_phase: RoastPhase) -> None:
        """Persist the last-known agent phase (the recovery read, E6-S3).

        A missing run is a programming error and raises — a silent no-op
        here would corrupt the restart-recovery breadcrumb.
        """
        cursor = await self.connection.execute(
            "UPDATE roast_runs SET agent_phase = ?, updated_at_utc = ? WHERE id = ?",
            (agent_phase.value, _utc_now(), run_id),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            raise RuntimeError(f"no roast_run with id {run_id!r}")

    async def record_t0_detected_at(
        self, run_id: str, t0_detected_at_utc: str | None = None
    ) -> None:
        """Persist the absolute charge/T0 instant for restart recovery (#235).

        Written once, when the controller first stamps its charge clock (the
        debounced T0 transition into pre-first-crack), so the persisted instant
        is the detection tick's wall-clock time. On a later
        restart→operator-resume the recovery read restores the advisor's
        charge-referenced DTR clock from it, so the DTR denominator survives
        instead of resetting to ``0.0``.

        Advisory/display-only: no safety gate reads ``t0_detected_at_utc`` (the
        safety policy keys on temperature, never DTR). A missing run is a
        programming error and raises, like :meth:`update_run_phase` — a silent
        no-op would lose the recovery breadcrumb.

        Write-once guard (defence in depth, #260): the UPDATE is scoped to
        ``WHERE t0_detected_at_utc IS NULL`` so the first persisted charge
        instant wins at the storage layer regardless of caller discipline. The
        controller's ``_t0_persisted`` flag and the recover-seed already prevent
        any double-write in live paths, so this is behaviour-preserving for the
        sole current caller (which always writes on the first charged tick, when
        the column is still NULL); the guard protects a future second caller
        from clobbering a recovered T0.

        Args:
            run_id: The active run whose charge instant is being stamped.
            t0_detected_at_utc: ISO-8601 UTC timestamp of the charge/T0 detection;
                defaults to now (the same-tick detection instant).
        """
        charged_at = t0_detected_at_utc or _utc_now()
        cursor = await self.connection.execute(
            "UPDATE roast_runs SET t0_detected_at_utc = ?, updated_at_utc = ?"
            " WHERE id = ? AND t0_detected_at_utc IS NULL",
            (charged_at, _utc_now(), run_id),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            # Either the run does not exist (a programming error) or T0 is
            # already persisted (the write-once guard fired). Distinguish the
            # two so a missing run still raises but a guarded second write is a
            # silent no-op (first-write-wins).
            exists_cursor = await self.connection.execute(
                "SELECT 1 FROM roast_runs WHERE id = ?", (run_id,)
            )
            row = await exists_cursor.fetchone()
            if row is None:
                raise RuntimeError(f"no roast_run with id {run_id!r}")

    async def set_ambient(
        self,
        run_id: str,
        *,
        temperature_c: float | None,
        humidity_percent: float | None,
        pressure_hpa: float | None,
    ) -> None:
        """Persist the ambient reading captured once at charge (#342, D85).

        The MCP owns ambient (temperature/humidity/pressure, an optional
        Yoctopuce probe); this stores a single reading snapshot on
        ``roast_runs`` for the corpus. Read-only corpus metadata — no safety
        gate, transition, or advisor context ever reads these columns (fail-soft
        by design, mirroring the MCP's own ``AmbientStatus``: an unavailable /
        disabled probe persists ``None`` for all three fields rather than
        raising).

        Written once, on the ACTIVE run, the first tick the runner observes the
        charge/T0 transition (mirroring :meth:`record_t0_detected_at`'s
        lifecycle) — the *caller* (``api._persist_ambient_if_charged``) owns the
        once-only latch. In the same statement this also sets the explicit
        ``ambient_captured`` flag (SCHEMA_V10, #463) to ``1`` — the capture RAN,
        regardless of whether the reading itself is null, so a
        ``status='ok'``-with-null-temperature capture still latches and cannot
        re-fire post-restart. (``ambient_temp_c IS NOT NULL`` alone cannot serve
        as that "already captured" sentinel, since a null reading is itself a
        valid persisted value.) A missing run is a programming error and
        raises, like :meth:`record_t0_detected_at`.

        Args:
            run_id: The active run whose ambient reading is being stamped.
            temperature_c: Ambient temperature in Celsius, or ``None`` when the
                probe is unavailable/disabled.
            humidity_percent: Ambient relative humidity percentage, or ``None``.
            pressure_hpa: Ambient barometric pressure in hectopascals, or
                ``None``.
        """
        cursor = await self.connection.execute(
            "UPDATE roast_runs SET ambient_temp_c = ?, ambient_humidity_pct = ?,"
            " ambient_pressure_hpa = ?, ambient_captured = 1, updated_at_utc = ?"
            " WHERE id = ?",
            (temperature_c, humidity_percent, pressure_hpa, _utc_now(), run_id),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            raise RuntimeError(f"no roast_run with id {run_id!r}")

    async def record_event(
        self,
        *,
        run_id: str,
        kind: RoastEventKind,
        source: RoastEventSource,
        monotonic_seconds: float | None = None,
        payload: object = None,
        recorded_at_utc: str | None = None,
    ) -> None:
        """Append one agent-level event (plan §5 roast_events)."""
        await self.connection.execute(
            "INSERT INTO roast_events"
            " (run_id, kind, source, monotonic_seconds, recorded_at_utc, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                kind.value,
                source.value,
                monotonic_seconds,
                recorded_at_utc or _utc_now(),
                None if payload is None else json.dumps(payload, sort_keys=True),
            ),
        )
        await self.connection.commit()

    async def record_telemetry(
        self,
        *,
        run_id: str,
        tick: int,
        agent_phase: RoastPhase,
        elapsed_seconds: float,
        interval_seconds: float,
        telemetry: RoastTelemetry | None,
        mcp_phase: str | None = None,
        heat_level_percent: int | None = None,
        fan_level_percent: int | None = None,
        development_percent: float | None = None,
        charge_elapsed_seconds: float | None = None,
        post_fc_recovery_enabled: bool | None = None,
        post_fc_heat_authority_state: PostFcHeatAuthorityState | None = None,
        post_fc_ror_setpoint_c_per_min: float | None = None,
        post_fc_smoothed_ror_c_per_min: float | None = None,
        post_fc_effective_heat_ceiling_percent: int | None = None,
        raw_state_json: str | None = None,
    ) -> bool:
        """Persist a periodic telemetry row or a D96 authority transition.

        The controller persists every tick; rows are only inserted every
        ``telemetry_log_interval_seconds`` (plan §5, default 5 s), except that
        a change in the resolved D96 flag or authority state always writes so a
        short recovery/glide cycle cannot disappear between periodic samples.
        Returns whether a row was written. The first row of a run always writes.
        """
        last = self._last_telemetry_elapsed.get(run_id)
        # DEVELOPMENT owns live D96 authority. Persist entry/exit of that
        # ownership domain even when a same-tick historical witness repeats the
        # prior authority value; otherwise the periodic throttle can move the
        # phase boundary a tick late and overstate validation durations.
        d96_state = (
            post_fc_recovery_enabled,
            post_fc_heat_authority_state,
            agent_phase is RoastPhase.DEVELOPMENT,
        )
        d96_state_changed = (
            run_id in self._last_telemetry_d96_state
            and self._last_telemetry_d96_state[run_id] != d96_state
        )
        if (
            last is not None
            and (elapsed_seconds - last) < interval_seconds
            and not d96_state_changed
        ):
            return False
        await self.connection.execute(
            "INSERT INTO telemetry_snapshots"
            " (run_id, tick, recorded_at_utc, elapsed_seconds, agent_phase, mcp_phase,"
            "  bean_temp_c, env_temp_c, bean_ror_c_per_min, env_ror_c_per_min,"
            "  heat_level_percent, fan_level_percent, cooling_on, development_percent,"
            "  charge_elapsed_seconds, post_fc_recovery_enabled,"
            "  post_fc_heat_authority_state, post_fc_ror_setpoint_c_per_min,"
            "  post_fc_smoothed_ror_c_per_min,"
            "  post_fc_effective_heat_ceiling_percent, raw_state_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                tick,
                _utc_now(),
                elapsed_seconds,
                agent_phase.value,
                mcp_phase,
                None if telemetry is None else telemetry.bean_temp_c,
                None if telemetry is None else telemetry.env_temp_c,
                None if telemetry is None else telemetry.bean_ror_c_per_min,
                None if telemetry is None else telemetry.env_ror_c_per_min,
                heat_level_percent,
                fan_level_percent,
                None if telemetry is None else int(telemetry.cooling_on),
                development_percent,
                charge_elapsed_seconds,
                None if post_fc_recovery_enabled is None else int(post_fc_recovery_enabled),
                (
                    None
                    if post_fc_heat_authority_state is None
                    else post_fc_heat_authority_state.value
                ),
                post_fc_ror_setpoint_c_per_min,
                post_fc_smoothed_ror_c_per_min,
                post_fc_effective_heat_ceiling_percent,
                raw_state_json,
            ),
        )
        await self.connection.commit()
        self._last_telemetry_elapsed[run_id] = elapsed_seconds
        self._last_telemetry_d96_state[run_id] = d96_state
        return True

    async def record_safety_evaluation(
        self,
        *,
        run_id: str,
        tick: int,
        evaluation: SafetyEvaluation,
    ) -> int:
        """Persist a SafetyEvaluation; returns the row id for linking
        advisor decisions and commands to their verdict."""
        cursor = await self.connection.execute(
            "INSERT INTO safety_evaluations"
            " (run_id, tick, rule, verdict, input_heat, input_fan,"
            "  adjusted_heat, adjusted_fan, reason, recorded_at_utc)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                tick,
                evaluation.rule,
                evaluation.verdict.value,
                evaluation.input_heat,
                evaluation.input_fan,
                evaluation.adjusted_heat,
                evaluation.adjusted_fan,
                evaluation.reason,
                _utc_now(),
            ),
        )
        await self.connection.commit()
        if cursor.lastrowid is None:  # pragma: no cover — SQLite guarantees it
            raise RuntimeError("INSERT into safety_evaluations returned no lastrowid")
        return cursor.lastrowid

    async def record_advisor_decision(
        self,
        *,
        run_id: str,
        tick: int,
        provider: str,
        model: str,
        prompt_version: str,
        context: AdvisorContext,
        latency_ms: int | None,
        decision: RoastDecision | None,
        status: Literal["ok", "timeout", "malformed", "provider_error"],
        safety_evaluation_id: int | None = None,
    ) -> None:
        """Persist an advisory outcome — context as a hash, never raw
        (plan policy: log prompt input hashes, not large payloads)."""
        context_hash = hashlib.sha256(context.model_dump_json().encode()).hexdigest()
        await self.connection.execute(
            "INSERT INTO advisor_decisions"
            " (run_id, tick, provider, model, prompt_version, context_hash,"
            "  latency_ms, decision_json, status, safety_evaluation_id, recorded_at_utc)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                tick,
                provider,
                model,
                prompt_version,
                context_hash,
                latency_ms,
                None if decision is None else decision.model_dump_json(),
                status,
                safety_evaluation_id,
                _utc_now(),
            ),
        )
        await self.connection.commit()

    async def record_command(
        self,
        *,
        run_id: str,
        tick: int,
        tool: RoastCommand,
        source: Literal["policy", "advisor", "operator", "safety", "recovery"],
        status: Literal["ok", "failed"],
        args: object = None,
        result: object = None,
        safety_evaluation_id: int | None = None,
    ) -> None:
        """Append one executed/failed MCP command (the decision trace)."""
        await self.connection.execute(
            "INSERT INTO command_log"
            " (run_id, tick, tool, args_json, source, safety_evaluation_id,"
            "  status, result_json, recorded_at_utc)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                tick,
                tool.value,
                None if args is None else json.dumps(args, sort_keys=True),
                source,
                safety_evaluation_id,
                status,
                None if result is None else json.dumps(result, sort_keys=True),
                _utc_now(),
            ),
        )
        await self.connection.commit()

    async def record_operator_action(
        self,
        *,
        action: str,
        result: Literal["accepted", "rejected", "failed"],
        run_id: str | None = None,
        payload: object = None,
    ) -> None:
        """Append one operator action (run_id nullable: actions like
        emergency stop may precede any run record)."""
        await self.connection.execute(
            "INSERT INTO operator_actions (run_id, action, payload_json, result,"
            " recorded_at_utc) VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                action,
                None if payload is None else json.dumps(payload, sort_keys=True),
                result,
                _utc_now(),
            ),
        )
        await self.connection.commit()

    @staticmethod
    def _dump(model: BaseModel) -> dict[str, object]:
        return model.model_dump(mode="json")

    @staticmethod
    def _optional_float(value: object) -> float | None:
        """Project one nullable numeric SQLite value as a finite float or ``None``."""
        if value is None:
            return None
        converted = float(cast("float", value))
        return converted if math.isfinite(converted) else None

    @staticmethod
    def _reference_onset_pair(
        candidates: list[object],
        started_at_utc: object,
        fc_at: object,
        clock_rows: list[tuple[aiosqlite.Row, float]],
        usable_rows: list[aiosqlite.Row],
        first_dev: aiosqlite.Row,
    ) -> tuple[float, float] | None:
        """Return a complete safe onset landmark pair, or ``None`` to fall back."""
        onset = earliest_onset_within_event_window(candidates, started_at_utc, fc_at)
        if onset is None:
            return None
        clock_seconds = [seconds for _, seconds in clock_rows]
        if any(
            current <= previous
            for previous, current in zip(clock_seconds, clock_seconds[1:], strict=False)
        ):
            return None
        anchors = [(row["recorded_at_utc"], seconds) for row, seconds in clock_rows]
        mapped = utc_to_run_seconds(onset.isoformat(), anchors)
        first_t = RoastStore._optional_float(usable_rows[0]["charge_elapsed_seconds"])
        last_t = RoastStore._optional_float(usable_rows[-1]["charge_elapsed_seconds"])
        first_dev_t = RoastStore._optional_float(first_dev["charge_elapsed_seconds"])
        if (
            mapped is None
            or first_t is None
            or last_t is None
            or first_dev_t is None
            or mapped < first_t
            or mapped > last_t
            or mapped > first_dev_t
        ):
            return None
        temperature = interpolate_at(
            mapped,
            [(row["charge_elapsed_seconds"], row["bean_temp_c"]) for row in usable_rows],
        )
        return None if temperature is None else (mapped, temperature)

    # --- E6-S3: recovery reads, run completion, immutability exceptions ---

    async def read_latest_run(self) -> PersistedRun | None:
        """The startup recovery read (orchestration plan § Persistence):
        the most recent run with its last persisted phase. None on a
        fresh database."""
        async with self.connection.execute(
            "SELECT id, agent_phase, outcome, started_at_utc, completed_at_utc,"
            " t0_detected_at_utc, ambient_captured, profile_json, config_json"
            " FROM roast_runs"
            " ORDER BY started_at_utc DESC, rowid DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return PersistedRun(
            run_id=str(row["id"]),
            agent_phase=RoastPhase(str(row["agent_phase"])),
            outcome=row["outcome"],  # CHECK-constrained; None when still active
            started_at_utc=str(row["started_at_utc"]),
            completed_at_utc=None
            if row["completed_at_utc"] is None
            else str(row["completed_at_utc"]),
            t0_detected_at_utc=None
            if row["t0_detected_at_utc"] is None
            else str(row["t0_detected_at_utc"]),
            # #463: the explicit flag (SCHEMA_V10), not ``ambient_temp_c IS NOT
            # NULL`` — a captured-but-null reading must still latch.
            ambient_captured=bool(row["ambient_captured"]),
            profile=RoastProfile.model_validate_json(str(row["profile_json"])),
            frozen_config=FrozenRunConfig.model_validate_json(str(row["config_json"])),
        )

    async def complete_run(
        self,
        *,
        run_id: str,
        outcome: Literal["completed", "aborted", "faulted"],
        agent_phase: RoastPhase,
        fault_reason: str | None = None,
        log_dir: str | None = None,
        export_manifest: object = None,
    ) -> None:
        """Finalize a run; from this point the immutability triggers guard
        everything except the operator/cloud fields."""
        now = _utc_now()  # one instant: completed_at == updated_at at completion
        cursor = await self.connection.execute(
            "UPDATE roast_runs SET completed_at_utc = ?, outcome = ?, agent_phase = ?,"
            " fault_reason = ?, log_dir = ?, export_manifest_json = ?, updated_at_utc = ?"
            " WHERE id = ?",
            (
                now,
                outcome,
                agent_phase.value,
                fault_reason,
                log_dir,
                None if export_manifest is None else json.dumps(export_manifest, sort_keys=True),
                now,
                run_id,
            ),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            raise RuntimeError(f"no roast_run with id {run_id!r}")

    async def finalize_stale_faulted_run(self, run_id: str) -> None:
        """Terminally finalise a prior-session unfinalised FAULTED run (#331).

        On restart, a previous session's faulted run that the operator never
        acknowledged is still ``completed_at_utc IS NULL`` — which ``active_run``
        treats as live, so it is restored as the ACTIVE run and blocks the UI from
        starting a fresh roast. This stamps ``completed_at`` with outcome
        ``faulted`` so the stale run lands in HISTORY (terminal) instead of being
        recovered as active — a fresh boot is then clean/idle.

        Distinct from :meth:`complete_run`: it deliberately does NOT touch
        ``fault_reason`` (or any diagnostic field) — the reason persisted when the
        run first faulted last session is PRESERVED for diagnosis. It only flips
        the run terminal; ``agent_phase`` stays ``faulted``.

        The UPDATE is GUARDED to a genuinely-faulted, unfinalised run
        (``agent_phase = 'faulted' AND completed_at_utc IS NULL``) so the method
        can never terminalise an ACTIVE non-faulted run — it matches its name /
        contract, and accidental misuse raises rather than silently corrupting a
        live roast. A non-matching id/phase touches no row and raises.

        This is a STORE write only — it resumes nothing, issues no MCP write, and
        does not touch heat/fan (the restart-never-auto-resumes invariant is about
        actuation, untouched here).
        """
        now = _utc_now()  # one instant: completed_at == updated_at at finalisation
        cursor = await self.connection.execute(
            "UPDATE roast_runs SET completed_at_utc = ?, outcome = 'faulted',"
            " updated_at_utc = ? WHERE id = ? AND completed_at_utc IS NULL"
            " AND agent_phase = ?",
            (now, now, run_id, RoastPhase.FAULTED.value),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            raise RuntimeError(f"no unfinalized FAULTED roast_run with id {run_id!r}")

    async def finalize_orphaned_run(self, run_id: str, *, recency_window_seconds: float) -> None:
        """Terminally finalise an operator-cleared STALE run (#525), or raise.

        Generalises :meth:`finalize_stale_faulted_run` for the OPERATOR-invoked
        case: any unfinalised phase (not just ``faulted``) that the caller has
        already established is not its own tracked active run (guard (a), the
        service layer's ``run_id != self.active_run_id`` check — this method
        has no visibility into that and does not re-check it). Stamps
        ``completed_at_utc`` / ``outcome = 'aborted'`` / ``updated_at_utc`` in
        one instant; ``agent_phase`` and ``fault_reason`` are left UNTOUCHED,
        mirroring :meth:`finalize_stale_faulted_run`'s preserve-diagnosis
        rationale — this finalises an already-abandoned run, it does not
        reclassify what happened during it.

        Two independent atomicity guards, both evaluated against the CURRENT
        row at the instant of the write (never a value read moments earlier):

        1. ``completed_at_utc IS NULL`` in the UPDATE's own WHERE clause (#525
           guard (b)) — a concurrent finalize (another clear, a legitimate
           completion) loses the race cleanly: this call's rowcount is 0 and
           it raises, never silently re-finalising an already-terminal row.
        2. Guard (c) is a TWO-CLAUSE liveness gate (recent WRITE-recency OR
           recent START-recency), safety-reviewer PASS-WITH-CONDITIONS +
           PR #548 round-1 P1 fold — closes the gap guard (a) structurally
           cannot: a single process can only ever attest to its OWN
           active-run pointer, never to whether some OTHER process is
           live-driving this exact run_id.

           2a. A ``NOT EXISTS`` telemetry-recency subquery: a live roast
               persists a telemetry row on every controller tick (throttled
               only by ``telemetry_log_interval_seconds``, never by phase),
               so ANY row within the EFFECTIVE window (see ``Args`` — PR
               #548 round-2 P1: derived from the run's OWN frozen config,
               never just the answering process's, since the two can
               genuinely differ in the exact cross-process case this whole
               gate exists for) of now is durable, shared-DB evidence that
               some process is actively ticking this run.
           2b. ``started_at_utc <= threshold`` (the P1 fold): clause 2a alone
               has a real hole in the window BEFORE the first tick.
               ``RoastRunner.start()`` drives ``controller.start_run()`` —
               which issues the profile's initial heat/fan through the
               safety policy — and returns BEFORE ``run()``'s scheduler ever
               calls ``tick_once()`` (the sole caller of
               ``_publish_and_persist_telemetry``). So a run created moments
               ago can have heat/fan ACTIVELY COMMANDED with ZERO telemetry
               rows — clause 2a alone would pass ``NOT EXISTS`` and let an
               impostor process finalise a row whose hardware is being
               driven right now. Requiring the run to ALSO be older than the
               SAME recency window closes this: a genuinely stale orphan is
               by definition minutes old, so this costs a real orphan
               nothing, while a just-started run (regardless of whether its
               first tick has landed yet) is refused with the same
               ``RunActivelyDrivenError`` / "actively driven" 409 as clause
               2a. Uses the IDENTICAL ``threshold`` string as 2a (same
               format-contract constraint applies) — not a separate window,
               so there is only one recency budget to reason about.

           Together: this generalises "MCP-state gate: never clear while the
           machine could be hot" (#525's original ask) into an honest
           DB-liveness check across the run's WHOLE unfinalised lifetime, not
           just from tick 1 onward — an own-process MCP-idle read cannot
           observe a DIFFERENT process's roaster, so it would be a no-op at
           best and misleading at worst; recent telemetry OR a recent start
           is the one signal any process can trust regardless of who is
           driving the run.

        This is a STORE write only — it resumes nothing, issues no MCP write,
        and never touches heat/fan (the restart-never-auto-resumes invariant
        is about actuation, untouched here).

        Args:
            run_id: The stranded run to finalise.
            recency_window_seconds: The ANSWERING process's own recency
                window (scaled against ITS configured
                ``telemetry_log_interval_seconds`` — never a bare constant).
                This is a fail-closed FLOOR, not necessarily the effective
                window: PR #548 round-2 P1 — the answering process may not
                be the run's OWNER, and the two processes' configured
                ``telemetry_log_interval_seconds`` can genuinely differ (the
                exact cross-process case this whole gate exists for). The
                EFFECTIVE window used for both clauses is
                ``max(recency_window_seconds, the run's OWN frozen interval ×
                4, 20.0)`` — read from the target run's own frozen
                ``config_json`` (immutable once written; no race to guard
                against, unlike ``completed_at_utc``/telemetry). Taking the
                LARGER of the two is the fail-closed direction: a wider
                window only makes the clear HARDER, never easier, so an
                impostor process with a shorter-interval config can never
                use its own narrower window to slip past an owner's
                genuinely slower cadence. Falls back to
                ``recency_window_seconds`` alone if the run predates the
                ``config_json``/``telemetry_log_interval_seconds`` key (not
                reachable in practice — the column is ``NOT NULL`` since
                schema v1 — but a missing key degrades safely rather than
                raising).

        Raises:
            RuntimeError: No unfinalised run matches ``run_id`` (unknown id,
                or it was already finalised — guard (b)).
            RunActivelyDrivenError: A telemetry row exists inside the
                EFFECTIVE recency window (clause 2a), OR the run started
                within it (clause 2b, the pre-first-tick P1 fold) — guard
                (c): some process is still driving this run.
        """
        row_cursor = await self.connection.execute(
            "SELECT json_extract(config_json, '$.controller.telemetry_log_interval_seconds')"
            " FROM roast_runs WHERE id = ?",
            (run_id,),
        )
        interval_row = await row_cursor.fetchone()
        owner_interval = None if interval_row is None else interval_row[0]
        # The run's OWN window, mirroring the answering process's own
        # max(floor, 4x interval) scaling exactly (RoastService's
        # _stale_session_recency_window_seconds) — computed here so a run
        # frozen with a slower cadence gets the SAME margin an owning
        # process would apply to itself, regardless of who is asking.
        owner_window_seconds = (
            None if owner_interval is None else max(20.0, 4.0 * float(owner_interval))
        )
        effective_window_seconds = (
            recency_window_seconds
            if owner_window_seconds is None
            else max(recency_window_seconds, owner_window_seconds)
        )
        # Format contract (safety-reviewer note): this must stay
        # datetime.now(UTC).isoformat() (a "+00:00" offset, matching
        # _utc_now()'s telemetry-write format) — a "Z"-suffixed or naive
        # datetime would silently break the lexicographic TEXT comparison
        # below (SQLite has no datetime type; every ">"/"<=" here is a plain
        # string compare, correct ONLY because every writer — telemetry AND
        # roast_runs.started_at_utc — uses this exact form). The SAME
        # threshold string backs both clause 2a and clause 2b (one recency
        # budget, not two independently-drifting windows.
        threshold = (datetime.now(UTC) - timedelta(seconds=effective_window_seconds)).isoformat()
        now = _utc_now()  # one instant: completed_at == updated_at at finalisation
        cursor = await self.connection.execute(
            "UPDATE roast_runs SET completed_at_utc = ?, outcome = 'aborted',"
            " updated_at_utc = ? WHERE id = ? AND completed_at_utc IS NULL"
            # Clause 2b (P1 fold): a run started WITHIN the recency window is
            # refused regardless of telemetry — closes the pre-first-tick gap.
            " AND started_at_utc <= ?"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM telemetry_snapshots"
            "   WHERE telemetry_snapshots.run_id = roast_runs.id"
            "   AND telemetry_snapshots.recorded_at_utc > ?"
            " )",
            (now, now, run_id, threshold, threshold),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            # Disambiguate: already finalized / unknown id (RuntimeError) vs.
            # blocked by guard (c)'s recency check — either clause 2a or 2b
            # (RunActivelyDrivenError) — a follow-up read against the row the
            # WHERE clause actually saw.
            completed_cursor = await self.connection.execute(
                "SELECT completed_at_utc FROM roast_runs WHERE id = ?", (run_id,)
            )
            completed_row = await completed_cursor.fetchone()
            if completed_row is None or completed_row["completed_at_utc"] is not None:
                raise RuntimeError(f"no unfinalized roast_run with id {run_id!r}")
            raise RunActivelyDrivenError(
                f"roast_run {run_id!r} has telemetry within the last "
                f"{effective_window_seconds:.0f}s — some process is actively driving it"
            )

    async def set_operator_rating(
        self, run_id: str, *, rating: Literal[1, 2, 3, 4, 5], notes: str | None = None
    ) -> None:
        """Operator self-rating — one of the explicit immutability
        exceptions on completed runs (plan §5). Completed runs only: the
        store enforces the contract so E7 never has to (an in-progress
        run cannot be silently stamped with a rating)."""
        cursor = await self.connection.execute(
            "UPDATE roast_runs SET operator_rating = ?, operator_notes = ?,"
            " updated_at_utc = ? WHERE id = ? AND completed_at_utc IS NOT NULL",
            (rating, notes, _utc_now(), run_id),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            raise RuntimeError(f"no completed roast_run with id {run_id!r}")

    async def set_roasted_weight(self, run_id: str, *, roasted_weight_grams: float) -> None:
        """Operator-entered roasted-out weight (#388) — completed runs only.

        The same immutability-exception lifecycle as :meth:`set_operator_rating`:
        captured post-roast after weighing, guarded to completed runs so an
        in-progress run cannot be stamped. The weight-loss % is derived on read,
        never stored.

        The roasted weight must not exceed the EFFECTIVE charge (the
        corrected charge when present, else the frozen profile default) —
        folded into the UPDATE's own ``WHERE`` clause (#520 round-2 P3) so the
        bound is checked ATOMICALLY against the row's CURRENT state, not a
        value the caller read moments earlier: two concurrent corrections
        (this call and a racing :meth:`set_corrected_charge`) could otherwise
        each pass an API-layer pre-check against a stale snapshot. A
        zero-``rowcount`` UPDATE is then disambiguated by a follow-up read —
        "unknown/in-progress" (:class:`RuntimeError`) vs. "physically
        impossible against the current row" (:class:`PhysicallyImpossibleWeightError`).

        Args:
            run_id: The completed run to update.
            roasted_weight_grams: The roasted-out weight in grams (> 0; the API
                model enforces the bound before this is called — this is the
                atomic BACKSTOP, not the primary UX feedback path).

        Raises:
            RuntimeError: The run is unknown or still in progress.
            PhysicallyImpossibleWeightError: The write would exceed the
                run's current effective charge weight.
        """
        cursor = await self.connection.execute(
            "UPDATE roast_runs SET roasted_weight_grams = ?, updated_at_utc = ?"
            " WHERE id = ? AND completed_at_utc IS NOT NULL"
            " AND ? <= COALESCE(corrected_charge_grams,"
            "   json_extract(profile_json, '$.bean_weight_grams'))",
            (roasted_weight_grams, _utc_now(), run_id, roasted_weight_grams),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            await self._raise_disambiguated_weight_error(run_id)

    async def set_corrected_charge(self, run_id: str, *, corrected_charge_grams: float) -> None:
        """Operator-entered CHARGE-weight correction (#520) — completed runs only.

        The same immutability-exception lifecycle as :meth:`set_roasted_weight`:
        the frozen ``profile_json.bean_weight_grams`` stays untouched (it is what
        the controller/advisor actually ran with) — the corrected value lands in
        this separate column instead. Derived weight-loss % on read prefers this
        value over the frozen charge weight when present (see ``read_run`` /
        ``list_runs``).

        The correction must not fall below the run's CURRENT
        ``roasted_weight_grams`` (when one exists) — folded into the UPDATE's
        own ``WHERE`` clause (#520 round-2 P3), the same atomicity fix as
        :meth:`set_roasted_weight`'s own bound, closing the identical race in
        the other direction.

        Args:
            run_id: The completed run to update.
            corrected_charge_grams: The corrected green/charge weight in grams
                (> 0; the API model enforces the bound, plus the
                not-below-roasted-weight physical-sanity check, before this is
                called — this is the atomic BACKSTOP, not the primary UX
                feedback path).

        Raises:
            RuntimeError: The run is unknown or still in progress.
            PhysicallyImpossibleWeightError: The write would fall below the
                run's current roasted-out weight.
        """
        cursor = await self.connection.execute(
            "UPDATE roast_runs SET corrected_charge_grams = ?, updated_at_utc = ?"
            " WHERE id = ? AND completed_at_utc IS NOT NULL"
            " AND (roasted_weight_grams IS NULL OR ? >= roasted_weight_grams)",
            (corrected_charge_grams, _utc_now(), run_id, corrected_charge_grams),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            await self._raise_disambiguated_weight_error(run_id)

    async def _raise_disambiguated_weight_error(self, run_id: str) -> None:
        """Distinguish a zero-``rowcount`` weight/charge UPDATE's cause (#520
        round-2 P3): unknown/in-progress run vs. a physically-impossible
        write against the row's current state. Shared by
        :meth:`set_roasted_weight` / :meth:`set_corrected_charge` so the two
        atomic ``WHERE``-clause bounds report the same error shape.

        Raises:
            RuntimeError: No completed run matches ``run_id``.
            PhysicallyImpossibleWeightError: The run exists and is completed,
                so the UPDATE's zero rowcount can only be the cross-value
                bound.
        """
        async with self.connection.execute(
            "SELECT 1 FROM roast_runs WHERE id = ? AND completed_at_utc IS NOT NULL",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError(f"no completed roast_run with id {run_id!r}")
        raise PhysicallyImpossibleWeightError(
            f"roast_run {run_id!r}: the write is physically impossible against "
            f"the run's current roasted/charge weights"
        )

    async def set_run_excluded(self, run_id: str, *, excluded: bool) -> None:
        """Soft-exclude (or restore) a completed run from history/corpus (#582).

        The same completed-only immutability-exception lifecycle as
        :meth:`set_operator_rating` / :meth:`set_roasted_weight` /
        :meth:`set_corrected_charge`: a discard/restore is entered after the
        roast has finished (``completed_at_utc IS NOT NULL``), so an
        in-progress run can never be silently hidden mid-roast. Reversible by
        design — calling this again with the opposite value un-discards a
        run. Nothing else is touched: the run row, its telemetry, events,
        safety/advisor/command rows, and any exported audio stay exactly as
        they were (soft flag, never a delete — the store has no delete-run
        path at all, see the v2 ``BEFORE DELETE`` trigger).

        Args:
            run_id: The completed run to discard or restore.
            excluded: ``True`` to discard (hide from history + corpus
                retrieval), ``False`` to restore.

        Raises:
            RuntimeError: No completed run matches ``run_id``.
        """
        cursor = await self.connection.execute(
            "UPDATE roast_runs SET excluded = ?, updated_at_utc = ?"
            " WHERE id = ? AND completed_at_utc IS NOT NULL",
            (1 if excluded else 0, _utc_now(), run_id),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            raise RuntimeError(f"no completed roast_run with id {run_id!r}")

    async def add_tasting(
        self,
        run_id: str,
        *,
        stars: Literal[1, 2, 3, 4, 5],
        notes: str | None = None,
        tasted_at_utc: str | None = None,
        brew_method: BrewMethod | None = None,
        grind_note: str | None = None,
        attributes: list[TastingAttribute] | None = None,
        defects: list[TastingDefect] | None = None,
    ) -> RoastTasting:
        """Append one tasting entry (#522, D91) — completed runs only.

        The same completed-only lifecycle as :meth:`set_operator_rating`, but this
        table has no immutability-trigger exception to lean on: ``roast_tastings``
        is a separate append-only table (see ``SCHEMA_V11_TASTINGS``), so the
        completed-run guard is an explicit existence check here rather than an
        UPDATE ``rowcount``. A revisit tasting is always a NEW row — this method
        never updates an existing entry.

        Args:
            run_id: The completed run this tasting belongs to.
            stars: 1-5 star rating (mirrors :meth:`set_operator_rating`).
            notes: Optional free-text tasting notes.
            tasted_at_utc: Optional UTC ISO-8601 instant the tasting happened
                (distinct from ``recorded_at_utc``, stamped here). Left ``None``
                (not defaulted to "now") when the operator does not supply one.
            brew_method: Optional brew method (``models.BrewMethod``).
            grind_note: Optional free-text grind note.
            attributes: Optional positive attribute tags (``models.TastingAttribute``).
            defects: Optional defect tags (``models.TastingDefect``).

        Returns:
            The persisted :class:`RoastTasting`, including its assigned id.

        Raises:
            RuntimeError: The run is unknown or still in progress.
        """
        async with self.connection.execute(
            "SELECT 1 FROM roast_runs WHERE id = ? AND completed_at_utc IS NOT NULL",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError(f"no completed roast_run with id {run_id!r}")
        recorded_at = _utc_now()
        cursor = await self.connection.execute(
            "INSERT INTO roast_tastings (run_id, tasted_at_utc, recorded_at_utc, stars,"
            " notes, brew_method, grind_note, attributes_json, defects_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                tasted_at_utc,
                recorded_at,
                stars,
                notes,
                brew_method,
                grind_note,
                json.dumps(attributes or [], sort_keys=True),
                json.dumps(defects or [], sort_keys=True),
            ),
        )
        await self.connection.commit()
        tasting_id = cursor.lastrowid
        assert tasting_id is not None  # pragma: no cover — AUTOINCREMENT always assigns one
        return RoastTasting(
            id=tasting_id,
            tasted_at_utc=tasted_at_utc,
            recorded_at_utc=recorded_at,
            stars=stars,
            notes=notes,
            brew_method=brew_method,
            grind_note=grind_note,
            attributes=list(attributes) if attributes else [],
            defects=list(defects) if defects else [],
        )

    async def list_tastings(self, run_id: str) -> list[RoastTasting]:
        """Every tasting entry for a run, oldest first (#522) — the natural
        revisit order (first taste, then any later refinement). Empty list for a
        run with no tastings yet or an unknown run id (mirrors ``read_run``'s
        ``None``-for-unknown convention at the collection level: an empty list,
        not an error, since "no tastings" and "unknown run" both render the
        same empty state and the caller already 404s on the run itself)."""
        async with self.connection.execute(
            "SELECT id, tasted_at_utc, recorded_at_utc, stars, notes, brew_method,"
            " grind_note, attributes_json, defects_json FROM roast_tastings"
            " WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            RoastTasting(
                id=int(row["id"]),
                tasted_at_utc=None if row["tasted_at_utc"] is None else str(row["tasted_at_utc"]),
                recorded_at_utc=str(row["recorded_at_utc"]),
                stars=int(row["stars"]),
                notes=None if row["notes"] is None else str(row["notes"]),
                brew_method=None
                if row["brew_method"] is None
                else cast(BrewMethod, str(row["brew_method"])),
                grind_note=None if row["grind_note"] is None else str(row["grind_note"]),
                attributes=cast(
                    list[TastingAttribute],
                    [] if row["attributes_json"] is None else json.loads(row["attributes_json"]),
                ),
                defects=cast(
                    list[TastingDefect],
                    [] if row["defects_json"] is None else json.loads(row["defects_json"]),
                ),
            )
            for row in rows
        ]

    # --- E7-S1: API read paths (component plan §6) ---
    #
    # Read-only projections backing the REST surface. They return the typed
    # response models from models.py directly: the SQL-to-DTO mapping belongs
    # with the schema it reads, and the store already owns every other
    # persistence concern (writes, recovery reads, immutability).

    async def active_run(self) -> PersistedRun | None:
        """The current in-progress run, or ``None``.

        "Active" means a run with no ``completed_at_utc`` — ``complete_run``
        stamps that field for every terminal outcome (completed, aborted,
        *and* faulted), so a faulted/finished run never counts as active.
        Backs the ``POST /api/roasts`` 409 guard and the health route's
        active-run id; survives restart because it reads persisted state, not
        in-memory flags."""
        async with self.connection.execute(
            "SELECT id, agent_phase, outcome, started_at_utc, completed_at_utc,"
            " profile_json FROM roast_runs WHERE completed_at_utc IS NULL"
            " ORDER BY started_at_utc DESC, rowid DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return PersistedRun(
            run_id=str(row["id"]),
            agent_phase=RoastPhase(str(row["agent_phase"])),
            outcome=row["outcome"],
            started_at_utc=str(row["started_at_utc"]),
            completed_at_utc=None
            if row["completed_at_utc"] is None
            else str(row["completed_at_utc"]),
            profile=RoastProfile.model_validate_json(str(row["profile_json"])),
        )

    async def list_runs(self) -> list[RoastSummary]:
        """The roast history list (plan §6): newest first.

        Development percent comes from the run's latest non-null telemetry
        snapshot via a correlated subquery — one statement, no N+1 — and is
        ``None`` for a run that never recorded one.

        First-crack time is the chronologically earliest accepted ``first_crack``
        event, except an MCP event may use the earliest valid backdated onset in
        state snapshots persisted at or after that confirmation. Operator,
        legacy, malformed, absent, and out-of-window onset paths retain the event
        timestamp. Unlike the exporter’s whole-run diagnostic sweep, this history
        projection deliberately considers only post-confirmation state.

        The subquery orders by ``recorded_at_utc`` (the event time), NOT by the
        insertion ``id``: ``record_event`` accepts an explicit ``recorded_at_utc``,
        so a later-inserted event can carry an earlier timestamp, and ordering by
        ``id`` would then return the wrong FC time. The stored value is always
        ``datetime.now(UTC).isoformat()`` (see :func:`_utc_now`) — a fixed-width
        ISO-8601 string with a constant ``+00:00`` offset — so a lexicographic
        ``ORDER BY`` on the text column is also chronological.

        Advisor stats (#184) — consults, clamped, rejected, failed — are
        aggregated here from ``advisor_decisions`` so the history list no longer
        N+1s ``GET /api/roasts/{id}/timeline`` to derive them client-side. The
        counts are FK-exact: ``consults`` is every persisted decision row;
        ``failed`` is those whose ``status`` is not ``ok``; ``clamped`` /
        ``rejected`` count a consult against its linked safety evaluation. The
        ``idx_advisor_run_tick`` index covers the advisor lookups; all are
        correlated subqueries (one statement, no N+1). A NULL or dangling
        advisor-side safety FK counts as neither clamped nor rejected, so pre-FK
        rows under-report rather than guessing from a non-unique tick. A run with
        no advisor decisions yields zeros, which the SPA renders as "no advice".

        The clamp/reject verdict values the clamped/rejected subqueries compare
        against are sourced from the typed :class:`SafetyVerdict` enum
        (``SafetyVerdict.CLAMP.value`` / ``SafetyVerdict.REJECT.value``) and
        **bound as query parameters**, never raw SQL string literals (D15: a
        verdict rename must surface as a type error, not silently dodge the
        compare).

        A discarded run (``excluded = 1``, #582) is filtered out of this list
        entirely — the whole point of the soft-exclude flag is to keep a
        bad-data roast out of the operator-facing history surface without
        deleting it. Its row, telemetry, events, and every child table stay
        untouched; :meth:`read_run` still returns it (carrying
        ``excluded=True``) for a direct link."""
        async with self.connection.execute(
            "SELECT r.id, r.started_at_utc, r.completed_at_utc, r.agent_phase,"
            " r.outcome, r.profile_json, r.operator_rating, r.roasted_weight_grams,"
            " r.corrected_charge_grams,"
            " r.ambient_temp_c, r.ambient_humidity_pct, r.ambient_pressure_hpa,"
            " (SELECT t.development_percent FROM telemetry_snapshots t"
            "  WHERE t.run_id = r.id AND t.development_percent IS NOT NULL"
            "  ORDER BY t.id DESC LIMIT 1) AS dev_pct,"
            " (SELECT e.recorded_at_utc FROM roast_events e"
            "  WHERE e.run_id = r.id AND e.kind = 'first_crack'"
            "  ORDER BY e.recorded_at_utc ASC, e.id ASC LIMIT 1) AS fc_at,"
            " (SELECT e.source FROM roast_events e"
            "  WHERE e.run_id = r.id AND e.kind = 'first_crack'"
            "  ORDER BY e.recorded_at_utc ASC, e.id ASC LIMIT 1) AS fc_source,"
            " (SELECT COUNT(*) FROM advisor_decisions a"
            "  WHERE a.run_id = r.id) AS advisor_consults,"
            " (SELECT COUNT(*) FROM advisor_decisions a"
            "  WHERE a.run_id = r.id AND a.status != 'ok') AS advisor_failed,"
            " (SELECT COUNT(*) FROM advisor_decisions a WHERE a.run_id = r.id AND"
            "  (SELECT s.verdict FROM safety_evaluations s"
            "   WHERE s.id = a.safety_evaluation_id) = ?) AS advisor_clamped,"
            " (SELECT COUNT(*) FROM advisor_decisions a WHERE a.run_id = r.id AND"
            "  (SELECT s.verdict FROM safety_evaluations s"
            "   WHERE s.id = a.safety_evaluation_id) = ?) AS advisor_rejected"
            " FROM roast_runs r WHERE r.excluded = 0"
            " ORDER BY r.started_at_utc DESC, r.rowid DESC",
            # D15: the verdict values bound as query parameters come from the typed
            # SafetyVerdict enum, never raw SQL string literals — a rename of an
            # enum member is a pyright error here, not a silently-passing string.
            (SafetyVerdict.CLAMP.value, SafetyVerdict.REJECT.value),
        ) as cursor:
            rows = await cursor.fetchall()
        onset_candidates: dict[str, list[object]] = {}
        onset_query = (
            "SELECT t.run_id, CASE WHEN json_valid(t.raw_state_json) "
            "THEN json_extract(t.raw_state_json, '$.first_crack_status.detected_at_utc') END "
            "AS onset_candidate FROM telemetry_snapshots t "
            "WHERE EXISTS (SELECT 1 FROM roast_runs r WHERE r.id = t.run_id "
            "AND r.excluded = 0) AND EXISTS (SELECT 1 FROM roast_events e "
            "WHERE e.id = (SELECT first_event.id FROM roast_events first_event "
            "WHERE first_event.run_id = t.run_id AND first_event.kind = 'first_crack' "
            "ORDER BY first_event.recorded_at_utc ASC, first_event.id ASC LIMIT 1) "
            "AND e.source = ? AND t.recorded_at_utc >= e.recorded_at_utc) "
            "GROUP BY t.run_id, CASE WHEN json_valid(t.raw_state_json) "
            "THEN json_extract(t.raw_state_json, '$.first_crack_status.detected_at_utc') END"
        )
        async with self.connection.execute(onset_query, (RoastEventSource.MCP.value,)) as cursor:
            for onset_row in await cursor.fetchall():
                onset_candidates.setdefault(str(onset_row["run_id"]), []).append(
                    onset_row["onset_candidate"]
                )
        summaries: list[RoastSummary] = []
        for row in rows:
            profile = RoastProfile.model_validate_json(str(row["profile_json"]))
            roasted_weight = self._optional_float(row["roasted_weight_grams"])
            corrected_charge = self._optional_float(row["corrected_charge_grams"])
            first_crack_at = None if row["fc_at"] is None else str(row["fc_at"])
            if is_mcp_first_crack_source(row["fc_source"]):
                onset = earliest_onset_within_event_window(
                    onset_candidates.get(str(row["id"]), []), row["started_at_utc"], row["fc_at"]
                )
                if onset is not None:
                    first_crack_at = onset.isoformat()
            summaries.append(
                RoastSummary(
                    id=str(row["id"]),
                    started_at_utc=str(row["started_at_utc"]),
                    completed_at_utc=None
                    if row["completed_at_utc"] is None
                    else str(row["completed_at_utc"]),
                    first_crack_at_utc=first_crack_at,
                    agent_phase=RoastPhase(str(row["agent_phase"])),
                    outcome=row["outcome"],
                    bean_origin=profile.bean_origin,
                    bean_varietal=profile.bean_varietal,
                    country=profile.country,
                    bean_species=profile.bean_species,
                    is_blend=profile.is_blend,
                    processing=profile.processing,
                    altitude_m=profile.altitude_m,
                    rating=None if row["operator_rating"] is None else int(row["operator_rating"]),
                    roasted_weight_grams=roasted_weight,
                    corrected_charge_grams=corrected_charge,
                    weight_loss_percent=weight_loss_percent(
                        charge_weight_grams=corrected_charge or profile.bean_weight_grams,
                        roasted_weight_grams=roasted_weight,
                    ),
                    development_percent=self._optional_float(row["dev_pct"]),
                    advisor_consults=int(row["advisor_consults"]),
                    advisor_failed=int(row["advisor_failed"]),
                    advisor_clamped=int(row["advisor_clamped"]),
                    advisor_rejected=int(row["advisor_rejected"]),
                    ambient_temp_c=self._optional_float(row["ambient_temp_c"]),
                    ambient_humidity_pct=self._optional_float(row["ambient_humidity_pct"]),
                    ambient_pressure_hpa=self._optional_float(row["ambient_pressure_hpa"]),
                    # Always False here: the WHERE clause above already filters
                    # excluded=1 rows out of this list entirely (#582).
                    excluded=False,
                )
            )
        return summaries

    async def read_run(self, run_id: str) -> RoastDetail | None:
        """Run detail (plan §6): profile, phase, outcome, export manifest.
        ``None`` when no run has that id.

        Unlike :meth:`list_runs`, this returns a discarded run (``excluded =
        1``, #582) too — carrying ``excluded=True`` — so a direct link to a
        discarded roast still works; only the history list hides it."""
        async with self.connection.execute(
            "SELECT id, agent_phase, profile_json, outcome, started_at_utc,"
            " completed_at_utc, fault_reason, operator_rating, operator_notes,"
            " roasted_weight_grams, corrected_charge_grams, ambient_temp_c,"
            " ambient_humidity_pct, ambient_pressure_hpa, export_manifest_json,"
            " excluded"
            " FROM roast_runs WHERE id = ?",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        manifest = (
            None
            if row["export_manifest_json"] is None
            else LogManifest.model_validate_json(str(row["export_manifest_json"]))
        )
        agent_phase = RoastPhase(str(row["agent_phase"]))
        profile = RoastProfile.model_validate_json(str(row["profile_json"]))
        roasted_weight = self._optional_float(row["roasted_weight_grams"])
        corrected_charge = self._optional_float(row["corrected_charge_grams"])
        return RoastDetail(
            id=str(row["id"]),
            agent_phase=agent_phase,
            profile=profile,
            outcome=row["outcome"],
            started_at_utc=str(row["started_at_utc"]),
            completed_at_utc=None
            if row["completed_at_utc"] is None
            else str(row["completed_at_utc"]),
            fault_reason=None if row["fault_reason"] is None else str(row["fault_reason"]),
            rating=None if row["operator_rating"] is None else int(row["operator_rating"]),
            notes=None if row["operator_notes"] is None else str(row["operator_notes"]),
            roasted_weight_grams=roasted_weight,
            corrected_charge_grams=corrected_charge,
            weight_loss_percent=weight_loss_percent(
                charge_weight_grams=corrected_charge or profile.bean_weight_grams,
                roasted_weight_grams=roasted_weight,
            ),
            export_manifest=manifest,
            # Derived read-only from the phase (E10 option (a)): the SPA's action
            # bar mirrors this set; the live SSE phase_changed frame re-sends it.
            enabled_actions=enabled_operator_actions(agent_phase),
            ambient_temp_c=self._optional_float(row["ambient_temp_c"]),
            ambient_humidity_pct=self._optional_float(row["ambient_humidity_pct"]),
            ambient_pressure_hpa=self._optional_float(row["ambient_pressure_hpa"]),
            excluded=bool(row["excluded"]),
        )

    async def read_telemetry_points(
        self, run_id: str, *, downsample: int = 1
    ) -> list[TelemetryPoint]:
        """Insertion-ordered telemetry snapshots, sampled every ``downsample`` rows.

        ``downsample`` must be >= 1; ``1`` returns every snapshot. The stride
        is index-based and keeps the first row, so the series start is stable
        regardless of stride. The durable insertion id owns chronology because
        the process-local tick counter restarts at zero after agent recovery."""
        if downsample < 1:
            raise ValueError("downsample must be >= 1")
        async with self.connection.execute(
            "SELECT tick, elapsed_seconds, agent_phase, bean_temp_c, env_temp_c,"
            " bean_ror_c_per_min, env_ror_c_per_min, heat_level_percent,"
            " fan_level_percent, cooling_on, development_percent, charge_elapsed_seconds"
            ", post_fc_recovery_enabled, post_fc_heat_authority_state,"
            " post_fc_ror_setpoint_c_per_min, post_fc_smoothed_ror_c_per_min,"
            " post_fc_effective_heat_ceiling_percent"
            " FROM telemetry_snapshots WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ) as cursor:
            rows = list(await cursor.fetchall())
        sampled = rows[::downsample]
        return [
            TelemetryPoint(
                tick=int(row["tick"]),
                elapsed_seconds=self._optional_float(row["elapsed_seconds"]),
                agent_phase=RoastPhase(str(row["agent_phase"])),
                bean_temp_c=self._optional_float(row["bean_temp_c"]),
                env_temp_c=self._optional_float(row["env_temp_c"]),
                bean_ror_c_per_min=self._optional_float(row["bean_ror_c_per_min"]),
                env_ror_c_per_min=self._optional_float(row["env_ror_c_per_min"]),
                heat_level_percent=None
                if row["heat_level_percent"] is None
                else int(row["heat_level_percent"]),
                fan_level_percent=None
                if row["fan_level_percent"] is None
                else int(row["fan_level_percent"]),
                cooling_on=None if row["cooling_on"] is None else bool(row["cooling_on"]),
                development_percent=self._optional_float(row["development_percent"]),
                charge_elapsed_seconds=self._optional_float(row["charge_elapsed_seconds"]),
                post_fc_recovery_enabled=None
                if row["post_fc_recovery_enabled"] is None
                else bool(row["post_fc_recovery_enabled"]),
                post_fc_heat_authority_state=None
                if row["post_fc_heat_authority_state"] is None
                else PostFcHeatAuthorityState(str(row["post_fc_heat_authority_state"])),
                post_fc_ror_setpoint_c_per_min=self._optional_float(
                    row["post_fc_ror_setpoint_c_per_min"]
                ),
                post_fc_smoothed_ror_c_per_min=self._optional_float(
                    row["post_fc_smoothed_ror_c_per_min"]
                ),
                post_fc_effective_heat_ceiling_percent=None
                if row["post_fc_effective_heat_ceiling_percent"] is None
                else int(row["post_fc_effective_heat_ceiling_percent"]),
            )
            for row in sampled
        ]

    async def read_timeline(self, run_id: str) -> RoastTimeline:
        """The decision trace (plan §6): roast events, safety verdicts,
        advisor decisions, and the command trail, each insertion-ordered.

        Returns an empty trace for an unknown run id — distinguishing
        not-found is the run-detail route's job, not the timeline's."""
        async with self.connection.execute(
            "SELECT kind, source, monotonic_seconds, recorded_at_utc, payload_json"
            " FROM roast_events WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ) as cursor:
            event_rows = await cursor.fetchall()
        events = [
            TimelineEvent(
                kind=RoastEventKind(str(row["kind"])),
                source=RoastEventSource(str(row["source"])),
                monotonic_seconds=self._optional_float(row["monotonic_seconds"]),
                recorded_at_utc=str(row["recorded_at_utc"]),
                payload=_loads(row["payload_json"]),
            )
            for row in event_rows
        ]
        async with self.connection.execute(
            "SELECT id, tick, rule, verdict, input_heat, input_fan, adjusted_heat,"
            " adjusted_fan, reason, recorded_at_utc FROM safety_evaluations"
            " WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ) as cursor:
            safety_rows = await cursor.fetchall()
        safety_evaluations = [
            TimelineSafetyEvaluation(
                id=int(row["id"]),
                tick=int(row["tick"]),
                rule=str(row["rule"]),
                # The store CHECK constraints pin these columns to the typed
                # wire forms; cast at the read boundary rather than re-validate.
                verdict=cast(TimelineVerdict, str(row["verdict"])),
                input_heat=None if row["input_heat"] is None else int(row["input_heat"]),
                input_fan=None if row["input_fan"] is None else int(row["input_fan"]),
                adjusted_heat=None if row["adjusted_heat"] is None else int(row["adjusted_heat"]),
                adjusted_fan=None if row["adjusted_fan"] is None else int(row["adjusted_fan"]),
                reason=str(row["reason"]),
                recorded_at_utc=str(row["recorded_at_utc"]),
            )
            for row in safety_rows
        ]
        async with self.connection.execute(
            "SELECT tick, provider, model, prompt_version, latency_ms, status,"
            " decision_json, safety_evaluation_id, recorded_at_utc FROM advisor_decisions"
            " WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ) as cursor:
            advisor_rows = await cursor.fetchall()
        advisor_decisions = [
            TimelineAdvisorDecision(
                tick=int(row["tick"]),
                provider=str(row["provider"]),
                model=str(row["model"]),
                prompt_version=str(row["prompt_version"]),
                latency_ms=None if row["latency_ms"] is None else int(row["latency_ms"]),
                status=cast(AdvisorTraceStatus, str(row["status"])),
                decision=_loads(row["decision_json"]),
                safety_evaluation_id=None
                if row["safety_evaluation_id"] is None
                else int(row["safety_evaluation_id"]),
                recorded_at_utc=str(row["recorded_at_utc"]),
            )
            for row in advisor_rows
        ]
        async with self.connection.execute(
            "SELECT tick, tool, source, status, args_json, result_json,"
            " safety_evaluation_id, recorded_at_utc FROM command_log"
            " WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ) as cursor:
            command_rows = await cursor.fetchall()
        commands = [
            TimelineCommand(
                tick=int(row["tick"]),
                tool=RoastCommand(str(row["tool"])),
                source=cast(CommandTraceSource, str(row["source"])),
                status=cast(CommandTraceStatus, str(row["status"])),
                args=_loads(row["args_json"]),
                result=_loads(row["result_json"]),
                safety_evaluation_id=None
                if row["safety_evaluation_id"] is None
                else int(row["safety_evaluation_id"]),
                recorded_at_utc=str(row["recorded_at_utc"]),
            )
            for row in command_rows
        ]
        return RoastTimeline(
            run_id=run_id,
            events=events,
            safety_evaluations=safety_evaluations,
            advisor_decisions=advisor_decisions,
            commands=commands,
        )

    # --- #567 Slice A: reference-curve retrieval + representation ---
    #
    # Pure retrieval + representation logic for a completed, well-rated past
    # roast of the same bean (design note, issue #567, §1/§3/§6.4a). Read-only;
    # nothing here is wired into ``start_roast``, ``AdvisorContext``, the
    # controller, replay, or config — that plumbing is Slice B. Deliberately
    # bypasses the schema-v1 ``reference_roasts`` table: that table is a
    # dormant M2 cloud-sync cache shaped for a different (future) producer
    # (design note §2) — these methods query ``roast_runs`` /
    # ``telemetry_snapshots`` directly instead.

    async def _ranked_reference_run_ids(
        self,
        origin_slug: str,
        charge_grams: float,
        *,
        min_rating: int,
        weight_tolerance_frac: float,
    ) -> list[str]:
        """Every qualifying reference run id, best-first (shared by :meth:`find_reference_run`
        and :meth:`load_reference_roast`, Fix B — PR #574 review).

        Retrieval rule (design note §1.2/§1.4): among COMPLETED runs
        (``completed_at_utc IS NOT NULL AND outcome = 'completed'`` — a
        faulted-but-finalized run consumed a recording slot per
        :meth:`count_completed_runs_for_origin`, but its trajectory is never a
        *good* reference, so it is excluded here even though it would still
        count toward that unrelated metric), never DISCARDED
        (``excluded = 0`` — #582: a soft-excluded run is bad-data by the
        operator's own explicit call, so it must never surface as a
        reference candidate — the corpus-hygiene point of the flag), rated
        ``>= min_rating`` whose frozen ``profile_json`` slugs
        (:func:`~roastpilot_agent.models.recording_origin_slug`) to
        ``origin_slug`` and whose actual charge weight is within
        ``weight_tolerance_frac`` of ``charge_grams``. "Actual charge" is
        ``corrected_charge_grams`` when the operator corrected it, else the
        frozen ``profile_json.bean_weight_grams`` — the same fallback
        :meth:`list_runs` / :meth:`read_run` already use for weight-loss %.

        The completion/outcome/rating filter runs in SQL (``ORDER BY
        operator_rating DESC, completed_at_utc DESC`` does the
        best-rated/tie-break-recency ordering); the slug and charge-weight
        checks run in Python row-by-row against that ordering, so the
        returned list is ALREADY correctly ranked — no re-sort needed.
        Collecting every passing candidate (not just the first) lets
        :meth:`load_reference_roast` fall through to the next-best candidate
        when the top-ranked one turns out to have no usable telemetry.

        Args:
            origin_slug: The recording-origin slug to match.
            charge_grams: This roast's charge weight in grams — the tolerance
                band (``weight_tolerance_frac`` of THIS value, design note
                §1.4) a candidate's actual charge weight must fall within.
            min_rating: The minimum ``operator_rating`` (1-5) a candidate must
                have.
            weight_tolerance_frac: The fractional charge-weight tolerance
                (e.g. ``0.10`` = ±10 %) applied to ``charge_grams``.

        Returns:
            Every qualifying run id, best-rated first (ties broken by most
            recent completion) — empty when none qualify.
        """
        async with self.connection.execute(
            "SELECT id, profile_json, corrected_charge_grams FROM roast_runs"
            " WHERE completed_at_utc IS NOT NULL AND outcome = 'completed'"
            " AND excluded = 0"
            " AND operator_rating >= ?"
            " ORDER BY operator_rating DESC, completed_at_utc DESC",
            (min_rating,),
        ) as cursor:
            rows = await cursor.fetchall()
        tolerance = weight_tolerance_frac * charge_grams
        ranked: list[str] = []
        for row in rows:
            try:
                profile = RoastProfile.model_validate_json(str(row["profile_json"]))
            except ValueError:  # pragma: no cover - a frozen profile is always valid
                continue
            if recording_origin_slug(profile) != origin_slug:
                continue
            actual_charge = self._optional_float(row["corrected_charge_grams"])
            if actual_charge is None:
                actual_charge = profile.bean_weight_grams
            if abs(actual_charge - charge_grams) <= tolerance:
                ranked.append(str(row["id"]))
        return ranked

    async def find_reference_run(
        self,
        origin_slug: str,
        charge_grams: float,
        *,
        min_rating: int = 3,
        weight_tolerance_frac: float = 0.10,
    ) -> str | None:
        """Find the best completed reference run for a bean + charge weight.

        A thin projection of :meth:`_ranked_reference_run_ids` onto its
        top-ranked id — see that method's docstring for the full retrieval
        rule. Note this is the best-RATED match, not necessarily the best
        *usable* one (an older run might have no development telemetry);
        :meth:`load_reference_roast` is the one that falls through to the
        next-best candidate when the top one can't be built (Fix B, PR #574
        review) — this method's ``str | None`` contract (and its existing
        callers/tests) is unchanged.

        Args:
            origin_slug: The recording-origin slug to match (from
                :func:`~roastpilot_agent.models.recording_origin_slug` on the
                roast being started).
            charge_grams: This roast's charge weight in grams.
            min_rating: The minimum ``operator_rating`` (1-5) a candidate must
                have. Defaults to ``3`` (the design note's quality floor).
            weight_tolerance_frac: The fractional charge-weight tolerance
                (default ``0.10`` = ±10 %) applied to ``charge_grams``.

        Returns:
            The best-qualifying run's ``id``, or ``None`` when no completed,
            rated, same-origin, comparable-weight run exists.
        """
        ranked = await self._ranked_reference_run_ids(
            origin_slug,
            charge_grams,
            min_rating=min_rating,
            weight_tolerance_frac=weight_tolerance_frac,
        )
        return ranked[0] if ranked else None

    async def _build_reference_roast(self, run_id: str, origin_slug: str) -> ReferenceRoast | None:
        """Build a :class:`~roastpilot_agent.models.ReferenceRoast` from a run's telemetry.

        PRIVATE (Codex PR #574 finding): this method only checks that
        ``run_id`` was ever rated — it does NOT re-check ``outcome ==
        'completed'`` or the ``>= min_rating`` quality floor that
        :meth:`_ranked_reference_run_ids` enforces at retrieval time. It
        assumes ``run_id`` is already a pre-filtered, retrieval-selected id
        (the outcome/quality predicate is kept solely in
        :meth:`_ranked_reference_run_ids`, not duplicated here). Calling it
        directly with an arbitrary run id bypasses that filtering — the only
        supported entry point for "a completed, well-rated reference" is
        :meth:`load_reference_roast`; :meth:`find_reference_run` is the
        best-match id query, and this builder is its internal, unfiltered
        materialization step.

        Reads ``telemetry_snapshots`` for ``run_id`` in tick order and derives:

        - **Landmarks**: drop is the LAST ``development``-phase row. First
          crack uses the accepted MCP event's backdated onset only when its
          wall-clock mapping and interpolated temperature form a complete,
          usable pre-drop pair no later than confirmation; otherwise both
          first-crack values remain the FIRST ``development``-phase row.
        - **Curve**: downsampled to at most 30 points, evenly spaced by index,
          always including the first and last usable row (design note §3.1).
          A row is "usable" when both ``charge_elapsed_seconds`` and
          ``bean_temp_c`` are recorded (``t_s``/``bean_c`` are non-optional on
          :class:`~roastpilot_agent.models.ReferenceCurveSample``). The curve
          is also TRIMMED to rows at or before the drop landmark (Fix D, PR
          #574 review) — the last-development row's position is computed
          ONCE and shared with the drop landmark above, so the two can never
          disagree about where the roast's "good shape" ends and the
          post-drop cooling tail (falling temperatures) begins.

        Args:
            run_id: The reference run's ``roast_runs.id`` (typically one of
                the ids ranked by :meth:`_ranked_reference_run_ids`).
            origin_slug: The recording-origin slug to stamp onto the returned
                :class:`~roastpilot_agent.models.ReferenceRoast` (this method
                does not re-derive it from the run's own frozen profile —
                the caller already has it from the retrieval side).

        Returns:
            The built :class:`~roastpilot_agent.models.ReferenceRoast`, or
            ``None`` when the run doesn't exist, was never rated, has no
            ``development``-phase telemetry, or every pre-drop telemetry row
            is missing its charge-elapsed clock (no usable curve point).
        """
        async with self.connection.execute(
            "SELECT operator_rating, started_at_utc,"
            " (SELECT e.recorded_at_utc FROM roast_events e WHERE e.run_id = roast_runs.id"
            "  AND e.kind = 'first_crack' ORDER BY e.recorded_at_utc ASC, e.id ASC"
            "  LIMIT 1) AS fc_at,"
            " (SELECT e.source FROM roast_events e WHERE e.run_id = roast_runs.id"
            "  AND e.kind = 'first_crack' ORDER BY e.recorded_at_utc ASC, e.id ASC"
            "  LIMIT 1) AS fc_source"
            " FROM roast_runs WHERE id = ?",
            (run_id,),
        ) as cursor:
            run_row = await cursor.fetchone()
        if run_row is None or run_row["operator_rating"] is None:
            return None
        operator_rating = int(run_row["operator_rating"])
        onset_candidates: list[object] = []
        if is_mcp_first_crack_source(run_row["fc_source"]):
            onset_query = (
                "SELECT CASE WHEN json_valid(t.raw_state_json) THEN "
                "json_extract(t.raw_state_json, '$.first_crack_status.detected_at_utc') "
                "END AS onset_candidate "
                "FROM telemetry_snapshots t WHERE t.run_id = ? AND EXISTS "
                "(SELECT 1 FROM roast_events e WHERE e.id = "
                "(SELECT first_event.id FROM roast_events first_event "
                "WHERE first_event.run_id = t.run_id AND first_event.kind = 'first_crack' "
                "ORDER BY first_event.recorded_at_utc ASC, first_event.id ASC LIMIT 1) "
                "AND e.source = ? AND t.recorded_at_utc >= e.recorded_at_utc) "
                "GROUP BY CASE WHEN json_valid(t.raw_state_json) THEN "
                "json_extract(t.raw_state_json, '$.first_crack_status.detected_at_utc') END"
            )
            async with self.connection.execute(
                onset_query, (run_id, RoastEventSource.MCP.value)
            ) as cursor:
                onset_candidates = [row["onset_candidate"] for row in await cursor.fetchall()]

        async with self.connection.execute(
            "SELECT charge_elapsed_seconds, bean_temp_c, env_temp_c,"
            " bean_ror_c_per_min, agent_phase, development_percent, recorded_at_utc"
            " FROM telemetry_snapshots WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ) as cursor:
            rows = list(await cursor.fetchall())

        # Typed RoastPhase comparison (D15: never string-compare a phase in
        # core logic) — indexed so the last-development position can be
        # shared between the drop landmark and the curve trim below (Fix A +
        # Fix D, PR #574 review).
        development_indices = [
            index
            for index, row in enumerate(rows)
            if RoastPhase(str(row["agent_phase"])) is RoastPhase.DEVELOPMENT
        ]
        if not development_indices:
            return None
        first_dev = rows[development_indices[0]]
        last_dev_index = development_indices[-1]
        last_dev = rows[last_dev_index]

        # Trim the curve to rows AT OR BEFORE the drop (Fix D): the post-drop
        # cooling tail keeps recording a FALLING bean temperature, which must
        # never appear in "what a good roast's shape looked like".
        pre_drop_rows = rows[: last_dev_index + 1]
        clock_rows: list[tuple[aiosqlite.Row, float]] = []
        for row in pre_drop_rows:
            seconds = self._optional_float(row["charge_elapsed_seconds"])
            if seconds is not None:
                clock_rows.append((row, seconds))
        usable = [
            row
            for row in pre_drop_rows
            if self._optional_float(row["charge_elapsed_seconds"]) is not None
            and self._optional_float(row["bean_temp_c"]) is not None
        ]
        if not usable:
            return None
        sample_rows = [usable[i] for i in self._evenly_spaced_indices(len(usable), 30)]
        curve = [
            ReferenceCurveSample(
                t_s=float(cast("float", row["charge_elapsed_seconds"])),
                bean_c=float(cast("float", row["bean_temp_c"])),
                env_c=self._optional_float(row["env_temp_c"]),
                ror_c_min=self._optional_float(row["bean_ror_c_per_min"]),
            )
            for row in sample_rows
        ]

        onset_pair = self._reference_onset_pair(
            onset_candidates,
            run_row["started_at_utc"],
            run_row["fc_at"],
            clock_rows,
            usable,
            first_dev,
        )
        first_crack_elapsed_s, first_crack_temp_c = (
            onset_pair
            if onset_pair is not None
            else (
                self._optional_float(first_dev["charge_elapsed_seconds"]),
                self._optional_float(first_dev["bean_temp_c"]),
            )
        )
        landmarks = ReferenceLandmarks(
            first_crack_temp_c=first_crack_temp_c,
            first_crack_elapsed_s=first_crack_elapsed_s,
            drop_temp_c=self._optional_float(last_dev["bean_temp_c"]),
            drop_development_percent=self._optional_float(last_dev["development_percent"]),
            operator_rating=operator_rating,
        )
        return ReferenceRoast(
            source_run_id=run_id,
            origin_slug=origin_slug,
            landmarks=landmarks,
            curve=curve,
        )

    async def load_reference_roast(
        self,
        origin_slug: str,
        charge_grams: float,
        *,
        min_rating: int = 3,
        weight_tolerance_frac: float = 0.10,
    ) -> ReferenceRoast | None:
        """The best USABLE reference: ranks candidates, builds the first that succeeds.

        Unlike :meth:`find_reference_run` (which only ever returns the
        top-RATED id), this iterates every ranked candidate from
        :meth:`_ranked_reference_run_ids` and returns the first one
        :meth:`_build_reference_roast` can actually build — falling through
        past a top-ranked candidate with no usable telemetry (e.g. an older
        run with no ``development``-phase rows) to the next-best one (Fix B,
        PR #574 review).

        Args:
            origin_slug: The recording-origin slug to match.
            charge_grams: This roast's charge weight in grams.
            min_rating: The minimum ``operator_rating`` a candidate must have
                (see :meth:`_ranked_reference_run_ids`).
            weight_tolerance_frac: The fractional charge-weight tolerance
                (see :meth:`_ranked_reference_run_ids`).

        Returns:
            The best USABLE :class:`~roastpilot_agent.models.ReferenceRoast`,
            or ``None`` when no candidate qualifies, or every qualifying
            candidate fails to build (see :meth:`_build_reference_roast`).
        """
        ranked = await self._ranked_reference_run_ids(
            origin_slug,
            charge_grams,
            min_rating=min_rating,
            weight_tolerance_frac=weight_tolerance_frac,
        )
        for run_id in ranked:
            reference = await self._build_reference_roast(run_id, origin_slug)
            if reference is not None:
                return reference
        return None

    @staticmethod
    def _evenly_spaced_indices(count: int, max_samples: int) -> list[int]:
        """Evenly-spaced sample indices over ``count`` items, capped at ``max_samples``.

        Always includes index ``0`` and ``count - 1`` (design note §3.1: a
        plain index-stride selection could otherwise miss a boundary row by
        construction). Returns every index unchanged when ``count <=
        max_samples``.

        Args:
            count: The number of items to sample from.
            max_samples: The maximum number of indices to return.

        Returns:
            A sorted, deduplicated list of indices into a ``count``-length
            sequence, at most ``max_samples`` long.
        """
        if count <= max_samples:
            return list(range(count))
        step = (count - 1) / (max_samples - 1)
        indices = {round(i * step) for i in range(max_samples)}
        return sorted(indices)

    # --- #303: bean-profile library CRUD (D45) ---
    #
    # The saved-profile library behind the Start-Roast dropdown. Purely additive
    # over the v4 ``bean_profiles`` table — none of these paths touch
    # ``roast_runs``, so a saved profile and a frozen roast snapshot are wholly
    # independent (editing a profile cannot mutate a past roast). Delete is a soft
    # archive (``archived = 1``), never a hard DELETE, so a profile referenced by
    # a past roast's notes is never dangling.

    async def expire_bean_sourcing_drafts(self, *, now_utc: str | None = None) -> int:
        """Clear expired, unclaimed draft snapshots while retaining metrics.

        Args:
            now_utc: Injectable UTC boundary for deterministic tests.

        Returns:
            The number of snapshots cleared.
        """
        now = now_utc or _utc_now()
        cursor = await self.connection.execute(
            "UPDATE bean_sourcing_attempts SET draft_snapshot_json = NULL,"
            " claim_expires_at_utc = NULL WHERE saved_profile_id IS NULL"
            " AND draft_snapshot_json IS NOT NULL AND claim_expires_at_utc <= ?",
            (now,),
        )
        await self.connection.commit()
        return cursor.rowcount

    async def clear_unclaimed_bean_sourcing_drafts(self, *, owner_instance_id: str) -> int:
        """Clear one service owner's unclaimed drafts during orderly shutdown.

        Args:
            owner_instance_id: The shutting-down service instance. A live peer's
                drafts in the shared SQLite database are left untouched.

        Returns:
            The number of snapshots cleared. Aggregate attempt telemetry remains.
        """
        if self._connection is None:
            return 0
        cursor = await self._connection.execute(
            "UPDATE bean_sourcing_attempts SET draft_snapshot_json = NULL,"
            " claim_expires_at_utc = NULL WHERE saved_profile_id IS NULL"
            " AND draft_snapshot_json IS NOT NULL AND owner_instance_id = ?",
            (owner_instance_id,),
        )
        await self._connection.commit()
        return cursor.rowcount

    async def next_bean_sourcing_expiry(self) -> str | None:
        """Return the earliest unclaimed draft expiry, if one exists."""
        async with self.connection.execute(
            "SELECT MIN(claim_expires_at_utc) AS expires_at"
            " FROM bean_sourcing_attempts WHERE saved_profile_id IS NULL"
            " AND draft_snapshot_json IS NOT NULL"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["expires_at"] is None:
            return None
        return str(row["expires_at"])

    async def start_bean_sourcing_attempt(
        self,
        *,
        provider: str,
        model_slug: str,
        prompt_version: str,
        owner_instance_id: str = "direct-store",
        started_at_utc: str | None = None,
    ) -> str:
        """Durably admit one bean-sourcing attempt before remote work starts."""
        attempt_id = uuid.uuid4().hex
        started = started_at_utc or _utc_now()
        lease_expires = (
            datetime.fromisoformat(started) + _BEAN_SOURCING_LEASE_DURATION
        ).isoformat()
        confirmation_cutoff = (
            datetime.fromisoformat(started) - _BEAN_SOURCING_LEASE_CONFIRMATION
        ).isoformat()
        admission_connection = await aiosqlite.connect(self._db_path)
        try:
            await admission_connection.execute("PRAGMA busy_timeout=500")
            await admission_connection.execute("BEGIN IMMEDIATE")
            await admission_connection.execute(
                "UPDATE bean_sourcing_attempts SET completed_at_utc = ?,"
                " outcome = 'cancelled', usage_evidence = 'unknown'"
                " WHERE outcome = 'in_progress' AND lease_expires_at_utc <= ?"
                " AND lease_expired_observed_at_utc <= ?",
                (started, started, confirmation_cutoff),
            )
            await admission_connection.execute(
                "UPDATE bean_sourcing_attempts SET lease_expired_observed_at_utc = ?"
                " WHERE outcome = 'in_progress' AND lease_expires_at_utc <= ?"
                " AND lease_expired_observed_at_utc IS NULL",
                (started, started),
            )
            await admission_connection.execute(
                "UPDATE bean_sourcing_attempts SET draft_snapshot_json = NULL,"
                " claim_expires_at_utc = NULL WHERE saved_profile_id IS NULL"
                " AND draft_snapshot_json IS NOT NULL AND claim_expires_at_utc <= ?",
                (started,),
            )
            await admission_connection.execute(
                "INSERT INTO bean_sourcing_attempts"
                " (id, provider, model_slug, prompt_version, owner_instance_id,"
                " started_at_utc, lease_expires_at_utc, outcome)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress')",
                (
                    attempt_id,
                    provider,
                    model_slug,
                    prompt_version,
                    owner_instance_id,
                    started,
                    lease_expires,
                ),
            )
            await admission_connection.commit()
        except BaseException:
            await admission_connection.rollback()
            raise
        finally:
            await admission_connection.close()
        return attempt_id

    async def reconcile_interrupted_bean_sourcing_attempts(
        self,
        *,
        completed_at_utc: str | None = None,
        lease_deadline_utc: str | None = None,
    ) -> int:
        """Terminalize admissions left in progress by a previous process exit.

        Args:
            completed_at_utc: Injectable startup timestamp for deterministic tests.
            lease_deadline_utc: Optional lease cutoff; defaults to completion time.

        Returns:
            The number of orphaned attempts marked cancelled.
        """
        completed = completed_at_utc or _utc_now()
        lease_deadline = lease_deadline_utc or completed
        confirmation_cutoff = (
            datetime.fromisoformat(completed) - _BEAN_SOURCING_LEASE_CONFIRMATION
        ).isoformat()
        cursor = await self.connection.execute(
            "UPDATE bean_sourcing_attempts SET completed_at_utc = ?,"
            " outcome = 'cancelled', usage_evidence = 'unknown'"
            " WHERE outcome = 'in_progress' AND lease_expires_at_utc <= ?"
            " AND lease_expired_observed_at_utc <= ?",
            (completed, lease_deadline, confirmation_cutoff),
        )
        await self.connection.execute(
            "UPDATE bean_sourcing_attempts SET lease_expired_observed_at_utc = ?"
            " WHERE outcome = 'in_progress' AND lease_expires_at_utc <= ?"
            " AND lease_expired_observed_at_utc IS NULL",
            (completed, lease_deadline),
        )
        await self.connection.commit()
        return cursor.rowcount

    async def renew_bean_sourcing_attempt_lease(
        self,
        attempt_id: str,
        *,
        owner_instance_id: str,
        renewed_at_utc: str | None = None,
    ) -> bool:
        """Extend a live attempt lease only for its owning service instance."""
        renewed = renewed_at_utc or _utc_now()
        expires = (datetime.fromisoformat(renewed) + _BEAN_SOURCING_LEASE_DURATION).isoformat()
        lease_connection = await aiosqlite.connect(self._db_path)
        try:
            await lease_connection.execute("PRAGMA busy_timeout=500")
            cursor = await lease_connection.execute(
                "UPDATE bean_sourcing_attempts SET lease_expires_at_utc = ?,"
                " lease_expired_observed_at_utc = NULL"
                " WHERE id = ? AND owner_instance_id = ? AND outcome = 'in_progress'",
                (expires, attempt_id, owner_instance_id),
            )
            await lease_connection.commit()
            return cursor.rowcount == 1
        except BaseException:
            await lease_connection.rollback()
            raise
        finally:
            await lease_connection.close()

    async def finish_bean_sourcing_attempt(
        self,
        attempt_id: str,
        *,
        outcome: Literal[
            "success",
            "fetch_error",
            "extraction_error",
            "provider_error",
            "preempted",
            "cancelled",
        ],
        latency_ms: int,
        request_tokens: int | None,
        response_tokens: int | None,
        usage_evidence: Literal["exact", "partial", "unknown"],
        timed_out_runs: int,
        draft: BeanProfileDraft | None = None,
        claimable_draft: bool = True,
        catalogue_discovered_count: int | None = None,
        catalogue_extracted_count: int | None = None,
        completed_at_utc: str | None = None,
    ) -> None:
        """Commit one terminal attempt outcome without retaining unsafe inputs."""
        completed = completed_at_utc or _utc_now()
        snapshot: str | None = None
        expires: str | None = None
        on_page_count: int | None = None
        estimated_count: int | None = None
        if outcome == "success" and claimable_draft and draft is None:
            raise ValueError("a successful bean-sourcing attempt requires a draft")
        if outcome == "success" and not claimable_draft and draft is not None:
            raise ValueError("a nonclaimable bean-sourcing success cannot carry a draft")
        catalogue_counts = (catalogue_discovered_count, catalogue_extracted_count)
        if outcome == "success" and not claimable_draft and None in catalogue_counts:
            raise ValueError("a nonclaimable catalogue success requires aggregate counts")
        if claimable_draft and any(value is not None for value in catalogue_counts):
            raise ValueError("a claimable bean draft cannot carry catalogue counts")
        if (
            catalogue_discovered_count is not None
            and catalogue_extracted_count is not None
            and catalogue_extracted_count > catalogue_discovered_count
        ):
            raise ValueError("catalogue extracted count cannot exceed discovered count")
        if outcome == "success" and draft is not None:
            # Single-product success carries one claimable draft baseline.
            # Catalogue success deliberately carries no draft: it records only
            # aggregate attempt metrics and the distinct catalogue prompt version;
            # choosing a result starts the existing single-product flow (D121).
            snapshot_fields = BeanProfileInput.model_fields.keys()
            snapshot_data = {
                field: getattr(draft, field) for field in snapshot_fields if field != "source_url"
            }
            snapshot = json.dumps(snapshot_data, sort_keys=True, separators=(",", ":"))
            expires = (datetime.fromisoformat(completed) + timedelta(hours=24)).isoformat()
            on_page_count = sum(value == "on_page" for value in draft.field_sources.values())
            estimated_count = sum(
                value == "origin_estimated" for value in draft.field_sources.values()
            )
        finish_connection = await aiosqlite.connect(self._db_path)
        try:
            await finish_connection.execute("PRAGMA busy_timeout=500")
            await finish_connection.execute("BEGIN IMMEDIATE")
            cursor = await finish_connection.execute(
                "UPDATE bean_sourcing_attempts SET completed_at_utc = ?, latency_ms = ?,"
                " outcome = ?, request_tokens = ?, response_tokens = ?, usage_evidence = ?,"
                " timed_out_runs = ?, on_page_field_count = ?,"
                " origin_estimated_field_count = ?, catalogue_discovered_count = ?,"
                " catalogue_extracted_count = ?, draft_snapshot_json = ?,"
                " claim_expires_at_utc = ? WHERE id = ? AND outcome = 'in_progress'",
                (
                    completed,
                    latency_ms,
                    outcome,
                    request_tokens,
                    response_tokens,
                    usage_evidence,
                    timed_out_runs,
                    on_page_count,
                    estimated_count,
                    catalogue_discovered_count,
                    catalogue_extracted_count,
                    snapshot,
                    expires,
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"bean-sourcing attempt {attempt_id} was not in progress")
            await finish_connection.commit()
        except BaseException:
            await finish_connection.rollback()
            raise
        finally:
            await finish_connection.close()

    async def create_bean_profile(
        self,
        profile_input: BeanProfileInput,
        *,
        draft_attempt_id: str | None = None,
    ) -> BeanProfile:
        """Persist a new saved bean profile and return it with its id + timestamps.

        Mints a fresh uuid4 id and stamps ``created_at`` == ``updated_at`` at the
        same instant; the full :class:`BeanProfile` is stored as one JSON column
        (the same dump shape ``roast_runs.profile_json`` uses).

        Args:
            profile_input: The operator-supplied bean-profile fields (no id /
                timestamps — the store owns those).

        Returns:
            The saved :class:`BeanProfile`, with id and timestamps populated.
        """
        async with self._bean_profile_write_lock:
            now = _utc_now()
            profile = BeanProfile(
                id=uuid.uuid4().hex,
                created_at=now,
                updated_at=now,
                **profile_input.model_dump(),
            )
            if draft_attempt_id is None:
                await self.connection.execute(
                    "INSERT INTO bean_profiles (id, name, profile_json, archived,"
                    " created_at_utc, updated_at_utc) VALUES (?, ?, ?, 0, ?, ?)",
                    (profile.id, profile.name, profile.model_dump_json(), now, now),
                )
                await self.connection.commit()
                return profile

            # A dedicated connection owns this transaction. The main store
            # connection also serves the periodic expiry task; sharing it here
            # would let an unrelated commit split the claim transaction.
            claim_connection = await aiosqlite.connect(self._db_path)
            claim_connection.row_factory = aiosqlite.Row
            try:
                await claim_connection.execute("PRAGMA foreign_keys=ON")
                await claim_connection.execute("PRAGMA busy_timeout=500")
                await claim_connection.execute("BEGIN IMMEDIATE")
                await claim_connection.execute(
                    "UPDATE bean_sourcing_attempts SET draft_snapshot_json = NULL,"
                    " claim_expires_at_utc = NULL WHERE saved_profile_id IS NULL"
                    " AND draft_snapshot_json IS NOT NULL AND claim_expires_at_utc <= ?",
                    (now,),
                )
                async with claim_connection.execute(
                    "SELECT draft_snapshot_json, saved_profile_id, outcome,"
                    " claim_expires_at_utc FROM bean_sourcing_attempts WHERE id = ?",
                    (draft_attempt_id,),
                ) as cursor:
                    attempt = await cursor.fetchone()
                if attempt is not None and attempt["saved_profile_id"] is not None:
                    async with claim_connection.execute(
                        "SELECT profile_json FROM bean_profiles WHERE id = ?",
                        (attempt["saved_profile_id"],),
                    ) as cursor:
                        saved_row = await cursor.fetchone()
                    if saved_row is None:  # pragma: no cover - protected by the schema FK
                        raise RuntimeError("claimed draft references a missing bean profile")
                    saved_profile = BeanProfile.model_validate_json(str(saved_row["profile_json"]))
                    saved_input = BeanProfileInput.model_validate(saved_profile.model_dump())
                    if saved_input == profile_input:
                        await claim_connection.rollback()
                        return saved_profile
                    raise BeanDraftAttemptAlreadyClaimedError(
                        "draft attempt was already saved with different profile values"
                    )
                if (
                    attempt is None
                    or attempt["outcome"] != "success"
                    or attempt["draft_snapshot_json"] is None
                    or attempt["claim_expires_at_utc"] is None
                    or str(attempt["claim_expires_at_utc"]) <= now
                ):
                    raise BeanDraftAttemptClaimError(
                        "draft attempt is unknown, expired, or unsuccessful"
                    )
                baseline = cast(dict[str, Any], json.loads(str(attempt["draft_snapshot_json"])))
                saved_values = profile_input.model_dump(mode="json")
                changed_fields = sorted(
                    field for field, value in baseline.items() if saved_values[field] != value
                )
                await claim_connection.execute(
                    "INSERT INTO bean_profiles (id, name, profile_json, archived,"
                    " created_at_utc, updated_at_utc) VALUES (?, ?, ?, 0, ?, ?)",
                    (profile.id, profile.name, profile.model_dump_json(), now, now),
                )
                claim = await claim_connection.execute(
                    "UPDATE bean_sourcing_attempts SET saved_profile_id = ?,"
                    " changed_fields_json = ?, claimed_at_utc = ?,"
                    " draft_snapshot_json = NULL, claim_expires_at_utc = NULL"
                    " WHERE id = ? AND outcome = 'success' AND saved_profile_id IS NULL",
                    (profile.id, json.dumps(changed_fields), now, draft_attempt_id),
                )
                if claim.rowcount != 1:  # pragma: no cover - lock + transaction defence
                    raise BeanDraftAttemptClaimError("draft attempt was already saved")
                await claim_connection.commit()
            except BaseException:
                await claim_connection.rollback()
                raise
            finally:
                await claim_connection.close()
            return profile

    async def list_bean_profiles(self) -> list[BeanProfile]:
        """The active (non-archived) saved profiles, name-ordered (#303).

        Backs the Start-Roast dropdown. Archived profiles are excluded so a
        soft-deleted bean never reappears as selectable, while its row survives so
        any past roast that referenced it stays intact.
        """
        async with self.connection.execute(
            "SELECT profile_json FROM bean_profiles WHERE archived = 0"
            " ORDER BY name COLLATE NOCASE ASC, id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [BeanProfile.model_validate_json(str(row["profile_json"])) for row in rows]

    async def catalogue_ranking_axes(
        self,
    ) -> tuple[
        list[tuple[str | None, ProcessingMethod | None]],
        list[tuple[str, ProcessingMethod]],
    ]:
        """Read only the distinct local facts used by catalogue ranking.

        This deliberately avoids :meth:`list_runs`, whose history projection
        materializes full profiles and correlated telemetry/advisor aggregates.
        Catalogue ranking needs only active-profile country/process axes and
        completed, non-excluded 4--5-star country/process pairs.

        Returns:
            ``(profile_axes, rated_pairs)``. Profile axes retain either nullable
            component so missing-country and missing-processing sets can be
            built independently; rated pairs require both components.
        """
        async with self.connection.execute(
            "SELECT DISTINCT json_extract(profile_json, '$.country') AS country,"
            " json_extract(profile_json, '$.processing') AS processing"
            " FROM bean_profiles WHERE archived = 0"
        ) as cursor:
            profile_rows = await cursor.fetchall()
        async with self.connection.execute(
            "SELECT DISTINCT json_extract(profile_json, '$.country') AS country,"
            " json_extract(profile_json, '$.processing') AS processing"
            " FROM roast_runs WHERE excluded = 0 AND outcome = 'completed'"
            " AND operator_rating >= 4"
            " AND json_extract(profile_json, '$.country') IS NOT NULL"
            " AND json_extract(profile_json, '$.processing') IS NOT NULL"
        ) as cursor:
            rated_rows = await cursor.fetchall()
        profile_axes: list[tuple[str | None, ProcessingMethod | None]] = [
            (
                None if row["country"] is None else str(row["country"]),
                None
                if row["processing"] is None
                else cast(ProcessingMethod, str(row["processing"])),
            )
            for row in profile_rows
        ]
        rated_pairs: list[tuple[str, ProcessingMethod]] = [
            (
                str(row["country"]),
                cast(ProcessingMethod, str(row["processing"])),
            )
            for row in rated_rows
        ]
        return profile_axes, rated_pairs

    async def get_bean_profile(self, profile_id: str) -> BeanProfile | None:
        """One active saved profile by id, or ``None`` (unknown or archived)."""
        async with self.connection.execute(
            "SELECT profile_json FROM bean_profiles WHERE id = ? AND archived = 0",
            (profile_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return BeanProfile.model_validate_json(str(row["profile_json"]))

    async def update_bean_profile(
        self, profile_id: str, profile_input: BeanProfileInput
    ) -> BeanProfile:
        """Edit a saved profile in place; bumps ``updated_at`` only (#303).

        Future-roasts-only by construction: this touches no ``roast_runs`` row, so
        a past roast's frozen ``profile_json`` snapshot is unaffected (the
        edit-is-safe guarantee). ``created_at`` and the id are preserved.

        Args:
            profile_id: The active profile to edit.
            profile_input: The replacement bean-profile fields.

        Returns:
            The updated :class:`BeanProfile`.

        Raises:
            BeanProfileNotFoundError: No active profile has that id.
        """
        existing = await self.get_bean_profile(profile_id)
        if existing is None:
            raise BeanProfileNotFoundError(profile_id)
        now = _utc_now()
        updated = BeanProfile(
            id=existing.id,
            created_at=existing.created_at,
            updated_at=now,
            **profile_input.model_dump(),
        )
        cursor = await self.connection.execute(
            "UPDATE bean_profiles SET name = ?, profile_json = ?, updated_at_utc = ?"
            " WHERE id = ? AND archived = 0",
            (updated.name, updated.model_dump_json(), now, profile_id),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            # TOCTOU: the profile was archived (or deleted) between the
            # get_bean_profile() read above and this UPDATE, so the
            # ``archived = 0`` guard matched no row. Fail closed with the
            # not-found error rather than return the fabricated ``updated`` model
            # — never report a phantom success for a row that was not written.
            raise BeanProfileNotFoundError(profile_id)
        return updated

    async def delete_bean_profile(self, profile_id: str) -> None:
        """Soft-delete (archive) a saved profile (#303).

        Sets ``archived = 1`` so the bean drops out of the dropdown while its row
        survives — a profile referenced by a past roast's notes is never dangling,
        and ``roast_runs`` is untouched. Idempotent at the storage layer only via
        the not-found guard: a second delete of an already-archived id raises (it
        is no longer an *active* profile).

        Archiving deliberately does NOT bump ``updated_at_utc``: ``updated_at`` is
        a *profile-edit* timestamp (the embedded ``profile_json.updated_at`` is its
        source of truth), and a soft-delete edits no profile content. Bumping only
        the column would leave it disagreeing with the stale ``updated_at`` inside
        ``profile_json``; leaving both untouched keeps the row's two timestamps
        consistent without re-serializing the blob. (The archive instant is not
        separately recorded — it is not part of the BeanProfile contract; add an
        ``archived_at`` column if an audit trail is ever needed.)

        Raises:
            BeanProfileNotFoundError: No active profile has that id.
        """
        cursor = await self.connection.execute(
            "UPDATE bean_profiles SET archived = 1 WHERE id = ? AND archived = 0",
            (profile_id,),
        )
        await self.connection.commit()
        if cursor.rowcount == 0:
            raise BeanProfileNotFoundError(profile_id)

    async def seed_bean_profile(self, seed: BeanProfile) -> bool:
        """Idempotently insert a built-in seed profile by its fixed id (#303).

        Used at startup to seed the Ethiopia Koke profile for the first roast.
        Keyed on the seed's stable ``id`` with ``INSERT OR IGNORE`` so a restart
        never double-inserts (and never clobbers an operator's later edit to the
        seeded row). The full :class:`BeanProfile` is passed in (id + timestamps
        already set) so the seed values are the single source of truth.

        Args:
            seed: The fully-formed seed profile (stable id + timestamps).

        Returns:
            ``True`` if the row was inserted this call; ``False`` if it already
            existed (the idempotent no-op).
        """
        cursor = await self.connection.execute(
            "INSERT OR IGNORE INTO bean_profiles (id, name, profile_json, archived,"
            " created_at_utc, updated_at_utc) VALUES (?, ?, ?, 0, ?, ?)",
            (seed.id, seed.name, seed.model_dump_json(), seed.created_at, seed.updated_at),
        )
        await self.connection.commit()
        return cursor.rowcount > 0


def _loads(value: Any) -> dict[str, Any] | None:
    """Parse a stored JSON text column into a dict payload.

    The trace columns (event payloads, advisor decisions, command args/results)
    are written with ``json.dumps`` of dict payloads; a NULL column reads back
    as ``None``. A non-object JSON value is wrapped so the typed model field
    (``dict[str, Any] | None``) always holds a mapping."""
    if value is None:
        return None
    parsed: object = json.loads(str(value))
    if isinstance(parsed, dict):
        return cast("dict[str, Any]", parsed)
    return {"value": parsed}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
