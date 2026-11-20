"""Sessionless prepared-owner fences and isolated synthetic dispatch controls.

Direct boundary controls deliberately do not model an integrated prepared roast.
"""

import asyncio
from enum import Enum
from typing import Literal

import pytest

from roastpilot_agent.config import ControllerConfig, SafetyLimits
from roastpilot_agent.controller import CommandArbiter, RoastController
from roastpilot_agent.human_protection import (
    CommandDisposition,
    ContainmentAttempt,
    ControlAuthority,
    CounterClass,
    CounterEvidence,
    EndpointDisposition,
    EndpointOwner,
    ReadingFreshness,
    WriteClass,
)
from roastpilot_agent.human_run_ownership import (
    AdmissionCloseReason,
    HumanCallAdmissionFence,
    HumanRunAdmission,
    HumanRunOwnership,
    HumanRunState,
    PendingHumanCall,
)
from roastpilot_agent.models import RoastPhase
from roastpilot_agent.safety import SafetyPolicy, SafetyVerdict
from tests.conftest import EventSink, FakeClock, RecordingSnapshotSink
from tests.test_controller import PROFILE
from tests.test_controller_human_mode import HumanHarness, HumanRoaster, make_human


def prepared(
    *,
    ownership: HumanRunOwnership | None = None,
    run_id: str = "prepared",
    authority: ControlAuthority = ControlAuthority.HUMAN,
) -> HumanHarness:
    """Construct only a private sessionless owner, with no Start or attachment."""
    clock = FakeClock()
    log: list[str] = []
    actuator = HumanRoaster(clock, log)
    sink = RecordingSnapshotSink(log)
    emitter = EventSink(log)
    ownership = ownership if ownership is not None else HumanRunOwnership()
    fence = HumanCallAdmissionFence()
    arbiter = CommandArbiter()
    controller = RoastController(
        config=ControllerConfig(),
        safety=SafetyPolicy(SafetyLimits()),
        state_reader=actuator,
        command_executor=actuator,
        snapshot_sink=sink,
        event_emitter=emitter,
        clock=clock,
        actuator_arbiter=arbiter,
        authority=authority,
        run_id=run_id,
        human_reader=actuator,
        human_ownership=ownership,
        human_admission=fence,
        _prepare_human_owner=True,
    )
    return HumanHarness(clock, actuator, sink, emitter, controller, ownership, fence, arbiter, log)


def run_of(h: HumanHarness) -> HumanRunState:
    """Get the actual controller claim without fabricating a session."""
    run = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert run is not None
    return run


def fresh_boundary(h: HumanHarness) -> None:
    """Supply real tracker observations only for isolated boundary controls."""
    tracker = h.controller._accepted_readings  # pyright: ignore[reportPrivateUsage]
    for status in (1, 2):
        h.clock.advance(0.25)
        tracker.observe(
            child_epoch=1,
            counters=CounterEvidence(status, 0, CounterClass.PRESENT),
            bean_temp_c=180,
            env_temp_c=200,
            request_admitted_at=h.clock(),
        )
    assert tracker.recheck() is ReadingFreshness.FRESH


def test_claim_is_read_only_sessionless_and_blocks_same_id_replacement() -> None:
    assert HumanRunAdmission.__bases__ == (Enum,)
    assert set(HumanRunAdmission) == {
        HumanRunAdmission.PREPARED,
        HumanRunAdmission.ADMITTED,
        HumanRunAdmission.SESSION_BOUND,
    }
    assert HumanRunState("default").admission is HumanRunAdmission.ADMITTED
    h = prepared()
    run = run_of(h)
    assert run.admission is HumanRunAdmission.PREPARED
    assert run.child_generation is run.session_id is None
    with pytest.raises(AttributeError):
        run.admission = HumanRunAdmission.ADMITTED  # pyright: ignore[reportAttributeAccessIssue]
    for run_id in ("prepared", "another"):
        with pytest.raises(RuntimeError, match="unresolved HUMAN ownership"):
            prepared(ownership=h.ownership, run_id=run_id)
        with pytest.raises(RuntimeError, match="unresolved HUMAN ownership"):
            h.ownership.claim_run(run_id)
    assert h.ownership.is_current(run)
    assert run.admission is HumanRunAdmission.PREPARED
    h.controller.note_human_run_durably_closed()
    assert not h.ownership.lifecycle_blocked
    replacement = h.ownership.claim_run("prepared")
    assert replacement.admission is HumanRunAdmission.ADMITTED
    assert not h.ownership.is_current(run)


def test_private_claim_rejects_automatic_and_pending_lifecycle() -> None:
    with pytest.raises(ValueError, match="HUMAN authority"):
        prepared(authority=ControlAuthority.AUTOMATIC)
    ownership = HumanRunOwnership()
    with (
        ownership.hold(PendingHumanCall.READ),
        pytest.raises(RuntimeError, match="unresolved HUMAN ownership"),
    ):
        prepared(ownership=ownership)


@pytest.mark.asyncio
async def test_start_refuses_before_phase_profile_or_history_mutation() -> None:
    h = prepared()
    before = h.controller.snapshot()
    with pytest.raises(RuntimeError, match="prepared HUMAN owner"):
        await h.controller.start_run(PROFILE)
    assert h.controller.snapshot() == before
    assert h.controller._profile is None  # pyright: ignore[reportPrivateUsage]
    assert h.actuator.calls == h.sink.evaluations == h.emitter.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["fc", "beans", "drop", "start_cooling", "stop_cooling"])
async def test_prepared_operator_origins_are_silent(entry: str) -> None:
    h = prepared()
    actions = {
        "fc": h.controller.operator_mark_first_crack,
        "beans": h.controller.operator_mark_beans_added,
        "drop": h.controller.operator_drop_beans,
        "start_cooling": h.controller.operator_start_cooling,
        "stop_cooling": h.controller.operator_stop_cooling,
    }
    await actions[entry]()
    assert h.controller.phase is RoastPhase.IDLE
    assert h.actuator.calls == h.sink.evaluations == h.emitter.events == []
    assert run_of(h).endpoint is EndpointDisposition.NONE
    assert h.actuator.containments == 0


@pytest.mark.asyncio
async def test_recovery_cooling_cannot_resume_prepared_owner() -> None:
    h = prepared()
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    before = list(h.emitter.events)
    await h.controller.operator_start_cooling()
    assert h.emitter.events == before
    h.emitter.events.clear()
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.actuator.calls == h.sink.evaluations == h.emitter.events == []


@pytest.mark.asyncio
async def test_reachable_emergency_then_sessionless_recovery_drop_rejects() -> None:
    h = prepared()
    assert await h.controller.operator_emergency_stop("synthetic")
    assert h.controller.phase is RoastPhase.FAULTED
    before = len(h.sink.evaluations)
    await h.controller.operator_drop_beans()
    assert len(h.sink.evaluations) == before + 1
    assert h.sink.evaluations[-1].verdict is SafetyVerdict.REJECT
    assert h.commands == ["emergency_stop"]
    assert run_of(h).endpoint is EndpointDisposition.NONE
    assert run_of(h).containment is ContainmentAttempt.AVAILABLE
    assert h.actuator.containments == 0


@pytest.mark.asyncio
async def test_direct_drop_origin_refuses_before_latch_or_containment() -> None:
    # Synthetic OPERATOR boundary; no reachable prepared roast is claimed.
    h = prepared()
    fresh_boundary(h)
    assert not await h.controller._execute_human_drop(  # pyright: ignore[reportPrivateUsage]
        EndpointOwner.OPERATOR, None, generation=h.arbiter.generation
    )
    assert run_of(h).endpoint is EndpointDisposition.NONE
    assert run_of(h).endpoint_owner is None
    assert run_of(h).containment is ContainmentAttempt.AVAILABLE
    assert h.controller.phase is RoastPhase.IDLE
    assert h.actuator.calls == h.sink.evaluations == h.emitter.events == []
    assert h.actuator.containments == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(WriteClass))
async def test_actual_dispatch_fence_refuses_otherwise_admissible_candidate(
    kind: WriteClass,
) -> None:
    # Deliberately isolate the dispatch layer: exact OPERATOR latch + FRESH
    # readings for DROP, untouched ownership for SESSION_START. No session.
    h = prepared()
    fresh_boundary(h)
    run = run_of(h)
    if kind is WriteClass.DROP:
        run.endpoint = EndpointDisposition.ADMITTED
        run.endpoint_owner = EndpointOwner.OPERATOR
    assert h.controller._human_routine_permitted(kind, run.endpoint_owner)  # pyright: ignore[reportPrivateUsage]
    assert h.ownership.allows_session_start(run) is (kind is not WriteClass.DROP)
    assert await h.controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
        h.arbiter.generation,
        h.actuator.start_cooling,
        write_class=kind,
        endpoint_owner=run.endpoint_owner,
    ) == (False, None)
    assert h.actuator.calls == []
    assert h.actuator.containments == 0


@pytest.mark.asyncio
async def test_prepared_refuses_before_waiting_for_occupied_arbiter() -> None:
    h = prepared()
    entered, release = asyncio.Event(), asyncio.Event()

    async def occupied() -> None:
        entered.set()
        await release.wait()

    blocker = asyncio.create_task(h.arbiter.run_routine(h.arbiter.generation, occupied))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        assert await asyncio.wait_for(
            h.controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
                h.arbiter.generation, h.actuator.start_cooling, write_class=WriteClass.NON_HEAT
            ),
            1,
        ) == (False, None)
        assert not blocker.done()
        assert h.actuator.calls == []
    finally:
        release.set()
        await blocker


@pytest.mark.asyncio
async def test_fault_cooling_exception_preserves_fault_and_non_heat_independence() -> None:
    h = prepared()
    assert await h.controller.operator_emergency_stop()
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    h.controller._teardown_unconfirmed = lambda: True  # pyright: ignore[reportPrivateUsage]
    await h.controller.operator_start_cooling()
    assert h.controller.phase is RoastPhase.FAULTED
    await h.controller.operator_stop_cooling()
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.commands == ["emergency_stop", "start_cooling", "stop_cooling"]
    assert await h.controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
        h.arbiter.generation, h.actuator.start_cooling, write_class=WriteClass.NON_HEAT
    ) == (False, None)
    run_of(h).command = CommandDisposition.UNKNOWN
    await h.controller.operator_start_cooling()
    assert h.commands == ["emergency_stop", "start_cooling", "stop_cooling"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(WriteClass))
@pytest.mark.parametrize("reservation", ["current", "wrong", "stale"])
async def test_d212_exception_requires_current_reservation_and_non_heat(
    kind: WriteClass, reservation: str
) -> None:
    h = prepared()
    assert await h.controller.operator_emergency_stop()
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.commands == ["emergency_stop"]
    h.actuator.calls.clear()
    generation = h.arbiter.generation
    assert h.controller.reserve_fault_acknowledgement_close(generation)
    token = generation
    if reservation == "wrong":
        token += 1
    elif reservation == "stale":
        h.controller.release_fault_acknowledgement_close(generation)
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    h.controller._teardown_unconfirmed = lambda: True  # pyright: ignore[reportPrivateUsage]
    expected = reservation == "current" and kind is WriteClass.NON_HEAT
    result = await h.controller.execute_fault_acknowledgement_stop(
        generation, h.actuator.stop_cooling, close_reservation=token, write_class=kind
    )
    assert result == (expected, None)
    assert h.commands == (["stop_cooling"] if expected else [])
    assert h.controller.phase is RoastPhase.FAULTED


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(WriteClass))
@pytest.mark.parametrize("origin", ["reservation", "fault_cooling"])
async def test_non_heat_exception_class_discrimination_has_unmasked_base_candidate(
    kind: WriteClass, origin: str
) -> None:
    # Synthetic dispatch evidence, not a reachable prepared post-Start owner.
    # In particular, the real D212 wrapper never supplies a DROP endpoint owner.
    h = prepared()
    assert await h.controller.operator_emergency_stop()
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.commands == ["emergency_stop"]
    h.actuator.calls.clear()
    fresh_boundary(h)
    run = run_of(h)
    if kind is WriteClass.DROP:
        run.endpoint = EndpointDisposition.ADMITTED
        run.endpoint_owner = EndpointOwner.OPERATOR
    h.controller._teardown_unconfirmed = lambda: False  # pyright: ignore[reportPrivateUsage]
    assert h.fence.closed_reason is None
    assert h.ownership.is_current(run)
    assert run.command is CommandDisposition.SETTLED
    assert h.controller._human_actuation_admitted()  # pyright: ignore[reportPrivateUsage]
    assert h.controller._accepted_readings.recheck() is ReadingFreshness.FRESH  # pyright: ignore[reportPrivateUsage]
    assert h.controller._human_routine_permitted(kind, run.endpoint_owner)  # pyright: ignore[reportPrivateUsage]
    if kind is WriteClass.SESSION_START:
        assert h.ownership.allows_session_start(run)
    generation = h.arbiter.generation
    reservation = generation if origin == "reservation" else None
    if reservation is not None:
        assert h.controller.reserve_fault_acknowledgement_close(reservation)
        assert h.controller.fault_acknowledgement_close_is_current(reservation)
    result = await h.controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
        generation,
        h.actuator.stop_cooling,
        write_class=kind,
        endpoint_owner=run.endpoint_owner,
        close_reservation=reservation,
        _fault_cooling=origin == "fault_cooling",
    )
    expected = kind is WriteClass.NON_HEAT
    assert result == (expected, None)
    assert h.commands == (["stop_cooling"] if expected else [])
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.actuator.containments == 0


@pytest.mark.asyncio
async def test_terminal_emergency_retry_survives_released_transition_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = prepared()

    async def failed_stop(*, reason: str) -> None:
        raise TimeoutError(reason)

    monkeypatch.setattr(h.actuator, "emergency_stop", failed_stop)
    assert not await h.controller.operator_emergency_stop("synthetic failure")
    assert h.controller.phase is RoastPhase.FAULTED
    assert not h.arbiter.emergency_stop_active
    assert not h.controller._emergency_stop_transition_pending  # pyright: ignore[reportPrivateUsage]
    pending = h.controller._pending_fail_safe  # pyright: ignore[reportPrivateUsage]
    assert pending is not None
    assert pending.adjusted_heat == 0
    assert pending.adjusted_fan == SafetyLimits().overrun_safe_fan_percent
    assert not await h.controller._set_targets(heat_percent=0, fan_percent=0)  # pyright: ignore[reportPrivateUsage]
    assert h.actuator.calls == []
    await h.controller.tick()
    assert h.actuator.calls == [
        ("set_heat", {"heat_percent": 0}),
        ("set_fan", {"fan_percent": SafetyLimits().overrun_safe_fan_percent}),
    ]
    assert h.controller._pending_fail_safe is None  # pyright: ignore[reportPrivateUsage]
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.actuator.containments == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation", ["generation", "owner", "reservation", "phase"])
async def test_dispatch_rechecks_real_wait_without_changing_prepared_state(
    invalidation: Literal["generation", "owner", "reservation", "phase"],
) -> None:
    h = prepared()
    assert await h.controller.operator_emergency_stop()
    h.actuator.calls.clear()
    generation = h.arbiter.generation
    entered, release = asyncio.Event(), asyncio.Event()

    async def occupied() -> None:
        entered.set()
        await release.wait()

    blocker = asyncio.create_task(h.arbiter.run_routine(generation, occupied))
    await asyncio.wait_for(entered.wait(), 1)
    reservation = generation if invalidation == "reservation" else None
    if reservation is not None:
        assert h.controller.reserve_fault_acknowledgement_close(reservation)
    queued = asyncio.create_task(
        h.controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
            generation,
            h.actuator.start_cooling,
            write_class=WriteClass.NON_HEAT,
            _fault_cooling=reservation is None,
            close_reservation=reservation,
        )
    )
    await asyncio.sleep(0)
    assert not queued.done()
    if invalidation == "generation":
        h.arbiter.admit_emergency_stop()
        await h.arbiter.cancel_emergency_stop_admission()
    elif invalidation == "owner":
        h.controller.note_human_run_durably_closed()
        h.ownership.claim_run("replacement")
    elif invalidation == "reservation":
        h.controller.release_fault_acknowledgement_close(generation)
    else:
        h.controller.transition_to(RoastPhase.IDLE)
    release.set()
    await blocker
    assert await queued == (False, None)
    assert h.actuator.calls == []
    assert run_of(h).admission is HumanRunAdmission.PREPARED


@pytest.mark.asyncio
async def test_admitted_caller_keeps_start_marks_cooling_and_drop_parity() -> None:
    h = await make_human()
    assert run_of(h).admission is HumanRunAdmission.ADMITTED
    await h.turn(bean=180)
    await h.turn(bean=183, t0=True)
    await h.controller.operator_mark_first_crack()
    await h.controller.operator_drop_beans()
    await h.controller.operator_start_cooling()
    await h.controller.operator_stop_cooling()
    assert h.controller.phase is RoastPhase.COMPLETE
    assert h.commands == [
        "start_session",
        "set_heat",
        "set_fan",
        "mark_first_crack",
        "drop_beans",
        "start_cooling",
        "stop_cooling",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_by", ["unknown_command", "replaced_owner"])
@pytest.mark.parametrize("origin", ["fault_cooling", "d212"])
async def test_non_heat_exceptions_retain_base_ownership_checks(
    blocked_by: str,
    origin: str,
) -> None:
    h = prepared()
    assert await h.controller.operator_emergency_stop()
    h.actuator.calls.clear()
    if blocked_by == "unknown_command":
        run_of(h).command = CommandDisposition.UNKNOWN
    else:
        h.controller.note_human_run_durably_closed()
        h.ownership.claim_run("replacement")
    if origin == "fault_cooling":
        await h.controller.operator_start_cooling()
        await h.controller.operator_stop_cooling()
    else:
        generation = h.arbiter.generation
        assert h.controller.reserve_fault_acknowledgement_close(generation)
        assert await h.controller.execute_fault_acknowledgement_stop(
            generation,
            h.actuator.stop_cooling,
            close_reservation=generation,
            write_class=WriteClass.NON_HEAT,
        ) == (False, None)
    assert h.actuator.calls == []


@pytest.mark.asyncio
async def test_prepared_sessionless_containment_has_no_exact_identity_authority() -> None:
    h = prepared()
    assert h.controller._known_human_identity() is None  # pyright: ignore[reportPrivateUsage]
    await h.controller._contain_human_heat(generation=h.arbiter.generation)  # pyright: ignore[reportPrivateUsage]
    assert h.actuator.calls == []
    assert h.actuator.containments == 0
    assert run_of(h).containment is ContainmentAttempt.AVAILABLE
