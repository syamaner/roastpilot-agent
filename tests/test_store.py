"""E6-S1: schema v1 and initialization (component plan §5, §8).

Write paths (E6-S2) and recovery reads / immutability (E6-S3) extend
this suite.
"""

from pathlib import Path

import aiosqlite as aiosqlite_module
import pytest

from roastpilot_agent import store as store_module
from roastpilot_agent.store import MIGRATIONS, RoastStore

EXPECTED_TABLES = {
    "roast_runs",
    "roast_events",
    "telemetry_snapshots",
    "safety_evaluations",
    "advisor_decisions",
    "command_log",
    "operator_actions",
    "sync_jobs",
    "reference_roasts",
}

EXPECTED_INDEXES = {
    "idx_roast_events_run_kind",
    "idx_telemetry_run_tick",
    "idx_safety_run_tick",
    "idx_advisor_run_tick",
    "idx_command_run_tick",
    "idx_roast_runs_sync_status",
}


async def fetch_names(store: RoastStore, kind: str) -> set[str]:
    async with store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
    ) as cursor:
        rows = await cursor.fetchall()
    return {str(row[0]) for row in rows if not str(row[0]).startswith("sqlite_")}


@pytest.mark.asyncio
async def test_schema_v1_creates_all_nine_tables(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        assert await fetch_names(tmp_store, "table") == EXPECTED_TABLES
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_schema_v1_creates_the_specified_indexes(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        assert await fetch_names(tmp_store, "index") >= EXPECTED_INDEXES
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_durability_pragmas_are_set(tmp_store: RoastStore) -> None:
    """WAL + synchronous=FULL (orchestration plan: active-roast power-loss
    protection is the default bias)."""
    await tmp_store.initialize()
    try:
        async with tmp_store.connection.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
        assert row is not None and str(row[0]).lower() == "wal"
        async with tmp_store.connection.execute("PRAGMA synchronous") as cursor:
            row = await cursor.fetchone()
        assert row is not None and int(row[0]) == 2  # 2 == FULL
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_schema_version_is_one(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        assert await tmp_store.schema_version() == 1
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_reinitialization_is_idempotent(tmp_path: Path) -> None:
    store = RoastStore(db_path=tmp_path / "idempotent.sqlite3")
    await store.initialize()
    await store.close()
    reopened = RoastStore(db_path=store.db_path)
    await reopened.initialize()
    try:
        assert await reopened.schema_version() == 1
        assert await fetch_names(reopened, "table") == EXPECTED_TABLES
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_migration_mechanism_applies_new_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An appended migration applies on re-open and bumps user_version —
    the append-only mechanism plan §8 asks to be test-covered."""
    store = RoastStore(db_path=tmp_path / "migrate.sqlite3")
    await store.initialize()
    await store.close()

    v2 = "CREATE TABLE migration_probe (id INTEGER PRIMARY KEY);"
    monkeypatch.setattr(store_module, "MIGRATIONS", (*MIGRATIONS, v2))
    upgraded = RoastStore(db_path=store.db_path)
    await upgraded.initialize()
    try:
        assert await upgraded.schema_version() == 2
        assert "migration_probe" in await fetch_names(upgraded, "table")
        # v1 content untouched.
        assert await fetch_names(upgraded, "table") >= EXPECTED_TABLES
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_foreign_keys_are_enforced(tmp_store: RoastStore) -> None:
    import aiosqlite

    await tmp_store.initialize()
    try:
        with pytest.raises(aiosqlite.IntegrityError):
            await tmp_store.connection.execute(
                "INSERT INTO roast_events (run_id, kind, source, recorded_at_utc)"
                " VALUES ('missing-run', 'fault', 'controller', '2026-06-07T00:00:00Z')"
            )
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_connection_before_initialize_is_an_error(tmp_store: RoastStore) -> None:
    with pytest.raises(RuntimeError):
        _ = tmp_store.connection
    await tmp_store.close()  # safe on never-initialized store


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding (E6-S1 PR): a migration that fails partway leaves no
    half-applied DDL and no stale version — the explicit per-migration
    transaction rolls everything back, so a clean retry is always possible."""
    store = RoastStore(db_path=tmp_path / "atomic.sqlite3")
    await store.initialize()
    await store.close()

    bad_v2 = (
        "CREATE TABLE migration_probe (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE roast_runs (id TEXT PRIMARY KEY);"  # collides: fails
    )
    monkeypatch.setattr(store_module, "MIGRATIONS", (*MIGRATIONS, bad_v2))
    broken = RoastStore(db_path=store.db_path)
    with pytest.raises(aiosqlite_module.OperationalError):
        await broken.initialize()

    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    recovered = RoastStore(db_path=store.db_path)
    await recovered.initialize()
    try:
        assert await recovered.schema_version() == 1  # version never bumped
        names = await fetch_names(recovered, "table")
        assert "migration_probe" not in names  # partial DDL rolled back
        assert names == EXPECTED_TABLES
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_enum_check_constraints_reject_invalid_values(
    tmp_store: RoastStore,
) -> None:
    """Review finding (E6-S1 PR): the documented text-enum columns are
    CHECK-enforced, so an E6-S2 write-path bug cannot silently store an
    invalid verdict/status/source."""
    import aiosqlite

    await tmp_store.initialize()
    try:
        await tmp_store.connection.execute(
            "INSERT INTO roast_runs (id, agent_phase, profile_json, config_json,"
            " started_at_utc, created_at_utc, updated_at_utc)"
            " VALUES ('run-1', 'idle', '{}', '{}', 't', 't', 't')"
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await tmp_store.connection.execute(
                "INSERT INTO safety_evaluations"
                " (run_id, tick, rule, verdict, reason, recorded_at_utc)"
                " VALUES ('run-1', 1, 'r', 'ALLOW', 'wrong case', 't')"
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await tmp_store.connection.execute(
                "UPDATE roast_runs SET cloud_sync_status = 'uploading' WHERE id = 'run-1'"
            )
    finally:
        await tmp_store.close()
