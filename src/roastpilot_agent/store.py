"""SQLite persistence (component plan §5; orchestration plan § Persistence).

E6-S1: schema v1 — the nine tables from plan §5 verbatim, the specified
indexes, WAL + ``synchronous=FULL`` durability PRAGMAs (active-roast
power-loss protection is the default bias), and a ``PRAGMA user_version``
migration mechanism. Write paths land in E6-S2, recovery reads in E6-S3.
"""

from pathlib import Path

import aiosqlite

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
