# pyright: reportPrivateUsage=false
"""Synthetic-only private T3a controller, SDK and service boundary witnesses."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ListToolsResult, Tool

from roastpilot_agent.api import RoastRunConflictError, RoastService
from roastpilot_agent.human_bootstrap import BOOTSTRAP_TOOL_INVENTORY, BootstrapRead
from roastpilot_agent.human_pre_fc_permission import (
    LeverReceipt,
    MarkReceipt,
    PairOutcome,
    PermitState,
)
from roastpilot_agent.human_protection import (
    CommandDisposition,
    ControlAuthority,
    EndpointDisposition,
    ToolDiscovery,
    WriteClass,
)
from roastpilot_agent.human_run_ownership import AdmissionCloseReason
from roastpilot_agent.mcp_client import MCPConnectionError, RoasterControlAdapter
from roastpilot_agent.models import (
    RestartClearanceRequest,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
    StartHoldSettlementOutcome,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict
from roastpilot_agent.timing_evidence import TimingEvidenceRead, TimingEvidenceReadOutcome
from tests.test_controller import PROFILE, reading
from tests.test_controller_human_mode import HumanHarness
from tests.test_human_pre_admission_fence import fresh_boundary, run_of
from tests.test_human_pre_fc_permission import control_receipt, mark_receipt, timing
from tests.test_human_session_bound import setup
from tests.test_human_start_service import fixture as fixture
from tests.test_mcp_client import (
    _BS_SERVER_INFO,
    _BootstrapDouble,
    _BootstrapPeer,
    _bs_process,
    _bs_raw,
    _hs_payload,
    _hs_raw,
    _sdk_session,
)
from tests.test_timing_evidence_consumer import schema

pytestmark = pytest.mark.asyncio


async def granted(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[HumanHarness, RoasterControlAdapter, _BootstrapDouble]:
    """Bind through actual Start issuance; simulate only final service settlement."""
    h, adapter, process, receipt = await setup(monkeypatch)
    h.controller._bind_prepared_human_session(adapter, receipt)
    h.controller._grant_private_pre_fc(adapter)
    fresh_boundary(h)
    h.actuator.status = 2
    double = cast("_BootstrapDouble", process._session)
    for name in ("set_heat", "set_fan"):
        double.replies[name] = _hs_raw(control_receipt())
    double.replies["mark_first_crack"] = _hs_raw(mark_receipt())
    double.calls.clear()
    return h, adapter, double


def evaluation() -> SafetyEvaluation:
    """Return a policy-approved pair for isolated dispatch-boundary tests."""
    return SafetyEvaluation(
        rule="synthetic",
        verdict=SafetyVerdict.ALLOW,
        reason="synthetic",
        adjusted_heat=65,
        adjusted_fan=30,
    )


def sends(double: _BootstrapDouble) -> list[str]:
    """Return actual named SDK sends."""
    return [name for name, _ in double.calls]


def change(h: HumanHarness, adapter: RoasterControlAdapter, fault: str) -> None:
    """Invalidate one independently meaningful current-owner or dispatch gate."""
    run = run_of(h)
    if fault == "run":
        replacement = replace(run)
        h.ownership._run = replacement
        h.controller._human_run = replacement
    elif fault == "session":
        adapter._started_session_id = "wrong"
    elif fault == "child":
        assert adapter._fixed_zero_transport is not None
        adapter._fixed_zero_transport._child_epoch += 1
    elif fault == "generation":
        h.arbiter.admit_emergency_stop()
    elif fault == "endpoint":
        run.endpoint = EndpointDisposition.ADMITTED
    elif fault == "unknown":
        run.command = CommandDisposition.UNKNOWN
    elif fault == "closed":
        h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    elif fault == "durable":
        run.durably_closed = True
    elif fault == "adapter":
        adapter._identity_generation += 1
    else:
        h.clock.advance(30)


async def test_grant_is_one_shot_and_default_stays_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    h, adapter, _ = await granted(monkeypatch)
    with pytest.raises(RuntimeError):
        h.controller._grant_private_pre_fc(adapter)
    assert await h.controller._execute_targets(evaluation())
    assert (h.controller._current_heat, h.controller._current_fan) == (65, 30)
    assert h.controller._private_pre_fc is not None
    assert h.controller._private_pre_fc.pair_outcome is PairOutcome.BOTH_CONFIRMED
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    if h.controller.phase is RoastPhase.PREHEATING:
        h.controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert not await h.controller._execute_targets(evaluation())
    assert h.controller._private_pre_fc.state is PermitState.ENDED


@pytest.mark.parametrize("fault", ["endpoint", "unknown", "closed", "durable"])
async def test_grant_refuses_invalid_binding_without_permission(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    h, adapter, process, receipt = await setup(monkeypatch)
    h.controller._bind_prepared_human_session(adapter, receipt)
    change(h, adapter, fault)
    double = cast("_BootstrapDouble", process._session)
    double.calls.clear()
    with pytest.raises(RuntimeError, match="grant displaced"):
        h.controller._grant_private_pre_fc(adapter)
    assert h.controller._private_pre_fc is None
    assert not await h.controller._execute_targets(evaluation())
    assert sends(double) == []


@pytest.mark.parametrize("boundary", ["set_heat", "set_fan"])
@pytest.mark.parametrize(
    "fault",
    [
        "run",
        "session",
        "child",
        "generation",
        "endpoint",
        "unknown",
        "closed",
        "durable",
        "adapter",
        "expired",
    ],
)
async def test_both_queued_boundaries_recheck(
    monkeypatch: pytest.MonkeyPatch, boundary: str, fault: str
) -> None:
    h, adapter, double = await granted(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def occupy() -> None:
        entered.set()
        await release.wait()

    blocker: asyncio.Task[tuple[bool, None]] | None = None
    if boundary == "set_heat":
        blocker = asyncio.create_task(h.arbiter.run_routine(h.arbiter.generation, occupy))
        await entered.wait()
    else:
        original = h.controller._run_routine_write

        async def queue(*args: Any, **kwargs: Any) -> Any:
            nonlocal blocker
            result = await original(*args, **kwargs)
            if kwargs.get("write_class") is WriteClass.HEAT:
                blocker = asyncio.create_task(h.arbiter.run_routine(h.arbiter.generation, occupy))
                await entered.wait()
            return result

        monkeypatch.setattr(h.controller, "_run_routine_write", queue)
    task = asyncio.create_task(h.controller._execute_targets(evaluation()))
    if boundary == "set_fan":
        await entered.wait()
    await asyncio.sleep(0)
    assert not task.done()
    change(h, adapter, fault)
    release.set()
    assert blocker is not None
    await blocker
    await task
    assert sends(double) == ([] if boundary == "set_heat" else ["set_heat"])
    assert h.controller._current_heat == (0 if boundary == "set_heat" else 65)
    assert h.controller._current_fan == 0
    before = sends(double)
    await h.controller._execute_targets(evaluation())
    assert sends(double) == before


@pytest.mark.parametrize("name", ["set_heat", "set_fan"])
@pytest.mark.parametrize("failure", ["lost", "wrong_value", "wrong_session", "raw_type", "cancel"])
async def test_entered_unknown_retains_only_confirmed_lever(
    monkeypatch: pytest.MonkeyPatch, name: str, failure: str
) -> None:
    h, _, double = await granted(monkeypatch)
    if failure in ("lost", "cancel"):
        double.replies[name] = (
            asyncio.CancelledError() if failure == "cancel" else RuntimeError("lost")
        )
    else:
        raw = control_receipt()
        key = "heat_level_percent" if name == "set_heat" else "fan_level_percent"
        raw[key if failure != "wrong_session" else "session_id"] = {
            "wrong_value": 12,
            "wrong_session": "wrong",
            "raw_type": float(raw[key]),
        }[failure]
        double.replies[name] = _hs_raw(raw)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await h.controller._execute_targets(evaluation())
    else:
        await h.controller._execute_targets(evaluation())
    assert sends(double) == (["set_heat"] if name == "set_heat" else ["set_heat", "set_fan"])
    assert (h.controller._current_heat, h.controller._current_fan) == (
        0 if name == "set_heat" else 65,
        0,
    )
    assert run_of(h).command is CommandDisposition.UNKNOWN
    assert h.controller._private_pre_fc is not None
    assert h.controller._private_pre_fc.pair_outcome is PairOutcome.UNKNOWN
    for _ in range(2):
        assert not await h.controller._execute_targets(evaluation())
    assert len(double.calls) == (1 if name == "set_heat" else 2)


async def test_cancel_before_callback_is_known_unsent(monkeypatch: pytest.MonkeyPatch) -> None:
    h, _, double = await granted(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def occupy() -> None:
        entered.set()
        await release.wait()

    blocker = asyncio.create_task(h.arbiter.run_routine(h.arbiter.generation, occupy))
    await entered.wait()
    task = asyncio.create_task(h.controller._execute_targets(evaluation()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await blocker
    assert sends(double) == [] and run_of(h).command is CommandDisposition.SETTLED


@pytest.mark.parametrize("name", ["set_heat", "set_fan"])
async def test_late_lever_reply_cannot_overwrite_replacement_mirrors(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    h, adapter, double = await granted(monkeypatch)
    origin = run_of(h)

    def displace() -> None:
        change(h, adapter, "run")
        h.controller._current_heat, h.controller._current_fan = 9, 8

    double.during[name] = displace
    assert not await h.controller._execute_targets(evaluation())
    assert (h.controller._current_heat, h.controller._current_fan) == (9, 8)
    assert run_of(h) is not origin and run_of(h).command is CommandDisposition.SETTLED
    assert sends(double) == (["set_heat"] if name == "set_heat" else ["set_heat", "set_fan"])
    permission = h.controller._private_pre_fc
    assert permission is not None and permission.pair_outcome is PairOutcome.DISPLACED
    assert permission.heat_receipt is not None
    assert (permission.fan_receipt is not None) is (name == "set_fan")


@pytest.mark.parametrize("fault", ["session", "child"])
async def test_entered_displaced_child_or_session_keeps_original_run_unknown(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    h, adapter, double = await granted(monkeypatch)
    origin = run_of(h)
    double.during["set_heat"] = lambda: change(h, adapter, fault)
    assert not await h.controller._execute_targets(evaluation())
    assert run_of(h) is origin and origin.command is CommandDisposition.UNKNOWN
    assert sends(double) == ["set_heat"]
    assert (h.controller._current_heat, h.controller._current_fan) == (0, 0)


async def test_mark_reservation_retains_acknowledged_heat_and_withholds_fan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, _, double = await granted(monkeypatch)
    permission = h.controller._private_pre_fc
    assert permission is not None

    def reserve() -> None:
        permission.reserve_mark()

    double.during["set_heat"] = reserve
    assert await h.controller._execute_targets(evaluation())
    assert sends(double) == ["set_heat"]
    assert (h.controller._current_heat, h.controller._current_fan) == (65, 0)
    assert permission.state is PermitState.MARK_RESERVED
    assert permission.pair_outcome is PairOutcome.HEAT_CONFIRMED
    assert not await h.controller._execute_targets(evaluation())


async def test_nondefault_adaptive_damping_retains_acknowledged_heat_on_cancelled_fan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolated existing adaptive switch; the production configuration stays unchanged."""
    h, adapter, double = await granted(monkeypatch)
    await prepare_mark(h, adapter, monkeypatch)
    config = h.controller._config
    levers = config.pre_first_crack_levers
    trim = levers.late_maillard_trim.model_copy(update={"adaptive_depth_enabled": True})
    h.controller._config = config.model_copy(
        update={"pre_first_crack_levers": levers.model_copy(update={"late_maillard_trim": trim})}
    )
    h.controller._trim_latched = True
    double.replies["set_fan"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await h.controller._apply_deterministic_pre_fc_levers(reading(165, bean_ror_c_per_min=15))
    assert h.controller._current_heat == h.controller._trim_depth_applied == 65
    assert h.controller._current_fan == 0 and run_of(h).command is CommandDisposition.UNKNOWN


@pytest.mark.parametrize("operation", ["lever", "mark"])
async def test_cancellation_survives_failing_attribution_observer(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    h, adapter, double = await granted(monkeypatch)
    await prepare_mark(h, adapter, monkeypatch)
    original = h.emitter.emit

    def observe(kind: RoastEventKind, payload: dict[str, object]) -> None:
        if kind in (RoastEventKind.COMMAND_FAILED, RoastEventKind.FIRST_CRACK):
            raise RuntimeError("synthetic observer failure")
        original(kind, payload)

    monkeypatch.setattr(h.emitter, "emit", observe)
    double.replies["set_heat" if operation == "lever" else "mark_first_crack"] = (
        asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        if operation == "lever":
            await h.controller._execute_targets(evaluation())
        else:
            await h.controller.operator_mark_first_crack()
    assert h.controller._private_pre_fc is not None
    assert h.controller._private_pre_fc.state is PermitState.ENDED
    assert run_of(h).command is (
        CommandDisposition.UNKNOWN if operation == "lever" else CommandDisposition.SETTLED
    )


async def prepare_mark(
    h: HumanHarness, adapter: RoasterControlAdapter, monkeypatch: pytest.MonkeyPatch
) -> AsyncMock:
    """Use real typed mark SDK receipts and synthetic accepted T2 records."""
    if h.controller.phase is RoastPhase.PREHEATING:
        h.controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    h.controller._t0_confirmed = True
    read = AsyncMock(
        side_effect=[
            TimingEvidenceRead(TimingEvidenceReadOutcome.EVIDENCE, record=timing()),
            TimingEvidenceRead(TimingEvidenceReadOutcome.EVIDENCE, record=timing(post=True)),
        ]
    )
    monkeypatch.setattr(adapter, "read_timing_evidence", read)
    return read


@pytest.mark.parametrize("stage", [1, 2], ids=["phase-evaluation", "source-evaluation"])
@pytest.mark.parametrize("fault", ["run", "phase"])
async def test_mark_evaluation_await_rechecks_owner_and_phase(
    monkeypatch: pytest.MonkeyPatch, stage: int, fault: str
) -> None:
    h, adapter, double = await granted(monkeypatch)
    read = await prepare_mark(h, adapter, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.sink.persist_evaluation
    count = 0

    async def persist(value: SafetyEvaluation) -> int | None:
        nonlocal count
        count += 1
        if count == stage:
            entered.set()
            await release.wait()
        return await original(value)

    monkeypatch.setattr(h.sink, "persist_evaluation", persist)
    task = asyncio.create_task(h.controller.operator_mark_first_crack())
    await entered.wait()
    if fault == "run":
        change(h, adapter, "run")
    else:
        # Both are permitted lever phases, but only roasting admits the mark.
        h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
        h.controller.transition_to(RoastPhase.PREHEATING)
    owner = replace(run_of(h))
    phase = h.controller.phase
    release.set()
    await task
    assert run_of(h) == owner
    assert h.controller.phase is phase
    assert not h.controller._canonical_human_fc_confirmed
    assert sends(double) == [] and read.await_count == 0


@pytest.mark.parametrize("action", ["cancel", "displace"])
async def test_queued_mark_is_withheld_before_operation_entry(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    h, adapter, double = await granted(monkeypatch)
    read = await prepare_mark(h, adapter, monkeypatch)
    occupied, release, preread = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def occupy() -> None:
        occupied.set()
        await release.wait()

    async def before() -> TimingEvidenceRead:
        preread.set()
        return TimingEvidenceRead(TimingEvidenceReadOutcome.EVIDENCE, record=timing())

    read.side_effect = before
    blocker = asyncio.create_task(h.arbiter.run_routine(h.arbiter.generation, occupy))
    await occupied.wait()
    task = asyncio.create_task(h.controller.operator_mark_first_crack())
    await preread.wait()
    assert not task.done()
    if action == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        change(h, adapter, "run")
    owner = replace(run_of(h))
    release.set()
    await blocker
    if action != "cancel":
        await task
    assert run_of(h) == owner and run_of(h).command is CommandDisposition.SETTLED
    assert sends(double) == [] and read.await_count == 1
    assert not h.controller._canonical_human_fc_confirmed
    assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert h.controller._private_pre_fc is not None
    assert h.controller._private_pre_fc.state is PermitState.ENDED
    assert h.emitter.events[-1] == (
        RoastEventKind.FIRST_CRACK,
        {"source": "operator", "outcome": "withheld", "session_id": "a" * 32},
    )


@pytest.mark.parametrize("operation", ["set_heat", "set_fan", "mark_first_crack"])
@pytest.mark.parametrize("displace", [False, True])
async def test_wrong_typed_adapter_acknowledgement_is_scoped_uncertainty(
    monkeypatch: pytest.MonkeyPatch, operation: str, displace: bool
) -> None:
    """Deliberate adapter double violation, not a result the strict SDK can emit."""
    h, adapter, double = await granted(monkeypatch)
    await prepare_mark(h, adapter, monkeypatch)
    original = adapter._private_pre_fc_call
    origin = run_of(h)
    calls: list[str] = []

    async def invalid(name: Any, value: int | None = None) -> LeverReceipt | MarkReceipt:
        calls.append(name)
        result = await original(name, value)
        if name != operation:
            return result
        if displace:
            change(h, adapter, "run")
            run_of(h).run_id = "replacement-run"
            h.controller._current_heat, h.controller._current_fan = 9, 8
        return (
            LeverReceipt("a" * 32, "roasting", 65, 30, False)
            if name == "mark_first_crack"
            else MarkReceipt("a" * 32, 2.0, 2)
        )

    monkeypatch.setattr(adapter, "_private_pre_fc_call", invalid)
    if operation == "mark_first_crack":
        await h.controller.operator_mark_first_crack()
        expected = [operation]
    else:
        assert await h.controller._execute_targets(evaluation()) is (
            operation == "set_fan" and not displace
        )
        expected = ["set_heat"] if operation == "set_heat" else ["set_heat", "set_fan"]
    assert calls == sends(double) == expected
    assert run_of(h).command is (
        CommandDisposition.UNKNOWN
        if operation != "mark_first_crack" and not displace
        else CommandDisposition.SETTLED
    )
    assert h.controller._private_pre_fc is not None
    assert h.controller._private_pre_fc.state is PermitState.ENDED
    assert not h.controller._canonical_human_fc_confirmed
    assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    if displace:
        assert run_of(h) is not origin and origin.command is CommandDisposition.SETTLED
        assert run_of(h).run_id == "replacement-run"
        assert (h.controller._current_heat, h.controller._current_fan) == (9, 8)
    if operation != "mark_first_crack":
        assert h.emitter.events[-1] == (
            RoastEventKind.COMMAND_FAILED,
            {
                "command": operation,
                "outcome": "unknown",
                "run_id": origin.run_id,
                "session_id": "a" * 32,
                "command_generation": h.controller._private_pre_fc.binding.command_generation,
                "child_epoch": h.controller._private_pre_fc.binding.child_epoch,
            },
        )
    else:
        assert h.emitter.events[-1] == (
            RoastEventKind.FIRST_CRACK,
            {"source": "operator", "outcome": "unresolved", "session_id": "a" * 32},
        )
    await h.controller.operator_mark_first_crack()
    assert not await h.controller._execute_targets(evaluation())
    assert calls == expected


async def test_entered_mark_completion_cannot_advance_replacement_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, adapter, double = await granted(monkeypatch)
    read = await prepare_mark(h, adapter, monkeypatch)
    original = adapter._private_pre_fc_call
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(name: Any, value: int | None = None) -> LeverReceipt | MarkReceipt:
        result = await original(name, value)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(adapter, "_private_pre_fc_call", held)
    task = asyncio.create_task(h.controller.operator_mark_first_crack())
    await entered.wait()
    change(h, adapter, "run")
    run_of(h).run_id = "replacement-run"
    replacement = run_of(h)
    before = replace(replacement)
    h.controller._current_heat, h.controller._current_fan = 9, 8
    release.set()
    await task
    assert run_of(h) is replacement and replacement == before
    assert (h.controller._current_heat, h.controller._current_fan) == (9, 8)
    assert sends(double) == ["mark_first_crack"] and read.await_count == 1
    assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert not h.controller._canonical_human_fc_confirmed
    assert h.controller._private_pre_fc is not None
    assert h.controller._private_pre_fc.state is PermitState.ENDED
    assert h.emitter.events[-1] == (
        RoastEventKind.FIRST_CRACK,
        {"source": "operator", "outcome": "unresolved", "session_id": "a" * 32},
    )


@pytest.mark.parametrize("fault", ["generation", "expired"])
async def test_curve_sampling_rechecks_lease_and_evidence_after_lever_await(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    h, adapter, double = await granted(monkeypatch)
    await prepare_mark(h, adapter, monkeypatch)
    h.controller._trim_latched = True
    original = adapter._private_pre_fc_call
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(name: Any, value: int | None = None) -> LeverReceipt | MarkReceipt:
        entered.set()
        await release.wait()
        return await original(name, value)

    monkeypatch.setattr(adapter, "_private_pre_fc_call", held)
    before = h.controller._history.curve_window()
    task = asyncio.create_task(
        h.controller._run_tick_controls(reading(165), generation=h.arbiter.generation)
    )
    await entered.wait()
    change(h, adapter, fault)
    release.set()
    await task
    assert sends(double) == ["set_heat"]
    assert h.controller._history.curve_window() == before == []


async def test_mark_requires_append_identity_and_never_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, adapter, double = await granted(monkeypatch)
    read = await prepare_mark(h, adapter, monkeypatch)
    await h.controller.operator_mark_first_crack()
    assert h.controller.phase is RoastPhase.DEVELOPMENT
    assert h.controller._canonical_human_fc_confirmed
    assert sends(double) == ["mark_first_crack"] and read.await_count == 2
    await h.controller.operator_mark_first_crack()
    assert sends(double) == ["mark_first_crack"] and read.await_count == 2
    assert not await h.controller._execute_targets(evaluation())


async def test_mark_through_strict_t2_adapter_and_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the strict T2 decoder on both sides of the actual named mark SDK call."""
    from copy import deepcopy

    from tests.test_timing_evidence_consumer import golden

    h, adapter, double = await granted(monkeypatch)
    h.controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    h.controller._t0_confirmed = True
    assert adapter._fixed_zero_transport is not None
    adapter._fixed_zero_transport._timing_evidence_discovery = ToolDiscovery.DISCOVERED
    before = golden()
    before["requested_session_id"] = "a" * 32
    record = before["record"]
    record.update(
        session_id="a" * 32,
        session_ownership_sequence=1,
        phase="roasting",
        first_crack=None,
        evidence_sequence=1,
    )
    for key in ("bean", "environment"):
        record[key].update(acquisition_lo_seconds=3.0, acquisition_hi_seconds=3.0)
    after = deepcopy(before)
    after["record"].update(
        phase="development", evidence_sequence=2, first_crack=golden()["record"]["first_crack"]
    )
    after["record"]["first_crack"].update(
        source="manual_mark", time_basis="producer_canonical_record"
    )
    for key in ("bean", "environment"):
        after["record"][key].update(acquisition_lo_seconds=4.0, acquisition_hi_seconds=4.0)
    double.replies["get_timing_evidence"] = _hs_raw(before)
    double.during["mark_first_crack"] = lambda: double.replies.update(
        get_timing_evidence=_hs_raw(after)
    )
    await h.controller.operator_mark_first_crack()
    assert h.controller.phase is RoastPhase.DEVELOPMENT
    assert sends(double) == ["get_timing_evidence", "mark_first_crack", "get_timing_evidence"]
    assert adapter._timing_evidence_pin is not None
    assert adapter._timing_evidence_pin.record.evidence_sequence == 2
    await h.controller.operator_mark_first_crack()
    assert len(double.calls) == 3


@pytest.mark.parametrize(
    "fault", ["existing", "absent", "receipt", "lost", "sequence", "time", "index", "source"]
)
async def test_mark_doubt_ends_permit_without_actuator_unknown(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    h, adapter, double = await granted(monkeypatch)
    read = await prepare_mark(h, adapter, monkeypatch)
    before, after = timing(), timing(post=True)
    if fault == "existing":
        before = replace(before, first_crack=after.first_crack)
    elif fault == "absent":
        read.side_effect = [TimingEvidenceRead(TimingEvidenceReadOutcome.NOT_ATTEMPTED)]
    elif fault == "receipt":
        double.replies["mark_first_crack"] = _hs_raw({**mark_receipt(), "event_count": True})
    elif fault == "lost":
        double.replies["mark_first_crack"] = RuntimeError("lost")
    elif fault == "sequence":
        after = replace(after, evidence_sequence=1)
    elif fault in ("time", "index", "source"):
        from roastpilot_agent.timing_evidence import EventSource

        assert after.first_crack is not None
        fields: dict[str, Any] = {
            "time": {"canonical_seconds": 2.0000000000000004},
            "index": {"event_index": 3},
            "source": {"source": EventSource.MODEL_DETECTION},
        }[fault]
        after = replace(after, first_crack=replace(after.first_crack, **fields))
    if fault != "absent":
        read.side_effect = [
            TimingEvidenceRead(TimingEvidenceReadOutcome.EVIDENCE, record=before),
            TimingEvidenceRead(TimingEvidenceReadOutcome.EVIDENCE, record=after),
        ]
    await h.controller.operator_mark_first_crack()
    assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert not h.controller._canonical_human_fc_confirmed
    assert run_of(h).command is CommandDisposition.SETTLED
    assert sends(double) == ([] if fault in ("existing", "absent") else ["mark_first_crack"])
    await h.controller.operator_mark_first_crack()
    assert len(double.calls) <= 1
    assert not await h.controller._execute_targets(evaluation())


async def test_emergency_while_post_read_held_fences_late_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, adapter, double = await granted(monkeypatch)
    await prepare_mark(h, adapter, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def read() -> TimingEvidenceRead:
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
            await release.wait()
        return TimingEvidenceRead(
            TimingEvidenceReadOutcome.EVIDENCE, record=timing(post=count == 2)
        )

    monkeypatch.setattr(adapter, "read_timing_evidence", read)
    task = asyncio.create_task(h.controller.operator_mark_first_crack())
    await entered.wait()
    assert await h.controller.operator_emergency_stop()
    assert h.commands == ["emergency_stop"]
    release.set()
    await task
    assert h.controller.phase is RoastPhase.FAULTED
    assert not h.controller._canonical_human_fc_confirmed
    assert sends(double) == ["mark_first_crack"]


async def test_held_mark_preserves_existing_arbiter_emergency_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, adapter, double = await granted(monkeypatch)
    read = await prepare_mark(h, adapter, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = adapter._private_pre_fc_call

    async def mark(name: Any, value: int | None = None) -> Any:
        entered.set()
        await release.wait()
        return await original(name, value)

    monkeypatch.setattr(adapter, "_private_pre_fc_call", mark)
    task = asyncio.create_task(h.controller.operator_mark_first_crack())
    await entered.wait()
    emergency = asyncio.create_task(h.controller.operator_emergency_stop())
    await asyncio.sleep(0)
    assert not emergency.done() and h.commands == []
    release.set()
    await task
    assert await emergency
    assert h.commands == ["emergency_stop"] and read.await_count == 1
    assert not h.controller._canonical_human_fc_confirmed
    assert sends(double) == ["mark_first_crack"]


@pytest.mark.parametrize("stage", ["pre", "post"])
@pytest.mark.parametrize(
    "fault", ["run", "session", "child", "generation", "endpoint", "unknown", "closed"]
)
async def test_mark_awaits_recheck_original_owner(
    monkeypatch: pytest.MonkeyPatch, stage: str, fault: str
) -> None:
    h, adapter, double = await granted(monkeypatch)
    await prepare_mark(h, adapter, monkeypatch)
    reads = 0

    async def read() -> TimingEvidenceRead:
        nonlocal reads
        reads += 1
        if reads == (1 if stage == "pre" else 2):
            change(h, adapter, fault)
        return TimingEvidenceRead(
            TimingEvidenceReadOutcome.EVIDENCE, record=timing(post=reads == 2)
        )

    monkeypatch.setattr(adapter, "read_timing_evidence", read)
    await h.controller.operator_mark_first_crack()
    assert not h.controller._canonical_human_fc_confirmed
    assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert sends(double) == ([] if stage == "pre" else ["mark_first_crack"])
    await h.controller.operator_mark_first_crack()
    assert len(double.calls) <= 1


async def test_urgent_stale_recovery_permanently_ends_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, adapter, double = await granted(monkeypatch)
    await prepare_mark(h, adapter, monkeypatch)
    h.clock.advance(30)
    h.actuator.status = 1  # Repeat the last accepted counter, now expired.
    await h.turn(bean=180)
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.controller._private_pre_fc is not None
    assert h.controller._private_pre_fc.state is PermitState.ENDED
    await h.turn(bean=181)
    assert sends(double) == []
    assert not await h.controller.human_operator_continue(RoastPhase.PREHEATING)


@pytest.mark.parametrize("ambient", [12.0, 22.0])
async def test_default_matched_room_forecast_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ambient: float
) -> None:
    """Matched synthetic inputs expose forecast dependence without nominal tuning."""

    async def run_trace(step: float, final_eta: float) -> list[dict[str, Any]]:
        h, adapter, double = await granted(monkeypatch)
        reader = h.actuator.read_human_evidence
        ror = 15.0

        async def fresh_read() -> Any:
            evidence = await reader()
            assert evidence.telemetry is not None
            return replace(
                evidence,
                telemetry=evidence.telemetry.model_copy(
                    update={"ambient_temp_c": ambient, "bean_ror_c_per_min": ror}
                ),
            )

        monkeypatch.setattr(h.actuator, "read_human_evidence", fresh_read)

        def acknowledge() -> None:
            name, arguments = double.calls[-1]
            assert arguments is not None
            raw = control_receipt()
            raw.update(
                heat_level_percent=h.controller._current_heat,
                fan_level_percent=h.controller._current_fan,
            )
            raw.update(arguments)
            double.replies[name] = _hs_raw(raw)

        double.during.update(set_heat=acknowledge, set_fan=acknowledge)
        trace: list[dict[str, Any]] = []
        for tick in range(36):
            bt = 146.0 + 0.5 * tick if tick <= 31 else 161.5 + step * (tick - 31)
            ror = 15.0 if tick <= 31 else step * 30.0
            if tick:
                h.clock.advance(1.75)  # h.turn adds the remaining 0.25 seconds.
            previous = len(double.calls)
            await h.turn(bean=bt, t0=True)
            trace.append(
                dict(
                    tick=tick,
                    room_c=ambient,
                    bt_c=bt,
                    et_c=200.0,
                    ror_c_per_min=ror,
                    forecast_s=h.controller._first_crack_eta_seconds(),
                    heat=h.controller._current_heat,
                    fan=h.controller._current_fan,
                    latch=h.controller._trim_latched,
                    sends=sends(double)[previous:],
                )
            )
        trim_config = h.controller._config.pre_first_crack_levers.late_maillard_trim
        assert not trim_config.adaptive_depth_enabled
        assert trace[31]["bt_c"] == 161.5 and trace[31]["forecast_s"] == pytest.approx(58.0)
        assert trace[31]["heat"] == 65 and trace[31]["fan"] == 30
        assert trace[31]["sends"] == ["set_heat", "set_fan"]
        assert trace[35]["forecast_s"] == pytest.approx(final_eta)
        assert all(
            item["sends"] == [] and item["heat"] == 65 and item["fan"] == 30 for item in trace[32:]
        )
        assert all(item["latch"] for item in trace[31:])
        assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
        assert h.controller._development_percent() is None
        assert len(h.controller._history.curve_window()) > 0
        # Mark exits levers; the synthetic commanded heat remains 65 until independent action.
        await prepare_mark(h, adapter, monkeypatch)
        await h.controller.operator_mark_first_crack()
        assert h.controller.phase is RoastPhase.DEVELOPMENT
        before = sends(double)
        await h.turn(bean=180, t0=True)
        assert sends(double) == before
        assert (h.controller._current_heat, h.controller._current_fan) == (65, 30)
        return trace

    traces = {
        "cool_approach": await run_trace(29.0 / 274.0, 266.0),
        "warm_approach": await run_trace(29.0 / 22.7, 14.7),
    }
    assert traces["cool_approach"][:32] == traces["warm_approach"][:32]
    (tmp_path / "synthetic-forecast-traces.json").write_text(json.dumps(traces, indent=2))


@pytest.mark.parametrize("name", ["set_heat", "set_fan", "mark_first_crack"])
@pytest.mark.parametrize("fault", ["none", "is_error", "raw_type", "duplicate", "divergence"])
async def test_actual_sdk_receipts_without_implicit_discovery(name: Any, fault: str) -> None:
    """Exercise strict send_request over an in-memory JSON-RPC peer, no provider."""
    from pydantic import ValidationError

    from roastpilot_agent.mcp_client import MCPConnectionError

    peer = _BootstrapPeer()
    raw = mark_receipt() if name == "mark_first_crack" else control_receipt()
    if fault == "raw_type":
        raw["event_count" if name == "mark_first_crack" else "heat_level_percent"] = True
    wire = _hs_raw(raw).model_dump(by_alias=True, exclude_none=True)
    if fault == "is_error":
        wire["isError"] = 0
    elif fault == "duplicate":
        wire["content"][0]["text"] = '{"session_id":"duplicate",' + wire["content"][0]["text"][1:]
    elif fault == "divergence":
        wire["content"][0]["text"] = wire["content"][0]["text"].replace("a" * 32, "b" * 32)
    peer.results[name] = wire
    async with _sdk_session(peer) as session:
        process = _bs_process(session)
        adapter = RoasterControlAdapter.from_process(process)
        # This isolated decoder witness supplies an already-started synthetic identity.
        adapter._started_session_id = "a" * 32
        adapter._started_child_epoch = process.child_epoch
        value = None if name == "mark_first_crack" else (65 if name == "set_heat" else 30)
        if fault == "none":
            result = await adapter._private_pre_fc_call(name, value)
            assert result.session_id == "a" * 32
        else:
            with pytest.raises((MCPConnectionError, ValidationError)):
                await adapter._private_pre_fc_call(name, value)
    assert len(peer.requests) == 1 and peer.requests[0][0] == "tools/call"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("drop_beans", None),
        ("emergency_stop", None),
        ("set_heat", True),
        ("set_fan", 30.0),
        ("set_heat", -1),
        ("mark_first_crack", 1),
    ],
)
async def test_private_adapter_capability_is_runtime_closed(
    monkeypatch: pytest.MonkeyPatch, name: Any, value: Any
) -> None:
    _, adapter, double = await granted(monkeypatch)
    with pytest.raises(ValueError):
        await adapter._private_pre_fc_call(name, value)
    assert double.calls == []


@pytest.mark.parametrize("identity", ["session", "child", "sdk-session", "transport"])
async def test_private_adapter_missing_identity_refuses_before_send(
    monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    _, adapter, double = await granted(monkeypatch)
    assert adapter._fixed_zero_transport is not None
    if identity == "session":
        adapter._started_session_id = None
    elif identity == "child":
        adapter._started_child_epoch = None
    elif identity == "sdk-session":
        adapter._fixed_zero_transport._session = None
    else:
        adapter._fixed_zero_transport = None
    with pytest.raises(MCPConnectionError, match="identity unavailable"):
        await adapter._private_pre_fc_call("set_heat", 65)
    assert sends(double) == []


@pytest.mark.parametrize("bean", [196.0, 196.1])
@pytest.mark.parametrize(
    "mark", ["unmarked", "canonical", "withheld", "unresolved", "actuator_unknown"]
)
async def test_inclusive_protection_after_mark_disposition(
    monkeypatch: pytest.MonkeyPatch, bean: float, mark: str
) -> None:
    h, adapter, double = await granted(monkeypatch)
    await prepare_mark(h, adapter, monkeypatch)
    if mark == "withheld":
        monkeypatch.setattr(
            adapter,
            "read_timing_evidence",
            AsyncMock(return_value=TimingEvidenceRead(TimingEvidenceReadOutcome.NOT_ATTEMPTED)),
        )
    elif mark == "unresolved":
        double.replies["mark_first_crack"] = RuntimeError("lost")
    elif mark == "actuator_unknown":
        double.replies["set_heat"] = RuntimeError("lost")
        await h.controller._execute_targets(evaluation())
    if mark == "unmarked":
        for name in ("set_heat", "set_fan"):
            double.replies[name] = _hs_raw({**control_receipt(), "heat_level_percent": 100})
    else:
        await h.controller.operator_mark_first_crack()
    await h.turn(bean=180, t0=True)
    lever_sends = sends(double)
    await h.turn(bean=bean, t0=True)
    assert sends(double) == lever_sends
    if mark == "actuator_unknown":
        assert "drop_beans" not in h.commands and h.actuator.containments == 0
        assert run_of(h).command is CommandDisposition.UNKNOWN
    else:
        assert h.commands == ["drop_beans"]
        assert h.controller.phase is RoastPhase.COOLING
    assert await h.controller.operator_emergency_stop()


async def enable_discovery(monkeypatch: pytest.MonkeyPatch, double: _BootstrapDouble) -> AsyncMock:
    """Supply the exact opted-in inventory and discover the strict timing tool."""
    server = {
        **_BS_SERVER_INFO,
        "available_bootstrap_tools": [
            *BOOTSTRAP_TOOL_INVENTORY[:9],
            "drop_beans_exact_session",
            *BOOTSTRAP_TOOL_INVENTORY[9:],
        ],
    }
    double.replies["get_server_info"] = CallToolResult.model_validate(
        _bs_raw(BootstrapRead.SERVER_INFO, server)
    )
    discovery = AsyncMock(
        return_value=ListToolsResult(tools=[Tool(name="get_timing_evidence", inputSchema=schema())])
    )
    monkeypatch.setattr(double, "list_tools", discovery, raising=False)
    return discovery


async def test_opted_start_grants_after_durable_readback(
    fixture: tuple[RoastService, _BootstrapDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, double = fixture
    discovery = await enable_discovery(monkeypatch, double)
    original = service._store.read_run

    async def read(run_id: str) -> Any:
        assert service.runner is not None
        controller = service.runner.controller_for_human_fault_drop()
        assert controller is not None and controller._private_pre_fc is None
        return await original(run_id)

    monkeypatch.setattr(service._store, "read_run", read)
    await service._start_mock_human_roast(PROFILE, pre_fc_mock=True)
    assert discovery.await_count == 1 and service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None and controller._private_pre_fc is not None
    assert controller._private_pre_fc_current()
    assert sends(double) == [
        "get_server_info",
        "get_runtime_config",
        "get_capture_evidence",
        "set_recording_metadata",
        "start_roast_session",
    ]


@pytest.mark.parametrize("fault", ["legacy", "reordered", "extra", "missing", "divergent"])
async def test_opted_inventory_mismatch_never_starts(
    fixture: tuple[RoastService, _BootstrapDouble], monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    service, double = fixture
    await enable_discovery(monkeypatch, double)
    tools = [
        *BOOTSTRAP_TOOL_INVENTORY[:9],
        "drop_beans_exact_session",
        *BOOTSTRAP_TOOL_INVENTORY[9:],
    ]
    text_tools = tools.copy()
    if fault == "legacy":
        tools = list(BOOTSTRAP_TOOL_INVENTORY)
    elif fault == "reordered":
        tools[8], tools[9] = tools[9], tools[8]
    elif fault == "extra":
        tools.append("unexpected")
    elif fault == "missing":
        tools.pop()
    else:
        text_tools = list(BOOTSTRAP_TOOL_INVENTORY)
    server = {**_BS_SERVER_INFO, "available_bootstrap_tools": tools}
    text = {**server, "available_bootstrap_tools": text_tools} if fault == "divergent" else server
    double.replies["get_server_info"] = CallToolResult.model_validate(
        _bs_raw(BootstrapRead.SERVER_INFO, server, text_value=text)
    )
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE, pre_fc_mock=True)
    assert sends(double) == ["get_server_info"]
    assert not service._mock_start_used
    assert service._mcp is not None and service._mcp._start_proof is None


async def test_fresh_service_start_preserves_synthetic_closed_history(
    fixture: tuple[RoastService, _BootstrapDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise store closure and fresh clearance; never reset one-shot owners."""
    service, double = fixture
    await enable_discovery(monkeypatch, double)
    old = await service._start_mock_human_roast(PROFILE, pre_fc_mock=True)
    assert service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None
    store = service._store
    # Seed explicit synthetic historical facts through the store's public writer API.
    await store.record_telemetry(
        run_id=old.id,
        tick=1,
        agent_phase=RoastPhase.DEVELOPMENT,
        elapsed_seconds=3.0,
        interval_seconds=1.0,
        telemetry=reading(),
        mcp_phase="development",
    )
    await store.record_event(
        run_id=old.id,
        kind=RoastEventKind.FIRST_CRACK,
        source=RoastEventSource.OPERATOR,
        monotonic_seconds=2.0,
        payload={"outcome": "canonical", "synthetic": True},
    )
    await store.complete_run(run_id=old.id, outcome="completed", agent_phase=RoastPhase.COMPLETE)
    controller.note_human_run_durably_closed()
    old_detail = await store.read_run(old.id)
    old_timeline = await store.read_timeline(old.id)
    old_points = await store.read_telemetry_points(old.id)
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE, pre_fc_mock=True)
    assert not controller._private_pre_fc_current()

    second = _BootstrapDouble()
    payload = _hs_payload()
    cast("dict[str, object]", payload["session"])["session_id"] = "b" * 32
    second.replies["start_roast_session"] = _hs_raw(payload)
    second.replies["set_recording_metadata"] = _hs_raw(
        {"origin": "ethiopia-harness", "roast_num": 2}
    )
    process = _bs_process(second)
    successor = RoastService(
        store, mcp=process, authority=ControlAuthority.HUMAN, run_loop=False, _mock_human_start=True
    )
    await enable_discovery(monkeypatch, second)
    try:
        initialized = await successor.initialize_missing_fault_control_safety_record(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert initialized.outcome == "confirmed"
        cleared = await successor.acknowledge_restart_clearance(
            RestartClearanceRequest(physical_confirmation=True)
        )
        assert cleared.cleared
        new = await successor._start_mock_human_roast(PROFILE, pre_fc_mock=True)
        assert new.id != old.id and successor.instance_id != service.instance_id
        assert successor.runner is not None
        current = successor.runner.controller_for_human_fault_drop()
        assert current is not None and current._private_pre_fc_current()
        assert current._known_human_identity() == ("b" * 32, process.child_epoch)
        assert current._history.curve_window() == []
        assert not current._canonical_human_fc_confirmed
        assert (current._current_heat, current._current_fan) == (0, 0)
        assert await store.read_run(old.id) == old_detail
        assert await store.read_timeline(old.id) == old_timeline
        assert await store.read_telemetry_points(old.id) == old_points
    finally:
        await successor.shutdown()


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
async def test_bad_opt_in_fails_before_entry(
    fixture: tuple[RoastService, _BootstrapDouble], value: Any
) -> None:
    service, double = fixture
    with pytest.raises(ValueError):
        await service._start_mock_human_roast(PROFILE, pre_fc_mock=value)
    assert not service._mock_start_used and double.calls == []


@pytest.mark.parametrize("fault", ["absent", "displaced", "cached"])
async def test_timing_discovery_refuses_without_spending_start(
    fixture: tuple[RoastService, _BootstrapDouble], monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    service, double = fixture
    assert service._mcp is not None
    if fault == "displaced":
        discovery = await enable_discovery(monkeypatch, double)

        def displace() -> ListToolsResult:
            assert service._mcp is not None
            service._mcp._child_epoch += 1
            return ListToolsResult(tools=[Tool(name="get_timing_evidence", inputSchema=schema())])

        discovery.side_effect = displace
    elif fault == "cached":
        service._mcp._timing_evidence_discovery = ToolDiscovery.ABSENT
    with pytest.raises(RoastRunConflictError):
        await service._start_mock_human_roast(PROFILE, pre_fc_mock=True)
    assert not service._mock_start_used and double.calls == []


@pytest.mark.parametrize(
    "fault", ["settle", "history", "phase", "readback", "readback-phase", "outcome", "end", "id"]
)
async def test_failed_start_settlement_never_grants(
    fixture: tuple[RoastService, _BootstrapDouble], monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    service, double = fixture
    await enable_discovery(monkeypatch, double)
    original = service._build_runner

    async def build(*args: Any, **kwargs: Any) -> Any:
        runner = await original(*args, **kwargs)
        assert runner is not None
        if fault == "history":
            monkeypatch.setattr(runner, "_flush_events", AsyncMock(return_value=False))
        elif fault == "phase":
            monkeypatch.setattr(
                runner, "_persist_phase_if_changed", AsyncMock(side_effect=RuntimeError("phase"))
            )
        return runner

    monkeypatch.setattr(service, "_build_runner", build)
    if fault == "settle":
        monkeypatch.setattr(
            service._store,
            "settle_start_attempt_hold",
            AsyncMock(return_value=StartHoldSettlementOutcome.INDETERMINATE),
        )
    elif fault == "readback":
        monkeypatch.setattr(service._store, "read_run", AsyncMock(return_value=None))
    elif fault in ("readback-phase", "outcome", "end", "id"):
        original_read = service._store.read_run

        async def inconsistent(run_id: str) -> Any:
            detail = await original_read(run_id)
            assert detail is not None
            cases: dict[str, dict[str, object]] = {
                "readback-phase": {"agent_phase": RoastPhase.DEVELOPMENT},
                "outcome": {"outcome": "completed"},
                "end": {"completed_at_utc": "2026-11-22T00:00:00+00:00"},
                "id": {"id": "wrong-run"},
            }
            return detail.model_copy(update=cases[fault])

        monkeypatch.setattr(service._store, "read_run", inconsistent)
    with pytest.raises((RoastRunConflictError, RuntimeError)):
        await service._start_mock_human_roast(PROFILE, pre_fc_mock=True)
    assert service.runner is not None
    controller = service.runner.controller_for_human_fault_drop()
    assert controller is not None and controller._private_pre_fc is None
    assert sends(double).count("start_roast_session") == 1
    before = sends(double)
    assert not await controller._execute_targets(evaluation())
    assert sends(double) == before
