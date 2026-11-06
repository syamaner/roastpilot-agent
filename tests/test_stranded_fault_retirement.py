"""D225 stranded-fault retirement service behaviour (hardware-free)."""

import asyncio
from typing import cast

import pytest

import roastpilot_agent.api as api_module
from roastpilot_agent.api import (
    RoastRunConflictError,
    RoastService,
    _DetachedRecoveryLoop,  # pyright: ignore[reportPrivateUsage]
)
from roastpilot_agent.config import AppConfig, ControllerConfig
from roastpilot_agent.mcp_client import MCPServerProcess
from roastpilot_agent.models import (
    FaultLeaseClosureProvenance,
    FaultLeaseStatus,
    OperatorAction,
    RestartClearanceRequest,
    RestartClearanceState,
    RoastEventKind,
    RoastPhase,
    RoastProfile,
    StrandedFaultRetirementEventData,
    StrandedFaultRetirementRequest,
)
from roastpilot_agent.store import (
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
@pytest.mark.parametrize("stale_binding", ["child_epoch", "process", "stop_unconfirmed"])
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
        if stale_binding == "child_epoch":
            child = cast("_CurrentChild", service._mcp)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
            child.child_epoch += 1
        elif stale_binding == "process":
            service.instance_id = "a" * 32
        else:
            cast("_CurrentChild", service._mcp).stop_unconfirmed = True  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        assert (await service.retire_stranded_fault(request)).outcome == "unconfirmed"
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
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TB-EVT/MB19-21: D225 has one public changed frame and no history write/leak."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        record_event_calls = 0

        async def forbidden_record_event(*_args: object, **_kwargs: object) -> None:
            nonlocal record_event_calls
            record_event_calls += 1

        monkeypatch.setattr(tmp_store, "record_event", forbidden_record_event)
        frames = service.events.subscribe()
        result = await service.retire_stranded_fault(_retirement_request())
        emitted = frames.get_nowait()
        rendered = f"{result.model_dump(mode='json')}|{emitted.data}"

        assert result.outcome == "confirmed"
        assert record_event_calls == 0
        assert frames.empty()
        assert emitted.event.value == RoastEventKind.FAULT_CONTROLS_CHANGED.value
        assert "outcome" not in emitted.data
        for private_value in (
            "terminal-faulted-run",
            "fault-session",
            service.instance_id,
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
