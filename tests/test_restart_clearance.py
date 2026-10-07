"""D213 process-clearance and fault-control lease tests (hardware-free)."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest

from roastpilot_agent.api import QueuedOperatorAction, RoastRunConflictError, RoastService
from roastpilot_agent.config import AppConfig, ControllerConfig
from roastpilot_agent.mcp_client import MCPServerProcess, SessionPresence
from roastpilot_agent.models import (
    FaultControlsActionRequest,
    FaultControlState,
    FaultLeaseStatus,
    OperatorAction,
    RestartClearanceRequest,
    RestartClearanceState,
    RoastPhase,
    RoastProfile,
)
from roastpilot_agent.store import FaultControlLease, RoastStore
from tests.conftest import FakeClock, FakeMCPClient


def _profile() -> RoastProfile:
    """Return a minimal saved-profile equivalent for controller construction."""
    return RoastProfile(
        name="Test bean",
        bean_origin="Test origin",
        bean_weight_grams=250,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=195,
        target_development_percent=14,
    )


async def _service(store: RoastStore) -> RoastService:
    """Build a live-mode service whose fake MCP reports no current session."""
    return RoastService(
        store,
        config=AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0)),
        roaster=FakeMCPClient(),
        live_serve_mode=True,
        run_loop=False,
        clock=FakeClock(),
    )


class _CurrentChild:
    """Minimal current-child identity seam for no-session clearance tests."""

    def __init__(self) -> None:
        self.running = True
        self.child_epoch = 1
        self.stop_unconfirmed = False
        self.teardown_incident_id: str | None = None


class _PresenceMCP(FakeMCPClient):
    """Fake control surface with an explicit, current typed presence snapshot."""

    def __init__(self, presence: object) -> None:
        super().__init__()
        self.presence = presence
        self.presence_reads = 0
        self.on_read: Callable[[], Awaitable[SessionPresence]] | None = None

    async def read_session_presence(self) -> SessionPresence:
        """Return one scripted typed-presence result or its failure."""
        self.presence_reads += 1
        if self.on_read is not None:
            return await self.on_read()
        if isinstance(self.presence, Exception):
            raise self.presence
        return cast("SessionPresence", self.presence)


async def _presence_service(
    store: RoastStore, presence: object
) -> tuple[RoastService, _PresenceMCP, _CurrentChild]:
    """Build a live service with a current child and scripted presence proof."""
    child = _CurrentChild()
    roaster = _PresenceMCP(presence)
    service = RoastService(
        store,
        config=AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0)),
        mcp=cast("MCPServerProcess", child),
        roaster=roaster,
        live_serve_mode=True,
        run_loop=False,
        clock=FakeClock(),
    )
    return service, roaster, child


class _TerminalFaultPort(FakeMCPClient):
    """Hardware-free exact-session port for terminal lease tests."""

    def __init__(self, state: FaultControlState) -> None:
        super().__init__()
        self.state = state
        self.stop_started: asyncio.Event | None = None
        self.release_stop: asyncio.Event | None = None

    async def read_fault_control_state(self, session_id: str) -> FaultControlState:
        assert session_id == self.state.session_id
        return self.state

    async def execute_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> FaultControlState:
        assert expected_session_id == self.state.session_id
        self.calls.append((action.value, {"expected_session_id": expected_session_id}))
        if action in (OperatorAction.STOP_COOLING, OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE):
            if self.stop_started is not None and self.release_stop is not None:
                self.stop_started.set()
                await self.release_stop.wait()
            self.state = self.state.model_copy(
                update={"cooling_on": False, "heat_level_percent": 0, "fan_level_percent": 0}
            )
        return self.state


async def _terminal_service(store: RoastStore, port: _TerminalFaultPort) -> RoastService:
    """Build a live service with a terminal exact-session fault port."""
    return RoastService(
        store,
        config=AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0)),
        roaster=port,
        live_serve_mode=True,
        run_loop=False,
        clock=FakeClock(),
    )


@pytest.mark.asyncio
async def test_no_session_clearance_fails_closed_without_a_current_child(tmp_path: Path) -> None:
    """Physical confirmation cannot convert an MCP exception into no-session proof."""
    store = RoastStore(tmp_path / "restart.sqlite3")
    await store.initialize()
    try:
        first = await _service(store)
        initial = await first.health()
        assert initial.restart_clearance.state is RestartClearanceState.REQUIRED
        assert not initial.restart_clearance.eligible
        with pytest.raises(RoastRunConflictError, match="restart clearance"):
            await first.start_roast(_profile())

        with pytest.raises(RoastRunConflictError, match="current MCP child"):
            await first.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        after = await first.health()
        assert after.restart_clearance.state is RestartClearanceState.REQUIRED
        assert not after.restart_clearance.eligible
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_no_session_clearance_confirms_current_child_none_and_persists(
    tmp_path: Path,
) -> None:
    """A fresh ``none`` proof plus physical confirmation clears only this process."""
    store = RoastStore(tmp_path / "restart-none.sqlite3")
    await store.initialize()
    try:
        service, roaster, _child = await _presence_service(store, "none")

        result = await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )

        assert result.outcome == "confirmed"
        assert result.cleared
        assert result.restart_clearance.eligible
        assert roaster.presence_reads == 1
        assert (
            await store.read_process_restart_clearance(service.instance_id)
            is RestartClearanceState.CLEARED
        )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "presence",
    [None, "starting", "active", "stopped", "unknown", RuntimeError("MCP timeout")],
)
async def test_no_session_clearance_rejects_absent_malformed_or_non_none_presence(
    tmp_path: Path, presence: object
) -> None:
    """Only the exact typed ``none`` value is affirmative no-session evidence."""
    store = RoastStore(tmp_path / "restart-invalid-presence.sqlite3")
    await store.initialize()
    try:
        service, _roaster, _child = await _presence_service(store, presence)

        with pytest.raises(RoastRunConflictError):
            await service.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )

        health = await service.health()
        assert health.restart_clearance.state is RestartClearanceState.REQUIRED
        assert not health.restart_clearance.eligible
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_no_session_clearance_rejects_child_replacement_during_proof(tmp_path: Path) -> None:
    """A response from a replaced child cannot clear the process gate."""
    store = RoastStore(tmp_path / "restart-child-race.sqlite3")
    await store.initialize()
    try:
        service, roaster, child = await _presence_service(store, "none")

        async def replace_child() -> SessionPresence:
            child.child_epoch += 1
            return "none"

        roaster.on_read = replace_child
        with pytest.raises(RoastRunConflictError, match="child changed"):
            await service.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        assert (await service.health()).restart_clearance.state is RestartClearanceState.REQUIRED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_no_session_clearance_rejects_lease_change_during_proof(tmp_path: Path) -> None:
    """A concurrent fault lease change invalidates a previously clean proof."""
    store = RoastStore(tmp_path / "restart-lease-race.sqlite3")
    await store.initialize()
    try:
        service, roaster, _child = await _presence_service(store, "none")

        async def change_lease() -> SessionPresence:
            await store.replace_fault_control_lease_unknown(None)
            return "none"

        roaster.on_read = change_lease
        with pytest.raises(RoastRunConflictError, match="evidence became stale"):
            await service.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        assert (await service.health()).fault_controls.status is FaultLeaseStatus.UNKNOWN
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_no_session_clearance_fails_closed_when_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful current-child proof cannot survive unconfirmed persistence."""
    store = RoastStore(tmp_path / "restart-persistence.sqlite3")
    await store.initialize()
    try:
        service, _roaster, _child = await _presence_service(store, "none")

        async def fail_confirmation(process_id: str) -> bool:
            del process_id
            raise RuntimeError("synthetic SQLite failure")

        monkeypatch.setattr(store, "confirm_process_restart_clearance", fail_confirmation)
        with pytest.raises(RoastRunConflictError, match="persistence is unconfirmed"):
            await service.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        assert (await service.health()).restart_clearance.state is RestartClearanceState.UNKNOWN
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_open_fault_clearance_keeps_existing_exact_session_path(tmp_path: Path) -> None:
    """No-session evidence is not required for the separate OPEN-lease proof."""
    store = RoastStore(tmp_path / "restart-open-fault.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        await store.create_run(
            run_id="private-run",
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.FAULTED,
        )
        await store.open_fault_control_lease(run_id="private-run", mcp_session_id="private-session")

        result = await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )

        assert result.outcome == "confirmed"
        assert result.restart_clearance.state is RestartClearanceState.CLEARED
        assert not result.restart_clearance.eligible
        assert result.fault_controls.status is FaultLeaseStatus.OPEN
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unknown_lease_exposes_only_software_estop(tmp_path: Path) -> None:
    """An unbound e-stop lease fails closed and never exposes routine controls."""
    store = RoastStore(tmp_path / "lease.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        await store.create_run(
            run_id="private-run",
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.FAULTED,
        )
        await store.replace_fault_control_lease_unknown("private-run")
        health = await service.health()
        assert health.fault_controls.status is FaultLeaseStatus.UNKNOWN
        assert health.fault_controls.enabled_actions == [OperatorAction.EMERGENCY_STOP]
        assert not health.restart_clearance.eligible
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_global_d212_requires_explicit_confirmation(tmp_path: Path) -> None:
    """The global fault endpoint cannot queue D212 without its confirmation."""
    store = RoastStore(tmp_path / "global-action.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        await store.create_run(
            run_id="private-run",
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.FAULTED,
        )
        await store.open_fault_control_lease(run_id="private-run", mcp_session_id="private-session")
        result = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE)
        )
        assert result.result == "rejected"
        assert result.fault_controls.status is FaultLeaseStatus.OPEN
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_terminal_open_lease_uses_exact_controller_executor_and_d212_closes(
    tmp_path: Path,
) -> None:
    """A terminal fault remains controllable only through the guarded executor."""
    store = RoastStore(tmp_path / "terminal-open.sqlite3")
    await store.initialize()
    try:
        port = _TerminalFaultPort(
            FaultControlState(
                session_id="private-session",
                mcp_phase="fault",
                active=False,
                device_connected=True,
                heat_level_percent=0,
                fan_level_percent=0,
                cooling_on=True,
                beans_added=False,
                beans_dropped=False,
            )
        )
        service = await _terminal_service(store, port)
        await store.create_run(
            run_id="private-run",
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.FAULTED,
        )
        await store.complete_run(
            run_id="private-run", outcome="faulted", agent_phase=RoastPhase.FAULTED
        )
        await store.open_fault_control_lease(run_id="private-run", mcp_session_id="private-session")

        health = await service.health()
        assert OperatorAction.DROP_BEANS not in health.fault_controls.enabled_actions
        assert OperatorAction.STOP_COOLING in health.fault_controls.enabled_actions
        stopped = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.STOP_COOLING)
        )
        assert stopped.result == "confirmed"
        assert port.calls == [("stop_cooling", {"expected_session_id": "private-session"})]

        acknowledged = await service.submit_fault_controls_action(
            FaultControlsActionRequest(
                action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE, confirmation=True
            )
        )
        assert acknowledged.result == "confirmed"
        assert acknowledged.fault_controls.status is FaultLeaseStatus.CLOSED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_newer_in_memory_estop_generation_cannot_regress_to_old_durable_lease(
    tmp_path: Path,
) -> None:
    """A stale store read cannot restore Start eligibility after e-stop admission."""
    store = RoastStore(tmp_path / "lease-generation.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        service._fault_lease_cache = FaultControlLease(  # noqa: SLF001 - race harness  # pyright: ignore[reportPrivateUsage]
            generation=2,
            status=FaultLeaseStatus.UNKNOWN,
            run_id="private-run",
            mcp_session_id=None,
        )
        durable = await service._read_fault_lease_fail_closed()  # noqa: SLF001 - race harness  # pyright: ignore[reportPrivateUsage]
        assert durable.generation == 2
        assert durable.status is FaultLeaseStatus.UNKNOWN
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stale_persistence_failure_preserves_newer_exact_session_lease(
    tmp_path: Path,
) -> None:
    """An old persistence failure cannot erase newer e-stop controls."""
    store = RoastStore(tmp_path / "lease-persistence-order.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        service._fault_lease_cache = FaultControlLease(  # noqa: SLF001 - race harness  # pyright: ignore[reportPrivateUsage]
            generation=2,
            status=FaultLeaseStatus.OPEN,
            run_id="private-newer-run",
            mcp_session_id="private-newer-session",
        )

        async def fail() -> None:
            raise RuntimeError("synthetic persistence failure")

        task = asyncio.create_task(fail())
        with pytest.raises(RuntimeError, match="synthetic persistence failure"):
            await task
        service._log_fault_lease_persistence_failure(task, 1)  # noqa: SLF001 - race harness  # pyright: ignore[reportPrivateUsage]

        lease = service._fault_lease_cache  # noqa: SLF001 - race harness  # pyright: ignore[reportPrivateUsage]
        assert lease.generation == 2
        assert lease.status is FaultLeaseStatus.OPEN
        assert lease.mcp_session_id == "private-newer-session"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_terminal_estop_admission_supersedes_concurrent_d212_close(
    tmp_path: Path,
) -> None:
    """A terminal e-stop generation wins before a delayed D212 may close."""
    store = RoastStore(tmp_path / "terminal-estop-race.sqlite3")
    await store.initialize()
    try:
        port = _TerminalFaultPort(
            FaultControlState(
                session_id="private-session",
                mcp_phase="fault",
                active=False,
                device_connected=True,
                heat_level_percent=0,
                fan_level_percent=0,
                cooling_on=True,
                beans_added=False,
                beans_dropped=False,
            )
        )
        port.stop_started = asyncio.Event()
        port.release_stop = asyncio.Event()
        service = await _terminal_service(store, port)
        await store.create_run(
            run_id="private-run",
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.FAULTED,
        )
        await store.complete_run(
            run_id="private-run", outcome="faulted", agent_phase=RoastPhase.FAULTED
        )
        await store.open_fault_control_lease(run_id="private-run", mcp_session_id="private-session")
        await service.health()  # Hydrate the server-owned terminal lease cache.

        acknowledgement = asyncio.create_task(
            service.submit_fault_controls_action(
                FaultControlsActionRequest(
                    action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE, confirmation=True
                )
            )
        )
        await port.stop_started.wait()
        emergency_stop = asyncio.create_task(
            service.submit_fault_controls_action(
                FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
            )
        )
        port.release_stop.set()
        acknowledgement_result = await acknowledgement
        emergency_result = await emergency_stop

        assert acknowledgement_result.result == "failed"
        assert emergency_result.result == "confirmed"
        controls = await service._fault_controls_projection_after_action()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert controls.status in (FaultLeaseStatus.OPEN, FaultLeaseStatus.UNKNOWN)
        assert controls.generation is not None and controls.generation > 1
        assert OperatorAction.EMERGENCY_STOP in controls.enabled_actions
    finally:
        await store.close()


class _PriorityEmergencyRunner:
    """Hardware-free runner seam proving global e-stop bypasses a full routine queue."""

    def __init__(self) -> None:
        self.calls: list[OperatorAction] = []
        self.fault_acknowledgement_invalidations = 0

    def invalidate_fault_acknowledgement_for_emergency_stop(self) -> None:
        """Model the live runner's synchronous acknowledgement fence."""
        self.fault_acknowledgement_invalidations += 1

    async def dispatch_priority_emergency_stop(self, item: QueuedOperatorAction) -> bool:
        """Record the controller-owned priority dispatch."""
        assert item.action is OperatorAction.EMERGENCY_STOP
        self.calls.append(item.action)
        return True


@pytest.mark.asyncio
async def test_global_estop_bypasses_full_routine_queue_for_live_runner(tmp_path: Path) -> None:
    """A full routine queue cannot prevent a controller-owned e-stop attempt."""
    store = RoastStore(tmp_path / "full-queue-estop.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        runner = _PriorityEmergencyRunner()
        service.runner = runner  # type: ignore[assignment]  # noqa: SLF001 - narrow runner seam
        service.active_run_id = "private-run"
        service._fault_lease_cache = FaultControlLease(  # noqa: SLF001 - race harness  # pyright: ignore[reportPrivateUsage]
            generation=1,
            status=FaultLeaseStatus.OPEN,
            run_id="private-run",
            mcp_session_id=None,
        )
        for _ in range(service.OPERATOR_QUEUE_MAX):
            service.operator_queue.put_nowait(
                QueuedOperatorAction(run_id="private-run", action=OperatorAction.START_COOLING)
            )

        result = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )

        assert result.result == "accepted"
        assert not result.queued
        assert runner.calls == [OperatorAction.EMERGENCY_STOP]
        assert runner.fault_acknowledgement_invalidations == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_terminal_estop_without_live_controller_does_not_claim_admission(
    tmp_path: Path,
) -> None:
    """A terminal row with no controller loop cannot report a queued e-stop."""
    store = RoastStore(tmp_path / "terminal-no-runner-estop.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        service._fault_lease_cache = FaultControlLease(  # noqa: SLF001 - race harness  # pyright: ignore[reportPrivateUsage]
            generation=1,
            status=FaultLeaseStatus.OPEN,
            run_id="private-terminal-run",
            mcp_session_id=None,
        )

        result = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )

        assert result.result == "failed"
        assert "independent physical stop" in result.reason
    finally:
        await store.close()
