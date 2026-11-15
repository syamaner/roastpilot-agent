"""HUMAN runner turn ordering and service lifecycle ownership witnesses."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from roastpilot_agent.api import (
    QueuedOperatorAction,
    RoastRunConflictError,
    RoastService,
    ShutdownHeatOffOutcome,
    create_app,
    stream_events,
)
from roastpilot_agent.config import AppConfig, ControllerConfig, MCPDeviceConfig
from roastpilot_agent.human_protection import (
    CommandDisposition,
    ContainmentOutcome,
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
    FaultControlState,
    OperatorAction,
    OperatorActionRequest,
    RestartClearanceState,
    RoastPhase,
)
from roastpilot_agent.store import RoastStore
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
                return _result({"recording_origin": "synthetic"})
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


async def test_t51b_malformed_ambient_is_persisted_as_invalid_not_sample(
    store: RoastStore,
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)
    detail = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    state = RoastSessionState.model_validate(SESSION_STATE_PAYLOAD)
    malformed = state.ambient_status.model_copy(update={"temperature_c": float("nan")})
    actuator.last_state = state.model_copy(update={"ambient_status": malformed})
    clock.advance(0.25)
    await runner.tick_once()
    async with store.connection.execute(
        "SELECT reason FROM safety_evaluations WHERE run_id = ?", (detail.id,)
    ) as cursor:
        rows = await cursor.fetchall()
    assert any("ambient invalid, ambient age seconds None" in row[0] for row in rows)


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
