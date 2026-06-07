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
async def test_recovery_read_feeds_the_controller(tmp_path: Path) -> None:
    """End to end across the E4/E6 seam: the persisted phase drives
    recover_from_restart into operator_recovery_required with zero writes."""
    from roastpilot_agent.config import ControllerConfig
    from roastpilot_agent.controller import RoastController
    from roastpilot_agent.safety import SafetyLimits, SafetyPolicy
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
