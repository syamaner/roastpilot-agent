"""#303 (D45): bean-profile library store CRUD + seed + additive migration.

Hardware-free: a temp SQLite store only. Covers create/list/get/update/archive,
the seed (present + idempotent), and that the additive v4 migration leaves
``roast_runs`` untouched (corpus integrity).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from roastpilot_agent import store as store_module
from roastpilot_agent.config import AppConfig
from roastpilot_agent.models import (
    BeanProfile,
    BeanProfileDraft,
    BeanProfileInput,
    RoastPhase,
    RoastProfile,
)
from roastpilot_agent.seed import (
    COLOMBIA_HUILA_ID,
    COLOMBIA_HUILA_SEED,
    EL_SALVADOR_DIAMANTE_ID,
    EL_SALVADOR_DIAMANTE_SEED,
    ETHIOPIA_KOKE_ID,
    ETHIOPIA_KOKE_SEED,
    GUATEMALA_EL_DURAZNO_ID,
    GUATEMALA_EL_DURAZNO_SEED,
    SEED_BEAN_PROFILES,
    SUMATRA_MANDHELING_ID,
    SUMATRA_MANDHELING_SEED,
)
from roastpilot_agent.store import (
    BeanDraftAttemptAlreadyClaimedError,
    BeanDraftAttemptClaimError,
    BeanProfileNotFoundError,
    RoastStore,
)


def _input(**overrides: object) -> BeanProfileInput:
    """A valid BeanProfileInput; override per test case."""
    base: dict[str, object] = {
        "name": "Colombia washed",
        "bean_origin": "Colombia",
        "default_bean_weight_grams": 250.0,
        "initial_heat_percent": 70,
        "initial_fan_percent": 40,
        "target_drop_temp_c": 205.0,
        "target_development_percent": 20.0,
    }
    base.update(overrides)
    return BeanProfileInput.model_validate(base)


def _draft(**overrides: object) -> BeanProfileDraft:
    """A successful extracted draft with sensitive provenance fields."""
    base = _input().model_dump()
    base.update(
        {
            "source_url": "https://vendor.example/bean?token=secret",
            "field_sources": {
                "name": "on_page",
                "target_drop_temp_c": "origin_estimated",
            },
            "field_evidence": {"name": "secret evidence quote"},
            "scouting_note": "private scouting prose",
        }
    )
    base.update(overrides)
    return BeanProfileDraft.model_validate(base)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[RoastStore]:
    instance = RoastStore(tmp_path / "beans.sqlite3")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_v4_migration_creates_bean_profiles_table(store: RoastStore) -> None:
    """#303: the additive bump lands the new table + index from v4 onward.

    The V4 migration introduced ``bean_profiles``; later additive migrations
    (e.g. #308's V5 ``charge_elapsed_seconds`` column) bump the version further,
    so the assertion is ``>= 4`` (the table is present at the current schema)
    rather than pinning the now-intermediate v4."""
    assert await store.schema_version() >= 4
    async with store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ) as cursor:
        tables = {str(row[0]) for row in await cursor.fetchall()}
    assert "bean_profiles" in tables


@pytest.mark.asyncio
async def test_v14_attempt_claim_is_atomic_one_use_and_records_corrections(
    store: RoastStore,
) -> None:
    """#588: one successful attempt can correlate to exactly one explicit save."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="openai_compatible",
        model_slug="openai/gpt-5-mini",
        prompt_version="v1",
    )
    await store.finish_bean_sourcing_attempt(
        attempt_id,
        outcome="success",
        latency_ms=123,
        request_tokens=100,
        response_tokens=20,
        usage_evidence="exact",
        timed_out_runs=0,
        draft=_draft(),
    )
    saved_input = _input(name="Operator correction", source_url="https://safe.example/bean")
    second_store = RoastStore(store.db_path)
    await second_store.initialize()
    try:
        first, second = await asyncio.gather(
            store.create_bean_profile(saved_input, draft_attempt_id=attempt_id),
            second_store.create_bean_profile(saved_input, draft_attempt_id=attempt_id),
            return_exceptions=True,
        )
    finally:
        await second_store.close()
    assert isinstance(first, BeanProfile)
    assert isinstance(second, BeanProfile)
    assert first.id == second.id
    saved = first

    async with store.connection.execute(
        "SELECT saved_profile_id, changed_fields_json, draft_snapshot_json"
        " FROM bean_sourcing_attempts WHERE id = ?",
        (attempt_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row["saved_profile_id"] == saved.id
    assert json.loads(str(row["changed_fields_json"])) == ["name"]
    assert row["draft_snapshot_json"] is None
    async with store.connection.execute(
        "SELECT COUNT(*) FROM bean_profiles WHERE id = ?", (saved.id,)
    ) as cursor:
        assert (await cursor.fetchone())[0] == 1  # type: ignore[index]

    with pytest.raises(BeanDraftAttemptAlreadyClaimedError):
        await store.create_bean_profile(
            _input(name="A different replay"), draft_attempt_id=attempt_id
        )
    async with store.connection.execute("SELECT COUNT(*) FROM bean_profiles") as cursor:
        assert (await cursor.fetchone())[0] == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_v14_orderly_shutdown_clear_retains_aggregate_telemetry(store: RoastStore) -> None:
    """Orderly teardown removes unclaimed baselines without deleting metrics."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider", model_slug="model", prompt_version="v1"
    )
    await store.finish_bean_sourcing_attempt(
        attempt_id,
        outcome="success",
        latency_ms=12,
        request_tokens=3,
        response_tokens=4,
        usage_evidence="exact",
        timed_out_runs=0,
        draft=_draft(),
    )

    assert await store.clear_unclaimed_bean_sourcing_drafts() == 1
    async with store.connection.execute(
        "SELECT outcome, latency_ms, request_tokens, response_tokens,"
        " draft_snapshot_json, claim_expires_at_utc FROM bean_sourcing_attempts WHERE id = ?",
        (attempt_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert tuple(row) == ("success", 12, 3, 4, None, None)


@pytest.mark.asyncio
async def test_v14_attempt_snapshot_excludes_url_evidence_and_prose(store: RoastStore) -> None:
    """#588: bounded baseline storage never copies sensitive fetch artifacts."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="openai_compatible", model_slug="model", prompt_version="v1"
    )
    await store.finish_bean_sourcing_attempt(
        attempt_id,
        outcome="success",
        latency_ms=1,
        request_tokens=1,
        response_tokens=1,
        usage_evidence="exact",
        timed_out_runs=0,
        draft=_draft(),
    )
    async with store.connection.execute(
        "SELECT draft_snapshot_json FROM bean_sourcing_attempts WHERE id = ?",
        (attempt_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    snapshot = str(row["draft_snapshot_json"])
    assert "source_url" not in snapshot
    assert "token=secret" not in snapshot
    assert "secret evidence quote" not in snapshot
    assert "private scouting prose" not in snapshot


@pytest.mark.asyncio
async def test_v14_expiry_clears_snapshot_and_fails_claim_closed(store: RoastStore) -> None:
    """#588: expired ids retain metrics but cannot create a profile."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider",
        model_slug="model",
        prompt_version="v1",
        started_at_utc="2026-07-29T00:00:00+00:00",
    )
    await store.finish_bean_sourcing_attempt(
        attempt_id,
        outcome="success",
        latency_ms=10,
        request_tokens=None,
        response_tokens=None,
        usage_evidence="unknown",
        timed_out_runs=1,
        draft=_draft(),
        completed_at_utc="2026-07-29T00:01:00+00:00",
    )
    assert await store.expire_bean_sourcing_drafts(now_utc="2026-07-31T00:00:00+00:00") == 1
    with pytest.raises(BeanDraftAttemptClaimError):
        await store.create_bean_profile(_input(), draft_attempt_id=attempt_id)
    async with store.connection.execute(
        "SELECT outcome, timed_out_runs, usage_evidence, draft_snapshot_json"
        " FROM bean_sourcing_attempts WHERE id = ?",
        (attempt_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert tuple(row) == ("success", 1, "unknown", None)


@pytest.mark.asyncio
async def test_v14_startup_reconciles_interrupted_attempt(store: RoastStore) -> None:
    """#588: a committed admission cannot remain in-progress after restart."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider",
        model_slug="model",
        prompt_version="v1",
        owner_instance_id="dead-owner",
        started_at_utc="2026-07-31T11:00:00+00:00",
    )
    assert (
        await store.reconcile_interrupted_bean_sourcing_attempts(
            completed_at_utc="2026-07-31T12:00:00+00:00"
        )
        == 0
    )
    assert (
        await store.reconcile_interrupted_bean_sourcing_attempts(
            completed_at_utc="2026-07-31T12:01:01+00:00"
        )
        == 1
    )
    async with store.connection.execute(
        "SELECT outcome, completed_at_utc, usage_evidence FROM bean_sourcing_attempts WHERE id = ?",
        (attempt_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert tuple(row) == ("cancelled", "2026-07-31T12:01:01+00:00", "unknown")


@pytest.mark.asyncio
async def test_v14_live_peer_lease_survives_reconcile_and_renewal(store: RoastStore) -> None:
    """#588: an overlapping process cannot cancel a live peer's leased attempt."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider",
        model_slug="model",
        prompt_version="v1",
        owner_instance_id="peer-a",
        started_at_utc="2026-07-31T12:00:00+00:00",
    )
    assert (
        await store.reconcile_interrupted_bean_sourcing_attempts(
            completed_at_utc="2026-07-31T12:01:00+00:00"
        )
        == 0
    )
    assert await store.renew_bean_sourcing_attempt_lease(
        attempt_id,
        owner_instance_id="peer-a",
        renewed_at_utc="2026-07-31T12:01:00+00:00",
    )
    assert not await store.renew_bean_sourcing_attempt_lease(
        attempt_id,
        owner_instance_id="peer-b",
        renewed_at_utc="2026-07-31T12:01:00+00:00",
    )
    assert (
        await store.reconcile_interrupted_bean_sourcing_attempts(
            completed_at_utc="2026-07-31T12:02:30+00:00"
        )
        == 0
    )
    assert (
        await store.reconcile_interrupted_bean_sourcing_attempts(
            completed_at_utc="2026-07-31T12:03:01+00:00"
        )
        == 0
    )
    # The old 30-second boundary cannot cancel before a heartbeat opportunity.
    assert (
        await store.reconcile_interrupted_bean_sourcing_attempts(
            completed_at_utc="2026-07-31T12:03:31+00:00"
        )
        == 0
    )
    assert (
        await store.reconcile_interrupted_bean_sourcing_attempts(
            completed_at_utc="2026-07-31T12:04:02+00:00"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_v14_clock_jump_candidate_is_cleared_by_live_renewal(store: RoastStore) -> None:
    """#588: one jumped-clock expiry observation cannot cancel a live owner."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider",
        model_slug="model",
        prompt_version="v1",
        owner_instance_id="peer-a",
        started_at_utc="2026-07-31T12:00:00+00:00",
    )
    assert (
        await store.reconcile_interrupted_bean_sourcing_attempts(
            completed_at_utc="2026-07-31T13:00:00+00:00"
        )
        == 0
    )
    assert await store.renew_bean_sourcing_attempt_lease(
        attempt_id,
        owner_instance_id="peer-a",
        renewed_at_utc="2026-07-31T13:00:10+00:00",
    )
    assert (
        await store.reconcile_interrupted_bean_sourcing_attempts(
            completed_at_utc="2026-07-31T13:00:31+00:00"
        )
        == 0
    )
    async with store.connection.execute(
        "SELECT outcome, lease_expired_observed_at_utc FROM bean_sourcing_attempts WHERE id = ?",
        (attempt_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert tuple(row) == ("in_progress", None)


@pytest.mark.asyncio
async def test_v14_lease_renewal_rolls_back_on_lock_timeout(store: RoastStore) -> None:
    """#588: a failed heartbeat owns and rolls back its dedicated transaction."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider",
        model_slug="model",
        prompt_version="v1",
        owner_instance_id="peer-a",
    )
    await store.connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(Exception, match="database is locked"):
            await store.renew_bean_sourcing_attempt_lease(attempt_id, owner_instance_id="peer-a")
    finally:
        await store.connection.rollback()


@pytest.mark.asyncio
async def test_v14_claim_waits_for_short_writer_contention(store: RoastStore) -> None:
    """#588: the claim connection waits briefly instead of failing locked."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider", model_slug="model", prompt_version="v1"
    )
    await store.finish_bean_sourcing_attempt(
        attempt_id,
        outcome="success",
        latency_ms=1,
        request_tokens=1,
        response_tokens=1,
        usage_evidence="exact",
        timed_out_runs=0,
        draft=_draft(),
    )
    await store.connection.execute("BEGIN IMMEDIATE")
    claim = asyncio.create_task(store.create_bean_profile(_input(), draft_attempt_id=attempt_id))
    await asyncio.sleep(0.05)
    assert not claim.done()
    await store.connection.commit()
    saved = await asyncio.wait_for(claim, timeout=1.0)
    assert saved.name == _input().name


@pytest.mark.parametrize(
    "outcome",
    ["fetch_error", "extraction_error", "provider_error", "preempted", "cancelled"],
)
@pytest.mark.asyncio
async def test_v14_every_failure_outcome_is_terminal(
    store: RoastStore,
    outcome: str,
) -> None:
    """#588: every admitted non-success path remains countable."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider", model_slug="model", prompt_version="v1"
    )
    await store.finish_bean_sourcing_attempt(
        attempt_id,
        outcome=outcome,  # pyright: ignore[reportArgumentType]
        latency_ms=5,
        request_tokens=None,
        response_tokens=None,
        usage_evidence="unknown",
        timed_out_runs=0,
    )
    async with store.connection.execute(
        "SELECT outcome, completed_at_utc, draft_snapshot_json"
        " FROM bean_sourcing_attempts WHERE id = ?",
        (attempt_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row["outcome"] == outcome
    assert row["completed_at_utc"] is not None
    assert row["draft_snapshot_json"] is None


@pytest.mark.asyncio
async def test_v14_success_requires_draft_and_attempt_finalizes_once(store: RoastStore) -> None:
    """#588: invalid success rolls back and a terminal row cannot be rewritten."""
    attempt_id = await store.start_bean_sourcing_attempt(
        provider="provider", model_slug="model", prompt_version="v1"
    )
    with pytest.raises(ValueError, match="requires a draft"):
        await store.finish_bean_sourcing_attempt(
            attempt_id,
            outcome="success",
            latency_ms=1,
            request_tokens=1,
            response_tokens=1,
            usage_evidence="exact",
            timed_out_runs=0,
        )
    await store.finish_bean_sourcing_attempt(
        attempt_id,
        outcome="cancelled",
        latency_ms=1,
        request_tokens=None,
        response_tokens=None,
        usage_evidence="unknown",
        timed_out_runs=0,
    )
    with pytest.raises(RuntimeError, match="was not in progress"):
        await store.finish_bean_sourcing_attempt(
            attempt_id,
            outcome="cancelled",
            latency_ms=2,
            request_tokens=None,
            response_tokens=None,
            usage_evidence="unknown",
            timed_out_runs=0,
        )


@pytest.mark.asyncio
async def test_v14_admission_constraint_failure_rolls_back(store: RoastStore) -> None:
    """#588: an invalid admission rolls back its dedicated transaction."""
    with pytest.raises(Exception, match="NOT NULL constraint failed"):
        await store.start_bean_sourcing_attempt(
            provider=None,  # pyright: ignore[reportArgumentType]
            model_slug="model",
            prompt_version="v1",
        )
    async with store.connection.execute("SELECT COUNT(*) FROM bean_sourcing_attempts") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == 0


@pytest.mark.asyncio
async def test_v4_migration_does_not_disturb_roast_runs(store: RoastStore) -> None:
    """#303: the additive migration touches no existing roast_runs row.

    Insert a run (the corpus), then prove a bean-profile create/update/archive
    leaves that run's row byte-for-byte unchanged.
    """
    profile = RoastProfile.model_validate(
        {
            "name": "House",
            "bean_origin": "Brazil",
            "bean_weight_grams": 250.0,
            "initial_heat_percent": 70,
            "initial_fan_percent": 40,
            "target_drop_temp_c": 205.0,
            "target_development_percent": 20.0,
        }
    )
    await store.create_run(
        run_id="run-1",
        profile=profile,
        config=AppConfig(),
        agent_phase=RoastPhase.STARTING,
    )

    async def run_row() -> tuple[object, ...]:
        async with store.connection.execute(
            "SELECT * FROM roast_runs WHERE id = 'run-1'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return tuple(row)

    before = await run_row()
    bean = await store.create_bean_profile(_input())
    await store.update_bean_profile(bean.id, _input(name="renamed"))
    await store.delete_bean_profile(bean.id)
    assert await run_row() == before


@pytest.mark.asyncio
async def test_create_then_get_round_trips(store: RoastStore) -> None:
    created = await store.create_bean_profile(_input(name="Kenya AA"))
    assert created.id  # minted
    assert created.created_at == created.updated_at  # stamped together
    fetched = await store.get_bean_profile(created.id)
    assert fetched == created


@pytest.mark.asyncio
async def test_list_returns_active_name_ordered(store: RoastStore) -> None:
    await store.create_bean_profile(_input(name="Zambia"))
    await store.create_bean_profile(_input(name="Angola"))
    profiles = await store.list_bean_profiles()
    assert [p.name for p in profiles] == ["Angola", "Zambia"]


@pytest.mark.asyncio
async def test_get_unknown_returns_none(store: RoastStore) -> None:
    assert await store.get_bean_profile("nope") is None


@pytest.mark.asyncio
async def test_update_bumps_updated_at_preserves_created_and_id(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin distinct create / update instants so the bump is strictly verifiable —
    # without this, sub-microsecond execution can make the two timestamps coincide
    # and an UPDATE that silently dropped ``updated_at_utc = ?`` would still pass.
    stamps = iter(["2026-06-21T00:00:00+00:00", "2026-06-21T00:00:05+00:00"])
    monkeypatch.setattr(store_module, "_utc_now", lambda: next(stamps))
    created = await store.create_bean_profile(_input(name="Original"))
    updated = await store.update_bean_profile(
        created.id, _input(name="Edited", target_drop_temp_c=190.0)
    )
    assert updated.id == created.id
    assert updated.created_at == created.created_at
    assert updated.updated_at > created.updated_at  # strictly bumped
    assert updated.name == "Edited"
    assert updated.target_drop_temp_c == 190.0
    # The persisted row reflects both the edit and the bumped timestamp.
    fetched = await store.get_bean_profile(created.id)
    assert fetched is not None
    assert fetched.name == "Edited"
    assert fetched.updated_at == updated.updated_at


@pytest.mark.asyncio
async def test_update_unknown_raises(store: RoastStore) -> None:
    with pytest.raises(BeanProfileNotFoundError):
        await store.update_bean_profile("nope", _input())


@pytest.mark.asyncio
async def test_delete_archives_and_hides_from_list(store: RoastStore) -> None:
    created = await store.create_bean_profile(_input(name="ToArchive"))
    await store.delete_bean_profile(created.id)
    # Gone from the dropdown + a get, but the row survives (no hard delete).
    assert await store.get_bean_profile(created.id) is None
    assert created.name not in [p.name for p in await store.list_bean_profiles()]
    async with store.connection.execute(
        "SELECT archived FROM bean_profiles WHERE id = ?", (created.id,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None and int(row[0]) == 1


@pytest.mark.asyncio
async def test_delete_unknown_or_twice_raises(store: RoastStore) -> None:
    created = await store.create_bean_profile(_input())
    await store.delete_bean_profile(created.id)
    with pytest.raises(BeanProfileNotFoundError):
        await store.delete_bean_profile(created.id)  # already archived
    with pytest.raises(BeanProfileNotFoundError):
        await store.delete_bean_profile("never-existed")


@pytest.mark.asyncio
async def test_update_archived_profile_raises_not_phantom_success(store: RoastStore) -> None:
    """#304 (augment): an update racing an archive must NOT report a phantom
    success. The ``archived = 0`` UPDATE guard matches no row once the profile is
    archived, and the rowcount check raises BeanProfileNotFoundError rather than
    returning a fabricated model.

    The normal path (get_bean_profile returns None for an archived id) already
    raises via the existence guard; this pins the TOCTOU rowcount guard directly
    by faking a stale get_bean_profile read of an already-archived row.
    """
    created = await store.create_bean_profile(_input(name="Racey"))
    await store.delete_bean_profile(created.id)  # now archived

    # Simulate the TOCTOU window: the read sees the row active, but it was
    # archived before the UPDATE lands (here it is already archived, so the
    # ``archived = 0`` UPDATE matches no row).
    async def _stale_read(_profile_id: str) -> BeanProfile | None:
        return created

    monkeypatch_get = pytest.MonkeyPatch()
    monkeypatch_get.setattr(store, "get_bean_profile", _stale_read)
    try:
        with pytest.raises(BeanProfileNotFoundError):
            await store.update_bean_profile(created.id, _input(name="Edited"))
    finally:
        monkeypatch_get.undo()

    # The archived row was not mutated by the failed update.
    async with store.connection.execute(
        "SELECT profile_json FROM bean_profiles WHERE id = ?", (created.id,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert BeanProfile.model_validate_json(str(row["profile_json"])).name == "Racey"


@pytest.mark.asyncio
async def test_archive_keeps_column_and_json_timestamps_consistent(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#304 (augment): archiving does not bump updated_at_utc, so the row's column
    timestamp and the timestamp embedded in profile_json stay in agreement (both
    are the create instant — a soft-delete edits no profile content)."""
    stamps = iter(["2026-06-21T00:00:00+00:00", "2026-06-21T00:00:05+00:00"])
    monkeypatch.setattr(store_module, "_utc_now", lambda: next(stamps))
    created = await store.create_bean_profile(_input(name="Archived"))
    # Next _utc_now() would be the 00:00:05 stamp; archiving must NOT consume it
    # (it must not write a timestamp), so it stays available for any later call.
    await store.delete_bean_profile(created.id)
    async with store.connection.execute(
        "SELECT updated_at_utc, profile_json FROM bean_profiles WHERE id = ?",
        (created.id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    embedded = BeanProfile.model_validate_json(str(row["profile_json"]))
    column_ts = str(row["updated_at_utc"])
    assert column_ts == embedded.updated_at  # the two timestamps agree
    assert column_ts == created.updated_at  # neither moved on archive


@pytest.mark.asyncio
async def test_seed_inserts_then_is_idempotent(store: RoastStore) -> None:
    """#303: the seed inserts on first call, no-ops on a restart (second call)."""
    assert await store.seed_bean_profile(ETHIOPIA_KOKE_SEED) is True
    assert await store.seed_bean_profile(ETHIOPIA_KOKE_SEED) is False
    seeded = await store.get_bean_profile(ETHIOPIA_KOKE_ID)
    assert seeded == ETHIOPIA_KOKE_SEED


@pytest.mark.asyncio
async def test_seed_idempotent_does_not_clobber_an_edit(store: RoastStore) -> None:
    """#303: a re-seed after an operator edit keeps the edit (INSERT OR IGNORE)."""
    await store.seed_bean_profile(ETHIOPIA_KOKE_SEED)
    await store.update_bean_profile(ETHIOPIA_KOKE_ID, _input(name="My edited Koke"))
    assert await store.seed_bean_profile(ETHIOPIA_KOKE_SEED) is False
    kept = await store.get_bean_profile(ETHIOPIA_KOKE_ID)
    assert kept is not None and kept.name == "My edited Koke"


@pytest.mark.asyncio
async def test_source_url_round_trips_through_store(store: RoastStore) -> None:
    """#315: a bean profile's source_url survives a create → get round-trip
    (it rides along in the persisted ``profile_json``, no separate column)."""
    created = await store.create_bean_profile(
        _input(source_url="https://example.com/beans/kenya-aa")
    )
    fetched = await store.get_bean_profile(created.id)
    assert fetched is not None
    assert fetched.source_url == "https://example.com/beans/kenya-aa"


def test_ethiopia_seed_values() -> None:
    """#303: the locked seed values (design review, 21 Jun)."""
    s = ETHIOPIA_KOKE_SEED
    assert s.name == "Ethiopia Yirgacheffe Koke (Natural)"
    assert s.bean_origin == "Ethiopia"
    assert s.country == "Ethiopia"
    assert s.farm == "Koke Washing Station"
    assert s.bean_varietal == "Dega, Kudhume, Wolisho"
    assert s.bean_species == "arabica"
    assert s.is_blend is False
    assert s.processing == "natural"
    assert s.altitude_m == 1885
    # #315: the Koke product page (provenance for the corpus).
    assert s.source_url == "https://redber.co.uk/products/ethiopia-yirgacheffe-koke"
    assert s.charge_guidance_min_c == 170.0
    assert s.charge_guidance_max_c == 200.0
    assert s.initial_heat_percent == 100
    assert s.initial_fan_percent == 30
    assert s.target_drop_temp_c == 195.0  # latest acceptable drop (roast 2 ran to 196 = dark)
    assert s.target_development_percent == 13.0
    assert s.default_bean_weight_grams == 250.0


def test_colombia_huila_seed_values() -> None:
    """#134 roast-4 origin: the locked Colombia Excelso Huila (Washed) seed values."""
    s = COLOMBIA_HUILA_SEED
    assert s.id == COLOMBIA_HUILA_ID
    assert s.name == "Colombia Excelso Huila (Washed)"
    assert s.bean_origin == "Colombia"
    assert s.country == "Colombia"
    assert s.farm == "Huila (regional Excelso lot)"
    assert s.bean_varietal == "Caturra, Typica, Bourbon"
    assert s.bean_species == "arabica"
    assert s.is_blend is False
    assert s.processing == "washed"
    assert s.altitude_m == 1600
    assert (
        s.source_url
        == "https://www.redber.co.uk/products/colombia-excelso-huila-green-coffee-beans"
    )
    assert s.charge_guidance_min_c == 170.0
    assert s.charge_guidance_max_c == 200.0
    assert s.initial_heat_percent == 100
    assert s.initial_fan_percent == 30
    assert s.target_drop_temp_c == 195.0  # operator's proven known-good drop line
    assert s.target_development_percent == 16.0  # 16 (was 13) after roast 6, ~192-193C drop
    assert s.default_bean_weight_grams == 250.0  # 1 kg / 4 batches


def test_guatemala_el_durazno_seed_values() -> None:
    """The locked Guatemala El Durazno (White Honey) seed values.

    12 Jul (D88/D89 promotion): target_development_percent stepped 13.0 →
    16.0 after the 11 Jul validation roast cupped 9/10 ("like sugar") —
    the operator's read ("this origin needing a bit more") ratifies the
    step toward the ~18 % eventual, mirroring Colombia's own 13 → 16 step
    (see test_colombia_huila_seed_values above)."""
    s = GUATEMALA_EL_DURAZNO_SEED
    assert s.id == GUATEMALA_EL_DURAZNO_ID
    assert s.name == "Guatemala El Durazno (White Honey)"
    assert s.bean_origin == "Guatemala"
    assert s.country == "Guatemala"
    assert s.farm == "Finca El Durazno (Ventura family), San Pedro Pinula, Jalapa"
    assert s.bean_varietal == "Bourbon"
    assert s.bean_species == "arabica"
    assert s.is_blend is False
    assert s.processing == "honey"
    assert s.altitude_m == 1750
    assert (
        s.source_url
        == "https://www.redber.co.uk/products/guatemala-el-durazno-white-honey-process-green-coffee-beans"
    )
    assert s.charge_guidance_min_c == 170.0
    assert s.charge_guidance_max_c == 200.0
    assert s.initial_heat_percent == 100
    assert s.initial_fan_percent == 30
    assert s.target_drop_temp_c == 195.0  # operator's proven known-good drop line (bitter > 196)
    assert s.target_development_percent == 16.0  # 16 (was 13) after 11 Jul validation, 9/10 cup
    assert s.default_bean_weight_grams == 250.0


def test_el_salvador_diamante_seed_values() -> None:
    """The locked El Salvador Diamante (SHG Washed) seed values.

    Seeded 12 Jul 2026 at the post-D90 washed posture (16 % dev / 195 drop)
    rather than the 13 % first-roast de-risk: a single bag cannot ladder up
    across roasts, the 11 Jul evidence on a comparable washed bean read
    13-15 % cups as "a bit flat", and the over-roast side is already bounded
    by the drop line + the default-on 196 ceiling guard."""
    s = EL_SALVADOR_DIAMANTE_SEED
    assert s.id == EL_SALVADOR_DIAMANTE_ID
    assert s.name == "El Salvador Diamante (SHG Washed)"
    assert s.bean_origin == "El Salvador"
    assert s.country == "El Salvador"
    assert s.farm == "Sierra Apaneca-Ilamatepec; Santa Ana & Izalco volcanoes"
    assert s.bean_varietal == "Bourbon, Pacas, Catimor"
    assert s.bean_species == "arabica"
    assert s.is_blend is False
    assert s.processing == "washed"
    assert s.altitude_m == 1350
    assert (
        s.source_url == "https://www.redber.co.uk/products/el-salvador-diamante-green-coffee-beans"
    )
    assert s.charge_guidance_min_c == 170.0
    assert s.charge_guidance_max_c == 200.0
    assert s.initial_heat_percent == 100
    assert s.initial_fan_percent == 30
    assert s.target_drop_temp_c == 195.0  # proven drop line (bitter > 196, guard default-on)
    assert s.target_development_percent == 16.0  # ratified washed posture; single-bag, no ladder
    assert s.default_bean_weight_grams == 250.0


def test_sumatra_mandheling_seed_values() -> None:
    """The locked Sumatra Mandheling G1 (Wet-Hulled) seed values.

    Seeded 12 Jul 2026. Processing and altitude are supplier-unstated:
    wet_hulled is the classic Mandheling assumption (flagged in the
    description for UI correction) and 1,200 m is a representative Lake
    Toba estimate, not a datum. Drop sits at the proven 195 line.

    15 Jul (operator-approved, evidence run 43c84c98 / D95, corrected after
    PR #553 review): roast 14 landed DTR 15.1 % vs the 17 % target with an
    83 s dev window. ``target_development_percent`` steps to 19.0 so the
    drop-coherence guard floor (19 − the default 3 pp margin = 16.0) sits
    past that failed 15.1 % (18.0 would floor at 15.0 and still admit a
    repeat). ``pre_fc_heat`` stays unset: it governs the WHOLE pre-FC heat
    ramp, not a trim into the crack, so it is not the right lever here — the
    per-roast momentum lever is the late-Maillard trim depth, set via
    /config, with no seed field for it today."""
    s = SUMATRA_MANDHELING_SEED
    assert s.id == SUMATRA_MANDHELING_ID
    assert s.name == "Sumatra Mandheling G1 (Wet-Hulled)"
    assert s.bean_origin == "Indonesia (Sumatra)"
    assert s.country == "Indonesia"
    assert s.farm == "Lake Toba region smallholders (Mandailing lineage)"
    assert s.bean_varietal == "Unspecified (typical: Ateng, Tim Tim, Jember)"
    assert s.bean_species == "arabica"
    assert s.is_blend is False
    assert s.processing == "wet_hulled"
    assert s.altitude_m == 1200
    assert s.source_url == (
        "https://www.pennineteaandcoffee.co.uk/collections/green-coffee/"
        "products/sumatra-mandheling-gr1-green-coffee-beans-1kg"
    )
    assert s.charge_guidance_min_c == 170.0
    assert s.charge_guidance_max_c == 200.0
    assert s.initial_heat_percent == 100
    assert s.initial_fan_percent == 30
    assert s.pre_fc_heat is None  # momentum lever is the /config trim depth, not a seed field
    assert s.target_drop_temp_c == 195.0  # Sumatra wants the darker end of the proven range
    assert s.target_development_percent == 19.0  # guard floor 16.0 rejects roast 14's 15.1% repeat
    assert s.default_bean_weight_grams == 250.0
    # The quiet-first-crack warning is operator-facing and load-bearing.
    assert "MARK FIRST CRACK" in (s.description or "")


def test_seed_bean_profiles_collection() -> None:
    """The built-in seed set: Koke + Huila + El Durazno + Diamante + Mandheling."""
    assert SEED_BEAN_PROFILES == (
        ETHIOPIA_KOKE_SEED,
        COLOMBIA_HUILA_SEED,
        GUATEMALA_EL_DURAZNO_SEED,
        EL_SALVADOR_DIAMANTE_SEED,
        SUMATRA_MANDHELING_SEED,
    )
