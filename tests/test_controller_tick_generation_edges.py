"""Generation-fence races at controller tick and deterministic-control boundaries.

Each case parks a real controller boundary, admits an emergency stop while the
old generation is suspended, then releases it.  The assertions prove the
superseded work cannot write a lever, drop beans, publish a success transition,
or reinstate an ordinary fail-safe retry.
"""

import asyncio

import pytest

from roastpilot_agent.config import ControllerConfig, SafetyLimits
from roastpilot_agent.controller import RoastController, RoastPhase
from roastpilot_agent.models import (
    AppliedRoasterState,
    DropReason,
    RoastEventKind,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyPolicy, SafetyVerdict
from tests.conftest import (
    EventSink,
    FakeClock,
    RecordingExecutor,
    RecordingSnapshotSink,
    ScriptedStateReader,
)

PROFILE = RoastProfile(
    name="generation-fence",
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
    """Build a hardware-free controller with observable boundary collaborators."""
    actual_executor = executor or RecordingExecutor()
    snapshots = RecordingSnapshotSink()
    events = EventSink()
    controller = RoastController(
        config=ControllerConfig(),
        safety=SafetyPolicy(SafetyLimits()),
        state_reader=ScriptedStateReader(readings),
        command_executor=actual_executor,
        snapshot_sink=snapshots,
        event_emitter=events,
        clock=FakeClock(),
    )
    return controller, actual_executor, snapshots, events


def _move_to_development(controller: RoastController) -> None:
    """Load a run and follow legal transitions to deterministic post-FC control."""
    controller.load_profile(PROFILE)
    for phase in (
        RoastPhase.STARTING,
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
        RoastPhase.DEVELOPMENT,
    ):
        controller.transition_to(phase)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary_name", "later_boundary_name"),
    [
        ("_apply_deterministic_post_fc_levers", "_maybe_ceiling_guard_drop"),
        ("_maybe_ceiling_guard_drop", "_maybe_deterministic_drop"),
        ("_maybe_deterministic_drop", "_maybe_run_advisory"),
    ],
)
async def test_estop_during_tick_deterministic_boundary_skips_all_later_control(
    monkeypatch: pytest.MonkeyPatch,
    boundary_name: str,
    later_boundary_name: str,
) -> None:
    """A stop admitted at each tick control boundary fences subsequent stages.

    The parked boundary models an independent awaited control operation.  The
    test deliberately uses the real tick, generation arbiter, and emergency
    path; only the selected boundary is delayed so the race order is explicit.
    """
    telemetry = RoastTelemetry.model_validate(
        {"bean_temp_c": 190.0, "env_temp_c": 210.0, "bean_ror_c_per_min": 6.0}
    )
    controller, executor, _, events = _controller(readings=[telemetry])
    _move_to_development(controller)
    entered = asyncio.Event()
    release = asyncio.Event()
    later_calls = 0

    async def parked_boundary(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await release.wait()

    async def later_boundary(*_args: object, **_kwargs: object) -> None:
        nonlocal later_calls
        later_calls += 1

    monkeypatch.setattr(controller, boundary_name, parked_boundary)
    monkeypatch.setattr(controller, later_boundary_name, later_boundary)
    ticking = asyncio.create_task(controller.tick())
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    # Earlier deterministic stages can have completed a target write before
    # this selected boundary.  Snapshot them here so the assertion below is
    # specifically about post-admission stale work rather than that valid
    # pre-boundary actuation.
    targets_before_stop = list(executor.targets)
    heat_before_stop = list(executor.heat_writes)
    fan_before_stop = list(executor.fan_writes)
    commands_before_stop = events.kinds().count(RoastEventKind.COMMAND_EXECUTED)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stopping = asyncio.create_task(
        controller.operator_emergency_stop(boundary_name, already_admitted=True)
    )
    release.set()
    await asyncio.wait_for(ticking, timeout=1.0)
    await asyncio.wait_for(stopping, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert later_calls == 0
    assert executor.targets == targets_before_stop
    assert executor.heat_writes == heat_before_stop
    assert executor.fan_writes == fan_before_stop
    assert "drop_beans" not in executor.commands
    assert events.kinds().count(RoastEventKind.COMMAND_EXECUTED) == commands_before_stop


@pytest.mark.asyncio
async def test_default_generation_prefc_write_is_fenced_after_audit_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-argument generation path cannot write targets after an e-stop."""
    telemetry = RoastTelemetry.model_validate({"bean_temp_c": 180.0, "env_temp_c": 200.0})
    controller, executor, snapshots, _ = _controller()
    controller.load_profile(PROFILE)
    controller.transition_to(RoastPhase.STARTING)
    controller.transition_to(RoastPhase.PREHEATING)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = snapshots.persist_evaluation
    evaluations = 0

    async def delayed_evaluation(value: SafetyEvaluation) -> int | None:
        nonlocal evaluations
        evaluations += 1
        if evaluations == 2:
            entered.set()
            await release.wait()
        return await original(value)

    monkeypatch.setattr(snapshots, "persist_evaluation", delayed_evaluation)
    applying = asyncio.create_task(
        controller._apply_deterministic_pre_fc_levers(telemetry)  # pyright: ignore[reportPrivateUsage]
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stopping = asyncio.create_task(
        controller.operator_emergency_stop("prefc default generation", already_admitted=True)
    )
    release.set()
    await asyncio.wait_for(applying, timeout=1.0)
    await asyncio.wait_for(stopping, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.targets == []
    assert executor.heat_writes == []
    assert executor.fan_writes == []


@pytest.mark.asyncio
async def test_estop_during_post_fc_audit_blocks_the_computed_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-FC output computed before a stop cannot reach the command executor."""
    telemetry = RoastTelemetry.model_validate(
        {"bean_temp_c": 190.0, "env_temp_c": 210.0, "bean_ror_c_per_min": 6.0}
    )
    controller, executor, snapshots, _ = _controller()
    _move_to_development(controller)
    controller._current_heat = 70  # pyright: ignore[reportPrivateUsage]
    controller._current_fan = 40  # pyright: ignore[reportPrivateUsage]
    entered = asyncio.Event()
    release = asyncio.Event()
    original = snapshots.persist_evaluation

    async def delayed_evaluation(value: SafetyEvaluation) -> int | None:
        entered.set()
        await release.wait()
        return await original(value)

    monkeypatch.setattr(snapshots, "persist_evaluation", delayed_evaluation)
    generation = controller.command_generation
    applying = asyncio.create_task(
        controller._apply_deterministic_post_fc_levers(  # pyright: ignore[reportPrivateUsage]
            telemetry, generation=generation
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stopping = asyncio.create_task(
        controller.operator_emergency_stop("post-fc audit", already_admitted=True)
    )
    release.set()
    await asyncio.wait_for(applying, timeout=1.0)
    await asyncio.wait_for(stopping, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.targets == []
    assert executor.heat_writes == []
    assert executor.fan_writes == []


@pytest.mark.asyncio
async def test_estop_after_deterministic_drop_write_blocks_cooling_success() -> None:
    """A completed but superseded deterministic drop cannot enter cooling."""

    class BlockingDropExecutor(RecordingExecutor):
        """Park the physical drop boundary after its routine admission."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def drop_beans(self) -> AppliedRoasterState | None:
            self.entered.set()
            await self.release.wait()
            return await super().drop_beans()

    executor = BlockingDropExecutor()
    controller, _, _, events = _controller(executor=executor)
    _move_to_development(controller)
    generation = controller.command_generation
    dropping = asyncio.create_task(
        controller._execute_deterministic_drop(  # pyright: ignore[reportPrivateUsage]
            DropReason.DEVELOPMENT_TARGET, generation=generation
        )
    )
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stopping = asyncio.create_task(
        controller.operator_emergency_stop("drop completion", already_admitted=True)
    )
    executor.release.set()
    assert await asyncio.wait_for(dropping, timeout=1.0) is False
    await asyncio.wait_for(stopping, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.commands == ["drop_beans"]
    assert RoastEventKind.COMMAND_EXECUTED not in events.kinds()


@pytest.mark.asyncio
async def test_estop_during_failed_failsafe_does_not_restore_ordinary_retry() -> None:
    """A stale failed write cannot overwrite the emergency-stop retry ownership."""

    class BlockingFailingExecutor(RecordingExecutor):
        """Park the ordinary fail-safe write and then fail it deterministically."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.emergency_entered = asyncio.Event()
            self.emergency_release = asyncio.Event()

        async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
            self.entered.set()
            await self.release.wait()
            raise RuntimeError("planned ordinary fail-safe failure")

        async def emergency_stop(self, *, reason: str) -> AppliedRoasterState | None:
            self.emergency_entered.set()
            await self.emergency_release.wait()
            return await super().emergency_stop(reason=reason)

    executor = BlockingFailingExecutor()
    controller, _, _, _ = _controller(executor=executor)
    evaluation = SafetyEvaluation(
        verdict=SafetyVerdict.FAULT,
        rule="test_fault",
        adjusted_heat=0,
        adjusted_fan=100,
        reason="controlled fail-safe race",
    )
    generation = controller.command_generation
    applying = asyncio.create_task(
        controller._apply_fail_safe(evaluation, generation=generation)  # pyright: ignore[reportPrivateUsage]
    )
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stopping = asyncio.create_task(
        controller.operator_emergency_stop("fail-safe race", already_admitted=True)
    )
    executor.release.set()
    await asyncio.wait_for(applying, timeout=1.0)

    # The e-stop is admitted but its hardware call remains parked.  This is
    # the moment the stale exception returns through guard 4416: if that guard
    # is removed it re-installs ``evaluation`` as an ordinary retry here.
    await asyncio.wait_for(executor.emergency_entered.wait(), timeout=1.0)
    assert controller._pending_fail_safe is None  # pyright: ignore[reportPrivateUsage]
    assert not controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]

    executor.emergency_release.set()
    await asyncio.wait_for(stopping, timeout=1.0)
    assert controller.phase is RoastPhase.FAULTED
    assert controller._pending_fail_safe is None  # pyright: ignore[reportPrivateUsage]
    assert not controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]
