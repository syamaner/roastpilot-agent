"""E6-S1: schema v1 and initialization (component plan §5, §8).

Write paths (E6-S2) and recovery reads / immutability (E6-S3) extend
this suite.
"""

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from unittest import mock

import aiosqlite as aiosqlite_module
import pytest

from roastpilot_agent import roast_landmarks
from roastpilot_agent import store as store_module
from roastpilot_agent.store import (
    MIGRATIONS,
    PhysicallyImpossibleWeightError,
    RoastStore,
    RunActivelyDrivenError,
)

# The full migrated table set: the nine v1 tables plus the additive
# ``bean_profiles`` table from the v4 migration (#303) and the additive
# ``roast_tastings`` table from the v11 migration (#522).
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
    "bean_profiles",
    "bean_sourcing_attempts",
    "roast_tastings",
}

EXPECTED_INDEXES = {
    "idx_roast_events_run_kind",
    "idx_telemetry_run_tick",
    "idx_safety_run_tick",
    "idx_advisor_run_tick",
    "idx_command_run_tick",
    "idx_roast_runs_sync_status",
    "idx_bean_profiles_archived",
    "idx_bean_sourcing_attempt_expiry",
    "idx_roast_tastings_run",
}


async def fetch_names(store: RoastStore, kind: str) -> set[str]:
    async with store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
    ) as cursor:
        rows = await cursor.fetchall()
    return {str(row[0]) for row in rows if not str(row[0]).startswith("sqlite_")}


@pytest.mark.asyncio
async def test_migrations_create_all_expected_tables(tmp_store: RoastStore) -> None:
    """The nine v1 tables plus the additive v4 ``bean_profiles`` (#303) and
    v11 ``roast_tastings`` (#522) tables."""
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
async def test_schema_version_matches_migrations(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        assert await tmp_store.schema_version() == len(MIGRATIONS)
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
        assert await reopened.schema_version() == len(MIGRATIONS)
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
        assert await upgraded.schema_version() == len(MIGRATIONS) + 1
        assert "migration_probe" in await fetch_names(upgraded, "table")
        # v1 content untouched.
        assert await fetch_names(upgraded, "table") >= EXPECTED_TABLES
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_v6_rebuild_preserves_existing_roast_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#351: the v6 roast_events rebuild (to widen the kind CHECK) must COPY every
    existing row, not just recreate an empty table — the risky part of a
    CHECK-altering table swap. Stop a store at v5, write an event, then upgrade to
    the real (v6-including) MIGRATIONS and assert the old row survived AND the new
    ``drying_end`` kind is now accepted."""
    pre_v6 = MIGRATIONS[:5]  # V1..V5 (before the drying_end CHECK widening)
    assert len(pre_v6) == 5
    db_path = tmp_path / "v6upgrade.sqlite3"
    monkeypatch.setattr(store_module, "MIGRATIONS", pre_v6)
    old = RoastStore(db_path=db_path)
    await old.initialize()
    try:
        assert await old.schema_version() == 5
        await seeded_store(old)
        await old.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            payload={"bean_temp_c": 178.0},
        )
    finally:
        await old.close()

    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    upgraded = RoastStore(db_path=db_path)
    await upgraded.initialize()
    try:
        assert await upgraded.schema_version() == len(MIGRATIONS)
        timeline = await upgraded.read_timeline("run-1")
        kinds = [e.kind for e in timeline.events]
        # The pre-v6 event survived the rebuild's INSERT...SELECT copy.
        assert RoastEventKind.FIRST_CRACK in kinds
        # And the widened CHECK now accepts the new observability kind.
        await upgraded.record_event(
            run_id="run-1",
            kind=RoastEventKind.DRYING_END,
            source=RoastEventSource.CONTROLLER,
            payload={"bean_temp_c": 150.0, "threshold_c": 150.0},
        )
        after = await upgraded.read_timeline("run-1")
        assert RoastEventKind.DRYING_END in [e.kind for e in after.events]
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_v8_rebuild_preserves_existing_roast_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#409: the v8 roast_events rebuild (to widen the kind CHECK to include
    ``turning_point``) must COPY every existing row, not just recreate an empty
    table — the risky part of a CHECK-altering table swap. Stop a store at v7,
    write a ``drying_end`` event (a pre-existing kind), then upgrade to the real
    (v8-including) MIGRATIONS and assert the old row survived AND the new
    ``turning_point`` kind is now accepted."""
    pre_v8 = MIGRATIONS[:7]  # V1..V7 (before the turning_point CHECK widening)
    assert len(pre_v8) == 7
    db_path = tmp_path / "v8upgrade.sqlite3"
    monkeypatch.setattr(store_module, "MIGRATIONS", pre_v8)
    old = RoastStore(db_path=db_path)
    await old.initialize()
    try:
        assert await old.schema_version() == 7
        await seeded_store(old)
        await old.record_event(
            run_id="run-1",
            kind=RoastEventKind.DRYING_END,
            source=RoastEventSource.CONTROLLER,
            payload={"bean_temp_c": 151.0, "threshold_c": 150.0},
        )
    finally:
        await old.close()

    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    upgraded = RoastStore(db_path=db_path)
    await upgraded.initialize()
    try:
        assert await upgraded.schema_version() == len(MIGRATIONS)
        timeline = await upgraded.read_timeline("run-1")
        kinds = [e.kind for e in timeline.events]
        # The pre-v8 event survived the rebuild's INSERT...SELECT copy.
        assert RoastEventKind.DRYING_END in kinds
        # And the widened CHECK now accepts the new observability kind.
        await upgraded.record_event(
            run_id="run-1",
            kind=RoastEventKind.TURNING_POINT,
            source=RoastEventSource.CONTROLLER,
            payload={"bean_temp_c": 142.0, "elapsed_since_charge_seconds": 45.0},
        )
        after = await upgraded.read_timeline("run-1")
        assert RoastEventKind.TURNING_POINT in [e.kind for e in after.events]
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_v9_migration_adds_nullable_ambient_columns_back_compat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#342 (D85): a pre-v9 run upgrades cleanly and reads back NULL ambient
    (back-compat) until :meth:`RoastStore.set_ambient` is called."""
    pre_v9 = MIGRATIONS[:8]  # V1..V8 (before the ambient columns)
    assert len(pre_v9) == 8
    db_path = tmp_path / "v9upgrade.sqlite3"
    monkeypatch.setattr(store_module, "MIGRATIONS", pre_v9)
    old = RoastStore(db_path=db_path)
    await old.initialize()
    try:
        assert await old.schema_version() == 8
        await seeded_store(old)
    finally:
        await old.close()

    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    upgraded = RoastStore(db_path=db_path)
    await upgraded.initialize()
    try:
        assert await upgraded.schema_version() == len(MIGRATIONS)
        # The pre-existing row reads back NULL ambient (back-compat).
        detail = await upgraded.read_run("run-1")
        assert detail is not None
        assert detail.ambient_temp_c is None
        assert detail.ambient_humidity_pct is None
        assert detail.ambient_pressure_hpa is None
        summaries = await upgraded.list_runs()
        summary = next(s for s in summaries if s.id == "run-1")
        assert summary.ambient_temp_c is None
        assert summary.ambient_humidity_pct is None
        assert summary.ambient_pressure_hpa is None
        # And the new column now accepts a write.
        await upgraded.set_ambient(
            "run-1", temperature_c=28.49, humidity_percent=38.6, pressure_hpa=1008.56
        )
        after = await upgraded.read_run("run-1")
        assert after is not None
        assert after.ambient_temp_c == pytest.approx(28.49)
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_v10_migration_adds_explicit_ambient_captured_latch_back_compat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#463: a REAL pre-v10 row (written on the pre-v10 schema, no
    ``ambient_captured`` column yet — the old implicit ``ambient_temp_c IS NOT
    NULL`` derivation was live for this row) upgrades cleanly through a genuine
    migration round-trip, and its ``ambient_captured`` flag reads back
    ``False`` (default 0) post-migration — #463 is a new explicit tracking
    mechanism going forward, not a data backfill of historical captures."""
    pre_v10 = MIGRATIONS[:9]  # V1..V9 (before the explicit capture latch)
    assert len(pre_v10) == 9
    db_path = tmp_path / "v10upgrade.sqlite3"
    monkeypatch.setattr(store_module, "MIGRATIONS", pre_v10)
    old = RoastStore(db_path=db_path)
    await old.initialize()
    try:
        assert await old.schema_version() == 9
        await seeded_store(old)
        # A pre-v10 row that already captured ambient the OLD way (a non-null
        # reading, written directly against the v9-shaped table — no
        # ``ambient_captured`` column exists yet on this schema, so the real
        # ``set_ambient`` [which now also writes that column] cannot run here).
        await old.connection.execute(
            "UPDATE roast_runs SET ambient_temp_c = ?, ambient_humidity_pct = ?,"
            " ambient_pressure_hpa = ? WHERE id = 'run-1'",
            (28.49, 38.6, 1008.56),
        )
        await old.connection.commit()
    finally:
        await old.close()

    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    upgraded = RoastStore(db_path=db_path)
    await upgraded.initialize()
    try:
        assert await upgraded.schema_version() == len(MIGRATIONS)
        row = await fetch_one(
            upgraded, "SELECT ambient_captured FROM roast_runs WHERE id = 'run-1'"
        )
        assert row == (0,)
        persisted = await upgraded.read_latest_run()
        assert persisted is not None
        assert persisted.ambient_captured is False
        # The pre-existing ambient reading itself is untouched by the migration.
        detail = await upgraded.read_run("run-1")
        assert detail is not None
        assert detail.ambient_temp_c == pytest.approx(28.49)
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_v13_migration_adds_excluded_flag_back_compat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#582: a REAL pre-v13 completed run (written on the pre-v13 schema, no
    ``excluded`` column yet) upgrades cleanly through a genuine migration
    round-trip, and its ``excluded`` flag reads back ``False`` (default 0)
    post-migration — every pre-existing roast stays visible until explicitly
    discarded (zero behavior change for the whole existing corpus on
    upgrade), and it is NOT accidentally hidden by the new
    ``list_runs``/reference-retrieval ``excluded = 0`` filters."""
    pre_v13 = MIGRATIONS[:12]  # V1..V12 (before the soft-exclude flag)
    assert len(pre_v13) == 12
    db_path = tmp_path / "v13upgrade.sqlite3"
    monkeypatch.setattr(store_module, "MIGRATIONS", pre_v13)
    old = RoastStore(db_path=db_path)
    await old.initialize()
    try:
        assert await old.schema_version() == 12
        await seeded_store(old)
        # Complete the run against the pre-v13 schema — no ``excluded``
        # column exists yet, so this is a genuine pre-existing completed
        # roast (``complete_run`` only touches columns present since v1/v2).
        await old.complete_run(run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    finally:
        await old.close()

    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    upgraded = RoastStore(db_path=db_path)
    await upgraded.initialize()
    try:
        assert await upgraded.schema_version() == 16 == len(MIGRATIONS)
        row = await fetch_one(upgraded, "SELECT excluded FROM roast_runs WHERE id = 'run-1'")
        assert row == (0,)
        detail = await upgraded.read_run("run-1")
        assert detail is not None
        assert detail.excluded is False
        # Still visible in history — not accidentally hidden by the new
        # list_runs() excluded=0 filter.
        summaries = await upgraded.list_runs()
        assert [s.id for s in summaries] == ["run-1"]
        assert summaries[0].excluded is False
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_fresh_store_is_v16(tmp_store: RoastStore) -> None:
    """A brand-new store lands on the current (v16) schema version."""
    await tmp_store.initialize()
    try:
        assert await tmp_store.schema_version() == 16 == len(MIGRATIONS)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_v14_migration_upgrades_real_v13_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#588: v13 roast data survives the additive attempt-ledger migration."""
    db_path = tmp_path / "v14upgrade.sqlite3"
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[:13])
    old = RoastStore(db_path)
    await old.initialize()
    try:
        assert await old.schema_version() == 13
        await seeded_store(old)
    finally:
        await old.close()

    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    upgraded = RoastStore(db_path)
    await upgraded.initialize()
    try:
        assert await upgraded.schema_version() == 16
        assert await upgraded.read_run("run-1") is not None
        assert "bean_sourcing_attempts" in await fetch_names(upgraded, "table")
        assert "idx_bean_sourcing_attempt_expiry" in await fetch_names(upgraded, "index")
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_v15_migration_adds_catalogue_counts_to_real_v14_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#573: v14 attempt rows survive the additive aggregate-count migration."""
    db_path = tmp_path / "v15upgrade.sqlite3"
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[:14])
    old = RoastStore(db_path)
    await old.initialize()
    try:
        assert await old.schema_version() == 14
        attempt_id = await old.start_bean_sourcing_attempt(
            provider="provider", model_slug="model", prompt_version="v1"
        )
    finally:
        await old.close()

    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    upgraded = RoastStore(db_path)
    await upgraded.initialize()
    try:
        assert await upgraded.schema_version() == 16 == len(MIGRATIONS)
        row = await fetch_one(
            upgraded,
            "SELECT catalogue_discovered_count, catalogue_extracted_count"
            " FROM bean_sourcing_attempts WHERE id = ?",
            (attempt_id,),
        )
        assert row == (None, None)
        for discovered_count, extracted_count in ((25, 1), (1, 2)):
            with pytest.raises(aiosqlite_module.IntegrityError):
                await upgraded.connection.execute(
                    "UPDATE bean_sourcing_attempts SET catalogue_discovered_count = ?,"
                    " catalogue_extracted_count = ? WHERE id = ?",
                    (discovered_count, extracted_count, attempt_id),
                )
            await upgraded.connection.rollback()
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_v16_migration_adds_nullable_d96_trace_to_real_v15_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#699: v15 telemetry survives the additive D96 trace migration."""
    db_path = tmp_path / "v16upgrade.sqlite3"
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[:15])
    old = RoastStore(db_path)
    await old.initialize()
    try:
        assert await old.schema_version() == 15
        await seeded_store(old)
        # Use the actual v15 column set: the current write method quite
        # correctly targets v16 and therefore cannot manufacture a pre-v16
        # row after the migration list has been pinned back.
        await old.connection.execute(
            "INSERT INTO telemetry_snapshots"
            " (run_id, tick, recorded_at_utc, elapsed_seconds, agent_phase,"
            "  bean_temp_c, env_temp_c) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                1,
                datetime.now(UTC).isoformat(),
                5.0,
                RoastPhase.DEVELOPMENT.value,
                184.0,
                205.0,
            ),
        )
        await old.connection.commit()
    finally:
        await old.close()

    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    upgraded = RoastStore(db_path)
    await upgraded.initialize()
    try:
        assert await upgraded.schema_version() == 16 == len(MIGRATIONS)
        [point] = await upgraded.read_telemetry_points("run-1")
        assert point.post_fc_recovery_enabled is None
        assert point.post_fc_heat_authority_state is None
        assert point.post_fc_ror_setpoint_c_per_min is None
        assert point.post_fc_smoothed_ror_c_per_min is None
        assert point.post_fc_effective_heat_ceiling_percent is None
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_set_ambient_sets_the_explicit_captured_flag(tmp_store: RoastStore) -> None:
    """#463: a real reading marks the explicit ``ambient_captured`` flag, and
    ``PersistedRun.ambient_captured`` (the recovery read) reflects it — not the
    old ``ambient_temp_c IS NOT NULL`` derivation."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.set_ambient(
            "run-1", temperature_c=28.49, humidity_percent=38.6, pressure_hpa=1008.56
        )
        row = await fetch_one(
            tmp_store, "SELECT ambient_captured FROM roast_runs WHERE id = 'run-1'"
        )
        assert row == (1,)
        persisted = await tmp_store.read_latest_run()
        assert persisted is not None
        assert persisted.ambient_captured is True
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_ambient_with_null_reading_still_sets_the_captured_flag(
    tmp_store: RoastStore,
) -> None:
    """#463 (the fix's whole point): a ``status='ok'``-with-null-temperature (or
    an unavailable/disabled probe) capture still latches — ``ambient_captured``
    is ``True`` even though every triad field reads back ``None``. This is the
    exact edge the old ``ambient_temp_c IS NOT NULL`` derivation got wrong (it
    would have read back ``False`` here and could re-fire post-restart)."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.set_ambient(
            "run-1", temperature_c=None, humidity_percent=None, pressure_hpa=None
        )
        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.ambient_temp_c is None  # the reading itself is null

        row = await fetch_one(
            tmp_store, "SELECT ambient_captured FROM roast_runs WHERE id = 'run-1'"
        )
        assert row == (1,)  # but the capture RAN — the flag still latches

        persisted = await tmp_store.read_latest_run()
        assert persisted is not None
        assert persisted.ambient_captured is True
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_ambient_captured_false_for_a_run_that_never_captured(
    tmp_store: RoastStore,
) -> None:
    """#463: a run that never charged / never ran the capture (pre-#342, or a
    #342-era run that simply hasn't charged yet) reads ``ambient_captured`` as
    ``False`` — the default-0 back-compat baseline this fix must preserve."""
    await seeded_store(tmp_store)
    try:
        persisted = await tmp_store.read_latest_run()
        assert persisted is not None
        assert persisted.ambient_captured is False
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_ambient_persists_and_reads_back(tmp_store: RoastStore) -> None:
    """#342: the ambient triad round-trips through both the detail and summary
    read paths."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.set_ambient(
            "run-1", temperature_c=28.49, humidity_percent=38.6, pressure_hpa=1008.56
        )
        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.ambient_temp_c == pytest.approx(28.49)
        assert detail.ambient_humidity_pct == pytest.approx(38.6)
        assert detail.ambient_pressure_hpa == pytest.approx(1008.56)

        summaries = await tmp_store.list_runs()
        summary = next(s for s in summaries if s.id == "run-1")
        assert summary.ambient_temp_c == pytest.approx(28.49)
        assert summary.ambient_humidity_pct == pytest.approx(38.6)
        assert summary.ambient_pressure_hpa == pytest.approx(1008.56)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_ambient_persists_nulls_when_unavailable(tmp_store: RoastStore) -> None:
    """#342: an unavailable/disabled MCP ambient config persists nulls, never
    raises — the fail-soft contract, store-side."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.set_ambient(
            "run-1", temperature_c=None, humidity_percent=None, pressure_hpa=None
        )
        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.ambient_temp_c is None
        assert detail.ambient_humidity_pct is None
        assert detail.ambient_pressure_hpa is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_ambient_on_unknown_run_raises(tmp_store: RoastStore) -> None:
    await seeded_store(tmp_store)
    try:
        with pytest.raises(RuntimeError, match="no roast_run"):
            await tmp_store.set_ambient(
                "ghost-run", temperature_c=28.49, humidity_percent=38.6, pressure_hpa=1008.56
            )
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_foreign_keys_are_enforced(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        with pytest.raises(aiosqlite_module.IntegrityError):
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
        assert await recovered.schema_version() == len(MIGRATIONS)  # never bumped
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
    await tmp_store.initialize()
    try:
        await tmp_store.connection.execute(
            "INSERT INTO roast_runs (id, agent_phase, profile_json, config_json,"
            " started_at_utc, created_at_utc, updated_at_utc)"
            " VALUES ('run-1', 'idle', '{}', '{}', 't', 't', 't')"
        )
        with pytest.raises(aiosqlite_module.IntegrityError):
            await tmp_store.connection.execute(
                "INSERT INTO safety_evaluations"
                " (run_id, tick, rule, verdict, reason, recorded_at_utc)"
                " VALUES ('run-1', 1, 'r', 'ALLOW', 'wrong case', 't')"
            )
        with pytest.raises(aiosqlite_module.IntegrityError):
            await tmp_store.connection.execute(
                "UPDATE roast_runs SET cloud_sync_status = 'uploading' WHERE id = 'run-1'"
            )
        with pytest.raises(aiosqlite_module.IntegrityError):
            await tmp_store.connection.execute(
                "UPDATE roast_runs SET agent_phase = 'warming_up' WHERE id = 'run-1'"
            )
        with pytest.raises(aiosqlite_module.IntegrityError):
            await tmp_store.connection.execute(
                "UPDATE roast_runs SET ambient_captured = 2 WHERE id = 'run-1'"
            )
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_initialize_twice_on_same_instance_is_a_noop(
    tmp_store: RoastStore,
) -> None:
    await tmp_store.initialize()
    try:
        first = tmp_store.connection
        await tmp_store.initialize()  # second call: keeps the live connection
        assert tmp_store.connection is first
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_migration_embedding_transaction_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_apply_migrations owns BEGIN/COMMIT — an embedded transaction in a
    migration script is a programming error, caught loudly."""
    store = RoastStore(db_path=tmp_path / "guard.sqlite3")
    await store.initialize()
    await store.close()
    bad = "BEGIN; CREATE TABLE nope (id INTEGER); COMMIT;"
    monkeypatch.setattr(store_module, "MIGRATIONS", (*MIGRATIONS, bad))
    broken = RoastStore(db_path=store.db_path)
    with pytest.raises(ValueError, match="embeds its own transaction"):
        await broken.initialize()


# --- E6-S2: write paths ---


from roastpilot_agent.advisor import AdvisorContext, RoastDecision  # noqa: E402
from roastpilot_agent.config import AppConfig  # noqa: E402
from roastpilot_agent.models import (  # noqa: E402
    PostFcHeatAuthorityState,
    ReferenceLandmarks,
    ReferenceRoast,
    RoastCommand,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
    recording_origin_slug,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict  # noqa: E402

PROFILE = RoastProfile(
    name="store-test",
    bean_origin="Ethiopia",
    bean_weight_grams=250.0,
    initial_heat_percent=70,
    initial_fan_percent=40,
    target_drop_temp_c=205.0,
    target_development_percent=20.0,
)


async def seeded_store(
    store: RoastStore, run_id: str = "run-1", started_at_utc: str | None = None
) -> RoastStore:
    await store.initialize()
    await store.create_run(
        run_id=run_id,
        profile=PROFILE,
        config=AppConfig(),
        agent_phase=RoastPhase.STARTING,
        started_at_utc=started_at_utc,
    )
    return store


async def fetch_one(
    store: RoastStore, sql: str, parameters: tuple[object, ...] = ()
) -> tuple[object, ...]:
    async with store.connection.execute(sql, parameters) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return tuple(row)


@pytest.mark.asyncio
async def test_create_run_freezes_profile_and_config(tmp_store: RoastStore) -> None:
    await seeded_store(tmp_store)
    try:
        row = await fetch_one(
            tmp_store, "SELECT agent_phase, profile_json, config_json FROM roast_runs"
        )
        assert row[0] == "starting"
        assert '"name":"store-test"' in str(row[1])
        assert "tick_interval_seconds" in str(row[2])
        assert "pre_t0_max_bean_temp_c" in str(row[2])
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_typed_writers_round_trip_enum_values(tmp_store: RoastStore) -> None:
    """Every writer stores the lowercase wire values the CHECKs enforce."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.T0_DETECTED,
            source=RoastEventSource.MCP,
            monotonic_seconds=120.5,
            payload={"bean_temp_c": 156.0},
        )
        evaluation_id = await tmp_store.record_safety_evaluation(
            run_id="run-1",
            tick=7,
            evaluation=SafetyEvaluation(
                rule="command_bounds",
                verdict=SafetyVerdict.CLAMP,
                input_heat=120,
                input_fan=40,
                adjusted_heat=100,
                adjusted_fan=40,
                reason="clamped",
            ),
        )
        await tmp_store.record_command(
            run_id="run-1",
            tick=7,
            tool=RoastCommand.SET_HEAT,
            source="advisor",
            status="ok",
            args={"heat_level_percent": 100},
            safety_evaluation_id=evaluation_id,
        )
        await tmp_store.record_operator_action(action="emergency_stop", result="accepted")

        event = await fetch_one(tmp_store, "SELECT kind, source, payload_json FROM roast_events")
        assert event[0] == "t0_detected" and event[1] == "mcp"
        safety = await fetch_one(
            tmp_store,
            "SELECT verdict, input_heat, adjusted_heat FROM safety_evaluations",
        )
        assert safety == ("clamp", 120, 100)
        command = await fetch_one(
            tmp_store,
            "SELECT tool, source, status, safety_evaluation_id FROM command_log",
        )
        assert command == ("set_heat", "advisor", "ok", evaluation_id)
        action = await fetch_one(tmp_store, "SELECT run_id, action, result FROM operator_actions")
        assert action == (None, "emergency_stop", "accepted")
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_timeline_round_trips_safety_ids_and_command_provenance(
    tmp_store: RoastStore,
) -> None:
    """Timeline wire rows retain safety IDs and both command FK states (#787)."""
    await seeded_store(tmp_store)
    try:
        evaluation_id = await tmp_store.record_safety_evaluation(
            run_id="run-1",
            tick=7,
            evaluation=SafetyEvaluation(
                rule="command_bounds",
                verdict=SafetyVerdict.CLAMP,
                input_heat=120,
                input_fan=40,
                adjusted_heat=100,
                adjusted_fan=40,
                reason="clamped",
            ),
        )
        await tmp_store.record_command(
            run_id="run-1",
            tick=7,
            tool=RoastCommand.SET_HEAT,
            source="advisor",
            status="ok",
            safety_evaluation_id=evaluation_id,
        )
        await tmp_store.record_command(
            run_id="run-1",
            tick=7,
            tool=RoastCommand.SET_FAN,
            source="operator",
            status="ok",
        )

        timeline = await tmp_store.read_timeline("run-1")
        assert [evaluation.id for evaluation in timeline.safety_evaluations] == [evaluation_id]
        assert [command.safety_evaluation_id for command in timeline.commands] == [
            evaluation_id,
            None,
        ]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_advisor_decision_stores_hash_never_raw_context(
    tmp_store: RoastStore,
) -> None:
    await seeded_store(tmp_store)
    try:
        context = AdvisorContext(
            phase=RoastPhase.DEVELOPMENT,
            roast_elapsed_seconds=500.0,
            development_elapsed_seconds=30.0,
            current_bean_temp_c=197.25,
            current_env_temp_c=215.0,
            bean_ror_c_per_min=8.0,
            env_ror_c_per_min=6.0,
            target_drop_temp_c=205.0,
            profile_name="store-test",
        )
        decision = RoastDecision(
            target_heat=45, target_fan=60, should_drop=False, confidence=0.8, rationale="hold"
        )
        await tmp_store.record_advisor_decision(
            run_id="run-1",
            tick=9,
            provider="openrouter",
            model="test-model",
            prompt_version="v0",
            context=context,
            latency_ms=420,
            decision=decision,
            status="ok",
        )
        row = await fetch_one(
            tmp_store, "SELECT context_hash, decision_json, status FROM advisor_decisions"
        )
        context_hash = str(row[0])
        assert len(context_hash) == 64 and all(c in "0123456789abcdef" for c in context_hash)
        assert "197.25" not in context_hash  # the hash, never the payload
        assert row[1] is not None and '"target_heat":45' in str(row[1])
        assert row[2] == "ok"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_telemetry_rows_are_interval_throttled(tmp_store: RoastStore) -> None:
    """Plan §5: persist every tick, insert rows every
    telemetry_log_interval_seconds (5 s default). First row always writes."""
    await seeded_store(tmp_store)
    try:
        reading = RoastTelemetry(bean_temp_c=150.0, env_temp_c=170.0)
        outcomes: list[bool] = []
        for tick, elapsed in [(1, 0.0), (3, 2.0), (6, 5.0), (8, 7.0), (11, 10.0)]:
            outcomes.append(
                await tmp_store.record_telemetry(
                    run_id="run-1",
                    tick=tick,
                    agent_phase=RoastPhase.PREHEATING,
                    elapsed_seconds=elapsed,
                    interval_seconds=5.0,
                    telemetry=reading,
                )
            )
        assert outcomes == [True, False, True, False, True]
        row = await fetch_one(tmp_store, "SELECT COUNT(*) FROM telemetry_snapshots")
        assert row[0] == 3
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_telemetry_round_trips_charge_elapsed_seconds(tmp_store: RoastStore) -> None:
    """#308 (V5): the charge-referenced roast clock is persisted and read back.

    The REST telemetry series re-origins the chart x-axis at charge on a
    history/reload read, so the per-snapshot ``charge_elapsed_seconds`` must
    survive the round-trip. ``None`` before charge (the pre-charge lead-in),
    a since-charge value after T0 — both persisted exactly and reconstructed
    on :meth:`read_telemetry_points`."""
    await seeded_store(tmp_store)
    try:
        reading = RoastTelemetry(bean_temp_c=150.0, env_temp_c=170.0)
        # Tick 1: pre-charge → charge_elapsed_seconds is None (the chart lead-in).
        await tmp_store.record_telemetry(
            run_id="run-1",
            tick=1,
            agent_phase=RoastPhase.PREHEATING,
            elapsed_seconds=0.0,
            interval_seconds=5.0,
            telemetry=reading,
            charge_elapsed_seconds=None,
        )
        # Tick 2: post-charge → the operator-facing roast clock since T0.
        await tmp_store.record_telemetry(
            run_id="run-1",
            tick=2,
            agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            elapsed_seconds=30.0,
            interval_seconds=5.0,
            telemetry=reading,
            charge_elapsed_seconds=12.0,
        )
        points = await tmp_store.read_telemetry_points("run-1")
        assert [p.charge_elapsed_seconds for p in points] == [None, 12.0]
        # The serve-referenced clock is retained and distinct (the chart's raw x).
        assert [p.elapsed_seconds for p in points] == [0.0, 30.0]
    finally:
        await tmp_store.close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (12.5, 12.5),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
    ],
)
def test_optional_float_projects_non_finite_values_as_absent(
    value: float | None, expected: float | None
) -> None:
    """Nullable SQLite REAL projection rejects every non-finite value."""
    result = RoastStore._optional_float(value)  # pyright: ignore[reportPrivateUsage]
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


@pytest.mark.asyncio
async def test_telemetry_read_projects_persisted_non_finite_float_as_absent(
    tmp_store: RoastStore,
) -> None:
    """A pre-fix telemetry REAL row cannot leak non-finite data from the store."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.record_telemetry(
            run_id="run-1",
            tick=1,
            agent_phase=RoastPhase.DEVELOPMENT,
            elapsed_seconds=5.0,
            interval_seconds=5.0,
            telemetry=RoastTelemetry(
                bean_temp_c=184.0,
                env_temp_c=205.0,
                bean_ror_c_per_min=4.2,
            ),
        )
        await tmp_store.connection.execute(
            "UPDATE telemetry_snapshots SET bean_ror_c_per_min = ? WHERE run_id = ?",
            (float("-inf"), "run-1"),
        )
        await tmp_store.connection.commit()

        [point] = await tmp_store.read_telemetry_points("run-1")
        assert point.bean_ror_c_per_min is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_projects_non_finite_development_percent_as_absent(
    tmp_store: RoastStore,
) -> None:
    """History cannot admit a legacy non-finite development percentage."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.record_telemetry(
            run_id="run-1",
            tick=1,
            agent_phase=RoastPhase.DEVELOPMENT,
            elapsed_seconds=5.0,
            interval_seconds=0.0,
            telemetry=None,
            development_percent=12.5,
        )
        await tmp_store.connection.execute(
            "UPDATE telemetry_snapshots SET development_percent = ? WHERE run_id = ?",
            (float("inf"), "run-1"),
        )
        await tmp_store.connection.commit()

        [summary] = await tmp_store.list_runs()
        assert summary.development_percent is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_timeline_projects_non_finite_monotonic_seconds_as_absent(
    tmp_store: RoastStore,
) -> None:
    """Timeline reads cannot admit a legacy non-finite monotonic timestamp."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.RUN_STARTED,
            source=RoastEventSource.CONTROLLER,
            monotonic_seconds=1.0,
        )
        await tmp_store.connection.execute(
            "UPDATE roast_events SET monotonic_seconds = ? WHERE run_id = ?",
            (float("-inf"), "run-1"),
        )
        await tmp_store.connection.commit()

        [event] = (await tmp_store.read_timeline("run-1")).events
        assert event.monotonic_seconds is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_telemetry_read_preserves_insertion_order_across_tick_restart(
    tmp_store: RoastStore,
) -> None:
    """A recovered process's reset tick counter must not reorder one run."""
    await seeded_store(tmp_store)
    restarted: RoastStore | None = None
    try:
        reading = RoastTelemetry(bean_temp_c=185.0, env_temp_c=205.0)
        for tick, elapsed, charge in [(10, 100.0, 50.0), (11, 105.0, 55.0)]:
            assert await tmp_store.record_telemetry(
                run_id="run-1",
                tick=tick,
                agent_phase=RoastPhase.DEVELOPMENT,
                elapsed_seconds=elapsed,
                interval_seconds=5.0,
                telemetry=reading,
                charge_elapsed_seconds=charge,
            )

        # A real agent restart constructs a fresh store and tick counter. Its
        # durable insertion ids continue, while process-local ticks restart at 0.
        await tmp_store.close()
        restarted = RoastStore(tmp_store.db_path)
        await restarted.initialize()
        for tick, elapsed, charge in [(0, 0.0, 180.0), (1, 5.0, 185.0)]:
            assert await restarted.record_telemetry(
                run_id="run-1",
                tick=tick,
                agent_phase=RoastPhase.OPERATOR_RECOVERY_REQUIRED,
                elapsed_seconds=elapsed,
                interval_seconds=5.0,
                telemetry=reading,
                charge_elapsed_seconds=charge,
            )

        points = await restarted.read_telemetry_points("run-1")
        assert [point.tick for point in points] == [10, 11, 0, 1]
        sampled = await restarted.read_telemetry_points("run-1", downsample=2)
        assert [point.tick for point in sampled] == [10, 0]
    finally:
        if restarted is not None:
            await restarted.close()
        await tmp_store.close()


@pytest.mark.asyncio
async def test_telemetry_round_trips_d96_validation_trace(tmp_store: RoastStore) -> None:
    """#699: retained telemetry preserves the controller-owned D96 trace."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.record_telemetry(
            run_id="run-1",
            tick=1,
            agent_phase=RoastPhase.DEVELOPMENT,
            elapsed_seconds=5.0,
            interval_seconds=5.0,
            telemetry=RoastTelemetry(bean_temp_c=184.0, env_temp_c=205.0),
            post_fc_recovery_enabled=True,
            post_fc_heat_authority_state=PostFcHeatAuthorityState.RECOVERING,
            post_fc_ror_setpoint_c_per_min=6.4,
            post_fc_smoothed_ror_c_per_min=4.8,
            post_fc_effective_heat_ceiling_percent=75,
        )
        [point] = await tmp_store.read_telemetry_points("run-1")
        assert point.post_fc_recovery_enabled is True
        assert point.post_fc_heat_authority_state is PostFcHeatAuthorityState.RECOVERING
        assert point.post_fc_ror_setpoint_c_per_min == pytest.approx(6.4)
        assert point.post_fc_smoothed_ror_c_per_min == pytest.approx(4.8)
        assert point.post_fc_effective_heat_ceiling_percent == 75
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_d96_authority_transitions_bypass_periodic_telemetry_throttle(
    tmp_store: RoastStore,
) -> None:
    """#699: short D96 cycles remain observable at a slower sample cadence."""
    await seeded_store(tmp_store)
    try:
        reading = RoastTelemetry(bean_temp_c=185.0, env_temp_c=205.0)
        states = [
            PostFcHeatAuthorityState.HOLDING,
            PostFcHeatAuthorityState.RECOVERING,
            PostFcHeatAuthorityState.GLIDING,
            PostFcHeatAuthorityState.RECOVERING,
            PostFcHeatAuthorityState.HOLDING,
        ]
        outcomes: list[bool] = []
        for tick, state in enumerate(states):
            outcomes.append(
                await tmp_store.record_telemetry(
                    run_id="run-1",
                    tick=tick,
                    agent_phase=RoastPhase.DEVELOPMENT,
                    elapsed_seconds=float(tick * 5),
                    interval_seconds=60.0,
                    telemetry=reading,
                    charge_elapsed_seconds=float(100 + tick * 5),
                    post_fc_recovery_enabled=True,
                    post_fc_heat_authority_state=state,
                    post_fc_ror_setpoint_c_per_min=6.4,
                    post_fc_smoothed_ror_c_per_min=4.8,
                    post_fc_effective_heat_ceiling_percent=75,
                )
            )

        # Every authority transition is durable even though none reaches the
        # ordinary 60-second sample boundary; an unchanged state still throttles.
        outcomes.append(
            await tmp_store.record_telemetry(
                run_id="run-1",
                tick=5,
                agent_phase=RoastPhase.DEVELOPMENT,
                elapsed_seconds=25.0,
                interval_seconds=60.0,
                telemetry=reading,
                charge_elapsed_seconds=125.0,
                post_fc_recovery_enabled=True,
                post_fc_heat_authority_state=PostFcHeatAuthorityState.HOLDING,
            )
        )
        # Leaving DEVELOPMENT clears the controller output; persist that
        # non-null -> null boundary even inside the periodic interval.
        outcomes.append(
            await tmp_store.record_telemetry(
                run_id="run-1",
                tick=6,
                agent_phase=RoastPhase.COOLING,
                elapsed_seconds=30.0,
                interval_seconds=60.0,
                telemetry=reading,
                charge_elapsed_seconds=130.0,
                post_fc_recovery_enabled=True,
                post_fc_heat_authority_state=None,
            )
        )

        assert outcomes == [True, True, True, True, True, False, True]
        points = await tmp_store.read_telemetry_points("run-1")
        assert [point.post_fc_heat_authority_state for point in points] == [
            *states,
            None,
        ]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_d96_development_exit_bypasses_throttle_with_same_historical_witness(
    tmp_store: RoastStore,
) -> None:
    """A same-state historical witness must not hide its phase boundary."""
    await seeded_store(tmp_store)
    try:
        reading = RoastTelemetry(bean_temp_c=196.0, env_temp_c=210.0)
        outcomes = [
            await tmp_store.record_telemetry(
                run_id="run-1",
                tick=1,
                agent_phase=RoastPhase.DEVELOPMENT,
                elapsed_seconds=100.0,
                interval_seconds=60.0,
                telemetry=reading,
                charge_elapsed_seconds=50.0,
                post_fc_recovery_enabled=True,
                post_fc_heat_authority_state=PostFcHeatAuthorityState.RECOVERING,
            ),
            await tmp_store.record_telemetry(
                run_id="run-1",
                tick=2,
                agent_phase=RoastPhase.COOLING,
                elapsed_seconds=101.0,
                interval_seconds=60.0,
                telemetry=reading,
                charge_elapsed_seconds=51.0,
                post_fc_recovery_enabled=True,
                post_fc_heat_authority_state=PostFcHeatAuthorityState.RECOVERING,
            ),
            await tmp_store.record_telemetry(
                run_id="run-1",
                tick=3,
                agent_phase=RoastPhase.COOLING,
                elapsed_seconds=102.0,
                interval_seconds=60.0,
                telemetry=reading,
                charge_elapsed_seconds=51.0,
                post_fc_recovery_enabled=True,
                post_fc_heat_authority_state=PostFcHeatAuthorityState.RECOVERING,
            ),
            await tmp_store.record_telemetry(
                run_id="run-1",
                tick=4,
                agent_phase=RoastPhase.COOLING,
                elapsed_seconds=103.0,
                interval_seconds=60.0,
                telemetry=reading,
                charge_elapsed_seconds=51.0,
                post_fc_recovery_enabled=True,
                post_fc_heat_authority_state=None,
            ),
        ]

        assert outcomes == [True, True, False, True]
        points = await tmp_store.read_telemetry_points("run-1")
        assert [point.agent_phase for point in points] == [
            RoastPhase.DEVELOPMENT,
            RoastPhase.COOLING,
            RoastPhase.COOLING,
        ]
        assert [point.post_fc_heat_authority_state for point in points] == [
            PostFcHeatAuthorityState.RECOVERING,
            PostFcHeatAuthorityState.RECOVERING,
            None,
        ]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_drying_end_event_round_trips_through_timeline(tmp_store: RoastStore) -> None:
    """#351: the v6 ``drying_end`` event kind is accepted by the rebuilt
    roast_events CHECK and surfaces on the persisted timeline (the detail page),
    proving the new observability event reaches the store and back."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.DRYING_END,
            source=RoastEventSource.CONTROLLER,
            monotonic_seconds=210.0,
            payload={"bean_temp_c": 150.0, "threshold_c": 150.0},
        )
        timeline = await tmp_store.read_timeline("run-1")
        drying = [e for e in timeline.events if e.kind is RoastEventKind.DRYING_END]
        assert len(drying) == 1
        assert drying[0].source is RoastEventSource.CONTROLLER
        assert drying[0].payload == {"bean_temp_c": 150.0, "threshold_c": 150.0}
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_writes_commit_per_tick(tmp_store: RoastStore) -> None:
    """Another connection sees every write immediately — proof that each
    writer commits (power loss never costs a committed tick)."""
    import aiosqlite as sqlite_check

    await seeded_store(tmp_store)
    try:
        await tmp_store.record_event(
            run_id="run-1", kind=RoastEventKind.RUN_STARTED, source=RoastEventSource.CONTROLLER
        )
        other = await sqlite_check.connect(tmp_store.db_path)
        try:
            async with other.execute("SELECT COUNT(*) FROM roast_events") as cursor:
                row = await cursor.fetchone()
            assert row is not None and row[0] == 1
        finally:
            await other.close()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_update_run_phase_touches_updated_at(tmp_store: RoastStore) -> None:
    await seeded_store(tmp_store)
    try:
        before = await fetch_one(tmp_store, "SELECT created_at_utc, updated_at_utc FROM roast_runs")
        await asyncio_sleep_tiny()
        await tmp_store.update_run_phase("run-1", RoastPhase.PREHEATING)
        row = await fetch_one(
            tmp_store, "SELECT agent_phase, created_at_utc, updated_at_utc FROM roast_runs"
        )
        assert row[0] == "preheating"
        assert row[1] == before[0]  # created_at untouched
        assert row[2] != before[1]  # updated_at actually advanced
    finally:
        await tmp_store.close()


async def asyncio_sleep_tiny() -> None:
    import asyncio

    await asyncio.sleep(0.002)  # isoformat carries microseconds; ensure a delta


@pytest.mark.asyncio
async def test_update_run_phase_on_unknown_run_raises(tmp_store: RoastStore) -> None:
    """Review finding (E6-S2 PR): a silent no-op would corrupt the
    restart-recovery breadcrumb."""
    await seeded_store(tmp_store)
    try:
        with pytest.raises(RuntimeError, match="no roast_run"):
            await tmp_store.update_run_phase("ghost-run", RoastPhase.PREHEATING)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_advisor_failure_persists_null_decision(tmp_store: RoastStore) -> None:
    """Review finding (E6-S2 PR): the timeout path — the one that fires
    when the LLM is unreachable — stores SQL NULL, not a JSON null."""
    await seeded_store(tmp_store)
    try:
        context = AdvisorContext(
            phase=RoastPhase.DEVELOPMENT,
            roast_elapsed_seconds=500.0,
            development_elapsed_seconds=None,
            current_bean_temp_c=197.0,
            current_env_temp_c=215.0,
            bean_ror_c_per_min=None,
            env_ror_c_per_min=None,
            target_drop_temp_c=205.0,
            profile_name="store-test",
        )
        await tmp_store.record_advisor_decision(
            run_id="run-1",
            tick=12,
            provider="openrouter",
            model="test-model",
            prompt_version="v0",
            context=context,
            latency_ms=None,
            decision=None,
            status="timeout",
        )
        row = await fetch_one(
            tmp_store, "SELECT decision_json, status, latency_ms FROM advisor_decisions"
        )
        assert row == (None, "timeout", None)
    finally:
        await tmp_store.close()


# --- E6-S3: recovery reads and immutability ---


@pytest.mark.asyncio
async def test_read_latest_run_on_fresh_database(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        assert await tmp_store.read_latest_run() is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [RoastPhase.PREHEATING, RoastPhase.DEVELOPMENT, RoastPhase.COOLING],
)
async def test_restart_scenario_recovers_persisted_phase(tmp_path: Path, phase: RoastPhase) -> None:
    """Plan §8: restart during preheat / development / cooling — a fresh
    store instance (the restarted process) reads back the exact phase."""
    store = await seeded_store(RoastStore(db_path=tmp_path / "restart.sqlite3"))
    await store.update_run_phase("run-1", phase)
    await store.close()  # process dies here

    restarted = RoastStore(db_path=store.db_path)
    await restarted.initialize()
    try:
        persisted = await restarted.read_latest_run()
        assert persisted is not None
        assert persisted.run_id == "run-1"
        assert persisted.agent_phase is phase
        assert persisted.outcome is None  # still active when the process died
        assert persisted.profile.name == "store-test"
        assert persisted.frozen_config is not None
        assert persisted.frozen_config.controller == AppConfig().controller
        assert persisted.frozen_config.safety == AppConfig().safety
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_t0_detected_at_round_trips_across_restart(tmp_path: Path) -> None:
    """#235: the absolute charge/T0 instant persists and reads back across a
    restart, so recovery can restore the advisory DTR clock. A run that never
    charged reads back ``None`` (the v3 column is nullable)."""
    store = await seeded_store(RoastStore(db_path=tmp_path / "t0.sqlite3"))
    await store.update_run_phase("run-1", RoastPhase.ROASTING_PRE_FIRST_CRACK)
    charged_at = "2026-06-15T10:00:00+00:00"
    await store.record_t0_detected_at("run-1", charged_at)
    await store.close()  # process dies here

    restarted = RoastStore(db_path=store.db_path)
    await restarted.initialize()
    try:
        persisted = await restarted.read_latest_run()
        assert persisted is not None
        assert persisted.t0_detected_at_utc == charged_at
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_t0_detected_at_defaults_to_none_before_charge(tmp_store: RoastStore) -> None:
    """#235: a run that has not yet charged reads back ``t0_detected_at_utc`` as
    ``None`` — the recovery read treats that as "charge clock unknown" (the
    conservative pre-#235 behaviour)."""
    await seeded_store(tmp_store)
    try:
        persisted = await tmp_store.read_latest_run()
        assert persisted is not None
        assert persisted.t0_detected_at_utc is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_record_t0_detected_at_unknown_run_raises(tmp_store: RoastStore) -> None:
    """#235: stamping the charge instant on a missing run is a programming error
    and raises (a silent no-op would lose the recovery breadcrumb)."""
    await tmp_store.initialize()
    try:
        with pytest.raises(RuntimeError):
            await tmp_store.record_t0_detected_at("no-such-run", "2026-06-15T10:00:00+00:00")
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_record_t0_detected_at_is_write_once(tmp_store: RoastStore) -> None:
    """#260: the SQL-layer write-once guard makes the first persisted charge
    instant win. The normal first write succeeds; a second call with a different
    timestamp is a silent no-op (``WHERE t0_detected_at_utc IS NULL``) and does
    not clobber the recovered value, regardless of caller discipline."""
    store = await seeded_store(tmp_store)
    try:
        first = "2026-06-15T10:00:00+00:00"
        second = "2026-06-15T10:05:00+00:00"

        # First write succeeds (column was NULL).
        await store.record_t0_detected_at("run-1", first)
        persisted = await store.read_latest_run()
        assert persisted is not None
        assert persisted.t0_detected_at_utc == first

        # Second write with a different timestamp does not overwrite.
        await store.record_t0_detected_at("run-1", second)
        persisted = await store.read_latest_run()
        assert persisted is not None
        assert persisted.t0_detected_at_utc == first
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recovery_read_feeds_the_controller(tmp_path: Path) -> None:
    """End to end across the E4/E6 seam: the persisted phase drives
    recover_from_restart into operator_recovery_required with zero writes."""
    from roastpilot_agent.config import ControllerConfig, SafetyLimits
    from roastpilot_agent.controller import RoastController
    from roastpilot_agent.safety import SafetyPolicy
    from tests.conftest import (
        EventSink,
        RecordingExecutor,
        RecordingSnapshotSink,
        ScriptedStateReader,
    )

    store = await seeded_store(RoastStore(db_path=tmp_path / "seam.sqlite3"))
    await store.update_run_phase("run-1", RoastPhase.DEVELOPMENT)
    await store.close()

    restarted = RoastStore(db_path=store.db_path)
    await restarted.initialize()
    try:
        persisted = await restarted.read_latest_run()
    finally:
        await restarted.close()
    assert persisted is not None

    executor = RecordingExecutor()
    controller = RoastController(
        config=ControllerConfig(),
        safety=SafetyPolicy(SafetyLimits()),
        state_reader=ScriptedStateReader(),
        command_executor=executor,
        snapshot_sink=RecordingSnapshotSink(),
        event_emitter=EventSink(),
    )
    await controller.recover_from_restart(persisted.agent_phase)
    assert controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert executor.targets == [] and executor.commands == []


@pytest.mark.asyncio
async def test_read_latest_run_returns_the_newest(tmp_path: Path) -> None:
    store = await seeded_store(RoastStore(db_path=tmp_path / "latest.sqlite3"))
    await store.complete_run(run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.create_run(
        run_id="run-2",
        profile=PROFILE,
        config=AppConfig(),
        agent_phase=RoastPhase.STARTING,
        started_at_utc="2099-01-01T00:00:00+00:00",
    )
    try:
        persisted = await store.read_latest_run()
        assert persisted is not None and persisted.run_id == "run-2"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_complete_run_finalizes_fields(tmp_store: RoastStore) -> None:
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1",
            outcome="completed",
            agent_phase=RoastPhase.COMPLETE,
            log_dir="/logs/run-1",
            export_manifest={"ready": True},
        )
        row = await fetch_one(
            tmp_store,
            "SELECT outcome, agent_phase, completed_at_utc, log_dir,"
            " export_manifest_json FROM roast_runs",
        )
        assert row[0] == "completed" and row[1] == "complete"
        assert row[2] is not None
        assert row[3] == "/logs/run-1"
        assert '"ready": true' in str(row[4])
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_stale_faulted_run_terminal_and_preserves_reason(
    tmp_store: RoastStore,
) -> None:
    """#331: ``finalize_stale_faulted_run`` flips an unfinalised faulted run terminal
    (outcome ``faulted``, ``completed_at`` stamped) so it leaves the active set, and
    PRESERVES the existing ``fault_reason`` (does not overwrite it with NULL like a
    bare ``complete_run(fault_reason=None)`` would) for diagnosis. ``agent_phase``
    stays ``faulted``."""
    await seeded_store(tmp_store)
    try:
        # An unfinalised faulted run with a fault reason already on the row.
        await tmp_store.connection.execute(
            "UPDATE roast_runs SET agent_phase = 'faulted', fault_reason = ? WHERE id = 'run-1'",
            ("env 242 C exceeds the hard ceiling 240 C",),
        )
        await tmp_store.connection.commit()

        await tmp_store.finalize_stale_faulted_run("run-1")

        row = await fetch_one(
            tmp_store,
            "SELECT outcome, agent_phase, completed_at_utc, fault_reason FROM roast_runs"
            " WHERE id = 'run-1'",
        )
        assert row[0] == "faulted"  # terminal outcome
        assert row[1] == "faulted"  # phase unchanged
        assert row[2] is not None  # completed_at stamped → no longer active
        assert row[3] == "env 242 C exceeds the hard ceiling 240 C"  # reason PRESERVED
        # No longer the active run.
        assert await tmp_store.active_run() is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_stale_faulted_run_raises_on_already_finalized(
    tmp_store: RoastStore,
) -> None:
    """#331: it targets only an unfinalised run (``completed_at IS NULL``); an
    already-terminal run matches no row and raises rather than silently re-stamping
    (and the immutability trigger would block touching a completed run anyway)."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        with pytest.raises(RuntimeError, match="no unfinalized FAULTED roast_run"):
            await tmp_store.finalize_stale_faulted_run("run-1")
        with pytest.raises(RuntimeError, match="no unfinalized FAULTED roast_run"):
            await tmp_store.finalize_stale_faulted_run("does-not-exist")
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_stale_faulted_run_never_terminalizes_a_non_faulted_run(
    tmp_store: RoastStore,
) -> None:
    """#331 defensive guard (Augment): the method is GUARDED to ``agent_phase =
    'faulted'`` so it can never terminalise an ACTIVE non-faulted run — accidental
    misuse on a live roast raises and leaves the run untouched (still active),
    rather than silently corrupting it. (run-1 is seeded in ``starting``.)"""
    await seeded_store(tmp_store)
    try:
        # An ACTIVE, unfinalised, NON-faulted run (e.g. mid-roast).
        await tmp_store.connection.execute(
            "UPDATE roast_runs SET agent_phase = ? WHERE id = 'run-1'",
            (RoastPhase.ROASTING_PRE_FIRST_CRACK.value,),
        )
        await tmp_store.connection.commit()

        with pytest.raises(RuntimeError, match="no unfinalized FAULTED roast_run"):
            await tmp_store.finalize_stale_faulted_run("run-1")

        # Untouched: still active (completed_at NULL), phase + outcome unchanged.
        row = await fetch_one(
            tmp_store,
            "SELECT outcome, agent_phase, completed_at_utc FROM roast_runs WHERE id = 'run-1'",
        )
        assert row[0] is None  # no outcome stamped
        assert row[1] == "roasting_pre_first_crack"  # phase unchanged
        assert row[2] is None  # still active — NOT terminalized
        assert await tmp_store.active_run() is not None  # the live run survives
    finally:
        await tmp_store.close()


async def _insert_telemetry_at(store: RoastStore, run_id: str, recorded_at_utc: str) -> None:
    """Insert one raw ``telemetry_snapshots`` row at an EXPLICIT timestamp
    (#525 guard (c) tests) — bypasses :meth:`RoastStore.record_telemetry`'s
    cadence throttle and ``_utc_now()`` stamping so the tests can place a row
    precisely inside/outside the recency window under test, independent of
    wall-clock timing during the test run."""
    await store.connection.execute(
        "INSERT INTO telemetry_snapshots (run_id, tick, recorded_at_utc, agent_phase)"
        " VALUES (?, 1, ?, 'roasting_pre_first_crack')",
        (run_id, recorded_at_utc),
    )
    await store.connection.commit()


async def _backdate_started_at(store: RoastStore, run_id: str, started_at_utc: str) -> None:
    """Rewrite a run's ``started_at_utc`` to an EXPLICIT timestamp (#525 P1
    fold, clause 2b tests) — ``seeded_store`` stamps ``started_at_utc`` at
    "now" (the test's wall-clock instant), which is exactly what a genuinely
    stale orphan is NOT (a real orphan started minutes ago). Tests whose
    intent is "a genuine orphan" backdate via this helper so clause 2b
    (``started_at_utc <= threshold``) does not spuriously refuse them —
    mirrors ``_insert_telemetry_at``'s explicit-timestamp pattern."""
    await store.connection.execute(
        "UPDATE roast_runs SET started_at_utc = ? WHERE id = ?", (started_at_utc, run_id)
    )
    await store.connection.commit()


@pytest.mark.asyncio
async def test_finalize_orphaned_run_terminal_and_preserves_phase(tmp_store: RoastStore) -> None:
    """#525: a genuinely stranded, unfinalised run (no recent telemetry, and
    started well before the recency window — clause 2b, the P1 fold) is
    finalised ``outcome='aborted'``, ``completed_at_utc`` stamped, and
    ``agent_phase``/``fault_reason`` are left UNTOUCHED (mirrors
    ``finalize_stale_faulted_run``'s preserve-diagnosis rationale) — this
    finalises an abandoned run, it does not reclassify what happened."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.connection.execute(
            "UPDATE roast_runs SET agent_phase = ? WHERE id = 'run-1'",
            (RoastPhase.OPERATOR_RECOVERY_REQUIRED.value,),
        )
        await tmp_store.connection.commit()
        long_ago = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        await _backdate_started_at(tmp_store, "run-1", long_ago)

        await tmp_store.finalize_orphaned_run("run-1", recency_window_seconds=20.0)

        row = await fetch_one(
            tmp_store,
            "SELECT outcome, agent_phase, completed_at_utc FROM roast_runs WHERE id = 'run-1'",
        )
        assert row[0] == "aborted"
        assert row[1] == "operator_recovery_required"  # phase unchanged
        assert row[2] is not None  # completed_at stamped — no longer active
        assert await tmp_store.active_run() is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_orphaned_run_raises_on_already_finalized(tmp_store: RoastStore) -> None:
    """#525 guard (b): an already-terminal run matches no row and raises — a
    concurrent finalize is a clean failure, never a silent re-stamp."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        with pytest.raises(RuntimeError, match="no unfinalized roast_run"):
            await tmp_store.finalize_orphaned_run("run-1", recency_window_seconds=20.0)
        with pytest.raises(RuntimeError, match="no unfinalized roast_run"):
            await tmp_store.finalize_orphaned_run("does-not-exist", recency_window_seconds=20.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_orphaned_run_blocked_by_recent_telemetry(tmp_store: RoastStore) -> None:
    """#525 guard (c): a telemetry row inside the recency window is durable,
    shared-DB proof that SOME process is actively ticking this run — the
    clear is refused with a distinct ``RunActivelyDrivenError``, and the run
    is left completely untouched (not silently no-op'd as "already
    finalized"). This is the cross-process kill-chain the safety-reviewer's
    PASS-WITH-CONDITIONS verdict targets: guard (a) alone (this process's own
    ``active_run_id``) cannot see a DIFFERENT process's live run, but a
    telemetry row inside the window is shared state neither process can
    disagree about."""
    await seeded_store(tmp_store)
    try:
        recent = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        await _insert_telemetry_at(tmp_store, "run-1", recent)

        with pytest.raises(RunActivelyDrivenError, match="actively driving it"):
            await tmp_store.finalize_orphaned_run("run-1", recency_window_seconds=20.0)

        row = await fetch_one(
            tmp_store,
            "SELECT outcome, completed_at_utc FROM roast_runs WHERE id = 'run-1'",
        )
        assert row[0] is None  # untouched — no outcome stamped
        assert row[1] is None  # still active
        assert await tmp_store.active_run() is not None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_orphaned_run_blocked_by_a_just_started_run_with_no_telemetry_yet(
    tmp_store: RoastStore,
) -> None:
    """#525 guard (c) clause 2b — the P1 fold (PR #548 round-1 Codex): a run
    started MOMENTS ago has ZERO telemetry rows (``RoastRunner.start()``
    drives ``controller.start_run()`` — which issues the profile's initial
    heat/fan through the safety policy — and RETURNS before ``run()``'s
    scheduler ever calls ``tick_once()``, the sole caller of
    ``_publish_and_persist_telemetry``). Clause 2a's ``NOT EXISTS`` telemetry
    check ALONE would pass here (there is no telemetry row at all, recent or
    otherwise) and let an impostor process finalise a row whose hardware is
    being actively driven right now — this is the exact P1 reproduction: run
    it against the PRE-FOLD WHERE clause (no ``started_at_utc`` bound) and it
    fails to raise. Clause 2b closes it: ``started_at_utc`` (stamped at
    creation, ``seeded_store``'s default) is "now", inside the SAME recency
    window as clause 2a, so the clear is refused with the same
    ``RunActivelyDrivenError`` regardless of the empty telemetry table."""
    await seeded_store(tmp_store)  # started_at_utc = "now" (the default, unbackdated)
    try:
        # Sanity: genuinely NO telemetry for this run — clause 2a's NOT
        # EXISTS would pass on its own, proving clause 2b is the one doing
        # the work here, not a coincidental telemetry hit.
        telemetry_count = await fetch_one(
            tmp_store, "SELECT COUNT(*) FROM telemetry_snapshots WHERE run_id = 'run-1'"
        )
        assert telemetry_count[0] == 0

        with pytest.raises(RunActivelyDrivenError):
            await tmp_store.finalize_orphaned_run("run-1", recency_window_seconds=20.0)

        row = await fetch_one(
            tmp_store, "SELECT outcome, completed_at_utc FROM roast_runs WHERE id = 'run-1'"
        )
        assert row[0] is None  # untouched — no outcome stamped
        assert row[1] is None  # still active
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_orphaned_run_succeeds_when_telemetry_is_outside_the_window(
    tmp_store: RoastStore,
) -> None:
    """#525 guard (c): telemetry OLDER than the recency window does not block
    the clear — a run that stopped being driven a while ago is genuinely
    stale, not actively driven. Also backdates ``started_at_utc`` (clause 2b)
    since this test's intent is a genuine orphan, not a just-started run."""
    await seeded_store(tmp_store)
    try:
        stale = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        await _insert_telemetry_at(tmp_store, "run-1", stale)
        await _backdate_started_at(tmp_store, "run-1", stale)

        await tmp_store.finalize_orphaned_run("run-1", recency_window_seconds=20.0)

        row = await fetch_one(tmp_store, "SELECT outcome FROM roast_runs WHERE id = 'run-1'")
        assert row[0] == "aborted"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_orphaned_run_exact_boundary_is_treated_as_stale(
    tmp_store: RoastStore,
) -> None:
    """#525 guard (c) exact-boundary semantics (safety-reviewer note): the SQL
    uses a STRICT ``>`` against the threshold (``recorded_at_utc > threshold``),
    so a telemetry row stamped AT-OR-BEFORE the cutoff instant does not count
    as "recent" — the clear succeeds. Intent: a row must be STRICTLY newer
    than the cutoff to prove a live writer; a row landing exactly at the
    cutoff is the boundary of "old enough to be stale", not "inside the
    window". Pins a fixed instant on both sides (rather than a real
    ``datetime.now(UTC)`` race) so the boundary is exercised deterministically.
    Also backdates ``started_at_utc`` well clear of the window (clause 2b is
    not what this test is exercising)."""
    await seeded_store(tmp_store)
    try:
        cutoff_instant = datetime.now(UTC)
        window_seconds = 20.0
        long_ago = (cutoff_instant - timedelta(minutes=10)).isoformat()
        await _backdate_started_at(tmp_store, "run-1", long_ago)

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:  # noqa: ARG003 - matches datetime.now's signature
                return cutoff_instant

        # A telemetry row stamped EXACTLY at the threshold the store will
        # compute (now - window_seconds) — the strict boundary itself.
        with mock.patch("roastpilot_agent.store.datetime", _FixedDatetime):
            threshold_instant = cutoff_instant - timedelta(seconds=window_seconds)
            await _insert_telemetry_at(tmp_store, "run-1", threshold_instant.isoformat())

            # finalize_orphaned_run computes its own threshold as
            # datetime.now(UTC) - recency_window_seconds — patched to the SAME
            # cutoff_instant, so it lands on EXACTLY the row's timestamp.
            await tmp_store.finalize_orphaned_run("run-1", recency_window_seconds=window_seconds)

        row = await fetch_one(tmp_store, "SELECT outcome FROM roast_runs WHERE id = 'run-1'")
        assert row[0] == "aborted"  # exact-boundary row does NOT block the clear
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_orphaned_run_recency_window_is_config_scaled(
    tmp_store: RoastStore,
) -> None:
    """#525: the recency window is the CALLER's responsibility to scale
    against the configured ``telemetry_log_interval_seconds`` — this store
    method just honours whatever window it is given. A telemetry row 30s old
    blocks a 60s window but not a 10s window, proving the window is a live
    parameter, not a hardcoded constant. Backdates ``started_at_utc`` well
    clear of both windows so clause 2b never interferes with this test's
    telemetry-window assertion."""
    await seeded_store(tmp_store)
    try:
        long_ago = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        await _backdate_started_at(tmp_store, "run-1", long_ago)
        thirty_seconds_ago = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        await _insert_telemetry_at(tmp_store, "run-1", thirty_seconds_ago)

        with pytest.raises(RunActivelyDrivenError):
            await tmp_store.finalize_orphaned_run("run-1", recency_window_seconds=60.0)

        # Same row, a SHORTER window: 30s-old telemetry is now outside a 10s
        # window, so the clear succeeds.
        await tmp_store.finalize_orphaned_run("run-1", recency_window_seconds=10.0)
        row = await fetch_one(tmp_store, "SELECT outcome FROM roast_runs WHERE id = 'run-1'")
        assert row[0] == "aborted"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_orphaned_run_uses_the_owning_runs_frozen_interval_not_the_callers(
    tmp_store: RoastStore,
) -> None:
    """#525 PR #548 round-2 P1: the effective recency window derives from the
    TARGET RUN's OWN frozen ``telemetry_log_interval_seconds`` (persisted in
    ``config_json`` at creation), not just the calling process's window — the
    exact cross-process case this whole gate exists for. An impostor/default
    process computing its OWN narrow window (e.g. the default 5s interval ->
    a 20s floor window) must NOT be able to use that narrower window to clear
    a run whose OWNER logs at a much slower 60s cadence, where 30s-old
    telemetry is perfectly normal (well within the owner's own 4x60=240s
    margin) — NOT evidence of staleness. Taking the LARGER of the two windows
    (fail-closed) is what the fix does; this test proves it against the
    ACTUAL scenario, not a synthetic parameter."""
    from roastpilot_agent.config import ControllerConfig

    slow_owner_config = AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=60.0))
    await tmp_store.initialize()
    long_ago = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await tmp_store.create_run(
        run_id="run-slow-owner",
        profile=PROFILE,
        config=slow_owner_config,
        agent_phase=RoastPhase.DEVELOPMENT,
        started_at_utc=long_ago,
    )
    try:
        thirty_seconds_ago = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        await _insert_telemetry_at(tmp_store, "run-slow-owner", thirty_seconds_ago)

        # The ANSWERING process's own (narrower, default-interval) window —
        # exactly what RoastService._stale_session_recency_window_seconds()
        # computes for a default AppConfig() (5s interval -> the 20s floor).
        answerer_window_seconds = 20.0

        # Against the PRE-FIX behaviour (using the caller's window alone),
        # 30s-old telemetry is OUTSIDE a bare 20s window, so the clear would
        # have wrongly succeeded — reproduced explicitly below as the
        # fail-then-pass baseline. The FIXED method must refuse instead,
        # because the run's OWN frozen 60s interval implies a 240s window
        # (4 x 60, well past this 30s-old row).
        with pytest.raises(RunActivelyDrivenError):
            await tmp_store.finalize_orphaned_run(
                "run-slow-owner", recency_window_seconds=answerer_window_seconds
            )

        row = await fetch_one(
            tmp_store,
            "SELECT outcome, completed_at_utc FROM roast_runs WHERE id = 'run-slow-owner'",
        )
        assert row[0] is None  # untouched — the wider owner-derived window blocked it
        assert row[1] is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_orphaned_run_falls_back_to_callers_window_when_interval_is_absent(
    tmp_store: RoastStore,
) -> None:
    """#525 PR #548 round-2 P1: if the target run's ``config_json`` lacks the
    ``telemetry_log_interval_seconds`` key (a legacy/malformed row — not
    reachable in practice since the column is ``NOT NULL`` since schema v1,
    but the read degrades safely rather than raising), the method falls back
    to the CALLER's own ``recency_window_seconds`` alone — never crashes on a
    missing key."""
    await seeded_store(tmp_store)
    try:
        # Corrupt config_json to simulate an absent key (no `controller`
        # object at all) — the json_extract read must return NULL, not raise.
        await tmp_store.connection.execute(
            "UPDATE roast_runs SET config_json = '{}' WHERE id = 'run-1'"
        )
        await tmp_store.connection.commit()
        long_ago = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        await _backdate_started_at(tmp_store, "run-1", long_ago)
        stale = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        await _insert_telemetry_at(tmp_store, "run-1", stale)

        # 60s-old telemetry is outside the caller's own 20s window, and there
        # is no owner interval to widen it — the clear succeeds on the
        # caller's window alone.
        await tmp_store.finalize_orphaned_run("run-1", recency_window_seconds=20.0)

        row = await fetch_one(tmp_store, "SELECT outcome FROM roast_runs WHERE id = 'run-1'")
        assert row[0] == "aborted"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_finalize_orphaned_run_ignores_other_runs_telemetry(tmp_store: RoastStore) -> None:
    """#525 guard (c) scoping: the recency check is per-``run_id`` — a fresh
    telemetry row (and a fresh START) for a DIFFERENT run must never block
    clearing THIS one (both the ``NOT EXISTS`` subquery and clause 2b's
    ``started_at_utc`` bound are scoped to the target row, not the table)."""
    store = await seeded_store(tmp_store)
    try:
        long_ago = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        await _backdate_started_at(store, "run-1", long_ago)
        await store.create_run(
            run_id="run-2",
            profile=PROFILE,
            config=AppConfig(),
            agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        )
        recent = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        await _insert_telemetry_at(store, "run-2", recent)  # a DIFFERENT run's fresh telemetry

        await store.finalize_orphaned_run("run-1", recency_window_seconds=20.0)

        row = await fetch_one(tmp_store, "SELECT outcome FROM roast_runs WHERE id = 'run-1'")
        assert row[0] == "aborted"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_completed_runs_are_immutable(tmp_store: RoastStore) -> None:
    """Plan §8: completed-run immutability — enforced by the v2 triggers,
    not by application discipline."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        with pytest.raises(aiosqlite_module.IntegrityError, match="immutable"):
            await tmp_store.update_run_phase("run-1", RoastPhase.IDLE)
        with pytest.raises(aiosqlite_module.IntegrityError, match="immutable"):
            await tmp_store.connection.execute(
                "UPDATE roast_runs SET outcome = 'aborted' WHERE id = 'run-1'"
            )
        with pytest.raises(aiosqlite_module.IntegrityError, match="immutable"):
            await tmp_store.connection.execute("DELETE FROM roast_runs WHERE id = 'run-1'")
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_operator_and_cloud_fields_stay_mutable(tmp_store: RoastStore) -> None:
    """The documented immutability exceptions: rating/notes and the cloud
    sync fields still update after completion."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("run-1", rating=4, notes="bright, clean")
        await tmp_store.connection.execute(
            "UPDATE roast_runs SET cloud_sync_status = 'pending_sync',"
            " updated_at_utc = 'later' WHERE id = 'run-1'"
        )
        await tmp_store.connection.commit()
        row = await fetch_one(
            tmp_store,
            "SELECT operator_rating, operator_notes, cloud_sync_status FROM roast_runs",
        )
        assert row == (4, "bright, clean", "pending_sync")
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_active_runs_remain_fully_mutable(tmp_store: RoastStore) -> None:
    """The triggers only bite after completion — the per-tick phase
    breadcrumb keeps working for active runs."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.update_run_phase("run-1", RoastPhase.PREHEATING)
        await tmp_store.update_run_phase("run-1", RoastPhase.ROASTING_PRE_FIRST_CRACK)
        row = await fetch_one(tmp_store, "SELECT agent_phase FROM roast_runs")
        assert row[0] == "roasting_pre_first_crack"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_complete_run_on_unknown_run_raises(tmp_store: RoastStore) -> None:
    await seeded_store(tmp_store)
    try:
        with pytest.raises(RuntimeError, match="no roast_run"):
            await tmp_store.complete_run(
                run_id="ghost-run", outcome="completed", agent_phase=RoastPhase.COMPLETE
            )
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_operator_rating_on_unknown_run_raises(tmp_store: RoastStore) -> None:
    await seeded_store(tmp_store)
    try:
        with pytest.raises(RuntimeError, match="no completed roast_run"):
            await tmp_store.set_operator_rating("ghost-run", rating=5)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_rating_an_active_run_raises(tmp_store: RoastStore) -> None:
    """Review observation (E6-S3 PR): the store enforces completed-only
    rating, so an in-progress run can never be silently stamped."""
    await seeded_store(tmp_store)
    try:
        with pytest.raises(RuntimeError, match="no completed roast_run"):
            await tmp_store.set_operator_rating("run-1", rating=5)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_roasted_weight_persists_and_derives_weight_loss(
    tmp_store: RoastStore,
) -> None:
    """#388: the operator roasted-out weight stamps on a completed run and the read
    paths derive weight-loss % against the frozen 250 g charge weight."""
    # PROFILE.bean_weight_grams == 250.0
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_roasted_weight("run-1", roasted_weight_grams=221.0)

        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.roasted_weight_grams == 221.0
        assert detail.weight_loss_percent == 11.6  # (250 - 221) / 250 * 100

        summaries = await tmp_store.list_runs()
        summary = next(s for s in summaries if s.id == "run-1")
        assert summary.roasted_weight_grams == 221.0
        assert summary.weight_loss_percent == 11.6
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_roasted_weight_is_an_immutability_exception(tmp_store: RoastStore) -> None:
    """#388: the roasted weight is operator-editable AFTER completion (same lifecycle
    as the rating) — the v2 immutability trigger does not guard the new column."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        # Set, then correct it on the already-completed run — both must succeed.
        await tmp_store.set_roasted_weight("run-1", roasted_weight_grams=221.0)
        await tmp_store.set_roasted_weight("run-1", roasted_weight_grams=219.0)
        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.roasted_weight_grams == 219.0
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_unweighed_completed_run_has_null_weight_loss(tmp_store: RoastStore) -> None:
    """#388: a completed-but-unweighed run reads back null roasted weight + null
    weight-loss %."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.roasted_weight_grams is None
        assert detail.weight_loss_percent is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_roasted_weight_on_active_run_raises(tmp_store: RoastStore) -> None:
    """#388: completed-only, like the rating — an in-progress run cannot be stamped."""
    await seeded_store(tmp_store)
    try:
        with pytest.raises(RuntimeError, match="no completed roast_run"):
            await tmp_store.set_roasted_weight("run-1", roasted_weight_grams=221.0)
    finally:
        await tmp_store.close()


# --- charge-weight correction (#520) ---


@pytest.mark.asyncio
async def test_set_corrected_charge_persists_and_overrides_weight_loss(
    tmp_store: RoastStore,
) -> None:
    """#520: the operator-corrected charge weight stamps on a completed run and
    the read paths derive weight-loss % against the CORRECTED charge, not the
    frozen profile's 250 g default — roast 13's exact worked example (charged
    255 g against a 250 g form default, roasted 223 g: truth is 12.5%, not the
    10.8% the frozen 250 g would compute)."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_roasted_weight("run-1", roasted_weight_grams=223.0)
        await tmp_store.set_corrected_charge("run-1", corrected_charge_grams=255.0)

        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.corrected_charge_grams == 255.0
        # The frozen profile is UNTOUCHED — still the 250 g the controller ran with.
        assert detail.profile.bean_weight_grams == 250.0
        assert detail.weight_loss_percent == 12.55  # (255 - 223) / 255 * 100

        summaries = await tmp_store.list_runs()
        summary = next(s for s in summaries if s.id == "run-1")
        assert summary.corrected_charge_grams == 255.0
        assert summary.weight_loss_percent == 12.55
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_corrected_charge_is_an_immutability_exception(tmp_store: RoastStore) -> None:
    """#520: the corrected charge is operator-editable AFTER completion (same
    lifecycle as roasted weight / rating) — the v2 immutability trigger does
    not guard the new column."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_corrected_charge("run-1", corrected_charge_grams=255.0)
        await tmp_store.set_corrected_charge("run-1", corrected_charge_grams=252.0)
        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.corrected_charge_grams == 252.0
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_uncorrected_run_derives_weight_loss_from_the_frozen_charge(
    tmp_store: RoastStore,
) -> None:
    """#520: a run with no correction reads back null corrected_charge_grams and
    the derived weight-loss % is unaffected — falls back to the frozen profile's
    charge weight exactly as before #520."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_roasted_weight("run-1", roasted_weight_grams=221.0)
        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.corrected_charge_grams is None
        assert detail.weight_loss_percent == 11.6  # (250 - 221) / 250 * 100, unaffected

        summaries = await tmp_store.list_runs()
        summary = next(s for s in summaries if s.id == "run-1")
        assert summary.corrected_charge_grams is None
        assert summary.weight_loss_percent == 11.6
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_corrected_charge_on_active_run_raises(tmp_store: RoastStore) -> None:
    """#520: completed-only, like the rating/roasted-weight — an in-progress
    run cannot be stamped."""
    await seeded_store(tmp_store)
    try:
        with pytest.raises(RuntimeError, match="no completed roast_run"):
            await tmp_store.set_corrected_charge("run-1", corrected_charge_grams=255.0)
    finally:
        await tmp_store.close()


# --- soft-exclude / discard a roast (#582) ---


@pytest.mark.asyncio
async def test_excluded_flag_is_an_immutability_exception(tmp_store: RoastStore) -> None:
    """#582 load-bearing empirical proof: the v2 immutability trigger's ``WHEN``
    clause is an EXPLICIT column list (agent_phase, profile_json, config_json,
    …) that does NOT enumerate ``excluded`` — so, exactly like
    ``operator_rating`` / ``roasted_weight_grams`` / ``corrected_charge_grams``,
    the new column is mutable on a completed run without editing the shipped
    trigger. Verified directly against a real completed run: set, then
    toggled back (reversible by design) — both UPDATEs must succeed."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_run_excluded("run-1", excluded=True)
        row = await fetch_one(tmp_store, "SELECT excluded FROM roast_runs WHERE id = 'run-1'")
        assert row == (1,)

        # Reversible: toggling back to False on the SAME completed run also succeeds.
        await tmp_store.set_run_excluded("run-1", excluded=False)
        row = await fetch_one(tmp_store, "SELECT excluded FROM roast_runs WHERE id = 'run-1'")
        assert row == (0,)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_immutability_trigger_still_aborts_real_fields_after_excluded_ships(
    tmp_store: RoastStore,
) -> None:
    """#582 safety crux, other half of the empirical proof: adding the
    ``excluded`` column must NOT weaken immutability for any REAL field —
    the v2 trigger's ``WHEN`` clause is unedited, so a real-field UPDATE on a
    completed run still ABORTs, exactly as :func:`test_completed_runs_are_immutable`
    already proves pre-#582. Re-asserted here, in the SAME session as the
    ``excluded`` mutability proof above, so the two facts are verified
    together rather than relying on a historical test staying green."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        # The exception column mutates fine...
        await tmp_store.set_run_excluded("run-1", excluded=True)
        # ...but a real frozen field still aborts, trigger unweakened.
        with pytest.raises(aiosqlite_module.IntegrityError, match="immutable"):
            await tmp_store.update_run_phase("run-1", RoastPhase.IDLE)
        with pytest.raises(aiosqlite_module.IntegrityError, match="immutable"):
            await tmp_store.connection.execute(
                "UPDATE roast_runs SET outcome = 'aborted' WHERE id = 'run-1'"
            )
        with pytest.raises(aiosqlite_module.IntegrityError, match="immutable"):
            await tmp_store.connection.execute("DELETE FROM roast_runs WHERE id = 'run-1'")
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_run_excluded_on_unknown_run_raises(tmp_store: RoastStore) -> None:
    await seeded_store(tmp_store)
    try:
        with pytest.raises(RuntimeError, match="no completed roast_run"):
            await tmp_store.set_run_excluded("ghost-run", excluded=True)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_run_excluded_on_active_run_raises(tmp_store: RoastStore) -> None:
    """#582: completed-only, like the rating/roasted-weight/charge-correction —
    an in-progress run cannot be silently hidden mid-roast."""
    await seeded_store(tmp_store)
    try:
        with pytest.raises(RuntimeError, match="no completed roast_run"):
            await tmp_store.set_run_excluded("run-1", excluded=True)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_excluded_run_is_filtered_from_list_runs(tmp_store: RoastStore) -> None:
    """#582: a discarded run vanishes from the history list."""
    await seeded_store(tmp_store, run_id="run-keep")
    try:
        await tmp_store.create_run(
            run_id="run-discard",
            profile=PROFILE,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await tmp_store.complete_run(
            run_id="run-keep", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.complete_run(
            run_id="run-discard", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_run_excluded("run-discard", excluded=True)

        summaries = await tmp_store.list_runs()
        ids = {s.id for s in summaries}
        assert ids == {"run-keep"}
        assert all(not s.excluded for s in summaries)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_excluded_run_restored_reappears_in_list_runs(tmp_store: RoastStore) -> None:
    """#582: reversible — restoring a discarded run brings it back into history."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_run_excluded("run-1", excluded=True)
        assert await tmp_store.list_runs() == []

        await tmp_store.set_run_excluded("run-1", excluded=False)
        summaries = await tmp_store.list_runs()
        assert [s.id for s in summaries] == ["run-1"]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_read_run_still_returns_a_discarded_run_flagged(tmp_store: RoastStore) -> None:
    """#582: unlike list_runs, read_run keeps returning a discarded run — a
    direct link still works — carrying excluded=True so the detail page can
    render the discarded indicator + restore action."""
    await seeded_store(tmp_store)
    try:
        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.excluded is False

        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_run_excluded("run-1", excluded=True)

        discarded = await tmp_store.read_run("run-1")
        assert discarded is not None
        assert discarded.excluded is True
        assert discarded.id == "run-1"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_discarding_a_run_leaves_its_child_rows_untouched(tmp_store: RoastStore) -> None:
    """#582: a soft-exclude, never a delete — telemetry, events, and the export
    manifest for a discarded run must survive byte-for-byte (the whole point:
    the audio + FC-miss label are prime fine-tuning data, kept regardless)."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.record_telemetry(
            run_id="run-1",
            tick=1,
            agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            elapsed_seconds=10.0,
            interval_seconds=0.0,
            telemetry=RoastTelemetry(bean_temp_c=150.0, env_temp_c=165.0, bean_ror_c_per_min=6.0),
        )
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.RUN_STARTED,
            source=RoastEventSource.CONTROLLER,
        )
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        telemetry_before = await tmp_store.read_telemetry_points("run-1")
        timeline_before = await tmp_store.read_timeline("run-1")

        await tmp_store.set_run_excluded("run-1", excluded=True)

        telemetry_after = await tmp_store.read_telemetry_points("run-1")
        timeline_after = await tmp_store.read_timeline("run-1")
        assert telemetry_after == telemetry_before
        assert timeline_after == timeline_before
    finally:
        await tmp_store.close()


# --- atomic cross-value bound (#520 round-2 P3) ---
#
# The API layer's own pre-checks (read `detail`, write moments later) leave a
# race: a concurrent write between the read and the write could invalidate
# the value the pre-check validated against. These tests exercise the STORE
# layer directly — bypassing any API-layer pre-check entirely — to prove the
# UPDATE's own WHERE clause is what actually enforces the bound, atomically,
# against the row's CURRENT state.


@pytest.mark.asyncio
async def test_set_roasted_weight_atomically_rejects_against_a_prior_correction(
    tmp_store: RoastStore,
) -> None:
    """#520 round-2 P3: set_roasted_weight's own UPDATE — not an API-layer
    pre-check — rejects a write that exceeds the run's CURRENT effective
    (corrected) charge weight, raising PhysicallyImpossibleWeightError."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        # Correct the charge down to 200 g (frozen default is 250 g).
        await tmp_store.set_corrected_charge("run-1", corrected_charge_grams=200.0)

        # 210 g would pass against the frozen 250 g default, but is
        # physically impossible against the CURRENT effective charge of
        # 200 g — the atomic bound must reject it even though nothing here
        # ever re-reads `detail` to check it in Python first.
        with pytest.raises(PhysicallyImpossibleWeightError):
            await tmp_store.set_roasted_weight("run-1", roasted_weight_grams=210.0)

        # The rejected write must not have landed.
        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.roasted_weight_grams is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_roasted_weight_atomic_bound_accepts_within_the_effective_charge(
    tmp_store: RoastStore,
) -> None:
    """#520 round-2 P3: the same atomic bound accepts a write that IS within
    the current effective (corrected) charge — the WHERE clause is a bound,
    not an unconditional reject."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_corrected_charge("run-1", corrected_charge_grams=200.0)
        await tmp_store.set_roasted_weight("run-1", roasted_weight_grams=180.0)

        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.roasted_weight_grams == 180.0
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_roasted_weight_atomic_bound_uses_the_frozen_charge_when_uncorrected(
    tmp_store: RoastStore,
) -> None:
    """#520 round-2 P3: with no correction on record, the atomic bound falls
    back to the frozen profile.bean_weight_grams (250 g in the fixture) —
    the COALESCE in the WHERE clause, not just the corrected-charge column."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        with pytest.raises(PhysicallyImpossibleWeightError):
            await tmp_store.set_roasted_weight("run-1", roasted_weight_grams=260.0)

        await tmp_store.set_roasted_weight("run-1", roasted_weight_grams=240.0)
        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.roasted_weight_grams == 240.0
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_corrected_charge_atomically_rejects_against_a_prior_roasted_weight(
    tmp_store: RoastStore,
) -> None:
    """#520 round-2 P3: set_corrected_charge's own UPDATE rejects a
    correction that falls below the run's CURRENT roasted-out weight,
    raising PhysicallyImpossibleWeightError — the mirror-direction atomic
    bound to set_roasted_weight's own."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_roasted_weight("run-1", roasted_weight_grams=223.0)

        with pytest.raises(PhysicallyImpossibleWeightError):
            await tmp_store.set_corrected_charge("run-1", corrected_charge_grams=200.0)

        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.corrected_charge_grams is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_set_corrected_charge_atomic_bound_accepts_no_roasted_weight_yet(
    tmp_store: RoastStore,
) -> None:
    """#520 round-2 P3: with no roasted weight entered yet, the atomic bound
    has nothing to reject against — any positive correction is accepted (the
    ``roasted_weight_grams IS NULL`` branch of the WHERE clause)."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_corrected_charge("run-1", corrected_charge_grams=5.0)
        detail = await tmp_store.read_run("run-1")
        assert detail is not None
        assert detail.corrected_charge_grams == 5.0
    finally:
        await tmp_store.close()


# --- tastings (#522, D91) ---


@pytest.mark.asyncio
async def test_add_tasting_stars_and_notes_only_is_valid(tmp_store: RoastStore) -> None:
    """#522: entry friction stays near zero — stars + notes alone, with every
    other field omitted, must persist and read back cleanly."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        tasting = await tmp_store.add_tasting("run-1", stars=4, notes="good body")
        assert tasting.stars == 4
        assert tasting.notes == "good body"
        assert tasting.tasted_at_utc is None
        assert tasting.brew_method is None
        assert tasting.grind_note is None
        assert tasting.attributes == []
        assert tasting.defects == []
        assert tasting.recorded_at_utc  # stamped by the store

        tastings = await tmp_store.list_tastings("run-1")
        assert len(tastings) == 1
        assert tastings[0].id == tasting.id
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_add_tasting_persists_every_optional_field(tmp_store: RoastStore) -> None:
    """#522: brew context + controlled attribute vocabulary round-trip exactly."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        tasting = await tmp_store.add_tasting(
            "run-1",
            stars=3,
            notes="flat immediately after roasting",
            tasted_at_utc="2026-07-12T18:00:00+00:00",
            brew_method="pour_over",
            grind_note="medium-fine, 22g/380g",
            attributes=["sweetness"],
            defects=["flat"],
        )
        assert tasting.tasted_at_utc == "2026-07-12T18:00:00+00:00"
        assert tasting.brew_method == "pour_over"
        assert tasting.grind_note == "medium-fine, 22g/380g"
        assert tasting.attributes == ["sweetness"]
        assert tasting.defects == ["flat"]

        [reread] = await tmp_store.list_tastings("run-1")
        assert reread == tasting
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_add_tasting_revisit_appends_not_overwrites(tmp_store: RoastStore) -> None:
    """#522, D91: the roast-13 "flat -> grassy" refinement shape — a second
    tasting is an ADDITIONAL row, never an overwrite of the first."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        first = await tmp_store.add_tasting("run-1", stars=2, defects=["flat"])
        second = await tmp_store.add_tasting("run-1", stars=4, defects=["grassy"])

        tastings = await tmp_store.list_tastings("run-1")
        assert [t.id for t in tastings] == [first.id, second.id]  # oldest first
        assert tastings[0].defects == ["flat"]
        assert tastings[1].defects == ["grassy"]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_add_tasting_on_unknown_run_raises(tmp_store: RoastStore) -> None:
    await seeded_store(tmp_store)
    try:
        with pytest.raises(RuntimeError, match="no completed roast_run"):
            await tmp_store.add_tasting("ghost-run", stars=5)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_add_tasting_on_active_run_raises(tmp_store: RoastStore) -> None:
    """#522: completed-only, like the rating and roasted weight — an
    in-progress run cannot be tasted (there is nothing to taste yet)."""
    await seeded_store(tmp_store)
    try:
        with pytest.raises(RuntimeError, match="no completed roast_run"):
            await tmp_store.add_tasting("run-1", stars=5)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_tastings_empty_for_untasted_run(tmp_store: RoastStore) -> None:
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        assert await tmp_store.list_tastings("run-1") == []
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_v11_tastings_table_is_a_pure_addition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#522: like V4's bean_profiles, V11 touches no existing table or row —
    upgrading a pre-v11 store with data preserves it untouched, and the new
    table starts empty."""
    pre_v11 = MIGRATIONS[:10]  # V1..V10 (before roast_tastings)
    assert len(pre_v11) == 10
    db_path = tmp_path / "v11upgrade.sqlite3"
    monkeypatch.setattr(store_module, "MIGRATIONS", pre_v11)
    old = RoastStore(db_path=db_path)
    await old.initialize()
    try:
        assert await old.schema_version() == 10
        await seeded_store(old)
        await old.complete_run(run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE)
    finally:
        await old.close()

    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    upgraded = RoastStore(db_path=db_path)
    await upgraded.initialize()
    try:
        assert await upgraded.schema_version() == len(MIGRATIONS)
        # Pre-existing run untouched by the additive migration.
        detail = await upgraded.read_run("run-1")
        assert detail is not None
        assert detail.outcome == "completed"
        # The new table exists and starts empty for that pre-existing run.
        assert await upgraded.list_tastings("run-1") == []
        # And it accepts a fresh write post-upgrade.
        await upgraded.add_tasting("run-1", stars=5)
        assert len(await upgraded.list_tastings("run-1")) == 1
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_persisted_run_is_frozen(tmp_path: Path) -> None:
    import pydantic

    store = await seeded_store(RoastStore(db_path=tmp_path / "frozen.sqlite3"))
    try:
        persisted = await store.read_latest_run()
        assert persisted is not None
        with pytest.raises(pydantic.ValidationError):
            persisted.agent_phase = RoastPhase.IDLE  # pyright: ignore[reportAttributeAccessIssue]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_completion_timestamps_coincide(tmp_store: RoastStore) -> None:
    """completed_at_utc == updated_at_utc at the completion instant."""
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        row = await fetch_one(tmp_store, "SELECT completed_at_utc, updated_at_utc FROM roast_runs")
        assert row[0] == row[1]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_created_at_is_trigger_guarded_too(tmp_store: RoastStore) -> None:
    await seeded_store(tmp_store)
    try:
        await tmp_store.complete_run(
            run_id="run-1", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        with pytest.raises(aiosqlite_module.IntegrityError, match="immutable"):
            await tmp_store.connection.execute(
                "UPDATE roast_runs SET created_at_utc = 'rewritten' WHERE id = 'run-1'"
            )
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_projects_first_crack_time(tmp_store: RoastStore) -> None:
    """``list_runs`` projects the earliest ``first_crack`` event time (#111).

    Both FC paths (MCP detection and the operator override) emit a
    ``first_crack`` roast event, so the projection reads either crossing. The
    earliest such event wins when more than one is present.
    """
    await seeded_store(tmp_store)
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc="2026-06-07T14:09:00+00:00",
        )
        # A later, duplicate FC event must not shadow the earliest one.
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.OPERATOR,
            recorded_at_utc="2026-06-07T14:11:00+00:00",
        )
        runs = await tmp_store.list_runs()
        assert len(runs) == 1
        assert runs[0].first_crack_at_utc == "2026-06-07T14:09:00+00:00"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_first_crack_time_orders_by_event_time_not_id(
    tmp_store: RoastStore,
) -> None:
    """The FC projection orders by ``recorded_at_utc``, not insertion ``id`` (#111).

    ``record_event`` accepts an explicit ``recorded_at_utc``, so a later-inserted
    event (higher ``id``) can carry an *earlier* timestamp. Inserting two
    ``first_crack`` events out of order — the LOWER-id one with the LATER time,
    the HIGHER-id one with the EARLIER time — and asserting the EARLIER timestamp
    is returned proves the query is time-ordered, the failure mode an id-ordered
    subquery would silently get wrong.
    """
    earlier = "2026-06-07T14:09:00+00:00"
    later = "2026-06-07T14:11:00+00:00"
    await seeded_store(tmp_store)
    try:
        # Inserted FIRST (lower id) but the LATER event time.
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.OPERATOR,
            recorded_at_utc=later,
        )
        # Inserted SECOND (higher id) but the EARLIER event time.
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc=earlier,
        )
        runs = await tmp_store.list_runs()
        assert len(runs) == 1
        # Earliest by time wins, even though it has the higher insertion id.
        assert runs[0].first_crack_at_utc == earlier
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_source", "second_source", "uses_onset"),
    [
        (RoastEventSource.MCP, RoastEventSource.OPERATOR, True),
        (RoastEventSource.OPERATOR, RoastEventSource.MCP, False),
    ],
)
async def test_list_runs_first_crack_equal_time_uses_one_lower_id_event_for_time_and_source(
    tmp_store: RoastStore,
    first_source: RoastEventSource,
    second_source: RoastEventSource,
    uses_onset: bool,
) -> None:
    """Equal-time FC subqueries share the lower-id event's provenance."""
    confirmation = "2026-06-07T14:09:10+00:00"
    await seeded_store(tmp_store, started_at_utc="2026-06-07T14:00:00+00:00")
    try:
        for source in (first_source, second_source):
            await tmp_store.record_event(
                run_id="run-1",
                kind=RoastEventKind.FIRST_CRACK,
                source=source,
                recorded_at_utc=confirmation,
            )
        await tmp_store.connection.execute(
            "INSERT INTO telemetry_snapshots "
            "(run_id,tick,recorded_at_utc,elapsed_seconds,agent_phase,raw_state_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                "run-1",
                1,
                "2026-06-07T14:09:11+00:00",
                1.0,
                RoastPhase.DEVELOPMENT.value,
                '{"first_crack_status":{"detected_at_utc":"2026-06-07T14:09:05+00:00"}}',
            ),
        )
        await tmp_store.connection.commit()
        [summary] = await tmp_store.list_runs()
        assert summary.first_crack_at_utc == (
            "2026-06-07T14:09:05+00:00" if uses_onset else confirmation
        )
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_first_crack_time_none_without_fc(tmp_store: RoastStore) -> None:
    """A run that never reached first crack serializes ``first_crack_at_utc`` as
    ``None`` (#111 back-compat: pre-FC runs and any run with no FC event)."""
    await seeded_store(tmp_store)
    try:
        # A non-FC event exists, but no first_crack event for this run.
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.RUN_STARTED,
            source=RoastEventSource.CONTROLLER,
        )
        await tmp_store.connection.execute(
            "INSERT INTO telemetry_snapshots "
            "(run_id, tick, recorded_at_utc, elapsed_seconds, agent_phase, raw_state_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                1,
                "2026-06-07T14:09:11+00:00",
                1.0,
                RoastPhase.DEVELOPMENT.value,
                '{"first_crack_status":{"detected_at_utc":"2026-06-07T14:09:05+00:00"}}',
            ),
        )
        await tmp_store.connection.commit()
        runs = await tmp_store.list_runs()
        assert len(runs) == 1
        assert runs[0].first_crack_at_utc is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_uses_post_confirmation_mcp_backdated_onset(tmp_store: RoastStore) -> None:
    """A confirmed MCP FC projects a later-persisted, backdated status onset."""
    confirmation = "2026-06-07T14:09:10+00:00"
    await seeded_store(tmp_store, started_at_utc="2026-06-07T14:00:00+00:00")
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc=confirmation,
        )
        await tmp_store.connection.execute(
            "INSERT INTO telemetry_snapshots "
            "(run_id, tick, recorded_at_utc, elapsed_seconds, agent_phase, raw_state_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                1,
                "2026-06-07T14:09:11+00:00",
                1.0,
                RoastPhase.DEVELOPMENT.value,
                '{"first_crack_status":{"detected_at_utc":"2026-06-07T14:09:05Z"}}',
            ),
        )
        await tmp_store.connection.commit()

        [summary] = await tmp_store.list_runs()

        assert summary.first_crack_at_utc == "2026-06-07T14:09:05+00:00"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("onset", ["2026-06-07T13:59:59+00:00", "2026-06-07T14:09:11+00:00"])
async def test_list_runs_rejects_mcp_onset_outside_run_to_confirmation_window(
    tmp_store: RoastStore, onset: str
) -> None:
    """A post-confirmation snapshot cannot move FC outside trusted bounds."""
    confirmation = "2026-06-07T14:09:10+00:00"
    await seeded_store(tmp_store, started_at_utc="2026-06-07T14:00:00+00:00")
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc=confirmation,
        )
        await tmp_store.connection.execute(
            "INSERT INTO telemetry_snapshots "
            "(run_id,tick,recorded_at_utc,elapsed_seconds,agent_phase,raw_state_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                "run-1",
                1,
                "2026-06-07T14:09:11+00:00",
                1.0,
                RoastPhase.DEVELOPMENT.value,
                f'{{"first_crack_status":{{"detected_at_utc":"{onset}"}}}}',
            ),
        )
        await tmp_store.connection.commit()
        [summary] = await tmp_store.list_runs()
        assert summary.first_crack_at_utc == confirmation
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        (["2026-06-07T13:59:59+00:00", "2026-06-07T14:09:05+00:00"], "2026-06-07T14:09:05+00:00"),
        (["2026-06-07T14:09:11+00:00", "2026-06-07T14:09:06+00:00"], "2026-06-07T14:09:06+00:00"),
        (["2026-06-07T14:09:06+00:00", "2026-06-07T14:09:05+00:00"], "2026-06-07T14:09:05+00:00"),
    ],
)
async def test_list_runs_selects_earliest_surviving_mcp_onset(
    tmp_store: RoastStore, candidates: list[str], expected: str
) -> None:
    """Out-of-window candidates do not suppress an in-window sibling."""
    confirmation = "2026-06-07T14:09:10+00:00"
    await seeded_store(tmp_store, started_at_utc="2026-06-07T14:00:00+00:00")
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc=confirmation,
        )
        await tmp_store.connection.executemany(
            "INSERT INTO telemetry_snapshots "
            "(run_id,tick,recorded_at_utc,elapsed_seconds,agent_phase,raw_state_json) "
            "VALUES (?,?,?,?,?,?)",
            [
                (
                    "run-1",
                    index,
                    "2026-06-07T14:09:11+00:00",
                    float(index),
                    RoastPhase.DEVELOPMENT.value,
                    f'{{"first_crack_status":{{"detected_at_utc":"{candidate}"}}}}',
                )
                for index, candidate in enumerate(candidates, start=1)
            ],
        )
        await tmp_store.connection.commit()
        [summary] = await tmp_store.list_runs()
        assert summary.first_crack_at_utc == expected
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_mcp_window_and_source_fail_closed(tmp_store: RoastStore) -> None:
    """Operator FC and pre-confirmation state retain the accepted event time."""
    event_time = "2026-06-07T14:09:10+00:00"
    await seeded_store(tmp_store)
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.OPERATOR,
            recorded_at_utc=event_time,
        )
        await tmp_store.connection.execute(
            "INSERT INTO telemetry_snapshots "
            "(run_id, tick, recorded_at_utc, elapsed_seconds, agent_phase, raw_state_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                1,
                "2026-06-07T14:09:09+00:00",
                1.0,
                RoastPhase.DEVELOPMENT.value,
                '{"first_crack_status":{"detected_at_utc":"2026-06-07T14:09:05+00:00"}}',
            ),
        )
        await tmp_store.connection.commit()

        [summary] = await tmp_store.list_runs()

        assert summary.first_crack_at_utc == event_time
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_legacy_controller_first_crack_ignores_mcp_state_onset(
    tmp_store: RoastStore,
) -> None:
    """Persisted event provenance, not a state payload claim, gates onset use."""
    event_time = "2026-06-07T14:09:10+00:00"
    await seeded_store(tmp_store)
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.CONTROLLER,
            recorded_at_utc=event_time,
            payload={"source": "mcp"},
        )
        await tmp_store.connection.execute(
            "INSERT INTO telemetry_snapshots "
            "(run_id,tick,recorded_at_utc,elapsed_seconds,agent_phase,raw_state_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                "run-1",
                1,
                "2026-06-07T14:09:11+00:00",
                1.0,
                RoastPhase.DEVELOPMENT.value,
                '{"first_crack_status":{"detected_at_utc":"2026-06-07T14:09:05+00:00"}}',
            ),
        )
        await tmp_store.connection.commit()
        [summary] = await tmp_store.list_runs()
        assert summary.first_crack_at_utc == event_time
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_ignores_malformed_state_from_another_run(tmp_store: RoastStore) -> None:
    """One malformed raw state cannot abort or alter another run's onset."""
    await seeded_store(tmp_store, started_at_utc="2026-06-07T14:00:00+00:00")
    await tmp_store.create_run(
        run_id="run-2", profile=PROFILE, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc="2026-06-07T14:09:10+00:00",
        )
        await tmp_store.connection.executemany(
            "INSERT INTO telemetry_snapshots "
            "(run_id,tick,recorded_at_utc,elapsed_seconds,agent_phase,raw_state_json) "
            "VALUES (?,?,?,?,?,?)",
            [
                (
                    "run-1",
                    1,
                    "2026-06-07T14:09:11+00:00",
                    1.0,
                    RoastPhase.DEVELOPMENT.value,
                    '{"first_crack_status":{"detected_at_utc":"2026-06-07T14:09:05+00:00"}}',
                ),
                ("run-2", 1, "2026-06-07T14:09:11+00:00", 1.0, RoastPhase.DEVELOPMENT.value, "{"),
            ],
        )
        await tmp_store.connection.commit()
        summaries = {summary.id: summary for summary in await tmp_store.list_runs()}
        assert summaries["run-1"].first_crack_at_utc == "2026-06-07T14:09:05+00:00"
        assert summaries["run-2"].first_crack_at_utc is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_uses_only_post_confirmation_mcp_onsets(tmp_store: RoastStore) -> None:
    """A pre-confirmation bogus onset cannot beat a valid later snapshot."""
    await seeded_store(tmp_store, started_at_utc="2026-06-07T14:00:00+00:00")
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc="2026-06-07T14:09:10+00:00",
        )
        await tmp_store.connection.executemany(
            "INSERT INTO telemetry_snapshots "
            "(run_id,tick,recorded_at_utc,elapsed_seconds,agent_phase,raw_state_json) "
            "VALUES (?,?,?,?,?,?)",
            [
                (
                    "run-1",
                    1,
                    "2026-06-07T14:09:09+00:00",
                    1.0,
                    RoastPhase.DEVELOPMENT.value,
                    '{"first_crack_status":{"detected_at_utc":"2026-06-07T14:00:00+00:00"}}',
                ),
                (
                    "run-1",
                    2,
                    "2026-06-07T14:09:11+00:00",
                    2.0,
                    RoastPhase.DEVELOPMENT.value,
                    '{"first_crack_status":{"detected_at_utc":"2026-06-07T14:09:05+00:00"}}',
                ),
            ],
        )
        await tmp_store.connection.commit()
        [summary] = await tmp_store.list_runs()
        assert summary.first_crack_at_utc == "2026-06-07T14:09:05+00:00"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_onset_query_has_constant_statement_and_bind_shape(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History onset lookup stays two statements with one MCP bind at any run count."""
    await seeded_store(tmp_store)
    original_execute: Any = tmp_store.connection._execute  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType, reportUnknownVariableType]
    observed: list[tuple[str, object]] = []

    async def traced_execute(function: object, *args: object, **kwargs: object) -> object:
        """Record only this store's list-query calls before delegating."""
        if getattr(function, "__name__", None) == "execute":
            observed.append((str(args[0]), args[1] if len(args) > 1 else ()))
        return await original_execute(function, *args, **kwargs)

    monkeypatch.setattr(tmp_store.connection, "_execute", traced_execute)
    try:
        observed.clear()
        await tmp_store.list_runs()
        one_run = list(observed)
        for index in range(2, 11):
            await tmp_store.create_run(
                run_id=f"run-{index}",
                profile=PROFILE,
                config=AppConfig(),
                agent_phase=RoastPhase.STARTING,
            )
        observed.clear()
        await tmp_store.list_runs()
        ten_runs = list(observed)

        assert len(one_run) == len(ten_runs) == 2
        for statements in (one_run, ten_runs):
            onset_sql, onset_parameters = next(
                (sql, parameters) for sql, parameters in statements if "json_valid" in sql
            )
            assert " IN (" not in onset_sql
            assert onset_parameters == (RoastEventSource.MCP.value,)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_earliest_non_mcp_event_blocks_later_mcp_status(
    tmp_store: RoastStore,
) -> None:
    """The accepted earliest event's source, not a later duplicate, gates onset use."""
    earlier = "2026-06-07T14:09:10+00:00"
    await seeded_store(tmp_store)
    try:
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.OPERATOR,
            recorded_at_utc=earlier,
        )
        await tmp_store.record_event(
            run_id="run-1",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc="2026-06-07T14:09:11+00:00",
        )
        await tmp_store.connection.execute(
            "INSERT INTO telemetry_snapshots "
            "(run_id,tick,recorded_at_utc,elapsed_seconds,agent_phase,raw_state_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                "run-1",
                1,
                "2026-06-07T14:09:12+00:00",
                1.0,
                RoastPhase.DEVELOPMENT.value,
                '{"first_crack_status":{"detected_at_utc":"2026-06-07T14:09:05+00:00"}}',
            ),
        )
        await tmp_store.connection.commit()
        [summary] = await tmp_store.list_runs()
        assert summary.first_crack_at_utc == earlier
    finally:
        await tmp_store.close()


def test_package_landmark_helpers_use_fail_closed_normalized_values() -> None:
    """Package landmarks normalize onset values and reject unsafe mapping input."""
    onset = roast_landmarks.earliest_onset_utc(
        ["bad", "2026-06-07T14:09:05Z", "2026-06-07T14:09:06+00:00"]
    )
    assert onset is not None and onset.isoformat() == "2026-06-07T14:09:05+00:00"
    assert (
        roast_landmarks.utc_to_run_seconds(
            "2026-06-07T14:09:05+00:00",
            [
                ("2026-06-07T14:09:04+00:00", 4.0),
                ("2026-06-07T14:09:06+00:00", 6.0),
            ],
        )
        == 5.0
    )
    assert (
        roast_landmarks.utc_to_run_seconds(
            "2026-06-07T14:09:00+00:00", [("2026-06-07T14:09:05+00:00", 1.0)]
        )
        is None
    )
    assert roast_landmarks.interpolate_at(5.0, [(0.0, 0.0), (10.0, 10.0)]) == 5.0
    assert roast_landmarks.interpolate_at(11.0, [(0.0, 0.0), (10.0, 10.0)]) is None
    assert roast_landmarks.interpolate_at(5.0, [(10.0, 0.0), (0.0, 10.0)]) is None


@pytest.mark.asyncio
async def test_list_runs_development_percent_uses_insertion_order_after_tick_reset(
    tmp_path: Path,
) -> None:
    """The latest development percent survives an overlapping restart clock."""
    store = await seeded_store(RoastStore(tmp_path / "dev-pct-restart.sqlite3"))
    try:
        for tick, development_percent in enumerate((10.0, 11.0, 12.0)):
            wrote = await store.record_telemetry(
                run_id="run-1",
                tick=tick,
                agent_phase=RoastPhase.DEVELOPMENT,
                elapsed_seconds=float(tick),
                interval_seconds=0.0,
                telemetry=None,
                development_percent=development_percent,
            )
            assert wrote

        # A real restart creates a fresh store instance and resets both clocks.
        await store.close()
        store = RoastStore(store.db_path)
        await store.initialize()
        for tick, development_percent in enumerate((20.0, 21.0)):
            wrote = await store.record_telemetry(
                run_id="run-1",
                tick=tick,
                agent_phase=RoastPhase.DEVELOPMENT,
                elapsed_seconds=float(tick),
                interval_seconds=0.0,
                telemetry=None,
                development_percent=development_percent,
            )
            assert wrote

        runs = await store.list_runs()
        assert len(runs) == 1
        assert runs[0].development_percent == 21.0
    finally:
        await store.close()


def _advisor_context() -> AdvisorContext:
    """A minimal AdvisorContext for the advisor-stats projection tests (#184)."""
    return AdvisorContext(
        phase=RoastPhase.DEVELOPMENT,
        roast_elapsed_seconds=500.0,
        development_elapsed_seconds=30.0,
        current_bean_temp_c=197.0,
        current_env_temp_c=215.0,
        bean_ror_c_per_min=8.0,
        env_ror_c_per_min=6.0,
        target_drop_temp_c=205.0,
        profile_name="store-test",
    )


async def _record_consult(
    store: RoastStore,
    *,
    tick: int,
    status: Literal["ok", "timeout", "malformed", "provider_error"] = "ok",
    verdict: SafetyVerdict | None = None,
    run_id: str = "run-1",
) -> None:
    """Record one advisor consult and (optionally) the safety verdict it produced,
    joined by tick the same way the controller path persists them (#184)."""
    evaluation_id: int | None = None
    if verdict is not None:
        evaluation_id = await store.record_safety_evaluation(
            run_id=run_id,
            tick=tick,
            evaluation=SafetyEvaluation(
                rule="command_bounds",
                verdict=verdict,
                input_heat=120,
                input_fan=40,
                adjusted_heat=100,
                adjusted_fan=40,
                reason="test",
            ),
        )
    await store.record_advisor_decision(
        run_id=run_id,
        tick=tick,
        provider="openrouter",
        model="test-model",
        prompt_version="v0",
        context=_advisor_context(),
        latency_ms=100 if status == "ok" else None,
        decision=RoastDecision(
            target_heat=45, target_fan=60, should_drop=False, confidence=0.8, rationale="hold"
        )
        if status == "ok"
        else None,
        status=status,
        safety_evaluation_id=evaluation_id,
    )


@pytest.mark.asyncio
async def test_list_runs_aggregates_advisor_stats(tmp_store: RoastStore) -> None:
    """``list_runs`` projects per-run advisor consult/clamp/reject/fail counts (#184).

    This monotonic, one-evaluation-per-tick control pins the pre-change counts:
    ``consults`` is every persisted decision, ``failed`` is every non-``ok``
    status, and clamp/reject counts follow the decision's linked evaluation.
    """
    await seeded_store(tmp_store)
    try:
        await _record_consult(tmp_store, tick=4, status="ok", verdict=SafetyVerdict.ALLOW)
        await _record_consult(tmp_store, tick=8, status="ok", verdict=SafetyVerdict.CLAMP)
        await _record_consult(tmp_store, tick=12, status="ok", verdict=SafetyVerdict.REJECT)
        await _record_consult(tmp_store, tick=16, status="provider_error", verdict=None)
        runs = await tmp_store.list_runs()
        assert len(runs) == 1
        summary = runs[0]
        assert summary.advisor_consults == 4
        assert summary.advisor_clamped == 1
        assert summary.advisor_rejected == 1
        assert summary.advisor_failed == 1
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_advisor_stats_zero_without_consults(tmp_store: RoastStore) -> None:
    """A run with no advisor decisions projects zeros (#184 back-compat).

    The SPA renders a zero-consult run as "no advice"; a pre-advisor run must not
    error or surface phantom counts.
    """
    await seeded_store(tmp_store)
    try:
        runs = await tmp_store.list_runs()
        assert len(runs) == 1
        summary = runs[0]
        assert summary.advisor_consults == 0
        assert summary.advisor_clamped == 0
        assert summary.advisor_rejected == 0
        assert summary.advisor_failed == 0
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_advisor_counts_use_exact_fk_with_duplicate_ticks(
    tmp_store: RoastStore,
) -> None:
    """Clamp/reject counts follow each FK, never a later same-tick row."""
    await seeded_store(tmp_store)
    try:
        # The FK points at CLAMP; a later ALLOW at the same tick must not hide it.
        await _record_consult(tmp_store, tick=8, status="ok", verdict=SafetyVerdict.CLAMP)
        await tmp_store.record_safety_evaluation(
            run_id="run-1",
            tick=8,
            evaluation=SafetyEvaluation(
                rule="r",
                verdict=SafetyVerdict.ALLOW,
                input_heat=120,
                input_fan=40,
                adjusted_heat=100,
                adjusted_fan=40,
                reason="later same-tick allow",
            ),
        )

        # The FK points at ALLOW; a later REJECT must not fabricate a rejection.
        await _record_consult(tmp_store, tick=9, status="ok", verdict=SafetyVerdict.ALLOW)
        await tmp_store.record_safety_evaluation(
            run_id="run-1",
            tick=9,
            evaluation=SafetyEvaluation(
                rule="r",
                verdict=SafetyVerdict.REJECT,
                input_heat=120,
                input_fan=40,
                adjusted_heat=100,
                adjusted_fan=40,
                reason="later same-tick reject",
            ),
        )

        runs = await tmp_store.list_runs()
        assert len(runs) == 1
        summary = runs[0]
        assert summary.advisor_consults == 2
        assert summary.advisor_clamped == 1
        assert summary.advisor_rejected == 0
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_null_advisor_fk_does_not_guess_by_tick(
    tmp_store: RoastStore,
) -> None:
    """A NULL advisor FK counts as neither even with a same-tick reject."""
    await seeded_store(tmp_store)
    try:
        await _record_consult(tmp_store, tick=8, status="ok", verdict=SafetyVerdict.REJECT)
        await tmp_store.connection.execute(
            "UPDATE advisor_decisions SET safety_evaluation_id = NULL WHERE run_id = ?",
            ("run-1",),
        )
        await tmp_store.connection.commit()

        runs = await tmp_store.list_runs()
        assert len(runs) == 1
        summary = runs[0]
        assert summary.advisor_consults == 1
        assert summary.advisor_clamped == 0
        assert summary.advisor_rejected == 0
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_dangling_advisor_fk_does_not_guess_by_tick(
    tmp_store: RoastStore,
) -> None:
    """A corrupt dangling advisor FK stays closed instead of tick-falling back."""
    await seeded_store(tmp_store)
    try:
        await _record_consult(tmp_store, tick=8, status="ok", verdict=SafetyVerdict.ALLOW)
        await tmp_store.record_safety_evaluation(
            run_id="run-1",
            tick=8,
            evaluation=SafetyEvaluation(
                rule="r",
                verdict=SafetyVerdict.REJECT,
                input_heat=120,
                input_fan=40,
                adjusted_heat=100,
                adjusted_fan=40,
                reason="later same-tick reject",
            ),
        )
        # Shape a corrupt legacy row that normal FK enforcement would reject.
        await tmp_store.connection.execute("PRAGMA foreign_keys = OFF")
        await tmp_store.connection.execute(
            "UPDATE advisor_decisions SET safety_evaluation_id = ? WHERE run_id = ?",
            (999_999, "run-1"),
        )
        await tmp_store.connection.commit()
        await tmp_store.connection.execute("PRAGMA foreign_keys = ON")

        runs = await tmp_store.list_runs()
        assert len(runs) == 1
        summary = runs[0]
        assert summary.advisor_consults == 1
        assert summary.advisor_clamped == 0
        assert summary.advisor_rejected == 0
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_connection_uses_name_keyed_rows(tmp_store: RoastStore) -> None:
    """The connection's row factory is name-keyed ``aiosqlite.Row`` (#242).

    The read projections address columns by name (``row["col"]``) rather than by
    positional index, so adding or reordering a SELECT column can never silently
    shift a downstream index into the wrong field. This pins the mechanism that
    makes that possible: a regression to the default tuple factory would make the
    keyed reads fail outright.
    """
    await tmp_store.initialize()
    try:
        assert tmp_store.connection.row_factory is aiosqlite_module.Row
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_list_runs_maps_every_column_to_its_field(tmp_store: RoastStore) -> None:
    """``list_runs`` maps each SELECT column to the correct ``RoastSummary`` field (#242).

    Every projected field is seeded with a *distinct, type-distinguishable* value
    so that a positional ``row[N]`` shift — the exact regression named-column
    access prevents — would land a value in the wrong field and fail an assertion.
    Same-type fields (the four advisor counts; the two ISO timestamps) carry
    distinct values too, so even a within-type column reorder is caught.
    """
    profile = RoastProfile(
        name="map-test",
        bean_origin="Colombia",
        bean_varietal="Caturra",
        country="Colombia",
        bean_species="arabica",
        is_blend=True,
        processing="washed",
        altitude_m=1750,
        bean_weight_grams=300.0,
        initial_heat_percent=65,
        initial_fan_percent=35,
        target_drop_temp_c=204.0,
        target_development_percent=18.0,
    )
    await tmp_store.initialize()
    try:
        await tmp_store.create_run(
            run_id="map-run",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
            started_at_utc="2026-06-07T13:00:00+00:00",
        )
        # A telemetry snapshot carrying a distinct development_percent.
        wrote = await tmp_store.record_telemetry(
            run_id="map-run",
            tick=10,
            agent_phase=RoastPhase.DEVELOPMENT,
            elapsed_seconds=120.0,
            interval_seconds=5.0,
            telemetry=None,
            development_percent=17.5,
        )
        assert wrote
        # A distinct first-crack event time (≠ started/completed timestamps).
        await tmp_store.record_event(
            run_id="map-run",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc="2026-06-07T13:09:30+00:00",
        )
        # Advisor consults: distinct clamp/reject/fail counts so no two of the
        # four count fields share a value (2 clamped, 1 rejected, 3 failed,
        # 6 consults total — all different).
        await _record_consult(
            tmp_store, tick=1, status="ok", verdict=SafetyVerdict.CLAMP, run_id="map-run"
        )
        await _record_consult(
            tmp_store, tick=2, status="ok", verdict=SafetyVerdict.CLAMP, run_id="map-run"
        )
        await _record_consult(
            tmp_store, tick=3, status="ok", verdict=SafetyVerdict.REJECT, run_id="map-run"
        )
        await _record_consult(tmp_store, tick=4, status="timeout", verdict=None, run_id="map-run")
        await _record_consult(tmp_store, tick=5, status="malformed", verdict=None, run_id="map-run")
        await _record_consult(
            tmp_store, tick=6, status="provider_error", verdict=None, run_id="map-run"
        )
        # Complete the run, then rate it: distinct completed time, outcome, rating.
        await tmp_store.complete_run(
            run_id="map-run",
            outcome="completed",
            agent_phase=RoastPhase.COMPLETE,
        )
        await tmp_store.set_operator_rating("map-run", rating=4, notes="balanced")

        runs = await tmp_store.list_runs()
        assert len(runs) == 1
        summary = runs[0]
        # Each assertion pins a column→field mapping; a positional shift breaks one.
        assert summary.id == "map-run"
        assert summary.started_at_utc == "2026-06-07T13:00:00+00:00"
        assert summary.first_crack_at_utc == "2026-06-07T13:09:30+00:00"
        assert summary.agent_phase is RoastPhase.COMPLETE
        assert summary.outcome == "completed"
        assert summary.completed_at_utc is not None
        assert summary.completed_at_utc != summary.started_at_utc
        assert summary.bean_origin == "Colombia"
        assert summary.bean_varietal == "Caturra"
        assert summary.country == "Colombia"
        assert summary.bean_species == "arabica"
        assert summary.is_blend is True
        assert summary.processing == "washed"
        assert summary.altitude_m == 1750
        assert summary.rating == 4
        assert summary.development_percent == 17.5
        assert summary.advisor_consults == 6
        assert summary.advisor_clamped == 2
        assert summary.advisor_rejected == 1
        assert summary.advisor_failed == 3
    finally:
        await tmp_store.close()


def _origin_profile(origin: str) -> RoastProfile:
    return RoastProfile(
        name=f"{origin} test",
        bean_origin=origin,
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=205.0,
        target_development_percent=20.0,
    )


@pytest.mark.asyncio
async def test_count_completed_runs_for_origin_counts_only_finalised_matches(
    tmp_store: RoastStore,
) -> None:
    """#385: the per-origin recording count is prior COMPLETED runs of the same
    origin slug — excluding other origins and the still-active (uncompleted) run."""
    await tmp_store.initialize()
    try:
        colombia = _origin_profile("Colombia")
        ethiopia = _origin_profile("Ethiopia")
        slug = recording_origin_slug(colombia)
        assert slug is not None

        # First roast of the bean: no prior completed runs.
        assert await tmp_store.count_completed_runs_for_origin(slug) == 0

        # Two completed Colombia roasts + one completed Ethiopia + one active
        # Colombia run that must NOT count (not finalised).
        for run_id, profile in (
            ("c1", colombia),
            ("c2", colombia),
            ("e1", ethiopia),
        ):
            await tmp_store.create_run(
                run_id=run_id,
                profile=profile,
                config=AppConfig(),
                agent_phase=RoastPhase.STARTING,
            )
            await tmp_store.complete_run(
                run_id=run_id, outcome="completed", agent_phase=RoastPhase.COMPLETE
            )
        await tmp_store.create_run(
            run_id="c-active",
            profile=colombia,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )

        # Two completed Colombia → next Colombia roast is roast_num 3.
        assert await tmp_store.count_completed_runs_for_origin(slug) == 2
        ethiopia_slug = recording_origin_slug(ethiopia)
        assert ethiopia_slug is not None
        assert await tmp_store.count_completed_runs_for_origin(ethiopia_slug) == 1
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_count_completed_runs_for_origin_counts_finalised_faulted(
    tmp_store: RoastStore,
) -> None:
    """#385: a faulted-but-FINALISED run consumed a recording slot, so it counts
    (the criterion is ``completed_at_utc IS NOT NULL``, not a 'completed' outcome)."""
    await tmp_store.initialize()
    try:
        colombia = _origin_profile("Colombia")
        slug = recording_origin_slug(colombia)
        assert slug is not None
        await tmp_store.create_run(
            run_id="c-faulted",
            profile=colombia,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await tmp_store.complete_run(
            run_id="c-faulted",
            outcome="faulted",
            agent_phase=RoastPhase.FAULTED,
            fault_reason="test",
        )
        assert await tmp_store.count_completed_runs_for_origin(slug) == 1
    finally:
        await tmp_store.close()


# --- #567 Slice A: reference-curve retrieval + representation ---


def _reference_profile(origin: str = "Guatemala", *, weight: float = 250.0) -> RoastProfile:
    return RoastProfile(
        name=f"{origin} reference test",
        bean_origin=origin,
        bean_weight_grams=weight,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=205.0,
        target_development_percent=20.0,
    )


async def _seed_completed_run(
    store: RoastStore,
    run_id: str,
    *,
    profile: RoastProfile,
    rating: Literal[1, 2, 3, 4, 5] | None,
    corrected_charge_grams: float | None = None,
) -> None:
    """Create + complete a run, optionally rating it and correcting its charge."""
    await store.create_run(
        run_id=run_id, profile=profile, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
    await store.complete_run(run_id=run_id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    if rating is not None:
        await store.set_operator_rating(run_id, rating=rating)
    if corrected_charge_grams is not None:
        await store.set_corrected_charge(run_id, corrected_charge_grams=corrected_charge_grams)


async def _record_row(
    store: RoastStore,
    run_id: str,
    tick: int,
    *,
    phase: RoastPhase,
    charge_elapsed: float | None,
    bean_temp: float | None,
    dev_pct: float | None = None,
) -> None:
    """Insert one telemetry row via the real write path (interval_seconds=0.0
    so every call writes, regardless of the increasing ``elapsed_seconds``)."""
    await store.record_telemetry(
        run_id=run_id,
        tick=tick,
        agent_phase=phase,
        elapsed_seconds=float(tick),
        interval_seconds=0.0,
        telemetry=None
        if bean_temp is None
        else RoastTelemetry(
            bean_temp_c=bean_temp, env_temp_c=bean_temp + 15.0, bean_ror_c_per_min=6.0
        ),
        development_percent=dev_pct,
        charge_elapsed_seconds=charge_elapsed,
    )


async def _seed_onset_reference(
    store: RoastStore,
    *,
    run_id: str,
    event_source: RoastEventSource | None = RoastEventSource.MCP,
    event_at: str = "2026-08-23T12:00:10+00:00",
    started_at: str = "2026-08-23T11:50:00+00:00",
    rows: tuple[tuple[float | None, float, RoastPhase, str, str], ...] = (
        (600.0, 185.0, RoastPhase.ROASTING_PRE_FIRST_CRACK, "2026-08-23T12:00:00+00:00", "{}"),
        (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
        (620.0, 191.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:20+00:00", "{}"),
    ),
) -> None:
    """Seed one rated reference run with controlled telemetry/onset state."""
    profile = _reference_profile()
    await store.create_run(
        run_id=run_id,
        profile=profile,
        config=AppConfig(),
        agent_phase=RoastPhase.STARTING,
        started_at_utc=started_at,
    )
    for tick, (elapsed, temperature, phase, recorded_at, raw_state) in enumerate(rows, start=1):
        await _record_row(
            store,
            run_id,
            tick,
            phase=phase,
            charge_elapsed=elapsed,
            bean_temp=temperature,
        )
        await store.connection.execute(
            "UPDATE telemetry_snapshots SET recorded_at_utc = ?, raw_state_json = ?"
            " WHERE run_id = ? AND tick = ?",
            (recorded_at, raw_state, run_id, tick),
        )
    if event_source is not None:
        await store.record_event(
            run_id=run_id,
            kind=RoastEventKind.FIRST_CRACK,
            source=event_source,
            recorded_at_utc=event_at,
        )
    await store.connection.commit()
    await store.complete_run(run_id=run_id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.set_operator_rating(run_id, rating=4)


async def _reference_landmark_pair(
    store: RoastStore, run_id: str
) -> tuple[float | None, float | None]:
    """Return the persisted reference first-crack pair for one seeded run."""
    reference = await store._build_reference_roast(  # pyright: ignore[reportPrivateUsage]
        run_id, "test-slug"
    )
    assert reference is not None
    return reference.landmarks.first_crack_elapsed_s, reference.landmarks.first_crack_temp_c


async def _seed_reference_identity_run(
    store: RoastStore,
    *,
    run_id: str,
    event_source: RoastEventSource,
    row_count: int = 42,
) -> None:
    """Seed a curve whose onset path can vary without changing its shape."""
    profile = _reference_profile()
    await store.create_run(
        run_id=run_id,
        profile=profile,
        config=AppConfig(),
        agent_phase=RoastPhase.STARTING,
        started_at_utc="2026-08-23T11:50:00+00:00",
    )
    first_development = row_count // 2
    last_development = max(row_count - 6, first_development)
    for index in range(row_count):
        phase = (
            RoastPhase.ROASTING_PRE_FIRST_CRACK
            if index < first_development
            else RoastPhase.DEVELOPMENT
            if index <= last_development
            else RoastPhase.COOLING
        )
        await _record_row(
            store,
            run_id,
            index + 1,
            phase=phase,
            charge_elapsed=600.0 + index,
            bean_temp=180.0 + index,
            dev_pct=float(index) if phase is RoastPhase.DEVELOPMENT else None,
        )
        recorded_at = datetime(2026, 8, 23, 12, 0, index, tzinfo=UTC).isoformat()
        raw_state = (
            '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:15+00:00"}}'
            if index == last_development
            else "{}"
        )
        await store.connection.execute(
            "UPDATE telemetry_snapshots SET recorded_at_utc = ?, raw_state_json = ?"
            " WHERE run_id = ? AND tick = ?",
            (recorded_at, raw_state, run_id, index + 1),
        )
    await store.connection.commit()
    await store.record_event(
        run_id=run_id,
        kind=RoastEventKind.FIRST_CRACK,
        source=event_source,
        recorded_at_utc=datetime(2026, 8, 23, 12, 0, first_development, tzinfo=UTC).isoformat(),
    )
    await store.complete_run(run_id=run_id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.set_operator_rating(run_id, rating=4)


def _reference_shape_without_first_crack(reference: ReferenceRoast) -> str:
    """Serialize output that must not vary with landmark-pair selection."""
    return json.dumps(
        {
            "drop_temp_c": reference.landmarks.drop_temp_c,
            "drop_development_percent": reference.landmarks.drop_development_percent,
            "curve": [sample.model_dump(mode="json") for sample in reference.curve],
        },
        separators=(",", ":"),
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_build_reference_roast_curve_uses_insertion_order_after_tick_reset(
    tmp_path: Path,
) -> None:
    """Reference samples stay chronological across overlapping restart ticks."""
    profile = _reference_profile()
    store = RoastStore(tmp_path / "reference-restart.sqlite3")
    await store.initialize()
    try:
        await store.create_run(
            run_id="restart-reference",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        for tick, charge_elapsed, bean_temp in (
            (0, 10.0, 150.0),
            (1, 20.0, 160.0),
            (2, 30.0, 170.0),
        ):
            await _record_row(
                store,
                "restart-reference",
                tick,
                phase=RoastPhase.DEVELOPMENT,
                charge_elapsed=charge_elapsed,
                bean_temp=bean_temp,
            )

        await store.close()
        store = RoastStore(store.db_path)
        await store.initialize()
        for tick, charge_elapsed, bean_temp in (
            (0, 40.0, 180.0),
            (1, 50.0, 190.0),
        ):
            await _record_row(
                store,
                "restart-reference",
                tick,
                phase=RoastPhase.DEVELOPMENT,
                charge_elapsed=charge_elapsed,
                bean_temp=bean_temp,
            )
        await store.complete_run(
            run_id="restart-reference",
            outcome="completed",
            agent_phase=RoastPhase.COMPLETE,
        )
        await store.set_operator_rating("restart-reference", rating=4)

        reference = await store._build_reference_roast(  # pyright: ignore[reportPrivateUsage]
            "restart-reference", "test-slug"
        )
        assert reference is not None
        assert [sample.t_s for sample in reference.curve] == [10.0, 20.0, 30.0, 40.0, 50.0]
    finally:
        await store.close()


async def _seed_unbuildable_run(
    store: RoastStore, run_id: str, *, profile: RoastProfile, rating: Literal[1, 2, 3, 4, 5]
) -> None:
    """Create + complete + rate a run with telemetry but NO ``development``-
    phase rows — passes retrieval's rating/slug/weight filters (it is a
    legitimate ranked candidate) but fails :meth:`RoastStore._build_reference_roast`
    (no usable landmarks), pinning the Fix B (PR #574 review) fallthrough."""
    await store.create_run(
        run_id=run_id, profile=profile, config=AppConfig(), agent_phase=RoastPhase.STARTING
    )
    await _record_row(
        store,
        run_id,
        1,
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        charge_elapsed=100.0,
        bean_temp=150.0,
    )
    await store.complete_run(run_id=run_id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    await store.set_operator_rating(run_id, rating=rating)


@pytest.mark.asyncio
async def test_find_reference_run_picks_the_highest_rating(tmp_store: RoastStore) -> None:
    """#567 §1.2: best-rated wins among qualifying candidates."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None
        await _seed_completed_run(tmp_store, "low", profile=profile, rating=3)
        await _seed_completed_run(tmp_store, "high", profile=profile, rating=5)
        assert await tmp_store.find_reference_run(slug, 250.0) == "high"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_ties_break_on_recency(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#567 §1.2: equal ratings tie-break on the most recent ``completed_at_utc``.

    ``completed_at_utc`` is immutable once a run is finalized (the store's own
    completion trigger), so the two runs are given distinct completion instants
    by controlling ``_utc_now()`` at seed time rather than backdating after the
    fact."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None

        monkeypatch.setattr(store_module, "_utc_now", lambda: "2026-01-01T00:00:00+00:00")
        await _seed_completed_run(tmp_store, "older", profile=profile, rating=4)

        monkeypatch.setattr(store_module, "_utc_now", lambda: "2026-06-01T00:00:00+00:00")
        await _seed_completed_run(tmp_store, "newer", profile=profile, rating=4)

        assert await tmp_store.find_reference_run(slug, 250.0) == "newer"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_excludes_below_min_rating(tmp_store: RoastStore) -> None:
    """#567 §1.2: a 2-star reference is worse than none — excluded by the
    default ``min_rating=3`` floor."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None
        await _seed_completed_run(tmp_store, "two-star", profile=profile, rating=2)
        assert await tmp_store.find_reference_run(slug, 250.0) is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_excludes_unrated_runs(tmp_store: RoastStore) -> None:
    """A completed but never-rated run is today's empty-behavior baseline —
    it must stay excluded, not silently qualify."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None
        await _seed_completed_run(tmp_store, "unrated", profile=profile, rating=None)
        assert await tmp_store.find_reference_run(slug, 250.0) is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_excludes_a_different_origin_slug(tmp_store: RoastStore) -> None:
    """#567 §1.1: retrieval is keyed on the recording-origin slug — a
    highly-rated run of a DIFFERENT bean never qualifies."""
    await tmp_store.initialize()
    try:
        this_profile = _reference_profile("Guatemala")
        other_profile = _reference_profile("Ethiopia")
        this_slug = recording_origin_slug(this_profile)
        assert this_slug is not None
        await _seed_completed_run(tmp_store, "other-bean", profile=other_profile, rating=5)
        assert await tmp_store.find_reference_run(this_slug, 250.0) is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_excludes_outside_weight_tolerance(tmp_store: RoastStore) -> None:
    """#567 §1.4: ±10% of a 250 g charge is 225-275 g; a 200 g candidate falls
    outside the band and is excluded."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile(weight=200.0)
        slug = recording_origin_slug(profile)
        assert slug is not None
        await _seed_completed_run(tmp_store, "light", profile=profile, rating=5)
        assert await tmp_store.find_reference_run(slug, 250.0) is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_includes_inside_weight_tolerance(tmp_store: RoastStore) -> None:
    """#567 §1.4: ±10% of a 250 g charge is 225-275 g; a 240 g candidate falls
    inside the band and is included."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile(weight=240.0)
        slug = recording_origin_slug(profile)
        assert slug is not None
        await _seed_completed_run(tmp_store, "close", profile=profile, rating=5)
        assert await tmp_store.find_reference_run(slug, 250.0) == "close"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_uses_corrected_charge_over_the_frozen_weight(
    tmp_store: RoastStore,
) -> None:
    """#567 §1.4: the frozen profile weight (200 g) is outside ±10% of 250 g,
    but the operator-corrected charge (245 g) is inside it — the correction
    is what the tolerance check compares against."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile(weight=200.0)
        slug = recording_origin_slug(profile)
        assert slug is not None
        await _seed_completed_run(
            tmp_store, "corrected", profile=profile, rating=5, corrected_charge_grams=245.0
        )
        assert await tmp_store.find_reference_run(slug, 250.0) == "corrected"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_corrected_charge_can_push_a_candidate_out_of_tolerance(
    tmp_store: RoastStore,
) -> None:
    """#567 §1.4: the correction overrides the frozen weight in BOTH
    directions — a frozen weight that would qualify (240 g, inside ±10% of
    250 g) is excluded once corrected to 100 g, well outside tolerance."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile(weight=240.0)
        slug = recording_origin_slug(profile)
        assert slug is not None
        await _seed_completed_run(
            tmp_store, "corrected-out", profile=profile, rating=5, corrected_charge_grams=100.0
        )
        assert await tmp_store.find_reference_run(slug, 250.0) is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_includes_the_low_weight_tolerance_boundary(
    tmp_store: RoastStore,
) -> None:
    """#567 §1.4: the tolerance check (store.py's ``<= tolerance``) is
    inclusive — a candidate at EXACTLY ``250 * 0.90 = 225.0`` g must be
    included, not excluded by an off-by-one ``<`` regression."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile(weight=225.0)
        slug = recording_origin_slug(profile)
        assert slug is not None
        await _seed_completed_run(tmp_store, "low-boundary", profile=profile, rating=5)
        assert await tmp_store.find_reference_run(slug, 250.0) == "low-boundary"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_includes_the_high_weight_tolerance_boundary(
    tmp_store: RoastStore,
) -> None:
    """#567 §1.4: the tolerance check (store.py's ``<= tolerance``) is
    inclusive — a candidate at EXACTLY ``250 * 1.10 = 275.0`` g must be
    included, not excluded by an off-by-one ``<`` regression."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile(weight=275.0)
        slug = recording_origin_slug(profile)
        assert slug is not None
        await _seed_completed_run(tmp_store, "high-boundary", profile=profile, rating=5)
        assert await tmp_store.find_reference_run(slug, 250.0) == "high-boundary"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_falls_through_a_higher_rated_wrong_slug_candidate(
    tmp_store: RoastStore,
) -> None:
    """#567 §1.2: the SQL ordering is best-rated-first, but a top-ranked
    candidate that fails the Python-level slug check must fall through to
    the next-best candidate rather than short-circuiting to ``None`` (a
    ``continue`` regressing to ``break``/``return None`` would pass every
    single-candidate test but fail here)."""
    await tmp_store.initialize()
    try:
        this_profile = _reference_profile("Guatemala")
        other_profile = _reference_profile("Ethiopia")
        this_slug = recording_origin_slug(this_profile)
        assert this_slug is not None
        assert recording_origin_slug(other_profile) != this_slug

        await _seed_completed_run(
            tmp_store, "wrong-bean-five-star", profile=other_profile, rating=5
        )
        await _seed_completed_run(tmp_store, "right-bean-four-star", profile=this_profile, rating=4)

        assert await tmp_store.find_reference_run(this_slug, 250.0) == "right-bean-four-star"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_falls_through_a_higher_rated_out_of_tolerance_candidate(
    tmp_store: RoastStore,
) -> None:
    """#567 §1.2/§1.4: the SQL-ranked top candidate is the SAME bean but
    OUTSIDE the ±10% weight tolerance — retrieval must fall through to a
    lower-rated, in-tolerance candidate rather than stopping at the first
    same-slug row it sees."""
    await tmp_store.initialize()
    try:
        out_of_tolerance = _reference_profile(weight=100.0)  # far outside ±10% of 250 g
        in_tolerance = _reference_profile(weight=240.0)  # inside ±10% of 250 g
        slug = recording_origin_slug(out_of_tolerance)
        assert slug is not None
        assert recording_origin_slug(in_tolerance) == slug

        await _seed_completed_run(
            tmp_store, "top-rated-wrong-weight", profile=out_of_tolerance, rating=5
        )
        await _seed_completed_run(
            tmp_store, "lower-rated-right-weight", profile=in_tolerance, rating=3
        )

        assert await tmp_store.find_reference_run(slug, 250.0) == "lower-rated-right-weight"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_excludes_faulted_runs(tmp_store: RoastStore) -> None:
    """#567 Fix C (PR #574 review): a faulted-but-FINALIZED run passes
    ``completed_at_utc IS NOT NULL`` (it still consumed a recording slot, per
    :meth:`RoastStore.count_completed_runs_for_origin`), but a faulted
    trajectory is a bad reference — excluded via the retrieval SQL's
    ``outcome = 'completed'`` clause."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None
        await tmp_store.create_run(
            run_id="faulted",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await tmp_store.complete_run(
            run_id="faulted",
            outcome="faulted",
            agent_phase=RoastPhase.FAULTED,
            fault_reason="test",
        )
        await tmp_store.set_operator_rating("faulted", rating=5)
        assert await tmp_store.find_reference_run(slug, 250.0) is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_falls_through_a_higher_rated_faulted_candidate(
    tmp_store: RoastStore,
) -> None:
    """#567 Fix C (PR #574 review): a higher-rated FAULTED run of the same
    bean+weight must not win over a lower-rated but genuinely completed one —
    the ``outcome = 'completed'`` filter, not a Python-level short-circuit."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None
        await tmp_store.create_run(
            run_id="faulted-five-star",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await tmp_store.complete_run(
            run_id="faulted-five-star",
            outcome="faulted",
            agent_phase=RoastPhase.FAULTED,
            fault_reason="test",
        )
        await tmp_store.set_operator_rating("faulted-five-star", rating=5)
        await _seed_completed_run(tmp_store, "completed-three-star", profile=profile, rating=3)

        assert await tmp_store.find_reference_run(slug, 250.0) == "completed-three-star"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_excludes_a_discarded_run(tmp_store: RoastStore) -> None:
    """#582: a soft-excluded (discarded) run must never surface as a reference
    candidate — the corpus-hygiene point of the flag. Mirrors
    :func:`test_find_reference_run_excludes_faulted_runs`, but for a run that
    is otherwise a perfectly legitimate completed+rated+same-bean candidate."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None
        await _seed_completed_run(tmp_store, "discarded", profile=profile, rating=5)
        await tmp_store.set_run_excluded("discarded", excluded=True)
        assert await tmp_store.find_reference_run(slug, 250.0) is None
        assert await tmp_store.load_reference_roast(slug, 250.0) is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_find_reference_run_falls_through_a_higher_rated_discarded_candidate(
    tmp_store: RoastStore,
) -> None:
    """#582: a higher-rated but DISCARDED run of the same bean+weight must not
    win over a lower-rated genuinely-included one — mirrors the faulted-run
    fallthrough test for the excluded flag."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None
        await _seed_completed_run(tmp_store, "discarded-five-star", profile=profile, rating=5)
        await tmp_store.set_run_excluded("discarded-five-star", excluded=True)
        await _seed_completed_run(tmp_store, "included-three-star", profile=profile, rating=3)

        assert await tmp_store.find_reference_run(slug, 250.0) == "included-three-star"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_landmarks_use_development_phase_not_cooling_tail(
    tmp_store: RoastStore,
) -> None:
    """#567 design note §6.4a: first-crack/drop landmarks are the FIRST/LAST
    ``development``-phase telemetry rows, never the run's final row — which
    here is a post-drop cooling tail with a FALLING bean temperature."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        await tmp_store.create_run(
            run_id="landmarks",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await _record_row(
            tmp_store,
            "landmarks",
            1,
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            charge_elapsed=600.0,
            bean_temp=185.0,
        )
        await _record_row(
            tmp_store,
            "landmarks",
            2,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=612.0,
            bean_temp=188.0,
            dev_pct=1.0,
        )
        await _record_row(
            tmp_store,
            "landmarks",
            3,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=650.0,
            bean_temp=192.0,
            dev_pct=6.0,
        )
        await _record_row(
            tmp_store,
            "landmarks",
            4,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=715.0,
            bean_temp=190.0,
            dev_pct=15.1,
        )
        # Post-drop cooling tail: bean temp keeps being recorded and FALLS.
        await _record_row(
            tmp_store,
            "landmarks",
            5,
            phase=RoastPhase.COOLING,
            charge_elapsed=730.0,
            bean_temp=174.0,
        )
        await _record_row(
            tmp_store,
            "landmarks",
            6,
            phase=RoastPhase.COMPLETE,
            charge_elapsed=760.0,
            bean_temp=100.0,
        )
        await tmp_store.complete_run(
            run_id="landmarks", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("landmarks", rating=4)

        reference = await tmp_store._build_reference_roast("landmarks", "test-slug")  # pyright: ignore[reportPrivateUsage]
        assert reference is not None
        assert reference.source_run_id == "landmarks"
        assert reference.origin_slug == "test-slug"
        assert reference.landmarks.first_crack_temp_c == 188.0
        assert reference.landmarks.first_crack_elapsed_s == 612.0
        assert reference.landmarks.drop_temp_c == 190.0
        assert reference.landmarks.drop_development_percent == 15.1
        assert reference.landmarks.operator_rating == 4
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_adopts_complete_mcp_onset_pair(
    tmp_store: RoastStore,
) -> None:
    """A post-confirmation MCP onset maps and interpolates as one landmark pair."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        await tmp_store.create_run(
            run_id="onset-reference",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        for tick, elapsed, temperature, phase in (
            (1, 600.0, 185.0, RoastPhase.ROASTING_PRE_FIRST_CRACK),
            (2, 610.0, 188.0, RoastPhase.DEVELOPMENT),
            (3, 620.0, 191.0, RoastPhase.DEVELOPMENT),
        ):
            await _record_row(
                tmp_store,
                "onset-reference",
                tick,
                phase=phase,
                charge_elapsed=elapsed,
                bean_temp=temperature,
            )
        await tmp_store.connection.execute(
            "UPDATE telemetry_snapshots SET recorded_at_utc = CASE id - "
            "(SELECT MIN(id) FROM telemetry_snapshots) "
            "WHEN 0 THEN '2026-08-23T12:00:00+00:00' WHEN 1 THEN '2026-08-23T12:00:10+00:00' "
            "ELSE '2026-08-23T12:00:20+00:00' END, raw_state_json = CASE id - "
            "(SELECT MIN(id) FROM telemetry_snapshots) WHEN 2 THEN "
            '\'{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:05Z"}}\' '
            "ELSE '{}' END "
            "WHERE run_id = ?",
            ("onset-reference",),
        )
        await tmp_store.connection.execute(
            "UPDATE roast_runs SET started_at_utc = ? WHERE id = ?",
            ("2026-08-23T11:50:00+00:00", "onset-reference"),
        )
        await tmp_store.connection.commit()
        await tmp_store.record_event(
            run_id="onset-reference",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc="2026-08-23T12:00:10+00:00",
        )
        await tmp_store.complete_run(
            run_id="onset-reference", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("onset-reference", rating=4)
        reference = await tmp_store._build_reference_roast(  # pyright: ignore[reportPrivateUsage]
            "onset-reference", "test-slug"
        )
        assert reference is not None
        assert reference.landmarks.first_crack_elapsed_s == 605.0
        assert reference.landmarks.first_crack_temp_c == 186.5
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_keeps_development_pair_for_non_mcp_accepted_event(
    tmp_store: RoastStore,
) -> None:
    """T3: MCP-looking state cannot launder an operator confirmation."""
    await tmp_store.initialize()
    try:
        await _seed_onset_reference(
            tmp_store,
            run_id="operator-confirmed",
            event_source=RoastEventSource.OPERATOR,
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
                (
                    620.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:20+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:05Z"}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "operator-confirmed") == (610.0, 188.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_uses_earliest_event_source_not_later_mcp_duplicate(
    tmp_store: RoastStore,
) -> None:
    """T4: the later MCP duplicate cannot replace the accepted controller event."""
    await tmp_store.initialize()
    try:
        await _seed_onset_reference(
            tmp_store,
            run_id="later-mcp",
            event_source=RoastEventSource.CONTROLLER,
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
                (
                    620.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:20+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:05Z"}}',
                ),
            ),
        )
        await tmp_store.record_event(
            run_id="later-mcp",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc="2026-08-23T12:00:11+00:00",
        )
        assert await _reference_landmark_pair(tmp_store, "later-mcp") == (610.0, 188.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_ignores_onset_without_first_crack_event(
    tmp_store: RoastStore,
) -> None:
    """T5: state alone cannot establish first-crack provenance."""
    await tmp_store.initialize()
    try:
        await _seed_onset_reference(
            tmp_store,
            run_id="no-confirmation",
            event_source=None,
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
                (
                    620.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:20+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:05Z"}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "no-confirmation") == (610.0, 188.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_excludes_pre_confirmation_onset_state(
    tmp_store: RoastStore,
) -> None:
    """T6: only state persisted at or after MCP confirmation is eligible."""
    await tmp_store.initialize()
    try:
        await _seed_onset_reference(
            tmp_store,
            run_id="pre-confirmation",
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:05Z"}}',
                ),
                (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
                (620.0, 191.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:20+00:00", "{}"),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "pre-confirmation") == (610.0, 188.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_skips_malformed_state_and_uses_other_onset(
    tmp_store: RoastStore,
) -> None:
    """T7: malformed state is ignored without blocking valid post-event state."""
    await tmp_store.initialize()
    try:
        await _seed_onset_reference(
            tmp_store,
            run_id="malformed-state",
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (
                    610.0,
                    188.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:10+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:05Z"}}',
                ),
                (620.0, 191.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:20+00:00", "not-json"),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "malformed-state") == (605.0, 186.5)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "started_at", "event_at"),
    [
        ("not-a-timestamp", "2026-08-23T11:50:00+00:00", "2026-08-23T12:00:10+00:00"),
        ("2026-08-23T11:49:59+00:00", "2026-08-23T11:50:00+00:00", "2026-08-23T12:00:10+00:00"),
        ("2026-08-23T12:00:11+00:00", "2026-08-23T11:50:00+00:00", "2026-08-23T12:00:10+00:00"),
    ],
)
async def test_build_reference_roast_falls_back_for_unusable_or_out_of_window_onset(
    tmp_store: RoastStore, candidate: str, started_at: str, event_at: str
) -> None:
    """T8: parsing and inclusive run/confirmation bounds fail closed."""
    await tmp_store.initialize()
    try:
        await _seed_onset_reference(
            tmp_store,
            run_id="bad-onset",
            started_at=started_at,
            event_at=event_at,
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
                (
                    620.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:20+00:00",
                    f'{{"first_crack_status":{{"detected_at_utc":"{candidate}"}}}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "bad-onset") == (610.0, 188.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_falls_back_when_mapping_is_after_confirmation(
    tmp_store: RoastStore,
) -> None:
    """T9: a valid onset cannot map past the first development confirmation."""
    await tmp_store.initialize()
    try:
        await _seed_onset_reference(
            tmp_store,
            run_id="mapped-late",
            event_at="2026-08-23T12:00:30+00:00",
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
                (
                    620.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:30+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:25+00:00"}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "mapped-late") == (610.0, 188.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_falls_back_when_mapping_precedes_usable_curve(
    tmp_store: RoastStore,
) -> None:
    """T10: reference interpolation does not extrapolate before the curve."""
    await tmp_store.initialize()
    try:
        await _seed_onset_reference(
            tmp_store,
            run_id="mapped-early",
            event_at="2026-08-23T12:00:30+00:00",
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:10+00:00",
                    "{}",
                ),
                (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:20+00:00", "{}"),
                (
                    620.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:30+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:05+00:00"}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "mapped-early") == (610.0, 188.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_enforces_usable_span_before_interpolation(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T10 isolation: pre-curve mappings fall back before interpolation runs."""
    await tmp_store.initialize()
    try:

        def interpolate_pre_span(_t: object, _samples: Iterable[tuple[object, object]]) -> float:
            return 186.5

        monkeypatch.setattr(store_module, "interpolate_at", interpolate_pre_span)
        await _seed_onset_reference(
            tmp_store,
            run_id="span-before-interpolation",
            event_at="2026-08-23T12:00:30+00:00",
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:10+00:00",
                    "{}",
                ),
                (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:20+00:00", "{}"),
                (
                    620.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:30+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:05+00:00"}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "span-before-interpolation") == (
            610.0,
            188.0,
        )
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_enforces_event_window_before_mapping(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T8 isolation: an out-of-window onset cannot reach safe mapping."""
    await tmp_store.initialize()
    try:

        def map_out_of_window(_target: object, _anchors: Iterable[tuple[object, object]]) -> float:
            return 605.0

        monkeypatch.setattr(store_module, "utc_to_run_seconds", map_out_of_window)
        await _seed_onset_reference(
            tmp_store,
            run_id="window-before-mapping",
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
                (
                    620.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:20+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T11:49:59+00:00"}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "window-before-mapping") == (610.0, 188.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_falls_back_for_non_monotone_usable_curve(
    tmp_store: RoastStore,
) -> None:
    """T11: a non-monotone curve cannot provide a first-crack temperature."""
    await tmp_store.initialize()
    try:
        await _seed_onset_reference(
            tmp_store,
            run_id="non-monotone",
            event_at="2026-08-23T12:00:30+00:00",
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (620.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
                (
                    610.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:30+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:00+00:00"}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "non-monotone") == (620.0, 188.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_keeps_complete_fallback_pair_when_interpolation_fails(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T12: an interpolation-only failure never partially adopts an onset."""
    await tmp_store.initialize()
    try:

        def reject_interpolation(_t: object, _samples: Iterable[tuple[object, object]]) -> None:
            return None

        monkeypatch.setattr(store_module, "interpolate_at", reject_interpolation)
        await _seed_onset_reference(
            tmp_store,
            run_id="atomic-interpolation",
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
                (
                    620.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:20+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:05+00:00"}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "atomic-interpolation") == (610.0, 188.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_falls_back_when_first_development_clock_is_null(
    tmp_store: RoastStore,
) -> None:
    """A NULL first-development clock prevents atomic onset adoption safely."""
    await tmp_store.initialize()
    try:
        await _seed_onset_reference(
            tmp_store,
            run_id="first-development-clock-null",
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (None, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
                (
                    620.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:20+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:05+00:00"}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "first-development-clock-null") == (
            None,
            188.0,
        )
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_fallback_keeps_null_first_development_temperature(
    tmp_store: RoastStore,
) -> None:
    """A forced fallback keeps the first-development elapsed/NULL-temp pair."""
    await tmp_store.initialize()
    try:
        await _seed_onset_reference(
            tmp_store,
            run_id="first-development-temperature-null",
            event_source=RoastEventSource.OPERATOR,
        )
        await tmp_store.connection.execute(
            "UPDATE telemetry_snapshots SET bean_temp_c = NULL WHERE run_id = ? AND tick = ?",
            ("first-development-temperature-null", 2),
        )
        await tmp_store.connection.commit()
        assert await _reference_landmark_pair(tmp_store, "first-development-temperature-null") == (
            610.0,
            None,
        )
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_sql_mcp_source_filter_backs_up_python_gate(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SQL source bind rejects an admitted non-MCP accepted event."""
    await tmp_store.initialize()
    try:

        def admit_any_source(_source: object) -> bool:
            return True

        monkeypatch.setattr(store_module, "is_mcp_first_crack_source", admit_any_source)
        await _seed_onset_reference(
            tmp_store,
            run_id="sql-source-filter",
            event_source=RoastEventSource.OPERATOR,
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (610.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:10+00:00", "{}"),
                (
                    620.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:20+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:05+00:00"}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "sql-source-filter") == (610.0, 188.0)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_enforces_upper_usable_span_before_interpolation(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T13: mapping above the last usable row falls back before interpolation."""
    await tmp_store.initialize()
    try:

        def interpolate_past_upper_span(
            _t: object, _samples: Iterable[tuple[object, object]]
        ) -> float:
            return 186.5

        monkeypatch.setattr(store_module, "interpolate_at", interpolate_past_upper_span)
        await _seed_onset_reference(
            tmp_store,
            run_id="upper-span-before-interpolation",
            event_at="2026-08-23T12:00:20+00:00",
            rows=(
                (
                    600.0,
                    185.0,
                    RoastPhase.ROASTING_PRE_FIRST_CRACK,
                    "2026-08-23T12:00:00+00:00",
                    "{}",
                ),
                (620.0, 188.0, RoastPhase.DEVELOPMENT, "2026-08-23T12:00:20+00:00", "{}"),
                (
                    610.0,
                    191.0,
                    RoastPhase.DEVELOPMENT,
                    "2026-08-23T12:00:30+00:00",
                    '{"first_crack_status":{"detected_at_utc":"2026-08-23T12:00:15+00:00"}}',
                ),
            ),
        )
        assert await _reference_landmark_pair(tmp_store, "upper-span-before-interpolation") == (
            620.0,
            188.0,
        )
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_onset_selection_preserves_curve_and_drop_identity(
    tmp_store: RoastStore,
) -> None:
    """T14: adopting or rejecting onset changes neither curve nor drop output."""
    await tmp_store.initialize()
    try:
        await _seed_reference_identity_run(
            tmp_store, run_id="adopted-identity", event_source=RoastEventSource.MCP
        )
        await _seed_reference_identity_run(
            tmp_store, run_id="fallback-identity", event_source=RoastEventSource.OPERATOR
        )
        adopted = await tmp_store._build_reference_roast(  # pyright: ignore[reportPrivateUsage]
            "adopted-identity", "test-slug"
        )
        fallback = await tmp_store._build_reference_roast(  # pyright: ignore[reportPrivateUsage]
            "fallback-identity", "test-slug"
        )
        assert adopted is not None and fallback is not None
        assert len(adopted.curve) == len(fallback.curve) == 30
        adopted_shape = _reference_shape_without_first_crack(adopted)
        fallback_shape = _reference_shape_without_first_crack(fallback)
        assert adopted_shape == fallback_shape
        assert (
            adopted.landmarks.first_crack_elapsed_s,
            adopted.landmarks.first_crack_temp_c,
        ) != (
            fallback.landmarks.first_crack_elapsed_s,
            fallback.landmarks.first_crack_temp_c,
        )
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_uses_constant_mcp_statement_shape(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T15: MCP onset uses one typed, bounded extra query, never per row."""
    await tmp_store.initialize()
    try:
        await _seed_reference_identity_run(
            tmp_store, run_id="mcp-small", event_source=RoastEventSource.MCP, row_count=6
        )
        await _seed_reference_identity_run(
            tmp_store, run_id="mcp-large", event_source=RoastEventSource.MCP, row_count=42
        )
        await _seed_reference_identity_run(
            tmp_store, run_id="operator", event_source=RoastEventSource.OPERATOR, row_count=6
        )
        original_execute: Any = tmp_store.connection._execute  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType, reportUnknownVariableType]
        observed: list[tuple[str, object]] = []

        async def traced_execute(function: object, *args: object, **kwargs: object) -> object:
            """Capture only SQL calls made by the reference builder."""
            if getattr(function, "__name__", None) == "execute":
                observed.append((str(args[0]), args[1] if len(args) > 1 else ()))
            return await original_execute(function, *args, **kwargs)

        monkeypatch.setattr(tmp_store.connection, "_execute", traced_execute)

        async def build_statements(run_id: str) -> list[tuple[str, object]]:
            observed.clear()
            reference = await tmp_store._build_reference_roast(  # pyright: ignore[reportPrivateUsage]
                run_id, "test-slug"
            )
            assert reference is not None
            return list(observed)

        mcp_small = await build_statements("mcp-small")
        mcp_large = await build_statements("mcp-large")
        operator = await build_statements("operator")
        assert len(mcp_small) == len(mcp_large) == 3
        assert len(operator) == 2
        for statements, run_id in ((mcp_small, "mcp-small"), (mcp_large, "mcp-large")):
            onset_sql, onset_parameters = next(
                (sql, parameters) for sql, parameters in statements if "json_valid" in sql
            )
            assert " IN (" not in onset_sql.upper()
            assert onset_parameters == (run_id, RoastEventSource.MCP.value)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_onset_uses_insertion_order_after_tick_reset(
    tmp_path: Path,
) -> None:
    """T16: duplicate ticks after restart cannot reorder onset anchors or the curve."""
    profile = _reference_profile()
    store = RoastStore(tmp_path / "onset-restart.sqlite3")
    await store.initialize()
    try:
        await store.create_run(
            run_id="onset-restart",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
            started_at_utc="2026-08-23T11:50:00+00:00",
        )
        for tick, elapsed, temperature in ((0, 10.0, 150.0), (1, 20.0, 160.0), (2, 30.0, 170.0)):
            await _record_row(
                store,
                "onset-restart",
                tick,
                phase=RoastPhase.ROASTING_PRE_FIRST_CRACK
                if elapsed < 30.0
                else RoastPhase.DEVELOPMENT,
                charge_elapsed=elapsed,
                bean_temp=temperature,
            )
        await store.close()
        store = RoastStore(store.db_path)
        await store.initialize()
        for tick, elapsed, temperature in ((0, 40.0, 180.0), (1, 50.0, 190.0)):
            await _record_row(
                store,
                "onset-restart",
                tick,
                phase=RoastPhase.DEVELOPMENT,
                charge_elapsed=elapsed,
                bean_temp=temperature,
            )
        await store.connection.execute(
            "UPDATE telemetry_snapshots SET recorded_at_utc = CASE id - "
            "(SELECT MIN(id) FROM telemetry_snapshots WHERE run_id = ?) "
            "WHEN 0 THEN '2026-08-23T12:00:00+00:00' "
            "WHEN 1 THEN '2026-08-23T12:00:10+00:00' "
            "WHEN 2 THEN '2026-08-23T12:00:20+00:00' "
            "WHEN 3 THEN '2026-08-23T12:00:30+00:00' "
            "ELSE '2026-08-23T12:00:40+00:00' END, "
            "raw_state_json = CASE id - (SELECT MIN(id) FROM telemetry_snapshots WHERE run_id = ?) "
            'WHEN 4 THEN \'{"first_crack_status":{'
            '"detected_at_utc":"2026-08-23T12:00:15+00:00"}}\' '
            "ELSE '{}' END WHERE run_id = ?",
            ("onset-restart", "onset-restart", "onset-restart"),
        )
        await store.connection.commit()
        await store.record_event(
            run_id="onset-restart",
            kind=RoastEventKind.FIRST_CRACK,
            source=RoastEventSource.MCP,
            recorded_at_utc="2026-08-23T12:00:20+00:00",
        )
        await store.complete_run(
            run_id="onset-restart", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await store.set_operator_rating("onset-restart", rating=4)
        reference = await store._build_reference_roast(  # pyright: ignore[reportPrivateUsage]
            "onset-restart", "test-slug"
        )
        assert reference is not None
        assert [sample.t_s for sample in reference.curve] == [10.0, 20.0, 30.0, 40.0, 50.0]
        assert (
            reference.landmarks.first_crack_elapsed_s,
            reference.landmarks.first_crack_temp_c,
        ) == (25.0, 165.0)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_load_reference_roast_is_idempotent_for_mcp_onset(tmp_store: RoastStore) -> None:
    """T17: repeated retrieval of one accepted onset reference is identical."""
    await tmp_store.initialize()
    try:
        await _seed_reference_identity_run(
            tmp_store, run_id="idempotent-onset", event_source=RoastEventSource.MCP
        )
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None
        first = await tmp_store.load_reference_roast(slug, profile.bean_weight_grams)
        second = await tmp_store.load_reference_roast(slug, profile.bean_weight_grams)
        assert first is not None and second is not None
        assert first == second
        assert first.model_dump_json() == second.model_dump_json()
    finally:
        await tmp_store.close()


def test_reference_roast_schema_and_field_sets_are_unchanged() -> None:
    """T18: onset provenance remains internal; reference wire shape is stable."""
    assert set(ReferenceLandmarks.model_fields) == {
        "first_crack_temp_c",
        "first_crack_elapsed_s",
        "drop_temp_c",
        "drop_development_percent",
        "operator_rating",
    }
    assert set(ReferenceRoast.model_fields) == {
        "source_run_id",
        "origin_slug",
        "landmarks",
        "curve",
    }
    schema: dict[str, object] = ReferenceRoast.model_json_schema()
    schema_properties = schema.get("properties")
    assert isinstance(schema_properties, dict)
    assert set(cast(dict[str, object], schema_properties)) == set(ReferenceRoast.model_fields)
    definitions = schema.get("$defs")
    assert isinstance(definitions, dict)
    landmark_schema = cast(dict[str, object], definitions).get("ReferenceLandmarks")
    assert isinstance(landmark_schema, dict)
    landmark_properties = cast(dict[str, object], landmark_schema).get("properties")
    assert isinstance(landmark_properties, dict)
    assert set(cast(dict[str, object], landmark_properties)) == set(ReferenceLandmarks.model_fields)


@pytest.mark.asyncio
async def test_build_reference_roast_curve_trims_at_drop_not_cooling_tail(
    tmp_store: RoastStore,
) -> None:
    """#567 design note §3.1 + §6.4a (Fix D, PR #574 review): the curve is
    trimmed to rows AT OR BEFORE the drop landmark — a post-drop cooling tail
    with a FALLING bean temperature must never pollute "what a good roast's
    shape looked like". The last-development index is shared with the drop
    landmark (Fix A/D), so the two can never disagree."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        await tmp_store.create_run(
            run_id="trim",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await _record_row(
            tmp_store,
            "trim",
            1,
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            charge_elapsed=600.0,
            bean_temp=185.0,
        )
        await _record_row(
            tmp_store,
            "trim",
            2,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=612.0,
            bean_temp=188.0,
            dev_pct=1.0,
        )
        await _record_row(
            tmp_store,
            "trim",
            3,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=715.0,
            bean_temp=190.0,
            dev_pct=15.1,
        )
        # Post-drop cooling tail: charge_elapsed keeps advancing, bean temp FALLS.
        await _record_row(
            tmp_store,
            "trim",
            4,
            phase=RoastPhase.COOLING,
            charge_elapsed=730.0,
            bean_temp=174.0,
        )
        await _record_row(
            tmp_store,
            "trim",
            5,
            phase=RoastPhase.COMPLETE,
            charge_elapsed=760.0,
            bean_temp=100.0,
        )
        await tmp_store.complete_run(
            run_id="trim", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("trim", rating=5)

        reference = await tmp_store._build_reference_roast("trim", "test-slug")  # pyright: ignore[reportPrivateUsage]
        assert reference is not None
        assert reference.landmarks.drop_temp_c == 190.0
        # The curve's LAST sample is the drop (development) row, not the
        # falling cooling tail — and no sample lies past the drop time.
        assert reference.curve[-1].t_s == 715.0
        assert reference.curve[-1].bean_c == 190.0
        assert all(sample.t_s <= 715.0 for sample in reference.curve)
        assert all(sample.bean_c not in (174.0, 100.0) for sample in reference.curve)
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_curve_sample_field_mapping(tmp_store: RoastStore) -> None:
    """Each curve sample maps ``charge_elapsed_seconds``/``bean_temp_c``/
    ``env_temp_c``/``bean_ror_c_per_min`` onto ``t_s``/``bean_c``/``env_c``/
    ``ror_c_min`` exactly."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        await tmp_store.create_run(
            run_id="mapping",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await tmp_store.record_telemetry(
            run_id="mapping",
            tick=1,
            agent_phase=RoastPhase.DEVELOPMENT,
            elapsed_seconds=1.0,
            interval_seconds=0.0,
            telemetry=RoastTelemetry(bean_temp_c=188.0, env_temp_c=210.0, bean_ror_c_per_min=4.5),
            development_percent=1.0,
            charge_elapsed_seconds=612.0,
        )
        await tmp_store.complete_run(
            run_id="mapping", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("mapping", rating=5)

        reference = await tmp_store._build_reference_roast("mapping", "test-slug")  # pyright: ignore[reportPrivateUsage]
        assert reference is not None
        assert len(reference.curve) == 1
        sample = reference.curve[0]
        assert sample.t_s == 612.0
        assert sample.bean_c == 188.0
        assert sample.env_c == 210.0
        assert sample.ror_c_min == 4.5
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_curve_skips_null_charge_elapsed_rows(
    tmp_store: RoastStore,
) -> None:
    """The pre-charge lead-in (``charge_elapsed_seconds IS NULL``) never
    becomes a curve point — ``t_s`` is a required field on
    ``ReferenceCurveSample``."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        await tmp_store.create_run(
            run_id="skip-null",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await _record_row(
            tmp_store,
            "skip-null",
            1,
            phase=RoastPhase.PREHEATING,
            charge_elapsed=None,
            bean_temp=25.0,
        )
        await _record_row(
            tmp_store,
            "skip-null",
            2,
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            charge_elapsed=None,
            bean_temp=90.0,
        )
        await _record_row(
            tmp_store,
            "skip-null",
            3,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=612.0,
            bean_temp=188.0,
            dev_pct=1.0,
        )
        await _record_row(
            tmp_store,
            "skip-null",
            4,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=715.0,
            bean_temp=190.0,
            dev_pct=15.1,
        )
        await tmp_store.complete_run(
            run_id="skip-null", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("skip-null", rating=5)

        reference = await tmp_store._build_reference_roast("skip-null", "test-slug")  # pyright: ignore[reportPrivateUsage]
        assert reference is not None
        assert [sample.t_s for sample in reference.curve] == [612.0, 715.0]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_downsamples_curve_to_at_most_30_keeping_first_and_last(
    tmp_store: RoastStore,
) -> None:
    """#567 design note §3.1: 100 usable rows downsample to <= 30 curve
    points, the first and last of which must survive the downsample."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        await tmp_store.create_run(
            run_id="curve",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        for i in range(100):
            tick = 1 + i
            phase = RoastPhase.DEVELOPMENT if i >= 90 else RoastPhase.ROASTING_PRE_FIRST_CRACK
            await _record_row(
                tmp_store,
                "curve",
                tick,
                phase=phase,
                charge_elapsed=float(i),
                bean_temp=100.0 + i,
                dev_pct=float(i) if i >= 90 else None,
            )
        await tmp_store.complete_run(
            run_id="curve", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("curve", rating=5)

        reference = await tmp_store._build_reference_roast("curve", "test-slug")  # pyright: ignore[reportPrivateUsage]
        assert reference is not None
        assert len(reference.curve) <= 30
        assert reference.curve[0].t_s == 0.0
        assert reference.curve[-1].t_s == 99.0
        # Strictly increasing: no duplicate/out-of-order index made it through.
        t_values = [sample.t_s for sample in reference.curve]
        assert t_values == sorted(set(t_values))
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_keeps_every_row_at_exactly_30_usable_rows(
    tmp_store: RoastStore,
) -> None:
    """#567 design note §3.1: exactly 30 usable rows is the
    ``count <= max_samples`` no-op boundary of ``_evenly_spaced_indices`` —
    every row survives unchanged, none dropped by an off-by-one."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        await tmp_store.create_run(
            run_id="exactly-30",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        for i in range(30):
            await _record_row(
                tmp_store,
                "exactly-30",
                1 + i,
                phase=RoastPhase.DEVELOPMENT,
                charge_elapsed=float(i),
                bean_temp=180.0 + i,
                dev_pct=float(i),
            )
        await tmp_store.complete_run(
            run_id="exactly-30", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("exactly-30", rating=5)

        reference = await tmp_store._build_reference_roast("exactly-30", "test-slug")  # pyright: ignore[reportPrivateUsage]
        assert reference is not None
        assert len(reference.curve) == 30
        assert [sample.t_s for sample in reference.curve] == [float(i) for i in range(30)]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_downsamples_31_rows_to_exactly_30(
    tmp_store: RoastStore,
) -> None:
    """#567 design note §3.1: 31 usable rows is one past the no-op boundary —
    the downsample must return EXACTLY 30 points (not 29, not 31), with the
    first and last usable rows preserved."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        await tmp_store.create_run(
            run_id="exactly-31",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        for i in range(31):
            await _record_row(
                tmp_store,
                "exactly-31",
                1 + i,
                phase=RoastPhase.DEVELOPMENT,
                charge_elapsed=float(i),
                bean_temp=180.0 + i,
                dev_pct=float(i),
            )
        await tmp_store.complete_run(
            run_id="exactly-31", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("exactly-31", rating=5)

        reference = await tmp_store._build_reference_roast("exactly-31", "test-slug")  # pyright: ignore[reportPrivateUsage]
        assert reference is not None
        assert len(reference.curve) == 30
        assert reference.curve[0].t_s == 0.0
        assert reference.curve[-1].t_s == 30.0
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_returns_none_without_development_telemetry(
    tmp_store: RoastStore,
) -> None:
    """A run that never reached first crack (no ``development`` rows) has no
    usable landmarks — the whole reference is unusable, not partially built."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        await tmp_store.create_run(
            run_id="no-dev",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await _record_row(
            tmp_store,
            "no-dev",
            1,
            phase=RoastPhase.PREHEATING,
            charge_elapsed=None,
            bean_temp=25.0,
        )
        await _record_row(
            tmp_store,
            "no-dev",
            2,
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            charge_elapsed=100.0,
            bean_temp=150.0,
        )
        await tmp_store.complete_run(
            run_id="no-dev", outcome="aborted", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("no-dev", rating=5)
        assert await tmp_store._build_reference_roast("no-dev", "test-slug") is None  # pyright: ignore[reportPrivateUsage]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_returns_none_when_every_charge_elapsed_is_null(
    tmp_store: RoastStore,
) -> None:
    """Development rows exist, but none carry a charge-elapsed clock — there
    is no usable curve point, so the reference stays unusable rather than a
    landmarks-only partial result."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        await tmp_store.create_run(
            run_id="null-clock",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await _record_row(
            tmp_store,
            "null-clock",
            1,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=None,
            bean_temp=188.0,
            dev_pct=1.0,
        )
        await _record_row(
            tmp_store,
            "null-clock",
            2,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=None,
            bean_temp=190.0,
            dev_pct=15.0,
        )
        await tmp_store.complete_run(
            run_id="null-clock", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("null-clock", rating=5)
        assert await tmp_store._build_reference_roast("null-clock", "test-slug") is None  # pyright: ignore[reportPrivateUsage]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_returns_none_for_unknown_run(tmp_store: RoastStore) -> None:
    await tmp_store.initialize()
    try:
        assert await tmp_store._build_reference_roast("ghost", "test-slug") is None  # pyright: ignore[reportPrivateUsage]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_build_reference_roast_returns_none_when_never_rated(tmp_store: RoastStore) -> None:
    """A completed run with real development telemetry but no operator
    rating yet cannot populate the non-optional ``operator_rating`` field —
    stays unusable, matching :meth:`find_reference_run`'s own floor."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        await tmp_store.create_run(
            run_id="unrated-dev",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await _record_row(
            tmp_store,
            "unrated-dev",
            1,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=612.0,
            bean_temp=188.0,
            dev_pct=1.0,
        )
        await tmp_store.complete_run(
            run_id="unrated-dev", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        assert await tmp_store._build_reference_roast("unrated-dev", "test-slug") is None  # pyright: ignore[reportPrivateUsage]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_load_reference_roast_composes_find_and_build(tmp_store: RoastStore) -> None:
    """The thin convenience wrapper chains :meth:`find_reference_run` into
    :meth:`_build_reference_roast`, and stays ``None`` when nothing qualifies."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None
        await tmp_store.create_run(
            run_id="composed",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await _record_row(
            tmp_store,
            "composed",
            1,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=612.0,
            bean_temp=188.0,
            dev_pct=1.0,
        )
        await _record_row(
            tmp_store,
            "composed",
            2,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=715.0,
            bean_temp=190.0,
            dev_pct=15.1,
        )
        await tmp_store.complete_run(
            run_id="composed", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("composed", rating=5)

        reference = await tmp_store.load_reference_roast(slug, 250.0)
        assert reference is not None
        assert reference.source_run_id == "composed"
        assert reference.origin_slug == slug

        # No qualifying reference for a bean nobody has roasted.
        assert await tmp_store.load_reference_roast("ethiopia-nope-yet", 250.0) is None
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_load_reference_roast_drops_non_finite_required_curve_samples(
    tmp_store: RoastStore,
) -> None:
    """Rows without finite required time/temperature never enter the curve."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None
        await tmp_store.create_run(
            run_id="non-finite-curve",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        rows = (
            (1, RoastPhase.ROASTING_PRE_FIRST_CRACK, 600.0, 180.0, None),
            (2, RoastPhase.ROASTING_PRE_FIRST_CRACK, 605.0, 184.0, None),
            (3, RoastPhase.DEVELOPMENT, 612.0, 188.0, 1.0),
            (4, RoastPhase.DEVELOPMENT, 715.0, 190.0, 15.1),
        )
        for tick, phase, charge_elapsed, bean_temp, dev_pct in rows:
            await _record_row(
                tmp_store,
                "non-finite-curve",
                tick,
                phase=phase,
                charge_elapsed=charge_elapsed,
                bean_temp=bean_temp,
                dev_pct=dev_pct,
            )
        await tmp_store.connection.execute(
            "UPDATE telemetry_snapshots SET bean_temp_c = ? WHERE run_id = ? AND tick = ?",
            (float("inf"), "non-finite-curve", 2),
        )
        await tmp_store.connection.execute(
            "UPDATE telemetry_snapshots SET charge_elapsed_seconds = ?"
            " WHERE run_id = ? AND tick = ?",
            (float("-inf"), "non-finite-curve", 3),
        )
        await tmp_store.connection.commit()
        await tmp_store.complete_run(
            run_id="non-finite-curve",
            outcome="completed",
            agent_phase=RoastPhase.COMPLETE,
        )
        await tmp_store.set_operator_rating("non-finite-curve", rating=5)

        reference = await tmp_store.load_reference_roast(slug, 250.0)
        assert reference is not None
        assert [(sample.t_s, sample.bean_c) for sample in reference.curve] == [
            (600.0, 180.0),
            (715.0, 190.0),
        ]
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_load_reference_roast_falls_through_an_unbuildable_top_candidate(
    tmp_store: RoastStore,
) -> None:
    """#567 Fix B (PR #574 review): the top-RATED candidate has no
    ``development``-phase telemetry (unbuildable) — ``load_reference_roast``
    must fall through to the next-best, buildable candidate rather than
    stopping at :meth:`find_reference_run`'s own top pick."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None

        await _seed_unbuildable_run(tmp_store, "top-unbuildable", profile=profile, rating=5)
        # find_reference_run itself still (correctly) reports the top-RATED id.
        assert await tmp_store.find_reference_run(slug, 250.0) == "top-unbuildable"

        await tmp_store.create_run(
            run_id="lower-buildable",
            profile=profile,
            config=AppConfig(),
            agent_phase=RoastPhase.STARTING,
        )
        await _record_row(
            tmp_store,
            "lower-buildable",
            1,
            phase=RoastPhase.DEVELOPMENT,
            charge_elapsed=612.0,
            bean_temp=188.0,
            dev_pct=1.0,
        )
        await tmp_store.complete_run(
            run_id="lower-buildable", outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        await tmp_store.set_operator_rating("lower-buildable", rating=3)

        reference = await tmp_store.load_reference_roast(slug, 250.0)
        assert reference is not None
        assert reference.source_run_id == "lower-buildable"
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_load_reference_roast_returns_none_when_every_candidate_is_unbuildable(
    tmp_store: RoastStore,
) -> None:
    """#567 Fix B (PR #574 review): every ranked candidate fails to build —
    ``load_reference_roast`` returns ``None`` rather than raising or
    returning a partially-built reference."""
    await tmp_store.initialize()
    try:
        profile = _reference_profile()
        slug = recording_origin_slug(profile)
        assert slug is not None

        await _seed_unbuildable_run(tmp_store, "unbuildable-a", profile=profile, rating=5)
        await _seed_unbuildable_run(tmp_store, "unbuildable-b", profile=profile, rating=4)

        assert await tmp_store.load_reference_roast(slug, 250.0) is None
    finally:
        await tmp_store.close()
