# pyright: reportPrivateUsage=false
"""Exact private mock receipt binding and SESSION_BOUND dispatch witnesses."""

import ast
import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from roastpilot_agent.advisor import FakeAdvisor
from roastpilot_agent.human_bootstrap import HumanStartAccepted, StartAttemptState
from roastpilot_agent.human_protection import (
    CommandDisposition,
    EndpointDisposition,
    EndpointOwner,
    HumanReadEvidence,
    WriteClass,
)
from roastpilot_agent.human_run_ownership import (
    AdmissionCloseReason,
    HumanRunAdmission,
    HumanSessionBinding,
    PendingHumanCall,
)
from roastpilot_agent.mcp_client import MCPServerProcess, RoasterControlAdapter
from roastpilot_agent.models import RoastEventKind, RoastPhase
from roastpilot_agent.safety import SafetyVerdict
from tests.test_controller import PROFILE, reading
from tests.test_controller_human_mode import HumanHarness
from tests.test_human_pre_admission_fence import fresh_boundary, prepared, run_of
from tests.test_mcp_client import _hs_attempt, _hs_raw, _hs_ready

pytestmark = pytest.mark.asyncio


async def setup(
    monkeypatch: pytest.MonkeyPatch, *, prepare: bool = True
) -> tuple[HumanHarness, RoasterControlAdapter, MCPServerProcess, HumanStartAccepted]:
    """Use real private issuance, with synthetic SDK and controller transport ports."""
    process, _, evidence = await _hs_ready()
    adapter = RoasterControlAdapter.from_process(process)
    h = prepared()
    h.controller._executor = adapter
    h.controller._current_session_id = adapter._human_session_id
    h.controller._current_child_generation = adapter._human_child_epoch
    h.controller._containment_discovered = lambda: True
    h.controller._config = h.controller._config.model_copy(update={"t0_debounce_ticks": 1})
    h.actuator.epoch = process.child_epoch
    for name in (
        "drop_beans",
        "start_cooling",
        "stop_cooling",
        "emergency_stop",
        "set_heat",
        "set_fan",
        "contain_heat_fixed_zero",
    ):
        monkeypatch.setattr(adapter, name, getattr(h.actuator, name))
    if prepare:
        assert h.controller._prepare_human_session(adapter, PROFILE)
    receipt = await _hs_attempt(process, evidence)
    assert isinstance(receipt, HumanStartAccepted)
    return h, adapter, process, receipt


async def bound(monkeypatch: pytest.MonkeyPatch) -> tuple[HumanHarness, RoasterControlAdapter]:
    """Return the same owner and arbiter after successful private binding."""
    h, adapter, _, receipt = await setup(monkeypatch)
    assert h.controller._bind_prepared_human_session(adapter, receipt) is HumanSessionBinding.BOUND
    assert h.controller._known_human_identity() == (receipt.session_id, receipt.child_epoch)
    assert h.controller.owns_confirmed_human_start()
    return h, adapter


async def test_atomic_binding_no_initial_evaluation_or_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, adapter, _, receipt = await setup(monkeypatch)
    run = run_of(h)
    arbiter = h.arbiter
    before = h.emitter.emit

    def observe(kind: RoastEventKind, payload: object) -> None:
        if kind in (RoastEventKind.PHASE_CHANGED, RoastEventKind.RUN_STARTED):
            assert run is run_of(h)
            assert h.arbiter is arbiter
            assert run.admission is HumanRunAdmission.SESSION_BOUND
            assert h.controller._known_human_identity() == (receipt.session_id, receipt.child_epoch)
            assert h.controller._human_confirmed_start_generation == arbiter.generation
        before(kind, payload)

    monkeypatch.setattr(h.emitter, "emit", observe)
    evaluate = Mock(side_effect=AssertionError("initial heat/fan evaluated"))
    monkeypatch.setattr(h.controller._safety, "evaluate_command", evaluate)
    assert h.controller._bind_prepared_human_session(adapter, receipt) is HumanSessionBinding.BOUND
    assert h.controller.phase is RoastPhase.PREHEATING
    assert h.commands == h.sink.evaluations == []
    assert h.emitter.events[-1] == (RoastEventKind.RUN_STARTED, {"profile": PROFILE.name})
    assert (
        h.controller._bind_prepared_human_session(adapter, receipt)
        is HumanSessionBinding.ENTERED_UNBOUND
    )
    assert not h.controller._prepare_human_session(adapter, PROFILE)
    assert not evaluate.called


@pytest.mark.parametrize("variant", ["copied", "fabricated", "foreign", "none"])
async def test_non_issued_objects_never_spend_genuine(
    monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    h, adapter, process, receipt = await setup(monkeypatch)
    other, _, evidence = await _hs_ready()
    foreign = await _hs_attempt(other, evidence)
    candidates = {
        "copied": replace(receipt),
        "fabricated": HumanStartAccepted("b" * 32, receipt.child_epoch),
        "foreign": foreign,
        "none": None,
    }
    assert (
        h.controller._bind_prepared_human_session(adapter, candidates[variant])
        is HumanSessionBinding.NO_ISSUED_RECEIPT
    )
    assert not process._human_start_receipt_spent
    assert h.controller._bind_prepared_human_session(adapter, receipt) is HumanSessionBinding.BOUND


@pytest.mark.parametrize(
    "reason",
    [
        "generation",
        "emergency_active",
        "pending_emergency",
        "phase",
        "edge",
        "owner",
        "read",
        "containment",
        "close",
        "closed",
        "teardown",
        "command",
        "endpoint",
        "durable",
        "containment_used",
        "run_epoch",
        "adapter_reset",
        "adapter_session",
        "adapter_epoch",
        "adapter_state",
        "transport",
        "transport_callback",
        "executor",
        "proof",
        "epoch",
        "callback",
        "session_callback",
        "epoch_callback",
        "loop",
        "frame",
        "run",
    ],
)
async def test_genuine_refusal_is_terminal_entered_unbound(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    h, adapter, process, receipt = await setup(monkeypatch)
    run = run_of(h)
    if reason == "generation":
        h.arbiter.admit_emergency_stop()
        await h.arbiter.cancel_emergency_stop_admission()
    elif reason == "emergency_active":
        h.arbiter._pending_emergency_stops = 1
    elif reason == "pending_emergency":
        h.controller._emergency_stop_transition_pending = True
    elif reason == "phase":
        h.controller.transition_to(RoastPhase.FAULTED)
    elif reason == "edge":
        monkeypatch.setattr(h.controller, "can_transition", Mock(return_value=False))
    elif reason == "owner":
        h.ownership._run = replace(run)
    elif reason in ("read", "containment"):
        h.ownership._pending.add(
            PendingHumanCall.READ if reason == "read" else PendingHumanCall.CONTAINMENT
        )
    elif reason == "close":
        assert h.controller.reserve_fault_acknowledgement_close(h.arbiter.generation)
    elif reason == "closed":
        h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    elif reason == "teardown":
        h.controller._teardown_unconfirmed = lambda: True
    elif reason == "command":
        run.command = CommandDisposition.UNKNOWN
    elif reason == "endpoint":
        run.endpoint = EndpointDisposition.ADMITTED
    elif reason == "durable":
        run.durably_closed = True
    elif reason == "containment_used":
        from roastpilot_agent.human_protection import ContainmentAttempt

        run.containment = ContainmentAttempt.CONSUMED
    elif reason == "run_epoch":
        run.child_generation = 9
    elif reason == "adapter_reset":
        adapter.reset_session_identity_for_new_run()
    elif reason == "adapter_session":
        adapter._started_session_id = "wrong"
    elif reason == "adapter_epoch":
        adapter._started_child_epoch = 9
    elif reason == "adapter_state":
        monkeypatch.setattr(type(adapter), "last_state", property(lambda _: object()))
    elif reason == "transport":
        # A different adapter cannot consume the real issuer's object.
        alternate = RoasterControlAdapter.from_process(process)
        adapter = alternate
    elif reason == "transport_callback":
        other, _, _ = await _hs_ready()

        def displace_transport() -> bool:
            adapter._fixed_zero_transport = other
            return False

        h.controller._teardown_unconfirmed = displace_transport
    elif reason == "executor":
        h.controller._executor = h.actuator
    elif reason == "proof":
        process._start_proof = None
    elif reason == "epoch":
        process._replace_session(None)
    elif reason == "callback":
        h.controller._teardown_unconfirmed = Mock(side_effect=ValueError("callback"))
    elif reason == "session_callback":
        h.controller._current_session_id = None
    elif reason == "epoch_callback":
        h.controller._current_child_generation = lambda: -1
    elif reason == "loop":
        frame = h.controller._prepared_human_session
        assert frame is not None
        # Closed stand-in loop proves same-loop identity without running another loop.
        loop = asyncio.new_event_loop()
        loop.close()
        h.controller._prepared_human_session = replace(frame, loop=loop)
    elif reason == "frame":
        h.controller._prepared_human_session = None
    else:
        h.controller._human_run = None
    old_identity = adapter._identity_snapshot()
    old_command = run.command
    assert (
        h.controller._bind_prepared_human_session(adapter, receipt)
        is HumanSessionBinding.ENTERED_UNBOUND
    )
    assert process._human_start_receipt_spent
    if reason == "transport_callback":
        assert adapter._started_session_id is adapter._started_child_epoch is None
    else:
        assert adapter._identity_snapshot() == old_identity
    assert run.admission is HumanRunAdmission.PREPARED
    assert run.session_id is None
    assert run.command is old_command
    assert not h.controller.owns_confirmed_human_start()
    adapter._fixed_zero_transport = process
    assert (
        h.controller._bind_prepared_human_session(adapter, receipt)
        is HumanSessionBinding.ENTERED_UNBOUND
    )


@pytest.mark.parametrize("state", list(StartAttemptState))
async def test_preparation_requires_unattempted_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch, state: StartAttemptState
) -> None:
    h, adapter, process, _ = await setup(monkeypatch, prepare=False)
    process._human_start_attempt = state
    snapshot = h.controller.snapshot()
    identity = adapter._identity_snapshot()
    if state is StartAttemptState.UNATTEMPTED:
        assert h.controller._prepare_human_session(adapter, PROFILE)
    else:
        assert not h.controller._prepare_human_session(adapter, PROFILE)
        assert h.controller.snapshot() == snapshot
        assert adapter._identity_snapshot() == identity
        assert not run_of(h)._session_handoff_prepared
        assert h.emitter.events == []


async def test_receipt_before_frame_and_emergency_ack_cannot_rearm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, adapter, _, receipt = await setup(monkeypatch, prepare=False)
    assert not h.controller._prepare_human_session(adapter, PROFILE)
    assert (
        h.controller._bind_prepared_human_session(adapter, receipt)
        is HumanSessionBinding.ENTERED_UNBOUND
    )
    assert h.controller.phase is RoastPhase.IDLE
    h, adapter, _, receipt = await setup(monkeypatch)
    assert await h.controller.operator_emergency_stop()
    h.controller.note_fault_acknowledged()
    h.controller.transition_to(RoastPhase.IDLE)
    assert not h.controller._prepare_human_session(adapter, PROFILE)
    assert (
        h.controller._bind_prepared_human_session(adapter, receipt)
        is HumanSessionBinding.ENTERED_UNBOUND
    )
    assert run_of(h).session_id is None
    assert h.commands == ["emergency_stop"]


@pytest.mark.parametrize("owner", list(EndpointOwner))
@pytest.mark.parametrize("kind", list(WriteClass))
async def test_session_bound_dispatch_classes(
    monkeypatch: pytest.MonkeyPatch, owner: EndpointOwner, kind: WriteClass
) -> None:
    h, _ = await bound(monkeypatch)
    fresh_boundary(h)
    run = run_of(h)
    run.endpoint = EndpointDisposition.ADMITTED
    run.endpoint_owner = owner
    expected = kind is WriteClass.CONTAINMENT or (
        kind is WriteClass.DROP and owner is not EndpointOwner.OPERATOR
    )
    result = await h.controller._run_routine_write(
        h.arbiter.generation, h.actuator.start_cooling, write_class=kind, endpoint_owner=owner
    )
    assert result == (expected, None)
    assert h.commands == (["start_cooling"] if expected else [])


@pytest.mark.parametrize(
    "invalidation", ["generation", "owner", "closed", "identity", "unknown", "stale"]
)
async def test_protective_dispatch_rechecks_after_actual_arbiter_wait(
    monkeypatch: pytest.MonkeyPatch, invalidation: str
) -> None:
    h, adapter = await bound(monkeypatch)
    fresh_boundary(h)
    run = run_of(h)
    run.endpoint = EndpointDisposition.ADMITTED
    run.endpoint_owner = EndpointOwner.PROTECTION
    entered, release = asyncio.Event(), asyncio.Event()

    async def occupy() -> None:
        entered.set()
        await release.wait()

    blocker = asyncio.create_task(h.arbiter.run_routine(h.arbiter.generation, occupy))
    await asyncio.wait_for(entered.wait(), 1)
    candidate = asyncio.create_task(
        h.controller._run_routine_write(
            h.arbiter.generation,
            h.actuator.drop_beans,
            write_class=WriteClass.DROP,
            endpoint_owner=EndpointOwner.PROTECTION,
        )
    )
    await asyncio.sleep(0)
    assert not candidate.done()
    if invalidation == "generation":
        h.arbiter.admit_emergency_stop()
        await h.arbiter.cancel_emergency_stop_admission()
    elif invalidation == "owner":
        h.ownership._run = replace(run)
    elif invalidation == "closed":
        h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    elif invalidation == "identity":
        adapter.reset_session_identity_for_new_run()
    elif invalidation == "unknown":
        run.command = CommandDisposition.UNKNOWN
    else:
        h.clock.advance(30)
    release.set()
    await blocker
    assert await candidate == (False, None)
    assert h.commands == []


@pytest.mark.parametrize("bean", [196.0, 196.1])
@pytest.mark.parametrize("phase", [RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.DEVELOPMENT])
async def test_real_tick_preserves_protection_without_routine_evaluation(
    monkeypatch: pytest.MonkeyPatch, bean: float, phase: RoastPhase
) -> None:
    h, _ = await bound(monkeypatch)
    advisor = FakeAdvisor()
    h.controller._advisor = advisor
    evaluate = Mock(side_effect=AssertionError("routine command evaluation"))
    monkeypatch.setattr(h.controller._safety, "evaluate_command", evaluate)
    await h.turn(bean=180)
    await h.turn(bean=183, t0=True)
    if phase is RoastPhase.DEVELOPMENT:
        h.controller.transition_to(phase)
    await h.turn(bean=195.9)
    assert h.controller.phase is phase
    assert h.commands == []
    assert not advisor.contexts
    assert h.sink.advisor_decisions == []
    assert not evaluate.called
    h.log.clear()
    await h.turn(bean=bean)
    assert h.commands == ["drop_beans"]
    assert h.controller.phase is RoastPhase.COOLING
    assert h.log.index("drop_beans") < h.log.index("persist_snapshot")
    assert any(
        e.rule == "human_protective_drop" and e.verdict is SafetyVerdict.ALLOW
        for e in h.sink.evaluations
    )
    assert not evaluate.called
    assert not advisor.contexts
    before = len(h.sink.evaluations)
    await h.controller.operator_start_cooling()
    await h.controller.operator_stop_cooling()
    assert len(h.sink.evaluations) == before
    assert h.commands == ["drop_beans"]
    assert await h.controller.operator_emergency_stop()
    assert h.commands[-1] == "emergency_stop"


async def test_active_continuation_and_ordinary_origins_refuse_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, _ = await bound(monkeypatch)
    before = len(h.sink.evaluations)
    for action in (
        h.controller.operator_mark_first_crack,
        h.controller.operator_mark_beans_added,
        h.controller.operator_drop_beans,
    ):
        await action()
    assert h.commands == []
    assert len(h.sink.evaluations) == before
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    for phase in (
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
        RoastPhase.DEVELOPMENT,
    ):
        assert not await h.controller.human_operator_continue(phase)
    assert len(h.sink.evaluations) == before
    assert await h.controller.human_operator_continue(RoastPhase.COOLING)


async def test_model_event_does_not_set_operational_fc_or_dtr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, _ = await bound(monkeypatch)
    await h.turn(bean=180)
    await h.turn(bean=183, t0=True)
    original = h.actuator.read_human_evidence

    async def model_fc() -> HumanReadEvidence:
        evidence = await original()
        return replace(evidence, telemetry=reading(190, 200, first_crack_detected=True))

    # Keep the existing HUMAN event-source path; only the input signal changes.
    monkeypatch.setattr(h.actuator, "read_human_evidence", model_fc)
    await h.turn(bean=190)
    assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert not h.controller._canonical_human_fc_confirmed
    assert h.controller._development_time_ratio() is None


async def test_no_production_caller_of_private_handoff() -> None:
    root = Path(__file__).parents[1] / "src"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        assert not any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"_prepare_human_session", "_bind_prepared_human_session"}
            for n in ast.walk(tree)
        )


@pytest.mark.parametrize("origin", ["fault_cooling", "d212"])
@pytest.mark.parametrize("reservation", ["current", "wrong", "stale"])
async def test_bound_fault_and_close_non_heat_guards(
    monkeypatch: pytest.MonkeyPatch, origin: str, reservation: str
) -> None:
    h, _ = await bound(monkeypatch)
    assert await h.controller.operator_emergency_stop()
    h.actuator.calls.clear()
    generation = h.arbiter.generation
    token = generation
    if origin == "d212":
        assert h.controller.reserve_fault_acknowledgement_close(generation)
        if reservation == "wrong":
            token += 1
        elif reservation == "stale":
            h.controller.release_fault_acknowledgement_close(generation)
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    h.controller._teardown_unconfirmed = lambda: True
    if origin == "fault_cooling":
        await h.controller.operator_start_cooling()
        await h.controller.operator_stop_cooling()
        assert h.commands == ["start_cooling", "stop_cooling"]
    else:
        dispatched, _ = await h.controller.execute_fault_acknowledgement_stop(
            generation,
            h.actuator.stop_cooling,
            close_reservation=token,
            write_class=WriteClass.NON_HEAT,
        )
        assert dispatched is (reservation == "current")
        assert h.commands == (["stop_cooling"] if reservation == "current" else [])
    assert h.controller.phase is RoastPhase.FAULTED
    h.actuator.calls.clear()
    run_of(h).command = CommandDisposition.UNKNOWN
    await h.controller.operator_start_cooling()
    await h.controller.operator_stop_cooling()
    assert h.commands == []


@pytest.mark.parametrize("owner", list(EndpointOwner))
async def test_bound_drop_early_latch_owner_discrimination(
    monkeypatch: pytest.MonkeyPatch, owner: EndpointOwner
) -> None:
    h, _ = await bound(monkeypatch)
    await h.turn(bean=180)
    await h.turn(bean=183, t0=True)
    if owner in (EndpointOwner.OPERATOR_RECOVERY, EndpointOwner.FAULT_CONTROL):
        h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    allowed = owner is not EndpointOwner.OPERATOR
    assert (
        await h.controller._execute_human_drop(owner, None, generation=h.arbiter.generation)
        is allowed
    )
    assert h.commands == (["drop_beans"] if allowed else [])
    assert run_of(h).endpoint is (
        EndpointDisposition.ISSUED if allowed else EndpointDisposition.NONE
    )
    assert run_of(h).endpoint_owner is (owner if allowed else None)


@pytest.mark.parametrize("discovered", [True, False])
async def test_bound_containment_keeps_discovery_and_one_shot(
    monkeypatch: pytest.MonkeyPatch, discovered: bool
) -> None:
    from roastpilot_agent.human_protection import ContainmentOutcome, ContainmentResult

    h, _ = await bound(monkeypatch)
    h.controller._containment_discovered = lambda: discovered

    async def positive() -> ContainmentResult:
        return ContainmentResult(ContainmentOutcome.COMMAND_STATE_RETURNED)

    h.actuator.contain = positive
    result = await h.controller._contain_human_heat(generation=h.arbiter.generation)
    assert result.outcome is (
        ContainmentOutcome.COMMAND_STATE_RETURNED
        if discovered
        else ContainmentOutcome.NOT_ATTEMPTED
    )
    assert h.actuator.containments == int(discovered)
    assert h.commands == []
    await h.controller._contain_human_heat(generation=h.arbiter.generation)
    assert h.actuator.containments == int(discovered)
    if discovered:
        assert RoastEventKind.COMMAND_EXECUTED in h.emitter.kinds()


@pytest.mark.parametrize("position", ["before_prepare", "sdk", "before_bind", "observer"])
async def test_independent_emergency_all_handoff_positions(
    monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    if position == "before_prepare":
        h, adapter, _, receipt = await setup(monkeypatch, prepare=False)
        assert await h.controller.operator_emergency_stop()
        assert not h.controller._prepare_human_session(adapter, PROFILE)
    else:
        h, adapter, process, receipt = await setup(monkeypatch)
        if position == "sdk":
            # Rebuild a fresh unattempted path; an event barrier pauses its SDK call.
            h = prepared()
            process, double, evidence = await _hs_ready()
            adapter = RoasterControlAdapter.from_process(process)
            h.controller._executor = adapter
            h.controller._current_session_id = adapter._human_session_id
            h.controller._current_child_generation = adapter._human_child_epoch
            monkeypatch.setattr(adapter, "emergency_stop", h.actuator.emergency_stop)
            assert h.controller._prepare_human_session(adapter, PROFILE)
            entered, release = asyncio.Event(), asyncio.Event()
            call = double.call_tool

            async def wait_reply(name: str, arguments: dict[str, object] | None = None) -> object:
                if name == "start_roast_session":
                    entered.set()
                    await release.wait()
                return await call(name, arguments)

            monkeypatch.setattr(double, "call_tool", wait_reply)
            attempt = asyncio.create_task(_hs_attempt(process, evidence))
            await asyncio.wait_for(entered.wait(), 1)
            assert run_of(h).session_id is None
            assert await h.controller.operator_emergency_stop()
            release.set()
            result = await attempt
            assert isinstance(result, HumanStartAccepted)
            receipt = result
        elif position == "observer":
            original = h.emitter.emit

            def stop_observer(kind: RoastEventKind, payload: object) -> None:
                if kind is RoastEventKind.PHASE_CHANGED:
                    assert h.controller._known_human_identity() == (
                        receipt.session_id,
                        receipt.child_epoch,
                    )
                    assert process._human_start_receipt_spent
                    h.arbiter.admit_emergency_stop()
                original(kind, payload)

            monkeypatch.setattr(h.emitter, "emit", stop_observer)
            assert (
                h.controller._bind_prepared_human_session(adapter, receipt)
                is HumanSessionBinding.BOUND
            )
            monkeypatch.setattr(h.emitter, "emit", original)
            await h.arbiter.cancel_emergency_stop_admission()
            assert await h.controller.operator_emergency_stop()
        else:
            assert await h.controller.operator_emergency_stop()
    assert (
        h.controller._bind_prepared_human_session(adapter, receipt)
        is HumanSessionBinding.ENTERED_UNBOUND
    )
    assert h.commands == ["emergency_stop"]
    assert h.controller.phase is RoastPhase.FAULTED
    assert run_of(h).admission is (
        HumanRunAdmission.SESSION_BOUND if position == "observer" else HumanRunAdmission.PREPARED
    )


@pytest.mark.parametrize("callback", ["teardown", "session", "epoch"])
async def test_receipt_spent_before_raising_callback_and_before_callback_revocation(
    monkeypatch: pytest.MonkeyPatch, callback: str
) -> None:
    h, adapter, process, receipt = await setup(monkeypatch)

    def revoke() -> None:
        assert process._human_start_receipt_spent
        assert run_of(h).admission is HumanRunAdmission.PREPARED
        h.arbiter.admit_emergency_stop()

    if callback == "teardown":
        h.controller._teardown_unconfirmed = lambda: (revoke(), False)[1]
    elif callback == "session":
        h.controller._current_session_id = lambda: (revoke(), None)[1]
    else:
        h.controller._current_child_generation = lambda: (revoke(), process.child_epoch)[1]
    assert (
        h.controller._bind_prepared_human_session(adapter, receipt)
        is HumanSessionBinding.ENTERED_UNBOUND
    )
    assert run_of(h).session_id is None
    assert adapter.latest_fault_session_id is None


@pytest.mark.parametrize(
    "failure",
    [
        "run",
        "transport",
        "executor",
        "phase",
        "edge",
        "admission",
        "close",
        "pending",
        "teardown",
        "reset",
    ],
)
async def test_prepare_refuses_without_partial_bookkeeping(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    h, adapter, process, _ = await setup(monkeypatch, prepare=False)
    process._human_start_attempt = StartAttemptState.UNATTEMPTED
    if failure == "run":
        h.controller._human_run = None
    elif failure == "transport":
        adapter._fixed_zero_transport = None
    elif failure == "executor":
        h.controller._executor = h.actuator
    elif failure == "phase":
        h.controller.transition_to(RoastPhase.FAULTED)
    elif failure == "edge":
        monkeypatch.setattr(h.controller, "can_transition", Mock(return_value=False))
    elif failure == "admission":
        run_of(h)._admission = HumanRunAdmission.ADMITTED
    elif failure == "close":
        assert h.controller.reserve_fault_acknowledgement_close(h.arbiter.generation)
    elif failure == "pending":
        h.ownership._pending.add(PendingHumanCall.READ)
    elif failure == "teardown":
        h.controller._teardown_unconfirmed = lambda: True
    else:

        def dirty_reset() -> None:
            adapter._started_session_id = "leftover"

        monkeypatch.setattr(adapter, "reset_session_identity_for_new_run", dirty_reset)
    before = h.controller.snapshot()
    events = list(h.emitter.events)
    assert not h.controller._prepare_human_session(adapter, PROFILE)
    assert h.controller.snapshot() == before
    assert h.controller._profile is None
    assert h.emitter.events == events
    assert h.commands == []


async def test_wrong_transport_never_consumes_original_and_adapter_requires_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, adapter, process, receipt = await setup(monkeypatch)
    other, _, _ = await _hs_ready()
    adapter._fixed_zero_transport = other
    assert (
        h.controller._bind_prepared_human_session(adapter, receipt)
        is HumanSessionBinding.NO_ISSUED_RECEIPT
    )
    assert not process._human_start_receipt_spent
    adapter._fixed_zero_transport = None
    assert not adapter._prepare_human_session_identity()
    assert (
        h.controller._bind_prepared_human_session(adapter, receipt)
        is HumanSessionBinding.NO_ISSUED_RECEIPT
    )
    adapter._fixed_zero_transport = process
    assert h.controller._bind_prepared_human_session(adapter, receipt) is HumanSessionBinding.BOUND


async def test_genuine_refusal_cannot_retry_after_original_conditions_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, adapter, process, receipt = await setup(monkeypatch)
    with h.ownership.hold(PendingHumanCall.READ):
        assert (
            h.controller._bind_prepared_human_session(adapter, receipt)
            is HumanSessionBinding.ENTERED_UNBOUND
        )
    assert process._human_start_receipt_spent
    assert h.controller._prepared_session_conditions(run_of(h), h.arbiter.generation)
    assert (
        h.controller._bind_prepared_human_session(adapter, receipt)
        is HumanSessionBinding.ENTERED_UNBOUND
    )
    assert run_of(h).admission is HumanRunAdmission.PREPARED
    assert run_of(h).session_id is None


@pytest.mark.parametrize("target", [RoastPhase.IDLE, RoastPhase.COMPLETE, RoastPhase.COOLING])
@pytest.mark.parametrize("admission", [HumanRunAdmission.PREPARED, HumanRunAdmission.SESSION_BOUND])
async def test_bound_terminal_continuation_keeps_existing_guards(
    monkeypatch: pytest.MonkeyPatch, target: RoastPhase, admission: HumanRunAdmission
) -> None:
    h = prepared() if admission is HumanRunAdmission.PREPARED else (await bound(monkeypatch))[0]
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    assert await h.controller.human_operator_continue(target)
    assert h.controller.phase is target
    assert run_of(h).admission is admission
    assert not run_of(h).durably_closed
    assert h.sink.evaluations == h.commands == []


@pytest.mark.parametrize("stage", ["prepare", "bind"])
@pytest.mark.parametrize("projection", ["session", "epoch", "other_adapter"])
async def test_detached_matching_projection_refuses_without_publication(
    monkeypatch: pytest.MonkeyPatch, stage: str, projection: str
) -> None:
    h, adapter, process, receipt = await setup(monkeypatch, prepare=stage == "bind")
    if stage == "prepare":
        process._human_start_attempt = StartAttemptState.UNATTEMPTED
    if projection == "session":
        h.controller._current_session_id = lambda: None
    elif projection == "epoch":
        h.controller._current_child_generation = lambda: process.child_epoch
    else:
        other = RoasterControlAdapter.from_process(process)
        h.controller._current_session_id = other._human_session_id
    before = h.controller.snapshot()
    if stage == "prepare":
        assert not h.controller._prepare_human_session(adapter, PROFILE)
        assert h.controller.snapshot() == before
    else:
        assert (
            h.controller._bind_prepared_human_session(adapter, receipt)
            is HumanSessionBinding.ENTERED_UNBOUND
        )
        assert process._human_start_receipt_spent
    assert adapter.latest_fault_session_id is None
    assert run_of(h).session_id is None
    assert run_of(h).admission is HumanRunAdmission.PREPARED
    assert h.commands == h.sink.evaluations == []


async def test_callback_cannot_displace_bound_projection_during_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, adapter, process, receipt = await setup(monkeypatch)

    def displace() -> bool:
        assert process._human_start_receipt_spent
        h.controller._current_session_id = lambda: None
        return False

    h.controller._teardown_unconfirmed = displace
    assert (
        h.controller._bind_prepared_human_session(adapter, receipt)
        is HumanSessionBinding.ENTERED_UNBOUND
    )
    assert run_of(h).session_id is None
    assert adapter.latest_fault_session_id is None


async def test_exact_run_prepared_marker_survives_ack_and_reconstruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, _, _, _ = await setup(monkeypatch)
    run = run_of(h)
    assert await h.controller.operator_emergency_stop()
    assert h.controller.phase is RoastPhase.FAULTED
    h.controller.note_fault_acknowledged()
    h.controller.transition_to(RoastPhase.IDLE)
    # Reconstruct only the controller ports around the untouched exact owner.
    rebuilt = prepared()
    rebuilt.controller._human_run = run
    rebuilt.controller._human_ownership = h.ownership
    rebuilt.controller._arbiter = h.arbiter
    process, _, _ = await _hs_ready()
    adapter = RoasterControlAdapter.from_process(process)
    controller = rebuilt.controller
    controller._executor = adapter
    controller._current_session_id = adapter._human_session_id
    controller._current_child_generation = adapter._human_child_epoch
    assert h.ownership.is_current(run)
    assert run._session_handoff_prepared
    assert controller.phase is RoastPhase.IDLE
    assert controller.can_transition(RoastPhase.STARTING)
    assert controller._prepared_session_conditions(run, h.arbiter.generation)
    assert controller._human_session_projections_bound(adapter)
    assert process._human_start_state is StartAttemptState.UNATTEMPTED
    before = controller.snapshot(), adapter._identity_generation, list(rebuilt.emitter.events)
    assert not controller._prepare_human_session(adapter, PROFILE)
    assert (controller.snapshot(), adapter._identity_generation, rebuilt.emitter.events) == before
    assert controller._prepared_human_session is None
    assert run is run_of(h) and run.session_id is None
    assert rebuilt.sink.evaluations == rebuilt.commands == []


@pytest.mark.parametrize("admission", [HumanRunAdmission.PREPARED, HumanRunAdmission.SESSION_BOUND])
async def test_active_continuation_preserves_prepared_evaluation(
    monkeypatch: pytest.MonkeyPatch, admission: HumanRunAdmission
) -> None:
    h = prepared() if admission is HumanRunAdmission.PREPARED else (await bound(monkeypatch))[0]
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    for target in (
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
        RoastPhase.DEVELOPMENT,
    ):
        before = len(h.sink.evaluations)
        assert not await h.controller.human_operator_continue(target)
        assert len(h.sink.evaluations) == before + (admission is HumanRunAdmission.PREPARED)
        if admission is HumanRunAdmission.PREPARED:
            assert h.sink.evaluations[-1].verdict is SafetyVerdict.REJECT
    assert h.commands == []


@pytest.mark.parametrize("charged", [True, False])
async def test_bound_recovery_projection_matches_actual_fault_dispatch(
    monkeypatch: pytest.MonkeyPatch, charged: bool
) -> None:
    h, _ = await bound(monkeypatch)
    await h.turn(bean=180)
    if charged:
        await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.FAULTED)
    identity = h.controller._known_human_identity()
    assert identity is not None
    assert h.controller.human_recovery_drop_allowed(session_id=identity[0]) is charged
    writes: list[str] = []

    async def write() -> None:
        writes.append("fault_drop")

    assert (
        await h.controller.execute_human_fault_drop(
            identity[0],
            generation=h.arbiter.generation,
            lease_current=lambda: True,
            operation=write,
        )
        is charged
    )
    assert writes == (["fault_drop"] if charged else [])
    assert h.commands == []


@pytest.mark.parametrize("outcome", ["accepted", "refused", "malformed", "uncertain"])
async def test_private_a_retains_only_exact_validated_acceptance(outcome: str) -> None:
    process, peer, evidence = await _hs_ready()
    proof = process._start_proof
    assert proof is not None
    assert process._human_start_receipt is process._human_start_receipt_proof is None
    if outcome == "malformed":
        peer.replies["start_roast_session"] = _hs_raw({"session_id": "invalid"})
    elif outcome == "uncertain":
        peer.replies["start_roast_session"] = RuntimeError("synthetic transport failure")
    result = await _hs_attempt(process, evidence, gate=lambda: outcome != "refused")
    if outcome == "accepted":
        assert isinstance(result, HumanStartAccepted)
        assert process._human_start_receipt is result
        assert process._human_start_receipt_proof is proof
    else:
        assert not isinstance(result, HumanStartAccepted)
        assert process._human_start_receipt is None
        assert process._human_start_receipt_proof is None
    assert len(peer.calls) == (0 if outcome == "refused" else 1)


async def test_bound_direct_start_refuses_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, _ = await bound(monkeypatch)
    before = h.controller.snapshot(), list(h.sink.evaluations), list(h.commands)
    with pytest.raises(RuntimeError, match="cannot start a run"):
        await h.controller.start_run(PROFILE)
    assert (h.controller.snapshot(), h.sink.evaluations, h.commands) == before


@pytest.mark.parametrize(
    "invalidate", ["phase", "generation", "projector", "owner", "executor", "transport"]
)
async def test_prepare_callback_invalidation_precedes_reset_and_publication(
    monkeypatch: pytest.MonkeyPatch, invalidate: str
) -> None:
    h = prepared()
    process, _, _ = await _hs_ready()
    adapter = RoasterControlAdapter.from_process(process)
    h.controller._executor = adapter
    h.controller._current_session_id = adapter._human_session_id
    h.controller._current_child_generation = adapter._human_child_epoch

    def revoke() -> bool:
        if invalidate == "phase":
            h.controller.transition_to(RoastPhase.FAULTED)
        elif invalidate == "generation":
            h.arbiter.admit_emergency_stop()
        elif invalidate == "projector":
            h.controller._current_session_id = lambda: None
        elif invalidate == "owner":
            h.controller._human_run = replace(run_of(h))
        elif invalidate == "executor":
            h.controller._executor = h.actuator
        else:
            adapter._fixed_zero_transport = None
        return False

    h.controller._teardown_unconfirmed = revoke
    before = adapter._identity_generation
    assert not h.controller._prepare_human_session(adapter, PROFILE)
    assert adapter._identity_generation == before
    assert not run_of(h)._session_handoff_prepared
    assert h.controller._prepared_human_session is h.controller._profile is None
    assert h.commands == h.sink.evaluations == []
