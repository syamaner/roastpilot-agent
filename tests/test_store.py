"""E6-S1: schema v1 and initialization (component plan §5, §8).

Write paths (E6-S2) and recovery reads / immutability (E6-S3) extend
this suite.
"""

from pathlib import Path
from typing import Literal

import aiosqlite as aiosqlite_module
import pytest

from roastpilot_agent import store as store_module
from roastpilot_agent.store import MIGRATIONS, RoastStore

# The full migrated table set: the nine v1 tables plus the additive
# ``bean_profiles`` table from the v4 migration (#303).
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
}

EXPECTED_INDEXES = {
    "idx_roast_events_run_kind",
    "idx_telemetry_run_tick",
    "idx_safety_run_tick",
    "idx_advisor_run_tick",
    "idx_command_run_tick",
    "idx_roast_runs_sync_status",
    "idx_bean_profiles_archived",
}


async def fetch_names(store: RoastStore, kind: str) -> set[str]:
    async with store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
    ) as cursor:
        rows = await cursor.fetchall()
    return {str(row[0]) for row in rows if not str(row[0]).startswith("sqlite_")}


@pytest.mark.asyncio
async def test_migrations_create_all_expected_tables(tmp_store: RoastStore) -> None:
    """The nine v1 tables plus the additive v4 ``bean_profiles`` table (#303)."""
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
