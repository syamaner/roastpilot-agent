"""Hardware-free witnesses for HUMAN controller dispatch and ownership."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

import pytest

from roastpilot_agent.config import ControllerConfig, SafetyLimits
from roastpilot_agent.controller import CommandArbiter, RoastController
from roastpilot_agent.human_protection import (
    CommandDisposition,
    ContainmentAttempt,
    ContainmentOutcome,
    ContainmentResult,
    ControlAuthority,
    CounterClass,
    CounterEvidence,
    EndpointDisposition,
    EndpointOwner,
    HumanReadEvidence,
    RawTemperature,
    ReadingFreshness,
    WriteClass,
)
from roastpilot_agent.human_run_ownership import (
    AdmissionCloseReason,
    HumanCallAdmissionFence,
    HumanRunOwnership,
    PendingHumanCall,
)
from roastpilot_agent.models import AppliedRoasterState, RoastEventKind, RoastPhase
from roastpilot_agent.safety import SafetyPolicy
from tests.conftest import EventSink, FakeClock, FakeMCPClient, RecordingSnapshotSink
from tests.test_controller import PROFILE, reading

pytestmark = pytest.mark.asyncio


class HumanRoaster(FakeMCPClient):
    """One synthetic current session with separately scriptable HUMAN operations."""

    def __init__(self, clock: FakeClock, log: list[str]) -> None:
        super().__init__(log=log)
        self.clock = clock
        self.epoch = 1
        self.session_id = "synthetic-session"
        self.status = 0
        self.bean = 180.0
        self.charged = False
        self.bound = True
        self.collect: Callable[[], Awaitable[None]] | None = None
        self.contain: Callable[[], Awaitable[ContainmentResult]] | None = None
        self.reads = 0
        self.containments = 0

    async def read_human_evidence(self) -> HumanReadEvidence:
        self.reads += 1
        admitted = self.clock()
        if self.collect is not None:
            await self.collect()
        self.status += 1
        return HumanReadEvidence(
            telemetry=reading(self.bean, 200.0, t0_detected=self.charged),
            device_object=True,
            bean=RawTemperature.VALID,
            env=RawTemperature.VALID,
            bean_temp_c=self.bean,
            env_temp_c=200.0,
            counters=CounterEvidence(self.status, 0, CounterClass.PRESENT),
            request_admitted_at=admitted,
            bound_child_epoch=self.epoch if self.bound else None,
        )

    async def contain_heat_fixed_zero(self) -> ContainmentResult:
        self.containments += 1
        if self.contain is not None:
            return await self.contain()
        return ContainmentResult(ContainmentOutcome.REFUSED_BUSY)


@dataclass
class HumanHarness:
    clock: FakeClock
    actuator: HumanRoaster
    sink: RecordingSnapshotSink
    emitter: EventSink
    controller: RoastController
    ownership: HumanRunOwnership
    fence: HumanCallAdmissionFence
    arbiter: CommandArbiter
    log: list[str]

    @property
    def commands(self) -> list[str]:
        return [command for command, _ in self.actuator.calls]

    async def turn(self, *, bean: float, t0: bool = False) -> None:
        self.clock.advance(0.25)
        self.actuator.bean = bean
        self.actuator.charged = t0
        await self.controller.tick()


async def make_human(
    *,
    teardown_unconfirmed: Callable[[], bool] = lambda: False,
    discovered: bool = True,
) -> HumanHarness:
    clock = FakeClock()
    log: list[str] = []
    actuator = HumanRoaster(clock, log)
    sink = RecordingSnapshotSink(log)
    emitter = EventSink(log)
    ownership = HumanRunOwnership()
    fence = HumanCallAdmissionFence()
    arbiter = CommandArbiter()
    controller = RoastController(
        config=ControllerConfig(t0_debounce_ticks=1),
        safety=SafetyPolicy(SafetyLimits()),
        state_reader=actuator,
        command_executor=actuator,
        snapshot_sink=sink,
        event_emitter=emitter,
        clock=clock,
        actuator_arbiter=arbiter,
        authority=ControlAuthority.HUMAN,
        run_id="synthetic-run",
        human_reader=actuator,
        human_admission=fence,
        human_ownership=ownership,
        teardown_unconfirmed=teardown_unconfirmed,
        current_session_id=lambda: actuator.session_id,
        current_child_generation=lambda: actuator.epoch,
        containment_discovered=lambda: discovered,
    )
    await controller.start_run(PROFILE)
    return HumanHarness(clock, actuator, sink, emitter, controller, ownership, fence, arbiter, log)


@pytest.mark.parametrize(("bean", "expected"), [(195.99, 0), (196.0, 1), (196.01, 1)])
async def test_t31_charge_turn_commits_once_and_threshold_is_inclusive(
    bean: float, expected: int
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=bean, t0=True)
    assert h.actuator.reads == 2
    assert h.commands.count("drop_beans") == expected
    assert h.emitter.kinds().count(RoastEventKind.T0_DETECTED) == 1
    assert h.controller.phase is (
        RoastPhase.COOLING if expected else RoastPhase.ROASTING_PRE_FIRST_CRACK
    )
    if expected:
        assert any(
            isinstance(payload, dict)
            and cast("dict[str, object]", payload).get("target_outcome") == "interrupted_unmet"
            for _, payload in h.emitter.events
        )


async def test_t30_protective_drop_precedes_any_sink_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_sink(_: object) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(h.sink, "persist_snapshot", blocked_sink)
    task = asyncio.create_task(h.turn(bean=196, t0=True))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert h.commands.count("drop_beans") == 1
        assert h.controller.phase is RoastPhase.COOLING
    finally:
        release.set()
        await task


@pytest.mark.parametrize("failure", ["timeout", "malformed", "cancel", "exception"])
async def test_t36_unknown_drop_never_retries_or_contains(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    h = await make_human()

    async def drop() -> AppliedRoasterState | None:
        h.actuator.calls.append(("drop_beans", {}))
        if failure == "timeout":
            raise TimeoutError()
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "exception":
            raise RuntimeError("synthetic drop failure")
        return None

    monkeypatch.setattr(h.actuator, "drop_beans", drop)
    await h.turn(bean=180)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await h.turn(bean=196, t0=True)
    else:
        await h.turn(bean=196, t0=True)
    await h.controller.operator_drop_beans()
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.commands.count("drop_beans") == 1
    assert h.actuator.containments == 0
    assert h.ownership.lifecycle_blocked
    assert h.controller._human_run is not None  # pyright: ignore[reportPrivateUsage]
    assert h.controller._human_run.command is CommandDisposition.UNKNOWN  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("bean", "drop", "emergency"), [(185, False, False), (196, True, False), (250, False, True)]
)
async def test_te23_late_read_retains_evidence_and_cannot_drive_guarded_writes(
    bean: float, drop: bool, emergency: bool
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.actuator.calls.clear()
    entered = asyncio.Event()
    allow = asyncio.Event()

    async def hold_read() -> None:
        entered.set()
        await allow.wait()

    h.actuator.collect = hold_read
    task = asyncio.create_task(h.turn(bean=bean, t0=True))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert h.ownership.pending(PendingHumanCall.READ)
        h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    finally:
        allow.set()
        await task
    assert not h.ownership.pending(PendingHumanCall.READ)
    assert "drop_beans" not in h.commands
    assert "set_heat" not in h.commands
    assert "set_fan" not in h.commands
    assert ("emergency_stop" in h.commands) is emergency
    run = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert run is not None
    assert run.endpoint is (EndpointDisposition.WITHHELD if drop else EndpointDisposition.NONE)
    assert run.containment is ContainmentAttempt.AVAILABLE
    assert h.actuator.containments == 0


@pytest.mark.parametrize("latched", [False, True])
async def test_te11_refused_read_breaks_continuity_without_failure_verdict(latched: bool) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    if latched:
        h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    before = h.controller.phase
    evaluations = list(h.sink.evaluations)
    old_reads = h.actuator.reads
    h.fence.close(AdmissionCloseReason.DEVICE_RESPAWN)
    await h.turn(bean=196)
    assert h.actuator.reads == old_reads
    assert h.controller.phase is before
    assert h.sink.evaluations == evaluations
    assert h.controller._accepted_readings.recheck() is ReadingFreshness.INVALID  # pyright: ignore[reportPrivateUsage]
    assert h.fence.reopen_after_respawn(
        child_started=True,
        teardown_unconfirmed=False,
        lifecycle_blocked=False,
        shutdown_started=False,
    )
    h.controller.transition_to(RoastPhase.FAULTED)
    await h.turn(bean=184)
    assert h.controller._accepted_readings.recheck() is ReadingFreshness.UNPROVEN  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("action", [WriteClass.HEAT, WriteClass.FAN, WriteClass.SESSION_START])
async def test_te22_queued_guarded_write_rechecks_closed_fence(action: WriteClass) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    entered = asyncio.Event()
    allow = asyncio.Event()
    attempted = asyncio.Event()

    async def occupied() -> None:
        entered.set()
        await allow.wait()

    async def write() -> None:
        attempted.set()

    generation = h.arbiter.generation
    blocker = asyncio.create_task(h.arbiter.run_routine(generation, occupied))
    await asyncio.wait_for(entered.wait(), 1)
    queued = asyncio.create_task(
        h.controller._run_routine_write(generation, write, write_class=action)  # pyright: ignore[reportPrivateUsage]
    )
    await asyncio.sleep(0)
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    allow.set()
    assert await queued == (False, None)
    await blocker
    assert not attempted.is_set()


@pytest.mark.parametrize(
    "outcome",
    [
        ContainmentOutcome.COMMAND_STATE_RETURNED,
        ContainmentOutcome.REFUSED_IDENTITY,
        ContainmentOutcome.REFUSED_CAPABILITY,
        ContainmentOutcome.REFUSED_BUSY,
        ContainmentOutcome.UNKNOWN_COMPLETION,
        ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE,
    ],
)
async def test_t46b_containment_budget_does_not_reset(
    outcome: ContainmentOutcome,
) -> None:
    h = await make_human()

    async def contain() -> ContainmentResult:
        return ContainmentResult(outcome)

    h.actuator.contain = contain
    result = await h.controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
        generation=h.arbiter.generation
    )
    await h.controller._contain_human_heat(generation=h.arbiter.generation)  # pyright: ignore[reportPrivateUsage]
    assert result.outcome is outcome
    assert h.actuator.containments == 1
    assert h.controller.snapshot().human_command_disposition is (
        CommandDisposition.UNKNOWN if outcome.is_unknown else CommandDisposition.SETTLED
    )
    assert "set_heat" not in h.commands
    assert "emergency_stop" not in h.commands


@pytest.mark.parametrize("bean", [195.0, 196.0, 197.0])
async def test_t42_explicit_continuation_rechecks_before_any_lever(bean: float) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)

    async def delay() -> None:
        h.clock.advance(20)

    h.actuator.collect = delay
    await h.turn(bean=196)
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.commands.count("drop_beans") == 0
    h.actuator.collect = None
    await h.turn(bean=184)
    await h.turn(bean=185)
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.commands.count("drop_beans") == 0
    assert await h.controller.human_operator_continue(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
    assert h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]
    h.actuator.calls.clear()
    await h.turn(bean=bean)
    if bean >= 196:
        assert h.commands == ["drop_beans"]
        assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
        assert h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]
    else:
        assert not h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
        assert not h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]
        assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK


@pytest.mark.parametrize(
    ("endpoint", "owner", "closed", "allowed"),
    [
        (EndpointDisposition.ISSUED, EndpointOwner.FAULT_CONTROL, False, True),
        (EndpointDisposition.ISSUED, EndpointOwner.OPERATOR_RECOVERY, False, True),
        (EndpointDisposition.ISSUED, EndpointOwner.OPERATOR_RECOVERY, True, False),
        (EndpointDisposition.ISSUED, EndpointOwner.PROTECTION, False, False),
        (EndpointDisposition.UNKNOWN, EndpointOwner.FAULT_CONTROL, False, False),
        (EndpointDisposition.WITHHELD, EndpointOwner.FAULT_CONTROL, False, False),
    ],
)
async def test_fault_drop_cooling_transition_is_owned_and_live(
    endpoint: EndpointDisposition, owner: EndpointOwner, closed: bool, allowed: bool
) -> None:
    h = await make_human()
    h.controller.transition_to(RoastPhase.FAULTED)
    run = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert run is not None
    run.endpoint = endpoint
    run.endpoint_owner = owner
    run.durably_closed = closed
    assert h.controller.can_transition(RoastPhase.COOLING) is allowed


async def test_t44_live_faulted_recovery_drop_uses_current_evidence() -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.FAULTED)
    assert h.controller.human_recovery_drop_allowed()
    dispatched: list[str] = []

    async def write() -> None:
        dispatched.append("fault_drop")

    assert await h.controller.execute_human_fault_drop(
        h.actuator.session_id,
        generation=h.arbiter.generation,
        lease_current=lambda: True,
        operation=write,
    )
    assert dispatched == ["fault_drop"]
    assert h.controller.phase is RoastPhase.COOLING
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.ISSUED


@pytest.mark.parametrize("unconfirmed", [False, True])
async def test_te26_fault_drop_fence_preserves_known_withheld(unconfirmed: bool) -> None:
    flag = False
    h = await make_human(teardown_unconfirmed=lambda: flag)
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.FAULTED)
    if unconfirmed:
        flag = True
    else:
        h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)

    async def write() -> None:
        pytest.fail("terminal fault drop reached the exact-session write after closure")

    assert not await h.controller.execute_human_fault_drop(
        h.actuator.session_id,
        generation=h.arbiter.generation,
        lease_current=lambda: True,
        operation=write,
    )
    snapshot = h.controller.snapshot()
    assert snapshot.human_endpoint is EndpointDisposition.WITHHELD
    assert snapshot.human_command_disposition is CommandDisposition.SETTLED
    assert snapshot.physical_intervention_required
    assert h.actuator.containments == 0


async def test_te25_only_existing_emergency_retry_can_write_zero_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stop_unconfirmed() -> bool:
        raise AssertionError("emergency control must not consult child teardown evidence")

    h = await make_human()

    async def failed_emergency(*, reason: str) -> AppliedRoasterState | None:
        raise TimeoutError(reason)

    monkeypatch.setattr(h.actuator, "emergency_stop", failed_emergency)
    monkeypatch.setattr(h.controller, "_teardown_unconfirmed", stop_unconfirmed)
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    await h.controller.operator_emergency_stop("test stop")
    h.actuator.calls.clear()
    await h.controller.tick()
    assert h.commands == ["set_heat", "set_fan"]
    assert h.actuator.calls[0][1] == {"heat_percent": 0}
    refused = await h.controller._set_targets(heat_percent=0, fan_percent=0)  # pyright: ignore[reportPrivateUsage]
    assert not refused
    assert h.commands == ["set_heat", "set_fan"]


async def test_t51_acceptance_age_diagnostic_is_finite_and_persisted() -> None:
    h = await make_human()
    tracker = h.controller._accepted_readings  # pyright: ignore[reportPrivateUsage]
    assert tracker.acceptance_age_seconds() is None
    await h.turn(bean=180)
    assert tracker.acceptance_age_seconds() is None
    await h.turn(bean=185, t0=True)
    assert tracker.acceptance_age_seconds() == 0.25
    assert any(
        "accepted reading fresh, conservative age seconds 0.25" in item.reason
        for item in h.sink.evaluations
    )
    h.clock.advance(20)
    before = vars(tracker).copy()
    assert tracker.acceptance_age_seconds() == 20.25
    assert vars(tracker) == before
    h.clock.now = float("nan")
    assert tracker.acceptance_age_seconds() is None
    h.clock.now = -1
    assert tracker.acceptance_age_seconds() is None
    assert vars(tracker) == before


async def test_t33_admitted_drop_stays_withheld_after_queue_age_expiry() -> None:
    h = await make_human()
    await h.turn(bean=180)
    occupied = asyncio.Event()
    allow = asyncio.Event()

    async def prior_write() -> None:
        occupied.set()
        await allow.wait()

    in_flight = asyncio.create_task(h.arbiter.run_routine(h.arbiter.generation, prior_write))
    await asyncio.wait_for(occupied.wait(), 1)
    turn = asyncio.create_task(h.turn(bean=196, t0=True))
    try:
        await asyncio.sleep(0)
        assert h.controller.snapshot().human_endpoint is EndpointDisposition.ADMITTED
        h.clock.advance(20)
    finally:
        allow.set()
        await turn
        await in_flight
    assert h.commands.count("drop_beans") == 0
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.WITHHELD
    await h.turn(bean=196)
    await h.turn(bean=196)
    assert h.commands.count("drop_beans") == 0
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED


async def test_te17_unexpected_child_owner_failure_keeps_read_and_emergency() -> None:
    unconfirmed = False
    h = await make_human(teardown_unconfirmed=lambda: unconfirmed)
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.actuator.calls.clear()
    unconfirmed = True
    await h.turn(bean=196)
    assert h.actuator.reads == 3
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.WITHHELD
    assert h.actuator.containments == 0
    assert h.commands == []
    await h.turn(bean=250)
    assert h.actuator.reads == 4
    assert h.commands == ["emergency_stop"]


async def test_td7_second_read_is_typed_refusal_without_opening_scope() -> None:
    h = await make_human()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        entered.set()
        await release.wait()

    h.actuator.collect = hold
    first = asyncio.create_task(
        h.controller._read_human_call()  # pyright: ignore[reportPrivateUsage]
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await h.controller.tick()
        assert h.actuator.reads == 1
        assert h.ownership.pending(PendingHumanCall.READ)
        assert h.controller.phase is RoastPhase.PREHEATING
        assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
    finally:
        release.set()
        await first
    assert not h.ownership.pending(PendingHumanCall.READ)


async def test_t49_restart_does_not_replay_heat_or_drop() -> None:
    h = await make_human()
    h.actuator.calls.clear()
    await h.controller.recover_from_restart(RoastPhase.DEVELOPMENT)
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.HISTORY_UNKNOWN
    assert h.controller.snapshot().physical_intervention_required
    assert not await h.controller.human_operator_continue(RoastPhase.DEVELOPMENT)
    await h.controller.operator_drop_beans()
    assert h.actuator.calls == []


async def test_t50_machine_fc_and_post_endpoint_human_note_do_not_advance_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    read = h.actuator.read_human_evidence

    async def machine_mark() -> HumanReadEvidence:
        evidence = await read()
        telemetry = evidence.telemetry
        assert telemetry is not None
        return replace(
            evidence, telemetry=telemetry.model_copy(update={"first_crack_detected": True})
        )

    monkeypatch.setattr(h.actuator, "read_human_evidence", machine_mark)
    await h.turn(bean=190)
    assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    await h.turn(bean=196)
    before = h.controller.snapshot()
    await h.controller.operator_mark_first_crack()
    after = h.controller.snapshot()
    assert before.phase is after.phase is RoastPhase.COOLING
    assert before.development_percent is after.development_percent is None
    assert after.human_endpoint is EndpointDisposition.ISSUED
    assert "mark_first_crack" not in h.commands
