"""SQLite persistence (component plan §5; orchestration plan § Persistence).

E6-S1: schema v1 — the nine tables from plan §5 verbatim, the specified
indexes, WAL + ``synchronous=FULL`` durability PRAGMAs (active-roast
power-loss protection is the default bias), and a ``PRAGMA user_version``
migration mechanism. Write paths land in E6-S2, recovery reads in E6-S3.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite
from pydantic import BaseModel

from roastpilot_agent.advisor import AdvisorContext, RoastDecision
from roastpilot_agent.config import AppConfig
from roastpilot_agent.models import (
    RoastCommand,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.safety import SafetyEvaluation

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

#: Ordered migration scripts; index+1 is the resulting PRAGMA user_version.
#: Append-only — never edit a shipped migration (plan §8: schema migration
#: is test-covered).
MIGRATIONS: tuple[str, ...] = (SCHEMA_V1,)


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
                if "BEGIN" in script.upper():
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
