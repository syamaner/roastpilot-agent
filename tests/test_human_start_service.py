# pyright: reportPrivateUsage=false
"""Synthetic B3 service composition with real SQLite and strict mock SDK replies."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
import pytest_asyncio

from roastpilot_agent.api import RoastRunConflictError, RoastService
from roastpilot_agent.human_bootstrap import StartAttemptState
from roastpilot_agent.human_protection import CommandDisposition, ControlAuthority
from roastpilot_agent.human_run_ownership import HumanRunAdmission
from roastpilot_agent.models import (
    RestartClearanceRequest,
    RoastPhase,
    StartAttemptHoldState,
    StartHoldInstallOutcome,
    StartHoldSettlementOutcome,
)
from roastpilot_agent.store import RoastStore
from tests.test_controller import PROFILE
from tests.test_mcp_client import _BootstrapDouble, _bs_process, _hs_payload, _hs_raw

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def fixture(tmp_path: Path) -> AsyncIterator[tuple[RoastService, _BootstrapDouble]]:
    """Clear the actual service through D220 and D213 without assigning flags."""
    double = _BootstrapDouble()
    double.replies["start_roast_session"] = _hs_raw(_hs_payload())
    double.replies["set_recording_metadata"] = _hs_raw(
        {"origin": "ethiopia-harness", "roast_num": 1}
    )
    store = RoastStore(tmp_path / "b3.sqlite3")
    await store.initialize()
    service = RoastService(
        store,
        mcp=_bs_process(double),
        authority=ControlAuthority.HUMAN,
        run_loop=False,
        _mock_human_start=True,
    )
    request = RestartClearanceRequest(physical_confirmation=True)
    assert (
        await service.initialize_missing_fault_control_safety_record(request)
    ).outcome == "rejected"
    assert (await service.acknowledge_restart_clearance(request)).cleared
    double.calls.clear()
    try:
        yield service, double
    finally:
        await service.shutdown()
        await store.close()


async def test_bound_start(
    fixture: tuple[RoastService, _BootstrapDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, double = fixture
    original = service._store.install_start_attempt_hold

    async def install(hold: object) -> StartHoldInstallOutcome:
        assert not any(n == "start_roast_session" for n, _ in double.calls)
        return await original(hold)  # type: ignore[arg-type]

    monkeypatch.setattr(service._store, "install_start_attempt_hold", install)
    evaluate = Mock(side_effect=AssertionError("initial heat/fan evaluation"))
    monkeypatch.setattr(service._safety, "evaluate_command", evaluate)
    detail = await service._start_mock_human_roast(PROFILE)
    assert detail.agent_phase is RoastPhase.PREHEATING
    assert detail.instance_id == service.instance_id
    assert service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None and controller._human_run is not None
    run = controller._human_run
    assert run.admission is HumanRunAdmission.SESSION_BOUND
    assert service._mcp is not None
    assert controller._known_human_identity() == (
        "a" * 32,
        service._mcp.child_epoch,
    )  # type: ignore[union-attr]
    assert run._private_start_retained and service._human_ownership.lifecycle_blocked
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.CLEAR
    assert [n for n, _ in double.calls] == [
        "get_server_info",
        "get_runtime_config",
        "get_capture_evidence",
        "set_recording_metadata",
        "start_roast_session",
    ]
    evaluate.assert_not_called()
    for start in (service.start_roast, service._start_mock_human_roast):
        with pytest.raises(RoastRunConflictError):
            await start(PROFILE)
    assert sum(n == "start_roast_session" for n, _ in double.calls) == 1


@pytest.mark.parametrize("kind", ["indeterminate", "exception", "cancel"])
async def test_install_never_sends_and_fences(
    fixture: tuple[RoastService, _BootstrapDouble], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    service, double = fixture

    async def install(*args: object) -> StartHoldInstallOutcome:
        if kind == "exception":
            raise RuntimeError("synthetic")
        if kind == "cancel":
            raise asyncio.CancelledError
        return StartHoldInstallOutcome.INDETERMINATE

    monkeypatch.setattr(service._store, "install_start_attempt_hold", install)
    with pytest.raises((RoastRunConflictError, RuntimeError, asyncio.CancelledError)):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_unresolved
    assert not any(n == "start_roast_session" for n, _ in double.calls)
    with pytest.raises(RoastRunConflictError):
        await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
    assert (
        await service.initialize_missing_fault_control_safety_record(
            RestartClearanceRequest(physical_confirmation=True)
        )
    ).outcome == "rejected"


@pytest.mark.parametrize("kind", ["invalid", "error", "cancel"])
async def test_claimed_uncertainty_before_failure_observer(
    fixture: tuple[RoastService, _BootstrapDouble], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    service, double = fixture
    double.replies["start_roast_session"] = (
        _hs_raw({})
        if kind == "invalid"
        else RuntimeError("failed")
        if kind == "error"
        else asyncio.CancelledError()
    )
    observed: list[object] = []
    original = service._build_runner

    async def build(*args: object, **kwargs: object) -> object:
        runner = await original(*args, **kwargs)  # type: ignore[arg-type]
        assert runner is not None
        controller = runner.controller_for_human_fault_drop()
        assert controller is not None
        emit = controller._events.emit

        def observe(kind: object, payload: object) -> None:
            from roastpilot_agent.models import RoastEventKind

            if kind is RoastEventKind.COMMAND_FAILED:
                observed.append(kind)
                assert service._mock_start_unresolved
                assert controller._human_run is not None
                assert controller._human_run.command is CommandDisposition.UNKNOWN
                raise RuntimeError("observer failed")
            emit(kind, payload)  # type: ignore[arg-type]

        monkeypatch.setattr(controller._events, "emit", observe)
        return runner

    monkeypatch.setattr(service, "_build_runner", build)
    with pytest.raises((RoastRunConflictError, RuntimeError, asyncio.CancelledError)):
        await service._start_mock_human_roast(PROFILE)
    assert observed
    assert service._mock_start_unresolved
    assert service._mcp is not None and service._mcp._human_start_state is StartAttemptState.CLAIMED
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD


@pytest.mark.parametrize("kind", ["indeterminate", "cancel"])
async def test_bound_settlement_retains_known_identity(
    fixture: tuple[RoastService, _BootstrapDouble], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    service, _ = fixture

    async def settle(*args: object, **kwargs: object) -> StartHoldSettlementOutcome:
        if kind == "cancel":
            raise asyncio.CancelledError
        return StartHoldSettlementOutcome.INDETERMINATE

    monkeypatch.setattr(service._store, "settle_start_attempt_hold", settle)
    with pytest.raises((RoastRunConflictError, asyncio.CancelledError)):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_unresolved and service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None and controller._human_run is not None
    assert controller._human_run.admission is HumanRunAdmission.SESSION_BOUND
    assert controller._human_run.command is CommandDisposition.SETTLED
    controller._human_run.durably_closed = True
    assert service._human_ownership.lifecycle_blocked
    controller._human_run._private_start_retained = False
    assert not service._human_ownership.lifecycle_blocked


async def test_private_recovery_refuses(fixture: tuple[RoastService, _BootstrapDouble]) -> None:
    service, double = fixture
    with pytest.raises(RoastRunConflictError):
        await service.recover_on_start()
    assert double.calls == []


async def test_proven_non_entry_settles_without_uncertainty(
    fixture: tuple[RoastService, _BootstrapDouble],
) -> None:
    service, double = fixture
    double.replies["set_recording_metadata"] = RuntimeError("before claim")
    with pytest.raises(RoastRunConflictError, match="refused before claim"):
        await service._start_mock_human_roast(PROFILE)
    assert not service._mock_start_unresolved
    assert service._human_ownership.lifecycle_blocked
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.CLEAR
    assert not any(n == "start_roast_session" for n, _ in double.calls)
    assert service._mcp is not None
    assert service._mcp._human_start_state is StartAttemptState.REFUSED_BEFORE_CLAIM


@pytest.mark.parametrize("kind", ["phase", "started"])
async def test_publication_observer_raises_but_settles_exact_binding(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from roastpilot_agent.models import RoastEventKind

    service, double = fixture
    original = service._build_runner

    async def build(run_id: str, profile: object) -> object:
        runner = await original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None
        controller = runner.controller_for_human_fault_drop()
        assert controller is not None
        emit = controller._events.emit

        def observe(event: RoastEventKind, payload: dict[str, object]) -> None:
            if (
                kind == "phase"
                and event is RoastEventKind.PHASE_CHANGED
                and controller.phase is RoastPhase.PREHEATING
            ) or (kind == "started" and event is RoastEventKind.RUN_STARTED):
                assert controller._human_run is not None
                assert controller._human_run.admission is HumanRunAdmission.SESSION_BOUND
                raise RuntimeError("published observer")
            emit(event, payload)

        monkeypatch.setattr(controller._events, "emit", observe)
        return runner

    monkeypatch.setattr(service, "_build_runner", build)
    with pytest.raises(RuntimeError, match="published observer"):
        await service._start_mock_human_roast(PROFILE)
    assert not service._mock_start_unresolved
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.CLEAR
    assert service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None and controller._human_run is not None
    assert controller._human_run.admission is HumanRunAdmission.SESSION_BOUND
    assert controller._routine_inhibited
    assert sum(n == "start_roast_session" for n, _ in double.calls) == 1


@pytest.mark.parametrize("site", ["lease", "hold", "durable", "active", "bootstrap"])
async def test_no_run_barriers_recheck_before_row(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    service, double = fixture
    assert service._mcp is not None
    target, name = {
        "lease": (service, "_read_fault_lease_fail_closed"),
        "hold": (service, "_read_start_attempt_hold_fail_closed"),
        "durable": (service._store, "read_process_restart_clearance"),
        "active": (service._store, "active_run"),
        "bootstrap": (service._mcp, "_read_human_bootstrap"),
    }[site]
    original = getattr(target, name)

    async def barrier(*args: object, **kwargs: object) -> object:
        result = await original(*args, **kwargs)
        service._invalidate_pending_start_admissions_for_emergency_stop()
        return result

    monkeypatch.setattr(target, name, barrier)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    assert await service._store.list_runs() == []
    assert not any(n == "start_roast_session" for n, _ in double.calls)


@pytest.mark.parametrize(
    "change", ["adapter", "child", "process", "admission", "phase", "owner", "lease", "close"]
)
async def test_installed_owner_barrier_blocks_claim_and_retains(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from roastpilot_agent.human_run_ownership import AdmissionCloseReason, HumanRunOwnership
    from roastpilot_agent.models import FaultLeaseStatus

    service, double = fixture
    install = service._store.install_start_attempt_hold

    async def barrier(hold: object) -> StartHoldInstallOutcome:
        result = await install(hold)  # type: ignore[arg-type]
        assert service.runner is not None and service._mcp is not None
        controller = service.runner.controller_for_human_fault_drop()
        assert controller is not None
        if change == "adapter":
            service._roaster = None
        elif change == "child":
            service._mcp._child_epoch += 1
        elif change == "process":
            service.instance_id = "displaced"
        elif change == "admission":
            service._human_admission.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
        elif change == "phase":
            controller.transition_to(RoastPhase.FAULTED)
        elif change == "owner":
            service._human_ownership = HumanRunOwnership()
        elif change == "lease":
            service._fault_lease_cache = service._fault_lease_cache.model_copy(
                update={"status": FaultLeaseStatus.UNKNOWN}
            )
        else:
            service._command_arbiter.reserve_close(service._command_arbiter.generation)
        return result

    monkeypatch.setattr(service._store, "install_start_attempt_hold", barrier)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_unresolved
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD
    assert not any(n == "start_roast_session" for n, _ in double.calls)


async def test_displaced_bound_settlement_exception_still_fences(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = fixture

    async def settle(*args: object, **kwargs: object) -> StartHoldSettlementOutcome:
        service._roaster = None
        raise RuntimeError("displaced settlement")

    monkeypatch.setattr(service._store, "settle_start_attempt_hold", settle)
    with pytest.raises(RuntimeError, match="displaced settlement"):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_unresolved
    assert service._human_ownership.lifecycle_blocked


async def test_non_entry_settlement_cancel_preserves_hold(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, double = fixture
    double.replies["set_recording_metadata"] = RuntimeError("refuse")

    async def settle(*args: object, **kwargs: object) -> StartHoldSettlementOutcome:
        raise asyncio.CancelledError

    monkeypatch.setattr(service._store, "settle_start_attempt_hold", settle)
    with pytest.raises(asyncio.CancelledError):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_unresolved
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD
    assert not any(n == "start_roast_session" for n, _ in double.calls)


async def test_exact_indeterminate_record_survives_later_clear(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roastpilot_agent.models import (
        HardwareClearAcknowledgementRequest,
        StartAttemptHold,
        StartAttemptHoldRead,
    )

    service, double = fixture
    original = service._store.install_start_attempt_hold
    captured: list[StartAttemptHold] = []

    async def install(hold: StartAttemptHold) -> StartHoldInstallOutcome:
        captured.append(hold)
        assert await original(hold) is StartHoldInstallOutcome.INSTALLED_VERIFIED
        return StartHoldInstallOutcome.INDETERMINATE

    monkeypatch.setattr(service._store, "install_start_attempt_hold", install)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_unresolved is captured[0]
    assert captured[0].attempt_key != captured[0].run_id

    async def synthetic_later_clear() -> StartAttemptHoldRead:
        return StartAttemptHoldRead(StartAttemptHoldState.CLEAR)

    monkeypatch.setattr(service._store, "read_start_attempt_hold", synthetic_later_clear)
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.CLEAR
    for start in (service.start_roast, service._start_mock_human_roast):
        with pytest.raises(RoastRunConflictError):
            await start(PROFILE)
    with pytest.raises(RoastRunConflictError):
        await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
    with pytest.raises(RoastRunConflictError):
        await service.acknowledge_hardware_clear(
            HardwareClearAcknowledgementRequest(
                hardware_clear=True, teardown_incident_id="b" * 32, reason="synthetic"
            )
        )
    assert service._mock_start_unresolved is captured[0]
    assert not any(n == "start_roast_session" for n, _ in double.calls)


async def test_creation_exception_consumes_private_slot(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, double = fixture
    calls = 0

    async def create(**kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("creation interrupted before commit")

    monkeypatch.setattr(service._store, "create_run", create)
    with pytest.raises(RuntimeError):
        await service._start_mock_human_roast(PROFILE)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    assert calls == 1 and service._unpublished_start_run_id is None
    assert not any(n == "start_roast_session" for n, _ in double.calls)


async def test_foreign_refusal_after_claim_is_not_non_entry(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roastpilot_agent.human_bootstrap import HumanStartRefused, StartRefusal

    service, _ = fixture
    assert service._mcp is not None
    original = service._mcp._attempt_human_start

    async def displaced(*args: object, **kwargs: object) -> HumanStartRefused:
        await original(*args, **kwargs)  # type: ignore[arg-type]
        return HumanStartRefused(StartRefusal.GATE_REFUSED)

    monkeypatch.setattr(service._mcp, "_attempt_human_start", displaced)
    with pytest.raises(RoastRunConflictError, match="may have entered"):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_unresolved is not None
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD
    assert service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None and controller._human_run is not None
    assert controller._human_run.command is CommandDisposition.UNKNOWN


async def test_no_target_emergency_invalidates_parked_admission(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roastpilot_agent.models import FaultControlsActionRequest, OperatorAction

    service, double = fixture
    entered, release = asyncio.Event(), asyncio.Event()
    original = service._store.active_run

    async def parked() -> object:
        result = await original()
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(service._store, "active_run", parked)
    task = asyncio.create_task(service._start_mock_human_roast(PROFILE))
    await entered.wait()
    emergency = asyncio.create_task(
        service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )
    )
    # Admission is synchronous before the endpoint's first storage await.
    await asyncio.sleep(0)
    assert service._start_admission_generation > 0 and not task.done()
    release.set()
    with pytest.raises(RoastRunConflictError):
        await task
    await emergency
    assert not any(n == "start_roast_session" for n, _ in double.calls)


@pytest.mark.parametrize("site", ["settle", "history"])
async def test_settled_result_rechecks_and_history_cancellation_preserves_owner(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    service, _ = fixture
    original = service._store.settle_start_attempt_hold

    async def settle(*args: object, **kwargs: object) -> StartHoldSettlementOutcome:
        result = await original(*args, **kwargs)  # type: ignore[arg-type]
        if site == "settle":
            service._invalidate_pending_start_admissions_for_emergency_stop()
        else:
            assert service.runner is not None

            async def cancel() -> bool:
                raise asyncio.CancelledError

            monkeypatch.setattr(service.runner, "_flush_events", cancel)
        return result

    monkeypatch.setattr(service._store, "settle_start_attempt_hold", settle)
    with pytest.raises((RoastRunConflictError, asyncio.CancelledError)):
        await service._start_mock_human_roast(PROFILE)
    assert (service._mock_start_unresolved is not None) == (site == "settle")
    assert service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None and controller._human_run is not None
    assert controller._human_run.admission is HumanRunAdmission.SESSION_BOUND
    assert controller._human_run._private_start_retained
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.CLEAR


async def test_original_clearance_child_evidence_is_required(
    fixture: tuple[RoastService, _BootstrapDouble],
) -> None:
    service, double = fixture
    other = _bs_process(_BootstrapDouble())
    service._mock_clear_child = (other, other.child_epoch)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    assert await service._store.list_runs() == []
    assert double.calls == []


async def test_stale_durable_process_evidence_refuses_before_creation(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roastpilot_agent.models import RestartClearanceState

    service, double = fixture

    async def stale(process_id: str) -> RestartClearanceState:
        return RestartClearanceState.REQUIRED

    monkeypatch.setattr(service._store, "read_process_restart_clearance", stale)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    assert await service._store.list_runs() == [] and double.calls == []


async def test_publication_receipt_replacement_is_unbound(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from roastpilot_agent.models import RoastEventKind

    service, _ = fixture
    original = service._build_runner

    async def build(run_id: str, profile: object) -> object:
        runner = await original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None
        controller = runner.controller_for_human_fault_drop()
        assert controller is not None
        emit = controller._events.emit

        def observe(event: RoastEventKind, payload: dict[str, object]) -> None:
            if event is RoastEventKind.RUN_STARTED:
                assert service._mcp is not None and service._mcp._human_start_receipt is not None
                service._mcp._human_start_receipt = replace(service._mcp._human_start_receipt)
            emit(event, payload)

        monkeypatch.setattr(controller._events, "emit", observe)
        return runner

    monkeypatch.setattr(service, "_build_runner", build)
    with pytest.raises(RoastRunConflictError, match="may have entered"):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_unresolved is not None
    assert service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None and controller._human_run is not None
    assert controller._human_run.command is CommandDisposition.UNKNOWN


async def test_private_clearance_refuses_before_any_epoch_or_projection_read(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from roastpilot_agent.models import HardwareClearAcknowledgementRequest

    service, double = fixture
    double.replies["start_roast_session"] = _hs_raw({})
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_unresolved is not None
    epoch = AsyncMock(wraps=service._ensure_restart_epoch)
    active = AsyncMock(wraps=service._store.active_run)
    monkeypatch.setattr(service, "_ensure_restart_epoch", epoch)
    monkeypatch.setattr(service._store, "active_run", active)
    with pytest.raises(RoastRunConflictError):
        await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
    with pytest.raises(RoastRunConflictError):
        await service.acknowledge_hardware_clear(
            HardwareClearAcknowledgementRequest(
                hardware_clear=True, teardown_incident_id="b" * 32, reason="synthetic"
            )
        )
    epoch.assert_not_awaited()
    active.assert_not_awaited()


async def test_known_bound_emergency_dispatches_while_settlement_is_parked(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roastpilot_agent.models import FaultControlsActionRequest, OperatorAction
    from tests.test_mcp_client import CANNED

    service, double = fixture
    entered, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = service._store.settle_start_attempt_hold
    double.replies["emergency_stop"] = _hs_raw(
        {**cast("dict[str, object]", CANNED["emergency_stop"]), "session_id": "a" * 32}
    )
    double.during["emergency_stop"] = sent.set

    async def parked(*args: object, **kwargs: object) -> StartHoldSettlementOutcome:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service._store, "settle_start_attempt_hold", parked)
    start = asyncio.create_task(service._start_mock_human_roast(PROFILE))
    await entered.wait()
    emergency = asyncio.create_task(
        service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )
    )
    try:
        await asyncio.wait_for(sent.wait(), timeout=2)
        assert not start.done() and not release.is_set()
        assert (
            "emergency_stop",
            {"reason": "emergency stop requested in phase preheating"},
        ) in double.calls
        result = await emergency
    finally:
        release.set()
    with pytest.raises(RoastRunConflictError):
        await start
    assert result.result == "accepted"
    assert service._mock_start_unresolved is not None
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD
    assert not any(n in ("set_heat", "set_fan") for n, _ in double.calls)


async def test_definite_install_refusal_does_not_latch_uncertainty(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, double = fixture

    async def rejected(*args: object) -> StartHoldInstallOutcome:
        return StartHoldInstallOutcome.PRECONDITION_REJECTED

    monkeypatch.setattr(service._store, "install_start_attempt_hold", rejected)
    with pytest.raises(RoastRunConflictError, match="not installed"):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_unresolved is None
    assert not any(n == "start_roast_session" for n, _ in double.calls)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)


async def test_bound_observer_failure_schedules_current_owner_supervision(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roastpilot_agent.api import RoastRunner
    from roastpilot_agent.models import RoastEventKind

    service, _ = fixture
    service._run_loop = True
    supervised, release = asyncio.Event(), asyncio.Event()
    original = service._build_runner

    async def supervise(runner: RoastRunner) -> None:
        assert service.runner is runner
        supervised.set()
        await release.wait()

    monkeypatch.setattr(RoastRunner, "run", supervise)

    async def build(run_id: str, profile: object) -> object:
        runner = await original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None
        controller = runner.controller_for_human_fault_drop()
        assert controller is not None
        emit = controller._events.emit

        def observe(event: RoastEventKind, payload: dict[str, object]) -> None:
            if event is RoastEventKind.RUN_STARTED:
                raise RuntimeError("published observer")
            emit(event, payload)

        monkeypatch.setattr(controller._events, "emit", observe)
        return runner

    monkeypatch.setattr(service, "_build_runner", build)
    with pytest.raises(RuntimeError, match="published observer"):
        await service._start_mock_human_roast(PROFILE)
    assert service._loop_task is not None and not service._loop_task.done()
    await asyncio.wait_for(supervised.wait(), timeout=2)
    release.set()
    await service._loop_task
    service._run_loop = False


@pytest.mark.parametrize("copy_receipt", [False, True])
async def test_issued_foreign_or_copied_receipt_cannot_bind_service(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    copy_receipt: bool,
) -> None:
    from dataclasses import replace

    from roastpilot_agent.human_bootstrap import HumanStartAccepted
    from tests.test_mcp_client import _hs_attempt, _hs_ready

    service, _ = fixture
    assert service._mcp is not None
    original = service._mcp._attempt_human_start
    other, _, evidence = await _hs_ready()
    foreign = await _hs_attempt(other, evidence)
    assert isinstance(foreign, HumanStartAccepted)

    async def substitute(*args: object, **kwargs: object) -> HumanStartAccepted:
        accepted = await original(*args, **kwargs)  # type: ignore[arg-type]
        assert isinstance(accepted, HumanStartAccepted)
        return replace(accepted) if copy_receipt else foreign

    monkeypatch.setattr(service._mcp, "_attempt_human_start", substitute)
    with pytest.raises(RoastRunConflictError, match="may have entered"):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_unresolved is not None
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD


async def test_private_recovery_refusal_and_ordinary_automatic_successor(
    fixture: tuple[RoastService, _BootstrapDouble],
) -> None:
    from roastpilot_agent.mcp_client import RoasterControlAdapter

    service, double = fixture
    await service._store.create_run(
        run_id="persisted",
        profile=PROFILE,
        config=service._config,
        agent_phase=RoastPhase.PREHEATING,
    )
    with pytest.raises(RoastRunConflictError):
        await service.recover_on_start()
    assert service.runner is None and double.calls == []
    process = _bs_process(double)
    successor = RoastService(
        service._store,
        mcp=process,
        roaster=RoasterControlAdapter.from_process(process),
        authority=ControlAuthority.AUTOMATIC,
        run_loop=False,
    )
    await successor.recover_on_start()
    assert successor.instance_id != service.instance_id and not successor._mock_human_start
    assert successor.runner is not None
    assert successor.runner._controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert not any(n in ("start_roast_session", "set_heat", "set_fan") for n, _ in double.calls)


async def test_ordinary_start_and_clean_epoch_cannot_reach_live_respawn(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    service, double = fixture
    respawn = AsyncMock(side_effect=AssertionError("respawn"))
    clean = AsyncMock(side_effect=AssertionError("clean marker"))
    monkeypatch.setattr(service, "_respawn_mcp_for_device_config", respawn)
    monkeypatch.setattr(service._store, "mark_process_epoch_clean", clean)
    with pytest.raises(RoastRunConflictError):
        await service.start_roast(PROFILE)
    await service.mark_process_epoch_clean_after_ordered_teardown()
    respawn.assert_not_awaited()
    clean.assert_not_awaited()
    assert double.calls == []


@pytest.mark.parametrize("invalid", ["roaster", "live", "authority", "missing-mcp", "replay"])
async def test_private_constructor_rejects_wrong_composition(
    fixture: tuple[RoastService, _BootstrapDouble],
    invalid: str,
) -> None:
    service, _ = fixture
    with pytest.raises(ValueError):
        RoastService(
            service._store,
            mcp=None if invalid == "missing-mcp" else service._mcp,
            roaster=service._roaster if invalid == "roaster" else None,
            live_serve_mode=invalid == "live",
            recording_replay_mode=invalid == "replay",
            authority=ControlAuthority.AUTOMATIC
            if invalid == "authority"
            else ControlAuthority.HUMAN,
            run_loop=False,
            _mock_human_start=True,
        )


async def test_strict_mock_proof_refuses_before_any_row(
    fixture: tuple[RoastService, _BootstrapDouble],
) -> None:
    service, double = fixture
    double.replies["get_runtime_config"] = _hs_raw({})
    with pytest.raises(RoastRunConflictError, match="strict current mock bootstrap"):
        await service._start_mock_human_roast(PROFILE)
    assert not service._mock_start_used
    assert await service._store.list_runs() == []
    assert not any(n == "start_roast_session" for n, _ in double.calls)


@pytest.mark.parametrize(
    "stage", ["build-exception", "build-none", "prepare-refusal", "prepare-exception"]
)
async def test_build_and_preparation_interruptions_cannot_repeat_or_send(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    from roastpilot_agent.controller import RoastController

    service, double = fixture
    if stage.startswith("build"):

        async def build(*args: object, **kwargs: object) -> None:
            if stage == "build-exception":
                raise RuntimeError("build failed")

        monkeypatch.setattr(service, "_build_runner", build)
    else:

        def prepare(*args: object) -> bool:
            if stage == "prepare-exception":
                raise RuntimeError("prepare failed")
            return False

        monkeypatch.setattr(RoastController, "_prepare_human_session", prepare)
    with pytest.raises((RoastRunConflictError, RuntimeError)):
        await service._start_mock_human_roast(PROFILE)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    assert service._mock_start_used
    assert service._unpublished_start_run_id is None and service._starting_run_id is None
    assert len(await service._store.list_runs()) == 1
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.CLEAR
    assert not any(n == "start_roast_session" for n, _ in double.calls)


async def test_committed_creation_emergency_retires_exact_unpublished_row(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, double = fixture
    original = service._store.create_run

    async def create(**kwargs: object) -> None:
        await original(**kwargs)  # type: ignore[arg-type]
        assert service._unpublished_start_run_id == kwargs["run_id"]
        service._invalidate_pending_start_admissions_for_emergency_stop()

    monkeypatch.setattr(service._store, "create_run", create)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    rows = await service._store.list_runs()
    assert len(rows) == 1 and rows[0].outcome == "aborted"
    assert service._unpublished_start_run_id is None and service.active_run_id is None
    assert not any(n == "start_roast_session" for n, _ in double.calls)


@pytest.mark.parametrize(
    "state",
    [StartAttemptState.CLAIMED, StartAttemptState.ACCEPTED, StartAttemptState.REFUSED_BEFORE_CLAIM],
)
async def test_metadata_slot_gate_never_invokes_a(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    state: StartAttemptState,
) -> None:
    service, double = fixture
    assert service._mcp is not None
    child = service._mcp
    attempt = Mock(side_effect=AssertionError("A invocation crossed spent-slot service gate"))
    settle = Mock(side_effect=AssertionError("spent slot must not settle"))

    async def metadata(profile: object) -> None:
        child._human_start_attempt = state

    monkeypatch.setattr(service, "_recording_roast_num", metadata)
    monkeypatch.setattr(child, "_attempt_human_start", attempt)
    monkeypatch.setattr(service._store, "settle_start_attempt_hold", settle)
    with pytest.raises(RoastRunConflictError, match="metadata owner"):
        await service._start_mock_human_roast(PROFILE)
    attempt.assert_not_called()
    settle.assert_not_called()
    assert service._mock_start_unresolved is not None
    run = service._human_ownership._run
    assert run is not None and run._private_start_retained
    assert (run.command is CommandDisposition.UNKNOWN) == (
        state in (StartAttemptState.CLAIMED, StartAttemptState.ACCEPTED)
    )
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD
    assert not any(n == "start_roast_session" for n, _ in double.calls)


@pytest.mark.parametrize("stage", ["missing-controller", "prepare-refusal", "prepare-exception"])
async def test_idle_preparation_is_retired_without_idle_loop(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    service, double = fixture
    original = service._build_runner
    captured: list[object] = []

    async def build(run_id: str, profile: object) -> object:
        runner = await original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None
        controller = runner.controller_for_human_fault_drop()
        assert controller is not None and controller._human_run is not None
        captured.append(controller._human_run)
        if stage == "missing-controller":
            monkeypatch.setattr(runner, "controller_for_human_fault_drop", lambda: None)
        else:

            def prepare(*args: object) -> bool:
                if stage == "prepare-exception":
                    raise RuntimeError("idle prepare exception")
                return False

            monkeypatch.setattr(controller, "_prepare_human_session", prepare)
        return runner

    monkeypatch.setattr(service, "_build_runner", build)
    service._run_loop = True
    error = RuntimeError if stage == "prepare-exception" else RoastRunConflictError
    with pytest.raises(error):
        await service._start_mock_human_roast(PROFILE)
    assert len(captured) == 1
    assert service.runner is None and service.active_run_id is None
    assert service._loop_task is None
    assert not service._human_ownership.lifecycle_blocked
    rows = await service._store.list_runs()
    assert len(rows) == 1 and rows[0].outcome == "aborted"
    assert not any(n == "start_roast_session" for n, _ in double.calls)


@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_settlement_failure_does_not_claim_history_failure(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    service, double = fixture
    error = (
        asyncio.CancelledError("settlement cancelled")
        if failure == "cancel"
        else RuntimeError("settlement failed")
    )

    async def settle(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(service._store, "settle_start_attempt_hold", settle)
    with pytest.raises(type(error)) as raised:
        await service._start_mock_human_roast(PROFILE)
    assert raised.value is error
    assert service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None
    assert controller.phase is RoastPhase.PREHEATING
    assert controller._routine_inhibited
    assert controller._human_run is not None
    assert controller._human_run.command is CommandDisposition.SETTLED
    assert service._mock_start_unresolved is not None
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD
    assert sum(n == "start_roast_session" for n, _ in double.calls) == 1
    assert not any(
        n in ("set_heat", "set_fan", "stop_roast", "emergency_stop") for n, _ in double.calls
    )


@pytest.mark.parametrize("when", ["preclaim", "postclaim", "call-soon"])
async def test_actual_a_gates_and_inline_publication(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    from collections.abc import Callable

    from roastpilot_agent.human_bootstrap import HumanBootstrapEvidence, HumanStartResult
    from roastpilot_agent.models import RoastEventKind

    service, double = fixture
    assert service._mcp is not None
    child = service._mcp
    original = child._attempt_human_start
    calls: list[bool] = []
    order: list[str] = []
    queued = asyncio.Event()

    def emergency() -> None:
        order.append("emergency")
        service._invalidate_pending_start_admissions_for_emergency_stop()
        queued.set()

    async def attempt(
        evidence: HumanBootstrapEvidence,
        *,
        gate: Callable[[], bool],
        origin: str | None = None,
        roast_num: int | None = None,
    ) -> HumanStartResult:
        def checked() -> bool:
            if when == "preclaim" or when == "postclaim" and len(calls) == 1:
                service._invalidate_pending_start_admissions_for_emergency_stop()
            if when == "call-soon" and len(calls) == 1:
                asyncio.get_running_loop().call_soon(emergency)
            result = gate()
            calls.append(result)
            return result

        return await original(evidence, gate=checked, origin=origin, roast_num=roast_num)

    build_original = service._build_runner

    async def build(run_id: str, profile: object) -> object:
        runner = await build_original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None
        controller = runner.controller_for_human_fault_drop()
        assert controller is not None
        emit = controller._events.emit

        def observe(kind: RoastEventKind, payload: dict[str, object]) -> None:
            if kind is RoastEventKind.RUN_STARTED:
                order.append("published")
                assert child._human_start_receipt_spent
                assert (
                    controller._human_run is not None
                    and controller._human_run.admission is HumanRunAdmission.SESSION_BOUND
                )
            emit(kind, payload)

        monkeypatch.setattr(controller._events, "emit", observe)
        return runner

    monkeypatch.setattr(service, "_build_runner", build)
    monkeypatch.setattr(child, "_attempt_human_start", attempt)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    if when == "call-soon":
        await queued.wait()
        assert order == ["published", "emergency"]
        assert calls == [True, True]
    else:
        assert calls == ([False] if when == "preclaim" else [True, False])
    assert sum(n == "start_roast_session" for n, _ in double.calls) == (when != "preclaim")
    run = service._human_ownership._run
    assert run is not None
    assert (run.command is CommandDisposition.UNKNOWN) == (when == "postclaim")
    assert service._mock_start_unresolved is not None


@pytest.mark.parametrize(
    "site", ["flush-false", "flush-displace", "phase-displace", "response-displace"]
)
async def test_initial_history_boundaries_never_return_success(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    service, double = fixture
    original = service._build_runner
    called: list[str] = []

    async def build(run_id: str, profile: object) -> object:
        runner = await original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None
        flush, phase = runner._flush_events, runner._persist_phase_if_changed

        async def flush_events() -> bool:
            called.append("flush")
            if site == "flush-false":
                return False
            result = await flush()
            if site == "flush-displace":
                service._invalidate_pending_start_admissions_for_emergency_stop()
            return result

        async def persist_phase() -> None:
            called.append("phase")
            await phase()
            if site == "phase-displace":
                service._invalidate_pending_start_admissions_for_emergency_stop()

        monkeypatch.setattr(runner, "_flush_events", flush_events)
        monkeypatch.setattr(runner, "_persist_phase_if_changed", persist_phase)
        return runner

    read = service._store.read_run

    async def read_run(run_id: str) -> object:
        result = await read(run_id)
        if site == "response-displace":
            called.append("response")
            service._invalidate_pending_start_admissions_for_emergency_stop()
        return result

    monkeypatch.setattr(service, "_build_runner", build)
    monkeypatch.setattr(service._store, "read_run", read_run)
    from roastpilot_agent.human_persistence import HistoryUnavailable

    with pytest.raises(HistoryUnavailable if site == "flush-false" else RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    assert "flush" in called
    assert ("phase" in called) == (site in ("phase-displace", "response-displace"))
    run = service._human_ownership._run
    assert run is not None and run.admission is HumanRunAdmission.SESSION_BOUND
    assert run.command is CommandDisposition.SETTLED and run._private_start_retained
    assert sum(n == "start_roast_session" for n, _ in double.calls) == 1
    assert service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None
    assert controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert not any(n == "contain_heat_fixed_zero" for n, _ in double.calls)


async def test_no_target_emergency_after_install_returns_while_metadata_parked(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roastpilot_agent.models import FaultControlsActionRequest, OperatorAction

    service, double = fixture
    entered, release = asyncio.Event(), asyncio.Event()
    baseline = service._start_admission_generation

    async def metadata(profile: object) -> None:
        assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD
        entered.set()
        await release.wait()

    monkeypatch.setattr(service, "_recording_roast_num", metadata)
    task = asyncio.create_task(service._start_mock_human_roast(PROFILE))
    await entered.wait()
    try:
        result = await service.submit_fault_controls_action(
            FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        )
        assert result.result == "accepted"
        assert service._start_admission_generation > baseline
        assert not task.done() and not release.is_set()
    finally:
        release.set()
    with pytest.raises(RoastRunConflictError):
        await task
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD
    assert not any(n in ("start_roast_session", "set_heat", "set_fan") for n, _ in double.calls)


async def test_bound_ambiguity_later_clear_cannot_clear_via_guarded_entries(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roastpilot_agent.models import HardwareClearAcknowledgementRequest, StartAttemptHoldRead

    service, double = fixture

    async def settle(*args: object, **kwargs: object) -> StartHoldSettlementOutcome:
        return StartHoldSettlementOutcome.INDETERMINATE

    monkeypatch.setattr(service._store, "settle_start_attempt_hold", settle)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    retained = service._human_ownership._run
    assert retained is not None and retained.session_id == "a" * 32

    async def clear() -> StartAttemptHoldRead:
        return StartAttemptHoldRead(StartAttemptHoldState.CLEAR)

    monkeypatch.setattr(service._store, "read_start_attempt_hold", clear)
    request = RestartClearanceRequest(physical_confirmation=True)
    with pytest.raises(RoastRunConflictError):
        await service.acknowledge_restart_clearance(request)
    assert (
        await service.initialize_missing_fault_control_safety_record(request)
    ).outcome == "rejected"
    with pytest.raises(RoastRunConflictError):
        await service.acknowledge_hardware_clear(
            HardwareClearAcknowledgementRequest(
                hardware_clear=True, teardown_incident_id="b" * 32, reason="synthetic"
            )
        )
    for start in (service.start_roast, service._start_mock_human_roast):
        with pytest.raises(RoastRunConflictError):
            await start(PROFILE)
    assert service._human_ownership._run is retained and service._human_ownership.lifecycle_blocked
    assert retained.command is CommandDisposition.SETTLED
    assert sum(n == "start_roast_session" for n, _ in double.calls) == 1


@pytest.mark.parametrize("kind", ["lease", "held", "unknown", "active"])
async def test_cold_admission_refuses_before_create_or_claim(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from datetime import UTC, datetime

    from roastpilot_agent.models import StartAttemptHold, StartAttemptHoldRead

    service, double = fixture
    assert service._mcp is not None
    if kind == "lease":
        read = service._read_fault_lease_fail_closed

        async def lease() -> object:
            result = await read()
            return result.model_copy(update={"generation": result.generation + 1})

        monkeypatch.setattr(service, "_read_fault_lease_fail_closed", lease)
    elif kind == "held":
        await service._store.create_run(
            run_id="d" * 32,
            profile=PROFILE,
            config=service._config,
            agent_phase=RoastPhase.STARTING,
        )
        hold = StartAttemptHold(
            "c" * 32,
            "d" * 32,
            service.instance_id,
            service._mcp.child_epoch,
            service._fault_lease_cache.generation,
            datetime.now(UTC),
        )  # type: ignore[union-attr]
        assert (
            await service._store.install_start_attempt_hold(hold)
            is StartHoldInstallOutcome.INSTALLED_VERIFIED
        )
    elif kind == "unknown":

        async def unknown() -> StartAttemptHoldRead:
            return StartAttemptHoldRead(StartAttemptHoldState.UNKNOWN)

        monkeypatch.setattr(service, "_read_start_attempt_hold_fail_closed", unknown)
    else:
        await service._store.create_run(
            run_id="other",
            profile=PROFILE,
            config=service._config,
            agent_phase=RoastPhase.PREHEATING,
        )
    create = Mock(side_effect=AssertionError("cold admission created row"))
    monkeypatch.setattr(service._store, "create_run", create)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    create.assert_not_called()
    assert not service._mock_start_used
    assert not any(n == "start_roast_session" for n, _ in double.calls)


async def test_d213_final_projection_cannot_publish_stale_clearance(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from roastpilot_agent.models import RestartClearanceState

    _, double = fixture
    store = RoastStore(tmp_path / "projection.sqlite3")
    await store.initialize()
    service = RoastService(
        store,
        mcp=_bs_process(double),
        authority=ControlAuthority.HUMAN,
        run_loop=False,
        _mock_human_start=True,
    )
    request = RestartClearanceRequest(physical_confirmation=True)
    assert (
        await service.initialize_missing_fault_control_safety_record(request)
    ).outcome == "rejected"
    original = service._fault_controls_projection
    called: list[str] = []

    async def projection(*args: object, **kwargs: object) -> object:
        result = await original(*args, **kwargs)  # type: ignore[arg-type]
        called.append("projection")
        service._invalidate_pending_start_admissions_for_emergency_stop()
        return result

    confirm = Mock(side_effect=AssertionError("stale projection confirmed"))
    monkeypatch.setattr(service, "_fault_controls_projection", projection)
    monkeypatch.setattr(service._store, "confirm_process_restart_clearance", confirm)
    with pytest.raises(RoastRunConflictError, match="evidence became stale"):
        await service.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
    assert called == ["projection"]
    confirm.assert_not_called()
    # The old cached value is not authority for this failed operation.
    assert service._restart_clearance_state is RestartClearanceState.REQUIRED

    await service.shutdown()
    await store.close()


@pytest.mark.parametrize(
    "when",
    [
        "retire-failed",
        "retire-superseded",
        "build-displaced",
        "prepare-superseded",
        "missing-frame",
    ],
)
async def test_preinstall_interruptions_preserve_blocked_or_superseding_owner(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    service, double = fixture
    original = service._build_runner
    observed: list[str] = []

    async def build(run_id: str, profile: object) -> object:
        runner = await original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None
        controller = runner.controller_for_human_fault_drop()
        assert controller is not None
        prepare = controller._prepare_human_session

        def preparing(*args: object) -> bool:
            if when.startswith("retire"):
                return False
            if when == "prepare-superseded":
                service._invalidate_pending_start_admissions_for_emergency_stop()
                return False
            result = prepare(*args)  # type: ignore[arg-type]
            if when == "missing-frame":
                controller._prepared_human_session = None
            return result

        monkeypatch.setattr(controller, "_prepare_human_session", preparing)
        if when == "build-displaced":
            service._invalidate_pending_start_admissions_for_emergency_stop()
        return runner

    retire = service._retire_never_dispatched_start

    async def retiring(run_id: str) -> None:
        observed.append("retire")
        if when == "retire-failed":
            raise RuntimeError("retirement failed")
        await retire(run_id)
        service._invalidate_pending_start_admissions_for_emergency_stop()

    monkeypatch.setattr(service, "_build_runner", build)
    monkeypatch.setattr(service, "_retire_never_dispatched_start", retiring)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    assert observed == (["retire"] if when.startswith("retire") else [])
    assert service.active_run_id is not None and service.runner is not None
    assert service._human_ownership.lifecycle_blocked
    assert service._loop_task is None
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.CLEAR
    assert not any(n == "start_roast_session" for n, _ in double.calls)


@pytest.mark.parametrize("supersede", [False, True])
async def test_failed_start_observer_preserves_original_exception_and_emergency_priority(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
    supersede: bool,
) -> None:
    from roastpilot_agent.models import RoastEventKind

    service, double = fixture
    original = service._build_runner
    failure = RuntimeError("original install exception")
    observed: list[RoastEventKind] = []

    async def install(*args: object) -> None:
        raise failure

    async def build(run_id: str, profile: object) -> object:
        runner = await original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None
        controller = runner.controller_for_human_fault_drop()
        assert controller is not None
        emit = controller._events.emit

        def observe(kind: RoastEventKind, payload: dict[str, object]) -> None:
            if kind is RoastEventKind.COMMAND_FAILED:
                observed.append(kind)
                assert service._mock_start_unresolved is not None
                if supersede:
                    service._command_arbiter.admit_emergency_stop()
                raise RuntimeError("later failed observer")
            emit(kind, payload)

        monkeypatch.setattr(controller._events, "emit", observe)
        return runner

    monkeypatch.setattr(service, "_build_runner", build)
    monkeypatch.setattr(service._store, "install_start_attempt_hold", install)
    with pytest.raises(RuntimeError) as raised:
        await service._start_mock_human_roast(PROFILE)
    assert raised.value is failure and observed == [RoastEventKind.COMMAND_FAILED]
    assert service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None
    assert controller.phase is (RoastPhase.STARTING if supersede else RoastPhase.FAULTED)
    assert not any(n == "start_roast_session" for n, _ in double.calls)


async def test_normal_bound_run_loop_is_scheduled(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = fixture
    original = service._build_runner
    running, release = asyncio.Event(), asyncio.Event()

    async def build(run_id: str, profile: object) -> object:
        runner = await original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None

        async def run() -> None:
            running.set()
            await release.wait()

        monkeypatch.setattr(runner, "run", run)
        return runner

    monkeypatch.setattr(service, "_build_runner", build)
    service._run_loop = True
    detail = await service._start_mock_human_roast(PROFILE)
    assert detail.agent_phase is RoastPhase.PREHEATING
    await running.wait()
    assert service._loop_task is not None and not service._loop_task.done()
    release.set()
    await service._loop_task


async def test_bound_publication_loses_currentness_before_settlement(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roastpilot_agent.models import RoastEventKind

    service, _ = fixture
    original = service._build_runner
    observed: list[RoastEventKind] = []

    async def build(run_id: str, profile: object) -> object:
        runner = await original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None
        controller = runner.controller_for_human_fault_drop()
        assert controller is not None
        emit = controller._events.emit

        def observe(kind: RoastEventKind, payload: dict[str, object]) -> None:
            emit(kind, payload)
            if kind is RoastEventKind.RUN_STARTED:
                observed.append(kind)
                assert (
                    controller._human_run is not None
                    and controller._human_run.session_id == "a" * 32
                )
                controller.transition_to(RoastPhase.FAULTED)

        monkeypatch.setattr(controller._events, "emit", observe)
        return runner

    settle = Mock(side_effect=AssertionError("superseded publication settled"))
    monkeypatch.setattr(service, "_build_runner", build)
    monkeypatch.setattr(service._store, "settle_start_attempt_hold", settle)
    with pytest.raises(RoastRunConflictError, match="settlement owner"):
        await service._start_mock_human_roast(PROFILE)
    settle.assert_not_called()
    assert observed == [RoastEventKind.RUN_STARTED]
    run = service._human_ownership._run
    assert run is not None and run.admission is HumanRunAdmission.SESSION_BOUND
    assert run.command is CommandDisposition.SETTLED
    assert service._mock_start_unresolved is not None


async def test_automatic_successor_retains_real_b3_held_record(
    fixture: tuple[RoastService, _BootstrapDouble],
) -> None:
    from roastpilot_agent.mcp_client import RoasterControlAdapter

    service, double = fixture
    double.replies["start_roast_session"] = RuntimeError("claimed transport lost")
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE)
    before = await service._store.read_start_attempt_hold()
    assert before.state is StartAttemptHoldState.HELD
    process = _bs_process(double)
    successor = RoastService(
        service._store,
        mcp=process,
        roaster=RoasterControlAdapter.from_process(process),
        authority=ControlAuthority.AUTOMATIC,
        run_loop=False,
    )
    double.calls.clear()
    await successor.recover_on_start()
    assert successor.runner is not None
    assert successor.runner._controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert await service._store.read_start_attempt_hold() == before
    with pytest.raises(RoastRunConflictError):
        await successor.start_roast(PROFILE)
    assert not any(n in ("start_roast_session", "set_heat", "set_fan") for n, _ in double.calls)


@pytest.mark.parametrize("identity", ["exact", "foreign", "unknown"])
async def test_d212_actual_entry_cannot_release_private_owner(
    fixture: tuple[RoastService, _BootstrapDouble],
    identity: str,
) -> None:
    from roastpilot_agent.models import FaultControlsActionRequest, FaultLeaseStatus, OperatorAction
    from tests.test_mcp_client import _fault_control_state_payload

    service, double = fixture
    if identity == "unknown":
        double.replies["start_roast_session"] = RuntimeError("possible entry")
        with pytest.raises(RoastRunConflictError):
            await service._start_mock_human_roast(PROFILE)
    else:
        await service._start_mock_human_roast(PROFILE)
    runner = service.runner
    assert runner is not None and service.active_run_id is not None
    controller = runner.controller_for_human_fault_drop()
    assert controller is not None
    if controller.phase is not RoastPhase.FAULTED:
        controller.transition_to(RoastPhase.FAULTED)
    await runner._persist_phase_if_changed()
    lease = await service._store.open_fault_control_lease(
        run_id=service.active_run_id, mcp_session_id="a" * 32
    )
    payload = _fault_control_state_payload("b" * 32 if identity == "foreign" else "a" * 32)
    payload.update(fan_level_percent=0, cooling_on=False)
    device = cast("dict[str, object]", payload["device_state"])
    device.update(fan_level_percent=0, cooling_on=False)
    double.replies["get_roast_state"] = _hs_raw(payload)
    result = await service.submit_fault_controls_action(
        FaultControlsActionRequest(
            action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
            confirmation=True,
            expected_generation=lease.generation,
        )
    )
    if result.result == "accepted":
        await runner.tick_once()
    current = await service._store.read_fault_control_lease()
    owner = service._human_ownership._run
    assert owner is not None and owner._private_start_retained
    assert service._human_ownership.lifecycle_blocked
    timeline = await service._store.read_timeline(owner.run_id)
    confirmed = any(event.payload == {"outcome": "confirmed"} for event in timeline.events)
    if identity == "exact":
        assert result.result == "accepted" and confirmed
        assert current.status is FaultLeaseStatus.CLOSED and owner.durably_closed
    else:
        assert not confirmed and current.status is FaultLeaseStatus.OPEN
    assert not any(n in ("set_heat", "set_fan", "stop_cooling") for n, _ in double.calls)


async def test_history_failure_observer_cannot_replace_original_error(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roastpilot_agent.human_persistence import HistoryUnavailable
    from roastpilot_agent.models import RoastEventKind

    service, _ = fixture
    original = service._build_runner
    error = HistoryUnavailable("initial history failed")
    observed: list[RoastEventKind] = []

    async def build(run_id: str, profile: object) -> object:
        runner = await original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None
        controller = runner.controller_for_human_fault_drop()
        assert controller is not None
        emit = controller._events.emit

        async def flush() -> bool:
            raise error

        def observe(kind: RoastEventKind, payload: dict[str, object]) -> None:
            if (
                kind is RoastEventKind.PHASE_CHANGED
                and controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
            ):
                observed.append(kind)
                raise RuntimeError("later history observer")
            emit(kind, payload)

        monkeypatch.setattr(runner, "_flush_events", flush)
        monkeypatch.setattr(controller._events, "emit", observe)
        return runner

    monkeypatch.setattr(service, "_build_runner", build)
    with pytest.raises(HistoryUnavailable) as raised:
        await service._start_mock_human_roast(PROFILE)
    assert raised.value is error and observed == [RoastEventKind.PHASE_CHANGED]
    run = service._human_ownership._run
    assert run is not None and run.admission is HumanRunAdmission.SESSION_BOUND
    assert run.command is CommandDisposition.SETTLED


async def test_metadata_owner_gate_never_invokes_a(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = fixture
    assert service._mcp is not None
    attempt = Mock(side_effect=AssertionError("A invocation crossed stale-owner service gate"))

    async def metadata(profile: object) -> None:
        service._invalidate_pending_start_admissions_for_emergency_stop()

    monkeypatch.setattr(service, "_recording_roast_num", metadata)
    monkeypatch.setattr(service._mcp, "_attempt_human_start", attempt)
    with pytest.raises(RoastRunConflictError, match="metadata owner"):
        await service._start_mock_human_roast(PROFILE)
    attempt.assert_not_called()
    run = service._human_ownership._run
    assert run is not None and run.command is CommandDisposition.SETTLED
    assert service._mock_start_unresolved is not None
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD


async def test_successful_prepare_superseded_before_hold_install(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, double = fixture
    original = service._build_runner

    async def build(run_id: str, profile: object) -> object:
        runner = await original(run_id, profile)  # type: ignore[arg-type]
        assert runner is not None
        controller = runner.controller_for_human_fault_drop()
        assert controller is not None
        prepare = controller._prepare_human_session

        def superseded(*args: object) -> bool:
            assert prepare(*args)  # type: ignore[arg-type]
            service._invalidate_pending_start_admissions_for_emergency_stop()
            return True

        monkeypatch.setattr(controller, "_prepare_human_session", superseded)
        return runner

    install = Mock(side_effect=AssertionError("superseded preparation installed a hold"))
    monkeypatch.setattr(service, "_build_runner", build)
    monkeypatch.setattr(service._store, "install_start_attempt_hold", install)
    with pytest.raises(RoastRunConflictError, match="prepared owner was superseded"):
        await service._start_mock_human_roast(PROFILE)
    install.assert_not_called()
    assert service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None and controller.phase is RoastPhase.FAULTED
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.CLEAR
    assert not any(n == "start_roast_session" for n, _ in double.calls)


async def test_missing_runner_supersession_does_not_retire_foreign_generation(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, double = fixture

    async def build(*args: object) -> None:
        service._invalidate_pending_start_admissions_for_emergency_stop()

    retire = Mock(side_effect=AssertionError("superseded missing runner retired"))
    monkeypatch.setattr(service, "_build_runner", build)
    monkeypatch.setattr(service, "_retire_never_dispatched_start", retire)
    with pytest.raises(RoastRunConflictError, match="runner was not constructed"):
        await service._start_mock_human_roast(PROFILE)
    retire.assert_not_called()
    assert service.runner is None and service.active_run_id is not None
    assert (await service._store.active_run()) is not None
    assert not any(n == "start_roast_session" for n, _ in double.calls)


async def test_post_callback_phase_gate_never_invokes_a(
    fixture: tuple[RoastService, _BootstrapDouble],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = fixture
    assert service._mcp is not None
    observed: list[str] = []
    attempt = Mock(side_effect=AssertionError("A invocation crossed changed callback phase"))

    async def metadata(profile: object) -> None:
        assert service.runner is not None
        controller = service.runner.controller_for_human_fault_drop()
        assert controller is not None

        def teardown() -> bool:
            if not observed:
                observed.append("phase changed")
                controller.transition_to(RoastPhase.FAULTED)
            return False

        monkeypatch.setattr(controller, "_teardown_unconfirmed", teardown)

    monkeypatch.setattr(service, "_recording_roast_num", metadata)
    monkeypatch.setattr(service._mcp, "_attempt_human_start", attempt)
    with pytest.raises(RoastRunConflictError, match="metadata owner"):
        await service._start_mock_human_roast(PROFILE)
    assert observed == ["phase changed"]
    attempt.assert_not_called()
    assert service._mock_start_unresolved is not None
    assert (await service._store.read_start_attempt_hold()).state is StartAttemptHoldState.HELD
