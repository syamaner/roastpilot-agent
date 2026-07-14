"""E6-S1: schema v1 and initialization (component plan §5, §8).

Write paths (E6-S2) and recovery reads / immutability (E6-S3) extend
this suite.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from unittest import mock

import aiosqlite as aiosqlite_module
import pytest

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
async def test_fresh_store_is_v12(tmp_store: RoastStore) -> None:
    """A brand-new store lands on the current (v12) schema version."""
    await tmp_store.initialize()
    try:
        assert await tmp_store.schema_version() == 12 == len(MIGRATIONS)
    finally:
        await tmp_store.close()


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


async def seeded_store(store: RoastStore, run_id: str = "run-1") -> RoastStore:
    await store.initialize()
    await store.create_run(
        run_id=run_id,
        profile=PROFILE,
        config=AppConfig(),
        agent_phase=RoastPhase.STARTING,
    )
    return store


async def fetch_one(store: RoastStore, sql: str) -> tuple[object, ...]:
    async with store.connection.execute(sql) as cursor:
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
        runs = await tmp_store.list_runs()
        assert len(runs) == 1
        assert runs[0].first_crack_at_utc is None
    finally:
        await tmp_store.close()


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

    These reproduce the SPA's prior client-side ``advisorSummary`` so the history
    advisor column renders identically without N+1ing ``/timeline``: ``consults``
    is every persisted decision; ``failed`` is the non-``ok`` statuses; ``clamped``
    / ``rejected`` count a consult against the safety verdict at its tick.
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
async def test_list_runs_advisor_clamp_counts_latest_verdict_per_tick(
    tmp_store: RoastStore,
) -> None:
    """Clamp/reject counts use the LATEST safety verdict at the consult's tick (#184).

    The SPA's ``advisorSummary`` joined each consult to the *last* safety
    evaluation at its tick (last-wins-by-tick). Recording two evaluations at one
    tick — an earlier ``CLAMP`` then a later ``REJECT`` — and asserting the consult
    counts as ``rejected`` (not ``clamped``) proves the projection mirrors that
    join, the failure mode an id-blind aggregation would get wrong.
    """
    await seeded_store(tmp_store)
    try:
        # Earlier verdict at tick 8 (lower id): CLAMP.
        await tmp_store.record_safety_evaluation(
            run_id="run-1",
            tick=8,
            evaluation=SafetyEvaluation(
                rule="r",
                verdict=SafetyVerdict.CLAMP,
                input_heat=120,
                input_fan=40,
                adjusted_heat=100,
                adjusted_fan=40,
                reason="earlier",
            ),
        )
        # The consult and its later verdict (higher id) at the same tick: REJECT.
        await _record_consult(tmp_store, tick=8, status="ok", verdict=SafetyVerdict.REJECT)
        runs = await tmp_store.list_runs()
        assert len(runs) == 1
        summary = runs[0]
        assert summary.advisor_consults == 1
        assert summary.advisor_clamped == 0
        assert summary.advisor_rejected == 1
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
