"""SQLite persistence (component plan §5; orchestration plan § Persistence).

E6-S1: schema v1 — the nine tables from plan §5 verbatim, the specified
indexes, WAL + ``synchronous=FULL`` durability PRAGMAs (active-roast
power-loss protection is the default bias), and a ``PRAGMA user_version``
migration mechanism. Write paths land in E6-S2, recovery reads in E6-S3.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import aiosqlite
from pydantic import BaseModel, ConfigDict

from roastpilot_agent.advisor import AdvisorContext, RoastDecision
from roastpilot_agent.config import AppConfig
from roastpilot_agent.models import (
    AdvisorTraceStatus,
    CommandTraceSource,
    CommandTraceStatus,
    LogManifest,
    RoastCommand,
    RoastDetail,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastSummary,
    RoastTelemetry,
    RoastTimeline,
    TelemetryPoint,
    TimelineAdvisorDecision,
    TimelineCommand,
    TimelineEvent,
    TimelineSafetyEvaluation,
    TimelineVerdict,
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

#: Ordered migration scripts; index+1 is the resulting PRAGMA user_version.
#: Append-only — never edit a shipped migration (plan §8: schema migration
#: is test-covered).
MIGRATIONS: tuple[str, ...] = (SCHEMA_V1, SCHEMA_V2_IMMUTABILITY)


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
        raw_state_json: str | None = None,
    ) -> bool:
        """Persist a telemetry row, throttled to ``interval_seconds``.

        The controller persists every tick; rows are only inserted every
        ``telemetry_log_interval_seconds`` (plan §5, default 5 s). Returns
        whether a row was written. The first row of a run always writes.
        """
        last = self._last_telemetry_elapsed.get(run_id)
        if last is not None and (elapsed_seconds - last) < interval_seconds:
            return False
        await self.connection.execute(
            "INSERT INTO telemetry_snapshots"
            " (run_id, tick, recorded_at_utc, elapsed_seconds, agent_phase, mcp_phase,"
            "  bean_temp_c, env_temp_c, bean_ror_c_per_min, env_ror_c_per_min,"
            "  heat_level_percent, fan_level_percent, cooling_on, development_percent,"
            "  raw_state_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                raw_state_json,
            ),
        )
        await self.connection.commit()
        self._last_telemetry_elapsed[run_id] = elapsed_seconds
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

    # --- E6-S3: recovery reads, run completion, immutability exceptions ---

    async def read_latest_run(self) -> PersistedRun | None:
        """The startup recovery read (orchestration plan § Persistence):
        the most recent run with its last persisted phase. None on a
        fresh database."""
        async with self.connection.execute(
            "SELECT id, agent_phase, outcome, started_at_utc, completed_at_utc,"
            " profile_json FROM roast_runs ORDER BY started_at_utc DESC, rowid DESC LIMIT 1"
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
            profile=RoastProfile.model_validate_json(str(row["profile_json"])),
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

        First-crack time (#111) is the *chronologically earliest* persisted
        ``first_crack`` roast event for the run (the ``idx_roast_events_run_kind``
        index covers the lookup), and is ``None`` for a run that never reached
        first crack — both the MCP detection and the operator-override FC paths
        emit that event, so either crossing is projected.

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
        counts reproduce the SPA's prior ``advisorSummary`` exactly: ``consults``
        is every persisted decision row; ``failed`` is those whose ``status`` is
        not ``ok``; ``clamped`` / ``rejected`` count a consult against the
        *latest* safety evaluation at the consult's ``tick`` (the same
        last-wins-by-tick join the SPA did against the timeline rows). The
        ``idx_advisor_run_tick`` / ``idx_safety_run_tick`` indexes cover the
        lookups; all are correlated subqueries (one statement, no N+1). A run with
        no advisor decisions yields zeros, which the SPA renders as "no advice".

        The clamp/reject verdict values the clamped/rejected subqueries compare
        against are sourced from the typed :class:`SafetyVerdict` enum
        (``SafetyVerdict.CLAMP.value`` / ``SafetyVerdict.REJECT.value``) and
        **bound as query parameters**, never raw SQL string literals (D15: a
        verdict rename must surface as a type error, not silently dodge the
        compare)."""
        async with self.connection.execute(
            "SELECT r.id, r.started_at_utc, r.completed_at_utc, r.agent_phase,"
            " r.outcome, r.profile_json, r.operator_rating,"
            " (SELECT t.development_percent FROM telemetry_snapshots t"
            "  WHERE t.run_id = r.id AND t.development_percent IS NOT NULL"
            "  ORDER BY t.tick DESC LIMIT 1) AS dev_pct,"
            " (SELECT e.recorded_at_utc FROM roast_events e"
            "  WHERE e.run_id = r.id AND e.kind = 'first_crack'"
            "  ORDER BY e.recorded_at_utc ASC LIMIT 1) AS fc_at,"
            " (SELECT COUNT(*) FROM advisor_decisions a"
            "  WHERE a.run_id = r.id) AS advisor_consults,"
            " (SELECT COUNT(*) FROM advisor_decisions a"
            "  WHERE a.run_id = r.id AND a.status != 'ok') AS advisor_failed,"
            " (SELECT COUNT(*) FROM advisor_decisions a WHERE a.run_id = r.id AND"
            "  (SELECT s.verdict FROM safety_evaluations s"
            "   WHERE s.run_id = r.id AND s.tick = a.tick"
            "   ORDER BY s.id DESC LIMIT 1) = ?) AS advisor_clamped,"
            " (SELECT COUNT(*) FROM advisor_decisions a WHERE a.run_id = r.id AND"
            "  (SELECT s.verdict FROM safety_evaluations s"
            "   WHERE s.run_id = r.id AND s.tick = a.tick"
            "   ORDER BY s.id DESC LIMIT 1) = ?) AS advisor_rejected"
            " FROM roast_runs r ORDER BY r.started_at_utc DESC, r.rowid DESC",
            # D15: the verdict values bound as query parameters come from the typed
            # SafetyVerdict enum, never raw SQL string literals — a rename of an
            # enum member is a pyright error here, not a silently-passing string.
            (SafetyVerdict.CLAMP.value, SafetyVerdict.REJECT.value),
        ) as cursor:
            rows = await cursor.fetchall()
        summaries: list[RoastSummary] = []
        for row in rows:
            profile = RoastProfile.model_validate_json(str(row["profile_json"]))
            summaries.append(
                RoastSummary(
                    id=str(row["id"]),
                    started_at_utc=str(row["started_at_utc"]),
                    completed_at_utc=None
                    if row["completed_at_utc"] is None
                    else str(row["completed_at_utc"]),
                    first_crack_at_utc=None if row["fc_at"] is None else str(row["fc_at"]),
                    agent_phase=RoastPhase(str(row["agent_phase"])),
                    outcome=row["outcome"],
                    bean_origin=profile.bean_origin,
                    bean_varietal=profile.bean_varietal,
                    country=profile.country,
                    bean_species=profile.bean_species,
                    is_blend=profile.is_blend,
                    rating=None if row["operator_rating"] is None else int(row["operator_rating"]),
                    development_percent=None if row["dev_pct"] is None else float(row["dev_pct"]),
                    advisor_consults=int(row["advisor_consults"]),
                    advisor_failed=int(row["advisor_failed"]),
                    advisor_clamped=int(row["advisor_clamped"]),
                    advisor_rejected=int(row["advisor_rejected"]),
                )
            )
        return summaries

    async def read_run(self, run_id: str) -> RoastDetail | None:
        """Run detail (plan §6): profile, phase, outcome, export manifest.
        ``None`` when no run has that id."""
        async with self.connection.execute(
            "SELECT id, agent_phase, profile_json, outcome, started_at_utc,"
            " completed_at_utc, fault_reason, operator_rating, operator_notes,"
            " export_manifest_json FROM roast_runs WHERE id = ?",
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
        return RoastDetail(
            id=str(row["id"]),
            agent_phase=agent_phase,
            profile=RoastProfile.model_validate_json(str(row["profile_json"])),
            outcome=row["outcome"],
            started_at_utc=str(row["started_at_utc"]),
            completed_at_utc=None
            if row["completed_at_utc"] is None
            else str(row["completed_at_utc"]),
            fault_reason=None if row["fault_reason"] is None else str(row["fault_reason"]),
            rating=None if row["operator_rating"] is None else int(row["operator_rating"]),
            notes=None if row["operator_notes"] is None else str(row["operator_notes"]),
            export_manifest=manifest,
            # Derived read-only from the phase (E10 option (a)): the SPA's action
            # bar mirrors this set; the live SSE phase_changed frame re-sends it.
            enabled_actions=enabled_operator_actions(agent_phase),
        )

    async def read_telemetry_points(
        self, run_id: str, *, downsample: int = 1
    ) -> list[TelemetryPoint]:
        """Tick-ordered telemetry snapshots, sampled every ``downsample`` rows.

        ``downsample`` must be >= 1; ``1`` returns every snapshot. The stride
        is index-based and keeps the first row, so the series start is stable
        regardless of stride."""
        if downsample < 1:
            raise ValueError("downsample must be >= 1")
        async with self.connection.execute(
            "SELECT tick, elapsed_seconds, agent_phase, bean_temp_c, env_temp_c,"
            " bean_ror_c_per_min, env_ror_c_per_min, heat_level_percent,"
            " fan_level_percent, cooling_on, development_percent"
            " FROM telemetry_snapshots WHERE run_id = ? ORDER BY tick ASC, id ASC",
            (run_id,),
        ) as cursor:
            rows = list(await cursor.fetchall())
        sampled = rows[::downsample]
        return [
            TelemetryPoint(
                tick=int(row["tick"]),
                elapsed_seconds=None
                if row["elapsed_seconds"] is None
                else float(row["elapsed_seconds"]),
                agent_phase=RoastPhase(str(row["agent_phase"])),
                bean_temp_c=None if row["bean_temp_c"] is None else float(row["bean_temp_c"]),
                env_temp_c=None if row["env_temp_c"] is None else float(row["env_temp_c"]),
                bean_ror_c_per_min=None
                if row["bean_ror_c_per_min"] is None
                else float(row["bean_ror_c_per_min"]),
                env_ror_c_per_min=None
                if row["env_ror_c_per_min"] is None
                else float(row["env_ror_c_per_min"]),
                heat_level_percent=None
                if row["heat_level_percent"] is None
                else int(row["heat_level_percent"]),
                fan_level_percent=None
                if row["fan_level_percent"] is None
                else int(row["fan_level_percent"]),
                cooling_on=None if row["cooling_on"] is None else bool(row["cooling_on"]),
                development_percent=None
                if row["development_percent"] is None
                else float(row["development_percent"]),
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
                monotonic_seconds=None
                if row["monotonic_seconds"] is None
                else float(row["monotonic_seconds"]),
                recorded_at_utc=str(row["recorded_at_utc"]),
                payload=_loads(row["payload_json"]),
            )
            for row in event_rows
        ]
        async with self.connection.execute(
            "SELECT tick, rule, verdict, input_heat, input_fan, adjusted_heat,"
            " adjusted_fan, reason, recorded_at_utc FROM safety_evaluations"
            " WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ) as cursor:
            safety_rows = await cursor.fetchall()
        safety_evaluations = [
            TimelineSafetyEvaluation(
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
            " recorded_at_utc FROM command_log WHERE run_id = ? ORDER BY id ASC",
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
