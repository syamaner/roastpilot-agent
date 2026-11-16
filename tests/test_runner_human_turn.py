"""HUMAN runner turn ordering and service lifecycle ownership witnesses."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import Mock, PropertyMock

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from roastpilot_agent.api import (
    QueuedOperatorAction,
    RoastRunConflictError,
    RoastRunner,
    RoastService,
    ShutdownHeatOffOutcome,
    _FinalisationCloseEvidence,  # pyright: ignore[reportPrivateUsage]
    create_app,
    stream_events,
)
from roastpilot_agent.config import AppConfig, ControllerConfig, MCPDeviceConfig, SafetyLimits
from roastpilot_agent.human_protection import (
    CommandDisposition,
    ContainmentAttempt,
    ContainmentOutcome,
    ContainmentReason,
    ContainmentResult,
    ControlAuthority,
    EndpointDisposition,
    HumanReadEvidence,
    ReadingFreshness,
    WriteClass,
)
from roastpilot_agent.human_run_ownership import (
    AdmissionCloseReason,
    HumanCallAdmissionFence,
    HumanCallRefusal,
    PendingHumanCall,
    RefusedHumanCall,
)
from roastpilot_agent.mcp_client import RoasterControlAdapter, RoastSessionState
from roastpilot_agent.models import (
    AppliedRoasterState,
    FaultAcknowledgementState,
    FaultControlsActionRequest,
    FaultControlsActionResult,
    FaultControlState,
    FaultCoolingStopResult,
    FaultLeaseStatus,
    OperatorAction,
    OperatorActionRequest,
    OperatorActionResult,
    RestartClearanceState,
    RoastCommand,
    RoastEventKind,
    RoastPhase,
)
from roastpilot_agent.safety import SafetyEvaluation
from roastpilot_agent.store import FaultControlCloseResult, FaultControlLease, RoastStore
from tests.conftest import FakeClock, FakeMCPClient
from tests.test_api import (
    _collect_frames,  # pyright: ignore[reportPrivateUsage]
    _FakeRequest,  # pyright: ignore[reportPrivateUsage]
    _parse_frame,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_controller import PROFILE
from tests.test_controller_human_mode import HumanRoaster
from tests.test_mcp_client import EMERGENCY_STOP_EVENT_RESULT_PAYLOAD
from tests.test_mcp_fixed_zero import (  # pyright: ignore[reportPrivateImportUsage]
    SESSION_STATE_PAYLOAD,
    _LifecycleSpy,  # pyright: ignore[reportPrivateUsage]
    _result,  # pyright: ignore[reportPrivateUsage]
    _Session,  # pyright: ignore[reportPrivateUsage]
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(name="store")
async def store(tmp_path: Path) -> AsyncIterator[RoastStore]:
    target = RoastStore(tmp_path / "human-turn.sqlite3")
    await target.initialize()
    try:
        yield target
    finally:
        await target.close()


class _RunnerRoaster(HumanRoaster):
    def __init__(self, clock: FakeClock, log: list[str]) -> None:
        super().__init__(clock, log)
        self.resets = 0
        self.last_state: RoastSessionState | None = None

    def reset_session_identity_for_new_run(self) -> None:
        self.resets += 1
        self.last_state = None

    @property
    def latest_fault_session_id(self) -> str:
        return self.session_id


def _service(
    store: RoastStore, clock: FakeClock, actuator: _RunnerRoaster, *, exact_child: bool = False
) -> RoastService:
    child = _LifecycleSpy(_Session(_result({}))) if exact_child else None
    if child is not None:
        actuator.epoch = child.child_epoch
    return RoastService(
        store,
        mcp=child,
        roaster=actuator,
        raw_state=actuator,
        authority=ControlAuthority.HUMAN,
        config=AppConfig(controller=ControllerConfig(t0_debounce_ticks=1)),
        clock=clock,
        run_loop=False,
    )


async def test_t30b_urgent_drop_precedes_ordinary_action_audit(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    log: list[str] = []
    actuator = _RunnerRoaster(clock, log)
    service = _service(store, clock, actuator)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    clock.advance(0.25)
    await runner.tick_once()
    result = await service.submit_operator_action(
        run.id, OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_RECOVERY)
    )
    assert result.queued
    original = store.record_operator_action

    async def audit(**kwargs: object) -> int:
        log.append("ordinary-action-audit")
        return await original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "record_operator_action", audit)
    clock.advance(0.25)
    actuator.charged = True
    actuator.bean = 196
    log.clear()
    await runner.tick_once()
    assert log.index("drop_beans") < log.index("ordinary-action-audit")
    assert actuator.reads == 2


async def test_td1_pending_read_blocks_start_http_build_and_recovery(store: RoastStore) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)
    first_run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    await store.complete_run(
        run_id=first_run.id, outcome="completed", agent_phase=RoastPhase.COMPLETE
    )
    runner._finalized = True  # pyright: ignore[reportPrivateUsage]
    controller.note_human_run_durably_closed()
    assert not service._human_ownership.lifecycle_blocked  # pyright: ignore[reportPrivateUsage]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_read() -> None:
        entered.set()
        await release.wait()

    actuator.collect = hold_read
    tick = asyncio.create_task(
        controller._read_human_call()  # pyright: ignore[reportPrivateUsage]
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert service._human_ownership.pending(PendingHumanCall.READ)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
            await service.start_roast(PROFILE)
        with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
            await service._build_runner("replacement-run", PROFILE)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
            await service.recover_on_start()
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://test"
        ) as http:
            response = await http.post("/api/roasts", json=PROFILE.model_dump(mode="json"))
        assert response.status_code == 409
        assert "ownership" in response.text
        assert actuator.resets == 1
        assert actuator.commands().count("start_session") == 1
        assert len((await service.history()).runs) == 1
    finally:
        release.set()
        await tick
    next_run = await service.start_roast(PROFILE)
    assert next_run.id != first_run.id
    assert actuator.resets == 2
    assert actuator.commands().count("start_session") == 2


@pytest.mark.parametrize("held_tool", ["set_recording_metadata", "start_roast_session"])
async def test_te19_pending_start_never_allows_reset_or_new_start(
    store: RoastStore, held_tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Session(_Session):
        async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
            if name == held_tool:
                entered.set()
                await release.wait()
            if name == "set_recording_metadata":
                self.calls.append((name, arguments))
                return _result({"origin": "ethiopia-harness", "roast_num": 1})
            return await super().call_tool(name, arguments)

    session = Session(_result({}))
    process = _LifecycleSpy(session)
    adapter = RoasterControlAdapter.from_process(process)
    service = RoastService(
        store,
        mcp=process,
        roaster=adapter,
        raw_state=adapter,
        authority=ControlAuthority.HUMAN,
        run_loop=False,
    )
    start = asyncio.create_task(service.start_roast(PROFILE))
    stop: asyncio.Task[OperatorActionRequest] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        runner = service.runner
        run_id = service.active_run_id
        assert runner is not None and run_id is not None
        assert runner.controller_snapshot().phase is RoastPhase.STARTING
        assert not runner.finalized
        generation = adapter._identity_generation  # pyright: ignore[reportPrivateUsage]
        assert service._loop_task is None  # pyright: ignore[reportPrivateUsage]
        assert not service._human_ownership.pending(PendingHumanCall.READ)  # pyright: ignore[reportPrivateUsage]

        async def emergency() -> OperatorActionRequest:
            request = OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
            await service.submit_operator_action(run_id, request)
            return request

        emergency_admitted = asyncio.Event()
        admit = runner.admit_emergency_stop

        def observe_admission() -> None:
            admit()
            emergency_admitted.set()

        monkeypatch.setattr(runner, "admit_emergency_stop", observe_admission)
        stop = asyncio.create_task(emergency())
        await asyncio.wait_for(emergency_admitted.wait(), 1)
        with pytest.raises(RoastRunConflictError):
            await service.start_roast(PROFILE)
        with pytest.raises(RoastRunConflictError):
            await service.recover_on_start()
        with pytest.raises(RoastRunConflictError):
            await service._build_runner("replacement", PROFILE)  # pyright: ignore[reportPrivateUsage]
        assert adapter._identity_generation == generation  # pyright: ignore[reportPrivateUsage]
        assert not runner.finalized
    finally:
        release.set()
        with suppress(Exception):
            await start
        if stop is not None:
            await stop
    assert [name for name, _ in session.calls].count("start_roast_session") == 1
    assert all(name != "get_roast_state" for name, _ in session.calls)
    assert len((await service.history()).runs) == 1


async def test_td10_unbound_read_does_not_persist_prior_mirror(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)
    await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    actuator.last_state = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    captured: list[object] = []
    original = store.record_telemetry

    async def telemetry(**kwargs: object) -> None:
        captured.append(kwargs["raw_state_json"])
        await original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "record_telemetry", telemetry)
    await runner.tick_once()
    actuator.bound = False
    clock.advance(0.25)
    await runner.tick_once()
    assert len(captured) == 2
    assert isinstance(captured[0], str)
    assert captured[1] is None
    assert actuator.last_state is not None


async def test_t30c_queued_emergency_suppresses_human_read_and_urgent_drop(
    store: RoastStore,
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)
    detail = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    clock.advance(0.25)
    await runner.tick_once()
    clock.advance(0.25)
    actuator.charged = True
    actuator.bean = 196
    service.operator_queue.put_nowait(
        QueuedOperatorAction(run_id=detail.id, action=OperatorAction.EMERGENCY_STOP, payload=None)
    )
    await runner.tick_once()
    assert actuator.reads == 1
    assert actuator.commands().count("emergency_stop") == 1
    assert actuator.commands().count("drop_beans") == 0


@pytest.mark.parametrize("ambient_validity", ["invalid", "absent", "valid"])
async def test_t51b_ambient_class_is_persisted_without_fabricating_evidence(
    store: RoastStore, ambient_validity: str
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)
    detail = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    state = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    status = state.ambient_status
    if ambient_validity == "invalid":
        status = status.model_copy(update={"temperature_c": float("nan")})
    elif ambient_validity == "absent":
        status = status.model_copy(
            update={
                "temperature_c": None,
                "humidity_percent": None,
                "pressure_hpa": None,
                "last_reading_monotonic_seconds": None,
            }
        )
    actuator.last_state = state.model_copy(update={"ambient_status": status})
    clock.advance(0.25)
    await runner.tick_once()
    async with store.connection.execute(
        "SELECT reason FROM safety_evaluations WHERE run_id = ?", (detail.id,)
    ) as cursor:
        rows = await cursor.fetchall()
    assert any(f"ambient {ambient_validity}, ambient age seconds None" in row[0] for row in rows)


async def test_t49_human_faulted_restart_retains_unknown_ownership(store: RoastStore) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    await store.create_run(
        run_id="persisted-fault",
        profile=PROFILE,
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    service = _service(store, clock, actuator)
    await service.recover_on_start()
    assert service.runner is not None
    assert service.runner.controller_snapshot().physical_intervention_required
    assert (
        service.runner.controller_snapshot().human_endpoint is EndpointDisposition.HISTORY_UNKNOWN
    )
    assert service.runner.controller_snapshot().phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
        await service.start_roast(PROFILE)
    assert actuator.commands() == []


@pytest.mark.parametrize("phase", [RoastPhase.FAULTED, RoastPhase.OPERATOR_RECOVERY_REQUIRED])
async def test_t44_recovery_drop_projection_and_admission_require_current_evidence(
    store: RoastStore, phase: RoastPhase
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    clock.advance(0.25)
    await runner.tick_once()
    actuator.charged = True
    clock.advance(0.25)
    await runner.tick_once()
    controller.transition_to(phase)
    clock.advance(0.25)
    await runner.tick_once()
    assert OperatorAction.DROP_BEANS in (await service.detail(run.id)).enabled_actions
    accepted = await service.submit_operator_action(
        run.id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert accepted.result == "accepted" and accepted.queued
    clock.advance(20)
    assert OperatorAction.DROP_BEANS not in (await service.detail(run.id)).enabled_actions
    rejected = await service.submit_operator_action(
        run.id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert rejected.result == "rejected"
    assert not rejected.queued
    assert "physical intervention" in rejected.reason
    assert actuator.commands().count("drop_beans") == 0


async def _charge_live_runner(
    service: RoastService, actuator: _RunnerRoaster, clock: FakeClock
) -> None:
    runner = service.runner
    assert runner is not None
    actuator.charged = False
    clock.advance(0.25)
    await runner.tick_once()
    actuator.charged = True
    clock.advance(0.25)
    await runner.tick_once()


@pytest.mark.parametrize("fresh", [True, False])
async def test_human_live_recovery_frame_matches_rest_without_private_fields(
    store: RoastStore, fresh: bool
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    await _charge_live_runner(service, actuator, clock)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    response = await stream_events(run.id, cast(Request, _FakeRequest()), service)
    controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    clock.advance(0.25)
    await runner.tick_once()
    if not fresh:
        clock.advance(20)
    frames = await asyncio.wait_for(_collect_frames(response, 2), 1)
    phase = _parse_frame(frames[1])
    expected = (await service.detail(run.id)).enabled_actions
    assert (OperatorAction.DROP_BEANS in expected) is fresh
    assert phase["data"] == {
        "phase": RoastPhase.OPERATOR_RECOVERY_REQUIRED.value,
        "enabled_actions": [action.value for action in expected],
    }
    async with store.connection.execute(
        "SELECT payload_json FROM roast_events WHERE run_id = ? AND kind = 'phase_changed' "
        "ORDER BY id DESC LIMIT 1",
        (run.id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert json.loads(row[0]) == {"phase": RoastPhase.OPERATOR_RECOVERY_REQUIRED.value}


@pytest.mark.parametrize("obsolete", ["different-phase", "same-phase", "foreign-run"])
async def test_human_replay_withholds_drop_for_obsolete_run_or_phase(
    store: RoastStore, obsolete: str
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    previous = await service.start_roast(PROFILE)
    await _charge_live_runner(service, actuator, clock)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    resume_from = service.events.last_event_id
    obsolete_phase = (
        RoastPhase.OPERATOR_RECOVERY_REQUIRED
        if obsolete == "different-phase"
        else RoastPhase.FAULTED
    )
    controller.transition_to(obsolete_phase)

    if obsolete == "foreign-run":
        await store.complete_run(
            run_id=previous.id, outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        runner._finalized = True  # pyright: ignore[reportPrivateUsage]
        controller.note_human_run_durably_closed()
        current = await service.start_roast(PROFILE)
        await _charge_live_runner(service, actuator, clock)
    else:
        current = previous
        if obsolete == "same-phase":
            controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)

    current_runner = service.runner
    assert current_runner is not None
    current_controller = current_runner.controller_for_human_fault_drop()
    assert current_controller is not None
    current_controller.transition_to(RoastPhase.FAULTED)
    clock.advance(0.25)
    await current_runner.tick_once()
    assert OperatorAction.DROP_BEANS in (await service.detail(current.id)).enabled_actions
    response = await stream_events(
        current.id, cast(Request, _FakeRequest(last_event_id_header=str(resume_from))), service
    )
    frames = await asyncio.wait_for(_collect_frames(response, 2), 1)
    obsolete_data = cast("dict[str, object]", _parse_frame(frames[1])["data"])
    assert obsolete_data["phase"] == obsolete_phase.value
    actions = cast("list[str]", obsolete_data["enabled_actions"])
    assert OperatorAction.DROP_BEANS.value not in actions
    assert set(obsolete_data) == {"phase", "enabled_actions"}


async def test_human_emergency_publish_never_reads_action_fence(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    await _charge_live_runner(service, actuator, clock)
    queue = service.events.subscribe()

    def forbidden(_: HumanCallAdmissionFence) -> None:
        pytest.fail("publishing a shutdown e-stop frame read the HUMAN fence")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(HumanCallAdmissionFence, "closed_reason", property(forbidden))
            assert await service.safe_shutdown_heat_off() is ShutdownHeatOffOutcome.DISPATCHED
        queued = [queue.get_nowait() for _ in range(queue.qsize())]
        phases = [event for event in queued if event.event.value == "phase_changed"]
        [fault] = phases
        projected = service.project_stream_event(run.id, fault)
        assert projected.data["enabled_actions"] == [OperatorAction.EMERGENCY_STOP.value]
    finally:
        service.events.unsubscribe(queue)


async def test_t42b_h3_continuation_does_not_allow_same_turn_levers(store: RoastStore) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    detail = await service.start_roast(PROFILE)
    await _charge_live_runner(service, actuator, clock)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    clock.advance(0.25)
    await runner.tick_once()
    accepted = await service.submit_operator_action(
        detail.id,
        OperatorActionRequest(
            action=OperatorAction.ACKNOWLEDGE_RECOVERY,
            payload={"resume_to": RoastPhase.ROASTING_PRE_FIRST_CRACK.value},
        ),
    )
    assert accepted.queued
    actuator.calls.clear()
    clock.advance(0.25)
    await runner.tick_once()
    assert controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]
    assert actuator.calls == []
    actuator.bean = 196
    clock.advance(0.25)
    await runner.tick_once()
    assert actuator.commands() == ["drop_beans"]


async def test_te16_old_and_replacement_runner_share_read_scope_and_fence(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    entered, release = asyncio.Event(), asyncio.Event()
    hold_reads = False

    class HeldSession(_Session):
        async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
            if name == "set_recording_metadata":
                self.calls.append((name, arguments))
                return _result({"origin": "synthetic", "roast_num": 1})
            if name == "get_roast_state" and hold_reads:
                entered.set()
                await release.wait()
            return await super().call_tool(name, arguments)

    session = HeldSession(_result(SESSION_STATE_PAYLOAD))
    process = _LifecycleSpy(session)
    adapter = RoasterControlAdapter.from_process(process, clock=clock)
    service = RoastService(
        store,
        mcp=process,
        roaster=adapter,
        raw_state=adapter,
        authority=ControlAuthority.HUMAN,
        clock=clock,
        run_loop=False,
    )
    first = await service.start_roast(PROFILE)
    first_runner = service.runner
    assert first_runner is not None
    first_controller = first_runner.controller_for_human_fault_drop()
    assert first_controller is not None
    await store.complete_run(run_id=first.id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    first_runner._finalized = True  # pyright: ignore[reportPrivateUsage]
    first_controller.note_human_run_durably_closed()
    await service.start_roast(PROFILE)
    second_runner = service.runner
    assert second_runner is not None and second_runner is not first_runner
    second_controller = second_runner.controller_for_human_fault_drop()
    assert second_controller is not None
    fence = service._human_admission  # pyright: ignore[reportPrivateUsage]
    assert first_controller._human_admission is fence  # pyright: ignore[reportPrivateUsage]
    assert second_controller._human_admission is fence  # pyright: ignore[reportPrivateUsage]

    before = list(session.calls)
    assert fence.closed_reason is None
    with monkeypatch.context() as synthetic_teardown:
        synthetic_teardown.setattr(process, "_stop_unconfirmed", True)
        for controller in (first_controller, second_controller):
            assert not controller._human_actuation_admitted()  # pyright: ignore[reportPrivateUsage]
            reading = await controller._read_human_call()  # pyright: ignore[reportPrivateUsage]
            assert isinstance(reading, HumanReadEvidence)
            result = await controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
                generation=controller.command_generation
            )
            assert result.outcome is ContainmentOutcome.NOT_ATTEMPTED

        async def illegal_write() -> None:
            pytest.fail("teardown-unconfirmed child admitted a HUMAN heat operation")

        assert await second_controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
            second_controller.command_generation, illegal_write, write_class=WriteClass.HEAT
        ) == (False, None)
    assert session.calls[len(before) :] == [("get_roast_state", {})] * 2
    assert not entered.is_set()
    before = list(session.calls)
    hold_reads = True

    previous_read = asyncio.create_task(
        first_controller._read_human_call()  # pyright: ignore[reportPrivateUsage]
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        refused = await second_controller._read_human_call()  # pyright: ignore[reportPrivateUsage]
        assert refused == RefusedHumanCall(
            PendingHumanCall.READ, HumanCallRefusal.READ_ALREADY_PENDING
        )
        service.close_human_call_admission(AdmissionCloseReason.APPLICATION_TEARDOWN)
    finally:
        release.set()
        await previous_read
    for controller in (first_controller, second_controller):
        refused = await controller._read_human_call()  # pyright: ignore[reportPrivateUsage]
        assert refused == RefusedHumanCall(PendingHumanCall.READ, HumanCallRefusal.ADMISSION_CLOSED)
    assert session.calls[len(before) :] == [("get_roast_state", {})]


async def test_human_open_fault_lease_projection_preserves_exact_recovery_evaluator(
    store: RoastStore,
) -> None:
    clock = FakeClock()

    class FaultRunner(_RunnerRoaster):
        async def read_fault_control_state(self, session_id: str) -> FaultControlState:
            assert session_id == self.session_id
            return FaultControlState(
                session_id=session_id,
                mcp_phase="fault",
                active=False,
                device_connected=True,
                heat_level_percent=0,
                fan_level_percent=0,
                cooling_on=False,
                beans_added=True,
                beans_dropped=False,
            )

        async def read_latest_fault_control_state(self) -> FaultControlState:
            return await self.read_fault_control_state(self.session_id)

        async def write_fault_control(
            self, action: OperatorAction, *, expected_session_id: str
        ) -> None:
            pytest.fail("projecting available actions attempted a write")

    actuator = FaultRunner(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    await _charge_live_runner(service, actuator, clock)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    controller.transition_to(RoastPhase.FAULTED)
    clock.advance(0.25)
    await runner.tick_once()
    await store.open_fault_control_lease(run_id=run.id, mcp_session_id=actuator.session_id)
    detail = await service.detail(run.id)
    assert detail.enabled_actions == [OperatorAction.EMERGENCY_STOP]
    assert detail.fault_controls is not None
    assert OperatorAction.DROP_BEANS in detail.fault_controls.enabled_actions
    clock.advance(20)
    stale = await service.detail(run.id)
    assert stale.fault_controls is not None
    assert OperatorAction.DROP_BEANS not in stale.fault_controls.enabled_actions


async def test_human_runner_without_evidence_port_refuses_before_lifecycle_reset(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actuator = FakeMCPClient()

    def reset() -> None:
        pytest.fail("runner reset the identity before admitting the HUMAN read port")

    monkeypatch.setattr(actuator, "reset_session_identity_for_new_run", reset, raising=False)
    service = RoastService(
        store, roaster=actuator, authority=ControlAuthority.HUMAN, run_loop=False
    )
    with pytest.raises(RoastRunConflictError, match="no HUMAN read capability"):
        await service._build_runner("synthetic", PROFILE)  # pyright: ignore[reportPrivateUsage]
    assert service.runner is None
    assert actuator.calls == []


async def test_unknown_human_ownership_prevents_false_clean_epoch(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    actuator = _RunnerRoaster(FakeClock(), [])
    service = RoastService(
        store,
        roaster=actuator,
        authority=ControlAuthority.HUMAN,
        live_serve_mode=True,
        run_loop=False,
    )
    service._restart_epoch_initialized = True  # pyright: ignore[reportPrivateUsage]
    service._restart_clearance_state = RestartClearanceState.CLEARED  # pyright: ignore[reportPrivateUsage]
    owner = service._human_ownership.claim_run("synthetic-unknown")  # pyright: ignore[reportPrivateUsage]
    owner.command = CommandDisposition.UNKNOWN
    owner.durably_closed = True

    async def forbidden_clearance_lookup(_: str) -> None:
        pytest.fail("UNKNOWN ownership reached the clean process epoch proof")

    monkeypatch.setattr(store, "read_process_restart_clearance", forbidden_clearance_lookup)
    await service.mark_process_epoch_clean_after_ordered_teardown()
    assert owner.command is CommandDisposition.UNKNOWN
    assert actuator.calls == []


@pytest.mark.parametrize(
    ("command", "endpoint"),
    [
        (CommandDisposition.UNKNOWN, EndpointDisposition.NONE),
        (CommandDisposition.SETTLED, EndpointDisposition.UNKNOWN),
        (CommandDisposition.SETTLED, EndpointDisposition.HISTORY_UNKNOWN),
    ],
)
async def test_lifecycle_blocks_each_uncertain_owner_after_durable_close(
    store: RoastStore,
    command: CommandDisposition,
    endpoint: EndpointDisposition,
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    first = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    await store.complete_run(run_id=first.id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    runner._finalized = True  # pyright: ignore[reportPrivateUsage]
    controller.note_human_run_durably_closed()
    owner = controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert owner is not None and owner.durably_closed
    assert not service._human_ownership.lifecycle_blocked  # pyright: ignore[reportPrivateUsage]
    assert not service._human_ownership.has_pending_call  # pyright: ignore[reportPrivateUsage]
    owner.command = command
    owner.endpoint = endpoint
    with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
        await service.start_roast(PROFILE)
    with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
        await service._build_runner("replacement", PROFILE)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
        await service.recover_on_start()
    assert actuator.resets == 1
    assert actuator.commands().count("start_session") == 1
    assert len((await service.history()).runs) == 1
    assert service.runner is runner


async def test_td8_losing_adapter_diagnostic_does_not_settle_owned_unknown(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _Session(_result({"origin": "synthetic", "roast_num": 1}))
    process = _LifecycleSpy(session)
    adapter = RoasterControlAdapter.from_process(process)
    await process.discover_fixed_zero()
    service = RoastService(
        store,
        mcp=process,
        roaster=adapter,
        raw_state=adapter,
        authority=ControlAuthority.HUMAN,
        run_loop=False,
    )
    first = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    result = await controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
        generation=controller.command_generation
    )
    assert result.outcome is ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE
    assert adapter.last_containment_result is result
    await store.complete_run(run_id=first.id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    runner._finalized = True  # pyright: ignore[reportPrivateUsage]
    controller.note_human_run_durably_closed()
    generation = adapter._identity_generation  # pyright: ignore[reportPrivateUsage]
    calls = list(session.calls)
    with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
        await service.start_roast(PROFILE)
    monkeypatch.setattr(adapter, "_last_containment_result", None)
    assert adapter.last_containment_result is None
    assert controller.snapshot().human_command_disposition is CommandDisposition.UNKNOWN
    with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
        await service.start_roast(PROFILE)
    assert len((await service.history()).runs) == 1
    assert generation == adapter._identity_generation  # pyright: ignore[reportPrivateUsage]
    assert session.calls == calls
    assert process.lifecycle == []


@pytest.mark.parametrize(
    ("replace_child", "reused_id"), [(False, False), (True, False), (False, True)]
)
async def test_td2_td3_td5_late_read_cannot_seed_the_next_started_session(
    store: RoastStore, replace_child: bool, reused_id: bool
) -> None:
    clock = FakeClock()
    entered, release = asyncio.Event(), asyncio.Event()
    hold_reads = False

    class Session(_Session):
        status = 0

        async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> object:
            if name == "set_recording_metadata":
                self.calls.append((name, arguments))
                return _result({"origin": "synthetic", "roast_num": 1})
            if name == "emergency_stop":
                self.calls.append((name, arguments))
                return _result(
                    {**EMERGENCY_STOP_EVENT_RESULT_PAYLOAD, "session_id": self.session_id}
                )
            if name != "get_roast_state":
                return await super().call_tool(name, arguments)
            self.calls.append((name, arguments))
            self.status += 1
            if hold_reads:
                entered.set()
                await release.wait()
            device = cast("dict[str, object]", SESSION_STATE_PAYLOAD["device_state"])
            return _result(
                {
                    **SESSION_STATE_PAYLOAD,
                    "session_id": self.session_id,
                    "elapsed_monotonic_seconds": float(self.status),
                    "device_state": {
                        **device,
                        "bean_temp_c": 185,
                        "raw_vendor_data": {
                            "status_packet_count": self.status,
                            "ignored_temperature_packet_count": 0,
                        },
                    },
                }
            )

    initial = Session(_result({}), session_id="session-a")
    next_id = "session-a" if reused_id else "session-b"
    replacement = Session(_result({}), session_id=next_id)

    class Process(_LifecycleSpy):
        async def start(self) -> None:
            await super().start()
            self.replace(replacement)

    process = Process(initial)
    adapter = RoasterControlAdapter.from_process(process, clock=clock)
    service = RoastService(
        store,
        mcp=process,
        roaster=adapter,
        raw_state=adapter,
        authority=ControlAuthority.HUMAN,
        clock=clock,
        run_loop=False,
    )
    first = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    for _ in range(2):
        clock.advance(0.25)
        await controller.human_urgent_stage()
    assert controller._accepted_readings.project_freshness() is ReadingFreshness.FRESH  # pyright: ignore[reportPrivateUsage]
    await store.complete_run(run_id=first.id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    runner._finalized = True  # pyright: ignore[reportPrivateUsage]
    controller.note_human_run_durably_closed()
    prior_epoch = process.child_epoch
    generation = adapter._identity_generation  # pyright: ignore[reportPrivateUsage]

    hold_reads = True
    clock.advance(0.25)
    late_read = asyncio.create_task(controller.human_urgent_stage())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
            await service.start_roast(PROFILE)
        if replace_child:
            with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
                await service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]
            assert process.lifecycle == ["stop"]
        else:
            assert process.lifecycle == []
        assert process.child_epoch == prior_epoch
        assert adapter._identity_generation == generation  # pyright: ignore[reportPrivateUsage]
        assert [name for name, _ in initial.calls].count("start_roast_session") == 1
        assert len((await service.history()).runs) == 1
    finally:
        hold_reads = False
        release.set()
        await late_read

    if replace_child:
        await service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]
        assert process.lifecycle == ["stop", "stop", "start"]
        assert process.child_epoch != prior_epoch
    else:
        initial.session_id = next_id
    second = await service.start_roast(PROFILE)
    active = service.runner
    assert active is not None and active is not runner
    current = active.controller_for_human_fault_drop()
    assert current is not None
    assert adapter.latest_fault_session_id == next_id
    clock.advance(0.25)
    await current.human_urgent_stage()
    assert current._accepted_readings.project_freshness() is ReadingFreshness.UNPROVEN  # pyright: ignore[reportPrivateUsage]
    assert await service.safe_shutdown_heat_off() is ShutdownHeatOffOutcome.DISPATCHED
    lease = service._fault_lease_cache  # pyright: ignore[reportPrivateUsage]
    assert lease.run_id == second.id
    assert lease.mcp_session_id == next_id


@pytest.mark.parametrize("failure", ["admission", "raises", "timeout"])
async def test_te5_shutdown_admission_failure_keeps_fence_terminal(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None and runner.controller_snapshot().phase is RoastPhase.PREHEATING
    attempts = 0

    def cannot_admit(_runner: object) -> None:
        nonlocal attempts
        attempts += 1
        assert service._human_admission.closed_reason is AdmissionCloseReason.APPLICATION_TEARDOWN  # pyright: ignore[reportPrivateUsage]
        raise RuntimeError("synthetic shutdown priority admission failure")

    async def failed_write(*, reason: str) -> None:
        nonlocal attempts
        attempts += 1
        assert service._human_admission.closed_reason is AdmissionCloseReason.APPLICATION_TEARDOWN  # pyright: ignore[reportPrivateUsage]
        if failure == "timeout":
            await asyncio.Future[None]()
        raise RuntimeError(reason)

    if failure == "admission":
        monkeypatch.setattr(service, "_admit_shutdown_emergency_stop", cannot_admit)
    else:
        monkeypatch.setattr(actuator, "emergency_stop", failed_write)
    actuator.calls.clear()
    assert (
        await service.safe_shutdown_heat_off(timeout_seconds=0.05)
        is ShutdownHeatOffOutcome.UNCONFIRMED
    )
    assert attempts == 2
    ticked = asyncio.Event()

    async def old_loop() -> None:
        await runner.tick_once()
        ticked.set()
        await asyncio.Future[None]()

    loop = asyncio.create_task(old_loop())
    monkeypatch.setattr(service, "_loop_task", loop)
    try:
        await asyncio.wait_for(ticked.wait(), 1)
    finally:
        await service.shutdown()
    assert loop.cancelled()
    assert actuator.commands() == ([] if failure == "admission" else ["set_heat", "set_fan"])
    if failure != "admission":
        assert actuator.calls[0] == ("set_heat", {"heat_percent": 0})
    assert actuator.reads == actuator.containments == 0
    assert service._human_admission.closed_reason is AdmissionCloseReason.APPLICATION_TEARDOWN  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("stop_unconfirmed", [False, True])
async def test_te12_containment_refusal_preserves_attempt_and_adapter_diagnostic(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, stop_unconfirmed: bool
) -> None:
    session = _Session(_result({"origin": "synthetic", "roast_num": 1}))
    process = _LifecycleSpy(session)
    adapter = RoasterControlAdapter.from_process(process)
    service = RoastService(
        store,
        mcp=process,
        roaster=adapter,
        raw_state=adapter,
        authority=ControlAuthority.HUMAN,
        run_loop=False,
    )
    await process.discover_fixed_zero()
    await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    owner = controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    prior_diagnostic = ContainmentResult(
        ContainmentOutcome.REFUSED_BUSY, ContainmentReason.COMMAND_IN_PROGRESS
    )
    monkeypatch.setattr(adapter, "_last_containment_result", prior_diagnostic)
    if stop_unconfirmed:
        monkeypatch.setattr(process, "_stop_unconfirmed", True)
    else:
        service.close_human_call_admission(AdmissionCloseReason.APPLICATION_TEARDOWN)
    before = list(session.calls)
    outcome = await controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
        generation=controller.command_generation
    )
    assert outcome.outcome is ContainmentOutcome.NOT_ATTEMPTED
    assert owner.containment is ContainmentAttempt.AVAILABLE
    assert owner.command is CommandDisposition.SETTLED
    assert adapter.last_containment_result is prior_diagnostic
    assert not service._human_ownership.has_pending_call  # pyright: ignore[reportPrivateUsage]
    assert session.calls == before


async def test_t32_queued_operator_drop_joins_same_turn_protection(
    store: RoastStore,
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None

    clock.advance(0.25)
    await runner.tick_once()
    actuator.charged = True
    actuator.bean = 185
    clock.advance(0.25)
    await runner.tick_once()
    assert runner.controller_snapshot().phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    response = await service.submit_operator_action(
        run.id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert response.queued
    actuator.calls.clear()
    actuator.bean = 196
    clock.advance(0.25)
    await runner.tick_once()
    assert runner.controller_snapshot().human_endpoint is EndpointDisposition.ISSUED
    clock.advance(0.25)
    await runner.tick_once()
    assert actuator.commands().count("drop_beans") == 1
    timeline = await store.read_timeline(run.id)
    outcomes = [
        event.payload.get("outcome")
        for event in timeline.events
        if event.payload is not None and event.payload.get("command") == "drop_beans"
    ]
    assert "joined_protective_endpoint" in outcomes


@pytest.mark.parametrize("uncertainty", ["drop", "containment"])
async def test_finalisation_preserves_unknown_ownership_at_http_and_respawn(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, uncertainty: str
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    process = _LifecycleSpy(_Session(_result({})))
    await process.discover_fixed_zero()
    actuator.epoch = process.child_epoch
    service = RoastService(
        store,
        mcp=process,
        roaster=actuator,
        raw_state=actuator,
        authority=ControlAuthority.HUMAN,
        config=AppConfig(controller=ControllerConfig(t0_debounce_ticks=1)),
        clock=clock,
        run_loop=False,
    )
    first = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    owner = controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert owner is not None

    async def unknown_drop() -> None:
        actuator.calls.append(("drop_beans", {}))

    async def unknown_containment() -> ContainmentResult:
        return ContainmentResult(ContainmentOutcome.UNKNOWN_TRANSPORT)

    clock.advance(0.25)
    await runner.tick_once()
    actuator.charged = True
    actuator.bean = 185
    clock.advance(0.25)
    await runner.tick_once()
    if uncertainty == "drop":
        monkeypatch.setattr(actuator, "drop_beans", unknown_drop)
        actuator.bean = 196
    else:
        actuator.contain = unknown_containment
        actuator.bound = False
    clock.advance(0.25)
    await runner.tick_once()
    assert controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert owner.command is CommandDisposition.UNKNOWN
    assert owner.endpoint is (
        EndpointDisposition.UNKNOWN if uncertainty == "drop" else EndpointDisposition.NONE
    )
    assert not owner.durably_closed
    assert not runner.finalized

    async with AsyncClient(
        transport=ASGITransport(app=create_app(service)), base_url="http://test"
    ) as http:
        response = await http.post(
            f"/api/roasts/{first.id}/operator-actions",
            json={"action": "acknowledge_recovery", "payload": {"resume_to": "complete"}},
        )
        assert response.status_code == 200
        assert response.json()["queued"] is True
        clock.advance(0.25)
        assert await runner.tick_once()
        assert runner.finalized and owner.durably_closed
        completed = await store.read_run(first.id)
        assert completed is not None and completed.completed_at_utc is not None
        assert completed.agent_phase is RoastPhase.COMPLETE
        assert owner.command is CommandDisposition.UNKNOWN
        assert not service._human_ownership.has_pending_call  # pyright: ignore[reportPrivateUsage]
        response = await http.post("/api/roasts", json=PROFILE.model_dump(mode="json"))
        assert response.status_code == 409
        assert "HUMAN" in response.json()["detail"]

    with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
        await service._respawn_mcp_for_device_config(MCPDeviceConfig())  # pyright: ignore[reportPrivateUsage]
    assert owner.command is CommandDisposition.UNKNOWN
    assert owner.containment is (
        ContainmentAttempt.CONSUMED
        if uncertainty == "containment"
        else ContainmentAttempt.AVAILABLE
    )
    assert process.lifecycle == ["stop"]
    assert actuator.resets == 1
    assert actuator.commands().count("start_session") == 1
    assert (await store.read_run(first.id)) == completed
    assert len((await service.history()).runs) == 1


async def test_t51_stale_read_persists_conservative_age_in_safety_evaluation(
    store: RoastStore,
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    clock.advance(0.25)
    await runner.tick_once()
    actuator.charged = True
    actuator.bean = 185
    clock.advance(0.25)
    await runner.tick_once()

    async def delay_response() -> None:
        clock.advance(20)

    actuator.collect = delay_response
    actuator.bean = 196
    clock.advance(0.25)
    await runner.tick_once()
    assert runner.controller_snapshot().phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert "drop_beans" not in actuator.commands()
    async with store.connection.execute(
        "SELECT reason FROM safety_evaluations WHERE run_id = ?", (run.id,)
    ) as cursor:
        reasons = [row[0] for row in await cursor.fetchall()]
    assert any(
        "BT valid, ET valid, accepted reading stale, conservative age seconds 20.25" in reason
        for reason in reasons
    )


class _ExactFaultRunner(_RunnerRoaster):
    """Synthetic exact-session fault port with separately controlled readback."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock, [])
        self.set_latest_fault_session_id(self.session_id)
        self.fault_writes: list[OperatorAction] = []
        self.issued_readback = "confirmed"
        self.after_drop_read: asyncio.Event | None = None
        self.release_drop_read: asyncio.Event | None = None
        self.cooling = False
        self.fault_reads: list[str] = []
        self.reject_unbound_inactive_emergency = False
        self.exact_emergency_write_raises = False
        self.exact_emergency_read_raises = False
        self.exact_emergency_read_heat = 0
        self.latest_fault_session_override: str | None = None

    @property
    def latest_fault_session_id(self) -> str:
        return self.latest_fault_session_override or super().latest_fault_session_id

    async def emergency_stop(self, *, reason: str) -> AppliedRoasterState | None:
        if self.reject_unbound_inactive_emergency:
            # This port models an inactive, stopped fault session.
            raise RuntimeError("No active roast session exists.")
        return await super().emergency_stop(reason=reason)

    async def read_fault_acknowledgement_state(self, session_id: str) -> FaultAcknowledgementState:
        state = await super().read_fault_acknowledgement_state(session_id)
        return state.model_copy(update={"cooling_on": self.cooling})

    async def read_fault_control_state(self, session_id: str) -> FaultControlState:
        assert session_id == self.session_id
        self.fault_reads.append(session_id)
        emergency_written = OperatorAction.EMERGENCY_STOP in self.fault_writes
        if emergency_written and self.exact_emergency_read_raises:
            raise RuntimeError("synthetic emergency readback failure")
        issued = OperatorAction.DROP_BEANS in self.fault_writes
        outcome = self.issued_readback if issued else "before"
        if issued and self.after_drop_read is not None:
            assert self.release_drop_read is not None
            self.after_drop_read.set()
            await self.release_drop_read.wait()
        if outcome == "timeout":
            raise TimeoutError("synthetic absent readback")
        if outcome == "error":
            raise RuntimeError("synthetic failed readback")
        state = FaultControlState(
            session_id=session_id,
            mcp_phase="fault",
            active=False,
            device_connected=outcome != "contradiction_disconnected",
            heat_level_percent=self.exact_emergency_read_heat if emergency_written else 0,
            fan_level_percent=0,
            cooling_on=self.cooling,
            beans_added=True,
            beans_dropped=outcome in {"confirmed", "nonconfirming"},
        )
        if outcome in {"contradiction_nonfault", "nonconfirming"}:
            return state.model_copy(update={"mcp_phase": "cooling"})
        return state

    async def read_latest_fault_control_state(self) -> FaultControlState:
        return await self.read_fault_control_state(self.session_id)

    async def write_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> None:
        assert expected_session_id == self.session_id
        self.fault_writes.append(action)
        if action is OperatorAction.EMERGENCY_STOP and self.exact_emergency_write_raises:
            raise RuntimeError("synthetic exact e-stop failure")
        if action in (OperatorAction.START_COOLING, OperatorAction.STOP_COOLING):
            self.cooling = action is OperatorAction.START_COOLING


async def _faulted_run(
    store: RoastStore, *, open_lease: bool = True
) -> tuple[RoastService, RoastRunner, _ExactFaultRunner, str, FaultControlLease | None]:
    clock = FakeClock()
    actuator = _ExactFaultRunner(clock)
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    await _charge_live_runner(service, actuator, clock)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    controller.transition_to(RoastPhase.FAULTED)
    clock.advance(0.25)
    await runner.tick_once()
    assert controller.human_recovery_drop_allowed(session_id=actuator.session_id)
    lease = (
        await store.open_fault_control_lease(run_id=run.id, mcp_session_id=actuator.session_id)
        if open_lease
        else None
    )
    actuator.calls.clear()
    return service, runner, actuator, run.id, lease


@pytest.mark.parametrize(
    ("readback", "expected", "confirmation"),
    [
        ("confirmed", EndpointDisposition.ISSUED, "read_back"),
        ("contradiction", EndpointDisposition.UNKNOWN, "rejected_readback"),
        ("contradiction_nonfault", EndpointDisposition.UNKNOWN, "rejected_readback"),
        ("contradiction_disconnected", EndpointDisposition.UNKNOWN, "rejected_readback"),
        ("nonconfirming", EndpointDisposition.ISSUED, "rejected_readback"),
        ("timeout", EndpointDisposition.ISSUED, "readback_failed"),
        ("error", EndpointDisposition.ISSUED, "readback_failed"),
    ],
)
async def test_tf2_tf8_exact_human_drop_classifies_the_returned_fault_state(
    store: RoastStore, readback: str, expected: EndpointDisposition, confirmation: str
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    actuator.issued_readback = readback
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    response = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert response.result == ("accepted" if readback == "confirmed" else "failed")
    assert response.reason == (
        "fault action accepted through exact-session controller dispatch"
        if readback == "confirmed"
        else "exact fault-session action was not confirmed"
    )
    assert actuator.fault_writes == [OperatorAction.DROP_BEANS]
    assert actuator.fault_reads == [actuator.session_id] * 2
    assert controller.phase is RoastPhase.FAULTED
    assert controller.snapshot().human_endpoint is expected
    assert controller.snapshot().human_command_disposition is (
        CommandDisposition.UNKNOWN
        if expected is EndpointDisposition.UNKNOWN
        else CommandDisposition.SETTLED
    )
    assert not controller.has_pending_fault_drop_readback()
    assert await store.read_fault_control_lease() == lease
    assert actuator.commands() == []
    executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
    assert executor is not None
    assert await executor.settle_for_shutdown()
    await runner.tick_once()
    timeline = await store.read_timeline(run_id)
    assert any(
        entry.tool is RoastCommand.DROP_BEANS
        and entry.result is not None
        and entry.result.get("confirmation") == confirmation
        for entry in timeline.commands
    )
    assert (
        sum(
            event.kind is RoastEventKind.COMMAND_EXECUTED
            and event.payload is not None
            and event.payload.get("command") == "drop_beans"
            for event in timeline.events
        )
        == 1
    )


@pytest.mark.parametrize(
    ("route", "settlement"),
    [
        ("d212", "success"),
        ("complete", "success"),
        ("legacy", "success"),
        ("d212", "hook_cancelled"),
        ("legacy", "runner_cancelled"),
        ("d212", "task_cancelled"),
        ("d212", "commit_raised"),
        ("complete", "commit_raised"),
        ("legacy", "commit_raised"),
    ],
)
async def test_tf20_tf22_completion_persistence_waits_for_owned_drop_evaluation(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, route: str, settlement: str
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store, open_lease=route == "d212")
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    held, release, barrier = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order: list[str] = []
    original_record = store.record_safety_evaluation
    original_settle = controller.settle_human_drop_evaluations
    original_complete = store.complete_run
    original_close = store.close_fault_control_lease_atomically
    original_commit = store.connection.commit
    evaluation_task: asyncio.Task[object] | None = None

    async def commit() -> None:
        if settlement == "commit_raised" and asyncio.current_task() is evaluation_task:
            order.append("evaluation INSERT reached COMMIT")
            raise RuntimeError("synthetic evaluation COMMIT failure")
        await original_commit()

    async def record(*, run_id: str, tick: int, evaluation: SafetyEvaluation) -> int:
        target = evaluation.rule == "human_recovery_drop"
        if target:
            nonlocal evaluation_task
            evaluation_task = asyncio.current_task()
            held.set()
            await release.wait()
        row = await original_record(run_id=run_id, tick=tick, evaluation=evaluation)
        if target:
            order.append("evaluation committed")
        return row

    async def settle() -> bool:
        assert controller.human_drop_closed()
        barrier.set()
        return await original_settle()

    async def finish(**kwargs: object) -> None:
        order.append("complete_run")
        await original_complete(**kwargs)  # type: ignore[arg-type]

    async def close(**kwargs: object) -> FaultControlCloseResult:
        order.append("D212 close")
        return await original_close(**kwargs)  # type: ignore[arg-type]

    async def export() -> None:
        order.append("export")

    async def impossible_write() -> None:
        pytest.fail("DROP evaluation completed after admission closed")

    monkeypatch.setattr(store.connection, "commit", commit)
    monkeypatch.setattr(store, "record_safety_evaluation", record)
    monkeypatch.setattr(store, "complete_run", finish)
    monkeypatch.setattr(store, "close_fault_control_lease_atomically", close)
    monkeypatch.setattr(runner, "_export_logs", export)
    monkeypatch.setattr(controller, "settle_human_drop_evaluations", settle)
    drop = asyncio.create_task(
        controller.execute_human_fault_drop(
            actuator.session_id,
            generation=controller.command_generation,
            lease_current=lambda: True,
            operation=impossible_write,
        )
    )
    try:
        await asyncio.wait_for(held.wait(), 1)
        assert evaluation_task is not None
        if settlement == "hook_cancelled":
            drop.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drop
            assert not evaluation_task.done()
        if route == "complete":
            # Reach COMPLETE through the normal phase sequence without
            # dispatching a second drop into the held evaluation.
            monkeypatch.setattr(controller, "_phase", RoastPhase.COOLING)
            controller.transition_to(RoastPhase.COMPLETE)
        else:
            action = (
                OperatorAction.ACKNOWLEDGE_FAULT
                if lease is None
                else OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE
            )
            payload = (
                None
                if lease is None
                else {"confirmation": True, "expected_generation": lease.generation}
            )
            admitted = await service.submit_operator_action(
                run_id, OperatorActionRequest(action=action, payload=payload)
            )
            assert admitted.result == "accepted" and admitted.queued
        completion = asyncio.create_task(runner.tick_once())
        completed = False
        try:
            await asyncio.wait_for(barrier.wait(), 1)
            assert controller.human_drop_closed()
            assert not completion.done()
            assert order == []
            detail = await store.read_run(run_id)
            assert detail is not None and detail.completed_at_utc is None
        finally:
            if settlement == "runner_cancelled":
                completion.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await completion
                assert not evaluation_task.done()
            if settlement == "task_cancelled":
                evaluation_task.cancel()
            release.set()
            if settlement != "runner_cancelled":
                completed = await completion
        if settlement != "runner_cancelled":
            assert completed is (settlement in {"success", "hook_cancelled"})
        if settlement in {"success", "runner_cancelled"}:
            assert not await drop
        elif settlement == "commit_raised":
            with pytest.raises(RuntimeError, match="synthetic evaluation COMMIT failure"):
                await drop
        elif settlement == "task_cancelled":
            with pytest.raises(asyncio.CancelledError):
                await drop
        assert controller.human_drop_closed()
        assert controller.snapshot().human_endpoint is EndpointDisposition.NONE
        assert not actuator.fault_writes
        if settlement in {"success", "hook_cancelled"}:
            assert (
                order
                == {
                    "d212": ["evaluation committed", "D212 close"],
                    "complete": ["evaluation committed", "export", "complete_run"],
                    "legacy": ["evaluation committed", "complete_run"],
                }[route]
            )
        elif settlement in {"commit_raised", "task_cancelled"}:
            if route == "d212":
                assert lease is not None
                request = OperatorActionRequest(
                    action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
                    payload={"confirmation": True, "expected_generation": lease.generation},
                )
                assert (await service.submit_operator_action(run_id, request)).queued
            assert not await runner.tick_once()
            detail = await store.read_run(run_id)
            assert detail is not None and detail.completed_at_utc is None
            assert controller.human_drop_closed()
            assert order == (
                ["evaluation INSERT reached COMMIT"] if settlement == "commit_raised" else []
            )
        else:
            assert order == ["evaluation committed"]
    finally:
        release.set()
        await asyncio.gather(drop, return_exceptions=True)


@pytest.mark.parametrize("readback", ["confirmed", "contradiction"])
async def test_tf6_tf9_pending_readback_blocks_lifecycle_even_after_d212(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, readback: str
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    actuator.after_drop_read, actuator.release_drop_read = asyncio.Event(), asyncio.Event()
    actuator.issued_readback = readback
    calls: list[FaultControlState | None] = []
    original_resolve = controller.resolve_fault_drop_readback

    def resolver(session: str, state: FaultControlState | None) -> None:
        calls.append(state)
        original_resolve(session, state)

    monkeypatch.setattr(controller, "resolve_fault_drop_readback", resolver)
    drop = asyncio.create_task(
        service.submit_operator_action(
            run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
        )
    )
    try:
        await asyncio.wait_for(actuator.after_drop_read.wait(), 1)
        assert controller.has_pending_fault_drop_readback()
        close = await service.submit_operator_action(
            run_id,
            OperatorActionRequest(
                action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
                payload={"confirmation": True, "expected_generation": lease.generation},
            ),
        )
        assert close.result == "accepted" and close.queued
        assert await runner.tick_once()
        assert runner.finalized
        assert controller.phase is RoastPhase.FAULTED
        assert (await store.read_fault_control_lease()).status is FaultLeaseStatus.CLOSED
        controller_binding = service._human_fault_drop_controller(run_id)  # pyright: ignore[reportPrivateUsage]
        assert controller_binding is None
        process = service._mcp  # pyright: ignore[reportPrivateUsage]
        assert process is not None

        async def forbidden_stop() -> None:
            pytest.fail("pending readback triggered MCP stop")

        monkeypatch.setattr(process, "stop", forbidden_stop)
        with pytest.raises(RoastRunConflictError, match="ownership remains unresolved"):
            await service.start_roast(PROFILE)
        with pytest.raises(RoastRunConflictError, match="ownership remains unresolved"):
            await service._build_runner("replacement", PROFILE)  # pyright: ignore[reportPrivateUsage]
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://test"
        ) as client:
            response = await client.post("/api/roasts", json=PROFILE.model_dump(mode="json"))
        assert response.status_code == 409 and "ownership remains unresolved" in response.text
        timeline_before = await store.read_timeline(run_id)
    finally:
        actuator.release_drop_read.set()
        result = await drop
    assert result.result == ("accepted" if readback == "confirmed" else "failed")
    assert not controller.has_pending_fault_drop_readback()
    assert len(calls) == 1 and calls[0] is not None
    assert actuator.fault_writes == [OperatorAction.DROP_BEANS]
    timeline_after = await store.read_timeline(run_id)
    assert timeline_after.events == timeline_before.events
    if readback == "confirmed":
        assert (await service.start_roast(PROFILE)).id != run_id
    else:
        assert controller.snapshot().human_endpoint is EndpointDisposition.UNKNOWN
        assert controller.snapshot().human_command_disposition is CommandDisposition.UNKNOWN
        with pytest.raises(RoastRunConflictError, match="ownership remains unresolved"):
            await service.start_roast(PROFILE)


@pytest.mark.parametrize("route", ["legacy", "d212"])
async def test_fault_acknowledgement_superseded_during_evaluation_barrier(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store, open_lease=route == "d212")
    controller = runner.controller_for_human_fault_drop()
    executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
    assert controller is not None and executor is not None
    actuator.reject_unbound_inactive_emergency = True
    queue = executor._queue  # pyright: ignore[reportPrivateUsage]
    original_put = queue.put_nowait
    original_emergency = actuator.emergency_stop
    enqueues: list[object] = []
    unbound_attempts: list[str] = []

    def put(item: tuple[object, ...]) -> None:
        if item[2] is OperatorAction.EMERGENCY_STOP:
            enqueues.append(item)
        original_put(item)  # type: ignore[arg-type]

    async def stop_unbound(*, reason: str) -> AppliedRoasterState | None:
        unbound_attempts.append(reason)
        return await original_emergency(reason=reason)

    monkeypatch.setattr(queue, "put_nowait", put)
    monkeypatch.setattr(actuator, "emergency_stop", stop_unbound)
    held, release, barrier = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_record = store.record_safety_evaluation
    original_settle = controller.settle_human_drop_evaluations

    async def record(*, run_id: str, tick: int, evaluation: SafetyEvaluation) -> int:
        if evaluation.rule == "human_recovery_drop":
            held.set()
            await release.wait()
        return await original_record(run_id=run_id, tick=tick, evaluation=evaluation)

    async def settle() -> bool:
        barrier.set()
        return await original_settle()

    async def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("superseded acknowledgement initiated completion or DROP")

    monkeypatch.setattr(store, "record_safety_evaluation", record)
    monkeypatch.setattr(store, "complete_run", forbidden)
    monkeypatch.setattr(runner, "_fault_lease_close", forbidden)
    monkeypatch.setattr(runner, "_export_logs", forbidden)
    monkeypatch.setattr(controller, "settle_human_drop_evaluations", settle)
    drop = asyncio.create_task(
        controller.execute_human_fault_drop(
            actuator.session_id,
            generation=controller.command_generation,
            lease_current=lambda: True,
            operation=forbidden,
        )
    )
    try:
        await asyncio.wait_for(held.wait(), 1)
        action = (
            OperatorAction.ACKNOWLEDGE_FAULT
            if lease is None
            else OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE
        )
        payload = (
            None
            if lease is None
            else {"confirmation": True, "expected_generation": lease.generation}
        )
        admitted = await service.submit_operator_action(
            run_id, OperatorActionRequest(action=action, payload=payload)
        )
        assert admitted.result == "accepted" and admitted.queued
        completion = asyncio.create_task(runner.tick_once())
        try:
            await asyncio.wait_for(barrier.wait(), 1)
            stopped = await service.submit_fault_controls_action(
                FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
            )
            refused = await service.submit_fault_controls_action(
                FaultControlsActionRequest(action=OperatorAction.START_COOLING)
            )
            conflict: RoastRunConflictError | None = None
            try:
                await service.submit_operator_action(
                    run_id, OperatorActionRequest(action=OperatorAction.START_COOLING)
                )
            except RoastRunConflictError as error:
                conflict = error
            lease_settled = await service.settle_fault_lease_persistence_for_teardown()
            current_lease = await store.read_fault_control_lease()
            projection = await service._fault_controls_projection(current_lease)  # pyright: ignore[reportPrivateUsage]
        finally:
            release.set()
            completed = await completion
        dropped = await drop
        assert await executor.settle_for_shutdown()
        assert stopped.result == "confirmed"
        assert stopped.reason == "terminal emergency stop has fresh exact-session proof"
        assert unbound_attempts == []
        assert actuator.fault_writes == [OperatorAction.EMERGENCY_STOP]
        assert enqueues == []
        assert refused.result == "failed"
        assert refused.reason == "fault finalization is closing the current exact-session lease"
        assert str(conflict) == "fault finalization is closing the current lease"
        assert lease_settled and current_lease.status is FaultLeaseStatus.OPEN
        assert OperatorAction.START_COOLING in projection.enabled_actions
        assert completed is False and dropped is False
        detail = await store.read_run(run_id)
        assert detail is not None and detail.completed_at_utc is None
        assert detail.agent_phase is RoastPhase.FAULTED
        assert controller.human_drop_closed()
        assert not controller.human_recovery_drop_allowed()
    finally:
        release.set()
        await drop


async def test_tf18_completed_human_drop_refusal_has_no_history_effect(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    committed, release = asyncio.Event(), asyncio.Event()
    original = store.close_fault_control_lease_atomically

    async def pause_after_commit(**kwargs: object) -> FaultControlCloseResult:
        result = await original(**kwargs)  # type: ignore[arg-type]
        assert result.historically_committed
        committed.set()
        await release.wait()
        return result

    monkeypatch.setattr(store, "close_fault_control_lease_atomically", pause_after_commit)
    request = OperatorActionRequest(
        action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
        payload={"confirmation": True, "expected_generation": lease.generation},
    )
    assert (await service.submit_operator_action(run_id, request)).queued
    completion = asyncio.create_task(runner.tick_once())
    try:
        await asyncio.wait_for(committed.wait(), 1)
        stop = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )
        assert stop.result == "confirmed"
        assert await service.settle_fault_lease_persistence_for_teardown()
    finally:
        release.set()
        assert await completion
    assert runner.finalized
    assert runner._completion_persistence_initiated  # pyright: ignore[reportPrivateUsage]
    later_lease = await store.read_fault_control_lease()
    assert later_lease.status is FaultLeaseStatus.OPEN
    assert later_lease.generation > lease.generation
    assert later_lease.mcp_session_id == actuator.session_id
    executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
    assert executor is not None and await executor.settle_for_shutdown()
    projection = await service._fault_controls_projection(later_lease)  # pyright: ignore[reportPrivateUsage]
    assert OperatorAction.DROP_BEANS not in projection.enabled_actions
    assert OperatorAction.START_COOLING in projection.enabled_actions
    before = await store.read_timeline(run_id)
    assert controller.snapshot().human_endpoint is EndpointDisposition.NONE

    async def no_executor(*_args: object, **_kwargs: object) -> None:
        pytest.fail("historical HUMAN DROP reached the executor")

    monkeypatch.setattr(executor, "execute", no_executor)
    response = await service.submit_fault_controls_action(
        FaultControlsActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert response.result == "failed"
    assert response.reason == "exact terminal fault control is unavailable"
    assert OperatorAction.DROP_BEANS not in response.fault_controls.enabled_actions
    assert controller.snapshot().human_endpoint is EndpointDisposition.NONE
    assert actuator.fault_writes == [OperatorAction.EMERGENCY_STOP]
    assert await store.read_timeline(run_id) == before


@pytest.mark.parametrize("action", [OperatorAction.START_COOLING, OperatorAction.STOP_COOLING])
async def test_tf12_non_drop_admission_is_independent_of_human_drop_guard(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, action: OperatorAction
) -> None:
    service, _runner, actuator, _run_id, lease = await _faulted_run(store)
    assert lease is not None
    actuator.cooling = action is OperatorAction.STOP_COOLING
    service.close_human_call_admission(AdmissionCloseReason.APPLICATION_TEARDOWN)
    reads: list[tuple[OperatorAction, ...]] = []

    def broken_term(_run_id: str | None) -> None:
        reads.append(tuple(actuator.fault_writes))
        raise RuntimeError("guarded fault DROP dependency is unavailable")

    monkeypatch.setattr(service, "_human_fault_drop_controller", broken_term)
    result = await service.submit_fault_controls_action(FaultControlsActionRequest(action=action))
    assert result.result == "accepted"
    assert actuator.fault_writes == [action]
    assert reads == [(action,)]
    assert OperatorAction.DROP_BEANS not in result.fault_controls.enabled_actions
    assert OperatorAction.EMERGENCY_STOP in result.fault_controls.enabled_actions


async def test_tf13_raising_drop_projection_omits_only_drop_in_rest_sse_and_admission(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    actuator = _ExactFaultRunner(clock)
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    await _charge_live_runner(service, actuator, clock)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    response = await stream_events(run.id, cast(Request, _FakeRequest()), service)
    controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    clock.advance(0.25)
    await runner.tick_once()
    before = (await service.detail(run.id)).enabled_actions
    assert OperatorAction.DROP_BEANS in before

    def broken_term(_run_id: str | None) -> None:
        raise RuntimeError("guarded HUMAN drop dependency failed")

    monkeypatch.setattr(service, "_human_fault_drop_controller", broken_term)
    admitted = await service.submit_operator_action(
        run.id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert admitted.result == "rejected"
    assert (
        admitted.reason
        == "software drop unavailable; physical intervention or reconciliation required"
    )
    expected = [action for action in before if action is not OperatorAction.DROP_BEANS]
    assert (await service.detail(run.id)).enabled_actions == expected
    frames = await asyncio.wait_for(_collect_frames(response, 2), 1)
    assert _parse_frame(frames[1])["data"] == {
        "phase": RoastPhase.OPERATOR_RECOVERY_REQUIRED.value,
        "enabled_actions": [action.value for action in expected],
    }
    assert not actuator.fault_writes
    controller.transition_to(RoastPhase.FAULTED)
    clock.advance(0.25)
    await runner.tick_once()
    await store.open_fault_control_lease(run_id=run.id, mcp_session_id=actuator.session_id)
    result = await service.submit_fault_controls_action(
        FaultControlsActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert result.result == "rejected"
    assert result.reason == "action is not enabled for the current fault controls"
    assert OperatorAction.EMERGENCY_STOP in result.fault_controls.enabled_actions
    assert not actuator.fault_writes


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("no_reservation", _FinalisationCloseEvidence.PROVEN_NOT_COMMITTED),
        ("no_open_lease", _FinalisationCloseEvidence.PROVEN_NOT_COMMITTED),
        ("wrong_generation", _FinalisationCloseEvidence.PROVEN_NOT_COMMITTED),
        ("wrong_identity", _FinalisationCloseEvidence.PROVEN_NOT_COMMITTED),
        ("missing_identity", _FinalisationCloseEvidence.PROVEN_NOT_COMMITTED),
        ("known_rollback", _FinalisationCloseEvidence.PROVEN_NOT_COMMITTED),
        ("store_raised", _FinalisationCloseEvidence.UNCERTAIN),
        ("store_none", _FinalisationCloseEvidence.UNCERTAIN),
        ("verify_unavailable", _FinalisationCloseEvidence.UNCERTAIN),
        ("changed_durable_shape", _FinalisationCloseEvidence.UNCERTAIN),
        ("historical_commit", _FinalisationCloseEvidence.COMMITTED),
        ("unconfirmed_commit", _FinalisationCloseEvidence.COMMITTED),
        ("confirmed_commit", _FinalisationCloseEvidence.COMMITTED),
    ],
)
async def test_tf19_fault_close_evidence_preserves_existing_results(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected: _FinalisationCloseEvidence,
) -> None:
    service, _runner, actuator, _run_id, initial = await _faulted_run(store)
    assert initial is not None
    gate = service._fault_acknowledgement_gate  # pyright: ignore[reportPrivateUsage]
    gate.fault_finalization_started = path != "no_reservation"
    lease = initial
    if path == "no_open_lease":
        lease = initial.model_copy(update={"status": FaultLeaseStatus.CLOSED})
    generation = initial.generation + int(path == "wrong_generation")
    session_id = (
        None
        if path == "missing_identity"
        else "other"
        if path == "wrong_identity"
        else actuator.session_id
    )
    commit = path in {"historical_commit", "confirmed_commit", "unconfirmed_commit"}
    base_result = FaultControlCloseResult(
        historically_committed=commit,
        confirmed=commit and path != "unconfirmed_commit",
        run_completed=commit,
        verification_unavailable=path == "verify_unavailable",
    )
    store_calls: list[int] = []

    async def close(**_kwargs: object) -> FaultControlCloseResult | None:
        store_calls.append(1)
        if path == "store_raised":
            raise RuntimeError("commit outcome lost")
        if path == "historical_commit":
            monkeypatch.setattr(
                service,
                "_fault_lease_cache",
                initial.model_copy(update={"generation": initial.generation + 1}),
            )
        return None if path == "store_none" else base_result

    async def verify(**_kwargs: object) -> FaultControlCloseResult:
        assert path == "verify_unavailable"
        return base_result

    async def reread() -> FaultControlLease:
        return (
            initial.model_copy(update={"status": FaultLeaseStatus.UNKNOWN})
            if path in {"verify_unavailable", "changed_durable_shape", "unconfirmed_commit"}
            else initial
        )

    async def mark_unknown(_generation: int, _lease: FaultControlLease) -> None:
        assert expected is _FinalisationCloseEvidence.UNCERTAIN or path == "unconfirmed_commit"

    monkeypatch.setattr(store, "close_fault_control_lease_atomically", close)
    monkeypatch.setattr(store, "verify_fault_control_lease_close", verify)
    monkeypatch.setattr(service, "_read_fault_lease_fail_closed", reread)
    monkeypatch.setattr(service, "_mark_unconfirmed_fault_lease_unknown", mark_unknown)
    monkeypatch.setattr(service, "_fault_lease_cache", lease)
    evidence = await service._close_current_fault_lease_for_finalisation(  # pyright: ignore[reportPrivateUsage]
        generation, "synthetic", session_id
    )
    assert evidence is expected

    monkeypatch.setattr(service, "_fault_lease_cache", lease)
    result = await service._close_current_fault_lease_for_acknowledgement_result(  # pyright: ignore[reportPrivateUsage]
        generation, "synthetic", session_id
    )
    assert result.historically_committed is commit
    assert result.run_completed is commit
    assert result.confirmed is (path == "confirmed_commit")

    monkeypatch.setattr(service, "_fault_lease_cache", lease)
    assert (
        await service._close_current_fault_lease_for_acknowledgement(  # pyright: ignore[reportPrivateUsage]
            generation, "synthetic", session_id
        )
        is commit
    )
    assert store_calls == (
        []
        if path
        in {
            "no_reservation",
            "no_open_lease",
            "wrong_generation",
            "wrong_identity",
            "missing_identity",
        }
        else [1, 1, 1]
    )


@pytest.mark.parametrize("prior", ["none", "uncertain", "raised"])
@pytest.mark.parametrize("proof", ["pre_store", "rollback"])
async def test_tf19_uncertain_completion_stays_closed_after_another_noncommit(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, prior: str, proof: str
) -> None:
    service, runner, actuator, run_id, initial = await _faulted_run(store)
    assert initial is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    wrapped_close = service._close_current_fault_lease_for_finalisation  # pyright: ignore[reportPrivateUsage]
    setting = prior

    async def early_result(
        generation: int | None, reason: str | None, session_id: str | None
    ) -> _FinalisationCloseEvidence:
        assert controller.human_drop_closed()
        assert runner._completion_persistence_initiated  # pyright: ignore[reportPrivateUsage]
        if setting == "raised":
            raise RuntimeError("synthetic lost completion commit")
        if setting == "uncertain":
            return _FinalisationCloseEvidence.UNCERTAIN
        if proof == "pre_store":
            session_id = "wrong-session"
        return await wrapped_close(generation, reason, session_id)

    async def rollback(**_kwargs: object) -> FaultControlCloseResult:
        assert proof == "rollback"
        return FaultControlCloseResult(
            historically_committed=False, confirmed=False, run_completed=False
        )

    monkeypatch.setattr(runner, "_fault_lease_close", early_result)
    monkeypatch.setattr(store, "close_fault_control_lease_atomically", rollback)
    request = OperatorActionRequest(
        action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
        payload={"confirmation": True, "expected_generation": initial.generation},
    )
    if prior != "none":
        assert (await service.submit_operator_action(run_id, request)).queued
        actuator.clock.advance(0.25)
        if prior == "raised":
            with pytest.raises(RuntimeError, match="synthetic lost completion commit"):
                await runner.tick_once()
        else:
            assert not await runner.tick_once()
        assert controller.human_drop_closed()
        assert runner._completion_persistence_initiated is True  # pyright: ignore[reportPrivateUsage]
    setting = "proven"
    if prior != "raised":
        assert (await service.submit_operator_action(run_id, request)).queued
    # The base propagation path leaves the already-reserved proof in place.
    actuator.clock.advance(0.25)
    assert not await runner.tick_once()
    assert controller.human_drop_closed() is (prior != "none")
    assert runner._completion_persistence_initiated is (prior != "none")  # pyright: ignore[reportPrivateUsage]

    # TG6: outstanding+unstarted is white-box once a real completion attempt
    # has been entered. It isolates the lifetime predicate after reopening.
    release_probe = asyncio.Event()

    async def hold_probe() -> int:
        await release_probe.wait()
        return 1

    probe = asyncio.create_task(hold_probe())
    registry = controller._human_drop_evaluation_tasks  # pyright: ignore[reportPrivateUsage]
    registry.add(probe)
    try:
        assert runner.emergency_stop_must_bypass_terminal_writer() is (prior == "none")
    finally:
        release_probe.set()
        await probe
        registry.remove(probe)
    assert controller.human_recovery_drop_allowed(session_id=actuator.session_id) is (
        prior == "none"
    )
    detail = await store.read_run(run_id)
    assert detail is not None and detail.completed_at_utc is None

    later_drop = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert later_drop.result == ("accepted" if prior == "none" else "failed")
    assert actuator.fault_writes == ([OperatorAction.DROP_BEANS] if prior == "none" else [])


async def test_tf16_unknown_fault_drop_keeps_exact_cooling_and_only_proven_no_write_d212(
    store: RoastStore,
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    actuator.issued_readback = "contradiction"
    first = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert first.result == "failed"
    assert controller.snapshot().human_endpoint is EndpointDisposition.UNKNOWN
    assert controller.snapshot().human_command_disposition is CommandDisposition.UNKNOWN
    assert controller.phase is RoastPhase.FAULTED
    # The exact-session port keeps returning beans-not-dropped. The second
    # per-run request therefore reaches the join independent of the port's precheck.
    repeated = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert repeated.result == "failed"
    global_repeat = await service.submit_fault_controls_action(
        FaultControlsActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert global_repeat.result == "rejected"
    started = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.START_COOLING)
    )
    assert started.result == "accepted" and actuator.cooling
    ack = OperatorActionRequest(
        action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
        payload={"confirmation": True, "expected_generation": lease.generation},
    )
    assert (await service.submit_operator_action(run_id, ack)).queued
    actuator.clock.advance(0.25)
    assert not await runner.tick_once()
    assert actuator.cooling
    before = await store.read_timeline(run_id)
    assert (
        sum(
            event.kind is RoastEventKind.COMMAND_FAILED
            and event.payload is not None
            and event.payload.get("outcome") == "refused_unknown_prior_drop"
            for event in before.events
        )
        == 1
    )
    assert any(
        event.kind is RoastEventKind.FAULT_ACKNOWLEDGEMENT_EXECUTED
        and event.payload is not None
        and event.payload.get("reason") == "stop_unconfirmed"
        for event in before.events
    )
    assert (await store.read_fault_control_lease()).status is FaultLeaseStatus.OPEN
    stopped = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.STOP_COOLING)
    )
    assert stopped.result == "accepted" and not actuator.cooling
    assert (await service.submit_operator_action(run_id, ack)).queued
    actuator.clock.advance(0.25)
    assert await runner.tick_once()
    assert actuator.fault_writes == [
        OperatorAction.DROP_BEANS,
        OperatorAction.START_COOLING,
        OperatorAction.STOP_COOLING,
    ]
    assert "stop_cooling_for_fault_acknowledgement" not in actuator.commands()
    assert (await store.read_fault_control_lease()).status is FaultLeaseStatus.CLOSED
    assert controller.snapshot().human_endpoint is EndpointDisposition.UNKNOWN
    assert controller.phase is RoastPhase.FAULTED
    with pytest.raises(RoastRunConflictError, match="ownership remains unresolved"):
        await service.start_roast(PROFILE)


@pytest.mark.parametrize("exit_kind", ["readback_cancelled", "lease_superseded"])
async def test_tf9_captured_resolver_clears_ownership_on_incomplete_readback(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, exit_kind: str
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    resolver = controller.resolve_fault_drop_readback
    seen: list[FaultControlState | None] = []
    selector_calls: list[str | None] = []
    executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
    assert executor is not None

    def select(bound: str | None) -> object:
        selector_calls.append(bound)
        return controller if len(selector_calls) == 1 else None

    def resolve(session_id: str, observed: FaultControlState | None) -> None:
        seen.append(observed)
        resolver(session_id, observed)

    monkeypatch.setattr(executor, "_human_drop_controller", select)
    monkeypatch.setattr(controller, "resolve_fault_drop_readback", resolve)
    if exit_kind == "lease_superseded":
        original = controller.execute_human_fault_drop

        async def supersede(session_id: str, **kwargs: object) -> bool:
            applied = await original(session_id, **kwargs)  # type: ignore[arg-type]
            assert applied and controller.has_pending_fault_drop_readback()
            monkeypatch.setattr(
                service,
                "_fault_lease_cache",
                lease.model_copy(update={"generation": lease.generation + 1}),
            )
            return applied

        monkeypatch.setattr(controller, "execute_human_fault_drop", supersede)
    else:
        actuator.after_drop_read, actuator.release_drop_read = asyncio.Event(), asyncio.Event()

    request = asyncio.create_task(
        service.submit_operator_action(
            run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
        )
    )
    if exit_kind == "readback_cancelled":
        assert actuator.after_drop_read is not None
        try:
            await asyncio.wait_for(actuator.after_drop_read.wait(), 1)
            assert controller.has_pending_fault_drop_readback()
            worker = executor._worker  # pyright: ignore[reportPrivateUsage]
            assert worker is not None
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
        finally:
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
    else:
        assert (await request).result == "failed"
    assert seen == [None]
    assert selector_calls == [run_id]
    assert not controller.has_pending_fault_drop_readback()
    assert controller.snapshot().human_endpoint is EndpointDisposition.ISSUED
    assert controller.snapshot().human_command_disposition is CommandDisposition.SETTLED
    assert actuator.fault_writes == [OperatorAction.DROP_BEANS]
    assert len(actuator.fault_reads) == (2 if exit_kind == "readback_cancelled" else 1)


@pytest.mark.parametrize(
    "outcome", ["confirmed", "contradiction", "write_error", "persistence_error"]
)
async def test_tf7_tf8_global_live_drop_uses_exact_executor_and_refuses_duplicate(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    original_write = actuator.write_fault_control
    original_evaluation = store.record_safety_evaluation
    actuator.issued_readback = "contradiction" if outcome == "contradiction" else "confirmed"

    async def write(action: OperatorAction, *, expected_session_id: str) -> None:
        await original_write(action, expected_session_id=expected_session_id)
        if outcome == "write_error":
            raise TimeoutError("strict write outcome unknown")

    async def record(*, run_id: str, tick: int, evaluation: SafetyEvaluation) -> int:
        if outcome == "persistence_error" and evaluation.rule == "human_recovery_drop":
            raise RuntimeError("storage failed before admission")
        return await original_evaluation(run_id=run_id, tick=tick, evaluation=evaluation)

    monkeypatch.setattr(actuator, "write_fault_control", write)
    monkeypatch.setattr(store, "record_safety_evaluation", record)
    request = FaultControlsActionRequest(action=OperatorAction.DROP_BEANS)
    result = await service.submit_fault_controls_action(request)
    assert result.result == ("accepted" if outcome == "confirmed" else "rejected")
    assert result.reason == (
        "fault action accepted through exact-session controller dispatch"
        if outcome == "confirmed"
        else "exact fault-session action was not confirmed"
    )
    expected = {
        "confirmed": EndpointDisposition.ISSUED,
        "contradiction": EndpointDisposition.UNKNOWN,
        "write_error": EndpointDisposition.UNKNOWN,
        "persistence_error": EndpointDisposition.NONE,
    }[outcome]
    assert controller.snapshot().human_endpoint is expected
    assert controller.phase is RoastPhase.FAULTED
    assert not controller.has_pending_fault_drop_readback()
    if outcome == "confirmed":
        # Present trackable contradictory evidence only to a later port
        # precheck so the per-run join, not the precheck, refuses the repeat.
        actuator.issued_readback = "contradiction"
        repeated = await service.submit_operator_action(
            run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
        )
        assert repeated.result == "failed"
        projected_out = await service.submit_fault_controls_action(request)
        assert projected_out.result == "rejected"
        assert projected_out.reason == "action is not enabled for the current fault controls"
        actuator.clock.advance(0.25)
        await runner.tick_once()
        trace = await store.read_timeline(run_id)
        assert (
            sum(
                event.payload is not None
                and event.payload.get("outcome") == "joined_protective_endpoint"
                for event in trace.events
            )
            == 1
        )
    assert actuator.fault_writes == (
        [] if outcome == "persistence_error" else [OperatorAction.DROP_BEANS]
    )


async def test_tf23_automatic_finalisation_and_completed_drop_never_consult_human_barriers(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    actuator = _ExactFaultRunner(clock)
    service = RoastService(store, roaster=actuator, run_loop=False, clock=clock)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner._controller  # pyright: ignore[reportPrivateUsage]
    controller.transition_to(RoastPhase.FAULTED)
    await runner.tick_once()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("AUTOMATIC consulted HUMAN completion, projection, or fence code")

    monkeypatch.setattr(controller, "begin_human_completion", forbidden)
    monkeypatch.setattr(controller, "settle_human_drop_evaluations", forbidden)
    monkeypatch.setattr(service, "_guarded_human_drop_allowed", forbidden)
    monkeypatch.setattr(service, "_human_fault_drop_controller", forbidden)
    ack = await service.submit_operator_action(
        run.id, OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_FAULT)
    )
    assert ack.queued
    assert await runner.tick_once()
    assert runner.finalized
    lease = await store.open_fault_control_lease(run_id=run.id, mcp_session_id=actuator.session_id)
    projection = await service._fault_controls_projection(lease)  # pyright: ignore[reportPrivateUsage]
    assert OperatorAction.DROP_BEANS in projection.enabled_actions
    response = await service.submit_fault_controls_action(
        FaultControlsActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert response.result == "confirmed"
    assert actuator.fault_writes == [OperatorAction.DROP_BEANS]


@pytest.mark.parametrize("terminal", [False, True])
async def test_tf12_d212_admission_calls_no_human_drop_terms(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    terminal: bool,
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    if terminal:
        # Pin the terminal branch separately from the live queue; TF18
        # independently proves the real superseded-D212 completed state.
        await store.complete_run(run_id=run_id, outcome="faulted", agent_phase=RoastPhase.FAULTED)
        controller.note_human_run_durably_closed()
    service.close_human_call_admission(AdmissionCloseReason.APPLICATION_TEARDOWN)
    original_submit = service.submit_operator_action
    original_close = store.close_fault_control_lease_atomically
    outcome_fixed = False
    projection_checks: list[bool] = []

    def broken_term(_run_id: str | None) -> None:
        projection_checks.append(outcome_fixed)
        raise RuntimeError("HUMAN DROP evidence unavailable")

    async def live_submit(run_id: str, request: OperatorActionRequest) -> object:
        nonlocal outcome_fixed
        assert not projection_checks
        result = await original_submit(run_id, request)
        assert not projection_checks
        outcome_fixed = True
        return result

    async def terminal_close(**kwargs: object) -> FaultControlCloseResult:
        nonlocal outcome_fixed
        assert not projection_checks
        result = await original_close(**kwargs)  # type: ignore[arg-type]
        outcome_fixed = True
        return result

    monkeypatch.setattr(service, "_human_fault_drop_controller", broken_term)
    monkeypatch.setattr(service, "submit_operator_action", live_submit)
    monkeypatch.setattr(store, "close_fault_control_lease_atomically", terminal_close)
    result = await service.submit_fault_controls_action(
        FaultControlsActionRequest(
            action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
            confirmation=True,
            expected_generation=lease.generation,
        )
    )
    assert result.result == ("confirmed" if terminal else "accepted")
    assert outcome_fixed
    assert projection_checks == ([] if terminal else [True])
    assert OperatorAction.DROP_BEANS not in result.fault_controls.enabled_actions
    assert actuator.fault_writes == []


@pytest.mark.parametrize("which", ["fence", "teardown"])
async def test_te26_exact_fault_executor_withholds_human_drop_under_revocation(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    which: str,
) -> None:
    service, runner, actuator, run_id, initial = await _faulted_run(store)
    assert initial is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    if which == "fence":
        service.close_human_call_admission(AdmissionCloseReason.APPLICATION_TEARDOWN)
    else:
        process = service._mcp  # pyright: ignore[reportPrivateUsage]
        assert process is not None
        monkeypatch.setattr(process, "_stop_unconfirmed", True)
    result = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert result.result == "failed"
    assert result.reason == "exact fault-session action was not confirmed"
    assert actuator.fault_writes == []
    snapshot = controller.snapshot()
    assert snapshot.phase is RoastPhase.FAULTED
    assert snapshot.human_endpoint is EndpointDisposition.WITHHELD
    assert snapshot.human_command_disposition is CommandDisposition.SETTLED
    assert snapshot.physical_intervention_required
    assert actuator.containments == 0
    executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
    assert executor is not None and await executor.settle_for_shutdown()
    assert await store.read_fault_control_lease() == initial


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize(
    "endpoint",
    [EndpointDisposition.ISSUED, EndpointDisposition.WITHHELD, EndpointDisposition.UNKNOWN],
)
async def test_tf11_emergency_routes_fix_outcome_before_any_drop_term(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    closed: bool,
    endpoint: EndpointDisposition,
) -> None:
    service, runner, actuator, run_id, _ = await _faulted_run(store)
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    run = controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert run is not None
    run.endpoint = endpoint
    if endpoint is EndpointDisposition.UNKNOWN:
        run.command = CommandDisposition.UNKNOWN
    if closed:
        service.close_human_call_admission(AdmissionCloseReason.APPLICATION_TEARDOWN)
    successful_stops = 0
    drops_considered: list[int] = []
    write = actuator.write_fault_control
    stop = actuator.emergency_stop

    def broken_drop_term(_run_id: str | None) -> None:
        drops_considered.append(successful_stops)
        raise RuntimeError("unavailable presentation dependency")

    async def record_write(action: OperatorAction, *, expected_session_id: str) -> None:
        nonlocal successful_stops
        await write(action, expected_session_id=expected_session_id)
        assert action is OperatorAction.EMERGENCY_STOP
        successful_stops += 1

    async def record_stop(*, reason: str) -> object:
        nonlocal successful_stops
        applied = await stop(reason=reason)
        successful_stops += 1
        return applied

    monkeypatch.setattr(service, "_human_fault_drop_controller", broken_drop_term)
    monkeypatch.setattr(actuator, "write_fault_control", record_write)
    monkeypatch.setattr(actuator, "emergency_stop", record_stop)
    request = FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
    result = await service.submit_fault_controls_action(request)
    assert result.result in {"accepted", "confirmed"}
    assert successful_stops == 1
    per_run = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
    )
    assert per_run.result == "accepted"
    assert successful_stops == 2
    assert all(count > 0 for count in drops_considered)
    assert await service.settle_fault_lease_persistence_for_teardown()
    current_lease = await store.read_fault_control_lease()
    assert current_lease.status is FaultLeaseStatus.OPEN
    projection = await service._fault_controls_projection(current_lease)  # pyright: ignore[reportPrivateUsage]
    assert drops_considered[-1] == 2
    assert OperatorAction.EMERGENCY_STOP in projection.enabled_actions
    assert OperatorAction.DROP_BEANS not in projection.enabled_actions


async def test_tf9_recovered_human_fault_drop_joins_history_unknown_without_new_evaluation(
    store: RoastStore,
) -> None:
    clock = FakeClock()
    actuator = _ExactFaultRunner(clock)
    run_id = "recovered-fault"
    await store.create_run(
        run_id=run_id, profile=PROFILE, config=AppConfig(), agent_phase=RoastPhase.FAULTED
    )
    service = _service(store, clock, actuator, exact_child=True)
    await service.recover_on_start()
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    assert controller.snapshot().human_endpoint is EndpointDisposition.HISTORY_UNKNOWN
    emergency = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
    )
    assert emergency.result == "accepted"
    assert await service.settle_fault_lease_persistence_for_teardown()
    assert controller.phase is RoastPhase.FAULTED
    clock.advance(0.25)
    await runner.tick_once()
    # Recovery carries no bound MCP session from history. Seed the white-box
    # exact lease prerequisite while preserving the real HISTORY_UNKNOWN owner.
    await store.open_fault_control_lease(run_id=run_id, mcp_session_id=actuator.session_id)
    before = await store.read_timeline(run_id)
    response = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert response.result == "failed", response.reason
    assert actuator.fault_writes == []
    assert not controller.has_pending_fault_drop_readback()
    assert controller.snapshot().human_endpoint is EndpointDisposition.HISTORY_UNKNOWN
    await runner.tick_once()
    after = await store.read_timeline(run_id)
    assert all(
        row.rule != "human_recovery_drop"
        for row in after.safety_evaluations[len(before.safety_evaluations) :]
    )
    assert any(
        event.payload is not None and event.payload.get("outcome") == "refused_unknown_prior_drop"
        for event in after.events[len(before.events) :]
    )


async def test_tf10_applied_fault_drop_superseded_before_return_keeps_in_flight_owner(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runner, actuator, run_id, _ = await _faulted_run(store)
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    entered, release = asyncio.Event(), asyncio.Event()
    original = actuator.write_fault_control

    async def held_write(action: OperatorAction, *, expected_session_id: str) -> None:
        await original(action, expected_session_id=expected_session_id)
        entered.set()
        await release.wait()

    monkeypatch.setattr(actuator, "write_fault_control", held_write)
    drop = asyncio.create_task(
        service.submit_operator_action(
            run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        controller.admit_emergency_stop()
        assert controller.snapshot().human_endpoint is EndpointDisposition.IN_FLIGHT
    finally:
        release.set()
        response = await drop
    assert response.result == "failed"
    assert not controller.has_pending_fault_drop_readback()
    assert controller.snapshot().human_endpoint is EndpointDisposition.IN_FLIGHT
    assert controller.snapshot().human_command_disposition is CommandDisposition.SETTLED
    assert await controller.operator_emergency_stop("superseding stop", already_admitted=True)
    assert actuator.fault_writes == [OperatorAction.DROP_BEANS]
    assert actuator.fault_reads == [actuator.session_id]
    controller.note_human_run_durably_closed()
    with pytest.raises(RoastRunConflictError, match="ownership remains unresolved"):
        await service.start_roast(PROFILE)


async def test_tf19_legacy_completion_exception_keeps_drop_closed(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runner, actuator, run_id, _ = await _faulted_run(store, open_lease=False)
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    original = store.complete_run

    async def fail(**_kwargs: object) -> None:
        assert controller.human_drop_closed()
        raise RuntimeError("legacy terminal commit unconfirmed")

    monkeypatch.setattr(store, "complete_run", fail)
    ack = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_FAULT)
    )
    assert ack.queued
    with pytest.raises(RuntimeError, match="legacy terminal commit unconfirmed"):
        await runner.tick_once()
    assert controller.human_drop_closed()
    assert runner._completion_persistence_initiated  # pyright: ignore[reportPrivateUsage]
    assert not controller.human_recovery_drop_allowed()
    monkeypatch.setattr(store, "complete_run", original)
    assert await runner.tick_once()
    assert controller.human_drop_closed()
    assert actuator.fault_writes == []


@pytest.mark.parametrize(
    ("route", "interval", "registry", "fault", "expected_route"),
    [
        pytest.param("global", "barrier", "pending", "none", "priority", id="TG1-TG12"),
        pytest.param("global", "proof", "pending", "none", "priority", id="TG2-c1"),
        pytest.param("global", "proof2", "pending", "none", "priority", id="TG2-c2"),
        pytest.param("global", "proof", "empty", "none", "queued", id="TG3-proof"),
        pytest.param("global", "barrier", "empty", "none", "queued", id="TG3-barrier"),
        pytest.param("global", "proof", "raised", "none", "queued", id="TG4"),
        pytest.param("global", "proof", "cancelled", "none", "priority", id="TG4b"),
        pytest.param("global", "close", "injected", "none", "queued", id="TG5"),
        pytest.param("global", "barrier", "pending", "capability_raises", "queued", id="TG9"),
        *[
            pytest.param(
                route,
                "barrier",
                "pending",
                capability,
                "queued" if route == "global" else "direct",
                id=f"TG9b-{route}-{capability}",
            )
            for route in ("global", "run")
            for capability in ("missing", "truthy", "mock")
        ],
        *[
            pytest.param(route, "barrier", "pending", fault, "priority", id=f"{row}-{route}")
            for route in ("global", "run")
            for row, fault in (
                ("TG10A", "stop_raises"),
                ("TG10B-read", "read_raises"),
                ("TG10B-heat", "read_hot"),
                ("TG10C", "command_audit_raises"),
                ("TH4", "latest_identity_changes"),
                ("TH13-executor", "missing_executor"),
                ("TH13-session", "missing_session"),
            )
        ],
        pytest.param("run", "barrier", "pending", "direct_audit_raises", "direct", id="TG10D"),
        pytest.param("global", "barrier", "pending", "cancel_http", "priority", id="TG11"),
        pytest.param("run", "barrier", "pending", "none", "priority", id="TP1"),
        pytest.param("run", "proof", "pending", "none", "priority", id="TP2"),
        pytest.param("run", "none", "pending", "none", "direct", id="TP3-TG13-run"),
        pytest.param("run", "proof", "empty", "none", "direct", id="TP4-empty"),
        pytest.param("run", "proof", "raised", "none", "direct", id="TP4-raised"),
        pytest.param("run", "barrier", "pending", "action_audit_raises", "priority", id="TP5"),
        pytest.param("run", "barrier", "pending", "cancel_http", "priority", id="TP6"),
        pytest.param("global", "none", "pending", "none", "direct", id="TG13-global"),
    ],
)
async def test_am5_estop_route_during_owned_human_evaluation(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    route: str,
    interval: str,
    registry: str,
    fault: str,
    expected_route: str,
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    controller = runner.controller_for_human_fault_drop()
    executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
    assert controller is not None and executor is not None
    arbiter = service._command_arbiter  # pyright: ignore[reportPrivateUsage]
    queue = executor._queue  # pyright: ignore[reportPrivateUsage]
    tasks = controller._human_drop_evaluation_tasks  # pyright: ignore[reportPrivateUsage]
    dispatch_tasks = service._emergency_stop_dispatch_tasks  # pyright: ignore[reportPrivateUsage]
    held, release_evaluation = asyncio.Event(), asyncio.Event()
    reserved, release_finalisation = asyncio.Event(), asyncio.Event()
    exact_written, unbound_attempted, estop_enqueued = (asyncio.Event() for _ in range(3))
    release_write = asyncio.Event()
    callback_consumed = asyncio.Event()
    completion_operations: list[str] = []
    exact_attempts: list[str] = []
    dispatch_order: list[str] = []
    admissions = capability_reads = direct_attempts = ack_reads = ack_writes = 0
    audit_failures = confirmations = 0
    dispatch_callbacks: list[object] = []
    cooling_samples: list[tuple[str, str, str]] = []
    advertised_cooling_while_blocked = reservation_after_stop = False
    actuator.reject_unbound_inactive_emergency = expected_route == "priority"
    actuator.exact_emergency_write_raises = fault == "stop_raises"
    actuator.exact_emergency_read_raises = fault == "read_raises"
    actuator.exact_emergency_read_heat = 1 if fault == "read_hot" else 0
    actuator.cooling = interval in {"proof", "proof2"} and registry == "pending"

    original_record = store.record_safety_evaluation
    original_command = store.record_command
    original_action_audit = store.record_operator_action
    original_ack = actuator.read_fault_acknowledgement_state
    original_ack_write = actuator.stop_cooling_for_fault_acknowledgement
    original_settle = controller.settle_human_drop_evaluations
    original_emergency = actuator.emergency_stop
    original_exact = actuator.write_fault_control
    original_read = actuator.read_fault_control_state
    original_put = queue.put_nowait
    original_capability = runner.emergency_stop_must_bypass_terminal_writer
    original_consume = service._log_cancelled_terminal_estop_dispatch  # pyright: ignore[reportPrivateUsage]
    original_track = service._track_emergency_stop_dispatch  # pyright: ignore[reportPrivateUsage]
    original_admit = arbiter.admit_emergency_stop
    original_confirm = runner.confirm_external_emergency_stop
    original_admit_lease = service._admit_fault_control_lease  # pyright: ignore[reportPrivateUsage]

    async def record(*, run_id: str, tick: int, evaluation: SafetyEvaluation) -> int:
        if evaluation.rule == "human_recovery_drop":
            held.set()
            await release_evaluation.wait()
            if registry == "raised":
                raise RuntimeError("synthetic evaluation persistence failure")
        return await original_record(run_id=run_id, tick=tick, evaluation=evaluation)

    async def record_command(
        *,
        run_id: str,
        tick: int,
        tool: RoastCommand,
        source: Literal["policy", "advisor", "operator", "safety", "recovery"],
        status: Literal["ok", "failed"],
        args: object = None,
        result: object = None,
        safety_evaluation_id: int | None = None,
    ) -> None:
        nonlocal audit_failures
        if tool is RoastCommand.EMERGENCY_STOP and fault in {
            "command_audit_raises",
            "direct_audit_raises",
        }:
            audit_failures += 1
            raise RuntimeError("synthetic emergency command audit failure")
        await original_command(
            run_id=run_id,
            tick=tick,
            tool=tool,
            source=source,
            status=status,
            args=args,
            result=result,
            safety_evaluation_id=safety_evaluation_id,
        )

    async def read_ack(session: str) -> FaultAcknowledgementState:
        nonlocal ack_reads
        ack_reads += 1
        if (interval, ack_reads) in {("proof", 1), ("proof2", 2)}:
            reserved.set()
            await release_finalisation.wait()
        return await original_ack(session)

    async def stop_ack(session: str) -> FaultCoolingStopResult:
        nonlocal ack_writes
        ack_writes += 1
        return await original_ack_write(session)

    async def settle() -> bool:
        if interval == "barrier":
            reserved.set()
            if registry != "pending":
                await release_finalisation.wait()
        return await original_settle()

    async def close(*_args: object) -> _FinalisationCloseEvidence:
        completion_operations.append("close")
        if interval == "close":
            reserved.set()
            await release_finalisation.wait()
        return _FinalisationCloseEvidence.PROVEN_NOT_COMMITTED

    async def complete(**_kwargs: object) -> None:
        completion_operations.append("complete_run")

    async def export() -> None:
        completion_operations.append("export")

    async def direct(*, reason: str) -> AppliedRoasterState | None:
        nonlocal direct_attempts
        direct_attempts += 1
        unbound_attempted.set()
        return await original_emergency(reason=reason)

    async def exact_write(action: OperatorAction, *, expected_session_id: str) -> None:
        if action is OperatorAction.EMERGENCY_STOP:
            dispatch_order.append("write")
            exact_attempts.append(expected_session_id)
            exact_written.set()
            if fault == "cancel_http":
                await release_write.wait()
        await original_exact(action, expected_session_id=expected_session_id)

    async def read(session: str) -> FaultControlState:
        if asyncio.current_task() in dispatch_tasks:
            dispatch_order.append("read")
        return await original_read(session)

    def put(item: tuple[object, ...]) -> None:
        original_put(item)  # type: ignore[arg-type]
        if item[2] is OperatorAction.EMERGENCY_STOP:
            estop_enqueued.set()

    def consume_cancelled_waiter(dispatch: asyncio.Task[bool]) -> None:
        original_consume(dispatch)
        callback_consumed.set()

    def track(dispatch: asyncio.Task[bool]) -> None:
        original_track(dispatch)
        if fault == "latest_identity_changes":
            actuator.latest_fault_session_override = "identity-after-admission"
        if fault == "cancel_http":
            original_add = dispatch.add_done_callback

            def add_callback(callback: object) -> None:
                dispatch_callbacks.append(callback)
                original_add(callback)  # type: ignore[arg-type]

            monkeypatch.setattr(dispatch, "add_done_callback", add_callback)

    def admit() -> int:
        nonlocal admissions
        admissions += 1
        return original_admit()

    def confirm() -> None:
        nonlocal confirmations
        confirmations += 1
        original_confirm()

    def admit_lease(
        run_id: str, *, session_id: str | None = None, use_latest_session: bool = True
    ) -> FaultControlLease:
        if fault == "missing_session":
            return original_admit_lease(run_id, session_id=None, use_latest_session=False)
        return original_admit_lease(
            run_id, session_id=session_id, use_latest_session=use_latest_session
        )

    def capability() -> bool:
        nonlocal capability_reads
        capability_reads += 1
        if fault in {"capability_raises", "direct_audit_raises"}:
            raise RuntimeError("synthetic routing capability failure")
        return original_capability()

    async def cooling_sample() -> tuple[str, str, str]:
        cooling = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.START_COOLING)
        )
        per_run = ""
        try:
            await service.submit_operator_action(
                run_id, OperatorActionRequest(action=OperatorAction.START_COOLING)
            )
        except RoastRunConflictError as error:
            per_run = str(error)
        return cooling.result, cooling.reason, per_run

    async def reject_action_audit(
        *,
        action: str,
        result: Literal["accepted", "rejected", "failed"],
        run_id: str | None = None,
        payload: object = None,
    ) -> None:
        if action == OperatorAction.EMERGENCY_STOP.value:
            raise RuntimeError("synthetic operator-action audit failure")
        await original_action_audit(action=action, result=result, run_id=run_id, payload=payload)

    monkeypatch.setattr(store, "record_safety_evaluation", record)
    monkeypatch.setattr(store, "record_command", record_command)
    monkeypatch.setattr(actuator, "read_fault_acknowledgement_state", read_ack)
    monkeypatch.setattr(actuator, "stop_cooling_for_fault_acknowledgement", stop_ack)
    monkeypatch.setattr(controller, "settle_human_drop_evaluations", settle)
    monkeypatch.setattr(runner, "_fault_lease_close", close)
    monkeypatch.setattr(runner, "_export_logs", export)
    monkeypatch.setattr(store, "complete_run", complete)
    monkeypatch.setattr(actuator, "emergency_stop", direct)
    monkeypatch.setattr(actuator, "write_fault_control", exact_write)
    monkeypatch.setattr(actuator, "read_fault_control_state", read)
    monkeypatch.setattr(queue, "put_nowait", put)
    monkeypatch.setattr(runner, "emergency_stop_must_bypass_terminal_writer", capability)
    monkeypatch.setattr(runner, "confirm_external_emergency_stop", confirm)
    monkeypatch.setattr(service, "_log_cancelled_terminal_estop_dispatch", consume_cancelled_waiter)
    monkeypatch.setattr(service, "_track_emergency_stop_dispatch", track)
    monkeypatch.setattr(service, "_admit_fault_control_lease", admit_lease)
    monkeypatch.setattr(arbiter, "admit_emergency_stop", admit)

    live_drops: asyncio.Task[OperatorActionResult] | None = None
    tick: asyncio.Task[bool] | None = None
    injected: asyncio.Task[bool] | None = None
    request: asyncio.Task[FaultControlsActionResult | OperatorActionResult] | None = None
    observers: list[asyncio.Task[bool]] = []
    dropped: OperatorActionResult | BaseException | None = None
    stopped: FaultControlsActionResult | OperatorActionResult | BaseException | None = None
    completed: bool | None = None
    registration_pending_at_dispatch = proof_held_at_dispatch = worker_unchanged = False
    original_ledger = runner._admitted_emergency_stops  # pyright: ignore[reportPrivateUsage]
    first = ""
    marker_while_reserved = False

    async def stop() -> FaultControlsActionResult | OperatorActionResult:
        if route == "run":
            return await service.submit_operator_action(
                run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
            )
        return await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )

    try:
        if registry in {"pending", "raised", "cancelled"}:
            live_drops = asyncio.create_task(
                service.submit_operator_action(
                    run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
                )
            )
            await asyncio.wait_for(held.wait(), 2)
            if registry == "raised":
                release_evaluation.set()
                dropped = await asyncio.wait_for(live_drops, 2)
            elif registry == "cancelled":
                (registered,) = tasks
                worker = executor._worker  # pyright: ignore[reportPrivateUsage]
                assert worker is not None
                registered.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await worker
                # The old executor worker leaves its per-request Future unresolved.
                live_drops.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await live_drops
        if interval != "none":
            accepted = await service.submit_operator_action(
                run_id,
                OperatorActionRequest(
                    action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
                    payload={"confirmation": True, "expected_generation": lease.generation},
                ),
            )
            assert accepted.queued
            tick = asyncio.create_task(runner.tick_once())
            await asyncio.wait_for(reserved.wait(), 2)
            marker_while_reserved = controller.human_drop_closed()
        if registry == "injected":
            injected = asyncio.create_task(release_evaluation.wait())
            tasks.add(injected)  # type: ignore[arg-type]
        if fault == "truthy":
            monkeypatch.setattr(runner, "emergency_stop_must_bypass_terminal_writer", lambda: 1)
        elif fault in {"missing", "mock"}:
            attributes = {
                "run_id": run_id,
                "finalized": False,
                "invalidate_fault_acknowledgement_for_emergency_stop": (
                    runner.invalidate_fault_acknowledgement_for_emergency_stop
                ),
                "admit_emergency_stop": runner.admit_emergency_stop,
                "dispatch_priority_emergency_stop": runner.dispatch_priority_emergency_stop,
                "confirm_external_emergency_stop": runner.confirm_external_emergency_stop,
                "rearm_emergency_stop_fail_safe": runner.rearm_emergency_stop_fail_safe,
                "controller_for_human_fault_drop": runner.controller_for_human_fault_drop,
            }
            narrow = SimpleNamespace(**attributes) if fault == "missing" else Mock(**attributes)
            monkeypatch.setattr(service, "runner", narrow)
        if fault == "missing_executor":
            monkeypatch.setattr(service, "_fault_control_executor", None)
        if fault == "action_audit_raises":
            monkeypatch.setattr(store, "record_operator_action", reject_action_audit)
        if (route, interval, registry, fault) == ("global", "barrier", "pending", "none"):
            cooling_samples.append(await cooling_sample())

        worker = executor._worker  # pyright: ignore[reportPrivateUsage]
        request = asyncio.create_task(stop())
        observers = [
            asyncio.create_task(event.wait())
            for event in (exact_written, unbound_attempted, estop_enqueued)
        ]
        observed, _ = await asyncio.wait_for(
            asyncio.wait([*observers, request], return_when=asyncio.FIRST_COMPLETED), 2
        )
        if estop_enqueued.is_set():
            first = "queued"
        elif unbound_attempted.is_set():
            first = "direct"
        elif exact_written.is_set():
            first = "priority"
        elif request in observed:
            first = "returned_without_dispatch"
        worker_unchanged = executor._worker is worker  # pyright: ignore[reportPrivateUsage]
        registration_pending_at_dispatch = any(not registered.done() for registered in tasks)
        proof_held_at_dispatch = not release_finalisation.is_set() and (
            tick is not None and not tick.done() and reserved.is_set()
        )
        if fault == "cancel_http":
            request.cancel()
            with suppress(asyncio.CancelledError):
                await request
        if cooling_samples:
            assert await service.settle_fault_lease_persistence_for_teardown()
            gate = service._fault_acknowledgement_gate  # pyright: ignore[reportPrivateUsage]
            reservation_after_stop = gate.fault_finalization_started
            if reservation_after_stop:
                cooling_samples.append(await cooling_sample())
            durable = await store.read_fault_control_lease()
            projection = await service._fault_controls_projection(durable)  # pyright: ignore[reportPrivateUsage]
            advertised_cooling_while_blocked = (
                OperatorAction.START_COOLING in projection.enabled_actions
            )
    finally:
        release_write.set()
        release_evaluation.set()
        release_finalisation.set()
        for observer in observers:
            observer.cancel()
        try:
            await asyncio.gather(*observers, return_exceptions=True)
            if injected is not None:
                try:
                    await asyncio.wait_for(injected, 2)
                finally:
                    tasks.discard(injected)  # type: ignore[arg-type]
            pending = [
                task
                for task in (live_drops, tick, request)
                if task is not None and not (task is live_drops and registry == "cancelled")
            ]
            awaitables: list[Awaitable[object]] = list(pending)
            outcomes = await asyncio.wait_for(
                asyncio.gather(*awaitables, return_exceptions=True), 2
            )
            for task, outcome in zip(pending, outcomes, strict=True):
                if task is live_drops:
                    assert isinstance(outcome, (OperatorActionResult, BaseException))
                    dropped = outcome
                elif task is tick:
                    if isinstance(outcome, BaseException):
                        raise outcome
                    assert isinstance(outcome, bool)
                    completed = outcome
                else:
                    assert isinstance(
                        outcome, (FaultControlsActionResult, OperatorActionResult, BaseException)
                    )
                    stopped = outcome
        finally:
            try:
                if dispatch_tasks:
                    await asyncio.wait_for(asyncio.gather(*tuple(dispatch_tasks)), 2)
            finally:
                try:
                    assert await service.settle_fault_lease_persistence_for_teardown()
                finally:
                    assert await executor.settle_for_shutdown()

    missing_binding = fault in {"missing_executor", "missing_session"}
    assert first == ("returned_without_dispatch" if missing_binding else expected_route), (
        "wrong emergency dispatch route observed"
    )
    if expected_route == "priority":
        assert worker_unchanged
        if registry == "pending":
            assert registration_pending_at_dispatch
        if interval.startswith("proof"):
            assert proof_held_at_dispatch
        assert exact_attempts == ([] if missing_binding else [actuator.session_id])
        assert dispatch_order == (
            [] if missing_binding else ["write"] if fault == "stop_raises" else ["write", "read"]
        )
    if interval != "none":
        assert completed is False
        assert not service._fault_acknowledgement_gate.fault_finalization_started  # pyright: ignore[reportPrivateUsage]
    assert marker_while_reserved is (interval in {"barrier", "close"})
    assert completion_operations == (["close"] if interval == "close" else [])
    assert admissions == 1
    assert not arbiter.emergency_stop_active
    expected_ledger_delta = int(expected_route == "queued")
    assert runner._admitted_emergency_stops == original_ledger + expected_ledger_delta  # pyright: ignore[reportPrivateUsage]
    assert direct_attempts == int(expected_route == "direct")
    assert unbound_attempted.is_set() is (expected_route == "direct")
    assert actuator.fault_writes == (
        [] if expected_route == "direct" or missing_binding else [OperatorAction.EMERGENCY_STOP]
    )
    assert estop_enqueued.is_set() is (expected_route == "queued")
    if expected_route == "direct":
        assert actuator.commands().count("emergency_stop") == 1
    if registry in {"pending", "raised"}:
        assert isinstance(dropped, OperatorActionResult) and dropped.result == "failed"
    if registry == "raised":
        (registered,) = tasks
        assert registered.done() and not registered.cancelled()
        assert isinstance(registered.exception(), RuntimeError)
    if registry == "cancelled":
        assert len(tasks) == 1 and next(iter(tasks)).cancelled()
    if fault not in {"missing", "mock", "truthy"}:
        assert capability_reads == int(interval != "none")
    if fault in {"capability_raises", "direct_audit_raises"}:
        assert "Could not qualify direct live emergency dispatch" in caplog.messages

    retry_armed = controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]
    if fault == "cancel_http":
        assert isinstance(stopped, asyncio.CancelledError)
        assert callback_consumed.is_set() and confirmations == 1
        assert not retry_armed
        if route == "run":
            assert dispatch_callbacks[-1:] == [consume_cancelled_waiter]
    elif fault in {"action_audit_raises", "direct_audit_raises"}:
        assert isinstance(stopped, RuntimeError)
        assert str(stopped) == (
            "synthetic operator-action audit failure"
            if fault == "action_audit_raises"
            else "synthetic emergency command audit failure"
        )
        assert confirmations == int(fault == "action_audit_raises")
        assert not retry_armed
        assert controller.phase is RoastPhase.FAULTED
    else:
        assert isinstance(stopped, (FaultControlsActionResult, OperatorActionResult))
        if missing_binding or fault in {"stop_raises", "read_raises", "read_hot"}:
            assert stopped.result == "failed"
            assert stopped.reason == "software e-stop attempt was not confirmed"
            assert retry_armed
        elif expected_route == "direct":
            assert stopped.result == "accepted"
            assert stopped.reason == "emergency_stop accepted for controller priority dispatch"
        else:
            assert stopped.result == ("confirmed" if route == "global" else "accepted")
            assert stopped.reason == "terminal emergency stop has fresh exact-session proof"
            assert not retry_armed
        assert not stopped.queued

    if route == "run":
        async with store.connection.execute(
            "SELECT result FROM operator_actions WHERE run_id = ? AND action = ?",
            (run_id, OperatorAction.EMERGENCY_STOP.value),
        ) as cursor:
            rows = [row[0] for row in await cursor.fetchall()]
        assert rows == ([stopped.result] if isinstance(stopped, OperatorActionResult) else [])
    latest = await store.read_fault_control_lease()
    assert latest.generation == lease.generation + 1
    if not missing_binding:
        assert latest.status is FaultLeaseStatus.OPEN
    if interval == "barrier":
        assert controller.human_drop_closed()
    if interval.startswith("proof") and expected_route != "direct":
        assert ack_writes == 0
        if actuator.cooling:
            assert ack_reads == (2 if interval == "proof2" else 1)
        timeline = await store.read_timeline(run_id)
        expected_reason = (
            "stop_unconfirmed" if interval == "proof2" else "superseded_by_emergency_stop"
        )
        assert any(
            event.kind is RoastEventKind.FAULT_ACKNOWLEDGEMENT_EXECUTED
            and event.payload is not None
            and event.payload.get("reason") == expected_reason
            for event in timeline.events
        )
    if cooling_samples:
        assert reservation_after_stop
        assert (
            cooling_samples
            == [
                (
                    "failed",
                    "fault finalization is closing the current exact-session lease",
                    "fault finalization is closing the current lease",
                )
            ]
            * 2
        )
        assert advertised_cooling_while_blocked
        cooling = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.START_COOLING)
        )
        assert cooling.result == "accepted"
        assert actuator.fault_writes == [
            OperatorAction.EMERGENCY_STOP,
            OperatorAction.START_COOLING,
        ]
        assert await executor.settle_for_shutdown()
    if fault == "command_audit_raises":
        assert audit_failures == 1 and confirmations == 1
        assert "could not persist fault-control command outcome" in caplog.messages
    if fault == "stop_raises":
        assert controller.phase is RoastPhase.FAULTED
        await runner.tick_once()
        assert actuator.calls == [
            ("set_heat", {"heat_percent": 0}),
            ("set_fan", {"fan_percent": SafetyLimits().overrun_safe_fan_percent}),
        ]
        assert exact_attempts == [actuator.session_id]


async def test_am5_completion_initiation_survives_runner_cancellation(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    entered, release = asyncio.Event(), asyncio.Event()

    async def close(*_args: object) -> _FinalisationCloseEvidence:
        entered.set()
        await release.wait()
        return _FinalisationCloseEvidence.COMMITTED

    monkeypatch.setattr(runner, "_fault_lease_close", close)
    assert not runner._completion_persistence_initiated  # pyright: ignore[reportPrivateUsage]
    admitted = await service.submit_operator_action(
        run_id,
        OperatorActionRequest(
            action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
            payload={"confirmation": True, "expected_generation": lease.generation},
        ),
    )
    assert admitted.queued
    tick = asyncio.create_task(runner.tick_once())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert runner._completion_persistence_initiated  # pyright: ignore[reportPrivateUsage]
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick
    finally:
        release.set()
        await asyncio.gather(tick, return_exceptions=True)
    assert runner._completion_persistence_initiated  # pyright: ignore[reportPrivateUsage]
    assert controller.human_drop_closed()
    assert actuator.fault_writes == []


@pytest.mark.parametrize("route", ["global", "run"])
async def test_am5_automatic_finalisation_keeps_exact_terminal_route_without_human_reads(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    clock = FakeClock()
    actuator = _ExactFaultRunner(clock)
    service = RoastService(store, roaster=actuator, run_loop=False, clock=clock)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner._controller  # pyright: ignore[reportPrivateUsage]
    controller.transition_to(RoastPhase.FAULTED)
    await runner.tick_once()
    lease = await store.open_fault_control_lease(run_id=run.id, mcp_session_id=actuator.session_id)
    reserved, release = asyncio.Event(), asyncio.Event()
    original_read = actuator.read_fault_acknowledgement_state
    reads = 0

    async def read_ack(session: str) -> FaultAcknowledgementState:
        reserved.set()
        await release.wait()
        return await original_read(session)

    def read_human_evidence() -> bool:
        nonlocal reads
        reads += 1
        return True

    ledger_consumer = Mock(wraps=runner.consume_admitted_emergency_stop_for_exact_dispatch)
    monkeypatch.setattr(
        runner, "consume_admitted_emergency_stop_for_exact_dispatch", ledger_consumer
    )
    flag = PropertyMock(return_value=False)
    monkeypatch.setattr(RoastRunner, "_completion_persistence_initiated", flag, raising=False)
    monkeypatch.setattr(controller, "human_drop_evaluation_outstanding", read_human_evidence)
    monkeypatch.setattr(actuator, "read_fault_acknowledgement_state", read_ack)
    admission = await service.submit_operator_action(
        run.id,
        OperatorActionRequest(
            action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
            payload={"confirmation": True, "expected_generation": lease.generation},
        ),
    )
    assert admission.queued
    tick = asyncio.create_task(runner.tick_once())
    try:
        await asyncio.wait_for(reserved.wait(), 2)
        if route == "global":
            stopped = await service.submit_fault_controls_action(
                FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
            )
        else:
            stopped = await service.submit_operator_action(
                run.id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
            )
    finally:
        release.set()
        completed = await asyncio.wait_for(tick, 2)
        assert await service.settle_fault_lease_persistence_for_teardown()
        executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
        assert executor is not None and await executor.settle_for_shutdown()
    assert reads == 0
    assert flag.call_args_list == []
    assert not completed
    ledger_consumer.assert_not_called()
    if route == "global":
        assert stopped.result == "confirmed"
        assert stopped.reason == "terminal emergency stop has fresh exact-session proof"
        assert actuator.fault_writes == [OperatorAction.EMERGENCY_STOP]
        assert actuator.commands().count("emergency_stop") == 0
    else:
        assert stopped.result == "accepted"
        assert stopped.reason == "emergency_stop accepted for controller priority dispatch"
        assert actuator.fault_writes == []
        assert actuator.commands().count("emergency_stop") == 1


@pytest.mark.parametrize("runner_case", ["finalised", "different", "absent"])
async def test_am5_historical_global_estop_never_consults_unselected_runner(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    runner_case: str,
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    reads = 0

    def non_live_capability() -> bool:
        nonlocal reads
        reads += 1
        return True

    monkeypatch.setattr(runner, "emergency_stop_must_bypass_terminal_writer", non_live_capability)
    await service._read_fault_lease_fail_closed()  # pyright: ignore[reportPrivateUsage]
    if runner_case == "finalised":
        admitted = await service.submit_operator_action(
            run_id,
            OperatorActionRequest(
                action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
                payload={"confirmation": True, "expected_generation": lease.generation},
            ),
        )
        assert admitted.queued
        assert await runner.tick_once()
        assert runner.finalized
    elif runner_case == "different":
        monkeypatch.setattr(service, "active_run_id", "another-run")
    else:
        monkeypatch.setattr(service, "runner", None)
    gate = service._fault_acknowledgement_gate  # pyright: ignore[reportPrivateUsage]
    gate.fault_finalization_started = True
    stopped = await service.submit_fault_controls_action(
        FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
    )
    assert await service.settle_fault_lease_persistence_for_teardown()
    executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
    assert executor is not None and await executor.settle_for_shutdown()
    assert reads == 0
    assert stopped.result == "confirmed"
    assert stopped.reason == "terminal emergency stop has fresh exact-session proof"
    assert actuator.fault_writes == [OperatorAction.EMERGENCY_STOP]


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("cancel_waiting", id="TH9"),
        pytest.param("serialize", id="TH10"),
        pytest.param("stale_before_write", id="TH12-before-write"),
        pytest.param("stale_after_write", id="TH12-after-write"),
    ],
)
async def test_am6_priority_arbiter_wait_and_supersession(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Observe admission into the shared arbiter before cancelling or superseding."""
    service, runner, actuator, _, lease = await _faulted_run(store)
    arbiter = service._command_arbiter  # pyright: ignore[reportPrivateUsage]
    executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
    gate = service._fault_acknowledgement_gate  # pyright: ignore[reportPrivateUsage]
    dispatch_tasks = service._emergency_stop_dispatch_tasks  # pyright: ignore[reportPrivateUsage]
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None and executor is not None and lease is not None
    actuator.reject_unbound_inactive_emergency = True
    tasks = controller._human_drop_evaluation_tasks  # pyright: ignore[reportPrivateUsage]
    # White-box reservation and still-owned evaluation. D212's proof and
    # completion intervals are exercised through their actual entry points above.
    release_evaluation = asyncio.Event()

    async def hold_evaluation() -> int:
        await release_evaluation.wait()
        return 1

    evaluation = asyncio.create_task(hold_evaluation())
    tasks.add(evaluation)
    assert gate.begin_fault_finalization(gate.emergency_request_epoch)

    in_flight, release_in_flight, first_write, release_first_write = (
        asyncio.Event() for _ in range(4)
    )
    first_waiting, second_waiting, old_routine_waiting = (asyncio.Event() for _ in range(3))
    waiting_dispatches: list[asyncio.Task[object]] = []
    order: list[str] = []
    routine: asyncio.Task[tuple[bool, None]] | None = None
    old_routine: asyncio.Task[tuple[bool, None]] | None = None
    request_a: asyncio.Task[FaultControlsActionResult] | None = None
    request_b: asyncio.Task[FaultControlsActionResult] | None = None
    condition = arbiter._condition  # pyright: ignore[reportPrivateUsage]
    original_wait = condition.wait
    original_write = actuator.write_fault_control
    before_release: list[str] = []

    async def wait() -> bool:
        task = asyncio.current_task()
        if task is not None and task in dispatch_tasks and task not in waiting_dispatches:
            waiting_dispatches.append(task)
            (first_waiting if len(waiting_dispatches) == 1 else second_waiting).set()
        elif task is old_routine:
            old_routine_waiting.set()
        return await original_wait()

    async def in_flight_write() -> None:
        order.append("routine_enter")
        in_flight.set()
        await release_in_flight.wait()
        order.append("routine_exit")

    async def old_routine_write() -> None:
        order.append("stale_routine_write")

    async def following_routine_write() -> None:
        order.append("following_routine_write")

    async def exact_write(action: OperatorAction, *, expected_session_id: str) -> None:
        if action is OperatorAction.EMERGENCY_STOP:
            order.append("priority_write")
            if not first_write.is_set():
                first_write.set()
                if case == "stale_after_write":
                    await release_first_write.wait()
        await original_write(action, expected_session_id=expected_session_id)

    async def stop() -> FaultControlsActionResult:
        return await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )

    monkeypatch.setattr(condition, "wait", wait)
    monkeypatch.setattr(actuator, "write_fault_control", exact_write)
    result_a: FaultControlsActionResult | BaseException | None = None
    result_b: FaultControlsActionResult | BaseException | None = None
    old_routine_result: tuple[bool, None] | None = None
    current_routine_result: tuple[bool, None] | None = None
    try:
        if case != "stale_after_write":
            routine = asyncio.create_task(arbiter.run_routine(arbiter.generation, in_flight_write))
            await asyncio.wait_for(in_flight.wait(), 2)
        if case == "serialize":
            old_routine = asyncio.create_task(
                arbiter.run_routine(arbiter.generation, old_routine_write)
            )
            await asyncio.wait_for(old_routine_waiting.wait(), 2)
        request_a = asyncio.create_task(stop())
        if case == "stale_after_write":
            await asyncio.wait_for(first_write.wait(), 2)
        else:
            waiting_observers = [
                asyncio.create_task(event.wait()) for event in (first_waiting, first_write)
            ]
            try:
                await asyncio.wait_for(
                    asyncio.wait(
                        [*waiting_observers, request_a], return_when=asyncio.FIRST_COMPLETED
                    ),
                    2,
                )
            finally:
                for observer in waiting_observers:
                    observer.cancel()
                await asyncio.gather(*waiting_observers, return_exceptions=True)
        if case == "cancel_waiting" and first_waiting.is_set():
            (waiting_dispatch,) = waiting_dispatches
            waiting_dispatch.cancel()
            with suppress(asyncio.CancelledError):
                await request_a
        elif case == "stale_after_write" or (
            case == "stale_before_write" and first_waiting.is_set()
        ):
            request_b = asyncio.create_task(stop())
            waiting = second_waiting if case == "stale_before_write" else first_waiting
            await asyncio.wait_for(waiting.wait(), 2)
        if case == "serialize" and first_waiting.is_set():
            current_routine_result = await arbiter.run_routine(
                arbiter.generation, following_routine_write
            )
        before_release = order.copy()
    finally:
        release_in_flight.set()
        release_first_write.set()
        release_evaluation.set()
        try:
            if routine is not None:
                await asyncio.wait_for(routine, 2)
            if old_routine is not None:
                old_routine_result = await asyncio.wait_for(old_routine, 2)
            if request_a is not None:
                (result_a,) = await asyncio.wait_for(
                    asyncio.gather(request_a, return_exceptions=True), 2
                )
            if request_b is not None:
                (result_b,) = await asyncio.wait_for(
                    asyncio.gather(request_b, return_exceptions=True), 2
                )
        finally:
            await asyncio.wait_for(evaluation, 2)
            tasks.discard(evaluation)
            gate.abort_fault_finalization()
            try:
                assert await service.settle_fault_lease_persistence_for_teardown()
            finally:
                assert await executor.settle_for_shutdown()

    assert actuator.commands().count("emergency_stop") == 0
    assert not arbiter.emergency_stop_active
    retry_armed = controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]
    if case == "cancel_waiting":
        assert before_release == ["routine_enter"]
        assert isinstance(result_a, asyncio.CancelledError)
        assert actuator.fault_writes == [] and retry_armed
    elif case == "serialize":
        assert before_release == ["routine_enter"]
        assert old_routine_result == current_routine_result == (False, None)
        assert isinstance(result_a, FaultControlsActionResult) and result_a.result == "confirmed"
        assert await arbiter.run_routine(arbiter.generation, following_routine_write) == (
            True,
            None,
        )
        assert order == [
            "routine_enter",
            "routine_exit",
            "priority_write",
            "following_routine_write",
        ]
        assert actuator.fault_writes == [OperatorAction.EMERGENCY_STOP] and not retry_armed
    else:
        assert before_release == (
            ["routine_enter"] if case == "stale_before_write" else ["priority_write"]
        )
        assert isinstance(result_a, FaultControlsActionResult) and result_a.result == "failed"
        assert isinstance(result_b, FaultControlsActionResult) and result_b.result == "confirmed"
        assert actuator.fault_writes == [OperatorAction.EMERGENCY_STOP] * (
            1 if case == "stale_before_write" else 2
        )
        if case == "stale_after_write":
            assert retry_armed


async def test_am6_exact_dispatch_ledger_consumption_floors_at_zero(store: RoastStore) -> None:
    """TH11 pins the defensive zero-floor without admitting an arbiter token."""
    service, runner, _, _, _ = await _faulted_run(store)
    arbiter = service._command_arbiter  # pyright: ignore[reportPrivateUsage]
    assert runner._admitted_emergency_stops == 0  # pyright: ignore[reportPrivateUsage]
    runner.consume_admitted_emergency_stop_for_exact_dispatch()
    assert runner._admitted_emergency_stops == 0  # pyright: ignore[reportPrivateUsage]
    assert not arbiter.emergency_stop_active
