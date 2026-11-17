"""HUMAN runner turn ordering and service lifecycle ownership witnesses."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from contextlib import aclosing, suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, Mock, PropertyMock

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from roastpilot_agent.api import (
    EventBroadcaster,
    QueuedOperatorAction,
    RoastRunConflictError,
    RoastRunner,
    RoastService,
    ShutdownHeatOffOutcome,
    _BufferedEvent,  # pyright: ignore[reportPrivateUsage]
    _FinalisationCloseEvidence,  # pyright: ignore[reportPrivateUsage]
    create_app,
    stream_events,
)
from roastpilot_agent.config import AppConfig, ControllerConfig, MCPDeviceConfig, SafetyLimits
from roastpilot_agent.human_persistence import (
    CommandAuditOwner,
    HistoryUnavailable,
    Settlement,
    StorageCaller,
    StorageDeadline,
    current_storage_caller,
    storage_caller,
)
from roastpilot_agent.human_protection import (
    CommandDisposition,
    ContainmentAttempt,
    ContainmentOutcome,
    ContainmentReason,
    ContainmentResult,
    ControlAuthority,
    EndpointDisposition,
    HumanReadEvidence,
    RawTemperature,
    ReadingFreshness,
    ToolDiscovery,
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
    FaultControlsProjection,
    FaultControlState,
    FaultCoolingStopResult,
    FaultLeaseStatus,
    HumanHistoryDurability,
    OperatorAction,
    OperatorActionRequest,
    OperatorActionResult,
    RestartClearanceState,
    RoastCommand,
    RoastDetail,
    RoastEventKind,
    RoastPhase,
    RoastTimeline,
    SseEventType,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict
from roastpilot_agent.store import (
    FaultControlCloseResult,
    FaultControlLease,
    PersistedRun,
    RoastStore,
)
from tests.conftest import FakeClock, FakeMCPClient
from tests.test_api import (
    _collect_frames,  # pyright: ignore[reportPrivateUsage]
    _FakeRequest,  # pyright: ignore[reportPrivateUsage]
    _parse_frame,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_controller import PROFILE, reading
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
    store: RoastStore,
    clock: FakeClock,
    actuator: _RunnerRoaster,
    *,
    exact_child: bool = False,
    authority: ControlAuthority = ControlAuthority.HUMAN,
) -> RoastService:
    child = _LifecycleSpy(_Session(_result({}))) if exact_child else None
    if child is not None:
        actuator.epoch = child.child_epoch
    return RoastService(
        store,
        mcp=child,
        roaster=actuator,
        raw_state=actuator,
        authority=authority,
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
    detail = await service.detail("persisted-fault")
    assert _protection_fields(detail.model_dump(mode="json")) == {
        "human_endpoint": "history_unknown",
        "human_command_disposition": "unknown",
        "physical_intervention_required": True,
        "emergency_fallback_disclosure": _FALLBACK_DISCLOSURE,
    }
    stream = await stream_events("persisted-fault", cast(Request, _FakeRequest()), service)
    frames = await asyncio.wait_for(_collect_frames(stream, 2), 1)
    assert _protection_heartbeat(frames[1].encode(), "persisted-fault") == _protection_fields(
        detail.model_dump(mode="json")
    )
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
    frames = await asyncio.wait_for(_collect_frames(response, 3), 1)
    phase = _parse_frame(frames[2])
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
    frames = await asyncio.wait_for(_collect_frames(response, 3), 1)
    obsolete_data = cast("dict[str, object]", _parse_frame(frames[2])["data"])
    assert obsolete_data["phase"] == obsolete_phase.value
    actions = cast("list[str]", obsolete_data["enabled_actions"])
    assert OperatorAction.DROP_BEANS.value not in actions
    assert set(obsolete_data) == {"phase", "enabled_actions"}


_PROTECTION_CLEAR: dict[str, object] = {
    "human_endpoint": None,
    "human_command_disposition": None,
    "physical_intervention_required": None,
    "emergency_fallback_disclosure": None,
}
_FALLBACK_DISCLOSURE = (
    "Emergency fallback may stop the drum and close the chute. "
    "Admission or RPC return does not establish physical evacuation."
)


def _protection_fields(body: dict[str, object]) -> dict[str, object]:
    return {key: body[key] for key in _PROTECTION_CLEAR}


def _protection_heartbeat(chunk: bytes, run_id: str) -> dict[str, object]:
    frame = _parse_frame(chunk.decode())
    assert frame["event"] == "heartbeat"
    assert frame["id"] is None
    data = cast("dict[str, object]", frame["data"])
    assert set(data) == {"run_id", "human_history_durability", *_PROTECTION_CLEAR}
    assert data["human_history_durability"] in (
        None,
        "complete",
        "delayed",
        "incomplete",
        "unknown",
        "unavailable",
    )
    assert data["run_id"] == run_id
    return _protection_fields(data)


@pytest.mark.parametrize("endpoint", list(EndpointDisposition))
@pytest.mark.parametrize("command", list(CommandDisposition))
async def test_human_protection_wire_vocabulary_is_exact_and_read_only(
    store: RoastStore, endpoint: EndpointDisposition, command: CommandDisposition
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    ownership = controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert ownership is not None
    # Sample the complete output vocabulary, including intermediate command
    # states that need not stay resident long enough for an ordinary GET.
    ownership.endpoint = endpoint
    ownership.command = command
    expected = {
        "human_endpoint": endpoint.value,
        "human_command_disposition": command.value,
        "physical_intervention_required": (
            command is CommandDisposition.UNKNOWN or endpoint is EndpointDisposition.WITHHELD
        ),
        "emergency_fallback_disclosure": _FALLBACK_DISCLOSURE,
    }
    writes_before = store.connection.total_changes
    calls_before = list(actuator.calls)
    reads_before = actuator.reads
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service)), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/roasts/{run.id}")
    assert response.status_code == 200
    assert _protection_fields(response.json()) == expected
    stream = await stream_events(run.id, cast(Request, _FakeRequest()), service)
    async with aclosing(cast("AsyncGenerator[bytes, None]", stream.body_iterator)) as frames:
        assert await anext(frames) == b": connected\n\n"
        assert _protection_heartbeat(await anext(frames), run.id) == expected
    assert store.connection.total_changes == writes_before
    assert actuator.calls == calls_before
    assert actuator.reads == reads_before


async def test_human_stream_uses_current_disclosure_before_replay_and_clears_on_replacement(
    store: RoastStore,
) -> None:
    clock = FakeClock()
    service = _service(store, clock, _RunnerRoaster(clock, []))
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    ownership = controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert ownership is not None
    resume_from = service.events.last_event_id
    service.events.emit(
        RoastEventKind.RECOVERY_REQUIRED,
        {"endpoint": "unknown", "physical_intervention_required": True},
    )
    stream = await stream_events(
        run.id, cast(Request, _FakeRequest(last_event_id_header=str(resume_from))), service
    )
    async with aclosing(cast("AsyncGenerator[bytes, None]", stream.body_iterator)) as frames:
        assert await anext(frames) == b": connected\n\n"
        initial = _protection_heartbeat(await anext(frames), run.id)
        assert initial["human_endpoint"] == "none"
        assert initial["physical_intervention_required"] is False
        historical = _parse_frame((await anext(frames)).decode())
        assert historical["id"] == resume_from + 1
        assert historical["data"] == {"endpoint": "unknown", "physical_intervention_required": True}

        # Still bound after durable closure, including the retained WITHHELD
        # intervention. A following explicit Start replaces that binding.
        ownership.endpoint = EndpointDisposition.WITHHELD
        await store.complete_run(
            run_id=run.id, outcome="completed", agent_phase=RoastPhase.COMPLETE
        )
        runner._finalized = True  # pyright: ignore[reportPrivateUsage]
        controller.note_human_run_durably_closed()
        assert not ownership.unresolved
        retained = _protection_fields((await service.detail(run.id)).model_dump(mode="json"))
        assert retained["human_endpoint"] == "withheld"
        assert retained["physical_intervention_required"] is True
        service.events.emit(RoastEventKind.RUN_COMPLETED, {})
        assert _protection_heartbeat(await anext(frames), run.id) == retained
        assert _parse_frame((await anext(frames)).decode())["event"] == "run_completed"

        new_run = await service.start_roast(PROFILE)
        assert new_run.id != run.id
        assert _protection_heartbeat(await anext(frames), run.id) == _PROTECTION_CLEAR
        assert _parse_frame((await anext(frames)).decode())["event"] != "heartbeat"
        assert (
            _protection_fields((await service.detail(run.id)).model_dump(mode="json"))
            == _PROTECTION_CLEAR
        )


@pytest.mark.parametrize("replacement", ["absent", "different_runner", "orphan_same_id"])
async def test_human_detail_rechecks_ownership_after_last_await(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    clock = FakeClock()
    service = _service(store, clock, _ExactFaultRunner(clock), exact_child=True)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    await store.open_fault_control_lease(run_id=run.id, mcp_session_id="test-session")
    entered, release = asyncio.Event(), asyncio.Event()
    project = service._fault_controls_projection  # pyright: ignore[reportPrivateUsage]

    async def hold_projection(lease: FaultControlLease) -> FaultControlsProjection:
        result = await project(lease)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(service, "_fault_controls_projection", hold_projection)
    pending = asyncio.create_task(service.detail(run.id))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not pending.done()
        if replacement == "absent":
            service.runner = None
        elif replacement == "different_runner":
            foreign = _service(store, clock, _RunnerRoaster(clock, []))
            service.runner = await foreign._build_runner("foreign-run", PROFILE)  # pyright: ignore[reportPrivateUsage]
        else:
            # Same visible run ID, obsolete controller record held by the
            # runner. String equality alone would leak its disclosure.
            controller.note_human_run_durably_closed()
            service._human_ownership.claim_run(run.id)  # pyright: ignore[reportPrivateUsage]
    finally:
        release.set()
        detail = await pending
    assert _protection_fields(detail.model_dump(mode="json")) == _PROTECTION_CLEAR
    stream = await stream_events(run.id, cast(Request, _FakeRequest()), service)
    async with aclosing(cast("AsyncGenerator[bytes, None]", stream.body_iterator)) as frames:
        await anext(frames)
        assert _protection_heartbeat(await anext(frames), run.id) == _PROTECTION_CLEAR


async def test_existing_stream_heartbeat_does_not_wait_for_rest_read_run(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    service = _service(store, clock, _RunnerRoaster(clock, []))
    service.sse_heartbeat_seconds = 0.01
    run = await service.start_roast(PROFILE)
    stream = await stream_events(run.id, cast(Request, _FakeRequest()), service)
    async with aclosing(cast("AsyncGenerator[bytes, None]", stream.body_iterator)) as frames:
        await anext(frames)
        expected = _protection_heartbeat(await anext(frames), run.id)
        entered, release = asyncio.Event(), asyncio.Event()
        read_run = store.read_run

        async def blocked_read(run_id: str) -> RoastDetail | None:
            entered.set()
            await release.wait()
            return await read_run(run_id)

        monkeypatch.setattr(store, "read_run", blocked_read)
        pending = asyncio.create_task(service.detail(run.id))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not pending.done()
            assert (
                _protection_heartbeat(await asyncio.wait_for(anext(frames), 1), run.id) == expected
            )
            assert not pending.done()
        finally:
            release.set()
            received = await pending
        assert _protection_fields(received.model_dump(mode="json")) == expected


async def test_busy_human_stream_observes_each_settled_history_turn(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = RoastService(
        store,
        config=AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=0.05)),
        roaster=actuator,
        raw_state=actuator,
        authority=ControlAuthority.HUMAN,
        clock=clock,
        run_loop=False,
    )
    service.sse_heartbeat_seconds = 15
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    clock.advance(0.25)
    await runner.tick_once()
    assert runner.history_durability() is HumanHistoryDurability.COMPLETE
    real_commit = store.connection.commit
    entered, release = asyncio.Event(), asyncio.Event()
    tick: asyncio.Task[bool] | None = None

    async def commit() -> None:
        native = store._current_native_storage()  # pyright: ignore[reportPrivateUsage]
        if native is not None and native.receipt.kind == "record_telemetry":
            entered.set()
            await release.wait()
        await real_commit()

    def history(chunk: bytes) -> object:
        frame = _parse_frame(chunk.decode())
        assert frame["event"] == "heartbeat" and frame["id"] is None
        data = cast("dict[str, object]", frame["data"])
        assert data["run_id"] == run.id
        return data["human_history_durability"]

    monkeypatch.setattr(store.connection, "commit", commit)
    try:
        response = await stream_events(run.id, cast(Request, _FakeRequest()), service)
        async with aclosing(cast("AsyncGenerator[bytes, None]", response.body_iterator)) as frames:
            await anext(frames)
            assert history(await anext(frames)) == "complete"
            for expected in ("complete", "complete", "delayed"):
                entered.clear()
                release.clear()
                clock.advance(0.25)
                next_frame = asyncio.create_task(anext(frames))
                tick = asyncio.create_task(runner.tick_once())
                await asyncio.wait_for(entered.wait(), 1)
                assert history(await asyncio.wait_for(next_frame, 1)) == "unknown"
                async with asyncio.timeout(1):
                    while True:
                        frame = _parse_frame((await anext(frames)).decode())
                        assert frame["id"] is not None
                        if frame["event"] == "telemetry":
                            break
                assert runner.history_durability() is HumanHistoryDurability.UNKNOWN
                settled_frame = asyncio.create_task(anext(frames))
                if expected == "delayed":
                    clock.advance(1.0)
                release.set()
                await asyncio.wait_for(tick, 1)
                assert history(await asyncio.wait_for(settled_frame, 1)) == expected
        # A later successful exact-run write cannot heal a positively missing audit.
        store.note_human_history_gap("record_event", run.id, detail="known_missing_test_audit")
        assert runner.history_durability() is HumanHistoryDurability.INCOMPLETE
        await store.update_run_phase(run.id, runner.controller_snapshot().phase)
        assert any(
            r.kind == "update_run_phase"
            and r.run_id == run.id
            and r.timely is True
            and r.settlement is Settlement.OBSERVED_COMMITTED
            for r in store.drain_human_storage_receipts()
        )
        assert runner.history_durability() is HumanHistoryDurability.INCOMPLETE
    finally:
        release.set()
        if tick is not None:
            await tick
        await service.shutdown()


async def test_quiet_human_stream_keeps_heartbeat_deadline_during_settlement_wakes(
    store: RoastStore,
) -> None:
    clock = FakeClock()
    service = _service(store, clock, _RunnerRoaster(clock, []))
    service.sse_heartbeat_seconds = 0.05
    run = await service.start_roast(PROFILE)
    count = 0

    async def notify() -> None:
        nonlocal count
        while True:
            service.events.notify_human_history()
            count += 1
            await asyncio.sleep(0.005)

    try:
        response = await stream_events(run.id, cast(Request, _FakeRequest()), service)
        async with aclosing(cast("AsyncGenerator[bytes, None]", response.body_iterator)) as frames:
            assert await anext(frames) == b": connected\n\n"
            first = await anext(frames)
            notifications = asyncio.create_task(notify())
            try:
                next_frame = await asyncio.wait_for(anext(frames), 0.3)
                assert next_frame == first
                assert count > 1 and not notifications.done()
            finally:
                notifications.cancel()
                with suppress(asyncio.CancelledError):
                    await notifications
    finally:
        await service.shutdown()


@pytest.mark.parametrize("stall_sink", [True, False])
async def test_current_human_stream_updates_without_telemetry_or_store_progress(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, stall_sink: bool
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    service.sse_heartbeat_seconds = 0.01
    run = await service.start_roast(PROFILE)
    await _charge_live_runner(service, actuator, clock)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    if not stall_sink:
        controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    stream = await stream_events(run.id, cast(Request, _FakeRequest()), service)
    async with aclosing(cast("AsyncGenerator[bytes, None]", stream.body_iterator)) as frames:
        await anext(frames)
        assert (
            _protection_heartbeat(await anext(frames), run.id)["physical_intervention_required"]
            is False
        )
        started, release = asyncio.Event(), asyncio.Event()
        original_read = actuator.read_human_evidence

        async def unusable_read() -> HumanReadEvidence:
            if not stall_sink:
                raise RuntimeError("test read failure")
            return replace(
                await original_read(), telemetry=None, env=RawTemperature.INVALID, env_temp_c=None
            )

        async def blocked_write(**_kwargs: object) -> int:
            started.set()
            await release.wait()
            return 1

        monkeypatch.setattr(actuator, "read_human_evidence", unusable_read)
        if stall_sink:
            monkeypatch.setattr(store, "record_safety_evaluation", blocked_write)
        clock.advance(0.25)
        tick = asyncio.create_task(runner.tick_once())
        try:
            if stall_sink:
                await asyncio.wait_for(started.wait(), 1)
                assert not tick.done()
            else:
                await asyncio.wait_for(asyncio.shield(tick), 1)
            assert runner.controller_snapshot().telemetry is None
            next_frame = await asyncio.wait_for(anext(frames), 1)
            current = _protection_heartbeat(next_frame, run.id)
            assert current["human_endpoint"] == "none"
            assert current["physical_intervention_required"] is True
        finally:
            release.set()
            await tick


async def test_projection_evaluator_failure_keeps_tick_row_and_shutdown(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
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
    controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)

    evaluator = Mock(side_effect=RuntimeError("private exception value"))
    monkeypatch.setattr(controller, "_human_recovery_drop_evaluation", evaluator)
    clock.advance(6.0)
    await runner.tick_once()
    points = await store.read_telemetry_points(run.id, downsample=1)
    assert points[-1].tick == runner.current_tick
    assert points[-1].agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert points[-1].bean_temp_c == actuator.bean
    detail = await service.detail(run.id)
    assert detail.physical_intervention_required is True
    assert OperatorAction.DROP_BEANS not in detail.enabled_actions

    def forbidden(_: HumanCallAdmissionFence) -> None:
        pytest.fail("snapshot read the emergency-path admission fence")

    with monkeypatch.context() as patch:
        patch.setattr(HumanCallAdmissionFence, "closed_reason", property(forbidden))
        patch.setattr(
            service, "_human_child_teardown_unconfirmed", Mock(side_effect=AssertionError)
        )
        assert await service.safe_shutdown_heat_off() is ShutdownHeatOffOutcome.NOT_ACTIVE
    assert evaluator.call_count >= 3
    assert "Could not evaluate HUMAN recovery drop projection" in caplog.text
    assert "private exception value" not in caplog.text


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
    assert detail.physical_intervention_required is False
    state = controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert state is not None
    state.endpoint = EndpointDisposition.WITHHELD
    withheld = await service.detail(run.id)
    assert withheld.physical_intervention_required is True
    assert withheld.enabled_actions == detail.enabled_actions
    assert withheld.fault_controls == detail.fault_controls
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


@pytest.mark.parametrize(
    ("turn_interval", "expect_recorded"),
    [
        pytest.param(1.0, False, id="stale-reading-outside-storage-budget"),
        pytest.param(25.0, True, id="stale-reading-inside-storage-budget"),
    ],
)
async def test_t51_stale_read_recovery_and_diagnostics_follow_turn_budget(
    store: RoastStore, turn_interval: float, expect_recorded: bool
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = RoastService(
        store,
        config=AppConfig(
            controller=ControllerConfig(t0_debounce_ticks=1, tick_interval_seconds=turn_interval)
        ),
        roaster=actuator,
        raw_state=actuator,
        authority=ControlAuthority.HUMAN,
        clock=clock,
        run_loop=False,
    )
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
    assert (
        any(
            "BT valid, ET valid, accepted reading stale, conservative age seconds 20.25" in reason
            for reason in reasons
        )
        is expect_recorded
    )
    current_evaluations = [
        receipt
        for receipt in store.drain_human_storage_receipts()
        if receipt.kind == "record_safety_evaluation" and receipt.turn == runner.current_tick
    ]
    assert current_evaluations
    if expect_recorded:
        assert all(
            receipt.writer_entered is True and receipt.settlement is Settlement.OBSERVED_COMMITTED
            for receipt in current_evaluations
        )
    else:
        assert all(
            receipt.writer_entered is False and receipt.settlement is Settlement.VERIFIED_NO_WRITE
            for receipt in current_evaluations
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
        native = store._current_native_storage()  # pyright: ignore[reportPrivateUsage]
        if (
            settlement == "commit_raised"
            and native is not None
            and native.receipt.kind == "record_safety_evaluation"
            and release.is_set()
            and "evaluation INSERT reached COMMIT" not in order
        ):
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
    frames = await asyncio.wait_for(_collect_frames(response, 3), 1)
    assert _parse_frame(frames[2])["data"] == {
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
    # The acknowledgement loss leaves the terminal attempt fenced. A later
    # ordinary turn cannot retry it and must leave the run open for recovery.
    assert not await runner.tick_once()
    assert not runner.finalized
    detail = await store.read_run(run_id)
    assert detail is not None and detail.completed_at_utc is None
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
        if tool is RoastCommand.EMERGENCY_STOP and fault == "command_audit_raises":
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
        if fault == "capability_raises":
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
            # The e-stop returns independently of its audit. Observe the exact
            # lease/history projection only after the registered audit settles.
            await asyncio.wait_for(asyncio.shield(request), 2)
            assert await service.settle_fault_lease_persistence_for_teardown()
            assert await executor.settle_audit_tasks()
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
    if fault == "capability_raises":
        assert "Could not qualify direct live emergency dispatch" in caplog.messages

    retry_armed = controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]
    if fault == "cancel_http":
        assert isinstance(stopped, asyncio.CancelledError)
        assert callback_consumed.is_set() and confirmations == 1
        assert not retry_armed
        if route == "run":
            assert dispatch_callbacks[-1:] == [consume_cancelled_waiter]
    elif fault == "action_audit_raises":
        assert isinstance(stopped, OperatorActionResult)
        assert stopped.result == "accepted"
        assert stopped.reason == "terminal emergency stop has fresh exact-session proof"
        assert confirmations == 1
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
        if not isinstance(stopped, OperatorActionResult):
            assert rows == []
        elif rows:
            assert rows == [stopped.result]
        else:
            assert any(
                receipt.run_id == run_id
                and receipt.kind == "record_operator_action"
                and receipt.detail == OperatorAction.EMERGENCY_STOP.value
                and receipt.settlement
                is (
                    Settlement.UNRESOLVED
                    if fault == "action_audit_raises"
                    else Settlement.VERIFIED_NO_WRITE
                )
                for receipt in store.drain_human_storage_receipts()
            )
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
        if not any(
            event.kind is RoastEventKind.FAULT_ACKNOWLEDGEMENT_EXECUTED
            and event.payload is not None
            and event.payload.get("reason") == expected_reason
            for event in timeline.events
        ):
            assert any(
                receipt.run_id == run_id
                and receipt.kind == "record_event"
                and receipt.detail == RoastEventKind.FAULT_ACKNOWLEDGEMENT_EXECUTED.value
                and receipt.settlement is Settlement.VERIFIED_NO_WRITE
                for receipt in store.drain_human_storage_receipts()
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
            # Settle the prior lease audit while its driver attempt is still
            # held. This witness exercises actuator ordering rather than the
            # independently tested rejection of competing SQLite writers.
            assert await service.settle_fault_lease_persistence_for_teardown()
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


@pytest.mark.parametrize(
    "failed_write",
    [
        pytest.param(False, id="TG10D-success"),
        pytest.param(True, id="TG10D-failed-write-audit"),
    ],
)
async def test_am6_per_run_capability_doubt_preserves_direct_command_audit(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, failed_write: bool
) -> None:
    """Pin D-28 in the default fake, including actual audit failure propagation."""
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    controller = runner.controller_for_human_fault_drop()
    executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
    gate = service._fault_acknowledgement_gate  # pyright: ignore[reportPrivateUsage]
    assert controller is not None and executor is not None and lease is not None
    assert not actuator.reject_unbound_inactive_emergency

    proof_held, release_proof = asyncio.Event(), asyncio.Event()
    capability_reads = direct_attempts = 0
    current_request = asyncio.current_task()
    command_task: asyncio.Task[object] | None = None
    command_audits: list[tuple[RoastCommand, str]] = []
    action_audits: list[str] = []
    command_events: list[RoastEventKind] = []
    premature_flushes: list[asyncio.Task[object] | None] = []
    request_in_progress = False
    actuator_failure = RuntimeError("synthetic direct e-stop actuator failure")
    audit_failure = RuntimeError("synthetic direct e-stop command audit failure")

    original_read_ack = actuator.read_fault_acknowledgement_state
    original_emergency = actuator.emergency_stop
    original_command = store.record_command
    original_action = store.record_operator_action
    original_dispatch_audit = runner._persist_dispatch_audit  # pyright: ignore[reportPrivateUsage]
    original_flush = runner._flush_events  # pyright: ignore[reportPrivateUsage]

    async def read_ack(session_id: str) -> FaultAcknowledgementState:
        proof_held.set()
        await release_proof.wait()
        return await original_read_ack(session_id)

    def capability_doubt() -> bool:
        nonlocal capability_reads
        capability_reads += 1
        raise RuntimeError("synthetic route capability uncertainty")

    async def emergency(*, reason: str) -> AppliedRoasterState | None:
        nonlocal direct_attempts
        direct_attempts += 1
        if failed_write:
            raise actuator_failure
        return await original_emergency(reason=reason)

    async def dispatch_audit(
        tool: RoastCommand,
        item: QueuedOperatorAction,
        event: _BufferedEvent,
    ) -> None:
        nonlocal command_task
        if tool is RoastCommand.EMERGENCY_STOP:
            command_task = asyncio.current_task()
            command_events.append(event.kind)
        await original_dispatch_audit(tool, item, event)

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
        if (
            request_in_progress
            and asyncio.current_task() is command_task
            and tool is RoastCommand.EMERGENCY_STOP
            and source == "operator"
        ):
            command_audits.append((tool, status))
            raise audit_failure
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

    async def record_action(
        *,
        action: str,
        result: Literal["accepted", "rejected", "failed"],
        run_id: str | None = None,
        payload: object = None,
    ) -> None:
        if (
            asyncio.current_task() is current_request
            and action == OperatorAction.EMERGENCY_STOP.value
        ):
            action_audits.append(result)
        await original_action(action=action, result=result, run_id=run_id, payload=payload)

    async def flush() -> None:
        if failed_write and request_in_progress and command_task is None:
            premature_flushes.append(asyncio.current_task())
        await original_flush()

    monkeypatch.setattr(actuator, "read_fault_acknowledgement_state", read_ack)
    monkeypatch.setattr(actuator, "emergency_stop", emergency)
    monkeypatch.setattr(runner, "emergency_stop_must_bypass_terminal_writer", capability_doubt)
    monkeypatch.setattr(runner, "_persist_dispatch_audit", dispatch_audit)
    monkeypatch.setattr(runner, "_flush_events", flush)
    monkeypatch.setattr(store, "record_command", record_command)
    monkeypatch.setattr(store, "record_operator_action", record_action)

    ack = await service.submit_operator_action(
        run_id,
        OperatorActionRequest(
            action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
            payload={"confirmation": True, "expected_generation": lease.generation},
        ),
    )
    assert ack.queued
    tick = asyncio.create_task(runner.tick_once())
    stopped: OperatorActionResult | None = None
    propagated: Exception | None = None
    try:
        await asyncio.wait_for(proof_held.wait(), 2)
        finalisation_reserved = gate.fault_finalization_started
        phase_before = controller.phase
        retry_before = controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]
        request_in_progress = True
        try:
            stopped = await service.submit_operator_action(
                run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
            )
        except Exception as error:  # noqa: BLE001 - verify propagation by sentinel identity
            propagated = error
        finally:
            request_in_progress = False
        proof_still_held = not release_proof.is_set() and not tick.done()
        retry_after = controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]
        phase_after = controller.phase
    finally:
        # End the held proof without letting its tick proceed to the controller
        # retry stage. Cancellation is cleanup, outside the observed request.
        tick.cancel()
        release_proof.set()
        try:
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(tick, 2)
        finally:
            try:
                assert await service.settle_fault_lease_persistence_for_teardown()
            finally:
                assert await executor.settle_for_shutdown()

    assert finalisation_reserved and proof_still_held and premature_flushes == []
    assert phase_before is phase_after is RoastPhase.FAULTED
    assert tick.cancelled() and actuator.fault_writes == []
    assert actuator.commands() == ([] if failed_write else ["emergency_stop"])
    assert capability_reads == direct_attempts == 1
    assert (command_task is not None) is failed_write
    assert propagated is None and stopped is not None
    assert stopped.result == "accepted" and not stopped.queued
    assert stopped.reason == "emergency_stop accepted for controller priority dispatch"
    assert command_events == ([RoastEventKind.COMMAND_FAILED] if failed_write else [])
    assert command_audits == ([(RoastCommand.EMERGENCY_STOP, "failed")] if failed_write else [])
    assert action_audits == ["accepted"]
    assert retry_before is False and retry_after is failed_write
    assert (
        any(
            receipt.run_id == run_id
            and receipt.kind == "record_command"
            and receipt.detail == RoastCommand.EMERGENCY_STOP.value
            and receipt.settlement is Settlement.UNRESOLVED
            and receipt.writer_entered is None
            and receipt.command is not None
            for receipt in store.drain_human_storage_receipts()
        )
        is failed_write
    )


async def test_l8_replacement_cannot_reset_a_consumed_containment_attempt(
    store: RoastStore,
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    child = _LifecycleSpy(_Session(_result({})))
    assert await child.discover_fixed_zero() is ToolDiscovery.DISCOVERED
    actuator.epoch = child.child_epoch
    service = RoastService(
        store,
        mcp=child,
        roaster=actuator,
        raw_state=actuator,
        authority=ControlAuthority.HUMAN,
        config=AppConfig(controller=ControllerConfig(t0_debounce_ticks=1)),
        clock=clock,
        run_loop=False,
    )
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    owner = controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    await _charge_live_runner(service, actuator, clock)
    actuator.bound = False
    clock.advance(0.25)
    await runner.tick_once()
    assert actuator.containments == 1
    assert owner.containment is ContainmentAttempt.CONSUMED
    assert owner.command is CommandDisposition.SETTLED
    assert controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    commands_before_replacement = list(actuator.calls)

    with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
        await service._build_runner("replacement", PROFILE)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RoastRunConflictError, match="HUMAN.*ownership"):
        await service.start_roast(PROFILE)
    retry = await controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
        generation=controller.command_generation,
    )

    assert retry.outcome is ContainmentOutcome.NOT_ATTEMPTED
    assert owner.containment is ContainmentAttempt.CONSUMED
    assert service._human_ownership.is_current(owner)  # pyright: ignore[reportPrivateUsage]
    assert service.runner is runner
    assert actuator.containments == 1
    assert actuator.resets == 1
    assert actuator.calls == commands_before_replacement
    assert actuator.commands().count("start_session") == 1
    assert [row.id for row in (await service.history()).runs] == [run.id]


async def test_t52b_automatic_runner_uses_only_legacy_telemetry(store: RoastStore) -> None:
    clock = FakeClock()
    log: list[str] = []
    actuator = _RunnerRoaster(clock, log)
    actuator.frames = [reading(185, 200)]
    service = RoastService(
        store,
        roaster=actuator,
        raw_state=actuator,
        authority=ControlAuthority.AUTOMATIC,
        clock=clock,
        run_loop=False,
    )
    await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    log.clear()
    clock.advance(0.25)

    await runner.tick_once()

    assert log.count("read") == 1
    assert actuator.reads == 0


async def test_human_recovery_detail_without_live_runner_withholds_drop(store: RoastStore) -> None:
    await store.create_run(
        run_id="persisted-recovery",
        profile=PROFILE,
        config=AppConfig(),
        agent_phase=RoastPhase.OPERATOR_RECOVERY_REQUIRED,
    )
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)

    detail = await service.detail("persisted-recovery")

    assert service.runner is None
    assert detail.agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert OperatorAction.DROP_BEANS not in detail.enabled_actions
    assert OperatorAction.EMERGENCY_STOP in detail.enabled_actions
    assert actuator.calls == []


async def test_human_recovery_ack_loses_identity_before_runner_dispatch(store: RoastStore) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    await _charge_live_runner(service, actuator, clock)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    clock.advance(0.25)
    await runner.tick_once()
    queued = await service.submit_operator_action(
        run.id,
        OperatorActionRequest(
            action=OperatorAction.ACKNOWLEDGE_RECOVERY,
            payload={"resume_to": RoastPhase.ROASTING_PRE_FIRST_CRACK.value},
        ),
    )
    assert queued.queued
    actuator.session_id = "new-synthetic-session"
    actuator.calls.clear()

    await runner.tick_once()

    assert controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert actuator.calls == []
    async with store.connection.execute(
        "SELECT result FROM operator_actions WHERE run_id = ? AND action = ? ORDER BY id",
        (run.id, OperatorAction.ACKNOWLEDGE_RECOVERY.value),
    ) as cursor:
        rows = await cursor.fetchall()
    assert [row[0] for row in rows][-1] == "failed"


@pytest.mark.parametrize("route", ["d212", "legacy"])
async def test_automatic_fault_completion_cancellation_preserves_live_run(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    clock = FakeClock()
    actuator = _ExactFaultRunner(clock)
    service = _service(
        store, clock, actuator, exact_child=True, authority=ControlAuthority.AUTOMATIC
    )
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    runner._controller.transition_to(RoastPhase.FAULTED)  # pyright: ignore[reportPrivateUsage]
    await runner.tick_once()
    entered = asyncio.Event()

    async def cancel_at_persistence(**_kwargs: object) -> None:
        entered.set()
        await asyncio.Future[None]()

    if route == "d212":
        lease = await store.open_fault_control_lease(
            run_id=run.id, mcp_session_id=actuator.session_id
        )
        monkeypatch.setattr(store, "close_fault_control_lease_atomically", cancel_at_persistence)
        request = OperatorActionRequest(
            action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
            payload={"confirmation": True, "expected_generation": lease.generation},
        )
    else:
        monkeypatch.setattr(store, "complete_run", cancel_at_persistence)
        request = OperatorActionRequest(action=OperatorAction.ACKNOWLEDGE_FAULT)
    accepted = await service.submit_operator_action(run.id, request)
    assert accepted.queued
    tick = asyncio.create_task(runner.tick_once())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not runner.finalized
    finally:
        tick.cancel()
        with suppress(asyncio.CancelledError):
            await tick

    assert tick.cancelled()
    assert not runner.finalized
    assert not runner._completion_persistence_initiated  # pyright: ignore[reportPrivateUsage]
    persisted = await store.read_run(run.id)
    assert persisted is not None and persisted.completed_at_utc is None
    assert persisted.agent_phase is RoastPhase.FAULTED


async def _d216_recovered_service(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, run_id: str, authority: ControlAuthority
) -> tuple[RoastService, _RunnerRoaster, _LifecycleSpy, _Session, AsyncMock]:
    await store.create_run(
        run_id=run_id,
        profile=PROFILE,
        config=AppConfig(),
        agent_phase=RoastPhase.PREHEATING,
    )
    clock = FakeClock()
    roaster = _RunnerRoaster(clock, [])
    session = _Session(_result({}))
    child = _LifecycleSpy(session)
    roaster.epoch = child.child_epoch
    presence = AsyncMock(return_value="none")
    monkeypatch.setattr(roaster, "read_session_presence", presence)
    service = RoastService(
        store,
        mcp=child,
        roaster=roaster,
        raw_state=roaster,
        authority=authority,
        live_serve_mode=True,
        run_loop=False,
        clock=clock,
    )
    await service.recover_on_start()
    runner = service.runner
    assert runner is not None
    recovered = runner.controller_snapshot()
    assert recovered.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    if authority is ControlAuthority.HUMAN:
        assert recovered.human_endpoint is EndpointDisposition.HISTORY_UNKNOWN
    return service, roaster, child, session, presence


async def _d216_owned_phase_actions(service: RoastService, run_id: str) -> object:
    runner = service.runner
    assert runner is not None
    queue = service.events.subscribe(last_event_id=0)
    try:
        owned_id = None
        while not queue.empty():
            frame = queue.get_nowait()
            if runner.owns_human_phase_frame(frame.id):
                owned_id = frame.id
                break
        assert owned_id is not None
    finally:
        service.events.unsubscribe(queue)

    response = await stream_events(
        run_id, cast(Request, _FakeRequest(last_event_id_header=str(owned_id - 1))), service
    )
    frames = await asyncio.wait_for(_collect_frames(response, 3), 1)
    data = cast("dict[str, object]", _parse_frame(frames[2])["data"])
    assert data["phase"] == RoastPhase.OPERATOR_RECOVERY_REQUIRED.value
    return data["enabled_actions"]


@pytest.mark.parametrize("authority", [ControlAuthority.HUMAN, ControlAuthority.AUTOMATIC])
async def test_d216_confirmed_closure_only_exposes_end_in_recovered_detail(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, authority: ControlAuthority
) -> None:
    run_id = "d216-recovered"
    service, roaster, child, session, presence = await _d216_recovered_service(
        store, monkeypatch, run_id, authority
    )
    allow_drop = Mock(return_value=True)
    monkeypatch.setattr(service, "_guarded_human_drop_allowed", allow_drop)
    closure_actions = [OperatorAction.END_RECOVERED_RUN.value, OperatorAction.EMERGENCY_STOP.value]
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service)), base_url="http://test"
    ) as http:
        url = f"/api/roasts/{run_id}"
        before = await http.get(url)
        assert before.status_code == 200
        assert before.json()["enabled_actions"] == [OperatorAction.EMERGENCY_STOP.value]
        admitted = await http.post(
            "/api/restart-clearance/closure-only", json={"physical_confirmation": True}
        )
        assert admitted.status_code == 200
        assert admitted.json()["restart_clearance"]["state"] == "closure_only"
        detail = await http.get(url)
        assert detail.status_code == 200
        assert detail.json()["enabled_actions"] == closure_actions
        if authority is ControlAuthority.HUMAN:
            assert await _d216_owned_phase_actions(service, run_id) == closure_actions
        ended = await http.post(f"{url}/end-recovered-run")
        assert ended.status_code == 200 and ended.json()["outcome"] == "aborted"
        completed = await http.get(url)
        assert completed.status_code == 200
        assert OperatorAction.END_RECOVERED_RUN.value not in completed.json()["enabled_actions"]

    allow_drop.assert_not_called()
    persisted = await store.read_run(run_id)
    assert persisted is not None and persisted.outcome == "aborted"
    assert (
        await store.read_process_restart_clearance(service.instance_id)
    ) is RestartClearanceState.REQUIRED
    assert presence.await_count == 2
    assert roaster.calls == []
    assert session.calls == []
    assert child.lifecycle == []


@pytest.mark.parametrize(
    "revoked_by",
    [
        "emergency-admission",
        "context-invalidated",
        "child-stopped",
        "child-replaced",
        "lease-open",
        "lease-unknown",
        "durable-closed-lease-changed",
        "shutdown",
        "other-run",
    ],
)
async def test_d216_stale_or_foreign_closure_does_not_expose_human_end(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, revoked_by: str
) -> None:
    run_id = "d216-current"
    other_run = "d216-prior"
    if revoked_by == "durable-closed-lease-changed":
        await store.create_run(
            run_id=other_run, profile=PROFILE, config=AppConfig(), agent_phase=RoastPhase.FAULTED
        )
        await store.complete_run(
            run_id=other_run, outcome="faulted", agent_phase=RoastPhase.FAULTED
        )
    service, roaster, child, session, _ = await _d216_recovered_service(
        store, monkeypatch, run_id, ControlAuthority.HUMAN
    )
    allow_drop = Mock(return_value=True)
    monkeypatch.setattr(service, "_guarded_human_drop_allowed", allow_drop)
    replacement: _Session | None = None
    async with AsyncClient(
        transport=ASGITransport(app=create_app(service)), base_url="http://test"
    ) as http:
        url = f"/api/roasts/{run_id}"
        admitted = await http.post(
            "/api/restart-clearance/closure-only", json={"physical_confirmation": True}
        )
        assert admitted.status_code == 200
        current = await http.get(url)
        assert current.json()["enabled_actions"] == [
            OperatorAction.END_RECOVERED_RUN.value,
            OperatorAction.EMERGENCY_STOP.value,
        ]

        if revoked_by == "emergency-admission":
            service._note_emergency_stop_admission()  # pyright: ignore[reportPrivateUsage]
        elif revoked_by == "context-invalidated":
            service._invalidate_closure_only_clearance()  # pyright: ignore[reportPrivateUsage]
            await asyncio.gather(*service._closure_invalidation_tasks)  # pyright: ignore[reportPrivateUsage]
            assert (
                await store.read_process_restart_clearance(service.instance_id)
            ) is RestartClearanceState.UNKNOWN
        elif revoked_by == "child-stopped":
            child.replace(None)
            assert not child.running
        elif revoked_by == "child-replaced":
            old_epoch = child.child_epoch
            replacement = _Session(_result({}), session_id=session.session_id)
            child.replace(replacement)
            assert child.running and child.child_epoch > old_epoch
        elif revoked_by == "lease-open":
            opened = await store.open_fault_control_lease(
                run_id=run_id, mcp_session_id=roaster.session_id
            )
            assert opened.status is FaultLeaseStatus.OPEN
        elif revoked_by == "lease-unknown":
            unknown = await store.replace_fault_control_lease_unknown(run_id)
            assert unknown.status is FaultLeaseStatus.UNKNOWN
        elif revoked_by == "durable-closed-lease-changed":
            opened = await store.open_fault_control_lease(
                run_id=other_run, mcp_session_id="prior-session"
            )
            closed = await store.close_fault_control_lease_atomically(
                generation=opened.generation,
                run_id=other_run,
                mcp_session_id="prior-session",
                fault_reason=None,
            )
            assert closed.confirmed
            assert (await store.read_fault_control_lease()).status is FaultLeaseStatus.CLOSED
        elif revoked_by == "shutdown":
            await service.shutdown()

        if revoked_by == "other-run":
            lease = await store.read_fault_control_lease()
            foreign_actions = service._human_recovery_actions(  # pyright: ignore[reportPrivateUsage]
                other_run,
                RoastPhase.OPERATOR_RECOVERY_REQUIRED,
                [OperatorAction.DROP_BEANS],
                lease,
            )
            assert foreign_actions == [OperatorAction.EMERGENCY_STOP]
            wrong_run = await http.post(f"/api/roasts/{other_run}/end-recovered-run")
            assert wrong_run.status_code == 409
            same_run = await http.get(url)
            assert same_run.json()["enabled_actions"] == current.json()["enabled_actions"]
        else:
            detail = await http.get(url)
            assert detail.status_code == 200
            assert detail.json()["enabled_actions"] == [OperatorAction.EMERGENCY_STOP.value]
            assert await _d216_owned_phase_actions(service, run_id) == [
                OperatorAction.EMERGENCY_STOP.value
            ]

    allow_drop.assert_not_called()
    persisted = await store.read_run(run_id)
    assert persisted is not None and persisted.completed_at_utc is None
    assert roaster.calls == []
    assert session.calls == []
    active = await store.active_run()
    assert active is not None and active.run_id == run_id
    assert child.lifecycle == []
    if replacement is not None:
        assert replacement.calls == []


async def test_malformed_phase_broadcast_preserves_payload_without_enabled_actions() -> None:
    events = EventBroadcaster()
    subscriber = events.subscribe()
    payload = {"phase": "unsupported-phase", "detail": "unclassified phase payload"}

    try:
        events.emit(RoastEventKind.PHASE_CHANGED, payload, human_run_id="synthetic-human-run")
        frame = subscriber.get_nowait()
        assert frame.event is SseEventType.PHASE_CHANGED
        assert frame.data == payload
        assert "enabled_actions" not in frame.data
        assert subscriber.empty()
    finally:
        events.unsubscribe(subscriber)


@pytest.mark.parametrize("sample", ["trigger", "below", "stale"])
async def test_bounded_prior_turn_telemetry_does_not_take_urgent_authority(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, sample: str
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = RoastService(
        store,
        config=AppConfig(
            controller=ControllerConfig(
                t0_debounce_ticks=1,
                tick_interval_seconds=0.15,
                telemetry_log_interval_seconds=0.05,
            )
        ),
        roaster=actuator,
        raw_state=actuator,
        authority=ControlAuthority.HUMAN,
        clock=clock,
        run_loop=False,
    )
    detail = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    clock.advance(0.1)
    await runner.tick_once()
    actuator.charged = True
    actuator.bean = 185
    clock.advance(0.1)
    await runner.tick_once()
    assert runner.controller_snapshot().charge_detected
    assert runner.controller_snapshot().phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    clock.advance(0.1)

    entered, release = asyncio.Event(), asyncio.Event()
    real_commit = store.connection.commit
    real_telemetry = store.record_telemetry
    native_reached_telemetry = False

    async def held_telemetry(**kwargs: object) -> bool:
        nonlocal native_reached_telemetry
        native_reached_telemetry = True
        return await real_telemetry(**kwargs)  # type: ignore[arg-type]

    async def commit() -> None:
        nonlocal native_reached_telemetry
        if native_reached_telemetry and not entered.is_set():
            native_reached_telemetry = False
            entered.set()
            await release.wait()
        await real_commit()

    monkeypatch.setattr(store, "record_telemetry", held_telemetry)
    monkeypatch.setattr(store.connection, "commit", commit)
    pending_turn = asyncio.create_task(runner.tick_once())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not await asyncio.wait_for(pending_turn, 1)
        assert runner.controller_snapshot().phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://test"
        ) as client:
            unavailable = await client.get(f"/api/roasts/{detail.id}")
        assert unavailable.status_code == 503
        assert unavailable.json()["human_history_durability"] == "unknown"
        stream = await stream_events(detail.id, cast(Request, _FakeRequest()), service)
        async with aclosing(cast("AsyncGenerator[bytes, None]", stream.body_iterator)) as frames:
            assert await anext(frames) == b": connected\n\n"
            frame = _parse_frame((await anext(frames)).decode())
            frame_data = cast("dict[str, object]", frame["data"])
            assert frame_data["human_history_durability"] == "unknown"
        actuator.bean = 195 if sample == "below" else 196
        if sample == "stale":

            async def stale_read() -> None:
                clock.advance(20)

            actuator.collect = stale_read
        clock.advance(0.1)
        assert not await asyncio.wait_for(runner.tick_once(), 1)
        assert actuator.commands().count("drop_beans") == (1 if sample == "trigger" else 0), (
            runner.controller_snapshot(),
            actuator.reads,
        )
        assert runner.controller_snapshot().phase is (
            RoastPhase.COOLING if sample == "trigger" else RoastPhase.OPERATOR_RECOVERY_REQUIRED
        )
    finally:
        release.set()
        await store.settle_human_storage_for_teardown()
        await service.shutdown()


async def test_human_queued_estop_evaluates_urgent_stage_before_blocked_evaluation_audit(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    ordered: list[str] = []
    real_urgent = controller.human_urgent_stage
    real_store = store.record_safety_evaluation
    real_commit = store.connection.commit
    entered, release = asyncio.Event(), asyncio.Event()
    command_row = False

    async def urgent(*, emergency_won_batch: bool = False) -> None:
        ordered.append("urgent")
        assert emergency_won_batch
        await real_urgent(emergency_won_batch=emergency_won_batch)

    async def record_evaluation(
        *,
        run_id: str,
        tick: int,
        evaluation: SafetyEvaluation,
    ) -> int:
        nonlocal command_row
        command_row = evaluation.verdict is SafetyVerdict.EMERGENCY_STOP
        return await real_store(run_id=run_id, tick=tick, evaluation=evaluation)

    async def commit() -> None:
        if command_row and not entered.is_set():
            entered.set()
            await release.wait()
        await real_commit()

    monkeypatch.setattr(controller, "human_urgent_stage", urgent)
    monkeypatch.setattr(store, "record_safety_evaluation", record_evaluation)
    monkeypatch.setattr(store.connection, "commit", commit)
    service.operator_queue.put_nowait(
        QueuedOperatorAction(run_id=run.id, action=OperatorAction.EMERGENCY_STOP)
    )
    task = asyncio.create_task(runner.tick_once())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert ordered == ["urgent"]
        assert actuator.commands().count("emergency_stop") == 1
        assert "drop_beans" not in actuator.commands()
        assert not await asyncio.wait_for(task, 2)
        assert actuator.commands().count("emergency_stop") == 1
        pending = [
            receipt
            for receipt in store.drain_human_storage_receipts()
            if receipt.kind == "record_safety_evaluation" and receipt.command is not None
        ]
        assert len(pending) == 1 and pending[0].timely is False
        assert pending[0].turn == runner.current_tick
        reference = pending[0].command
        assert reference is not None and reference.session_id == actuator.session_id
        assert reference.child_generation == actuator.epoch
        assert reference.command_generation == controller.command_generation
        original = pending[0]
        release.set()
        assert await store.settle_human_storage_for_teardown()
        (late,) = [
            receipt
            for receipt in store.drain_human_storage_receipts()
            if receipt.token == original.token
        ]
        assert late.settlement is Settlement.OBSERVED_COMMITTED and late.timely is False
        assert actuator.commands().count("emergency_stop") == 1
    finally:
        release.set()
        await task
        await service.shutdown()


@pytest.mark.parametrize("during_dispatch", [True, False], ids=["adapter-invoked", "urgent-stage"])
async def test_queued_estop_cancellation_before_storage_preserves_exact_audit_gaps(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, during_dispatch: bool
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    real_stop = actuator.emergency_stop
    entered = asyncio.Event()
    wait_forever = asyncio.Event()
    dispatched_generations: list[int] = []

    async def stop(*, reason: str) -> AppliedRoasterState | None:
        dispatched_generations.append(controller.command_generation)
        result = await real_stop(reason=reason)
        if during_dispatch:
            entered.set()
            await wait_forever.wait()
        return result

    async def urgent(*, emergency_won_batch: bool = False) -> None:
        assert emergency_won_batch and not during_dispatch
        entered.set()
        await wait_forever.wait()

    store.drain_human_storage_receipts()
    record_command = AsyncMock(wraps=store.record_command)
    monkeypatch.setattr(store, "record_command", record_command)
    monkeypatch.setattr(actuator, "emergency_stop", stop)
    monkeypatch.setattr(controller, "human_urgent_stage", urgent)
    events = service.events.subscribe()
    service.operator_queue.put_nowait(
        QueuedOperatorAction(run_id=run.id, action=OperatorAction.EMERGENCY_STOP)
    )
    tick = asyncio.create_task(runner.tick_once())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert actuator.commands().count("emergency_stop") == 1
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick
        assert controller.phase is RoastPhase.FAULTED
        assert controller._latched_verdict is SafetyVerdict.EMERGENCY_STOP  # pyright: ignore[reportPrivateUsage]
        record_command.assert_not_awaited()
        assert controller._pending_fail_safe_is_emergency_stop_retry is during_dispatch  # pyright: ignore[reportPrivateUsage]
        failed = [
            event
            for event in (events.get_nowait() for _ in range(events.qsize()))
            if event.event is SseEventType.COMMAND_FAILED
            and event.data.get("command") == "emergency_stop"
        ]
        assert len(failed) == int(during_dispatch)

        receipts = store.drain_human_storage_receipts()
        expected_kinds = (
            ["record_safety_evaluation", "record_command"]
            if during_dispatch
            else ["record_safety_evaluation"]
        )
        assert [receipt.kind for receipt in receipts] == expected_kinds
        evaluation = receipts[0]
        command = evaluation.command
        assert command is not None and command.owner is CommandAuditOwner.LEASE
        assert (
            command.session_id == actuator.session_id and command.child_generation == actuator.epoch
        )
        assert dispatched_generations == [command.command_generation]
        for receipt in receipts:
            assert receipt.run_id == run.id and receipt.turn == runner.current_tick
            assert receipt.command == command and receipt.deadline == evaluation.deadline
            assert receipt.writer_entered is False and receipt.timely is False
            assert receipt.settlement is Settlement.VERIFIED_NO_WRITE
        assert not during_dispatch or receipts[-1].detail == "emergency_stop"
        assert store.human_storage_quiescent
        assert runner.history_durability() is HumanHistoryDurability.INCOMPLETE
        timeline = await store.read_timeline(run.id)
        assert not any(entry.tool is RoastCommand.EMERGENCY_STOP for entry in timeline.commands)
        assert actuator.commands().count("emergency_stop") == 1
    finally:
        if not tick.done():
            tick.cancel()
            with suppress(asyncio.CancelledError):
                await tick
        service.events.unsubscribe(events)
        await service.shutdown()


@pytest.mark.parametrize(
    ("acknowledged", "held_audit"),
    [
        pytest.param(False, "record_safety_evaluation", id="unknown-stop-unentered-command"),
        pytest.param(True, "record_safety_evaluation", id="confirmed-stop-no-command"),
        pytest.param(False, "record_command", id="unknown-stop-entered-command"),
    ],
)
async def test_cancelled_queued_estop_distinguishes_entered_and_unentered_audits(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, acknowledged: bool, held_audit: str
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    real_stop = actuator.emergency_stop
    real_commit = store.connection.commit
    urgent_stage = controller.human_urgent_stage
    ordered: list[str] = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop(*, reason: str) -> AppliedRoasterState | None:
        result = await real_stop(reason=reason)
        ordered.append("mock-stop-called")
        if not acknowledged:
            raise RuntimeError("mock e-stop acknowledgement lost")
        return result

    async def urgent(*, emergency_won_batch: bool = False) -> None:
        assert emergency_won_batch
        ordered.append("urgent")
        await urgent_stage(emergency_won_batch=emergency_won_batch)

    async def commit() -> None:
        owner = store._current_native_storage()  # pyright: ignore[reportPrivateUsage]
        if owner is not None and owner.receipt.kind == held_audit and not entered.is_set():
            ordered.append("native-audit-entered")
            entered.set()
            await release.wait()
        await real_commit()

    store.drain_human_storage_receipts()
    monkeypatch.setattr(actuator, "emergency_stop", stop)
    monkeypatch.setattr(controller, "human_urgent_stage", urgent)
    monkeypatch.setattr(store.connection, "commit", commit)
    events = service.events.subscribe()
    service.operator_queue.put_nowait(
        QueuedOperatorAction(run_id=run.id, action=OperatorAction.EMERGENCY_STOP)
    )
    tick = asyncio.create_task(runner.tick_once())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert ordered == ["mock-stop-called", "urgent", "native-audit-entered"]
        assert actuator.commands().count("emergency_stop") == 1
        assert not release.is_set()
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick
        assert not release.is_set()
        receipts = store.drain_human_storage_receipts()
        (evaluation,) = [r for r in receipts if r.kind == "record_safety_evaluation"]
        assert evaluation.run_id == run.id and evaluation.turn == runner.current_tick
        assert evaluation.writer_entered is True
        exact = evaluation.command
        assert exact is not None and exact.session_id == actuator.session_id
        assert exact.child_generation == actuator.epoch
        assert exact.command_generation == controller.command_generation
        assert exact.owner is CommandAuditOwner.LEASE
        commands = [r for r in receipts if r.kind == "record_command"]
        failed_events = [
            event
            for event in (events.get_nowait() for _ in range(events.qsize()))
            if event.event is SseEventType.COMMAND_FAILED
            and event.data.get("command") == "emergency_stop"
        ]
        assert len(failed_events) == (0 if acknowledged else 1)
        if acknowledged:
            assert commands == []
        else:
            (command,) = commands
            assert command.run_id == evaluation.run_id and command.turn == evaluation.turn
            assert command.deadline == evaluation.deadline and command.command == exact
            assert command.detail == "emergency_stop" and command.timely is False

        if held_audit == "record_command":
            (pending,) = commands
            assert (
                evaluation.settlement is Settlement.OBSERVED_COMMITTED and evaluation.timely is True
            )
        else:
            pending = evaluation
            if not acknowledged:
                (gap,) = commands
                assert (
                    gap.writer_entered is False and gap.settlement is Settlement.VERIFIED_NO_WRITE
                )
        assert pending.writer_entered is True and pending.timely is False
        assert pending.settlement is Settlement.UNRESOLVED

        release.set()
        assert await store.settle_human_storage_for_teardown()
        (late,) = [r for r in store.drain_human_storage_receipts() if r.token == pending.token]
        assert late.settlement is Settlement.OBSERVED_COMMITTED and late.timely is False
        assert late.command == exact
        known_gap = not acknowledged and held_audit == "record_safety_evaluation"
        settled = HumanHistoryDurability.INCOMPLETE if known_gap else HumanHistoryDurability.DELAYED
        assert store.human_history_durability(run.id) is settled
        assert runner.history_durability() is (
            HumanHistoryDurability.INCOMPLETE if known_gap else HumanHistoryDurability.UNKNOWN
        )
        assert await runner._flush_events()  # pyright: ignore[reportPrivateUsage]
        assert runner.history_durability() is settled
        timeline = await store.read_timeline(run.id)
        assert [
            entry.status for entry in timeline.commands if entry.tool is RoastCommand.EMERGENCY_STOP
        ] == (["failed"] if held_audit == "record_command" else [])
        assert actuator.commands().count("emergency_stop") == 1
    finally:
        release.set()
        if not tick.done():
            tick.cancel()
            with suppress(asyncio.CancelledError):
                await tick
        await store.settle_human_storage_for_teardown()
        service.events.unsubscribe(events)
        await service.shutdown()


@pytest.mark.parametrize(
    ("route", "held"),
    [
        ("priority", "adapter"),
        ("priority", "record_safety_evaluation"),
        ("shutdown", "record_safety_evaluation"),
        ("shutdown", "record_command"),
    ],
)
async def test_cancelled_direct_estop_preserves_unentered_and_native_audits(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, route: str, held: str
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    stop = actuator.emergency_stop
    commit = store.connection.commit
    entered, release = asyncio.Event(), asyncio.Event()
    actual_generations: list[int] = []

    async def lost_acknowledgement(*, reason: str) -> AppliedRoasterState | None:
        actual_generations.append(controller.command_generation)
        await stop(reason=reason)
        if held == "adapter":
            entered.set()
            await release.wait()
        raise RuntimeError("mock emergency acknowledgement lost")

    async def hold_commit() -> None:
        native = store._current_native_storage()  # pyright: ignore[reportPrivateUsage]
        if native is not None and native.receipt.kind == held and not entered.is_set():
            entered.set()
            await release.wait()
        await commit()

    store.drain_human_storage_receipts()
    command_write = AsyncMock(wraps=store.record_command)
    monkeypatch.setattr(store, "record_command", command_write)
    monkeypatch.setattr(store.connection, "commit", hold_commit)
    monkeypatch.setattr(actuator, "emergency_stop", lost_acknowledgement)
    dispatch = (
        runner.dispatch_priority_emergency_stop
        if route == "priority"
        else runner.dispatch_shutdown_emergency_stop
    )
    item = QueuedOperatorAction(run_id=run.id, action=OperatorAction.EMERGENCY_STOP)
    action = asyncio.create_task(dispatch(item))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        action.cancel()
        with pytest.raises(asyncio.CancelledError):
            await action
        assert controller.phase is RoastPhase.FAULTED
        assert controller._latched_verdict is SafetyVerdict.EMERGENCY_STOP  # pyright: ignore[reportPrivateUsage]
        assert actuator.commands().count("emergency_stop") == 1
        receipts = store.drain_human_storage_receipts()
        assert sorted(r.kind for r in receipts) == ["record_command", "record_safety_evaluation"]
        evaluation = next(r for r in receipts if r.kind == "record_safety_evaluation")
        command = next(r for r in receipts if r.kind == "record_command")
        identity = evaluation.command
        assert identity is not None and identity.owner is CommandAuditOwner.LEASE
        assert (
            identity.session_id == actuator.session_id
            and identity.child_generation == actuator.epoch
        )
        assert actual_generations == [identity.command_generation]
        assert command.command == identity and command.deadline == evaluation.deadline
        assert all(r.run_id == run.id and r.turn is None for r in receipts)
        if held == "record_command":
            command_write.assert_awaited_once()
            assert evaluation.settlement is Settlement.OBSERVED_COMMITTED and evaluation.timely
        else:
            command_write.assert_not_awaited()
            assert command.writer_entered is False and command.timely is False
            assert command.settlement is Settlement.VERIFIED_NO_WRITE
        if held == "adapter":
            assert evaluation.writer_entered is False and evaluation.timely is False
            assert evaluation.settlement is Settlement.VERIFIED_NO_WRITE
            assert store.human_storage_quiescent
        else:
            native = next(r for r in receipts if r.kind == held)
            assert native.settlement is Settlement.UNRESOLVED and native.writer_entered is True
            release.set()
            assert await store.settle_human_storage_for_teardown()
            (late,) = store.drain_human_storage_receipts()
            assert late.token == native.token and late.settlement is Settlement.OBSERVED_COMMITTED
            assert late.timely is False and late.command == identity
        timeline = await store.read_timeline(run.id)
        assert sum(row.tool is RoastCommand.EMERGENCY_STOP for row in timeline.commands) == int(
            held == "record_command"
        )
    finally:
        release.set()
        if not action.done():
            action.cancel()
            with suppress(asyncio.CancelledError):
                await action
        await store.settle_human_storage_for_teardown()
        await service.shutdown()


@pytest.mark.parametrize("failed_tick", [True, False], ids=["failed-tick", "timed-out-flush"])
async def test_discarded_events_ledger_uses_exact_emit_origin(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, failed_tick: bool
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    emitter = runner._emitter  # pyright: ignore[reportPrivateUsage]
    stop = actuator.emergency_stop
    direct = StorageCaller(StorageDeadline.start(clock, 3.0), run.id)
    turn_origins: list[StorageCaller] = []

    async def lost_acknowledgement(*, reason: str) -> AppliedRoasterState | None:
        await stop(reason=reason)
        raise RuntimeError("mock emergency acknowledgement lost")

    async def direct_stop() -> None:
        with storage_caller(direct):
            assert not await controller.operator_emergency_stop("independent stop")

    async def interrupt_after_controller(_events_before_tick: int) -> bool:
        caller = current_storage_caller()
        assert caller is not None and caller.turn == runner.current_tick
        turn_origins.append(caller)
        emitter.emit(RoastEventKind.DRYING_END, {})
        if failed_tick:
            raise HistoryUnavailable("tick history refused")
        raise asyncio.CancelledError

    monkeypatch.setattr(actuator, "emergency_stop", lost_acknowledgement)
    monkeypatch.setattr(runner, "_persist_tick_history", interrupt_after_controller)
    try:
        await asyncio.create_task(direct_stop())
        emitter.emit(RoastEventKind.T0_DETECTED, {})
        clock.advance(0.5)
        if failed_tick:
            await runner.tick_once()
        else:
            with pytest.raises(asyncio.CancelledError):
                await runner.tick_once()
            lock = runner._event_flush_lock  # pyright: ignore[reportPrivateUsage]
            later_flush = StorageCaller(StorageDeadline.start(clock, 2.0), run.id, turn=99)
            async with lock:
                clock.advance(2.0)
                with storage_caller(later_flush):
                    assert not await runner._flush_events()  # pyright: ignore[reportPrivateUsage]
        (turn,) = turn_origins
        assert not emitter.peek()
        receipts = [
            r
            for r in store.drain_human_storage_receipts()
            if r.kind == "record_event" and r.settlement is Settlement.VERIFIED_NO_WRITE
        ]
        (direct_gap,) = [r for r in receipts if r.detail == RoastEventKind.COMMAND_FAILED.value]
        (tick_gap,) = [r for r in receipts if r.detail == RoastEventKind.DRYING_END.value]
        (ownerless_gap,) = [r for r in receipts if r.detail == RoastEventKind.T0_DETECTED.value]
        assert direct_gap.run_id == tick_gap.run_id == ownerless_gap.run_id == run.id
        assert direct_gap.turn is None and direct_gap.deadline == direct.deadline.expires_at
        assert (
            direct_gap.command is not None and direct_gap.command.owner is CommandAuditOwner.LEASE
        )
        assert tick_gap.turn == turn.turn and tick_gap.deadline == turn.deadline.expires_at
        assert ownerless_gap.turn is None and ownerless_gap.command is None
        assert all(
            r.writer_entered is False and r.timely is False
            for r in (direct_gap, tick_gap, ownerless_gap)
        )
        assert actuator.commands().count("emergency_stop") == 1
    finally:
        await service.shutdown()


async def test_human_queued_estop_audit_exception_keeps_subsequent_supervision(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    events: list[str] = []
    urgent_stage = controller.human_urgent_stage
    record_evaluation = store.record_safety_evaluation

    async def urgent(*, emergency_won_batch: bool = False) -> None:
        assert actuator.commands().count("emergency_stop") == 1
        events.append("urgent_after_estop" if emergency_won_batch else "urgent_next_turn")
        await urgent_stage(emergency_won_batch=emergency_won_batch)

    async def audit(*, run_id: str, tick: int, evaluation: SafetyEvaluation) -> int:
        if evaluation.verdict is SafetyVerdict.EMERGENCY_STOP:
            assert events == ["urgent_after_estop"]
            events.append("audit_failed")
            raise RuntimeError("non-SQLite e-stop audit failure")
        return await record_evaluation(run_id=run_id, tick=tick, evaluation=evaluation)

    monkeypatch.setattr(controller, "human_urgent_stage", urgent)
    monkeypatch.setattr(store, "record_safety_evaluation", audit)
    service.operator_queue.put_nowait(
        QueuedOperatorAction(run_id=run.id, action=OperatorAction.EMERGENCY_STOP)
    )
    try:
        assert not await runner.tick_once()
        turn = runner.current_tick
        assert events == ["urgent_after_estop", "audit_failed"]
        (receipt,) = [
            r
            for r in store.drain_human_storage_receipts()
            if r.kind == "record_safety_evaluation"
            and r.turn == turn
            and r.settlement is Settlement.UNRESOLVED
        ]
        assert receipt.run_id == run.id and receipt.writer_entered is None
        command = receipt.command
        assert command is not None and command.session_id == actuator.session_id
        assert command.child_generation == actuator.epoch
        assert command.owner is CommandAuditOwner.LEASE
        assert command.command_generation == controller.command_generation
        clock.advance(0.25)
        assert not await runner.tick_once()
        assert runner.current_tick == turn + 1
        assert events == ["urgent_after_estop", "audit_failed", "urgent_next_turn"]
        assert actuator.commands().count("emergency_stop") == 1
        assert "drop_beans" not in actuator.commands()
    finally:
        await service.shutdown()


@pytest.mark.parametrize("stage", ["before_lock", "entered_first", "settled_first"])
async def test_human_event_cancellation_does_not_start_unentered_siblings(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    emitter = runner._emitter  # pyright: ignore[reportPrivateUsage]
    flush_lock = runner._event_flush_lock  # pyright: ignore[reportPrivateUsage]
    flush = runner._flush_events  # pyright: ignore[reportPrivateUsage]
    release, entered = asyncio.Event(), asyncio.Event()
    emitter.emit(RoastEventKind.DRYING_END, {})
    emitter.emit(RoastEventKind.T0_DETECTED, {})
    store.drain_human_storage_receipts()
    original_record = store.record_event
    original_commit = store.connection.commit

    async def commit() -> None:
        if stage == "entered_first":
            entered.set()
            await release.wait()
        await original_commit()

    async def record(**kwargs: object) -> None:
        await original_record(**kwargs)  # type: ignore[arg-type]
        if stage == "settled_first":
            entered.set()
            await release.wait()

    monkeypatch.setattr(store.connection, "commit", commit)
    monkeypatch.setattr(store, "record_event", record)
    if stage == "before_lock":
        await flush_lock.acquire()

    async def drain() -> None:
        if stage == "before_lock":
            entered.set()
        await flush()

    task = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        receipts = store.drain_human_storage_receipts()
        (sibling,) = [r for r in receipts if r.detail == RoastEventKind.T0_DETECTED.value]
        assert (
            sibling.settlement is Settlement.VERIFIED_NO_WRITE and sibling.writer_entered is False
        )
        (first,) = [r for r in receipts if r.detail == RoastEventKind.DRYING_END.value]
        assert (
            first.settlement
            is {
                "before_lock": Settlement.VERIFIED_NO_WRITE,
                "entered_first": Settlement.UNRESOLVED,
                "settled_first": Settlement.OBSERVED_COMMITTED,
            }[stage]
        )
        release.set()
        assert await store.settle_human_storage_for_teardown()
        timeline = await store.read_timeline(run.id)
        assert RoastEventKind.T0_DETECTED not in [event.kind for event in timeline.events]
        assert sum(event.kind is RoastEventKind.DRYING_END for event in timeline.events) == (
            0 if stage == "before_lock" else 1
        )
    finally:
        release.set()
        if stage == "before_lock":
            flush_lock.release()
        await service.shutdown()


async def test_flush_cancel_preserves_later_independent_failed_estop_event(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    emitter = runner._emitter  # pyright: ignore[reportPrivateUsage]
    lock = runner._event_flush_lock  # pyright: ignore[reportPrivateUsage]
    flush = runner._flush_events  # pyright: ignore[reportPrivateUsage]
    real_stop = actuator.emergency_stop
    waiting = asyncio.Event()

    async def stop(*, reason: str) -> AppliedRoasterState | None:
        await real_stop(reason=reason)
        raise RuntimeError("mock e-stop acknowledgement lost")

    async def wait_for_flush() -> None:
        waiting.set()
        await flush()

    monkeypatch.setattr(actuator, "emergency_stop", stop)
    store.drain_human_storage_receipts()
    assert not emitter.peek()
    try:
        async with lock:
            waiter = asyncio.create_task(wait_for_flush())
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                assert not await controller.operator_emergency_stop("independent stop")
                assert controller.phase is RoastPhase.FAULTED
                (failed,) = [
                    event for event in emitter.peek() if event.kind is RoastEventKind.COMMAND_FAILED
                ]
                command = failed.audit_command
                assert command is not None and command.owner is CommandAuditOwner.LEASE
                assert command.session_id == actuator.session_id
                assert command.child_generation == actuator.epoch
                assert command.command_generation == controller.command_generation
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                assert any(event is failed for event in emitter.peek())
                assert not any(
                    r.kind == "record_event" for r in store.drain_human_storage_receipts()
                )
            finally:
                if not waiter.done():
                    waiter.cancel()
                    with suppress(asyncio.CancelledError):
                        await waiter
        assert await flush()
        receipts = store.drain_human_storage_receipts()
        (persisted,) = [r for r in receipts if r.detail == RoastEventKind.COMMAND_FAILED.value]
        assert (
            persisted.command == command and persisted.settlement is Settlement.OBSERVED_COMMITTED
        )
        timeline = await store.read_timeline(run.id)
        assert sum(event.kind is RoastEventKind.COMMAND_FAILED for event in timeline.events) == 1
        assert actuator.commands().count("emergency_stop") == 1
    finally:
        await service.shutdown()


@pytest.mark.parametrize("write", ["t0", "phase"])
async def test_human_one_shot_history_does_not_retry_after_late_commit(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    write: str,
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    controller.transition_to(RoastPhase.FAULTED)
    snapshot = replace(runner.controller_snapshot(), charge_detected=True)
    store.drain_human_storage_receipts()
    original_commit = store.connection.commit
    commits = 0

    async def commit_after_deadline() -> None:
        nonlocal commits
        commits += 1
        clock.advance(1.0)
        await original_commit()

    monkeypatch.setattr(store.connection, "commit", commit_after_deadline)
    if write == "t0":
        write_t0 = runner._persist_t0_if_charged  # pyright: ignore[reportPrivateUsage]
        await write_t0(snapshot)
        await write_t0(snapshot)
        active = await store.read_latest_run()
        assert (
            active is not None and active.run_id == run.id and active.t0_detected_at_utc is not None
        )
        kind = "record_t0_detected_at"
    else:
        write_phase = runner._persist_phase_if_changed  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(HistoryUnavailable, match="deadline"):
            await write_phase()
        await write_phase()
        detail = await store.read_run(run.id)
        assert detail is not None and detail.agent_phase is RoastPhase.FAULTED
        kind = "update_run_phase"
    assert commits == 1
    (receipt,) = [
        receipt for receipt in store.drain_human_storage_receipts() if receipt.kind == kind
    ]
    assert receipt.settlement is Settlement.OBSERVED_COMMITTED and receipt.timely is False
    monkeypatch.setattr(store.connection, "commit", original_commit)
    await service.shutdown()


async def test_human_health_refuses_unverified_second_active_read(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    service = _service(store, clock, _RunnerRoaster(clock, []))
    run = await service.start_roast(PROFILE)
    active_run = store.active_run
    reads = 0

    async def available_then_refused() -> PersistedRun | None:
        nonlocal reads
        reads += 1
        if reads == 1:
            active = await active_run()
            assert active is not None and active.run_id == run.id
            return active
        raise HistoryUnavailable("second query refused")

    with monkeypatch.context() as injected:
        injected.setattr(store, "active_run", available_then_refused)
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://test"
        ) as client:
            response = await client.get("/api/health")
    assert reads == 2
    assert response.status_code == 503
    assert "active_run_id" not in response.json()
    await service.shutdown()


@pytest.mark.parametrize("write", ["create_run", "record_safety_evaluation", "record_event"])
async def test_human_start_history_failure_separates_mcp_entry_and_keeps_supervision(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    write: str,
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    child = _LifecycleSpy(_Session(_result({})))
    actuator.epoch = child.child_epoch
    service = RoastService(
        store,
        mcp=child,
        roaster=actuator,
        raw_state=actuator,
        authority=ControlAuthority.HUMAN,
        clock=clock,
        run_loop=True,
    )
    real_commit = store.connection.commit
    supervision_started = asyncio.Event()
    finish_supervision = asyncio.Event()
    crossed_budget = 0

    async def controlled_loop(runner: RoastRunner) -> None:
        assert runner is service.runner
        assert runner.controller_snapshot().phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
        supervision_started.set()
        await finish_supervision.wait()

    async def commit_after_deadline() -> None:
        nonlocal crossed_budget
        native = store._current_native_storage()  # pyright: ignore[reportPrivateUsage]
        if crossed_budget == 0 and native is not None and native.receipt.kind == write:
            crossed_budget += 1
            clock.advance(1.0)
        await real_commit()

    monkeypatch.setattr(RoastRunner, "run", controlled_loop)
    monkeypatch.setattr(store.connection, "commit", commit_after_deadline)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://test"
        ) as client:
            start = await client.post("/api/roasts", json=PROFILE.model_dump(mode="json"))
            assert start.status_code == 503
            assert crossed_budget == 1
            (late,) = [
                r
                for r in store.drain_human_storage_receipts()
                if r.kind == write
                and r.timely is False
                and r.settlement is Settlement.OBSERVED_COMMITTED
            ]
            if write == "create_run":
                assert "start_owned" not in start.json()
                assert service.runner is None
                assert not supervision_started.is_set()
                assert actuator.commands() == []
            else:
                run_id = start.json()["run_id"]
                assert start.json()["start_owned"] is True
                assert late.run_id == service.active_run_id == run_id
                assert actuator.commands().count("start_session") == 1
                assert not {"set_heat", "set_fan", "drop_beans"}.intersection(actuator.commands())
                await asyncio.wait_for(supervision_started.wait(), 1)
                runner = service.runner
                assert runner is not None
                controller = runner.controller_for_human_fault_drop()
                assert controller is not None and controller.owns_human_run(run_id)
                identity = controller._known_human_identity()  # pyright: ignore[reportPrivateUsage]
                assert identity == (actuator.session_id, child.child_epoch)
                assert runner.controller_snapshot().human_endpoint is EndpointDisposition.NONE
                result = await service.submit_operator_action(
                    run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
                )
                assert result.result == "accepted"
                assert actuator.commands().count("emergency_stop") == 1
            previous = list(actuator.commands())
            retry = await client.post("/api/roasts", json=PROFILE.model_dump(mode="json"))
            assert retry.status_code == 409
            assert actuator.commands() == previous
    finally:
        finish_supervision.set()
        await service.shutdown()


@pytest.mark.parametrize("response_only", [False, True])
async def test_human_owned_start_stays_supervised_when_history_is_unavailable(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, response_only: bool
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    child = _LifecycleSpy(_Session(_result({})))
    actuator.epoch = child.child_epoch
    service = RoastService(
        store,
        config=AppConfig(controller=ControllerConfig(tick_interval_seconds=0.25)),
        mcp=child,
        roaster=actuator,
        raw_state=actuator,
        authority=ControlAuthority.HUMAN,
        clock=clock,
        run_loop=True,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    supervised, finish = asyncio.Event(), asyncio.Event()
    real_commit = store.connection.commit
    read_run = store.read_run
    phase = RoastPhase.PREHEATING if response_only else RoastPhase.OPERATOR_RECOVERY_REQUIRED

    async def supervise(runner: RoastRunner) -> None:
        assert runner is service.runner
        assert runner.controller_snapshot().phase is phase
        supervised.set()
        await finish.wait()

    async def commit() -> None:
        owner = store._current_native_storage()  # pyright: ignore[reportPrivateUsage]
        if (
            not response_only
            and not entered.is_set()
            and owner is not None
            and owner.receipt.kind == "record_safety_evaluation"
        ):
            entered.set()
            await release.wait()
        await real_commit()

    async def response(run_id: str) -> RoastDetail | None:
        if response_only:
            await asyncio.wait_for(supervised.wait(), 1)
            raise HistoryUnavailable("injected response-only history refusal")
        return await read_run(run_id)

    monkeypatch.setattr(RoastRunner, "run", supervise)
    monkeypatch.setattr(store.connection, "commit", commit)
    monkeypatch.setattr(store, "read_run", response)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://test"
        ) as client:
            request = asyncio.create_task(
                client.post("/api/roasts", json=PROFILE.model_dump(mode="json"))
            )
            if not response_only:
                await asyncio.wait_for(entered.wait(), 1)
            result = await asyncio.wait_for(request, 2)
            await asyncio.wait_for(supervised.wait(), 1)
            assert result.status_code == 503 and result.json()["start_owned"] is True
            run_id = result.json()["run_id"]
            runner = service.runner
            assert runner is not None and runner.run_id == service.active_run_id == run_id
            assert runner.controller_snapshot().phase is phase
            controller = runner.controller_for_human_fault_drop()
            assert controller is not None
            assert controller._known_human_identity() == (  # pyright: ignore[reportPrivateUsage]
                actuator.session_id,
                child.child_epoch,
            )
            assert actuator.commands().count("start_session") == 1
            if not response_only:
                receipts = store.drain_human_storage_receipts()
                assert any(
                    r.run_id == run_id
                    and r.kind == "record_safety_evaluation"
                    and r.writer_entered is True
                    and r.settlement is Settlement.UNRESOLVED
                    for r in receipts
                )
                history = await client.get(f"/api/roasts/{run_id}")
                assert history.status_code == 503
                assert history.json()["human_history_durability"] == "unknown"
                assert not release.is_set()
                stop = await service.submit_operator_action(
                    run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
                )
                assert stop.result == "accepted"
                assert actuator.commands().count("emergency_stop") == 1
                assert not release.is_set()
            else:
                assert actuator.commands() == ["start_session"]
            again = await client.post("/api/roasts", json=PROFILE.model_dump(mode="json"))
            assert again.status_code == 409
            assert actuator.commands().count("start_session") == 1
    finally:
        release.set()
        finish.set()
        monkeypatch.setattr(store, "read_run", read_run)
        await store.settle_human_storage_for_teardown()
        await service.shutdown()


async def test_exact_fault_drop_withholds_when_required_evaluation_is_late(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    real_evaluation = store.record_safety_evaluation
    real_commit = store.connection.commit
    drop_evaluation_entered = False
    late_injected = False

    async def evaluate(*, run_id: str, tick: int, evaluation: SafetyEvaluation) -> int:
        nonlocal drop_evaluation_entered
        if evaluation.rule == "human_recovery_drop":
            assert evaluation.verdict is SafetyVerdict.ALLOW
            drop_evaluation_entered = True
        return await real_evaluation(run_id=run_id, tick=tick, evaluation=evaluation)

    async def commit() -> None:
        nonlocal late_injected
        if drop_evaluation_entered and not late_injected:
            late_injected = True
            actuator.clock.advance(1.0)
        await real_commit()

    store.drain_human_storage_receipts()
    monkeypatch.setattr(store, "record_safety_evaluation", evaluate)
    monkeypatch.setattr(store.connection, "commit", commit)
    try:
        result = await service.submit_operator_action(
            run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
        )
        assert late_injected
        assert result.result == "failed" and not result.queued
        assert controller.snapshot().human_endpoint is EndpointDisposition.NONE
        assert actuator.fault_writes == []
        (receipt,) = [
            r
            for r in store.drain_human_storage_receipts()
            if r.kind == "record_safety_evaluation" and r.run_id == run_id and r.timely is False
        ]
        assert receipt.writer_entered is True and receipt.command is None
        assert receipt.settlement is Settlement.OBSERVED_COMMITTED
        await runner.tick_once()
        assert actuator.fault_writes == []
        stop = await service.submit_operator_action(
            run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )
        assert stop.result == "accepted"
        assert actuator.commands().count("emergency_stop") == 1
        assert actuator.fault_writes == []
    finally:
        await service.shutdown()


async def test_exact_fault_drop_keeps_confirmed_result_and_one_correlated_dispatch(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, runner, actuator, run_id, lease = await _faulted_run(store)
    assert lease is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    dispatch: list[tuple[int, int | None]] = []
    write = actuator.write_fault_control

    async def tracked_write(action: OperatorAction, *, expected_session_id: str) -> None:
        generation = controller._arbiter.generation  # pyright: ignore[reportPrivateUsage]
        dispatch.append((generation, actuator.epoch))
        await write(action, expected_session_id=expected_session_id)

    async def broken_action_audit(**_kwargs: object) -> None:
        raise RuntimeError("injected ordinary storage failure after dispatch")

    store.drain_human_storage_receipts()
    monkeypatch.setattr(actuator, "write_fault_control", tracked_write)
    monkeypatch.setattr(store, "record_operator_action", broken_action_audit)
    try:
        result = await service.submit_operator_action(
            run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
        )
        assert result.result == "accepted" and not result.queued
        assert actuator.fault_writes == [OperatorAction.DROP_BEANS]
        assert controller.snapshot().human_endpoint is EndpointDisposition.ISSUED
        await runner.tick_once()
        receipts = store.drain_human_storage_receipts()
        (command,) = [
            r for r in receipts if r.kind == "record_command" and r.detail == "drop_beans"
        ]
        (event,) = [
            r
            for r in receipts
            if r.kind == "record_event" and r.detail == RoastEventKind.COMMAND_EXECUTED.value
        ]
        assert command.run_id == event.run_id == run_id
        assert command.command is not None and event.command == command.command
        assert command.command.session_id == actuator.session_id
        assert command.command.owner is CommandAuditOwner.ENDPOINT
        assert dispatch == [(command.command.command_generation, command.command.child_generation)]
        assert any(
            r.run_id == run_id
            and r.kind == "record_operator_action"
            and r.detail == "drop_beans"
            and r.settlement is Settlement.UNRESOLVED
            for r in receipts
        )
    finally:
        await service.shutdown()


@pytest.mark.parametrize(
    ("action", "bean", "dispatched_phase"),
    [
        (OperatorAction.DROP_BEANS, 185, RoastPhase.OPERATOR_RECOVERY_REQUIRED),
        (OperatorAction.MARK_FIRST_CRACK, 185, None),
        (OperatorAction.MARK_FIRST_CRACK, 196, RoastPhase.ROASTING_PRE_FIRST_CRACK),
    ],
)
async def test_human_queued_action_reports_admission_when_history_audit_refused(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    action: OperatorAction,
    bean: int,
    dispatched_phase: RoastPhase | None,
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
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
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    command_generation = controller.command_generation
    dispatched: list[tuple[RoastPhase, int]] = []
    original_drop = actuator.drop_beans

    async def drop() -> AppliedRoasterState | None:
        assert controller.command_generation == command_generation
        dispatched.append((controller.phase, controller.command_generation))
        return await original_drop()

    entered, release = asyncio.Event(), asyncio.Event()
    original_audit = store.record_operator_action
    original_fetch = aiosqlite.Cursor.fetchall
    reader: asyncio.Task[RoastTimeline] | None = None

    async def fetch(cursor: aiosqlite.Cursor) -> Any:
        owner = store._current_native_storage()  # pyright: ignore[reportPrivateUsage]
        if owner is not None and owner.receipt.kind == "read_timeline" and not entered.is_set():
            entered.set()
            await release.wait()
        return await original_fetch(cursor)

    async def audit(**kwargs: object) -> None:
        nonlocal reader
        assert kwargs["action"] == action.value
        if reader is None:
            reader = asyncio.create_task(service.timeline(run.id))
            await asyncio.wait_for(entered.wait(), 1)
        await original_audit(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(aiosqlite.Cursor, "fetchall", fetch)
    monkeypatch.setattr(store, "record_operator_action", audit)
    monkeypatch.setattr(actuator, "drop_beans", drop)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://test"
        ) as client:
            path = f"/api/roasts/{run.id}/operator-actions"
            admission = await client.post(path, json={"action": action.value})
            assert admission.status_code == 200
            assert admission.json()["result"] == "accepted" and admission.json()["queued"] is True
            pre_admission_failure = await client.post(path, json={"action": action.value})
            assert pre_admission_failure.status_code == 503
        assert service.operator_queue.qsize() == 1
        assert controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
        assert "drop_beans" not in actuator.commands()
        assert any(
            r.kind == "record_operator_action"
            and r.detail == action.value
            and r.run_id == run.id
            and r.settlement is Settlement.VERIFIED_NO_WRITE
            and r.writer_entered is False
            for r in store.drain_human_storage_receipts()
        )
        release.set()
        assert reader is not None
        await reader
        actuator.bean = bean
        clock.advance(0.25)
        await runner.tick_once()
        clock.advance(0.25)
        await runner.tick_once()
        assert "mark_first_crack" not in actuator.commands()
        expected = [] if dispatched_phase is None else [(dispatched_phase, command_generation)]
        assert dispatched == expected
        assert actuator.commands().count("drop_beans") == len(expected)
        assert controller.phase is (
            RoastPhase.OPERATOR_RECOVERY_REQUIRED if not expected else RoastPhase.COOLING
        )
        timeline = await service.timeline(run.id)
        assert any(
            e.kind is RoastEventKind.COMMAND_EXECUTED
            and isinstance(e.payload, dict)
            and e.payload.get("command") == "drop_beans"
            for e in timeline.events
        ) is bool(expected)
    finally:
        release.set()
        if reader is not None:
            await reader
        await service.shutdown()


@pytest.mark.parametrize(
    "route", ["start_revoke", "terminal_confirmed", "terminal_unknown", "terminal_replaced_child"]
)
async def test_human_estop_audit_failure_preserves_admitted_result(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    async def refuse_audit(**_kwargs: object) -> None:
        raise RuntimeError("unverified audit")

    if route == "start_revoke":
        clock = FakeClock()
        actuator = _RunnerRoaster(clock, [])
        service = _service(store, clock, actuator)
        commit_returned, release = asyncio.Event(), asyncio.Event()
        original_create = store.create_run
        run_id: str | None = None

        async def create(**kwargs: object) -> None:
            nonlocal run_id
            await original_create(**kwargs)  # type: ignore[arg-type]
            run_id = str(kwargs["run_id"])
            commit_returned.set()
            await release.wait()

        monkeypatch.setattr(store, "create_run", create)
        start_task = asyncio.create_task(service.start_roast(PROFILE))
        try:
            await asyncio.wait_for(commit_returned.wait(), 1)
            assert run_id is not None
            monkeypatch.setattr(store, "record_operator_action", refuse_audit)
            result = await service.submit_operator_action(
                run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
            )
            assert result.result == "accepted" and "revoked" in result.reason
            assert actuator.commands() == [] and result.queued is False
        finally:
            release.set()
            with pytest.raises(RoastRunConflictError):
                await start_task
        receipts = store.drain_human_storage_receipts()
    else:
        service, runner, exact, run_id, lease = await _faulted_run(store)
        assert lease is not None
        await service._read_fault_lease_fail_closed()  # pyright: ignore[reportPrivateUsage]
        runner._finalized = True  # pyright: ignore[reportPrivateUsage]
        await store.complete_run(run_id=run_id, outcome="faulted", agent_phase=RoastPhase.FAULTED)
        child = service._mcp  # pyright: ignore[reportPrivateUsage]
        assert isinstance(child, _LifecycleSpy)
        old_epoch = child.child_epoch
        assert old_epoch == exact.epoch
        exact.exact_emergency_write_raises = route != "terminal_confirmed"
        replacement = None
        if route == "terminal_replaced_child":
            replacement = _Session(_result({}), session_id="replacement-session")
            child.replace(replacement)
            assert child.child_epoch != old_epoch
            assert lease.mcp_session_id == exact.session_id
        store.drain_human_storage_receipts()
        monkeypatch.setattr(store, "record_operator_action", refuse_audit)
        result = await service.submit_operator_action(
            run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )
        assert result.result == ("accepted" if route == "terminal_confirmed" else "failed")
        assert result.queued is False
        assert exact.fault_writes == [OperatorAction.EMERGENCY_STOP]
        executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
        assert executor is not None and await executor.settle_for_shutdown()
        receipts = store.drain_human_storage_receipts()
        (dispatch,) = [
            r for r in receipts if r.kind == "record_command" and r.detail == "emergency_stop"
        ]
        assert dispatch.run_id == run_id
        if replacement is None:
            assert dispatch.command is not None and dispatch.command.session_id == exact.session_id
            assert dispatch.command.child_generation == old_epoch
            assert dispatch.command.owner is CommandAuditOwner.LEASE
        else:
            assert dispatch.command is None
            assert replacement.calls == []
    assert any(
        r.kind == "record_operator_action"
        and r.detail == OperatorAction.EMERGENCY_STOP.value
        and r.run_id == run_id
        and r.settlement is Settlement.UNRESOLVED
        for r in receipts
    )
    await service.shutdown()


async def test_human_queued_estop_failure_reaches_heat_off_with_history_held(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator)
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    entered, release = asyncio.Event(), asyncio.Event()
    original_fetch = aiosqlite.Cursor.fetchall
    initial_emergency = actuator.emergency_stop
    attempted = 0

    async def fetch(cursor: aiosqlite.Cursor) -> Any:
        owner = store._current_native_storage()  # pyright: ignore[reportPrivateUsage]
        if owner is not None and owner.receipt.kind == "read_timeline" and not entered.is_set():
            entered.set()
            await release.wait()
        return await original_fetch(cursor)

    async def first_stop_fails(*, reason: str) -> AppliedRoasterState | None:
        nonlocal attempted
        attempted += 1
        if attempted == 1:
            raise RuntimeError("unconfirmed emergency command")
        return await initial_emergency(reason=reason)

    monkeypatch.setattr(aiosqlite.Cursor, "fetchall", fetch)
    monkeypatch.setattr(actuator, "emergency_stop", first_stop_fails)
    store.drain_human_storage_receipts()
    reader = asyncio.create_task(service.timeline(run.id))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for action in (
            OperatorAction.EMERGENCY_STOP,
            OperatorAction.ACKNOWLEDGE_FAULT,
            OperatorAction.ACKNOWLEDGE_FAULT,
        ):
            service.operator_queue.put_nowait(QueuedOperatorAction(run_id=run.id, action=action))
        assert not await asyncio.wait_for(runner.tick_once(), 2)
        assert runner.controller_snapshot().phase is RoastPhase.FAULTED
        assert attempted == 1 and "drop_beans" not in actuator.commands()
        assert ("set_heat", {"heat_percent": 0}) in actuator.calls
        acknowledgements = [
            r
            for r in store.drain_human_storage_receipts()
            if r.kind == "record_operator_action"
            and r.detail == OperatorAction.ACKNOWLEDGE_FAULT.value
        ]
        assert len(acknowledgements) == 2
        assert all(r.settlement is Settlement.VERIFIED_NO_WRITE for r in acknowledgements)
        direct = await service.submit_operator_action(
            run.id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )
        assert direct.result == "accepted" and direct.queued is False
        assert attempted == 2
    finally:
        release.set()
        with suppress(HistoryUnavailable):
            await reader
        await store.settle_human_storage_for_teardown()
        await service.shutdown()


async def test_human_failed_terminal_estop_rearms_before_held_post_audit(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runner, actuator, run_id, _ = await _faulted_run(store)
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    gate = service._fault_acknowledgement_gate  # pyright: ignore[reportPrivateUsage]
    assert gate.begin_fault_finalization(gate.emergency_request_epoch)
    actuator.exact_emergency_write_raises = True
    release_evaluation, audit_entered, release_audit = (asyncio.Event() for _ in range(3))
    original_audit = store.record_command

    async def held_evaluation() -> int:
        await release_evaluation.wait()
        return 1

    async def hold_command(**kwargs: object) -> None:
        if kwargs["tool"] is RoastCommand.EMERGENCY_STOP:
            audit_entered.set()
            await release_audit.wait()
        await original_audit(**kwargs)  # type: ignore[arg-type]

    evaluation = asyncio.create_task(held_evaluation())
    registry = controller._human_drop_evaluation_tasks  # pyright: ignore[reportPrivateUsage]
    registry.add(evaluation)
    monkeypatch.setattr(store, "record_command", hold_command)
    stop = asyncio.create_task(
        service.submit_operator_action(
            run_id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )
    )
    try:
        await asyncio.wait_for(audit_entered.wait(), 1)
        assert controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]
        result = await asyncio.wait_for(asyncio.shield(stop), 1)
        assert result.result == "failed" and result.queued is False
        assert actuator.fault_writes == [OperatorAction.EMERGENCY_STOP]
        assert not evaluation.done()
    finally:
        release_audit.set()
        release_evaluation.set()
        await asyncio.gather(stop, evaluation)
        registry.discard(evaluation)
        gate.abort_fault_finalization()
        await service.shutdown()


@pytest.mark.parametrize("evaluation_refused", [True, False])
async def test_human_drop_history_distinguishes_withheld_from_failed_dispatch(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch, evaluation_refused: bool
) -> None:
    clock = FakeClock()
    actuator = _RunnerRoaster(clock, [])
    service = _service(store, clock, actuator, exact_child=True)
    run = await service.start_roast(PROFILE)
    await _charge_live_runner(service, actuator, clock)
    runner = service.runner
    assert runner is not None
    accepted = await service.submit_operator_action(
        run.id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert accepted.result == "accepted" and accepted.queued
    commit = store.connection.commit
    dispatch = actuator.drop_beans
    failed = False

    async def refuse_once() -> None:
        nonlocal failed
        native = store._current_native_storage()  # pyright: ignore[reportPrivateUsage]
        if not failed and native is not None and native.receipt.kind == "record_safety_evaluation":
            failed = True
            raise aiosqlite.OperationalError("required evaluation refused before COMMIT")
        await commit()

    async def fail_entered_drop() -> AppliedRoasterState | None:
        await dispatch()
        raise RuntimeError("entered Drop result not confirmed")

    if evaluation_refused:
        monkeypatch.setattr(store.connection, "commit", refuse_once)
    else:
        monkeypatch.setattr(actuator, "drop_beans", fail_entered_drop)
    try:
        clock.advance(0.25)
        assert not await runner.tick_once()
        assert failed is evaluation_refused
        assert actuator.commands().count("drop_beans") == (0 if evaluation_refused else 1)
        timeline = await service.timeline(run.id)
        (evidence,) = [
            event
            for event in timeline.events
            if event.kind is RoastEventKind.COMMAND_FAILED
            and isinstance(event.payload, dict)
            and event.payload.get("command") == "drop_beans"
        ]
        assert evidence.payload is not None
        commands = [entry for entry in timeline.commands if entry.tool is RoastCommand.DROP_BEANS]
        if not evaluation_refused:
            (attempt,) = commands
            assert attempt.status == "failed"
            assert evidence.payload["outcome"] == "unknown"
            (receipt,) = [
                r
                for r in store.drain_human_storage_receipts()
                if r.run_id == run.id and r.kind == "record_command" and r.detail == "drop_beans"
            ]
            assert (
                receipt.command is not None and receipt.command.owner is CommandAuditOwner.ENDPOINT
            )
            assert receipt.settlement is Settlement.OBSERVED_COMMITTED
            return
        assert evidence.payload["outcome"] == "withheld"
        assert evidence.payload["reason"] == "history_unavailable"
        assert commands == []
        (refused,) = [
            r
            for r in store.drain_human_storage_receipts()
            if r.run_id == run.id
            and r.kind == "record_safety_evaluation"
            and r.settlement is Settlement.VERIFIED_NO_WRITE
        ]
        assert refused.writer_entered is True and refused.command is None
        assert runner.history_durability() is HumanHistoryDurability.INCOMPLETE
    finally:
        await service.shutdown()


async def test_human_operator_drop_discloses_withheld_when_required_evaluation_is_unavailable(
    store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
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
    admitted = await service.submit_operator_action(
        run.id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert admitted.result == "accepted" and admitted.queued

    entered, release = asyncio.Event(), asyncio.Event()
    original_fetch = aiosqlite.Cursor.fetchall

    async def fetch(cursor: aiosqlite.Cursor) -> Any:
        native = store._current_native_storage()  # pyright: ignore[reportPrivateUsage]
        if native is not None and native.receipt.kind == "read_timeline" and not entered.is_set():
            entered.set()
            await release.wait()
        return await original_fetch(cursor)

    monkeypatch.setattr(aiosqlite.Cursor, "fetchall", fetch)
    reader = asyncio.create_task(service.timeline(run.id))
    events = service.events.subscribe()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        clock.advance(0.25)
        assert not await asyncio.wait_for(runner.tick_once(), 2)
        assert "drop_beans" not in actuator.commands()
        frames = [events.get_nowait() for _ in range(events.qsize())]
        assert any(
            event.event is SseEventType.COMMAND_FAILED
            and isinstance(event.data, dict)
            and event.data.get("command") == "drop_beans"
            and event.data.get("outcome") == "withheld"
            and event.data.get("reason") == "history_unavailable"
            for event in frames
        )
    finally:
        release.set()
        with suppress(HistoryUnavailable):
            await reader
        await store.settle_human_storage_for_teardown()
        service.events.unsubscribe(events)
        await service.shutdown()
