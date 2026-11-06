"""D225 stranded-fault retirement service behaviour (hardware-free)."""

import asyncio
from typing import cast

import pytest

from roastpilot_agent.api import RoastRunConflictError, RoastService
from roastpilot_agent.config import AppConfig, ControllerConfig
from roastpilot_agent.mcp_client import MCPServerProcess
from roastpilot_agent.models import (
    FaultLeaseStatus,
    OperatorAction,
    RestartClearanceRequest,
    RestartClearanceState,
    RoastPhase,
    RoastProfile,
    StrandedFaultRetirementRequest,
)
from roastpilot_agent.store import RoastStore, StrandedFaultRetirementCommit
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
        verify_calls = 0

        async def indeterminate_after_commit(**kwargs: object) -> StrandedFaultRetirementCommit:
            nonlocal retire_calls
            retire_calls += 1
            committed = await original_retire(**kwargs)  # type: ignore[arg-type]
            assert committed is StrandedFaultRetirementCommit.COMMITTED_VERIFIED
            return StrandedFaultRetirementCommit.INDETERMINATE

        async def verify_on_second_attempt(**kwargs: object) -> bool:
            nonlocal verify_calls
            verify_calls += 1
            return verify_calls > 2 and await original_verify(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate_after_commit)
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", verify_on_second_attempt)
        request = StrandedFaultRetirementRequest(
            stranded_fault_confirmation=(
                "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
            )
        )

        first = await service.retire_stranded_fault(request)
        second = await service.retire_stranded_fault(request)

        assert first.outcome == "unconfirmed"
        assert second.outcome == "confirmed"
        assert retire_calls == 1
        assert verify_calls == 3
        assert (await tmp_store.read_fault_control_lease()).status is FaultLeaseStatus.CLOSED
        await service.shutdown()
    finally:
        await tmp_store.close()


@pytest.mark.asyncio
async def test_foreign_child_binding_cannot_reconcile_an_owned_fence(
    tmp_store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child epoch change leaves uncertainty fenced and skips the verifier."""
    await tmp_store.initialize()
    try:
        service, _roaster = await _eligible_service(tmp_store)
        original_retire = tmp_store.retire_stranded_fault_lease
        original_verify = tmp_store.verify_stranded_fault_retirement
        verify_calls = 0

        async def indeterminate_after_commit(**kwargs: object) -> StrandedFaultRetirementCommit:
            assert (
                await original_retire(**kwargs)  # type: ignore[arg-type]
                is StrandedFaultRetirementCommit.COMMITTED_VERIFIED
            )
            return StrandedFaultRetirementCommit.INDETERMINATE

        async def unexpected_verify(**_kwargs: object) -> bool:
            raise AssertionError("stale child must not run exact reconciliation")

        monkeypatch.setattr(tmp_store, "retire_stranded_fault_lease", indeterminate_after_commit)

        async def fail_first_verification(**kwargs: object) -> bool:
            nonlocal verify_calls
            verify_calls += 1
            return verify_calls > 2 and await original_verify(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", fail_first_verification)
        monkeypatch.setattr(tmp_store, "verify_stranded_fault_retirement", unexpected_verify)
        request = StrandedFaultRetirementRequest(
            stranded_fault_confirmation=(
                "roaster_stopped_empty_heat_fan_cooling_off_physical_stop_ready"
            )
        )
        assert (await service.retire_stranded_fault(request)).outcome == "unconfirmed"
        child = cast("_CurrentChild", service._mcp)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        child.child_epoch += 1

        assert (await service.retire_stranded_fault(request)).outcome == "unconfirmed"
        assert (await tmp_store.read_fault_control_lease()).status is FaultLeaseStatus.CLOSED
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
