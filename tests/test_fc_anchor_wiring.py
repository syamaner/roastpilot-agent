"""Observe-only wiring of the D239 first-crack anchor (#1118, slice 2c)."""

from __future__ import annotations

from pathlib import Path

import pytest

from roastpilot_agent.config import ControllerConfig, FcAnchorConfig
from roastpilot_agent.fc_prediction import AnchorState, FcPredictionSnapshot
from roastpilot_agent.models import FirstCrackSource, RoastPhase, RoastTelemetry
from roastpilot_agent.store import RoastStore
from tests.test_controller import NORMAL_PATH, PROFILE, Harness, make_harness
from tests.test_store import seeded_store

OPERATOR_ONLY = ControllerConfig(first_crack_source=FirstCrackSource.OPERATOR_ONLY)


def _charged(config: ControllerConfig | None = None) -> Harness:
    harness = make_harness(readings=[], config=config or ControllerConfig())
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:  # …→ ROASTING_PRE_FIRST_CRACK
        harness.controller.transition_to(step)
    harness.controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    return harness


def _reading(
    bean: float,
    *,
    room: float | None = None,
    age: float | None = None,
    fc: bool = False,
    env: float | None = None,
) -> RoastTelemetry:
    return RoastTelemetry.model_validate(
        {
            "bean_temp_c": bean,
            "env_temp_c": bean + 50.0 if env is None else env,
            "ambient_temp_c": room,
            "ambient_age_seconds": age,
            "first_crack_detected": fc,
        }
    )


async def _ramp(harness: Harness, start: float, rate: float, seconds: int, **kw: object) -> None:
    """Tick once a second along a linear bean ramp (°C/s)."""
    for i in range(seconds):
        harness.reader.readings = [_reading(start + rate * i, **kw)]  # type: ignore[arg-type]
        await harness.controller.tick()
        harness.clock.advance(1.0)


@pytest.mark.asyncio
async def test_pre_fc_ramp_publishes_slope_eta_and_a_provisional_anchor() -> None:
    """A 0.2 °C/s ramp from 150 °C: the slope ETA and anchor follow it, and the
    planned finish is F / (1 - p) for the profile's 20 % target."""
    harness = _charged()
    await _ramp(harness, 150.0, 0.2, 40)  # ends at 157.8 °C, t = 39 s
    snap = harness.controller.snapshot().fc_prediction
    assert snap is not None
    assert snap.slope_eta_seconds == pytest.approx((176.0 - 157.8) / 0.2, rel=1e-6)
    assert snap.anchor_state is AnchorState.PROVISIONAL
    assert snap.anchor_fc_charge_seconds == pytest.approx(39.0 + 91.0, rel=1e-6)
    anchored = snap.anchor_fc_charge_seconds
    assert anchored is not None
    assert snap.planned_finish_charge_seconds == pytest.approx(anchored / 0.8)
    assert snap.model_eta_seconds is None  # no room reading: the shadow model stays silent


@pytest.mark.asyncio
@pytest.mark.parametrize(("age", "expected"), [(5.0, True), (91.0, False), (-1.0, False)])
async def test_shadow_model_runs_only_with_a_fresh_room_reading(age: float, expected: bool) -> None:
    harness = _charged()
    await _ramp(harness, 150.0, 0.2, 40, room=19.5, age=age)
    snap = harness.controller.snapshot().fc_prediction
    assert snap is not None
    assert (snap.model_eta_seconds is not None) is expected


@pytest.mark.asyncio
async def test_operator_only_shadow_detection_reanchors_when_far_from_the_anchor() -> None:
    """Under operator_only an MCP detection is still the first observed FC for
    the anchor: 60 s early against a provisional anchor, so it re-anchors."""
    harness = _charged(OPERATOR_ONLY)
    await _ramp(harness, 150.0, 0.2, 40)
    harness.reader.readings = [_reading(158.0, fc=True)]
    await harness.controller.tick()
    snap = harness.controller.snapshot().fc_prediction
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert snap is not None
    assert snap.anchor_state is AnchorState.REANCHORED
    assert snap.anchor_fc_charge_seconds == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_operator_mark_near_the_anchor_freezes_it() -> None:
    harness = _charged()
    await _ramp(harness, 165.0, 0.2, 40)  # anchor ≈ t 39 + (176-172.8)/0.2 = 55 s
    harness.clock.advance(10.0)  # mark at about 50 s: within the 30 s threshold
    await harness.controller.operator_mark_first_crack()
    snap = harness.controller.snapshot().fc_prediction
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert snap is not None
    assert snap.anchor_state is AnchorState.FROZEN
    assert snap.anchor_fc_charge_seconds == pytest.approx(55.0, abs=1.0)


@pytest.mark.asyncio
async def test_anchor_disabled_publishes_nothing_and_a_new_run_resets_it() -> None:
    off = _charged(ControllerConfig(fc_anchor=FcAnchorConfig(enabled=False)))
    await _ramp(off, 150.0, 0.2, 10)
    assert off.controller.snapshot().fc_prediction is None

    harness = _charged()
    await _ramp(harness, 150.0, 0.2, 40)
    assert harness.controller.snapshot().fc_prediction is not None
    for step in [
        RoastPhase.DEVELOPMENT,
        RoastPhase.COOLING,
        RoastPhase.COMPLETE,
        RoastPhase.IDLE,
        RoastPhase.STARTING,
        RoastPhase.PREHEATING,
    ]:
        harness.controller.transition_to(step)
    assert harness.controller.snapshot().fc_prediction is None
    tracker = harness.controller._fc_anchor_tracker  # pyright: ignore[reportPrivateUsage]
    assert tracker.anchor.state is AnchorState.NONE


async def _full_run_trace(enabled: bool, source: FirstCrackSource) -> dict[str, object]:
    """Charge → pre-FC ramp → first crack (MCP or mark) → development → drop."""
    harness = _charged(
        ControllerConfig(first_crack_source=source, fc_anchor=FcAnchorConfig(enabled=enabled))
    )
    phases: list[RoastPhase] = []
    dev: list[float | None] = []

    async def tick(reading: RoastTelemetry) -> None:
        harness.reader.readings = [reading]
        await harness.controller.tick()
        phases.append(harness.controller.phase)
        dev.append(harness.controller.snapshot().development_elapsed_seconds)
        harness.clock.advance(1.0)

    for i in range(60):
        await tick(_reading(150.0 + 0.4 * i, room=19.5, age=5.0, env=220.0))
    fc = RoastTelemetry.model_validate(
        {
            "bean_temp_c": 175.0,
            "env_temp_c": 220.0,
            "first_crack_detected": True,
            "first_crack_backdate_seconds": 15.0,
        }
    )
    await tick(fc)
    if source is FirstCrackSource.OPERATOR_ONLY:
        await harness.controller.operator_mark_first_crack()
    for i in range(40):
        await tick(_reading(176.0 + 0.6 * i, room=19.5, age=5.0, fc=True, env=220.0))
    return {
        "commands": list(harness.executor.commands),
        "targets": list(harness.executor.targets),
        "events": harness.events.kinds(),
        "phases": phases,
        "development_elapsed": dev,
        "verdicts": [(e.rule, e.verdict) for e in harness.sink.evaluations],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", list(FirstCrackSource))
async def test_anchor_is_observe_only_through_first_crack_development_and_drop(
    source: FirstCrackSource,
) -> None:
    """Safety review (#1118): with the anchor on and off, the whole run is
    identical: every roaster command and target, every event, the phase
    sequence, the development clock and every persisted verdict."""
    on = await _full_run_trace(True, source)
    off = await _full_run_trace(False, source)
    assert on == off
    assert RoastPhase.DEVELOPMENT in on["phases"]  # type: ignore[operator]
    assert "drop_beans" in on["commands"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_implausible_mcp_detection_never_takes_the_observation() -> None:
    """A below-floor operator_only detection leaves the anchor for the mark."""
    harness = _charged(OPERATOR_ONLY)
    await _ramp(harness, 150.0, 0.2, 40)
    harness.reader.readings = [_reading(120.0, fc=True)]  # false audio at 120 °C
    await harness.controller.tick()
    snap = harness.controller.snapshot().fc_prediction
    assert snap is not None
    assert snap.anchor_state is AnchorState.PROVISIONAL


@pytest.mark.asyncio
async def test_mcp_observation_uses_the_reported_crack_onset() -> None:
    harness = _charged(OPERATOR_ONLY)
    await _ramp(harness, 150.0, 0.2, 40)  # provisional anchor at 130 s
    harness.reader.readings = [
        RoastTelemetry.model_validate(
            {
                "bean_temp_c": 165.0,
                "env_temp_c": 215.0,
                "first_crack_detected": True,
                "first_crack_backdate_seconds": 20.0,
            }
        )
    ]
    await harness.controller.tick()  # received at 40 s, onset 20 s earlier
    snap = harness.controller.snapshot().fc_prediction
    assert snap is not None
    assert snap.anchor_state is AnchorState.REANCHORED
    assert snap.anchor_fc_charge_seconds == pytest.approx(20.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(("age", "expected"), [(90.0, True), (float("nan"), False)])
async def test_room_reading_age_boundaries(age: float, expected: bool) -> None:
    harness = _charged()
    await _ramp(harness, 150.0, 0.2, 40, room=19.5, age=age)
    snap = harness.controller.snapshot().fc_prediction
    assert snap is not None
    assert (snap.model_eta_seconds is not None) is expected


@pytest.mark.asyncio
async def test_store_persists_the_fc_prediction_columns(tmp_store: RoastStore) -> None:
    """V23: the five observe-only columns round-trip; a missing snapshot is NULL."""
    store = tmp_store
    try:
        await seeded_store(store)
        reading = RoastTelemetry(bean_temp_c=160.0, env_temp_c=210.0)
        snapshot = FcPredictionSnapshot(
            slope_eta_seconds=80.0,
            model_eta_seconds=95.5,
            anchor_state=AnchorState.PROVISIONAL,
            anchor_fc_charge_seconds=520.0,
            planned_finish_charge_seconds=650.0,
        )
        for tick, fc in ((1, snapshot), (2, None)):
            await store.record_telemetry(
                run_id="run-1",
                tick=tick,
                agent_phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
                elapsed_seconds=float(tick),
                interval_seconds=0.5,
                telemetry=reading,
                fc_prediction=fc,
            )
        async with store.connection.execute(
            "SELECT fc_eta_slope_seconds, fc_eta_model_seconds, fc_anchor_state,"
            " fc_anchor_charge_seconds, fc_planned_finish_charge_seconds"
            " FROM telemetry_snapshots WHERE run_id = 'run-1' ORDER BY id"
        ) as cursor:
            rows = [tuple(row) for row in await cursor.fetchall()]
        assert rows == [
            (80.0, 95.5, "provisional", 520.0, 650.0),
            (None, None, None, None, None),
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_operator_mark_far_from_a_provisional_anchor_reanchors() -> None:
    """The mark is an anchor input: 60 s after a provisional anchor it moves it."""
    harness = _charged()
    await _ramp(harness, 150.0, 0.2, 40)  # provisional anchor at 130 s, bean 157.8 °C
    harness.clock.advance(150.0)  # mark at about 190 s: 60 s past the anchor
    await harness.controller.operator_mark_first_crack()
    snap = harness.controller.snapshot().fc_prediction
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert snap is not None
    assert snap.anchor_state is AnchorState.REANCHORED
    assert snap.anchor_fc_charge_seconds == pytest.approx(190.0, abs=1.0)


@pytest.mark.asyncio
async def test_anchor_does_not_move_in_development_without_an_observation() -> None:
    """Only pre-FC ETAs feed the anchor: development entered without an
    observation (as a recovery resume would) leaves it where it was."""
    harness = _charged()
    await _ramp(harness, 150.0, 0.2, 40)
    before = harness.controller.snapshot().fc_prediction
    assert before is not None
    assert before.anchor_state is AnchorState.PROVISIONAL
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    await _ramp(harness, 158.0, 0.5, 20)  # a different rate would move a live anchor
    after = harness.controller.snapshot().fc_prediction
    assert after is not None
    assert (after.anchor_state, after.anchor_fc_charge_seconds) == (
        AnchorState.PROVISIONAL,
        before.anchor_fc_charge_seconds,
    )


def test_anchor_state_values_match_the_v23_check_constraint() -> None:
    from roastpilot_agent.store import SCHEMA_V23_FC_ANCHOR_TRACE

    for state in AnchorState:
        assert f"'{state.value}'" in SCHEMA_V23_FC_ANCHOR_TRACE


@pytest.mark.asyncio
async def test_v23_upgrade_keeps_existing_rows_null_and_checks_the_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real V22 store with telemetry upgrades with the five columns NULL on
    old rows, and the CHECK rejects an unknown anchor state."""
    import sqlite3

    from roastpilot_agent import store as store_module
    from roastpilot_agent.store import MIGRATIONS

    db = tmp_path / "v22.sqlite3"
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS[:22])
    old = RoastStore(db_path=db)
    await seeded_store(old)
    await old.connection.execute(
        "INSERT INTO telemetry_snapshots (run_id, tick, recorded_at_utc, elapsed_seconds,"
        " agent_phase, bean_temp_c, env_temp_c) VALUES"
        " ('run-1', 1, '2026-11-23T00:00:00+00:00', 1.0, 'roasting_pre_first_crack', 150, 200)"
    )
    await old.connection.commit()
    assert await old.schema_version() == 22
    await old.close()
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    upgraded = RoastStore(db_path=db)
    await upgraded.initialize()
    try:
        assert await upgraded.schema_version() == 23
        async with upgraded.connection.execute(
            "SELECT fc_eta_slope_seconds, fc_anchor_state FROM telemetry_snapshots"
        ) as cursor:
            assert [tuple(r) for r in await cursor.fetchall()] == [(None, None)]
        with pytest.raises(sqlite3.IntegrityError):
            await upgraded.connection.execute(
                "UPDATE telemetry_snapshots SET fc_anchor_state = 'bogus'"
            )
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_stale_generation_mcp_detection_does_not_observe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An e-stop admitted while the source verdict persists makes the
    detection stale: the anchor does not take it."""
    harness = _charged(OPERATOR_ONLY)
    await _ramp(harness, 150.0, 0.2, 40)
    original = harness.sink.persist_evaluation

    async def persist(evaluation: object) -> int | None:
        result = await original(evaluation)  # type: ignore[arg-type]
        if getattr(evaluation, "rule", None) == "event_source_validity":
            harness.controller.admit_emergency_stop()
        return result

    monkeypatch.setattr(harness.sink, "persist_evaluation", persist)
    harness.reader.readings = [_reading(165.0, fc=True)]
    await harness.controller.tick()
    tracker = harness.controller._fc_anchor_tracker  # pyright: ignore[reportPrivateUsage]
    assert tracker.anchor.state is AnchorState.PROVISIONAL


@pytest.mark.asyncio
async def test_negative_backdate_is_ignored() -> None:
    harness = _charged(OPERATOR_ONLY)
    await _ramp(harness, 150.0, 0.2, 40)  # provisional anchor at 130 s
    harness.reader.readings = [
        RoastTelemetry.model_validate(
            {
                "bean_temp_c": 165.0,
                "env_temp_c": 215.0,
                "first_crack_detected": True,
                "first_crack_backdate_seconds": -20.0,
            }
        )
    ]
    await harness.controller.tick()  # received at 40 s; a negative onset is ignored
    snap = harness.controller.snapshot().fc_prediction
    assert snap is not None
    assert snap.anchor_fc_charge_seconds == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_a_faulting_reading_publishes_no_stale_prediction() -> None:
    """Codex P2 on #1125: a reading that faults returns before the prediction
    step, so the snapshot carries no prediction rather than last tick's."""
    harness = _charged()
    await _ramp(harness, 150.0, 0.2, 40, env=220.0)
    assert harness.controller.snapshot().fc_prediction is not None
    harness.reader.readings = [_reading(158.0, env=400.0)]  # over the env limit
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.controller.snapshot().fc_prediction is None
