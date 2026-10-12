"""Focused regression tests for independently triaged PR1 safety repairs."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from roastpilot_agent.api import RoasterControl, RoastService
from roastpilot_agent.config import AppConfig, SafetyLimits
from roastpilot_agent.controller import FaultControlExecutor
from roastpilot_agent.models import (
    FaultControlsActionRequest,
    FaultControlState,
    OperatorAction,
    OperatorActionRequest,
    RestartClearanceState,
    RoastPhase,
    RoastProfile,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyPolicy, SafetyVerdict
from roastpilot_agent.store import RoastStore


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[RoastStore]:
    """Return an initialised temporary decision-trace store."""
    instance = RoastStore(tmp_path / "safety-repair.sqlite3")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


def _profile() -> RoastProfile:
    """Return the minimal valid profile for a synthetic safety row."""
    return RoastProfile(
        name="Safety repair",
        bean_origin="Test",
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=200.0,
        target_development_percent=14.0,
    )


class _FaultPort:
    """Exact-session port whose state and write outcome are test-controlled."""

    def __init__(self, *, fail_write: bool = False) -> None:
        self.fail_write = fail_write
        self.writes: list[OperatorAction] = []
        self.dropped = False

    def _state(self) -> FaultControlState:
        return FaultControlState(
            session_id="fault-session",
            mcp_phase="fault",
            active=False,
            device_connected=True,
            heat_level_percent=0,
            fan_level_percent=0,
            cooling_on=False,
            beans_added=True,
            beans_dropped=self.dropped,
        )

    async def read_fault_control_state(self, session_id: str) -> FaultControlState:
        """Return a stable exact fault-session state."""
        assert session_id == "fault-session"
        return self._state()

    async def read_latest_fault_control_state(self) -> FaultControlState:
        """Return the same state as the latest exact session."""
        return self._state()

    async def write_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> None:
        """Record, or deliberately fail, one guarded write."""
        assert expected_session_id == "fault-session"
        self.writes.append(action)
        if self.fail_write:
            raise RuntimeError("synthetic fault-control write failure")
        if action is OperatorAction.DROP_BEANS:
            self.dropped = True


@pytest.mark.asyncio
async def test_active_nonfaulted_open_lease_rejects_terminal_controls_before_executor(
    store: RoastStore,
) -> None:
    """A synthetic OPEN lease cannot redirect a live non-faulted drop."""
    run_id = "active-nonfaulted"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.PREHEATING,
    )
    port = _FaultPort()
    service = RoastService(
        store, roaster=cast(RoasterControl, port), live_serve_mode=True, run_loop=False
    )
    lease = await store.open_fault_control_lease(
        run_id=run_id, mcp_session_id="fault-session", generation=1
    )
    service._fault_lease_cache = lease  # noqa: SLF001  # pyright: ignore[reportPrivateUsage] - durable fixture
    service._restart_clearance_state = (  # noqa: SLF001  # pyright: ignore[reportPrivateUsage] - D213-cleared fixture
        RestartClearanceState.CLEARED
    )
    service._restart_epoch_initialized = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage] - D213-cleared fixture

    projection = await service._fault_controls_projection(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        lease
    )
    assert projection.enabled_actions == [OperatorAction.EMERGENCY_STOP]
    result = await service.submit_operator_action(
        run_id, request=OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )

    assert result.result == "rejected"
    assert port.writes == []
    assert service.operator_queue.empty()


@pytest.mark.asyncio
async def test_d213_cleared_detail_still_projects_open_nonfaulted_lease_as_estop_only(
    store: RoastStore,
) -> None:
    """A clearance cannot make the detail response advertise an OPEN lease's routine actions."""
    run_id = "detail-open-nonfaulted"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.PREHEATING,
    )
    service = RoastService(
        store, roaster=cast(RoasterControl, _FaultPort()), live_serve_mode=True, run_loop=False
    )
    lease = await store.open_fault_control_lease(
        run_id=run_id, mcp_session_id="fault-session", generation=1
    )
    service._fault_lease_cache = lease  # noqa: SLF001  # pyright: ignore[reportPrivateUsage] - durable fixture
    service._restart_clearance_state = (  # noqa: SLF001  # pyright: ignore[reportPrivateUsage] - D213-cleared fixture
        RestartClearanceState.CLEARED
    )
    service._restart_epoch_initialized = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage] - D213-cleared fixture

    detail = await service.detail(run_id)

    assert detail.enabled_actions == [OperatorAction.EMERGENCY_STOP]
    assert detail.fault_controls is not None
    assert detail.fault_controls.enabled_actions == [OperatorAction.EMERGENCY_STOP]


@pytest.mark.asyncio
async def test_terminal_fault_write_persists_linked_admission_and_failure_trace(
    store: RoastStore,
) -> None:
    """Terminal exact-session failure retains a linked safety and command trace."""
    run_id = "terminal-trace"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    port = _FaultPort(fail_write=True)
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=store)

    with pytest.raises(RuntimeError, match="synthetic fault-control write failure"):
        await executor.execute(
            OperatorAction.DROP_BEANS,
            "fault-session",
            run_id=run_id,
            is_current_generation=lambda: True,
        )

    assert await executor.settle_audit_tasks()
    timeline = await store.read_timeline(run_id)
    assert len(timeline.safety_evaluations) == 1
    assert len(timeline.commands) == 1
    command = timeline.commands[0]
    assert command.status == "failed"
    assert command.safety_evaluation_id == timeline.safety_evaluations[0].id
    assert command.result == {
        "admission": "allowed",
        "confirmation": "unconfirmed",
        "dispatch": "failed",
    }


@pytest.mark.asyncio
async def test_routine_fault_control_refuses_write_when_admission_trace_fails(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A routine exact-session command cannot reach MCP without durable admission."""
    run_id = "routine-admission-failure"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    port = _FaultPort()
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=store)

    async def fail_admission(*, run_id: str, evaluation: SafetyEvaluation) -> int:
        raise RuntimeError("synthetic admission persistence failure")

    monkeypatch.setattr(store, "record_fault_control_evaluation", fail_admission)

    assert (
        await executor.execute(
            OperatorAction.DROP_BEANS,
            "fault-session",
            run_id=run_id,
            is_current_generation=lambda: True,
        )
        is None
    )
    assert port.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("port", "expected"),
    [
        (
            _FaultPort(fail_write=True),
            {"dispatch": "write_failed", "confirmation": "not_attempted"},
        ),
    ],
)
async def test_estop_trace_distinguishes_write_failure(
    store: RoastStore, port: _FaultPort, expected: dict[str, str]
) -> None:
    """A failed emergency write is not reported as a failed readback."""
    run_id = "estop-write-failure"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=store)

    with pytest.raises(RuntimeError, match="synthetic fault-control write failure"):
        await executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            run_id=run_id,
            is_current_generation=lambda: True,
        )

    assert await executor.settle_audit_tasks()
    command = (await store.read_timeline(run_id)).commands[0]
    assert command.result is not None
    assert {key: command.result[key] for key in expected} == expected


@pytest.mark.asyncio
async def test_estop_trace_distinguishes_readback_failure(store: RoastStore) -> None:
    """A dispatched emergency write records a later exact-state read failure separately."""

    class ReadbackFailurePort(_FaultPort):
        async def read_fault_control_state(self, session_id: str) -> FaultControlState:
            raise RuntimeError("synthetic e-stop readback failure")

    run_id = "estop-readback-failure"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    executor = FaultControlExecutor(
        SafetyPolicy(SafetyLimits()), ReadbackFailurePort(), trace_sink=store
    )

    with pytest.raises(RuntimeError, match="synthetic e-stop readback failure"):
        await executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            run_id=run_id,
            is_current_generation=lambda: True,
        )

    assert await executor.settle_audit_tasks()
    command = (await store.read_timeline(run_id)).commands[0]
    assert command.result is not None
    assert command.result["dispatch"] == "completed"
    assert command.result["confirmation"] == "readback_failed"


@pytest.mark.asyncio
async def test_blocked_routine_admission_never_delays_priority_estop_dispatch(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SQLite-stalled routine admission cannot occupy the MCP writer before an e-stop."""
    run_id = "blocked-routine-admission"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    port = _FaultPort()
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=store)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = store.record_fault_control_evaluation

    async def block_routine_admission(*, run_id: str, evaluation: SafetyEvaluation) -> int:
        if evaluation.verdict is SafetyVerdict.ALLOW:
            entered.set()
            await release.wait()
        return await original(run_id=run_id, evaluation=evaluation)

    monkeypatch.setattr(store, "record_fault_control_evaluation", block_routine_admission)
    routine = asyncio.create_task(
        executor.execute(
            OperatorAction.DROP_BEANS,
            "fault-session",
            run_id=run_id,
            is_current_generation=lambda: True,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    stopped = await asyncio.wait_for(
        executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            run_id=run_id,
            is_current_generation=lambda: True,
        ),
        timeout=1.0,
    )

    assert stopped is not None
    assert port.writes == [OperatorAction.EMERGENCY_STOP]
    release.set()
    assert await asyncio.wait_for(routine, timeout=1.0) is None
    await executor.settle_audit_tasks()


@pytest.mark.asyncio
async def test_terminal_readback_that_fails_public_predicate_is_not_traced_as_ok(
    store: RoastStore,
) -> None:
    """A returned state is insufficient when the public exact-session predicate rejects it."""

    class NoEffectPort(_FaultPort):
        async def write_fault_control(
            self, action: OperatorAction, *, expected_session_id: str
        ) -> None:
            self.writes.append(action)

    run_id = "readback-rejected"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), NoEffectPort(), trace_sink=store)

    assert (
        await executor.execute(
            OperatorAction.DROP_BEANS,
            "fault-session",
            run_id=run_id,
            is_current_generation=lambda: True,
        )
        is None
    )
    assert await executor.settle_audit_tasks()
    command = (await store.read_timeline(run_id)).commands[0]
    assert command.status == "failed"
    assert command.result is not None
    assert command.result["confirmation"] == "rejected_readback"


@pytest.mark.asyncio
async def test_terminal_historical_open_lease_keeps_exact_session_route(store: RoastStore) -> None:
    """A completed fault row still uses its retained exact-session control."""
    run_id = "terminal-historical"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    await store.complete_run(run_id=run_id, outcome="faulted", agent_phase=RoastPhase.FAULTED)
    port = _FaultPort()
    service = RoastService(store, roaster=cast(RoasterControl, port), run_loop=False)
    lease = await store.open_fault_control_lease(
        run_id=run_id, mcp_session_id="fault-session", generation=1
    )
    service._fault_lease_cache = lease  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    result = await service.submit_fault_controls_action(
        FaultControlsActionRequest(action=OperatorAction.DROP_BEANS)
    )

    assert result.result == "confirmed"
    assert port.writes == [OperatorAction.DROP_BEANS]


@pytest.mark.asyncio
async def test_active_faulted_open_lease_keeps_guarded_exact_session_route(
    store: RoastStore,
) -> None:
    """The retained exception accepts a faulted live row only through exact-session control."""
    run_id = "active-faulted"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    port = _FaultPort()
    service = RoastService(store, roaster=cast(RoasterControl, port), run_loop=False)
    lease = await store.open_fault_control_lease(
        run_id=run_id, mcp_session_id="fault-session", generation=1
    )
    service._fault_lease_cache = lease  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    result = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )

    assert result.result == "accepted"
    assert not result.queued
    assert port.writes == [OperatorAction.DROP_BEANS]


@pytest.mark.asyncio
async def test_open_lease_projection_fails_closed_when_active_run_read_fails(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable live-run row cannot expose a retained lease's routine controls."""
    run_id = "active-read-failure"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    service = RoastService(store, roaster=cast(RoasterControl, _FaultPort()), run_loop=False)
    lease = await store.open_fault_control_lease(
        run_id=run_id, mcp_session_id="fault-session", generation=1
    )

    async def fail_active_run() -> None:
        raise RuntimeError("synthetic active-run read failure")

    monkeypatch.setattr(store, "active_run", fail_active_run)
    projection = await service._fault_controls_projection(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        lease
    )

    assert projection.enabled_actions == [OperatorAction.EMERGENCY_STOP]


@pytest.mark.asyncio
async def test_shutdown_settles_terminal_estop_audit(store: RoastStore) -> None:
    """Ordered service shutdown retains the asynchronous e-stop audit outcome."""
    run_id = "settled-estop-audit"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    await store.complete_run(run_id=run_id, outcome="faulted", agent_phase=RoastPhase.FAULTED)
    port = _FaultPort()
    service = RoastService(store, roaster=cast(RoasterControl, port), run_loop=False)
    lease = await store.open_fault_control_lease(
        run_id=run_id, mcp_session_id="fault-session", generation=1
    )
    service._fault_lease_cache = lease  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    result = await service.submit_fault_controls_action(
        FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
    )
    await service.shutdown()

    assert result.result == "confirmed"
    timeline = await store.read_timeline(run_id)
    assert timeline.commands[-1].tool.value == "emergency_stop"
    assert timeline.commands[-1].status == "ok"


@pytest.mark.asyncio
async def test_shutdown_waits_for_inflight_terminal_estop_then_drains_audit(
    store: RoastStore,
) -> None:
    """Ordered shutdown sees an audit created by an already-admitted e-stop worker."""

    class BlockingEStopPort(_FaultPort):
        def __init__(self) -> None:
            super().__init__()
            self.write_entered = asyncio.Event()
            self.release_write = asyncio.Event()

        async def write_fault_control(
            self, action: OperatorAction, *, expected_session_id: str
        ) -> None:
            self.writes.append(action)
            if action is OperatorAction.EMERGENCY_STOP:
                self.write_entered.set()
                await self.release_write.wait()

    run_id = "inflight-estop-shutdown"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    port = BlockingEStopPort()
    service = RoastService(store, roaster=cast(RoasterControl, port), run_loop=False)
    executor = service._fault_control_executor  # noqa: SLF001  # pyright: ignore[reportPrivateUsage] - owned integration seam
    assert executor is not None
    dispatch = asyncio.create_task(
        executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            run_id=run_id,
            is_current_generation=lambda: True,
        )
    )
    await asyncio.wait_for(port.write_entered.wait(), timeout=1.0)
    shutdown = asyncio.create_task(service.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()

    port.release_write.set()
    assert await asyncio.wait_for(dispatch, timeout=1.0) is not None
    await asyncio.wait_for(shutdown, timeout=1.0)

    timeline = await store.read_timeline(run_id)
    assert timeline.commands[-1].tool.value == "emergency_stop"
    assert timeline.commands[-1].status == "ok"


@pytest.mark.asyncio
async def test_estop_audit_shutdown_settle_is_bounded(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked audit cannot turn ordered shutdown into an unbounded wait."""
    run_id = "bounded-estop-audit"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), _FaultPort(), trace_sink=store)
    blocked = asyncio.Event()

    async def block_estop_audit(*, run_id: str, evaluation: SafetyEvaluation) -> int:
        if evaluation.verdict is SafetyVerdict.EMERGENCY_STOP:
            blocked.set()
            await asyncio.Event().wait()
        return 1

    monkeypatch.setattr(store, "record_fault_control_evaluation", block_estop_audit)
    assert (
        await executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            run_id=run_id,
            is_current_generation=lambda: True,
        )
        is not None
    )
    await asyncio.wait_for(blocked.wait(), timeout=1.0)

    assert not await executor.settle_audit_tasks(timeout_seconds=0.01)


@pytest.mark.asyncio
async def test_restart_required_detail_projects_only_emergency_stop(store: RoastStore) -> None:
    """A recovered non-faulted row never offers actions the D213 gate rejects."""
    run_id = "restart-required"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.OPERATOR_RECOVERY_REQUIRED,
    )
    service = RoastService(
        store, roaster=cast(RoasterControl, _FaultPort()), live_serve_mode=True, run_loop=False
    )

    detail = await service.detail(run_id)

    assert detail.enabled_actions == [OperatorAction.EMERGENCY_STOP]
