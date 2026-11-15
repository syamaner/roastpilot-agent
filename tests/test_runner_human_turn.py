"""HUMAN runner turn ordering and service lifecycle ownership witnesses."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from roastpilot_agent.api import (
    QueuedOperatorAction,
    RoastRunConflictError,
    RoastService,
    create_app,
)
from roastpilot_agent.config import AppConfig, ControllerConfig
from roastpilot_agent.human_protection import ControlAuthority, EndpointDisposition
from roastpilot_agent.human_run_ownership import PendingHumanCall
from roastpilot_agent.mcp_client import RoasterControlAdapter, RoastSessionState
from roastpilot_agent.models import OperatorAction, OperatorActionRequest, RoastPhase
from roastpilot_agent.store import RoastStore
from tests.conftest import FakeClock
from tests.test_controller import PROFILE
from tests.test_controller_human_mode import HumanRoaster
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


def _service(store: RoastStore, clock: FakeClock, actuator: _RunnerRoaster) -> RoastService:
    return RoastService(
        store,
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
    child = _LifecycleSpy(_Session(_result({})))
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
    clock.advance(0.25)
    await runner.tick_once()
    actuator.charged = True
    clock.advance(0.25)
    await runner.tick_once()
    controller.transition_to(phase)
    clock.advance(0.25)
    await runner.tick_once()
    assert OperatorAction.DROP_BEANS in (await service.detail(run.id)).enabled_actions
    clock.advance(20)
    assert OperatorAction.DROP_BEANS not in (await service.detail(run.id)).enabled_actions
    rejected = await service.submit_operator_action(
        run.id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert rejected.result == "rejected"
    assert not rejected.queued
    assert "physical intervention" in rejected.reason
    assert actuator.commands().count("drop_beans") == 0
