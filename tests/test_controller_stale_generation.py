"""Race-focused controller tests for stale command generations.

These hardware-free tests exercise points where an emergency-stop admission can
invalidate an otherwise successful routine operation while it is awaiting an
independent boundary such as persistence or a prior MCP call.
"""

import asyncio

import pytest

from roastpilot_agent.config import ControllerConfig, SafetyLimits
from roastpilot_agent.controller import RoastController, RoastPhase
from roastpilot_agent.models import RoastEventKind, RoastProfile, RoastTelemetry
from roastpilot_agent.safety import SafetyPolicy
from tests.conftest import (
    EventSink,
    FakeClock,
    RecordingExecutor,
    RecordingSnapshotSink,
    ScriptedStateReader,
)

PROFILE = RoastProfile(
    name="stale-generation",
    bean_origin="Ethiopia",
    bean_weight_grams=250.0,
    initial_heat_percent=70,
    initial_fan_percent=40,
    target_drop_temp_c=205.0,
    target_development_percent=20.0,
)


def _controller(
    *,
    readings: list[RoastTelemetry | None | Exception] | None = None,
    executor: RecordingExecutor | None = None,
) -> tuple[RoastController, RecordingExecutor, RecordingSnapshotSink, EventSink]:
    """Build a minimal hardware-free controller with observable collaborators."""
    controller_executor = executor or RecordingExecutor()
    snapshots = RecordingSnapshotSink()
    events = EventSink()
    controller = RoastController(
        config=ControllerConfig(),
        safety=SafetyPolicy(SafetyLimits()),
        state_reader=ScriptedStateReader(readings),
        command_executor=controller_executor,
        snapshot_sink=snapshots,
        event_emitter=events,
        clock=FakeClock(),
    )
    return controller, controller_executor, snapshots, events


@pytest.mark.asyncio
async def test_estop_invalidates_fault_acknowledgement_close_generation() -> None:
    """An e-stop admission revokes the D212 close proof owned by its old generation."""
    controller, _, _, _ = _controller()
    generation = controller.command_generation

    assert controller.reserve_fault_acknowledgement_close(generation)
    assert controller.fault_acknowledgement_close_is_current(generation)

    controller.admit_emergency_stop()

    assert not controller.fault_acknowledgement_close_is_current(generation)
    await controller._arbiter.cancel_emergency_stop_admission()  # pyright: ignore[reportPrivateUsage]
    controller._release_emergency_stop_transition_gate_if_safe()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_estop_during_snapshot_persistence_stops_the_rest_of_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale telemetry tick cannot evaluate or issue controls after its snapshot await."""
    telemetry = RoastTelemetry.model_validate({"bean_temp_c": 180.0, "env_temp_c": 200.0})
    controller, executor, snapshots, events = _controller(readings=[telemetry])
    entered = asyncio.Event()
    release = asyncio.Event()
    original = snapshots.persist_snapshot

    async def delayed_snapshot(value: RoastTelemetry | None) -> None:
        entered.set()
        await release.wait()
        await original(value)

    monkeypatch.setattr(snapshots, "persist_snapshot", delayed_snapshot)
    tick = asyncio.create_task(controller.tick())
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    await controller.operator_emergency_stop("snapshot race")
    release.set()
    await asyncio.wait_for(tick, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert snapshots.snapshots == [telemetry]
    assert executor.targets == []
    assert events.kinds().count(RoastEventKind.FAULT) == 1


@pytest.mark.asyncio
async def test_estop_during_safety_audit_stops_the_rest_of_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale telemetry evaluation cannot reach phase rules or actuator writes."""
    telemetry = RoastTelemetry.model_validate({"bean_temp_c": 180.0, "env_temp_c": 200.0})
    controller, executor, snapshots, _ = _controller(readings=[telemetry])
    entered = asyncio.Event()
    release = asyncio.Event()
    original = snapshots.persist_evaluation

    async def delayed_evaluation(value: object) -> int | None:
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await original(value)  # type: ignore[arg-type]

    monkeypatch.setattr(snapshots, "persist_evaluation", delayed_evaluation)
    tick = asyncio.create_task(controller.tick())
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    await controller.operator_emergency_stop("evaluation race")
    release.set()
    await asyncio.wait_for(tick, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert len(snapshots.evaluations) == 2
    assert executor.targets == []


@pytest.mark.asyncio
async def test_estop_during_start_session_cannot_commit_preheat_transition() -> None:
    """A completed but superseded start write cannot publish a running roast."""

    class BlockingStartExecutor(RecordingExecutor):
        """Park the start call so an e-stop can invalidate its generation."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def start_session(
            self, *, recording_origin: str | None = None, recording_roast_num: int | None = None
        ) -> None:
            self.entered.set()
            await self.release.wait()
            await super().start_session(
                recording_origin=recording_origin, recording_roast_num=recording_roast_num
            )

    executor = BlockingStartExecutor()
    controller, _, _, events = _controller(executor=executor)
    start = asyncio.create_task(controller.start_run(PROFILE))
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    admitted_generation = controller.admit_emergency_stop()
    assert admitted_generation == prior_generation + 1
    stop = asyncio.create_task(
        controller.operator_emergency_stop("start race", already_admitted=True)
    )
    executor.release.set()
    await asyncio.wait_for(start, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.targets == []
    assert RoastEventKind.RUN_STARTED not in events.kinds()


@pytest.mark.asyncio
async def test_estop_during_faulted_stop_cooling_cannot_publish_completion() -> None:
    """A pre-admitted stop-cooling write cannot emit success after an e-stop race."""

    class BlockingCoolingExecutor(RecordingExecutor):
        """Park stop_cooling after its arbiter admission and before completion."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def stop_cooling(self) -> None:
            self.entered.set()
            await self.release.wait()
            await super().stop_cooling()

    executor = BlockingCoolingExecutor()
    controller, _, _, events = _controller(executor=executor)
    controller.transition_to(RoastPhase.FAULTED)
    stop_cooling = asyncio.create_task(controller.operator_stop_cooling())
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    admitted_generation = controller.admit_emergency_stop()
    assert admitted_generation == prior_generation + 1
    emergency = asyncio.create_task(
        controller.operator_emergency_stop("cooling race", already_admitted=True)
    )
    executor.release.set()
    await asyncio.wait_for(stop_cooling, timeout=1.0)
    await asyncio.wait_for(emergency, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.commands == ["stop_cooling"]
    assert RoastEventKind.COMMAND_EXECUTED not in events.kinds()
