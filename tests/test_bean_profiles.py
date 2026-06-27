"""#303 (D45): bean-profile library store CRUD + seed + additive migration.

Hardware-free: a temp SQLite store only. Covers create/list/get/update/archive,
the seed (present + idempotent), and that the additive v4 migration leaves
``roast_runs`` untouched (corpus integrity).
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from roastpilot_agent import store as store_module
from roastpilot_agent.config import AppConfig
from roastpilot_agent.models import BeanProfile, BeanProfileInput, RoastPhase, RoastProfile
from roastpilot_agent.seed import (
    COLOMBIA_HUILA_ID,
    COLOMBIA_HUILA_SEED,
    ETHIOPIA_KOKE_ID,
    ETHIOPIA_KOKE_SEED,
    SEED_BEAN_PROFILES,
)
from roastpilot_agent.store import BeanProfileNotFoundError, RoastStore


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
    assert s.target_development_percent == 13.0  # LIGHT first-roast de-risk guide (~10-13%)
    assert s.default_bean_weight_grams == 250.0  # 1 kg / 4 batches


def test_seed_bean_profiles_collection() -> None:
    """The built-in seed set is the Koke natural + the Colombia Huila washed."""
    assert SEED_BEAN_PROFILES == (ETHIOPIA_KOKE_SEED, COLOMBIA_HUILA_SEED)
