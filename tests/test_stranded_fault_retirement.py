"""D225 stranded-fault retirement service behaviour (hardware-free)."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

import roastpilot_agent.api as api_module
from roastpilot_agent.api import (
    RoastRunConflictError,
    RoastService,
    _DetachedRecoveryLoop,  # pyright: ignore[reportPrivateUsage]
    create_app,
)
from roastpilot_agent.config import AppConfig, ControllerConfig
from roastpilot_agent.mcp_client import MCPServerProcess
from roastpilot_agent.models import (
    FaultControlState,
    FaultLeaseClosureProvenance,
    FaultLeaseStatus,
    OperatorAction,
    OperatorActionRequest,
    RestartClearanceRequest,
    RestartClearanceState,
    RoastEventKind,
    RoastPhase,
    RoastProfile,
    StrandedFaultRetirementEventData,
    StrandedFaultRetirementRequest,
)
from roastpilot_agent.store import (
    FaultControlLease,
    RoastStore,
    StrandedFaultRetirementCommit,
)
from tests.conftest import FakeClock, FakeMCPClient


class _CurrentChild:
    """Minimal running-child seam for the current-session proof."""

    running = True
    child_epoch = 1
    stop_unconfirmed = False
    teardown_incident_id: str | None = None


class _SafeZeroFaultPort(FakeMCPClient):
    """Hardware-free current-session port advertising only D212 safe-zero state."""

    def __init__(self) -> None:
        super().__init__()
        self.on_latest: Callable[[], None] | None = None
        self.state = FaultControlState(
            session_id="fault-session",
            mcp_phase="fault",
            active=False,
            device_connected=True,
            heat_level_percent=0,
            fan_level_percent=0,
            cooling_on=False,
            beans_added=False,
            beans_dropped=False,
        )

    async def read_fault_control_state(self, session_id: str) -> FaultControlState:
        """Return the exact safe-zero state for the retained test lease."""
        assert session_id == "fault-session"
        return self.state

    async def read_latest_fault_control_state(self) -> FaultControlState:
        """Return the same current state after an optional projection race hook."""
        if self.on_latest is not None:
            self.on_latest()
        return self.state


class _PendingLiveRunner:
    """Minimal live-runner seam for the two controller e-stop dispatch sites."""

    finalized = False

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.invalidations = 0
        self.admissions = 0

    def invalidate_fault_acknowledgement_for_emergency_stop(self) -> None:
        """Record the synchronous e-stop invalidation."""
        self.invalidations += 1

    def admit_emergency_stop(self) -> None:
        """Record priority admission before task creation."""
        self.admissions += 1


def _profile() -> RoastProfile:
    """Return one valid persisted profile for a terminal faulted row."""
    return RoastProfile(
        name="Test bean",
        bean_origin="Test origin",
        bean_weight_grams=250,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=195,
        target_development_percent=14,
    )


async def _eligible_service(store: RoastStore) -> tuple[RoastService, FakeMCPClient]:
    """Build the smallest terminal-faulted, exact-session D225 fixture."""
    run_id = "terminal-faulted-run"
    await store.create_run(
        run_id=run_id,
        profile=_profile(),
        config=AppConfig(),
        agent_phase=RoastPhase.FAULTED,
    )
    await store.complete_run(run_id=run_id, outcome="faulted", agent_phase=RoastPhase.FAULTED)
    await store.open_fault_control_lease(
        run_id=run_id, mcp_session_id="fault-session", generation=1
    )
    roaster = FakeMCPClient()

    async def no_session() -> str:
        return "none"

    roaster.read_session_presence = no_session  # type: ignore[method-assign]
    service = RoastService(
        store,
        config=AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0)),
        mcp=cast("MCPServerProcess", _CurrentChild()),
        roaster=roaster,
        live_serve_mode=True,
        run_loop=False,
        clock=FakeClock(),
    )
    await service.recover_on_start()
    return service, roaster


@pytest.mark.asyncio
async def test_retirement_closes_only_the_current_terminal_faulted_lease(
    tmp_store: RoastStore,
) -> None:
    """A current no-session proof can close D225 without an MCP write or Start grant."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        assert service._retirement_conditions_hold()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        identity = service._capture_retirement_identity()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert identity is not None
        assert service._retirement_identity_holds(identity)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert (
            await tmp_store.read_process_restart_clearance(service.instance_id)
            is RestartClearanceState.REQUIRED
        )
        result = await service.retire_stranded_fault(
            StrandedFaultRetirementRequest(
                stranded_fault_confirmation=(
                    "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
                )
            )
        )
        lease = await tmp_store.read_fault_control_lease()

        assert result.outcome == "confirmed"
        assert result.attempted_generation == 1
        assert lease.status is FaultLeaseStatus.CLOSED
        assert result.restart_clearance.state is RestartClearanceState.REQUIRED
        assert result.restart_clearance.eligible is False
        assert roaster.calls == []
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("presence", ["starting", "active", "stopped"])
async def test_retirement_rejects_every_non_none_session_presence(
    tmp_store: RoastStore, presence: str
) -> None:
    """D225 keeps the durable OPEN lease when current child presence is not none."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)

        async def read_presence() -> str:
            return presence

        roaster.read_session_presence = read_presence  # type: ignore[method-assign]
        result = await service.retire_stranded_fault(
            StrandedFaultRetirementRequest(
                stranded_fault_confirmation=(
                    "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
                )
            )
        )

        assert result.outcome == "rejected"
        assert (await tmp_store.read_fault_control_lease()).status is FaultLeaseStatus.OPEN
        assert roaster.calls == []
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_owned_fence_reconciles_exactly_once_without_a_second_transaction(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained own fence can only use the store's exact read-only verifier."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        original_retire = tmp_store.retire_stranded_fault_lease
        original_verify = tmp_store.verify_stranded_fault_retirement
        retire_calls = 0
        verifier_calls: list[dict[str, object]] = []
        commit_outcomes: list[StrandedFaultRetirementCommit] = []

        async def indeterminate_after_commit(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal retire_calls
            retire_calls += 1
            # Keep the store's internal post-commit verification real.  The
            # service's recovery verifier is installed only after it returns.
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", original_verify)
            committed = await original_retire(**kwargs)  # type: ignore[arg-type]
            commit_outcomes.append(committed)
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", service_verifier)
            return StrandedFaultRetirementCommit.INDETERMINATE

        async def service_verifier(**kwargs: object) -> bool:
            verifier_calls.append(dict(kwargs))
            return len(verifier_calls) == 2 and await original_verify(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate_after_commit)
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", service_verifier)
        request = StrandedFaultRetirementRequest(
            stranded_fault_confirmation=(
                "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
            )
        )

        first = await service.retire_stranded_fault(request)
        closed_after_commit = await tmp_store.read_fault_control_lease()
        second = await service.retire_stranded_fault(request)

        assert first.outcome == "unconfirmed"
        assert second.outcome == "confirmed"
        assert retire_calls == 1
        assert commit_outcomes == [StrandedFaultRetirementCommit.COMMITTED_VERIFIED]
        assert closed_after_commit.status is FaultLeaseStatus.CLOSED
        assert len(verifier_calls) == 2
        assert verifier_calls[0] == verifier_calls[1]
        assert verifier_calls[0]["process_id"] == service.instance_id
        assert verifier_calls[0]["generation"] == 1
        assert verifier_calls[0]["run_id"] == "terminal-faulted-run"
        assert verifier_calls[0]["mcp_session_id"] == "fault-session"
        assert isinstance(verifier_calls[0]["operation_id"], str)
        async with tmp_store.connection.execute(
            "SELECT COUNT(*) FROM operator_actions WHERE action = ?",
            (OperatorAction.RETIRE_STRANDED_FAULT.value,),
        ) as cursor:
            audit_row = await cursor.fetchone()
        assert audit_row is not None
        audit_count = int(audit_row[0])
        assert audit_count == 1
        assert (await tmp_store.read_fault_control_lease()).status is FaultLeaseStatus.CLOSED
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_binding",
    [
        "child_epoch",
        "process",
        "stop_unconfirmed",
        "foreign_fence",
        "d220_fence",
        "estop_unknown",
    ],
)
async def test_foreign_child_binding_cannot_reconcile_an_owned_fence(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, stale_binding: str
) -> None:
    """T-F1b: a stale own fence never authorises a second exact verifier read."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        original_retire = tmp_store.retire_stranded_fault_lease
        original_verify = tmp_store.verify_stranded_fault_retirement
        verifier_calls: list[dict[str, object]] = []
        commit_outcomes: list[StrandedFaultRetirementCommit] = []

        async def indeterminate_after_commit(**kwargs: object) -> StrandedFaultRetirementCommit:
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", original_verify)
            commit_outcomes.append(await original_retire(**kwargs))  # type: ignore[arg-type]
            monkeypatch.setattr(
                tmp_store, "verify_stranded_fault_retirement", counted_false_verifier
            )
            return StrandedFaultRetirementCommit.INDETERMINATE

        async def counted_false_verifier(**kwargs: object) -> bool:
            verifier_calls.append(dict(kwargs))
            return False

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate_after_commit)
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", counted_false_verifier)
        request = StrandedFaultRetirementRequest(
            stranded_fault_confirmation=(
                "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
            )
        )
        assert (await service.retire_stranded_fault(request)).outcome == "unconfirmed"
        assert commit_outcomes == [StrandedFaultRetirementCommit.COMMITTED_VERIFIED]
        assert len(verifier_calls) == 1
        expected_outcome = "unconfirmed"
        if stale_binding == "child_epoch":
            child = cast("_CurrentChild", service._mcp)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            child.child_epoch += 1
        elif stale_binding == "process":
            service.instance_id = "a" * 32
        elif stale_binding == "stop_unconfirmed":
            # T-F1b's stop-unconfirmed case must enter with a valid captured
            # child, then become stale at an await.  T-F5 separately pins the
            # pre-capture stop-unconfirmed rejection.
            original_reader = service._read_fault_lease_fail_closed  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

            async def stop_after_lease_read() -> object:
                lease = await original_reader()
                cast("_CurrentChild", service._mcp).stop_unconfirmed = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                return lease

            monkeypatch.setattr(service, "_read_fault_lease_fail_closed", stop_after_lease_read)
            expected_outcome = "unconfirmed"
        elif stale_binding == "foreign_fence":
            fence = service._unconfirmed_stranded_fault_retirement  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            assert fence is not None
            service._unconfirmed_stranded_fault_retirement = replace(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                fence,
                identity=replace(fence.identity, process_id="c" * 32),
            )
        elif stale_binding == "d220_fence":
            service._unconfirmed_safety_record_generation = 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            service._note_emergency_stop_admission()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert (await service.retire_stranded_fault(request)).outcome == expected_outcome
        assert len(verifier_calls) == 1
        assert (await tmp_store.read_fault_control_lease()).status is FaultLeaseStatus.CLOSED
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb9_second_concurrent_request_rejects_while_one_transaction_is_owned(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB9: one in-flight proof owns the reservation and reaches one transaction."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        entered = asyncio.Event()
        release = asyncio.Event()
        transaction_calls = 0
        original_transaction = tmp_store.retire_stranded_fault_lease

        async def parked_presence() -> str:
            entered.set()
            await release.wait()
            return "none"

        async def counted_transaction(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal transaction_calls
            transaction_calls += 1
            return await original_transaction(**kwargs)  # type: ignore[arg-type]

        roaster.read_session_presence = parked_presence  # type: ignore[method-assign]
        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", counted_transaction)
        first = asyncio.create_task(service.retire_stranded_fault(_retirement_request()))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        second = await service.retire_stranded_fault(_retirement_request())
        release.set()

        assert second.outcome == "rejected"
        assert second.attempted_generation is None
        assert (await first).outcome == "confirmed"
        assert transaction_calls == 1
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_lower_generation_fence_is_inert_after_a_newer_open_lease(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained G fence cannot permanently reject an independently settled G+1."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        original_retire = tmp_store.retire_stranded_fault_lease
        original_verify = tmp_store.verify_stranded_fault_retirement
        verify_calls = 0

        async def indeterminate_after_commit(**kwargs: object) -> StrandedFaultRetirementCommit:
            committed = await original_retire(**kwargs)  # type: ignore[arg-type]
            return (
                StrandedFaultRetirementCommit.INDETERMINATE
                if committed is StrandedFaultRetirementCommit.COMMITTED_VERIFIED
                else committed
            )

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate_after_commit)

        async def fail_first_verification(**kwargs: object) -> bool:
            nonlocal verify_calls
            verify_calls += 1
            return verify_calls > 2 and await original_verify(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", fail_first_verification)
        request = StrandedFaultRetirementRequest(
            stranded_fault_confirmation=(
                "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
            )
        )
        assert (await service.retire_stranded_fault(request)).outcome == "unconfirmed"
        await tmp_store.open_fault_control_lease(
            run_id="terminal-faulted-run", mcp_session_id="fault-session", generation=2
        )
        await service.health()
        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", original_retire)
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", original_verify)

        result = await service.retire_stranded_fault(request)

        assert result.outcome == "confirmed"
        assert result.attempted_generation == 2
        assert (await tmp_store.read_fault_control_lease()).generation == 2
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_cancelling_http_waiter_keeps_owned_retirement_task_and_fence(
    tmp_store: RoastStore,
) -> None:
    """HTTP cancellation cannot cancel a reserved retirement transaction."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def parked_presence() -> str:
            entered.set()
            await release.wait()
            return "none"

        roaster.read_session_presence = parked_presence  # type: ignore[method-assign]
        request = StrandedFaultRetirementRequest(
            stranded_fault_confirmation=(
                "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
            )
        )
        waiter = asyncio.create_task(service.retire_stranded_fault(request))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert service._unconfirmed_stranded_fault_retirement is not None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        release.set()
        owned = service._stranded_fault_retirement  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert owned is not None
        assert (await owned[1]).outcome == "confirmed"
        assert (await tmp_store.read_fault_control_lease()).status is FaultLeaseStatus.CLOSED
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_retirement_stays_required_until_a_new_d213_presence_proof(
    tmp_store: RoastStore,
) -> None:
    """D225 closure is not restart clearance and D213 must re-read presence."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        retirement = StrandedFaultRetirementRequest(
            stranded_fault_confirmation=(
                "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
            )
        )
        assert (await service.retire_stranded_fault(retirement)).outcome == "confirmed"
        assert service._restart_clearance_state is RestartClearanceState.REQUIRED  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        async def active_session() -> str:
            return "active"

        roaster.read_session_presence = active_session  # type: ignore[method-assign]
        with pytest.raises(RoastRunConflictError):
            await service.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        assert service._restart_clearance_state is RestartClearanceState.REQUIRED  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_health_projects_retirement_without_an_mcp_presence_read(
    tmp_store: RoastStore,
) -> None:
    """D225 health eligibility is a store snapshot, not another MCP proof."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)

        async def unexpected_presence_read() -> str:
            raise AssertionError("health must not read MCP session presence")

        roaster.read_session_presence = unexpected_presence_read  # type: ignore[method-assign]
        health = await service.health()

        assert health.restart_clearance.enabled_actions == [OperatorAction.RETIRE_STRANDED_FAULT]
        assert health.fault_controls.physical_stop_operative is True
        assert health.fault_controls.closure_provenance is None
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_health_fails_closed_when_child_changes_during_snapshot_read(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child-epoch race cannot advertise stale D225 availability."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        original_latest = tmp_store.read_latest_run

        async def replace_child_during_latest_read() -> object:
            child = cast("_CurrentChild", service._mcp)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            child.child_epoch += 1
            return await original_latest()

        monkeypatch.setattr(tmp_store, "read_latest_run", replace_child_during_latest_read)
        health = await service.health()

        assert OperatorAction.RETIRE_STRANDED_FAULT not in health.restart_clearance.enabled_actions
        assert health.fault_controls.status is FaultLeaseStatus.OPEN
        assert health.fault_controls.physical_stop_operative is True
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_health_hides_retirement_while_its_owned_reservation_is_in_flight(
    tmp_store: RoastStore,
) -> None:
    """An in-flight D225 task is a projection fence even before durable state moves."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def parked_presence() -> str:
            entered.set()
            await release.wait()
            return "none"

        roaster.read_session_presence = parked_presence  # type: ignore[method-assign]
        request = StrandedFaultRetirementRequest(
            stranded_fault_confirmation=(
                "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
            )
        )
        attempt = asyncio.create_task(service.retire_stranded_fault(request))
        await asyncio.wait_for(entered.wait(), timeout=1.0)

        health = await service.health()

        assert OperatorAction.RETIRE_STRANDED_FAULT not in health.restart_clearance.enabled_actions
        assert health.fault_controls.physical_stop_operative is True
        release.set()
        assert (await attempt).outcome == "confirmed"
        await service.shutdown()
    finally:
        await tmp_store.close()


def _retirement_request() -> StrandedFaultRetirementRequest:
    """Return the one D225 operator attestation accepted by the API model."""
    return StrandedFaultRetirementRequest(
        stranded_fault_confirmation=(
            "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
        )
    )


async def _table_rows(store: RoastStore, table: str) -> list[tuple[object, ...]]:
    """Read durable table bytes in rowid order for a no-history-write assertion."""
    async with store.connection.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid") as cursor:
        rows = await cursor.fetchall()
    return [tuple(row) for row in rows]


async def _settled_task() -> asyncio.Task[None]:
    """Return a real, settled task for a transient reservation-state seam."""
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    return task


@pytest.mark.asyncio
async def test_tb6_tb7_estop_dispatch_preempts_held_retirement_transaction_and_g_plus_one_settles(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB6/TB7: priority e-stop dispatch runs before G's transaction releases."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        transaction_entered = asyncio.Event()
        release_transaction = asyncio.Event()
        dispatch_reached = asyncio.Event()
        release_dispatch = asyncio.Event()
        original_execute = cast("Any", aiosqlite.Connection._execute)  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]
        held_once = False

        async def hold_first_begin_immediate(
            connection: Any, function: Any, *args: Any, **kwargs: Any
        ) -> Any:
            nonlocal held_once
            result = await original_execute(connection, function, *args, **kwargs)
            if not held_once and args and args[0] == "BEGIN IMMEDIATE":
                held_once = True
                transaction_entered.set()
                await release_transaction.wait()
            return result

        async def observe_terminal_dispatch(**_kwargs: object) -> bool:
            dispatch_reached.set()
            await release_dispatch.wait()
            return False

        monkeypatch.setattr(aiosqlite.Connection, "_execute", hold_first_begin_immediate)
        monkeypatch.setattr(service, "_execute_terminal_emergency_stop", observe_terminal_dispatch)
        service._fault_control_executor = cast("Any", object())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        retirement = asyncio.create_task(service.retire_stranded_fault(_retirement_request()))
        await asyncio.wait_for(transaction_entered.wait(), timeout=2.0)
        estop = asyncio.create_task(service._submit_fault_controls_emergency_stop())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await asyncio.wait_for(dispatch_reached.wait(), timeout=2.0)

        assert service._fault_lease_cache.generation == 2  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert len(service._emergency_stop_dispatch_tasks) == 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        release_transaction.set()
        release_dispatch.set()
        retirement_result = await retirement
        estop_result = await estop

        assert retirement_result.outcome == "unconfirmed"
        assert estop_result.action is OperatorAction.EMERGENCY_STOP
        assert service._emergency_stop_dispatch_tasks == set()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert (await tmp_store.read_fault_control_lease()).generation == 2
        async with tmp_store.connection.execute(
            "SELECT COUNT(*) FROM operator_actions WHERE action = ?",
            (OperatorAction.RETIRE_STRANDED_FAULT.value,),
        ) as cursor:
            retirement_audit = await cursor.fetchone()
        assert retirement_audit is not None
        assert retirement_audit[0] == 1
        service._fault_control_executor = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["global_live", "per_run"])
async def test_mb14_each_live_dispatch_site_tracks_pending_task_then_discards_it_on_settlement(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """MB14: each actual live e-stop create-task site owns its visible task lifetime."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        run_id = f"mb14-{site}"
        await tmp_store.create_run(
            run_id=run_id,
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.PREHEATING,
        )
        runner = _PendingLiveRunner(run_id)
        service.active_run_id = run_id
        service.runner = cast("Any", runner)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def parked_dispatch(**_kwargs: object) -> bool:
            entered.set()
            await release.wait()
            return True

        monkeypatch.setattr(service, "_dispatch_admitted_live_emergency_stop", parked_dispatch)
        if site == "global_live":
            attempt = asyncio.create_task(service._submit_fault_controls_emergency_stop())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            attempt = asyncio.create_task(
                service._submit_live_emergency_stop(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                    run_id,
                    OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP),
                )
            )
        await asyncio.wait_for(entered.wait(), timeout=1.0)

        assert len(service._emergency_stop_dispatch_tasks) == 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert runner.invalidations == 1
        assert runner.admissions == 1
        release.set()
        await attempt

        assert service._emergency_stop_dispatch_tasks == set()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_state", ["current", "absent", "not_running", "stop_unconfirmed"])
async def test_t3_required_closed_keeps_emergency_stop_visible_in_result_and_health(
    tmp_store: RoastStore, child_state: str
) -> None:
    """T3: restart gating never removes the independent e-stop from CLOSED D225 state."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "confirmed"
        assert result.restart_clearance.state is RestartClearanceState.REQUIRED
        assert result.fault_controls.enabled_actions == [OperatorAction.EMERGENCY_STOP]
        if child_state == "absent":
            service._mcp = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif child_state == "not_running":
            cast("_CurrentChild", service._mcp).running = False  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif child_state == "stop_unconfirmed":
            cast("_CurrentChild", service._mcp).stop_unconfirmed = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        health = await service.health()

        assert health.restart_clearance.state is RestartClearanceState.REQUIRED
        assert health.fault_controls.status is FaultLeaseStatus.CLOSED
        assert health.fault_controls.enabled_actions == [OperatorAction.EMERGENCY_STOP]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_t4_fenced_false_reconciliations_emit_one_unconfirmed_frame_without_retrying(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T4: public UNKNOWN fencing does not suppress the one honest recovery event."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        original_retire = tmp_store.retire_stranded_fault_lease
        original_verify = tmp_store.verify_stranded_fault_retirement
        transaction_calls = 0
        verifier_calls = 0
        commit_outcomes: list[StrandedFaultRetirementCommit] = []

        async def false_verifier(**_kwargs: object) -> bool:
            nonlocal verifier_calls
            verifier_calls += 1
            return False

        async def indeterminate_after_real_commit(
            **kwargs: object,
        ) -> StrandedFaultRetirementCommit:
            nonlocal transaction_calls
            transaction_calls += 1
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", original_verify)
            commit_outcomes.append(await original_retire(**kwargs))  # type: ignore[arg-type]
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", false_verifier)
            return StrandedFaultRetirementCommit.INDETERMINATE

        monkeypatch.setattr(
            tmp_store, "retire_stranded_fault_lease", indeterminate_after_real_commit
        )
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", false_verifier)
        frames = service.events.subscribe()
        results = [await service.retire_stranded_fault(_retirement_request()) for _ in range(50)]

        assert [result.outcome for result in results] == ["unconfirmed"] * 50
        assert transaction_calls == 1
        assert commit_outcomes == [StrandedFaultRetirementCommit.COMMITTED_VERIFIED]
        assert verifier_calls == 50
        emitted = frames.get_nowait()
        assert frames.empty()
        data = StrandedFaultRetirementEventData.model_validate(emitted.data)
        assert data.status is FaultLeaseStatus.UNKNOWN
        assert data.retirement.outcome == "unconfirmed"
        assert service._retirement_event_ledger == (1, frozenset(("unconfirmed",)))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_read", ["latest", "gate", "provenance"])
async def test_t5_new_snapshot_read_failures_remain_typed_fail_closed_health(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, failed_read: str
) -> None:
    """T5: each D225 snapshot read fails closed without turning health into a 500."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        if failed_read == "latest":

            async def fail_latest() -> object:
                raise RuntimeError("latest read unavailable")

            monkeypatch.setattr(tmp_store, "read_latest_run", fail_latest)
        elif failed_read == "gate":

            async def fail_gate(_process_id: str) -> RestartClearanceState:
                raise RuntimeError("gate read unavailable")

            monkeypatch.setattr(tmp_store, "read_process_restart_clearance", fail_gate)
        else:
            assert (
                await service.retire_stranded_fault(_retirement_request())
            ).outcome == "confirmed"

            async def fail_provenance(**_kwargs: object) -> FaultLeaseClosureProvenance | None:
                raise RuntimeError("provenance read unavailable")

            monkeypatch.setattr(
                tmp_store, "read_fault_control_lease_closure_provenance", fail_provenance
            )

        health = await service.health()

        assert OperatorAction.RETIRE_STRANDED_FAULT not in health.restart_clearance.enabled_actions
        assert health.fault_controls.closure_provenance is None
        assert health.fault_controls.enabled_actions == [OperatorAction.EMERGENCY_STOP]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_t6_snapshot_object_replacement_clears_closed_provenance_even_under_id_collision(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6: snapshot freshness tracks identity, not collidable integer object IDs."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        assert (await service.retire_stranded_fault(_retirement_request())).outcome == "confirmed"
        original_provenance = tmp_store.read_fault_control_lease_closure_provenance
        original_id = id
        replacement_runner = object()

        def colliding_local_id(value: object) -> int:
            if value is None or value is replacement_runner:
                return 7
            return original_id(value)

        async def replace_runner_during_provenance(*args: object, **kwargs: object) -> object:
            service.runner = cast("object", replacement_runner)  # type: ignore[assignment]
            return await original_provenance(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(api_module, "id", colliding_local_id, raising=False)
        monkeypatch.setattr(
            tmp_store,
            "read_fault_control_lease_closure_provenance",
            replace_runner_during_provenance,
        )
        health = await service.health()

        assert health.fault_controls.status is FaultLeaseStatus.CLOSED
        assert health.fault_controls.closure_provenance is None
        assert health.restart_clearance.enabled_actions == []
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb1_retirement_preserves_run_and_event_history_and_broadcasts_only(
    tmp_store: RoastStore,
) -> None:
    """TB1: D225 is an exact non-history write with one typed public frame."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        presence_calls = 0

        async def counted_none() -> str:
            nonlocal presence_calls
            presence_calls += 1
            return "none"

        roaster.read_session_presence = counted_none  # type: ignore[method-assign]
        before_runs = await _table_rows(tmp_store, "roast_runs")
        before_events = await _table_rows(tmp_store, "roast_events")
        frames = service.events.subscribe()

        result = await service.retire_stranded_fault(_retirement_request())
        emitted = frames.get_nowait()
        payload = StrandedFaultRetirementEventData.model_validate(emitted.data)

        assert result.outcome == "confirmed"
        assert presence_calls == 1
        assert roaster.calls == []
        assert await _table_rows(tmp_store, "roast_runs") == before_runs
        assert await _table_rows(tmp_store, "roast_events") == before_events
        assert emitted.event.value == RoastEventKind.FAULT_CONTROLS_CHANGED.value
        assert set(emitted.data) == {
            "generation",
            "status",
            "closure_provenance",
            "physical_stop_operative",
            "retirement",
        }
        assert payload.status is FaultLeaseStatus.CLOSED
        assert (
            payload.closure_provenance is FaultLeaseClosureProvenance.OPERATOR_ATTESTED_RETIREMENT
        )
        assert payload.retirement.outcome == "confirmed"
        assert frames.empty()
        assert result.restart_clearance.state is RestartClearanceState.REQUIRED
        with pytest.raises(RoastRunConflictError):
            await service.start_roast(_profile())
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb2_d213_requires_a_separate_fresh_presence_read_after_retirement(
    tmp_store: RoastStore,
) -> None:
    """TB2: an old D225 proof never substitutes for a later D213 proof."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        calls = 0

        async def presence() -> str:
            nonlocal calls
            calls += 1
            return "none" if calls == 1 else "active"

        roaster.read_session_presence = presence  # type: ignore[method-assign]
        assert (await service.retire_stranded_fault(_retirement_request())).outcome == "confirmed"
        with pytest.raises(RoastRunConflictError):
            await service.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        assert calls == 2
        assert service._restart_clearance_state is RestartClearanceState.REQUIRED  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb3_ordered_teardown_stays_unclean_until_separate_d213_then_can_mark_clean(
    tmp_store: RoastStore,
) -> None:
    """TB3: D225 never turns an unclean epoch into an ordered clean shutdown."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        assert (await service.retire_stranded_fault(_retirement_request())).outcome == "confirmed"

        await service.mark_process_epoch_clean_after_ordered_teardown()
        async with tmp_store.connection.execute(
            "SELECT status FROM process_epoch WHERE singleton = 1"
        ) as cursor:
            before_clear = await cursor.fetchone()
        assert before_clear is not None
        assert before_clear[0] == "unclean"

        async def new_d213_presence() -> str:
            return "none"

        roaster.read_session_presence = new_d213_presence  # type: ignore[method-assign]
        acknowledgement = await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert acknowledgement.cleared is True
        assert service._restart_clearance_state is RestartClearanceState.CLEARED  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        await service.mark_process_epoch_clean_after_ordered_teardown()
        async with tmp_store.connection.execute(
            "SELECT status FROM process_epoch WHERE singleton = 1"
        ) as cursor:
            after_clear = await cursor.fetchone()
        assert after_clear is not None
        assert after_clear[0] == "clean"
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        "hardware_clear",
        "starting",
        "unpublished",
        "active_run",
        "runner",
        "loop",
        "detached_loop",
        "fault_fence",
        "persistence",
        "safety_record",
        "closure_end",
        "not_live_restart_clearance",
        "in_process_gate_cleared",
        "durable_gate_cleared",
    ],
)
async def test_every_transient_admission_term_rejects_before_presence_or_transaction(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, mutate: str
) -> None:
    """T-F1c/MB1: each independently mutable transient rejects D225 fail-closed."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        presence_calls = 0
        transaction_calls = 0

        async def counted_presence() -> str:
            nonlocal presence_calls
            presence_calls += 1
            return "none"

        original_transaction = tmp_store.retire_stranded_fault_lease

        async def counted_transaction(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal transaction_calls
            transaction_calls += 1
            return await original_transaction(**kwargs)  # type: ignore[arg-type]

        roaster.read_session_presence = counted_presence  # type: ignore[method-assign]
        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", counted_transaction)
        if mutate == "hardware_clear":
            service._hardware_clear_request_in_flight = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutate == "starting":
            service._starting_run_id = "pending"  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutate == "unpublished":
            service._unpublished_start_run_id = "pending"  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutate == "active_run":
            service.active_run_id = "active"  # noqa: SLF001
        elif mutate == "runner":
            service.runner = cast("object", object())  # type: ignore[assignment]
        elif mutate == "loop":
            service._loop_task = await _settled_task()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutate == "detached_loop":
            service._detached_recovery_loop = _DetachedRecoveryLoop(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                run_id="retired", task=await _settled_task()
            )
        elif mutate == "fault_fence":
            service._unconfirmed_fault_lease_generation = 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutate == "persistence":
            service._fault_lease_persistence_tasks.add(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                cast("asyncio.Task[None]", object())
            )
        elif mutate == "safety_record":
            service._unconfirmed_safety_record_generation = 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutate == "not_live_restart_clearance":
            service._requires_live_restart_clearance = False  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutate == "in_process_gate_cleared":
            service._restart_clearance_state = RestartClearanceState.CLEARED  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutate == "durable_gate_cleared":
            assert await tmp_store.confirm_process_restart_clearance(service.instance_id)
        else:
            service._closure_end_task = ("run", await _settled_task())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]

        if mutate == "not_live_restart_clearance":
            assert service._capture_retirement_identity() is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutate == "durable_gate_cleared":
            assert service._retirement_conditions_hold()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            assert not service._retirement_conditions_hold()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "rejected"
        assert presence_calls == 0
        assert transaction_calls == 0
        assert service._unconfirmed_stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["absent", "not_running", "stop_unconfirmed", "no_roaster", "shutdown", "replay"]
)
async def test_tf5_invalid_identity_capture_is_typed_pre_reservation_rejection(
    tmp_store: RoastStore, invalid: str
) -> None:
    """T-F5/MB29: invalid capture cannot dereference, reserve, read MCP, or write."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        presence_calls = 0

        async def prohibited_presence() -> str:
            nonlocal presence_calls
            presence_calls += 1
            raise AssertionError("invalid identity must not ask MCP for presence")

        roaster.read_session_presence = prohibited_presence  # type: ignore[method-assign]
        if invalid == "absent":
            service._mcp = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif invalid == "not_running":
            cast("_CurrentChild", service._mcp).running = False  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif invalid == "stop_unconfirmed":
            cast("_CurrentChild", service._mcp).stop_unconfirmed = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif invalid == "no_roaster":
            service._roaster = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif invalid == "shutdown":
            service._shutdown_started = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            service._recording_replay_mode = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        try:
            captured = service._capture_retirement_identity()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        except Exception as error:  # noqa: BLE001 - this is the total-function mutation oracle
            pytest.fail(f"invalid capture raised {type(error).__name__}")
        assert captured is None
        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "rejected"
        assert result.attempted_generation is None
        assert presence_calls == 0
        assert service._stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert service._unconfirmed_stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lease",
    [
        FaultControlLease(
            generation=1,
            status=FaultLeaseStatus.CLOSED,
            run_id="terminal-faulted-run",
            mcp_session_id="fault-session",
        ),
        FaultControlLease(
            generation=1, status=FaultLeaseStatus.OPEN, run_id=None, mcp_session_id="fault-session"
        ),
        FaultControlLease(
            generation=1,
            status=FaultLeaseStatus.OPEN,
            run_id="terminal-faulted-run",
            mcp_session_id=None,
        ),
        FaultControlLease(
            generation=0,
            status=FaultLeaseStatus.OPEN,
            run_id="terminal-faulted-run",
            mcp_session_id="fault-session",
        ),
    ],
)
async def test_each_normal_open_lease_field_is_a_closed_admission_predicate(
    tmp_store: RoastStore, lease: FaultControlLease
) -> None:
    """MB1: a malformed/non-open normal lease cannot pass the separate OPEN predicate."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        identity = service._capture_retirement_identity()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert identity is not None
        assert not service._retirement_open_admission(lease, identity)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb10_indeterminate_transaction_keeps_unknown_fence_and_blocks_restart_paths(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB10/MB7/MB13: uncertainty is retained rather than treated as closure."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)

        async def indeterminate(**_kwargs: object) -> StrandedFaultRetirementCommit:
            return StrandedFaultRetirementCommit.INDETERMINATE

        async def never_verified(**_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate)
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", never_verified)
        result = await service.retire_stranded_fault(_retirement_request())
        health = await service.health()

        assert result.outcome == "unconfirmed"
        assert service._unconfirmed_stranded_fault_retirement is not None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert health.fault_controls.status is FaultLeaseStatus.UNKNOWN
        assert health.restart_clearance.state is RestartClearanceState.REQUIRED
        with pytest.raises(RoastRunConflictError):
            await service.acknowledge_restart_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        with pytest.raises(RoastRunConflictError):
            await service.acknowledge_closure_only_clearance(
                RestartClearanceRequest(physical_confirmation=True)
            )
        d220 = await service.initialize_missing_fault_control_safety_record(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert d220.outcome == "rejected"
        with pytest.raises(RoastRunConflictError):
            await service.start_roast(_profile())
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb_evt_repeated_presence_rejections_are_honest_but_emit_once(
    tmp_store: RoastStore,
) -> None:
    """TB-EVT/MB22/MB26: response truth is not hidden by bounded notification dedupe."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        presence_calls = 0

        async def active_presence() -> str:
            nonlocal presence_calls
            presence_calls += 1
            return "active"

        roaster.read_session_presence = active_presence  # type: ignore[method-assign]
        frames = service.events.subscribe()
        results = [await service.retire_stranded_fault(_retirement_request()) for _ in range(50)]
        emitted = frames.get_nowait()

        assert [result.outcome for result in results] == ["rejected"] * 50
        assert presence_calls == 50
        assert frames.empty()
        data = StrandedFaultRetirementEventData.model_validate(emitted.data)
        assert data.retirement.outcome == "rejected"
        assert "outcome" not in emitted.data
        assert emitted.event.value == RoastEventKind.FAULT_CONTROLS_CHANGED.value
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb_evt_pre_reservation_rejections_emit_no_frame(
    tmp_store: RoastStore,
) -> None:
    """TB-EVT: invalid capture rejections have neither a reservation nor a broadcast."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        service._mcp = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        frames = service.events.subscribe()

        results = [await service.retire_stranded_fault(_retirement_request()) for _ in range(50)]

        assert [result.outcome for result in results] == ["rejected"] * 50
        assert frames.empty()
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb_evt_is_broadcast_only_and_never_exposes_private_retirement_identity(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TB-EVT/MB19-21: D225 has one public changed frame and no history write/leak."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        record_event_calls = 0
        operation_ids: list[str] = []
        original_retire = tmp_store.retire_stranded_fault_lease

        async def forbidden_record_event(*_args: object, **_kwargs: object) -> None:
            nonlocal record_event_calls
            record_event_calls += 1

        monkeypatch.setattr(tmp_store, "record_event", forbidden_record_event)

        async def capture_retirement_operation(
            **kwargs: object,
        ) -> StrandedFaultRetirementCommit:
            operation_id = kwargs.get("operation_id")
            if isinstance(operation_id, str):
                operation_ids.append(operation_id)
            return await original_retire(**cast("Any", kwargs))

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", capture_retirement_operation)
        frames = service.events.subscribe()
        result = await service.retire_stranded_fault(_retirement_request())
        emitted = frames.get_nowait()
        rendered = "|".join(
            (
                f"{result.model_dump(mode='json')}|{emitted.data}",
                *(record.getMessage() for record in caplog.records),
            )
        )

        assert result.outcome == "confirmed"
        assert record_event_calls == 0
        assert len(operation_ids) == 1
        assert frames.empty()
        assert emitted.event.value == RoastEventKind.FAULT_CONTROLS_CHANGED.value
        assert "outcome" not in emitted.data
        for private_value in (
            "terminal-faulted-run",
            "fault-session",
            service.instance_id,
            operation_ids[0],
            str(tmp_store._db_path),  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        ):
            assert private_value not in rendered
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    ["epoch", "lease", "active", "latest", "gate", "presence", "transaction"],
)
async def test_tb_child_race_after_each_normal_admission_await_never_hydrates_stale_success(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """TB-CHILD/MB4: every normal admission await revalidates the captured child."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        transaction_calls = 0
        original_epoch = service._ensure_restart_epoch  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        original_lease = service._read_fault_lease_fail_closed  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        original_active = tmp_store.active_run
        original_latest = tmp_store.read_latest_run
        original_gate = tmp_store.read_process_restart_clearance
        original_presence = roaster.read_session_presence
        original_transaction = tmp_store.retire_stranded_fault_lease

        def stale_child() -> None:
            cast("_CurrentChild", service._mcp).child_epoch += 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        async def epoch_then_stale() -> None:
            await original_epoch()
            stale_child()

        async def lease_then_stale() -> object:
            value = await original_lease()
            stale_child()
            return value

        async def active_then_stale() -> object:
            value = await original_active()
            stale_child()
            return value

        async def latest_then_stale() -> object:
            value = await original_latest()
            stale_child()
            return value

        async def gate_then_stale(process_id: str) -> RestartClearanceState:
            value = await original_gate(process_id)
            stale_child()
            return value

        async def presence_then_stale() -> str:
            value = await original_presence()
            stale_child()
            return value

        async def transaction_then_stale(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal transaction_calls
            transaction_calls += 1
            value = await original_transaction(**kwargs)  # type: ignore[arg-type]
            stale_child()
            return value

        if boundary == "epoch":
            monkeypatch.setattr(service, "_ensure_restart_epoch", epoch_then_stale)
        elif boundary == "lease":
            monkeypatch.setattr(service, "_read_fault_lease_fail_closed", lease_then_stale)
        elif boundary == "active":
            monkeypatch.setattr(tmp_store, "active_run", active_then_stale)
        elif boundary == "latest":
            monkeypatch.setattr(tmp_store, "read_latest_run", latest_then_stale)
        elif boundary == "gate":
            monkeypatch.setattr(tmp_store, "read_process_restart_clearance", gate_then_stale)
        elif boundary == "presence":
            roaster.read_session_presence = presence_then_stale  # type: ignore[method-assign]
        else:
            monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", transaction_then_stale)

        result = await service.retire_stranded_fault(_retirement_request())

        if boundary == "transaction":
            assert result.outcome == "unconfirmed"
            assert transaction_calls == 1
            assert service._unconfirmed_stranded_fault_retirement is not None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            assert result.outcome == "rejected"
            assert transaction_calls == 0
            assert service._unconfirmed_stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["child_epoch", "stop_unconfirmed", "shutdown", "hardware_clear", "starting", "runner", "loop"],
)
async def test_tf3_projection_rechecks_each_local_binding_during_store_reads(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """T-F3/MB27: unchanged lease state cannot make a stale health snapshot eligible."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        original = tmp_store.read_latest_run

        async def mutate_during_read() -> object:
            if change == "child_epoch":
                cast("_CurrentChild", service._mcp).child_epoch += 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "stop_unconfirmed":
                cast("_CurrentChild", service._mcp).stop_unconfirmed = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "shutdown":
                service._shutdown_started = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "hardware_clear":
                service._hardware_clear_request_in_flight = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "starting":
                service._starting_run_id = "pending"  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "runner":
                service.runner = cast("object", object())  # type: ignore[assignment]
            else:
                service._loop_task = await _settled_task()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            return await original()

        monkeypatch.setattr(tmp_store, "read_latest_run", mutate_during_read)
        health = await service.health()

        assert OperatorAction.RETIRE_STRANDED_FAULT not in health.restart_clearance.enabled_actions
        assert health.fault_controls.closure_provenance is None
        assert health.fault_controls.status is FaultLeaseStatus.OPEN
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb12_cancelling_owned_task_retains_its_uncertainty_fence_without_a_frame(
    tmp_store: RoastStore,
) -> None:
    """TB12 C1: task cancellation after reservation preserves fail-closed uncertainty."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def parked_presence() -> str:
            entered.set()
            await release.wait()
            return "none"

        roaster.read_session_presence = parked_presence  # type: ignore[method-assign]
        frames = service.events.subscribe()
        waiter = asyncio.create_task(service.retire_stranded_fault(_retirement_request()))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        owned = service._stranded_fault_retirement  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert owned is not None
        owned[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert service._unconfirmed_stranded_fault_retirement is owned[0]  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert frames.empty()
        release.set()
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb12_c0_cancellation_before_lock_admission_installs_no_reservation_or_fence(
    tmp_store: RoastStore,
) -> None:
    """TB12 C0: cancellation before the lock cannot leave D225 local authority behind."""
    await tmp_store.initialize()
    service: RoastService | None = None
    try:
        service, _roaster = await _eligible_service(tmp_store)
        await service._restart_clearance_lock.acquire()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        waiter = asyncio.create_task(service.retire_stranded_fault(_retirement_request()))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert service._stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert service._unconfirmed_stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        service._restart_clearance_lock.release()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        if service is not None and service._restart_clearance_lock.locked():  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            service._restart_clearance_lock.release()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["transaction", "verifier"])
async def test_tb12_c2_c3_cancellation_after_reservation_retains_fence_and_emits_nothing(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """TB12 C2/C3: cancellation after proof reservation cannot clear uncertainty."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def parked_transaction(**_kwargs: object) -> StrandedFaultRetirementCommit:
            entered.set()
            await release.wait()
            return StrandedFaultRetirementCommit.INDETERMINATE

        async def indeterminate_transaction(**_kwargs: object) -> StrandedFaultRetirementCommit:
            return StrandedFaultRetirementCommit.INDETERMINATE

        async def parked_verifier(**_kwargs: object) -> bool:
            entered.set()
            await release.wait()
            return False

        if stage == "transaction":
            monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", parked_transaction)
        else:
            monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate_transaction)
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", parked_verifier)
        frames = service.events.subscribe()
        waiter = asyncio.create_task(service.retire_stranded_fault(_retirement_request()))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        owned = service._stranded_fault_retirement  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert owned is not None
        owned[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert service._unconfirmed_stranded_fault_retirement is owned[0]  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert frames.empty()
        release.set()
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    ["epoch", "lease", "active", "latest", "gate", "presence", "transaction", "verify"],
)
@pytest.mark.parametrize(
    "change",
    [
        "process",
        "child_object",
        "child_epoch",
        "child_not_running",
        "stop_unconfirmed",
        "hardware_clear",
        "cache",
        "shutdown",
        "estop",
    ],
)
async def test_tb_binding_races_fail_closed_at_each_non_child_boundary(
    tmp_store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    change: str,
) -> None:
    """TB-ID/STOP/HWCLR/CACHE/SHUT: each independently awaited proof is fenced."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        transactions = 0
        verifies = 0
        original_epoch = service._ensure_restart_epoch  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        original_lease = service._read_fault_lease_fail_closed  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        original_active = tmp_store.active_run
        original_latest = tmp_store.read_latest_run
        original_gate = tmp_store.read_process_restart_clearance
        original_presence = roaster.read_session_presence
        original_transaction = tmp_store.retire_stranded_fault_lease
        after_transaction = boundary in ("transaction", "verify")

        def make_stale() -> None:
            if change == "process":
                service.instance_id = "b" * 32
            elif change == "child_object":
                service._mcp = cast("MCPServerProcess", _CurrentChild())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "child_epoch":
                cast("_CurrentChild", service._mcp).child_epoch += 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "child_not_running":
                cast("_CurrentChild", service._mcp).running = False  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "cache":
                service._fault_lease_cache = FaultControlLease(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                    generation=1,
                    status=FaultLeaseStatus.UNKNOWN,
                    run_id="terminal-faulted-run",
                    mcp_session_id="fault-session",
                )
            elif change == "hardware_clear":
                service._hardware_clear_request_in_flight = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "shutdown":
                service._shutdown_started = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "estop":
                service._note_emergency_stop_admission()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            else:
                cast("_CurrentChild", service._mcp).stop_unconfirmed = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        async def epoch() -> None:
            await original_epoch()
            make_stale()

        async def lease() -> object:
            value = await original_lease()
            make_stale()
            return value

        async def active() -> object:
            value = await original_active()
            make_stale()
            return value

        async def latest() -> object:
            value = await original_latest()
            make_stale()
            return value

        async def gate(process_id: str) -> RestartClearanceState:
            value = await original_gate(process_id)
            make_stale()
            return value

        async def presence() -> str:
            value = await original_presence()
            make_stale()
            return value

        async def transaction(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal transactions
            transactions += 1
            value = await original_transaction(**kwargs)  # type: ignore[arg-type]
            make_stale()
            return value

        async def indeterminate(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal transactions
            transactions += 1
            value = await original_transaction(**kwargs)  # type: ignore[arg-type]
            assert value is StrandedFaultRetirementCommit.COMMITTED_VERIFIED
            return StrandedFaultRetirementCommit.INDETERMINATE

        async def verifier(**_kwargs: object) -> bool:
            nonlocal verifies
            verifies += 1
            make_stale()
            return True

        if boundary == "epoch":
            monkeypatch.setattr(service, "_ensure_restart_epoch", epoch)
        elif boundary == "lease":
            monkeypatch.setattr(service, "_read_fault_lease_fail_closed", lease)
        elif boundary == "active":
            monkeypatch.setattr(tmp_store, "active_run", active)
        elif boundary == "latest":
            monkeypatch.setattr(tmp_store, "read_latest_run", latest)
        elif boundary == "gate":
            monkeypatch.setattr(tmp_store, "read_process_restart_clearance", gate)
        elif boundary == "presence":
            roaster.read_session_presence = presence  # type: ignore[method-assign]
        elif boundary == "transaction":
            monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", transaction)
        else:
            monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate)
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", verifier)

        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == ("unconfirmed" if after_transaction else "rejected")
        assert transactions == (1 if after_transaction else 0)
        assert verifies == (1 if boundary == "verify" else 0)
        if after_transaction:
            assert service._unconfirmed_stranded_fault_retirement is not None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            assert service._unconfirmed_stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "process",
        "child_object",
        "child_epoch",
        "child_not_running",
        "stop_unconfirmed",
        "hardware_clear",
        "cache",
        "shutdown",
        "estop",
    ],
)
async def test_tb_binding_races_fail_closed_while_waiting_for_restart_lock(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """TB-ID/CHILD/STOP/HWCLR/CACHE/SHUT/TB5: lock wait preserves no admission."""
    await tmp_store.initialize()
    service: RoastService | None = None
    try:
        service, roaster = await _eligible_service(tmp_store)
        transactions = 0
        original_transaction = tmp_store.retire_stranded_fault_lease

        async def counted_transaction(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal transactions
            transactions += 1
            return await original_transaction(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", counted_transaction)
        await service._restart_clearance_lock.acquire()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        request = asyncio.create_task(service.retire_stranded_fault(_retirement_request()))
        await asyncio.sleep(0)
        if change == "process":
            service.instance_id = "d" * 32
        elif change == "child_object":
            service._mcp = cast("MCPServerProcess", _CurrentChild())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif change == "child_epoch":
            cast("_CurrentChild", service._mcp).child_epoch += 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif change == "child_not_running":
            cast("_CurrentChild", service._mcp).running = False  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif change == "stop_unconfirmed":
            cast("_CurrentChild", service._mcp).stop_unconfirmed = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif change == "hardware_clear":
            service._hardware_clear_request_in_flight = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif change == "cache":
            service._fault_lease_cache = FaultControlLease(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                generation=1,
                status=FaultLeaseStatus.UNKNOWN,
                run_id="terminal-faulted-run",
                mcp_session_id="fault-session",
            )
        elif change == "shutdown":
            service._shutdown_started = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            service._note_emergency_stop_admission()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        service._restart_clearance_lock.release()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        result = await request

        assert result.outcome == "rejected"
        assert transactions == 0
        assert roaster.calls == []
        assert service._unconfirmed_stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        if service is not None and service._restart_clearance_lock.locked():  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            service._restart_clearance_lock.release()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb_evt_distinct_outcomes_emit_once_and_reconciliation_does_not_write_again(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB-EVT: rejected, uncertain and reconciled outcomes are independently visible."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        frames = service.events.subscribe()
        original_retire = tmp_store.retire_stranded_fault_lease
        original_verify = tmp_store.verify_stranded_fault_retirement
        transactions = 0

        async def active() -> str:
            return "active"

        roaster.read_session_presence = active  # type: ignore[method-assign]
        assert (await service.retire_stranded_fault(_retirement_request())).outcome == "rejected"

        async def none() -> str:
            return "none"

        async def false_verify(**_kwargs: object) -> bool:
            return False

        async def indeterminate_after_commit(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal transactions
            transactions += 1
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", original_verify)
            committed = await original_retire(**kwargs)  # type: ignore[arg-type]
            assert committed is StrandedFaultRetirementCommit.COMMITTED_VERIFIED
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", false_verify)
            return StrandedFaultRetirementCommit.INDETERMINATE

        roaster.read_session_presence = none  # type: ignore[method-assign]
        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate_after_commit)
        assert (await service.retire_stranded_fault(_retirement_request())).outcome == "unconfirmed"
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", original_verify)
        confirmed = await service.retire_stranded_fault(_retirement_request())

        payloads = [StrandedFaultRetirementEventData.model_validate(frames.get_nowait().data)]
        payloads.extend(
            StrandedFaultRetirementEventData.model_validate(frames.get_nowait().data)
            for _ in range(2)
        )
        assert frames.empty()
        assert [payload.retirement.outcome for payload in payloads] == [
            "rejected",
            "unconfirmed",
            "confirmed",
        ]
        assert confirmed.outcome == "confirmed"
        assert transactions == 1
        assert service._retirement_event_ledger == (  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            1,
            frozenset(("rejected", "unconfirmed", "confirmed")),
        )
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tf7_stale_cache_before_emit_suppresses_frame_without_consuming_ledger(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MB23: a stale event is suppressed, rather than rewritten as newer UNKNOWN."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        frames = service.events.subscribe()
        original_emit = service._emit_stranded_fault_retirement  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        async def stale_before_emit(*args: object, **kwargs: object) -> None:
            service._fault_lease_cache = FaultControlLease(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                generation=1,
                status=FaultLeaseStatus.UNKNOWN,
                run_id="terminal-faulted-run",
                mcp_session_id="fault-session",
            )
            await original_emit(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(service, "_emit_stranded_fault_retirement", stale_before_emit)
        result = await service.retire_stranded_fault(_retirement_request())
        health = await service.health()

        assert result.outcome == "confirmed"
        assert frames.empty()
        assert service._retirement_event_ledger is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert health.fault_controls.status is FaultLeaseStatus.CLOSED
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tf6_old_rejection_and_done_callback_cannot_clear_a_newer_fence(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MB28: context ownership, not completion order, controls fence retirement."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)

        async def indeterminate(**_kwargs: object) -> StrandedFaultRetirementCommit:
            return StrandedFaultRetirementCommit.INDETERMINATE

        async def false_verify(**_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate)
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", false_verify)
        assert (await service.retire_stranded_fault(_retirement_request())).outcome == "unconfirmed"
        old_fence = service._unconfirmed_stranded_fault_retirement  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert old_fence is not None
        newer_fence = replace(old_fence, operation_id="c" * 32)
        service._unconfirmed_stranded_fault_retirement = newer_fence  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        old_task = asyncio.create_task(service._reject_stranded_fault_retirement(old_fence))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await old_task
        service._finish_stranded_fault_retirement(old_fence, old_task)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert service._unconfirmed_stranded_fault_retirement is newer_fence  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_name", ["lease", "active", "latest", "gate"])
async def test_tf3_snapshot_rechecks_each_durable_read_before_advertising_retirement(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, read_name: str
) -> None:
    """T-F3: no individual store await may publish a stale retirement action."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)

        def stale_child() -> None:
            cast("_CurrentChild", service._mcp).child_epoch += 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        if read_name == "lease":
            original = service._read_fault_lease_fail_closed  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

            async def read_lease() -> FaultControlLease:
                value = await original()
                stale_child()
                return value

            monkeypatch.setattr(service, "_read_fault_lease_fail_closed", read_lease)
        elif read_name == "active":
            original = tmp_store.active_run

            async def read_active() -> object:
                value = await original()
                stale_child()
                return value

            monkeypatch.setattr(tmp_store, "active_run", read_active)
        elif read_name == "latest":
            original = tmp_store.read_latest_run

            async def read_latest() -> object:
                value = await original()
                stale_child()
                return value

            monkeypatch.setattr(tmp_store, "read_latest_run", read_latest)
        else:
            original = tmp_store.read_process_restart_clearance

            async def read_gate(process_id: str) -> RestartClearanceState:
                value = await original(process_id)
                stale_child()
                return value

            monkeypatch.setattr(tmp_store, "read_process_restart_clearance", read_gate)

        snapshot = await service._stranded_fault_retirement_snapshot()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert snapshot.available is False
        assert snapshot.provenance is None
        assert snapshot.physical_stop_operative is True
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tf3_provenance_read_rechecks_current_child_before_exposing_closed_provenance(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB-PROJ: a closed provenance is never retained across a store-read race."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        assert (await service.retire_stranded_fault(_retirement_request())).outcome == "confirmed"
        original = tmp_store.read_fault_control_lease_closure_provenance

        async def read(lease: FaultControlLease) -> FaultLeaseClosureProvenance | None:
            value = await original(lease)
            cast("_CurrentChild", service._mcp).child_epoch += 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            return value

        monkeypatch.setattr(tmp_store, "read_fault_control_lease_closure_provenance", read)
        snapshot = await service._stranded_fault_retirement_snapshot()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert snapshot.fresh is False
        assert snapshot.provenance is None
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_name", ["lease", "active", "latest", "gate", "provenance"])
@pytest.mark.parametrize(
    "change",
    [
        "process",
        "child_object",
        "child_epoch",
        "child_not_running",
        "stop_unconfirmed",
        "hardware_clear",
        "start",
        "runner",
        "loop",
        "shutdown",
        "estop",
    ],
)
async def test_tb_proj_every_local_change_invalidates_every_store_projection_read(
    tmp_store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    read_name: str,
    change: str,
) -> None:
    """TB-PROJ/T-F3: every relevant store await rechecks each local binding."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        if read_name == "provenance":
            assert (
                await service.retire_stranded_fault(_retirement_request())
            ).outcome == "confirmed"

        def make_stale() -> None:
            if change == "process":
                service.instance_id = "e" * 32
            elif change == "child_object":
                service._mcp = cast("MCPServerProcess", _CurrentChild())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "child_epoch":
                cast("_CurrentChild", service._mcp).child_epoch += 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "child_not_running":
                cast("_CurrentChild", service._mcp).running = False  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "stop_unconfirmed":
                cast("_CurrentChild", service._mcp).stop_unconfirmed = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "hardware_clear":
                service._hardware_clear_request_in_flight = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "start":
                service._starting_run_id = "new-start"  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "runner":
                service.runner = cast("Any", object())
            elif change == "loop":
                service._loop_task = await_task  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            elif change == "shutdown":
                service._shutdown_started = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            else:
                service._note_emergency_stop_admission()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        await_task = await _settled_task()
        if read_name == "lease":
            original = service._read_fault_lease_fail_closed  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

            async def read_lease() -> FaultControlLease:
                value = await original()
                make_stale()
                return value

            monkeypatch.setattr(service, "_read_fault_lease_fail_closed", read_lease)
        elif read_name == "active":
            original = tmp_store.active_run

            async def read_active() -> object:
                value = await original()
                make_stale()
                return value

            monkeypatch.setattr(tmp_store, "active_run", read_active)
        elif read_name == "latest":
            original = tmp_store.read_latest_run

            async def read_latest() -> object:
                value = await original()
                make_stale()
                return value

            monkeypatch.setattr(tmp_store, "read_latest_run", read_latest)
        elif read_name == "gate":
            original = tmp_store.read_process_restart_clearance

            async def read_gate(process_id: str) -> RestartClearanceState:
                value = await original(process_id)
                make_stale()
                return value

            monkeypatch.setattr(tmp_store, "read_process_restart_clearance", read_gate)
        else:
            original = tmp_store.read_fault_control_lease_closure_provenance

            async def read_provenance(
                lease: FaultControlLease,
            ) -> FaultLeaseClosureProvenance | None:
                value = await original(lease)
                make_stale()
                return value

            monkeypatch.setattr(
                tmp_store, "read_fault_control_lease_closure_provenance", read_provenance
            )

        snapshot = await service._stranded_fault_retirement_snapshot()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert snapshot.fresh is False
        assert snapshot.available is False
        assert snapshot.provenance is None
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["shutdown", "replay"])
async def test_d212_projection_never_survives_shutdown_or_replay_during_its_current_read(
    tmp_store: RoastStore, change: str
) -> None:
    """S8: a legacy safe-zero D212 action is withdrawn when its binding goes stale."""
    await tmp_store.initialize()
    try:
        service, old_roaster = await _eligible_service(tmp_store)
        old_roaster.set_latest_fault_session_id("fault-session")
        cleared = await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert cleared.cleared is True
        port = _SafeZeroFaultPort()
        service._roaster = port  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        service._fault_control_executor = cast("Any", object())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        established = await service.health()
        assert (
            OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE
            in established.fault_controls.enabled_actions
        )

        if change == "shutdown":
            port.on_latest = lambda: setattr(service, "_shutdown_started", True)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            port.on_latest = lambda: setattr(service, "_recording_replay_mode", True)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        stale = await service.health()

        assert (
            OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE not in stale.fault_controls.enabled_actions
        )
        assert stale.restart_clearance.eligible is False
        assert stale.fault_controls.status is FaultLeaseStatus.OPEN
        assert port.calls == []
        service._fault_control_executor = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["absent_child", "malformed_presence", "raised_presence"])
async def test_tf5_invalid_capture_returns_typed_route_result_not_500(
    tmp_store: RoastStore, invalid: str
) -> None:
    """T-F5: invalid child/presence proof stays a typed HTTP rejection."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        if invalid == "absent_child":
            service._mcp = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif invalid == "malformed_presence":

            async def malformed() -> str:
                return "not-a-session-presence"

            roaster.read_session_presence = malformed  # type: ignore[method-assign]
        else:

            async def raised() -> str:
                raise OSError("synthetic current-proof failure")

            roaster.read_session_presence = raised  # type: ignore[method-assign]
        app = create_app(service)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/restart-clearance/retire-stranded-fault",
                json={
                    "stranded_fault_confirmation": (
                        "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
                    )
                },
            )

        assert response.status_code == 200
        assert response.json()["outcome"] == "rejected"
        assert (await tmp_store.read_fault_control_lease()).status is FaultLeaseStatus.OPEN
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb13_malformed_older_audit_cannot_reconcile_or_retry_the_transaction(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB13: malformed retained audit evidence remains unconfirmed without a retry."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        original_retire = tmp_store.retire_stranded_fault_lease
        transactions = 0

        async def false_verify(**_kwargs: object) -> bool:
            return False

        async def indeterminate_after_commit(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal transactions
            transactions += 1
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", original_verify)
            committed = await original_retire(**kwargs)  # type: ignore[arg-type]
            assert committed is StrandedFaultRetirementCommit.COMMITTED_VERIFIED
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", false_verify)
            return StrandedFaultRetirementCommit.INDETERMINATE

        original_verify = tmp_store.verify_stranded_fault_retirement
        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate_after_commit)
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", false_verify)
        assert (await service.retire_stranded_fault(_retirement_request())).outcome == "unconfirmed"
        async with tmp_store.connection.execute(
            "UPDATE operator_actions SET payload_json = ? WHERE action = ?",
            ("{malformed", OperatorAction.RETIRE_STRANDED_FAULT.value),
        ):
            pass
        await tmp_store.connection.commit()

        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "unconfirmed"
        assert transactions == 1
        assert service._unconfirmed_stranded_fault_retirement is not None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tb4_real_store_provenance_distinguishes_d212_d220_retirement_and_unproven_states(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB4: provenance is exact durable evidence, never inferred from lease status."""
    await tmp_store.initialize()
    baseline_path = tmp_store._db_path.with_name("tb4-baseline.sqlite3")  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    baseline_store = RoastStore(baseline_path)
    retirement_store = RoastStore(tmp_store._db_path.with_name("tb4-retirement.sqlite3"))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    await baseline_store.initialize()
    await retirement_store.initialize()
    # D220's absent record is intentionally trusted only after reopening an
    # existing database: a fresh install is a distinct safe-zero provenance.
    await baseline_store.close()
    baseline_store = RoastStore(baseline_path)
    await baseline_store.initialize()
    try:
        await tmp_store.create_run(
            run_id="tb4-d212-run",
            profile=_profile(),
            config=AppConfig(),
            agent_phase=RoastPhase.FAULTED,
        )
        await tmp_store.complete_run(
            run_id="tb4-d212-run", outcome="faulted", agent_phase=RoastPhase.FAULTED
        )
        await tmp_store.open_fault_control_lease(
            run_id="tb4-d212-run", mcp_session_id="tb4-d212-session", generation=1
        )
        closed = await tmp_store.close_fault_control_lease_atomically(
            generation=1,
            run_id="tb4-d212-run",
            mcp_session_id="tb4-d212-session",
            fault_reason=None,
        )
        d212_lease = await tmp_store.read_fault_control_lease()
        assert closed.confirmed is True
        assert (
            await tmp_store.read_fault_control_lease_closure_provenance(d212_lease)
            is FaultLeaseClosureProvenance.EXACT_SESSION_SAFE_ZERO
        )

        process_id = "f" * 32
        assert not await baseline_store.begin_process_epoch(process_id)
        assert await baseline_store.begin_process_restart_clearance(process_id)
        assert await baseline_store.initialize_missing_fault_control_safety_record(
            process_id=process_id, generation=1, operation_id="a" * 32
        )
        baseline_lease = await baseline_store.read_fault_control_lease()
        assert (
            await baseline_store.read_fault_control_lease_closure_provenance(baseline_lease)
            is FaultLeaseClosureProvenance.BASELINE
        )

        retirement_service, _roaster = await _eligible_service(retirement_store)
        assert (
            await retirement_service.retire_stranded_fault(_retirement_request())
        ).outcome == "confirmed"
        retirement_lease = await retirement_store.read_fault_control_lease()
        assert (
            await retirement_store.read_fault_control_lease_closure_provenance(retirement_lease)
            is FaultLeaseClosureProvenance.OPERATOR_ATTESTED_RETIREMENT
        )
        await retirement_service.shutdown()

        await tmp_store.open_fault_control_lease(
            run_id="tb4-d212-run", mcp_session_id="tb4-open-session", generation=2
        )
        open_lease = await tmp_store.read_fault_control_lease()
        assert await tmp_store.read_fault_control_lease_closure_provenance(open_lease) is None
        assert await tmp_store.mark_fault_control_lease_unknown(2)
        unknown_lease = await tmp_store.read_fault_control_lease()
        assert await tmp_store.read_fault_control_lease_closure_provenance(unknown_lease) is None

        async def unreadable(*_args: object, **_kwargs: object) -> FaultControlLease:
            raise OSError("synthetic provenance read failure")

        monkeypatch.setattr(tmp_store, "_read_fault_control_lease_from_connection", unreadable)
        assert await tmp_store.read_fault_control_lease_closure_provenance(unknown_lease) is None
    finally:
        await tmp_store.close()
        await baseline_store.close()
        await retirement_store.close()


def _test_retirement_context(service: RoastService, lease: FaultControlLease) -> Any:
    """Build one private context from a real current D225 identity for a narrow test seam."""
    identity = service._capture_retirement_identity()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    assert identity is not None
    context_type = api_module._StrandedFaultRetirementContext  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    return context_type(identity=identity, expected_lease=lease, operation_id="d" * 32)


@pytest.mark.asyncio
async def test_tb_evt_higher_generation_resets_ledger_but_an_older_attempt_cannot_consume_it(
    tmp_store: RoastStore,
) -> None:
    """TB-EVT/MB26: event deduplication is a one-generation, monotonic ledger."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        frames = service.events.subscribe()
        lease_one = service._fault_lease_cache  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        context_one = _test_retirement_context(service, lease_one)
        result_one = await service._stranded_fault_retirement_result("rejected", 1)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service._emit_stranded_fault_retirement(result_one, 1, context_one)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        await tmp_store.open_fault_control_lease(
            run_id="terminal-faulted-run", mcp_session_id="fault-session-two", generation=2
        )
        await service.health()
        lease_two = service._fault_lease_cache  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert lease_two.generation == 2
        context_two = _test_retirement_context(service, lease_two)
        result_two = await service._stranded_fault_retirement_result("unconfirmed", 2)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service._emit_stranded_fault_retirement(result_two, 2, context_two)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert service._retirement_event_ledger == (2, frozenset(("unconfirmed",)))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        await service._emit_stranded_fault_retirement(result_one, 1, context_one)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert service._retirement_event_ledger == (2, frozenset(("unconfirmed",)))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        rejected_two = await service._stranded_fault_retirement_result("rejected", 2)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service._emit_stranded_fault_retirement(rejected_two, 2, context_two)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        payloads = [
            StrandedFaultRetirementEventData.model_validate(frames.get_nowait().data)
            for _ in range(3)
        ]
        assert frames.empty()
        assert [payload.retirement.attempted_generation for payload in payloads] == [1, 2, 2]
        assert [payload.retirement.outcome for payload in payloads] == [
            "rejected",
            "unconfirmed",
            "rejected",
        ]
        assert service._retirement_event_ledger == (  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            2,
            frozenset(("unconfirmed", "rejected")),
        )
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_tf6_stale_done_callback_preserves_the_newer_reservation_tuple_and_fence(
    tmp_store: RoastStore,
) -> None:
    """T-F6/MB28: an old callback cannot discard a newer context-task reservation."""
    await tmp_store.initialize()
    old_task: Any = None
    newer_task: Any = None
    try:
        service, _roaster = await _eligible_service(tmp_store)
        lease = service._fault_lease_cache  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        old_context = _test_retirement_context(service, lease)
        newer_context = _test_retirement_context(service, lease)
        release = asyncio.Event()

        async def wait_for_release() -> None:
            await release.wait()

        old_task = asyncio.create_task(wait_for_release())
        newer_task = asyncio.create_task(wait_for_release())
        service._unconfirmed_stranded_fault_retirement = newer_context  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        service._stranded_fault_retirement = (newer_context, newer_task)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        service._finish_stranded_fault_retirement(old_context, old_task)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert service._unconfirmed_stranded_fault_retirement is newer_context  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert service._stranded_fault_retirement == (newer_context, newer_task)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        release.set()
        await asyncio.gather(old_task, newer_task)
        service._stranded_fault_retirement = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        for task in (old_task, newer_task):
            if task is not None and not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        await tmp_store.close()


@pytest.mark.asyncio
async def test_completed_owned_reservation_is_cleaned_before_a_new_d225_admission(
    tmp_store: RoastStore,
) -> None:
    """The completed-reservation re-entry branch removes only its own tuple before retrying."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        lease = service._fault_lease_cache  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        completed = await _settled_task()
        service._stranded_fault_retirement = cast(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            "Any", (_test_retirement_context(service, lease), completed)
        )

        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "confirmed"
        assert service._stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert (await tmp_store.read_fault_control_lease()).status is FaultLeaseStatus.CLOSED
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("guard_id", "mutation"),
    [
        ("MB1:_retirement_conditions_hold:0", "epoch"),
        ("MB1:_retirement_conditions_hold:3", "pending_dispatch"),
        ("MB1:_retirement_conditions_hold:4", "arbiter_estop"),
        ("MB1:_retirement_conditions_hold:5", "fault_finalization"),
        ("MB1:_retirement_conditions_hold:6", "d212_close"),
        ("MB1:_retirement_conditions_hold:9", "d220_context"),
        ("MB1:_retirement_conditions_hold:12", "d216_context"),
    ],
)
async def test_each_residual_transient_guard_rejects_before_presence_or_transaction(
    tmp_store: RoastStore, guard_id: str, mutation: str
) -> None:
    """MB1 mapping: each previously-uncovered transient has a direct closed predicate test."""
    await tmp_store.initialize()
    pending: Any = None
    try:
        service, roaster = await _eligible_service(tmp_store)
        presence_calls = 0
        transactions = 0

        async def counted_presence() -> str:
            nonlocal presence_calls
            presence_calls += 1
            return "none"

        async def counted_transaction(**_kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal transactions
            transactions += 1
            return StrandedFaultRetirementCommit.PRECONDITION_REJECTED

        roaster.read_session_presence = counted_presence  # type: ignore[method-assign]
        service._store.retire_stranded_fault_lease = counted_transaction  # type: ignore[method-assign]
        assert service._retirement_conditions_hold()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        if mutation == "epoch":
            service._restart_epoch_initialized = False  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutation == "pending_dispatch":
            pending = asyncio.create_task(asyncio.Event().wait())
            service._emergency_stop_dispatch_tasks.add(pending)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutation == "arbiter_estop":
            service._command_arbiter.admit_emergency_stop()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutation == "fault_finalization":
            service._fault_acknowledgement_gate.fault_finalization_started = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutation == "d212_close":
            assert service._command_arbiter.reserve_close(service._command_arbiter.generation)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutation == "d220_context":
            service._safety_record_initialization = cast("Any", (object(), await _settled_task()))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            service._closure_only_context = cast("Any", object())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert guard_id.startswith("MB1:")
        try:
            conditions_hold = service._retirement_conditions_hold()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        except Exception as error:  # noqa: BLE001 - this is the total-function mutation oracle
            pytest.fail(f"transient predicate raised {type(error).__name__}")
        assert conditions_hold is False
        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "rejected"
        assert result.attempted_generation is None
        assert presence_calls == 0
        assert transactions == 0
        assert service._unconfirmed_stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("guard_id", "mutation"),
    [
        ("MB1:_retirement_identity_holds:0", "not_live"),
        ("MB1:_retirement_identity_holds:1", "shutdown"),
        ("MB1:_retirement_identity_holds:2", "replay"),
        ("MB1:_retirement_identity_holds:3", "process"),
        ("MB1:_retirement_identity_holds:4", "child_object"),
        ("MB1:_retirement_identity_holds:5", "child_stopped"),
        ("MB1:_retirement_identity_holds:6", "stop_unconfirmed"),
        ("MB1:_retirement_identity_holds:7", "child_epoch"),
        ("MB1:_retirement_identity_holds:8", "roaster"),
        ("MB1:_retirement_identity_holds:9", "cache"),
        ("MB1:_retirement_identity_holds:10", "estop_epoch"),
        ("MB1:_retirement_identity_holds:11", "restart_state"),
    ],
)
async def test_every_identity_hold_term_has_an_independent_negative_and_positive_control(
    tmp_store: RoastStore, guard_id: str, mutation: str
) -> None:
    """MB1 mapping: each identity field independently closes the captured authority."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        identity = service._capture_retirement_identity()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert identity is not None
        assert service._retirement_identity_holds(identity)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        child = cast("_CurrentChild", service._mcp)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        if mutation == "not_live":
            service._requires_live_restart_clearance = False  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutation == "shutdown":
            service._shutdown_started = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutation == "replay":
            service._recording_replay_mode = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutation == "process":
            service.instance_id = "1" * 32
        elif mutation == "child_object":
            service._mcp = cast("MCPServerProcess", _CurrentChild())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutation == "child_stopped":
            child.running = False
        elif mutation == "stop_unconfirmed":
            child.stop_unconfirmed = True
        elif mutation == "child_epoch":
            child.child_epoch += 1
        elif mutation == "roaster":
            service._roaster = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif mutation == "cache":
            service._fault_lease_cache = FaultControlLease(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                generation=1,
                status=FaultLeaseStatus.UNKNOWN,
                run_id="terminal-faulted-run",
                mcp_session_id="fault-session",
            )
        elif mutation == "estop_epoch":
            service._note_emergency_stop_admission()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            service._restart_clearance_state = RestartClearanceState.CLEARED  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert guard_id.startswith("MB1:_retirement_identity_holds:")
        try:
            identity_holds = service._retirement_identity_holds(identity)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        except Exception as error:  # noqa: BLE001 - this is the total-function mutation oracle
            pytest.fail(f"identity predicate raised {type(error).__name__}")
        assert identity_holds is False
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    ["active", "latest_absent", "wrong_run", "not_completed", "not_faulted", "wrong_phase", "gate"],
)
async def test_each_final_durable_admission_term_rejects_before_presence_or_transaction(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """The final durable admission is independently closed after its ordinary awaits."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        presence_calls = 0
        transaction_calls = 0

        async def counted_presence() -> str:
            nonlocal presence_calls
            presence_calls += 1
            return "none"

        async def counted_transaction(**_kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal transaction_calls
            transaction_calls += 1
            return StrandedFaultRetirementCommit.PRECONDITION_REJECTED

        roaster.read_session_presence = counted_presence  # type: ignore[method-assign]
        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", counted_transaction)
        if variant == "active":
            original_active = tmp_store.active_run

            async def active() -> object:
                return await tmp_store.read_latest_run() or await original_active()

            monkeypatch.setattr(tmp_store, "active_run", active)
        elif variant == "latest_absent":

            async def absent() -> None:
                return None

            monkeypatch.setattr(tmp_store, "read_latest_run", absent)
        elif variant in {"wrong_run", "not_completed", "not_faulted", "wrong_phase"}:
            original_latest = tmp_store.read_latest_run

            async def altered_latest() -> object:
                latest = await original_latest()
                assert latest is not None
                if variant == "wrong_run":
                    return latest.model_copy(update={"run_id": "other-run"})
                if variant == "not_completed":
                    return latest.model_copy(update={"completed_at_utc": None})
                if variant == "not_faulted":
                    return latest.model_copy(update={"outcome": "completed"})
                return latest.model_copy(update={"agent_phase": RoastPhase.COMPLETE})

            monkeypatch.setattr(tmp_store, "read_latest_run", altered_latest)
        else:

            async def cleared(_process_id: str) -> RestartClearanceState:
                return RestartClearanceState.CLEARED

            monkeypatch.setattr(tmp_store, "read_process_restart_clearance", cleared)

        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "rejected"
        assert result.attempted_generation is None
        assert presence_calls == 0
        assert transaction_calls == 0
        assert (await tmp_store.read_fault_control_lease()).status is FaultLeaseStatus.OPEN
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_normal_open_reader_cache_mismatch_rejects_before_presence_or_write(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-F1c/MB1: normal admission requires reader and captured cache equality."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        presence_calls = 0

        async def counted_presence() -> str:
            nonlocal presence_calls
            presence_calls += 1
            return "none"

        async def mismatched_reader() -> FaultControlLease:
            return FaultControlLease(
                generation=1,
                status=FaultLeaseStatus.OPEN,
                run_id="other-run",
                mcp_session_id="fault-session",
            )

        roaster.read_session_presence = counted_presence  # type: ignore[method-assign]
        monkeypatch.setattr(service, "_read_fault_lease_fail_closed", mismatched_reader)
        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "rejected"
        assert result.attempted_generation is None
        assert presence_calls == 0
        assert service._unconfirmed_stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("verification", ["true", "false", "stale"])
async def test_transaction_exception_uses_at_most_one_current_exact_verifier(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, verification: str
) -> None:
    """An exception after durable work has one proof opportunity and never retries the write."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        original_retire = tmp_store.retire_stranded_fault_lease
        original_verify = tmp_store.verify_stranded_fault_retirement
        transaction_calls = 0
        verify_calls = 0

        async def raises_after_commit(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal transaction_calls
            transaction_calls += 1
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", original_verify)
            committed = await original_retire(**cast("Any", kwargs))
            assert committed is StrandedFaultRetirementCommit.COMMITTED_VERIFIED
            monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", verifier)
            if verification == "stale":
                cast("_CurrentChild", service._mcp).child_epoch += 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            raise OSError("synthetic uncertain transaction transport")

        async def verifier(**kwargs: object) -> bool:
            nonlocal verify_calls
            verify_calls += 1
            if verification == "true":
                return await original_verify(**kwargs)  # type: ignore[arg-type]
            if verification == "false":
                return False
            raise AssertionError("stale binding must not reach verifier")

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", raises_after_commit)
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", verifier)
        result = await service.retire_stranded_fault(_retirement_request())

        assert transaction_calls == 1
        assert verify_calls == (0 if verification == "stale" else 1)
        assert result.outcome == ("confirmed" if verification == "true" else "unconfirmed")
        if verification == "true":
            assert service._unconfirmed_stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        else:
            assert service._unconfirmed_stranded_fault_retirement is not None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_reconciliation_verifier_exception_retains_fence_without_a_second_transaction(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only reconciliation failure is unconfirmed and cannot manufacture a second write."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        original_retire = tmp_store.retire_stranded_fault_lease
        writes = 0

        async def indeterminate_after_commit(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal writes
            writes += 1
            committed = await original_retire(**cast("Any", kwargs))
            assert committed is StrandedFaultRetirementCommit.COMMITTED_VERIFIED
            return StrandedFaultRetirementCommit.INDETERMINATE

        async def raises(**_kwargs: object) -> bool:
            raise OSError("synthetic reconciliation read failure")

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate_after_commit)
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", raises)
        assert (await service.retire_stranded_fault(_retirement_request())).outcome == "unconfirmed"
        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "unconfirmed"
        assert writes == 1
        assert service._unconfirmed_stranded_fault_retirement is not None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_the_owned_retirement_task_without_clearing_its_fence(
    tmp_store: RoastStore,
) -> None:
    """TB12: shutdown cancels its D225 task while preserving uncertainty."""
    await tmp_store.initialize()
    request_task: asyncio.Task[object] | None = None
    release: asyncio.Event | None = None
    try:
        service, roaster = await _eligible_service(tmp_store)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_presence() -> str:
            entered.set()
            assert release is not None
            await release.wait()
            return "none"

        roaster.read_session_presence = blocked_presence  # type: ignore[method-assign]
        request_task = asyncio.create_task(service.retire_stranded_fault(_retirement_request()))
        await entered.wait()
        owned = service._stranded_fault_retirement  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert owned is not None

        await service.shutdown()

        with pytest.raises(asyncio.CancelledError):
            await request_task
        assert owned[1].cancelled()
        assert service._unconfirmed_stranded_fault_retirement is owned[0]  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    finally:
        if release is not None:
            release.set()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["health", "result"])
@pytest.mark.parametrize("read_name", ["lease", "active", "latest", "gate", "provenance"])
async def test_tb_proj_actual_persisted_g_plus_one_is_honestly_projected_at_every_snapshot_await(
    tmp_store: RoastStore,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    read_name: str,
) -> None:
    """TB-PROJ/T-F7: a real e-stop G+1 supersedes each old D225 snapshot read."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        if read_name == "provenance":
            assert (await service.retire_stranded_fault(_retirement_request())).outcome == "confirmed"
        old_lease = service._fault_lease_cache  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        old_context = _test_retirement_context(service, old_lease)
        prior_ledger = service._retirement_event_ledger  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        frames = service.events.subscribe()
        triggered = False
        advance_error: Exception | None = None
        admitted_result: Any = None
        admitted_durable: FaultControlLease | None = None
        in_shared_snapshot = consumer == "result"
        shared_snapshot: Any = None

        if consumer == "health":
            original_snapshot = service._stranded_fault_retirement_snapshot  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

            async def capture_shared_snapshot() -> Any:
                nonlocal in_shared_snapshot, shared_snapshot
                in_shared_snapshot = True
                shared_snapshot = await original_snapshot()
                return shared_snapshot

            monkeypatch.setattr(service, "_stranded_fault_retirement_snapshot", capture_shared_snapshot)

        async def failed_terminal_dispatch(**_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(service, "_execute_terminal_emergency_stop", failed_terminal_dispatch)
        service._fault_control_executor = cast("Any", object())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        roaster.set_latest_fault_session_id(cast(str, old_lease.mcp_session_id))
        terminal_runner = _PendingLiveRunner(cast(str, old_lease.run_id))
        terminal_runner.finalized = True
        service.active_run_id = terminal_runner.run_id
        service.runner = cast("Any", terminal_runner)

        async def advance_generation() -> None:
            nonlocal admitted_durable, admitted_result, advance_error, triggered
            if triggered:
                return
            triggered = True
            try:
                admitted_result = await service._submit_fault_controls_emergency_stop()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                service.runner = None
                service.active_run_id = None
                await service.settle_fault_lease_persistence_for_teardown()
                admitted_durable = await tmp_store.read_fault_control_lease()
            except Exception as error:  # noqa: BLE001 - must escape snapshot's fail-closed handler
                advance_error = error

        if read_name == "lease":
            original = service._read_fault_lease_fail_closed  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

            async def read_lease() -> FaultControlLease:
                value = await original()
                if in_shared_snapshot:
                    await advance_generation()
                return value

            monkeypatch.setattr(service, "_read_fault_lease_fail_closed", read_lease)
        elif read_name == "active":
            original = tmp_store.active_run

            async def read_active() -> object:
                value = await original()
                if in_shared_snapshot:
                    await advance_generation()
                return value

            monkeypatch.setattr(tmp_store, "active_run", read_active)
        elif read_name == "latest":
            original = tmp_store.read_latest_run

            async def read_latest() -> object:
                value = await original()
                if in_shared_snapshot:
                    await advance_generation()
                return value

            monkeypatch.setattr(tmp_store, "read_latest_run", read_latest)
        elif read_name == "gate":
            original = tmp_store.read_process_restart_clearance

            async def read_gate(process_id: str) -> RestartClearanceState:
                value = await original(process_id)
                if in_shared_snapshot:
                    await advance_generation()
                return value

            monkeypatch.setattr(tmp_store, "read_process_restart_clearance", read_gate)
        else:
            original = tmp_store.read_fault_control_lease_closure_provenance

            async def read_provenance(
                lease: FaultControlLease,
            ) -> FaultLeaseClosureProvenance | None:
                value = await original(lease)
                if in_shared_snapshot:
                    await advance_generation()
                return value

            monkeypatch.setattr(
                tmp_store, "read_fault_control_lease_closure_provenance", read_provenance
            )

        if consumer == "health":
            health = await service.health()
            snapshot = shared_snapshot
            assert snapshot is not None
            result = await service._stranded_fault_retirement_result("unconfirmed", 1, snapshot)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            controls = health.fault_controls
        else:
            snapshot = await service._stranded_fault_retirement_snapshot()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            result = await service._stranded_fault_retirement_result("unconfirmed", 1, snapshot)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            controls = result.fault_controls
        if not frames.empty():
            estop_frame = frames.get_nowait()
            assert estop_frame.data["generation"] == old_lease.generation + 1
        await service._emit_stranded_fault_retirement(result, 1, old_context, snapshot)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert triggered
        assert advance_error is None
        assert admitted_result is not None
        assert admitted_durable is not None
        assert admitted_durable.status is FaultLeaseStatus.OPEN
        assert admitted_durable.generation == old_lease.generation + 1
        assert service._fault_lease_cache == admitted_durable  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert result.attempted_generation == 1
        assert controls.status is FaultLeaseStatus.OPEN
        assert controls.generation == old_lease.generation + 1
        assert controls.closure_provenance is None
        assert controls.physical_stop_operative is True
        assert result.restart_clearance.eligible is False
        assert frames.empty()
        assert service._retirement_event_ledger == prior_ledger  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert roaster.calls == []
        service._fault_control_executor = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["child", "process", "stop"])
async def test_owned_fence_stale_while_waiting_for_restart_lock_returns_unconfirmed_without_retry(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, stale: str
) -> None:
    """The own-fence lock checkpoint reports uncertainty without reopening proof or write work."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        writes = 0
        verifies = 0

        async def indeterminate(**_kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal writes
            writes += 1
            return StrandedFaultRetirementCommit.INDETERMINATE

        async def false_verify(**_kwargs: object) -> bool:
            nonlocal verifies
            verifies += 1
            return False

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate)
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", false_verify)
        assert (await service.retire_stranded_fault(_retirement_request())).outcome == "unconfirmed"
        fence = service._unconfirmed_stranded_fault_retirement  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert fence is not None
        await service._restart_clearance_lock.acquire()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        request = asyncio.create_task(service.retire_stranded_fault(_retirement_request()))
        await asyncio.sleep(0)
        if stale == "child":
            cast("_CurrentChild", service._mcp).child_epoch += 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif stale == "process":
            service.instance_id = "b" * 32
        else:
            cast("_CurrentChild", service._mcp).stop_unconfirmed = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        service._restart_clearance_lock.release()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        result = await request

        assert result.outcome == "unconfirmed"
        assert result.attempted_generation == 1
        assert writes == 1
        assert verifies == 1
        assert service._unconfirmed_stranded_fault_retirement is fence  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        if service._restart_clearance_lock.locked():  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            service._restart_clearance_lock.release()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await tmp_store.close()


@pytest.mark.asyncio
async def test_owned_task_stale_before_first_presence_rejects_without_presence_or_transaction(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owned task's first checkpoint releases its own reservation before MCP proof."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        original_task = service._retire_stranded_fault_task  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        presence = 0
        writes = 0

        async def counted_presence() -> str:
            nonlocal presence
            presence += 1
            return "none"

        async def counted_write(**_kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal writes
            writes += 1
            return StrandedFaultRetirementCommit.PRECONDITION_REJECTED

        async def stale_entry(context: Any) -> Any:
            service._hardware_clear_request_in_flight = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            return await original_task(context)

        roaster.read_session_presence = counted_presence  # type: ignore[method-assign]
        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", counted_write)
        monkeypatch.setattr(service, "_retire_stranded_fault_task", stale_entry)
        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "rejected"
        assert result.attempted_generation == 1
        assert presence == 0
        assert writes == 0
        assert service._unconfirmed_stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_precondition_rejected_releases_only_its_owned_fence_without_history_or_retry(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, replacement: bool
) -> None:
    """PRECONDITION_REJECTED clears its own fence but cannot erase a successor fence."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        writes = 0

        async def rejected(**_kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal writes
            writes += 1
            if replacement:
                current = service._unconfirmed_stranded_fault_retirement  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                assert current is not None
                service._unconfirmed_stranded_fault_retirement = replace(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                    current, operation_id="e" * 32
                )
            return StrandedFaultRetirementCommit.PRECONDITION_REJECTED

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", rejected)
        before_runs = await _table_rows(tmp_store, "roast_runs")
        before_events = await _table_rows(tmp_store, "roast_events")
        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "rejected"
        assert result.attempted_generation == 1
        assert writes == 1
        assert await _table_rows(tmp_store, "roast_runs") == before_runs
        assert await _table_rows(tmp_store, "roast_events") == before_events
        fence = service._unconfirmed_stranded_fault_retirement  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert (fence is not None) is replacement
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_roaster_presence_is_an_independent_total_retirement_conditions_predicate(
    tmp_store: RoastStore,
) -> None:
    """MB1 term19: missing roaster closes the transient predicate without raising."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        assert service._retirement_conditions_hold()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        service._roaster = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        try:
            held = service._retirement_conditions_hold()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        except Exception as error:  # noqa: BLE001 - exact total-function oracle
            pytest.fail(f"roaster predicate raised {type(error).__name__}")
        assert held is False
        assert service._capture_retirement_identity() is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "term",
    ["cache", "status", "generation", "run_id", "session_id", "same_fence", "higher_fence"],
)
async def test_every_open_admission_term_is_isolated_from_cache_equality_masking(
    tmp_store: RoastStore, term: str
) -> None:
    """MB1: each normal OPEN-admission conjunct has a direct total-function oracle."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        identity = service._capture_retirement_identity()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert identity is not None
        valid = identity.cache_lease
        assert service._retirement_open_admission(valid, identity)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        candidate = valid
        if term == "cache":
            candidate = valid.model_copy(update={"mcp_session_id": "other-session"})
        elif term == "status":
            candidate = valid.model_copy(update={"status": FaultLeaseStatus.CLOSED})
        elif term == "generation":
            candidate = valid.model_copy(update={"generation": 0})
        elif term == "run_id":
            candidate = valid.model_copy(update={"run_id": None})
        elif term == "session_id":
            candidate = valid.model_copy(update={"mcp_session_id": None})
        elif term == "same_fence":
            service._unconfirmed_stranded_fault_retirement = _test_retirement_context(service, valid)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif term == "higher_fence":
            context = _test_retirement_context(service, valid)
            service._unconfirmed_stranded_fault_retirement = replace(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                context,
                expected_lease=valid.model_copy(update={"generation": valid.generation + 1}),
            )
        if term in {"status", "generation", "run_id", "session_id"}:
            service._fault_lease_cache = candidate  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            identity = replace(identity, cache_lease=candidate)
        try:
            admitted = service._retirement_open_admission(candidate, identity)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        except Exception as error:  # noqa: BLE001 - exact total-function oracle
            pytest.fail(f"open admission term {term} raised {type(error).__name__}")
        assert admitted is False
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("term", ["fence", "reservation", "context", "task"])
async def test_every_context_ownership_pointer_term_is_isolated_and_total(
    tmp_store: RoastStore, term: str
) -> None:
    """MB1: D225 ownership requires its exact fence, tuple, context, and current task."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        context = _test_retirement_context(service, service._fault_lease_cache)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        current = asyncio.current_task()
        assert current is not None
        service._unconfirmed_stranded_fault_retirement = context  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        service._stranded_fault_retirement = (context, cast("Any", current))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert service._retirement_context_is_owned(context)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        if term == "fence":
            service._unconfirmed_stranded_fault_retirement = replace(context, operation_id="f" * 32)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif term == "reservation":
            service._stranded_fault_retirement = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        elif term == "context":
            service._stranded_fault_retirement = (  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                replace(context, operation_id="f" * 32), cast("Any", current)
            )
        else:
            service._stranded_fault_retirement = (  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                context, cast("Any", await _settled_task())
            )
        try:
            owned = service._retirement_context_is_owned(context)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        except Exception as error:  # noqa: BLE001 - exact total-function oracle
            pytest.fail(f"ownership term {term} raised {type(error).__name__}")
        assert owned is False
        service._stranded_fault_retirement = None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["child_epoch", "hardware_clear"])
async def test_final_gate_read_race_rejects_before_reservation_presence_or_write(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """The final durable gate await cannot admit a stale D225 reservation."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        presence = 0
        writes = 0
        original_gate = tmp_store.read_process_restart_clearance

        async def counted_presence() -> str:
            nonlocal presence
            presence += 1
            return "none"

        async def counted_write(**_kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal writes
            writes += 1
            return StrandedFaultRetirementCommit.PRECONDITION_REJECTED

        async def stale_gate(process_id: str) -> RestartClearanceState:
            value = await original_gate(process_id)
            if change == "child_epoch":
                cast("_CurrentChild", service._mcp).child_epoch += 1  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            else:
                service._hardware_clear_request_in_flight = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            return value

        roaster.read_session_presence = counted_presence  # type: ignore[method-assign]
        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", counted_write)
        monkeypatch.setattr(tmp_store, "read_process_restart_clearance", stale_gate)
        result = await service.retire_stranded_fault(_retirement_request())

        assert result.outcome == "rejected"
        assert result.attempted_generation is None
        assert presence == 0
        assert writes == 0
        assert service._stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        assert service._unconfirmed_stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_latest_none_route_is_a_typed_pre_reservation_rejection_not_a_500(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing latest durable row is explicitly an HTTP rejection before any proof/write."""
    await tmp_store.initialize()
    try:
        service, roaster = await _eligible_service(tmp_store)
        presence = 0

        async def counted_presence() -> str:
            nonlocal presence
            presence += 1
            return "none"

        async def no_latest() -> None:
            return None

        roaster.read_session_presence = counted_presence  # type: ignore[method-assign]
        monkeypatch.setattr(tmp_store, "read_latest_run", no_latest)
        app = create_app(service)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/restart-clearance/retire-stranded-fault",
                json={"stranded_fault_confirmation": "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == "rejected"
        assert body["attempted_generation"] is None
        assert presence == 0
        assert service._stranded_fault_retirement is None  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        await service.shutdown()
    finally:
        await tmp_store.close()
