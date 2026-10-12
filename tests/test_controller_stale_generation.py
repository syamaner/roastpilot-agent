"""Race-focused controller tests for stale command generations.

These hardware-free tests exercise points where an emergency-stop admission can
invalidate an otherwise successful routine operation while it is awaiting an
independent boundary such as persistence or a prior MCP call.
"""

import asyncio

import pytest

from roastpilot_agent.config import ControllerConfig, SafetyLimits
from roastpilot_agent.controller import RoastController, RoastPhase
from roastpilot_agent.models import (
    AppliedRoasterState,
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
    assert len(snapshots.evaluations) == 1
    assert snapshots.evaluations[0].verdict is SafetyVerdict.EMERGENCY_STOP
    assert executor.targets == []
    assert executor.heat_writes == []
    assert executor.fan_writes == []
    assert events.kinds().count(RoastEventKind.FAULT) == 1


@pytest.mark.asyncio
async def test_estop_during_safety_audit_stops_the_rest_of_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale telemetry evaluation cannot reach phase rules or actuator writes."""
    telemetry = RoastTelemetry.model_validate({"bean_temp_c": 231.0, "env_temp_c": 200.0})
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
    assert len(executor.estop_reasons) == 1
    assert "evaluation race" in executor.estop_reasons[0]
    assert executor.targets == []
    assert executor.heat_writes == []
    assert executor.fan_writes == []


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
    assert executor.heat_writes == []
    assert executor.fan_writes == []
    assert RoastEventKind.RUN_STARTED not in events.kinds()


@pytest.mark.asyncio
async def test_estop_during_start_target_audit_cannot_apply_initial_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new session cannot apply preheat targets after its audit is superseded."""
    controller, executor, snapshots, _ = _controller()
    entered = asyncio.Event()
    release = asyncio.Event()
    original = snapshots.persist_evaluation

    async def delayed_evaluation(value: object) -> int | None:
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await original(value)  # type: ignore[arg-type]

    monkeypatch.setattr(snapshots, "persist_evaluation", delayed_evaluation)
    start = asyncio.create_task(controller.start_run(PROFILE))
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stop = asyncio.create_task(
        controller.operator_emergency_stop("start target race", already_admitted=True)
    )
    release.set()
    await asyncio.wait_for(start, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.targets == []
    assert executor.heat_writes == []
    assert executor.fan_writes == []
    assert len(executor.estop_reasons) == 1
    assert "start target race" in executor.estop_reasons[0]


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


@pytest.mark.asyncio
async def test_estop_during_telemetry_read_prevents_snapshot_and_safety_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that finishes after an e-stop admission cannot start a stale tick."""
    telemetry = RoastTelemetry.model_validate({"bean_temp_c": 180.0, "env_temp_c": 200.0})
    controller, executor, snapshots, events = _controller()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_read() -> RoastTelemetry:
        entered.set()
        await release.wait()
        return telemetry

    monkeypatch.setattr(controller._reader, "read_telemetry", delayed_read)  # pyright: ignore[reportPrivateUsage]
    tick = asyncio.create_task(controller.tick())
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stop = asyncio.create_task(
        controller.operator_emergency_stop("telemetry race", already_admitted=True)
    )
    release.set()
    await asyncio.wait_for(tick, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert snapshots.snapshots == []
    assert len(snapshots.evaluations) == 1
    assert snapshots.evaluations[0].verdict is SafetyVerdict.EMERGENCY_STOP
    assert len(executor.estop_reasons) == 1
    assert "telemetry race" in executor.estop_reasons[0]
    assert events.kinds().count(RoastEventKind.FAULT) == 1


@pytest.mark.asyncio
async def test_estop_during_latched_failsafe_retry_stops_upward_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A superseded terminal retry cannot continue into the upward-escalation read."""

    class BlockingHeatExecutor(RecordingExecutor):
        """Park the terminal heat-off retry before it returns to the tick."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def set_heat(self, heat_percent: int) -> None:
            self.entered.set()
            await self.release.wait()
            await super().set_heat(heat_percent)

    executor = BlockingHeatExecutor()
    controller, _, _, _ = _controller(executor=executor)
    controller.transition_to(RoastPhase.FAULTED)
    controller.rearm_emergency_stop_fail_safe()
    escalation_checked = False

    async def record_unexpected_recheck() -> None:
        nonlocal escalation_checked
        escalation_checked = True

    monkeypatch.setattr(controller, "_maybe_escalate_while_latched", record_unexpected_recheck)
    tick = asyncio.create_task(controller.tick())
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stop = asyncio.create_task(
        controller.operator_emergency_stop("retry race", already_admitted=True)
    )
    executor.release.set()
    await asyncio.wait_for(tick, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert not escalation_checked
    assert len(executor.estop_reasons) == 1
    assert "retry race" in executor.estop_reasons[0]


@pytest.mark.asyncio
async def test_estop_during_recovery_failsafe_cannot_commit_recovery_transition() -> None:
    """A stale recovery fail-safe cannot move a just-stopped run into recovery."""

    class BlockingHeatExecutor(RecordingExecutor):
        """Park recovery's heat-off command for an e-stop interleaving."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def set_heat(self, heat_percent: int) -> None:
            self.entered.set()
            await self.release.wait()
            await super().set_heat(heat_percent)

    executor = BlockingHeatExecutor()
    controller, _, _, events = _controller(executor=executor)
    generation = controller.command_generation
    recovery = SafetyEvaluation(
        rule="test_recovery_race",
        verdict=SafetyVerdict.RECOVERY,
        adjusted_heat=0,
        adjusted_fan=SafetyLimits().overrun_safe_fan_percent,
        reason="test recovery race",
    )
    applying = asyncio.create_task(
        controller._act_on_safety(  # pyright: ignore[reportPrivateUsage]
            recovery, generation=generation
        )
    )
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    assert controller.admit_emergency_stop() == generation + 1
    stop = asyncio.create_task(
        controller.operator_emergency_stop("recovery race", already_admitted=True)
    )
    executor.release.set()
    assert await asyncio.wait_for(applying, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert RoastEventKind.RECOVERY_REQUIRED not in events.kinds()
    assert executor.fan_writes == []
    assert len(executor.estop_reasons) == 1
    assert "recovery race" in executor.estop_reasons[0]


@pytest.mark.asyncio
async def test_estop_during_operator_first_crack_cannot_publish_development() -> None:
    """A completed stale first-crack write cannot emit or transition to development."""

    class BlockingFirstCrackExecutor(RecordingExecutor):
        """Park the operator first-crack write for an e-stop interleaving."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def mark_first_crack(self) -> None:
            self.entered.set()
            await self.release.wait()
            await super().mark_first_crack()

    executor = BlockingFirstCrackExecutor()
    controller, _, _, events = _controller(executor=executor)
    controller.transition_to(RoastPhase.STARTING)
    controller.transition_to(RoastPhase.PREHEATING)
    controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    first_crack = asyncio.create_task(controller.operator_mark_first_crack())
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stop = asyncio.create_task(
        controller.operator_emergency_stop("first-crack race", already_admitted=True)
    )
    executor.release.set()
    await asyncio.wait_for(first_crack, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.commands == ["mark_first_crack"]
    assert RoastEventKind.FIRST_CRACK not in events.kinds()


@pytest.mark.asyncio
async def test_estop_during_operator_beans_added_cannot_publish_success() -> None:
    """A completed stale beans-added write cannot emit its operator success event."""

    class BlockingBeansAddedExecutor(RecordingExecutor):
        """Park the beans-added write for an e-stop interleaving."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def mark_beans_added(self) -> None:
            self.entered.set()
            await self.release.wait()
            await super().mark_beans_added()

    executor = BlockingBeansAddedExecutor()
    controller, _, _, events = _controller(executor=executor)
    controller.transition_to(RoastPhase.STARTING)
    controller.transition_to(RoastPhase.PREHEATING)
    beans_added = asyncio.create_task(controller.operator_mark_beans_added())
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stop = asyncio.create_task(
        controller.operator_emergency_stop("beans-added race", already_admitted=True)
    )
    executor.release.set()
    await asyncio.wait_for(beans_added, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.commands == ["mark_beans_added"]
    assert RoastEventKind.COMMAND_EXECUTED not in events.kinds()


@pytest.mark.asyncio
async def test_estop_during_operator_drop_cannot_commit_cooling_transition() -> None:
    """A completed stale operator drop cannot publish success or start cooling."""

    class BlockingDropExecutor(RecordingExecutor):
        """Park the operator drop write for an e-stop interleaving."""

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
    controller.transition_to(RoastPhase.STARTING)
    controller.transition_to(RoastPhase.PREHEATING)
    controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    controller.transition_to(RoastPhase.DEVELOPMENT)
    dropping = asyncio.create_task(controller.operator_drop_beans())
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stop = asyncio.create_task(
        controller.operator_emergency_stop("drop race", already_admitted=True)
    )
    executor.release.set()
    await asyncio.wait_for(dropping, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.commands == ["drop_beans"]
    assert RoastEventKind.COMMAND_EXECUTED not in events.kinds()


@pytest.mark.asyncio
async def test_estop_during_prefc_target_audit_cannot_apply_heat_or_fan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-first-crack target audited before a stop cannot write either lever."""
    telemetry = RoastTelemetry.model_validate({"bean_temp_c": 180.0, "env_temp_c": 200.0})
    controller, executor, snapshots, _ = _controller()
    controller.load_profile(PROFILE)
    controller.transition_to(RoastPhase.STARTING)
    controller.transition_to(RoastPhase.PREHEATING)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = snapshots.persist_evaluation
    writes_recorded = 0

    async def delay_command_evaluation(value: object) -> int | None:
        nonlocal writes_recorded
        writes_recorded += 1
        if writes_recorded == 2:
            entered.set()
            await release.wait()
        return await original(value)  # type: ignore[arg-type]

    monkeypatch.setattr(snapshots, "persist_evaluation", delay_command_evaluation)
    generation = controller.command_generation
    applying = asyncio.create_task(
        controller._apply_deterministic_pre_fc_levers(  # pyright: ignore[reportPrivateUsage]
            telemetry, generation=generation
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    assert controller.admit_emergency_stop() == generation + 1
    stop = asyncio.create_task(
        controller.operator_emergency_stop("prefc target race", already_admitted=True)
    )
    release.set()
    await asyncio.wait_for(applying, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.targets == []
    assert executor.heat_writes == []
    assert executor.fan_writes == []


@pytest.mark.asyncio
async def test_estop_during_mcp_first_crack_audit_cannot_publish_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first-crack source proof that completes after a stop cannot advance phase."""
    telemetry = RoastTelemetry.model_validate(
        {"bean_temp_c": 190.0, "env_temp_c": 210.0, "first_crack_detected": True}
    )
    controller, executor, snapshots, events = _controller()
    controller.transition_to(RoastPhase.STARTING)
    controller.transition_to(RoastPhase.PREHEATING)
    controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = snapshots.persist_evaluation

    async def delayed_source(value: object) -> int | None:
        entered.set()
        await release.wait()
        return await original(value)  # type: ignore[arg-type]

    monkeypatch.setattr(snapshots, "persist_evaluation", delayed_source)
    generation = controller.command_generation
    applying = asyncio.create_task(
        controller._apply_phase_rules(  # pyright: ignore[reportPrivateUsage]
            telemetry, generation=generation
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    assert controller.admit_emergency_stop() == generation + 1
    stop = asyncio.create_task(
        controller.operator_emergency_stop("mcp first-crack race", already_admitted=True)
    )
    release.set()
    await asyncio.wait_for(applying, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.heat_writes == []
    assert executor.fan_writes == []
    assert RoastEventKind.FIRST_CRACK not in events.kinds()


@pytest.mark.asyncio
async def test_estop_during_recovery_cooling_cannot_commit_cooling_transition() -> None:
    """A completed stale cooling write cannot resume a recovering run into cooling."""

    class BlockingStartCoolingExecutor(RecordingExecutor):
        """Park the recovery cooling write for an e-stop interleaving."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def start_cooling(self) -> None:
            self.entered.set()
            await self.release.wait()
            await super().start_cooling()

    executor = BlockingStartCoolingExecutor()
    controller, _, _, events = _controller(executor=executor)
    controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    cooling = asyncio.create_task(controller.operator_start_cooling())
    await asyncio.wait_for(executor.entered.wait(), timeout=1.0)

    prior_generation = controller.command_generation
    assert controller.admit_emergency_stop() == prior_generation + 1
    stop = asyncio.create_task(
        controller.operator_emergency_stop("cooling transition race", already_admitted=True)
    )
    executor.release.set()
    await asyncio.wait_for(cooling, timeout=1.0)
    await asyncio.wait_for(stop, timeout=1.0)

    assert controller.phase is RoastPhase.FAULTED
    assert executor.commands == ["start_cooling"]
    assert RoastEventKind.COMMAND_EXECUTED not in events.kinds()
