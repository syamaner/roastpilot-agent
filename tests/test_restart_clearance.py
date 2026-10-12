"""D213 process-clearance and fault-control lease tests (hardware-free)."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest

from roastpilot_agent.api import (
    QueuedOperatorAction,
    RoastRunConflictError,
    RoastService,
    ShutdownHeatOffOutcome,
)
from roastpilot_agent.cli import _teardown_live  # pyright: ignore[reportPrivateUsage]
from roastpilot_agent.config import AppConfig, ControllerConfig
from roastpilot_agent.mcp_client import MCPServerProcess, SessionPresence
from roastpilot_agent.models import (
    FaultControlsActionRequest,
    FaultControlState,
    FaultLeaseStatus,
    OperatorAction,
    OperatorActionRequest,
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

    async def read_latest_fault_control_state(self) -> FaultControlState:
        """Return the same state as the latest-session proof for this fake."""
        return self.state

    async def write_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> None:
        assert expected_session_id == self.state.session_id
        self.calls.append((action.value, {"expected_session_id": expected_session_id}))
        if action in (OperatorAction.STOP_COOLING, OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE):
            if self.stop_started is not None and self.release_stop is not None:
                self.stop_started.set()
                await self.release_stop.wait()
            self.state = self.state.model_copy(
                update={"cooling_on": False, "heat_level_percent": 0, "fan_level_percent": 0}
            )
        return None

    async def execute_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> FaultControlState:
        await self.write_fault_control(action, expected_session_id=expected_session_id)
        return await self.read_fault_control_state(expected_session_id)


class _HistoricalTerminalFaultPort(_TerminalFaultPort):
    """Port exposing a stale exact snapshot and a distinct latest session."""

    def __init__(self, state: FaultControlState, latest_state: FaultControlState) -> None:
        super().__init__(state)
        self.latest_state = latest_state

    async def read_latest_fault_control_state(self) -> FaultControlState:
        """Return the current latest session rather than the historical lease."""
        return self.latest_state


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
async def test_fresh_no_session_clearance_allows_a_live_start_after_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh D213 clearance permits Start only after current-child proof."""
    store = RoastStore(tmp_path / "restart-none-start.sqlite3")
    await store.initialize()
    service: RoastService | None = None
    try:
        service, roaster, _child = await _presence_service(store, "none")
        with pytest.raises(RoastRunConflictError, match="restart clearance"):
            await service.start_roast(_profile())

        cleared = await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert cleared.cleared

        async def no_op_respawn(_config: object) -> None:
            return None

        monkeypatch.setattr(service, "_respawn_mcp_for_device_config", no_op_respawn)
        await service.start_roast(_profile())
        assert roaster.presence_reads == 1
    finally:
        if service is not None:
            await service.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_clean_predecessor_clearance_cannot_clear_a_fresh_process(
    tmp_path: Path,
) -> None:
    """A clean predecessor leaves the next Agent process gated until reproved."""
    store = RoastStore(tmp_path / "restart-fresh-process.sqlite3")
    await store.initialize()
    try:
        predecessor, predecessor_roaster, _predecessor_child = await _presence_service(
            store, "none"
        )
        predecessor_result = await predecessor.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert predecessor_result.cleared
        assert predecessor_roaster.presence_reads == 1
        assert await store.active_run() is None
        await predecessor.mark_process_epoch_clean_after_ordered_teardown()

        successor, successor_roaster, _successor_child = await _presence_service(store, "none")
        successor_health = await successor.health()
        assert successor_health.restart_clearance.state is RestartClearanceState.REQUIRED
        assert not successor_health.restart_clearance.eligible
        assert successor_roaster.presence_reads == 0
        with pytest.raises(RoastRunConflictError, match="restart clearance"):
            await successor.start_roast(_profile())

        successor_result = await successor.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert successor_result.cleared
        assert successor_roaster.presence_reads == 1
        assert (
            await store.read_process_restart_clearance(successor.instance_id)
            is RestartClearanceState.CLEARED
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_idle_ordered_teardown_allows_successor_physical_clearance_and_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle ordered teardown creates a trusted baseline, never a clearance waiver."""
    path = tmp_path / "idle-ordered-teardown.sqlite3"
    store = RoastStore(path)
    await store.initialize()
    try:
        predecessor, predecessor_roaster, _predecessor_child = await _presence_service(
            store, "none"
        )
        predecessor_clearance = await predecessor.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert predecessor_clearance.cleared
        assert predecessor_roaster.presence_reads == 1

        class _CleanStoppingMCP:
            stop_unconfirmed = False

            async def stop(self) -> None:
                return None

        await _teardown_live(predecessor, cast(MCPServerProcess, _CleanStoppingMCP()), store)
    finally:
        await store.close()

    successor_store = RoastStore(path)
    await successor_store.initialize()
    try:
        successor, successor_roaster, _successor_child = await _presence_service(
            successor_store, "none"
        )
        successor_health = await successor.health()
        assert successor_health.restart_clearance.state is RestartClearanceState.REQUIRED
        with pytest.raises(RoastRunConflictError, match="restart clearance"):
            await successor.start_roast(_profile())

        successor_clearance = await successor.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert successor_clearance.cleared
        assert successor_roaster.presence_reads == 1

        async def no_op_respawn(_config: object) -> None:
            return None

        monkeypatch.setattr(successor, "_respawn_mcp_for_device_config", no_op_respawn)
        detail = await successor.start_roast(_profile())
        assert detail.id
        assert detail.agent_phase is RoastPhase.PREHEATING
    finally:
        await successor_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_gate", ["missing", "required", "read_failure"])
async def test_clean_epoch_requires_durable_restart_clearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_gate: str
) -> None:
    """An in-memory CLEARED projection cannot certify an uncertain D213 gate."""
    path = tmp_path / f"uncertain-clean-epoch-{durable_gate}.sqlite3"
    store = RoastStore(path)
    await store.initialize()
    try:
        service, _roaster, _child = await _presence_service(store, "none")
        await service.health()
        service._restart_clearance_state = RestartClearanceState.CLEARED  # noqa: SLF001 - durable gate fence harness  # pyright: ignore[reportPrivateUsage]
        if durable_gate == "missing":
            await store.connection.execute(
                "DELETE FROM process_restart_clearance WHERE process_id = ?",
                (service.instance_id,),
            )
            await store.connection.commit()
        elif durable_gate == "read_failure":

            async def fail_gate_read(_process_id: str) -> RestartClearanceState:
                raise RuntimeError("synthetic restart gate read failure")

            monkeypatch.setattr(store, "read_process_restart_clearance", fail_gate_read)

        await service.mark_process_epoch_clean_after_ordered_teardown()
    finally:
        await store.close()

    successor_store = RoastStore(path)
    await successor_store.initialize()
    try:
        # The attempted clean marker did not commit, so an absent lease remains
        # UNKNOWN for the successor rather than becoming a fresh baseline.
        assert not await successor_store.begin_process_epoch("successor")
        assert (await successor_store.read_fault_control_lease()).status is FaultLeaseStatus.UNKNOWN
    finally:
        await successor_store.close()


@pytest.mark.asyncio
async def test_graceful_teardown_after_failed_open_lease_persistence_blocks_successor_clearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A graceful teardown cannot clean an epoch after OPEN persistence fails."""
    path = tmp_path / "crashed-estop.sqlite3"
    store = RoastStore(path)
    await store.initialize()
    try:
        first, _first_roaster, _first_child = await _presence_service(store, "none")
        cleared = await first.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert cleared.cleared
        first.active_run_id = "private-run"
        runner = _PriorityEmergencyRunner()
        first.runner = runner  # type: ignore[assignment]  # noqa: SLF001 - controlled admission seam
        failed = asyncio.Event()

        async def fail_open_write(
            *, run_id: str, mcp_session_id: str, generation: int | None = None
        ) -> FaultControlLease:
            assert run_id == "private-run"
            assert mcp_session_id
            assert generation == 1
            failed.set()
            raise RuntimeError("synthetic lease write failure")

        monkeypatch.setattr(store, "open_fault_control_lease", fail_open_write)

        marker_reached = asyncio.Event()
        original_marker = first.mark_process_epoch_clean_after_ordered_teardown

        async def record_marker_reached() -> None:
            assert runner.calls == [OperatorAction.EMERGENCY_STOP]
            marker_reached.set()
            await original_marker()

        monkeypatch.setattr(
            first, "mark_process_epoch_clean_after_ordered_teardown", record_marker_reached
        )

        class _CleanStoppingMCP:
            stop_unconfirmed = False

            async def stop(self) -> None:
                return None

        await _teardown_live(first, cast(MCPServerProcess, _CleanStoppingMCP()), store)
        await asyncio.wait_for(failed.wait(), timeout=1.0)
        assert runner.calls == [OperatorAction.EMERGENCY_STOP]
        assert runner.shutdown_admissions == 1
        assert not marker_reached.is_set()
    finally:
        await store.close()

    successor_store = RoastStore(path)
    await successor_store.initialize()
    try:
        successor, _roaster, _child = await _presence_service(successor_store, "none")
        await successor.recover_on_start()
        health = await successor.health()
        assert health.fault_controls.status is FaultLeaseStatus.UNKNOWN
        assert health.fault_controls.enabled_actions == [OperatorAction.EMERGENCY_STOP]
        with pytest.raises(RoastRunConflictError, match="fault controls are unknown"):
            await successor.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        with pytest.raises(RoastRunConflictError):
            await successor.start_roast(_profile())
    finally:
        await successor_store.close()


@pytest.mark.asyncio
async def test_startup_lease_read_failure_can_recover_a_fresh_closed_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient startup read failure does not permanently invent generation one."""
    store = RoastStore(tmp_path / "transient-startup-lease-read.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        original = store.read_fault_control_lease
        reads = 0

        async def fail_once() -> FaultControlLease:
            nonlocal reads
            reads += 1
            if reads == 1:
                raise RuntimeError("synthetic startup read failure")
            return await original()

        monkeypatch.setattr(store, "read_fault_control_lease", fail_once)
        await service.recover_on_start()
        assert service._fault_lease_cache.status is FaultLeaseStatus.UNKNOWN  # noqa: SLF001 - startup fence assertion  # pyright: ignore[reportPrivateUsage]
        recovered = await service._read_fault_lease_fail_closed()  # noqa: SLF001 - recovery seam  # pyright: ignore[reportPrivateUsage]
        assert recovered.generation == 0
        assert recovered.status is FaultLeaseStatus.CLOSED
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
async def test_no_session_clearance_rejects_child_replacement_during_durable_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child replacement while the durable confirmation awaits cannot clear memory."""
    store = RoastStore(tmp_path / "restart-confirm-child-race.sqlite3")
    await store.initialize()
    try:
        service, _roaster, child = await _presence_service(store, "none")
        confirmation_started = asyncio.Event()
        release_confirmation = asyncio.Event()
        original_confirm = store.confirm_process_restart_clearance

        async def delayed_confirmation(process_id: str) -> bool:
            confirmation_started.set()
            await release_confirmation.wait()
            return await original_confirm(process_id)

        monkeypatch.setattr(store, "confirm_process_restart_clearance", delayed_confirmation)
        clearance = asyncio.create_task(
            service.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        )
        await confirmation_started.wait()
        child.child_epoch += 1
        release_confirmation.set()

        with pytest.raises(RoastRunConflictError, match="evidence became stale"):
            await clearance
        health = await service.health()
        assert health.restart_clearance.state is RestartClearanceState.UNKNOWN
        assert not health.restart_clearance.eligible
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_no_session_clearance_rejects_estop_admission_during_durable_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synchronous e-stop admission during durable confirmation keeps Start blocked."""
    store = RoastStore(tmp_path / "restart-confirm-estop-race.sqlite3")
    await store.initialize()
    try:
        service, _roaster, _child = await _presence_service(store, "none")
        confirmation_started = asyncio.Event()
        release_confirmation = asyncio.Event()
        original_confirm = store.confirm_process_restart_clearance

        async def delayed_confirmation(process_id: str) -> bool:
            confirmation_started.set()
            await release_confirmation.wait()
            return await original_confirm(process_id)

        monkeypatch.setattr(store, "confirm_process_restart_clearance", delayed_confirmation)
        clearance = asyncio.create_task(
            service.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        )
        await confirmation_started.wait()
        service.active_run_id = "private-run"
        emergency_stop = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )
        assert emergency_stop.result == "failed"
        release_confirmation.set()

        with pytest.raises(RoastRunConflictError, match="evidence became stale"):
            await clearance
        await asyncio.sleep(0)
        health = await service.health()
        assert health.restart_clearance.state is RestartClearanceState.UNKNOWN
        assert not health.restart_clearance.eligible
        assert health.fault_controls.status is FaultLeaseStatus.UNKNOWN
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_open_fault_clearance_rejects_child_replacement_during_durable_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact-session proof cannot survive replacement of its current MCP child."""
    store = RoastStore(tmp_path / "restart-confirm-open-child-race.sqlite3")
    await store.initialize()
    try:
        service, _roaster, child = await _presence_service(store, "none")
        await store.create_run(
            run_id="private-run",
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.FAULTED,
        )
        await store.open_fault_control_lease(run_id="private-run", mcp_session_id="private-session")
        roaster = service._roaster  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert isinstance(roaster, FakeMCPClient)
        roaster.set_latest_fault_session_id("private-session")
        confirmation_started = asyncio.Event()
        release_confirmation = asyncio.Event()
        original_confirm = store.confirm_process_restart_clearance

        async def delayed_confirmation(process_id: str) -> bool:
            confirmation_started.set()
            await release_confirmation.wait()
            return await original_confirm(process_id)

        monkeypatch.setattr(store, "confirm_process_restart_clearance", delayed_confirmation)
        clearance = asyncio.create_task(
            service.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        )
        await confirmation_started.wait()
        child.child_epoch += 1
        release_confirmation.set()

        with pytest.raises(RoastRunConflictError, match="evidence became stale"):
            await clearance
        health = await service.health()
        assert health.restart_clearance.state is RestartClearanceState.UNKNOWN
        assert not health.restart_clearance.eligible
        assert health.fault_controls.status is FaultLeaseStatus.OPEN
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
        roaster = service._roaster  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert isinstance(roaster, FakeMCPClient)
        roaster.set_latest_fault_session_id("private-session")

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
        assert health.fault_controls.enabled_actions == [OperatorAction.EMERGENCY_STOP]
        rejected = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.STOP_COOLING)
        )
        assert rejected.result == "rejected"
        assert port.calls == []

        port.set_latest_fault_session_id("private-session")
        clearance = await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert clearance.cleared
        health = await service.health()
        assert OperatorAction.DROP_BEANS not in health.fault_controls.enabled_actions
        assert OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE in health.fault_controls.enabled_actions

        acknowledged = await service.submit_fault_controls_action(
            FaultControlsActionRequest(
                action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
                confirmation=True,
                expected_generation=1,
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
        port.set_latest_fault_session_id("private-session")
        clearance = await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert clearance.cleared
        await service.health()  # Hydrate the server-owned terminal lease cache.

        acknowledgement = asyncio.create_task(
            service.submit_fault_controls_action(
                FaultControlsActionRequest(
                    action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
                    confirmation=True,
                    expected_generation=1,
                )
            )
        )
        await asyncio.wait_for(port.stop_started.wait(), timeout=1.0)
        emergency_stop = asyncio.create_task(
            service.submit_fault_controls_action(
                FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
            )
        )
        port.release_stop.set()
        acknowledgement_result = await asyncio.wait_for(acknowledgement, timeout=1.0)
        emergency_result = await asyncio.wait_for(emergency_stop, timeout=1.0)

        assert acknowledgement_result.result == "failed"
        assert emergency_result.result == "confirmed"
        controls = await service._fault_controls_projection_after_action()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert controls.status in (FaultLeaseStatus.OPEN, FaultLeaseStatus.UNKNOWN)
        assert controls.generation is not None and controls.generation > 1
        assert OperatorAction.EMERGENCY_STOP in controls.enabled_actions
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_terminal_fault_controls_wait_for_fresh_process_clearance(
    tmp_path: Path,
) -> None:
    """A terminal lease exposes only e-stop until this process is cleared."""
    store = RoastStore(tmp_path / "terminal-restart-clearance.sqlite3")
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
        assert health.restart_clearance.state is RestartClearanceState.REQUIRED
        assert health.fault_controls.enabled_actions == [OperatorAction.EMERGENCY_STOP]
        result = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.STOP_COOLING)
        )

        assert result.result == "rejected"
        assert port.calls == []
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "connected", "heat"),
    [(False, True, 1), (True, True, 0), (False, False, 0)],
)
async def test_terminal_projection_hides_cooling_stop_when_state_is_unsafe(
    tmp_path: Path, active: bool, connected: bool, heat: int
) -> None:
    """Unsafe exact-state evidence never advertises a terminal cooling stop."""
    store = RoastStore(tmp_path / f"unsafe-{active}-{connected}-{heat}.sqlite3")
    await store.initialize()
    try:
        port = _TerminalFaultPort(
            FaultControlState(
                session_id="private-session",
                mcp_phase="fault",
                active=active,
                device_connected=connected,
                heat_level_percent=heat,
                fan_level_percent=0,
                cooling_on=True,
                beans_added=False,
                beans_dropped=False,
            )
        )
        service = await _terminal_service(store, port)
        service._restart_clearance_state = RestartClearanceState.CLEARED  # noqa: SLF001 - projection harness  # pyright: ignore[reportPrivateUsage]
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

        assert OperatorAction.STOP_COOLING not in health.fault_controls.enabled_actions
        assert (
            OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE not in health.fault_controls.enabled_actions
        )
        result = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.STOP_COOLING)
        )
        assert result.result == "rejected"
        assert port.calls == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_terminal_d212_rejects_historical_session_with_different_latest_session(
    tmp_path: Path,
) -> None:
    """A stale exact snapshot cannot close when MCP latest identity changed."""
    store = RoastStore(tmp_path / "historical-terminal-d212.sqlite3")
    await store.initialize()
    try:
        stale = FaultControlState(
            session_id="private-session",
            mcp_phase="fault",
            active=False,
            device_connected=True,
            heat_level_percent=0,
            fan_level_percent=0,
            cooling_on=False,
            beans_added=False,
            beans_dropped=False,
        )
        latest = stale.model_copy(update={"session_id": "different-session"})
        port = _HistoricalTerminalFaultPort(stale, latest)
        service = await _terminal_service(store, port)
        service._restart_clearance_state = RestartClearanceState.CLEARED  # noqa: SLF001 - exact-session harness  # pyright: ignore[reportPrivateUsage]
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
        await service.health()

        result = await service.submit_fault_controls_action(
            FaultControlsActionRequest(
                action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
                confirmation=True,
                expected_generation=1,
            )
        )

        assert result.result == "rejected"
        assert port.calls == []
        assert (await store.read_fault_control_lease()).status is FaultLeaseStatus.OPEN
    finally:
        await store.close()


class _PriorityEmergencyRunner:
    """Hardware-free runner seam proving global e-stop bypasses a full routine queue."""

    def __init__(self, run_id: str = "private-run") -> None:
        self.run_id = run_id
        self.calls: list[OperatorAction] = []
        self.fault_acknowledgement_invalidations = 0
        self.shutdown_admissions = 0

    def invalidate_fault_acknowledgement_for_emergency_stop(self) -> None:
        """Model the live runner's synchronous acknowledgement fence."""
        self.fault_acknowledgement_invalidations += 1

    def admit_emergency_stop(self) -> None:
        """Model the synchronous controller-arbiter priority admission."""
        self.shutdown_admissions += 1

    def controller_snapshot(self) -> object:
        """Expose an active phase for the graceful-shutdown admission seam."""
        return type("Snapshot", (), {"phase": RoastPhase.PREHEATING})()

    async def dispatch_priority_emergency_stop(
        self, item: QueuedOperatorAction, *, already_admitted: bool = False
    ) -> bool:
        """Record the controller-owned priority dispatch."""
        assert item.action is OperatorAction.EMERGENCY_STOP
        assert already_admitted
        self.calls.append(item.action)
        return True

    async def shutdown_heat_off(self, *, already_admitted: bool = False) -> ShutdownHeatOffOutcome:
        """Dispatch a graceful heat-off through the admitted priority seam."""
        await self.dispatch_priority_emergency_stop(
            QueuedOperatorAction(run_id=self.run_id, action=OperatorAction.EMERGENCY_STOP),
            already_admitted=already_admitted,
        )
        return ShutdownHeatOffOutcome.DISPATCHED

    async def retry_shutdown_heat_off(
        self, *, already_admitted: bool = False
    ) -> ShutdownHeatOffOutcome:
        """Dispatch a retry through the same admitted priority seam."""
        return await self.shutdown_heat_off(already_admitted=already_admitted)

    async def record_shutdown_unconfirmed(self, *, command: str, reason: str) -> None:
        """Provide the service's runner persistence seam for teardown tests."""
        raise AssertionError(f"unexpected unconfirmed shutdown: {command} / {reason}")


@pytest.mark.asyncio
async def test_global_estop_dispatches_before_delayed_open_lease_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E-stop dispatch and Start blocking do not wait for the real OPEN lease write."""
    store = RoastStore(tmp_path / "delayed-open-lease.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        service._restart_clearance_state = RestartClearanceState.CLEARED  # noqa: SLF001 - admission harness  # pyright: ignore[reportPrivateUsage]
        service._restart_epoch_initialized = True  # noqa: SLF001 - admission harness  # pyright: ignore[reportPrivateUsage]
        await store.create_run(
            run_id="private-run",
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.FAULTED,
        )
        runner = _PriorityEmergencyRunner()
        service.runner = runner  # type: ignore[assignment]  # noqa: SLF001 - narrow runner seam
        service.active_run_id = "private-run"
        service._fault_lease_cache = FaultControlLease(  # noqa: SLF001 - admission harness  # pyright: ignore[reportPrivateUsage]
            generation=1,
            status=FaultLeaseStatus.OPEN,
            run_id="private-run",
            mcp_session_id="private-session",
        )
        write_started = asyncio.Event()
        release_write = asyncio.Event()
        write_finished = asyncio.Event()
        original_open = store.open_fault_control_lease

        async def delayed_open(
            run_id: str, mcp_session_id: str, *, generation: int | None = None
        ) -> FaultControlLease:
            write_started.set()
            await release_write.wait()
            lease = await original_open(
                run_id=run_id, mcp_session_id=mcp_session_id, generation=generation
            )
            write_finished.set()
            return lease

        monkeypatch.setattr(store, "open_fault_control_lease", delayed_open)
        emergency = asyncio.create_task(
            service.submit_fault_controls_action(
                FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
            )
        )
        await asyncio.wait_for(write_started.wait(), timeout=1.0)

        assert runner.calls == [OperatorAction.EMERGENCY_STOP]
        assert service._fault_lease_cache.status is FaultLeaseStatus.UNKNOWN  # noqa: SLF001 - admission assertion  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(RoastRunConflictError, match="fault controls are not closed"):
            await service.start_roast(_profile())

        release_write.set()
        result = await emergency
        await asyncio.wait_for(write_finished.wait(), timeout=1.0)
        persisted = await store.read_fault_control_lease()
        assert result.result == "accepted"
        assert persisted.generation == 2
        assert persisted.status is FaultLeaseStatus.OPEN
        assert persisted.mcp_session_id == "private-session"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_global_estop_failed_open_lease_write_remains_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real failed OPEN write never turns an admitted e-stop into durable success."""
    store = RoastStore(tmp_path / "failed-open-lease.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        service._restart_clearance_state = RestartClearanceState.CLEARED  # noqa: SLF001 - admission harness  # pyright: ignore[reportPrivateUsage]
        service._restart_epoch_initialized = True  # noqa: SLF001 - admission harness  # pyright: ignore[reportPrivateUsage]
        runner = _PriorityEmergencyRunner()
        service.runner = runner  # type: ignore[assignment]  # noqa: SLF001 - narrow runner seam
        service.active_run_id = "private-run"
        service._fault_lease_cache = FaultControlLease(  # noqa: SLF001 - admission harness  # pyright: ignore[reportPrivateUsage]
            generation=1,
            status=FaultLeaseStatus.OPEN,
            run_id="private-run",
            mcp_session_id="private-session",
        )
        persistence_failed = asyncio.Event()
        callback_ran = asyncio.Event()
        original_callback = service._log_fault_lease_persistence_failure  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        async def fail_open(
            run_id: str, mcp_session_id: str, *, generation: int | None = None
        ) -> FaultControlLease:
            assert run_id == "private-run"
            assert mcp_session_id == "private-session"
            assert generation == 2
            persistence_failed.set()
            raise RuntimeError("synthetic OPEN persistence failure")

        def observe_failure(task: asyncio.Task[None], generation: int) -> None:
            original_callback(task, generation)
            callback_ran.set()

        monkeypatch.setattr(store, "open_fault_control_lease", fail_open)
        monkeypatch.setattr(service, "_log_fault_lease_persistence_failure", observe_failure)
        result = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )
        await asyncio.wait_for(persistence_failed.wait(), timeout=1.0)
        await asyncio.wait_for(callback_ran.wait(), timeout=1.0)

        assert result.result == "accepted"
        assert runner.calls == [OperatorAction.EMERGENCY_STOP]
        assert service._fault_lease_cache.status is FaultLeaseStatus.UNKNOWN  # noqa: SLF001 - failure assertion  # pyright: ignore[reportPrivateUsage]
        persisted = await store.read_fault_control_lease()
        assert persisted.generation == 0
        assert persisted.status is FaultLeaseStatus.CLOSED
        with pytest.raises(RoastRunConflictError, match="fault controls are not closed"):
            await service.start_roast(_profile())
    finally:
        await store.close()


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
async def test_global_estop_targets_current_live_run_not_closed_lease(tmp_path: Path) -> None:
    """A closed historical lease cannot redirect a later live-run e-stop."""
    store = RoastStore(tmp_path / "two-run-global-estop.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        runner = _PriorityEmergencyRunner(run_id="current-run")
        service.runner = runner  # type: ignore[assignment]  # noqa: SLF001
        service.active_run_id = "current-run"
        # The real adapter exposes its most recently observed MCP state.  A
        # current live runner has therefore established this session identity;
        # it must replace the unrelated historical lease identity below.
        roaster = service._roaster  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert isinstance(roaster, FakeMCPClient)
        roaster.set_latest_fault_session_id("current-session")
        service._fault_lease_cache = FaultControlLease(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            generation=3,
            status=FaultLeaseStatus.CLOSED,
            run_id="old-run",
            mcp_session_id="old-session",
        )

        result = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )

        assert result.result == "accepted"
        assert runner.calls == [OperatorAction.EMERGENCY_STOP]
        assert service._fault_lease_cache.run_id == "current-run"  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert service._fault_lease_cache.mcp_session_id == "current-session"  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recovered_run_blocks_routine_actions_until_clearance_but_admits_estop(
    tmp_path: Path,
) -> None:
    """D213 blocks recovery writes while retaining the independent e-stop route."""
    store = RoastStore(tmp_path / "recovered-routine-gate.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        await store.create_run(
            run_id="recovered-run",
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.OPERATOR_RECOVERY_REQUIRED,
        )
        runner = _PriorityEmergencyRunner(run_id="recovered-run")
        service.runner = runner  # type: ignore[assignment]  # noqa: SLF001
        service.active_run_id = "recovered-run"

        with pytest.raises(RoastRunConflictError, match="restart clearance"):
            await service.submit_operator_action(
                "recovered-run", OperatorActionRequest(action=OperatorAction.RESUME_ADVISORY)
            )
        emergency = await service.submit_operator_action(
            "recovered-run", OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )
        assert emergency.result == "accepted"
        assert runner.calls == [OperatorAction.EMERGENCY_STOP]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_per_run_estop_dispatches_before_store_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live per-run e-stop never waits for ordinary action-store admission."""
    store = RoastStore(tmp_path / "per-run-estop-no-read.sqlite3")
    await store.initialize()
    try:
        service = await _service(store)
        await store.create_run(
            run_id="live-run",
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.PREHEATING,
        )
        runner = _PriorityEmergencyRunner(run_id="live-run")
        service.runner = runner  # type: ignore[assignment]  # noqa: SLF001
        service.active_run_id = "live-run"

        async def unexpected_read(_run_id: str) -> object:
            raise AssertionError("per-run e-stop must not read the run before dispatch")

        monkeypatch.setattr(store, "read_run", unexpected_read)
        result = await service.submit_operator_action(
            "live-run", OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )

        assert result.result == "accepted"
        assert runner.calls == [OperatorAction.EMERGENCY_STOP]
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
