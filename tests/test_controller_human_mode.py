"""Hardware-free witnesses for HUMAN controller dispatch and ownership."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from unittest.mock import Mock, PropertyMock

import pytest

from roastpilot_agent import controller as controller_module
from roastpilot_agent.advisor import AdvisorFailureMode, FakeAdvisor, RoastAdvisor
from roastpilot_agent.config import AppConfig, ControllerConfig, PostFirstCrackControl, SafetyLimits
from roastpilot_agent.controller import (
    AdvisoryTrigger,
    CommandArbiter,
    FaultControlExecutor,
    InvalidTransitionError,
    RoastController,
)
from roastpilot_agent.human_protection import (
    CommandDisposition,
    ContainmentAttempt,
    ContainmentOutcome,
    ContainmentReason,
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
    protection_eligible,
)
from roastpilot_agent.human_run_ownership import (
    AdmissionCloseReason,
    HumanCallAdmissionFence,
    HumanCallRefusal,
    HumanRunOwnership,
    PendingHumanCall,
    RefusedHumanCall,
)
from roastpilot_agent.models import (
    AppliedRoasterState,
    DropReason,
    FaultControlState,
    OperatorAction,
    RoastEventKind,
    RoastEventSource,
    RoastPhase,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyPolicy, SafetyVerdict
from roastpilot_agent.store import RoastStore
from tests.conftest import EventSink, FakeClock, FakeMCPClient, RecordingSnapshotSink
from tests.test_controller import PROFILE, decision, reading

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
        return ContainmentResult(
            ContainmentOutcome.REFUSED_BUSY, ContainmentReason.COMMAND_IN_PROGRESS
        )


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

    async def turn(self, *, bean: int | float, t0: bool = False) -> None:
        self.clock.advance(0.25)
        self.actuator.bean = bean
        self.actuator.charged = t0
        await self.controller.tick()


async def make_human(
    *,
    teardown_unconfirmed: Callable[[], bool] = lambda: False,
    discovered: bool = True,
    charge_debounce: int = 1,
    start: bool = True,
    advisor: RoastAdvisor | None = None,
    safety: SafetyPolicy | None = None,
    bind_session: bool = True,
    bind_child: bool = True,
    read_port: bool = True,
    post_first_crack_control: PostFirstCrackControl | None = None,
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
        config=ControllerConfig(
            t0_debounce_ticks=charge_debounce,
            post_first_crack_control=(
                post_first_crack_control
                if post_first_crack_control is not None
                else PostFirstCrackControl()
            ),
        ),
        safety=safety if safety is not None else SafetyPolicy(SafetyLimits()),
        state_reader=actuator,
        command_executor=actuator,
        snapshot_sink=sink,
        event_emitter=emitter,
        clock=clock,
        advisor=advisor,
        actuator_arbiter=arbiter,
        authority=ControlAuthority.HUMAN,
        run_id="synthetic-run",
        human_reader=actuator if read_port else None,
        human_admission=fence,
        human_ownership=ownership,
        teardown_unconfirmed=teardown_unconfirmed,
        current_session_id=(lambda: actuator.session_id) if bind_session else None,
        current_child_generation=(lambda: actuator.epoch) if bind_child else None,
        containment_discovered=lambda: discovered,
    )
    if start:
        await controller.start_run(PROFILE)
    return HumanHarness(clock, actuator, sink, emitter, controller, ownership, fence, arbiter, log)


@pytest.mark.parametrize(
    ("phase", "advisor_fails", "post_fc_enabled", "ceiling_enabled"),
    [
        pytest.param(
            RoastPhase.ROASTING_PRE_FIRST_CRACK, False, True, True, id="no-advisor-no-dtr"
        ),
        pytest.param(RoastPhase.DEVELOPMENT, False, True, False, id="legacy-ceiling-off"),
        pytest.param(RoastPhase.DEVELOPMENT, True, False, True, id="failed-advisor-in-dwell"),
    ],
)
async def test_t11_human_protection_does_not_wait_for_legacy_gates(
    phase: RoastPhase, advisor_fails: bool, post_fc_enabled: bool, ceiling_enabled: bool
) -> None:
    advisor = FakeAdvisor([AdvisorFailureMode.PROVIDER_ERROR]) if advisor_fails else None
    h = await make_human(
        advisor=advisor,
        post_first_crack_control=PostFirstCrackControl(
            enabled=post_fc_enabled, ceiling_guard_drop_enabled=ceiling_enabled
        ),
    )
    await h.turn(bean=180)
    await h.turn(bean=183, t0=True)
    if phase is RoastPhase.DEVELOPMENT:
        await h.controller.operator_mark_first_crack()
    await h.turn(bean=185)

    before = h.controller.snapshot()
    assert before.phase is phase
    assert before.charge_detected
    assert before.human_endpoint is EndpointDisposition.NONE
    assert before.human_command_disposition is CommandDisposition.SETTLED
    assert h.controller._known_human_identity() == (  # pyright: ignore[reportPrivateUsage]
        h.actuator.session_id,
        h.actuator.epoch,
    )
    assert h.controller._accepted_readings.project_freshness() is ReadingFreshness.FRESH  # pyright: ignore[reportPrivateUsage]
    if phase is RoastPhase.ROASTING_PRE_FIRST_CRACK:
        assert before.development_percent is None
        assert h.controller._development_time_ratio() is None  # pyright: ignore[reportPrivateUsage]
    if advisor is not None:
        assert len(advisor.contexts) == 1
        assert [entry.status for entry in h.sink.advisor_decisions] == ["provider_error"]
        policy = h.controller._advisory_policy  # pyright: ignore[reportPrivateUsage]
        threshold = reading(196, 200)
        assert (
            policy.evaluate(
                phase=phase, telemetry=threshold, now=h.clock() + 0.25, manual_request=False
            )
            is None
        )
        assert (
            policy.evaluate(
                phase=phase,
                telemetry=threshold,
                now=h.clock() + ControllerConfig().post_fc_min_consult_interval_seconds,
                manual_request=False,
            )
            is AdvisoryTrigger.BEAN_TEMP_DELTA
        )

    h.actuator.calls.clear()
    h.sink.evaluations.clear()
    h.log.clear()
    await h.turn(bean=196)

    assert h.commands.count("drop_beans") == 1
    assert h.commands == ["drop_beans"]
    assert h.controller.phase is RoastPhase.COOLING
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.ISSUED
    assert [
        evaluation.verdict
        for evaluation in h.sink.evaluations
        if evaluation.rule == "human_protective_drop"
    ] == [SafetyVerdict.ALLOW]
    (command,) = [
        cast("dict[str, object]", payload)
        for kind, payload in h.emitter.events
        if kind is RoastEventKind.COMMAND_EXECUTED
        and isinstance(payload, dict)
        and cast("dict[str, object]", payload).get("command") == "drop_beans"
    ]
    assert command["reason"] == DropReason.HUMAN_PROTECTION.value
    assert command["target_outcome"] == "interrupted_unmet"
    assert h.log.index("drop_beans") < h.log.index("persist_snapshot")
    threshold_read = h.sink.snapshots[-1]
    assert threshold_read is not None
    assert threshold_read.bean_temp_c == 196
    assert not threshold_read.first_crack_detected
    await h.turn(bean=196)
    assert h.commands == ["drop_beans"]
    if advisor is not None:
        assert len(advisor.contexts) == 1
        assert [entry.status for entry in h.sink.advisor_decisions] == ["provider_error"]


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
        (record,) = [
            cast("dict[str, object]", payload)
            for kind, payload in h.emitter.events
            if kind is RoastEventKind.COMMAND_EXECUTED
            and isinstance(payload, dict)
            and cast("dict[str, object]", payload).get("command") == "drop_beans"
        ]
        assert record["target_outcome"] == "interrupted_unmet"
        _assert_dispatch_interval(record, duration=0.0)


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
        h.clock.advance(0.5)
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
    unknown = _drop_outcome_event(h, "unknown")
    _assert_dispatch_interval(unknown, duration=0.5)
    await h.controller.operator_drop_beans()
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.commands.count("drop_beans") == 1
    assert h.actuator.containments == 0
    refused = _drop_outcome_event(h, "refused_unknown_prior_drop")
    assert refused["physical_intervention_required"] is True
    assert "dispatch_started_at_utc" not in refused
    assert h.controller.snapshot().physical_intervention_required
    previous_commands = list(h.actuator.calls)
    assert h.controller._pending_fail_safe is None  # pyright: ignore[reportPrivateUsage]
    await h.turn(bean=185)
    assert h.actuator.calls == previous_commands
    assert "set_targets" not in h.commands
    assert h.controller._pending_fail_safe is None  # pyright: ignore[reportPrivateUsage]
    assert h.ownership.lifecycle_blocked
    assert h.controller._human_run is not None  # pyright: ignore[reportPrivateUsage]
    assert h.controller._human_run.command is CommandDisposition.UNKNOWN  # pyright: ignore[reportPrivateUsage]


def _drop_outcome_event(h: HumanHarness, outcome: str) -> dict[str, object]:
    (record,) = [
        cast("dict[str, object]", payload)
        for _, payload in h.emitter.events
        if isinstance(payload, dict)
        and cast("dict[str, object]", payload).get("command") == "drop_beans"
        and cast("dict[str, object]", payload).get("outcome") == outcome
    ]
    return record


def _assert_dispatch_interval(payload: dict[str, object], *, duration: float) -> None:
    start = payload["dispatch_started_at_utc"]
    end = payload["dispatch_ended_at_utc"]
    assert isinstance(start, str) and isinstance(end, str)
    started, ended = datetime.fromisoformat(start), datetime.fromisoformat(end)
    assert started.utcoffset() == ended.utcoffset() == timedelta(0)
    assert ended >= started
    assert payload["dispatch_duration_seconds"] == duration


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
    await _assert_te11_refusal(latched=latched, reason=HumanCallRefusal.ADMISSION_CLOSED)


@pytest.mark.parametrize("latched", [False, True])
async def test_te11_duplicate_read_has_the_same_non_failure_effects(latched: bool) -> None:
    await _assert_te11_refusal(latched=latched, reason=HumanCallRefusal.READ_ALREADY_PENDING)


async def _assert_te11_refusal(*, latched: bool, reason: HumanCallRefusal) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    if latched:
        h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    before = h.controller.phase
    evaluations = list(h.sink.evaluations)
    old_reads = h.actuator.reads
    h.actuator.calls.clear()
    h.controller._consecutive_read_failures = 1  # pyright: ignore[reportPrivateUsage]
    if reason is HumanCallRefusal.ADMISSION_CLOSED:
        h.fence.close(AdmissionCloseReason.DEVICE_RESPAWN)
    with (
        h.ownership.hold(PendingHumanCall.READ)
        if reason is HumanCallRefusal.READ_ALREADY_PENDING
        else nullcontext()
    ):
        refused = await h.controller._read_human_call()  # pyright: ignore[reportPrivateUsage]
        assert refused == RefusedHumanCall(PendingHumanCall.READ, reason)
        await h.turn(bean=196)
        assert h.actuator.reads == old_reads
        assert h.ownership.pending(PendingHumanCall.READ) is (
            reason is HumanCallRefusal.READ_ALREADY_PENDING
        )
    assert h.controller.phase is before
    assert h.sink.evaluations == evaluations
    assert h.controller._consecutive_read_failures == 1  # pyright: ignore[reportPrivateUsage]
    assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
    assert h.controller._accepted_readings.recheck() is ReadingFreshness.INVALID  # pyright: ignore[reportPrivateUsage]
    assert h.actuator.calls == []
    assert h.actuator.containments == 0
    if reason is HumanCallRefusal.ADMISSION_CLOSED:
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
    h = await make_human(start=action is not WriteClass.SESSION_START)
    if action is not WriteClass.SESSION_START:
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
    assert await h.controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
        generation, write, write_class=action
    ) == (True, None)
    assert attempted.is_set()
    attempted.clear()
    blocker = asyncio.create_task(h.arbiter.run_routine(generation, occupied))
    await asyncio.wait_for(entered.wait(), 1)
    queued = asyncio.create_task(
        h.controller._run_routine_write(generation, write, write_class=action)  # pyright: ignore[reportPrivateUsage]
    )
    await asyncio.sleep(0)
    assert not queued.done() and not attempted.is_set()
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
        ContainmentOutcome.UNKNOWN_TRANSPORT,
        ContainmentOutcome.UNKNOWN_DRIVER_RESULT,
        ContainmentOutcome.NOT_ATTEMPTED,
    ],
)
async def test_t46b_containment_budget_does_not_reset(
    outcome: ContainmentOutcome,
) -> None:
    child_teardown_unconfirmed = False
    h = await make_human(teardown_unconfirmed=lambda: child_teardown_unconfirmed)
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.actuator.calls.clear()
    owner = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert owner is not None

    async def contain() -> ContainmentResult:
        reasons = {
            ContainmentOutcome.REFUSED_IDENTITY: ContainmentReason.NOT_LATEST_SESSION,
            ContainmentOutcome.REFUSED_CAPABILITY: ContainmentReason.CAPABILITY_ABSENT,
            ContainmentOutcome.REFUSED_BUSY: ContainmentReason.COMMAND_IN_PROGRESS,
            ContainmentOutcome.UNKNOWN_COMPLETION: ContainmentReason.PROJECTION_UNCONFIRMED,
            ContainmentOutcome.UNKNOWN_DRIVER_RESULT: ContainmentReason.DRIVER_RAISED,
        }
        h.clock.advance(0.375)
        return ContainmentResult(
            outcome,
            reasons.get(outcome),
            evidence_heat_zero_commanded=True
            if outcome is ContainmentOutcome.COMMAND_STATE_RETURNED
            else None,
            evidence_is_pre_fallback=outcome is ContainmentOutcome.UNKNOWN_COMPLETION,
        )

    h.actuator.contain = contain
    result = await h.controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
        generation=h.arbiter.generation
    )
    assert result.outcome is outcome
    assert owner.command is (
        CommandDisposition.UNKNOWN if outcome.is_unknown else CommandDisposition.SETTLED
    )
    (record,) = [
        cast("dict[str, object]", payload)
        for _, payload in h.emitter.events
        if isinstance(payload, dict)
        and cast("dict[str, object]", payload).get("command") == "contain_heat_fixed_zero"
    ]
    _assert_dispatch_interval(record, duration=0.375)

    async def stays_consumed() -> None:
        assert owner.containment is ContainmentAttempt.CONSUMED
        retry = await h.controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
            generation=h.arbiter.generation
        )
        assert retry.outcome is ContainmentOutcome.NOT_ATTEMPTED
        assert h.actuator.containments == 1

    await stays_consumed()
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    await stays_consumed()
    await h.turn(bean=185)
    await h.turn(bean=185)
    assert h.controller._accepted_readings.project_freshness() is ReadingFreshness.FRESH  # pyright: ignore[reportPrivateUsage]
    await stays_consumed()
    child_teardown_unconfirmed = True
    await stays_consumed()
    child_teardown_unconfirmed = False
    await stays_consumed()
    h.actuator.epoch += 1
    await stays_consumed()
    h.actuator.epoch -= 1
    await stays_consumed()
    prior_session = h.actuator.session_id
    h.actuator.session_id = "different-synthetic-session"
    await stays_consumed()
    h.actuator.session_id = prior_session
    await stays_consumed()
    resumed = await h.controller.human_operator_continue(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert resumed is (not outcome.is_unknown)
    await stays_consumed()
    if not resumed:
        assert await h.controller.human_operator_continue(RoastPhase.COOLING)
        await stays_consumed()
    await h.controller.recover_from_restart(h.controller.phase)
    await stays_consumed()
    assert h.actuator.calls == []


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
    h.controller._adopt_applied_state(  # pyright: ignore[reportPrivateUsage]
        AppliedRoasterState(heat_level_percent=0, fan_level_percent=0, cooling_on=False)
    )
    h.actuator.calls.clear()
    h.sink.evaluations.clear()
    await h.turn(bean=bean)
    if bean >= 196:
        assert h.commands == ["drop_beans"]
        assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
        assert h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]
    else:
        assert not h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
        assert not h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]
        assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
        assert h.commands == ["set_heat", "set_fan"]
        assert any(
            row.rule == "command_phase_validity" and row.verdict is SafetyVerdict.ALLOW
            for row in h.sink.evaluations
        )


@pytest.mark.parametrize("endpoint", list(EndpointDisposition))
@pytest.mark.parametrize("owner", [None, *EndpointOwner])
async def test_tf1_faulted_has_no_cooling_edge(
    endpoint: EndpointDisposition, owner: EndpointOwner | None
) -> None:
    h = await make_human()
    h.controller.transition_to(RoastPhase.FAULTED)
    run = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert run is not None
    run.endpoint = endpoint
    run.endpoint_owner = owner
    assert not h.controller.can_transition(RoastPhase.COOLING)


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
    assert h.controller.phase is RoastPhase.FAULTED
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
    assert snapshot.phase is RoastPhase.FAULTED
    assert h.controller._latched_verdict is SafetyVerdict.RECOVERY  # pyright: ignore[reportPrivateUsage]
    assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
    assert (
        RoastEventKind.RECOVERY_REQUIRED,
        {"endpoint": "withheld", "physical_intervention_required": True},
    ) in h.emitter.events
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


@pytest.mark.parametrize("persisted_phase", [RoastPhase.DEVELOPMENT, RoastPhase.FAULTED])
async def test_t49_restart_does_not_replay_heat_or_drop_at_blocked_sink(
    monkeypatch: pytest.MonkeyPatch, persisted_phase: RoastPhase
) -> None:
    h = await make_human(start=False)
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.sink.persist_evaluation

    async def persist(evaluation: SafetyEvaluation) -> int | None:
        entered.set()
        await release.wait()
        return await original(evaluation)

    monkeypatch.setattr(h.sink, "persist_evaluation", persist)
    restart = (
        h.controller.recover_into_faulted
        if persisted_phase is RoastPhase.FAULTED
        else h.controller.recover_from_restart
    )
    recovering = asyncio.create_task(restart(persisted_phase))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert h.controller.snapshot().human_endpoint is EndpointDisposition.HISTORY_UNKNOWN
        assert h.controller.snapshot().human_command_disposition is CommandDisposition.UNKNOWN
        assert h.actuator.calls == []
        assert h.actuator.reads == h.actuator.containments == 0
    finally:
        release.set()
        await recovering
    assert h.controller.snapshot().physical_intervention_required
    assert not await h.controller.human_operator_continue(RoastPhase.DEVELOPMENT)
    await h.controller.operator_drop_beans()
    assert h.actuator.calls == []


async def test_t50_machine_fc_and_post_endpoint_human_note_do_not_advance_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


async def test_unbound_t0_breaks_charge_debounce_before_bound_confirmation() -> None:
    h = await make_human(charge_debounce=2)
    await h.turn(bean=180, t0=True)
    h.actuator.bound = False
    await h.turn(bean=196, t0=True)
    assert h.controller.phase is RoastPhase.PREHEATING
    assert not h.controller.snapshot().charge_detected
    assert RoastEventKind.T0_DETECTED not in h.emitter.kinds()
    h.actuator.bound = True
    await h.turn(bean=196, t0=True)
    assert h.controller.phase is RoastPhase.PREHEATING
    assert not h.controller.snapshot().charge_detected
    assert h.commands.count("drop_beans") == 0
    await h.turn(bean=196, t0=True)
    assert h.controller.snapshot().charge_detected
    assert h.controller.phase is RoastPhase.COOLING
    assert h.emitter.kinds().count(RoastEventKind.T0_DETECTED) == 1
    assert h.commands.count("drop_beans") == 1


async def test_unbound_t0_cannot_charge_but_still_reaches_existing_hard_limit() -> None:
    h = await make_human()
    await h.turn(bean=180)
    h.actuator.bound = False
    await h.turn(bean=260, t0=True)
    assert not h.controller.snapshot().charge_detected
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.commands.count("emergency_stop") == 1
    assert h.commands.count("drop_beans") == 0


async def test_unbound_temperature_preserves_prior_bound_charge() -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    assert h.controller.snapshot().charge_detected
    h.actuator.bound = False
    await h.turn(bean=196, t0=False)
    assert h.controller.snapshot().charge_detected
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.commands.count("drop_beans") == 0


@pytest.mark.parametrize("latched", [False, True])
async def test_t56_human_read_failure_counts_once_outside_latch(latched: bool) -> None:
    h = await make_human()
    if latched:
        h.controller.transition_to(RoastPhase.FAULTED)

    async def failed_read() -> None:
        raise ConnectionError("synthetic detached child")

    h.actuator.collect = failed_read
    await h.turn(bean=185)
    assert h.actuator.reads == 1
    assert h.controller._consecutive_read_failures == (0 if latched else 1)  # pyright: ignore[reportPrivateUsage]
    assert h.controller._accepted_readings.project_freshness() is ReadingFreshness.INVALID  # pyright: ignore[reportPrivateUsage]
    assert "set_heat" not in h.commands
    assert "drop_beans" not in h.commands


async def test_t56_cancelled_read_breaks_history_and_releases_only_read() -> None:
    h = await make_human()
    await h.turn(bean=180)
    entered = asyncio.Event()

    async def held_read() -> None:
        entered.set()
        await asyncio.Event().wait()

    h.actuator.collect = held_read
    task = asyncio.create_task(h.turn(bean=185))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not h.ownership.pending(PendingHumanCall.READ)
    assert h.controller._consecutive_read_failures == 0  # pyright: ignore[reportPrivateUsage]
    assert h.controller._accepted_readings.project_freshness() is ReadingFreshness.INVALID  # pyright: ignore[reportPrivateUsage]
    assert h.controller.snapshot().human_command_disposition is CommandDisposition.SETTLED


@pytest.mark.parametrize("outcome", ["cancel", "timeout", "malformed", "commanded"])
async def test_td4_pending_containment_classifies_before_releasing_ownership(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from collections.abc import Generator
    from contextlib import contextmanager

    h = await make_human()
    entered = asyncio.Event()
    release = asyncio.Event()
    before_exit: list[CommandDisposition | None] = []
    base_hold = h.ownership.hold

    @contextmanager
    def observe_exit(kind: PendingHumanCall) -> Generator[None, None, None]:
        with base_hold(kind):
            try:
                yield
            finally:
                assert h.ownership.pending(PendingHumanCall.CONTAINMENT)
                before_exit.append(h.controller.snapshot().human_command_disposition)

    async def held_containment() -> ContainmentResult:
        entered.set()
        await release.wait()
        if outcome == "timeout":
            raise TimeoutError()
        if outcome == "malformed":
            return cast("ContainmentResult", object())
        return ContainmentResult(
            ContainmentOutcome.COMMAND_STATE_RETURNED, evidence_heat_zero_commanded=True
        )

    monkeypatch.setattr(h.ownership, "hold", observe_exit)
    h.actuator.contain = held_containment
    task = asyncio.create_task(
        h.controller._contain_human_heat(generation=h.arbiter.generation)  # pyright: ignore[reportPrivateUsage]
    )
    await asyncio.wait_for(entered.wait(), 1)
    assert h.ownership.pending(PendingHumanCall.CONTAINMENT)
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        result = await task
        assert (
            result.outcome
            is {
                "timeout": ContainmentOutcome.UNKNOWN_TRANSPORT,
                "malformed": ContainmentOutcome.UNKNOWN_MALFORMED_RESPONSE,
                "commanded": ContainmentOutcome.COMMAND_STATE_RETURNED,
            }[outcome]
        )
    assert not h.ownership.pending(PendingHumanCall.CONTAINMENT)
    assert before_exit == [
        CommandDisposition.SETTLED if outcome == "commanded" else CommandDisposition.UNKNOWN
    ]
    assert h.actuator.containments == 1


async def test_t38_issued_operator_drop_joins_later_requests() -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    await h.controller.operator_drop_beans()
    assert h.controller.phase is RoastPhase.COOLING
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.ISSUED
    await h.controller.operator_drop_beans()
    assert h.commands.count("drop_beans") == 1
    assert any(
        isinstance(payload, dict)
        and cast("dict[str, object]", payload).get("outcome") == "joined_protective_endpoint"
        for _, payload in h.emitter.events
    )


async def test_l1_continuation_rechecks_exact_run_evidence_before_clearing() -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    assert await h.controller.human_operator_continue(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    h.actuator.session_id = "synthetic-new-identity"
    h.actuator.calls.clear()
    await h.turn(bean=185)
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
    assert h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]
    assert h.commands == []


@pytest.mark.parametrize("kind", [WriteClass.HEAT, WriteClass.FAN, WriteClass.SESSION_START])
async def test_te17_unconfirmed_owner_discards_each_guarded_write(kind: WriteClass) -> None:
    unconfirmed = False
    h = await make_human(
        teardown_unconfirmed=lambda: unconfirmed, start=kind is not WriteClass.SESSION_START
    )
    if kind is not WriteClass.SESSION_START:
        await h.turn(bean=180)
        await h.turn(bean=185, t0=True)
    calls: list[WriteClass] = []

    async def dispatch() -> None:
        calls.append(kind)

    unconfirmed = True
    discarded = await h.controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
        h.arbiter.generation, dispatch, write_class=kind
    )
    assert discarded == (False, None)
    assert calls == []
    unconfirmed = False
    admitted = await h.controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
        h.arbiter.generation, dispatch, write_class=kind
    )
    assert admitted == (True, None)
    assert calls == [kind]


async def test_te17_unconfirmed_owner_leaves_non_heat_and_refuses_containment() -> None:
    unconfirmed = False
    h = await make_human(teardown_unconfirmed=lambda: unconfirmed)
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    unconfirmed = True
    await h.controller.operator_start_cooling()
    assert h.commands.count("start_cooling") == 1
    result = await h.controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
        generation=h.arbiter.generation
    )
    assert result.outcome is ContainmentOutcome.NOT_ATTEMPTED
    assert h.actuator.containments == 0
    assert not h.ownership.pending(PendingHumanCall.CONTAINMENT)


@pytest.mark.parametrize("malformed", [False, True])
async def test_superseded_in_flight_drop_cannot_release_new_phase_owner(
    monkeypatch: pytest.MonkeyPatch, malformed: bool
) -> None:
    h = await make_human()
    original = h.actuator.drop_beans
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_drop() -> AppliedRoasterState | None:
        entered.set()
        await release.wait()
        result = await original()
        return None if malformed else result

    monkeypatch.setattr(h.actuator, "drop_beans", blocked_drop)
    await h.turn(bean=180)
    turn = asyncio.create_task(h.turn(bean=196, t0=True))
    await asyncio.wait_for(entered.wait(), 1)
    h.controller.admit_emergency_stop()
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    stop = asyncio.create_task(
        h.controller.operator_emergency_stop("supersede dispatched drop", already_admitted=True)
    )
    release.set()
    await asyncio.gather(turn, stop)
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.controller.snapshot().human_endpoint is (
        EndpointDisposition.UNKNOWN if malformed else EndpointDisposition.IN_FLIGHT
    )
    assert h.controller.snapshot().human_command_disposition is (
        CommandDisposition.UNKNOWN if malformed else CommandDisposition.SETTLED
    )
    assert h.commands.count("drop_beans") == h.commands.count("emergency_stop") == 1
    await h.controller.operator_drop_beans()
    assert h.commands.count("drop_beans") == 1


@pytest.mark.parametrize("binding", ["missing", "stale", "fresh"])
async def test_t39_exact_fault_executor_uses_the_human_drop_owner(
    tmp_path: Path, binding: str
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.FAULTED)
    if binding == "stale":
        h.clock.advance(20)
    writes: list[OperatorAction] = []

    class FaultPort:
        async def read_fault_control_state(self, session_id: str) -> FaultControlState:
            return FaultControlState(
                session_id=session_id,
                mcp_phase="fault",
                active=False,
                device_connected=True,
                heat_level_percent=0,
                fan_level_percent=0,
                cooling_on=False,
                beans_added=True,
                beans_dropped=bool(writes),
            )

        async def read_latest_fault_control_state(self) -> FaultControlState:
            return await self.read_fault_control_state(h.actuator.session_id)

        async def write_fault_control(
            self, action: OperatorAction, *, expected_session_id: str
        ) -> None:
            assert expected_session_id == h.actuator.session_id
            writes.append(action)

    store = RoastStore(tmp_path / "synthetic-fault-controls.sqlite3")
    await store.initialize()
    try:
        await store.create_run(
            run_id="synthetic-run",
            profile=PROFILE,
            config=AppConfig(),
            agent_phase=RoastPhase.FAULTED,
        )
        executor = FaultControlExecutor(
            SafetyPolicy(SafetyLimits()),
            FaultPort(),
            arbiter=h.arbiter,
            trace_sink=store,
            human_drop_controller=lambda _: h.controller if binding != "missing" else None,
        )
        result = await executor.execute(
            OperatorAction.DROP_BEANS,
            h.actuator.session_id,
            run_id="synthetic-run",
            is_current_generation=lambda: True,
        )
        assert (result is not None) is (binding == "fresh")
        assert writes == ([OperatorAction.DROP_BEANS] if binding == "fresh" else [])
        assert await executor.settle_for_shutdown()
    finally:
        await store.close()


async def test_t32_human_legacy_drop_requests_do_not_duplicate_the_endpoint() -> None:
    h = await make_human(advisor=FakeAdvisor([decision(drop=True)]))
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    await h.controller.operator_mark_first_crack()
    assert h.controller.phase is RoastPhase.DEVELOPMENT
    await h.turn(bean=195)
    assert h.commands.count("drop_beans") == 0
    await h.turn(bean=196)
    assert h.commands.count("drop_beans") == 1
    assert (
        await h.controller._execute_deterministic_drop(  # pyright: ignore[reportPrivateUsage]
            DropReason.CEILING_GUARD, generation=h.arbiter.generation
        )
        is False
    )
    assert h.commands.count("drop_beans") == 1


async def test_pending_scope_cannot_be_claimed_twice_or_replaced() -> None:
    owner = HumanRunOwnership()
    active = owner.claim_run("active")
    active.durably_closed = True
    with owner.hold(PendingHumanCall.READ):
        with (
            pytest.raises(RuntimeError, match="already pending"),
            owner.hold(PendingHumanCall.READ),
        ):
            pytest.fail("duplicate scope was opened")
        with pytest.raises(RuntimeError, match="unresolved HUMAN ownership"):
            owner.claim_run("replacement")
    assert owner.claim_run("replacement").run_id == "replacement"


@pytest.mark.parametrize("applied", [True, False, "timeout", "exception"])
async def test_te24_closure_does_not_reclassify_started_drop(
    monkeypatch: pytest.MonkeyPatch, applied: bool | str
) -> None:
    h = await make_human()
    original = h.actuator.drop_beans
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_drop() -> AppliedRoasterState | None:
        entered.set()
        await release.wait()
        result = await original()
        if applied == "timeout":
            raise TimeoutError("synthetic late response timeout")
        if applied == "exception":
            raise RuntimeError("synthetic late operation failure")
        return result if applied is True else None

    monkeypatch.setattr(h.actuator, "drop_beans", held_drop)
    await h.turn(bean=180)
    turn = asyncio.create_task(h.turn(bean=196, t0=True))
    await asyncio.wait_for(entered.wait(), 1)
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.IN_FLIGHT
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    release.set()
    await turn
    assert h.controller.snapshot().human_endpoint is (
        EndpointDisposition.ISSUED if applied is True else EndpointDisposition.UNKNOWN
    )
    assert h.commands.count("drop_beans") == 1
    assert h.actuator.containments == 0


async def test_te24_closure_allows_started_heat_operation_to_return() -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.actuator.calls.clear()
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_heat() -> None:
        entered.set()
        await release.wait()
        await h.actuator.set_heat(20)

    dispatched = asyncio.create_task(
        h.controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
            h.arbiter.generation, held_heat, write_class=WriteClass.HEAT
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    release.set()
    assert await dispatched == (True, None)
    assert h.commands == ["set_heat"]


@pytest.mark.parametrize("kind", [WriteClass.HEAT, WriteClass.FAN])
@pytest.mark.parametrize(
    "blocked_by",
    ["endpoint", "freshness", "durably_closed", "_routine_inhibited", "_protection_check_pending"],
)
async def test_t37_queued_lever_rechecks_endpoint_and_freshness(
    monkeypatch: pytest.MonkeyPatch, kind: WriteClass, blocked_by: str
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    occupied, release = asyncio.Event(), asyncio.Event()

    async def held() -> None:
        occupied.set()
        await release.wait()

    commands: list[WriteClass] = []

    async def lever() -> None:
        commands.append(kind)

    generation = h.arbiter.generation
    assert await h.controller._run_routine_write(  # pyright: ignore[reportPrivateUsage]
        generation, lever, write_class=kind
    ) == (True, None)
    assert commands == [kind]
    commands.clear()
    prior = asyncio.create_task(h.arbiter.run_routine(generation, held))
    await asyncio.wait_for(occupied.wait(), 1)
    lever_task = asyncio.create_task(
        h.controller._run_routine_write(generation, lever, write_class=kind)  # pyright: ignore[reportPrivateUsage]
    )
    await asyncio.sleep(0)
    assert not lever_task.done()
    drop: asyncio.Task[None] | None = None
    try:
        if blocked_by == "endpoint":
            drop = asyncio.create_task(h.turn(bean=196))
            await asyncio.sleep(0)
            assert h.controller.snapshot().human_endpoint is EndpointDisposition.ADMITTED
        elif blocked_by == "freshness":
            h.clock.advance(20)
        elif blocked_by == "durably_closed":
            h.controller.note_human_run_durably_closed()
        else:
            monkeypatch.setattr(h.controller, blocked_by, True)
    finally:
        release.set()
        await prior
        if drop is not None:
            await drop
    assert h.fence.closed_reason is None
    assert await lever_task == (False, None)
    assert commands == []
    assert h.commands.count("drop_beans") == int(blocked_by == "endpoint")


@pytest.mark.parametrize("invalid_et", [False, True])
async def test_unusable_bound_thermal_pair_uses_human_validity_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
    invalid_et: bool,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    original = h.actuator.read_human_evidence

    async def unusable_pair() -> HumanReadEvidence:
        evidence = await original()
        if invalid_et:
            return replace(evidence, telemetry=None, env=RawTemperature.INVALID, env_temp_c=None)
        return replace(evidence, telemetry=None, bean=RawTemperature.INVALID, bean_temp_c=None)

    monkeypatch.setattr(h.actuator, "read_human_evidence", unusable_pair)
    h.actuator.calls.clear()
    await h.turn(bean=196)
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.controller.snapshot().telemetry is None
    assert h.controller.snapshot().charge_detected
    assert h.actuator.calls == []
    if invalid_et:
        assert "BT valid, ET invalid" in h.sink.evaluations[-1].reason
    assert h.controller._pending_fail_safe is None  # pyright: ignore[reportPrivateUsage]
    await h.turn(bean=196)
    assert h.actuator.calls == []
    assert h.controller._pending_fail_safe is None  # pyright: ignore[reportPrivateUsage]


async def test_superseded_pending_read_cannot_write_snapshot_into_emergency_turn() -> None:
    h = await make_human()
    await h.turn(bean=180)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        entered.set()
        await release.wait()

    h.actuator.collect = hold
    before = list(h.sink.snapshots)
    pending = asyncio.create_task(h.turn(bean=196, t0=True))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        assert await h.controller.operator_emergency_stop("synthetic pending READ")
    finally:
        release.set()
        await pending
    assert not h.controller.snapshot().charge_detected
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.sink.snapshots == before
    assert h.commands.count("drop_beans") == 0


async def test_late_in_flight_fc_cannot_become_canonical_after_endpoint_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    entered, release = asyncio.Event(), asyncio.Event()
    actual_mark = h.actuator.mark_first_crack

    async def held_mark() -> None:
        entered.set()
        await release.wait()
        await actual_mark()

    monkeypatch.setattr(h.actuator, "mark_first_crack", held_mark)
    mark = asyncio.create_task(h.controller.operator_mark_first_crack())
    await asyncio.wait_for(entered.wait(), 1)
    drop = asyncio.create_task(h.turn(bean=196))
    try:
        await asyncio.sleep(0)
        assert h.controller.snapshot().human_endpoint is EndpointDisposition.ADMITTED
    finally:
        release.set()
        await asyncio.gather(mark, drop)
    assert h.commands.count("mark_first_crack") == h.commands.count("drop_beans") == 1
    assert h.controller.phase is RoastPhase.COOLING
    assert h.controller.snapshot().development_percent is None
    assert RoastEventKind.FIRST_CRACK not in h.emitter.kinds()


@pytest.mark.parametrize("charged", [False, True])
@pytest.mark.parametrize(
    ("counter_class", "freshness"),
    [
        (CounterClass.PRESENT, ReadingFreshness.INVALID),
        (CounterClass.ABSENT, ReadingFreshness.UNPROVEN),
        (CounterClass.INVALID, ReadingFreshness.INVALID),
    ],
)
@pytest.mark.parametrize("raw_class", [RawTemperature.ABSENT, RawTemperature.INVALID])
async def test_tb1_tb3_tb5_unusable_thermal_pair_is_successful_without_authority(
    monkeypatch: pytest.MonkeyPatch,
    charged: bool,
    counter_class: CounterClass,
    freshness: ReadingFreshness,
    raw_class: RawTemperature,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=charged)
    h.actuator.calls.clear()
    h.sink.evaluations.clear()
    baseline_reads = h.actuator.reads
    h.controller._consecutive_read_failures = 1  # pyright: ignore[reportPrivateUsage]
    original = h.actuator.read_human_evidence

    async def unusable_pair() -> HumanReadEvidence:
        evidence = await original()
        counters = (
            evidence.counters
            if counter_class is CounterClass.PRESENT
            else CounterEvidence(None, None, counter_class)
        )
        return replace(
            evidence, telemetry=None, bean=raw_class, bean_temp_c=None, counters=counters
        )

    monkeypatch.setattr(h.actuator, "read_human_evidence", unusable_pair)
    await h.turn(bean=260, t0=True)
    assert h.actuator.reads == baseline_reads + 1
    assert h.controller._consecutive_read_failures == 0  # pyright: ignore[reportPrivateUsage]
    (validity,) = h.sink.evaluations
    assert validity.rule == ("human_reading_invalid" if charged else "missing_telemetry")
    assert validity.verdict is (SafetyVerdict.RECOVERY if charged else SafetyVerdict.FAULT)
    assert f"BT {raw_class.value}, ET valid, accepted reading {freshness.value}" in validity.reason
    assert h.controller.phase is (
        RoastPhase.OPERATOR_RECOVERY_REQUIRED if charged else RoastPhase.FAULTED
    )
    assert h.controller.snapshot().telemetry is None
    assert h.controller.snapshot().charge_detected is charged
    assert h.actuator.calls == []
    assert not h.controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("case", "expected_rule", "expected_verdict"),
    [
        ("stalled_counters", "max_bean_temp", SafetyVerdict.EMERGENCY_STOP),
        ("unbound", "max_bean_temp", SafetyVerdict.EMERGENCY_STOP),
        ("stale_session_clock", "human_reading_invalid", SafetyVerdict.RECOVERY),
    ],
)
async def test_tb4_hard_limit_precedence_depends_on_validity_before_freshness(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_rule: str,
    expected_verdict: SafetyVerdict,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    assert h.controller.snapshot().charge_detected
    h.sink.evaluations.clear()
    h.actuator.calls.clear()
    original = h.actuator.read_human_evidence

    async def read() -> HumanReadEvidence:
        previous_status = h.actuator.status
        evidence = await original()
        if case == "stalled_counters":
            return replace(
                evidence, counters=CounterEvidence(previous_status, 0, CounterClass.PRESENT)
            )
        if case == "unbound":
            return replace(evidence, bound_child_epoch=None)
        assert evidence.telemetry is not None
        return replace(
            evidence, telemetry=evidence.telemetry.model_copy(update={"age_seconds": 50.0})
        )

    monkeypatch.setattr(h.actuator, "read_human_evidence", read)
    if case == "stalled_counters":

        async def delay() -> None:
            h.clock.advance(20)

        h.actuator.collect = delay
    await h.turn(bean=260)
    assert h.controller.snapshot().charge_detected
    freshness = {
        "stalled_counters": ReadingFreshness.STALE,
        "unbound": ReadingFreshness.INVALID,
        "stale_session_clock": ReadingFreshness.FRESH,
    }[case]
    assert f"accepted reading {freshness.value}" in h.sink.evaluations[0].reason
    rules = [evaluation.rule for evaluation in h.sink.evaluations]
    assert rules == (
        ["all_clear", expected_rule]
        if expected_verdict is SafetyVerdict.EMERGENCY_STOP
        else [expected_rule]
    )
    assert h.sink.evaluations[-1].verdict is expected_verdict
    if expected_verdict is SafetyVerdict.EMERGENCY_STOP:
        assert h.commands == ["emergency_stop"]
        assert h.actuator.containments == 0
    else:
        assert h.commands == []
        assert h.actuator.containments == 1
        assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED


@pytest.mark.parametrize("latched", [False, True])
async def test_tb6_unbound_envelope_breaks_history_with_existing_read_counter(
    latched: bool,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185)
    if latched:
        h.controller.transition_to(RoastPhase.FAULTED)
    h.controller._consecutive_read_failures = 1  # pyright: ignore[reportPrivateUsage]
    h.actuator.bound = False
    await h.turn(bean=186)
    assert h.controller._consecutive_read_failures == (1 if latched else 0)  # pyright: ignore[reportPrivateUsage]
    assert h.controller._accepted_readings.project_freshness() is ReadingFreshness.INVALID  # pyright: ignore[reportPrivateUsage]
    h.actuator.bound = True
    await h.turn(bean=187)
    assert h.controller._accepted_readings.project_freshness() is ReadingFreshness.UNPROVEN  # pyright: ignore[reportPrivateUsage]
    assert h.actuator.reads == 4


@pytest.mark.parametrize(
    "target", [RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.DEVELOPMENT]
)
@pytest.mark.parametrize(
    "endpoint", [e for e in EndpointDisposition if e is not EndpointDisposition.NONE]
)
async def test_t40_every_endpoint_disposition_blocks_heat_continuation(
    endpoint: EndpointDisposition, target: RoastPhase
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    owner = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert owner is not None and owner.command is CommandDisposition.SETTLED
    owner.endpoint = endpoint
    h.actuator.calls.clear()
    assert not await h.controller.human_operator_continue(target)
    assert h.sink.evaluations[-1].rule == "human_continuation"
    assert h.sink.evaluations[-1].verdict is SafetyVerdict.REJECT
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.actuator.calls == []


async def test_t45_operator_emergency_after_unknown_never_uses_ordinary_fail_safe() -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)

    async def unknown_containment() -> ContainmentResult:
        return ContainmentResult(ContainmentOutcome.UNKNOWN_TRANSPORT)

    h.actuator.contain = unknown_containment
    h.actuator.bound = False
    h.actuator.calls.clear()
    await h.turn(bean=186)
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    snapshot = h.controller.snapshot()
    assert snapshot.human_command_disposition is CommandDisposition.UNKNOWN
    assert snapshot.human_endpoint is EndpointDisposition.NONE
    assert snapshot.physical_intervention_required
    disclosure = snapshot.emergency_fallback_disclosure
    assert disclosure is not None
    assert "stop the drum and close the chute" in disclosure
    assert "does not establish physical evacuation" in disclosure
    await h.controller.operator_drop_beans()
    assert (
        _drop_outcome_event(h, "refused_unknown_prior_drop")["physical_intervention_required"]
        is True
    )
    assert h.actuator.containments == 1
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    await h.controller.tick()
    assert h.actuator.calls == []
    assert h.controller._pending_fail_safe is None  # pyright: ignore[reportPrivateUsage]
    assert await h.controller.operator_emergency_stop("synthetic stop after unknown")
    assert h.commands == ["emergency_stop"]
    assert h.controller.snapshot().human_command_disposition is CommandDisposition.UNKNOWN
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.ownership.lifecycle_blocked


@pytest.mark.parametrize("prior_owner", ["routine", "drop"])
async def test_t46c_containment_is_not_attempted_while_prior_command_in_flight(
    prior_owner: str,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    owner = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    entered, release = asyncio.Event(), asyncio.Event()

    async def held() -> None:
        entered.set()
        await release.wait()

    pending: asyncio.Task[tuple[bool, None]] | None = None
    if prior_owner == "routine":
        pending = asyncio.create_task(h.arbiter.run_routine(h.arbiter.generation, held))
        await asyncio.wait_for(entered.wait(), 1)
    else:
        owner.endpoint = EndpointDisposition.IN_FLIGHT
    try:
        result = await h.controller._contain_human_heat(  # pyright: ignore[reportPrivateUsage]
            generation=h.arbiter.generation
        )
        assert result.outcome is ContainmentOutcome.NOT_ATTEMPTED
        assert owner.containment is ContainmentAttempt.AVAILABLE
        assert h.actuator.containments == 0
    finally:
        release.set()
        if pending is not None:
            await pending


async def test_t30_refused_mcp_charge_cannot_latch_protective_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    original = SafetyPolicy.evaluate_event_source

    def evaluate(
        policy: SafetyPolicy, *, transition: Literal["t0", "first_crack"], source: RoastEventSource
    ) -> SafetyEvaluation:
        if transition == "t0" and source is RoastEventSource.MCP:
            return SafetyEvaluation(
                verdict=SafetyVerdict.REJECT,
                rule="event_source_validity",
                reason="synthetic refusal",
            )
        return original(policy, transition=transition, source=source)

    with monkeypatch.context() as rejected:
        rejected.setattr(SafetyPolicy, "evaluate_event_source", evaluate)
        await h.turn(bean=196, t0=True)
    assert h.controller.phase is RoastPhase.PREHEATING
    assert not h.controller.snapshot().charge_detected
    assert RoastEventKind.T0_DETECTED not in h.emitter.kinds()
    assert "drop_beans" not in h.commands
    assert any(
        row.rule == "event_source_validity" and row.verdict is SafetyVerdict.REJECT
        for row in h.sink.evaluations
    )
    await h.turn(bean=196, t0=True)
    assert h.commands.count("drop_beans") == 1
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.ISSUED


async def test_tb7_exact_raw_integer_drives_protection_beside_float_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await make_human()
    original = h.actuator.read_human_evidence

    async def raw_int_read() -> HumanReadEvidence:
        evidence = await original()
        assert evidence.telemetry is not None
        assert type(evidence.bean_temp_c) is int
        assert type(evidence.telemetry.bean_temp_c) is float
        return evidence

    monkeypatch.setattr(h.actuator, "read_human_evidence", raw_int_read)
    eligibility = Mock(wraps=protection_eligible)
    monkeypatch.setattr(controller_module, "protection_eligible", eligibility)
    await h.turn(bean=180)
    await h.turn(bean=196, t0=True)
    assert type(eligibility.call_args.kwargs["bean_temp_c"]) is int
    assert eligibility.call_args.kwargs["bean_temp_c"] == 196
    assert h.commands.count("drop_beans") == 1
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.ISSUED
    assert any(
        row.rule == "human_protective_drop" and row.verdict is SafetyVerdict.ALLOW
        for row in h.sink.evaluations
    )


async def test_t56_exhausted_read_failures_fault_without_routine_heat_off() -> None:
    h = await make_human()
    await h.turn(bean=180)
    h.actuator.calls.clear()
    h.sink.evaluations.clear()
    attempts = SafetyLimits().max_consecutive_mcp_failures

    async def failed_read() -> None:
        raise ConnectionError("synthetic genuine read failure")

    h.actuator.collect = failed_read
    for _ in range(attempts):
        await h.turn(bean=185)
    assert h.actuator.reads == attempts + 1
    assert h.controller._consecutive_read_failures == attempts  # pyright: ignore[reportPrivateUsage]
    assert h.sink.evaluations[-1].rule == "mcp_read_failures_exhausted"
    assert h.sink.evaluations[-1].verdict is SafetyVerdict.FAULT
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.actuator.calls == []
    assert h.actuator.containments == 0
    assert h.controller._pending_fail_safe is None  # pyright: ignore[reportPrivateUsage]
    evaluation_count = len(h.sink.evaluations)
    await h.turn(bean=185)
    assert h.controller._consecutive_read_failures == attempts  # pyright: ignore[reportPrivateUsage]
    assert len(h.sink.evaluations) == evaluation_count
    assert h.actuator.calls == []


@pytest.mark.parametrize("invalidated_by", ["identity", "emergency", "expiry"])
async def test_t43_continuation_rechecks_authority_after_audit_await(
    monkeypatch: pytest.MonkeyPatch, invalidated_by: str
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    h.actuator.calls.clear()
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.sink.persist_evaluation

    async def audit(evaluation: SafetyEvaluation) -> int | None:
        if evaluation.rule == "human_continuation":
            assert evaluation.verdict is SafetyVerdict.ALLOW
            entered.set()
            await release.wait()
        return await original(evaluation)

    monkeypatch.setattr(h.sink, "persist_evaluation", audit)
    continuation = asyncio.create_task(
        h.controller.human_operator_continue(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if invalidated_by == "identity":
            h.actuator.session_id = "unreconciled-replacement"
        elif invalidated_by == "emergency":
            assert await h.controller.operator_emergency_stop("superseded continuation")
        else:
            h.clock.advance(20)
    finally:
        release.set()
        admitted = await continuation
    assert not admitted
    assert h.commands == (["emergency_stop"] if invalidated_by == "emergency" else [])
    assert h.controller.phase is (
        RoastPhase.FAULTED
        if invalidated_by == "emergency"
        else RoastPhase.OPERATOR_RECOVERY_REQUIRED
    )
    assert not h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("source", ["cancelled_stop", "latched_escalation", "exact_fault_rearm"])
async def test_te25_other_existing_emergency_retry_sources_survive_closure(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)

    def forbidden_teardown_read() -> bool:
        pytest.fail("an admitted emergency retry read child-teardown evidence")

    async def failed_emergency(*, reason: str) -> AppliedRoasterState | None:
        raise TimeoutError(reason)

    if source == "cancelled_stop":
        entered = asyncio.Event()

        async def pending_emergency(*, reason: str) -> AppliedRoasterState | None:
            entered.set()
            await asyncio.Event().wait()
            pytest.fail(reason)

        monkeypatch.setattr(h.actuator, "emergency_stop", pending_emergency)
        h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
        stop = asyncio.create_task(h.controller.operator_emergency_stop("cancelled stop"))
        await asyncio.wait_for(entered.wait(), 1)
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop
    elif source == "latched_escalation":

        async def delay() -> None:
            h.clock.advance(20)

        h.actuator.collect = delay
        await h.turn(bean=186)
        assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED

        async def close_during_read() -> None:
            h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)

        monkeypatch.setattr(h.actuator, "emergency_stop", failed_emergency)
        h.actuator.collect = close_during_read
        await h.turn(bean=260)
    else:
        h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
        h.controller.transition_to(RoastPhase.FAULTED)
        h.controller.rearm_emergency_stop_fail_safe()

    assert h.controller.phase is RoastPhase.FAULTED
    assert h.fence.closed_reason is AdmissionCloseReason.APPLICATION_TEARDOWN
    assert h.controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(h.controller, "_teardown_unconfirmed", forbidden_teardown_read)
    h.actuator.calls.clear()
    await h.controller.tick()
    assert h.actuator.calls == [
        ("set_heat", {"heat_percent": 0}),
        ("set_fan", {"fan_percent": SafetyLimits().overrun_safe_fan_percent}),
    ]
    assert not await h.controller._set_targets(heat_percent=0, fan_percent=0)  # pyright: ignore[reportPrivateUsage]
    assert h.commands == ["set_heat", "set_fan"]


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize(
    ("method", "command", "setup_phase", "settled_phase"),
    [
        (
            "operator_mark_beans_added",
            "mark_beans_added",
            RoastPhase.PREHEATING,
            RoastPhase.PREHEATING,
        ),
        (
            "operator_mark_first_crack",
            "mark_first_crack",
            RoastPhase.ROASTING_PRE_FIRST_CRACK,
            RoastPhase.DEVELOPMENT,
        ),
        ("operator_start_cooling", "start_cooling", RoastPhase.FAULTED, RoastPhase.FAULTED),
        ("operator_stop_cooling", "stop_cooling", RoastPhase.FAULTED, RoastPhase.FAULTED),
    ],
)
async def test_te22_queued_non_heat_obeys_existing_ownership_without_fence_reads(
    monkeypatch: pytest.MonkeyPatch,
    uncertain: bool,
    method: str,
    command: str,
    setup_phase: RoastPhase,
    settled_phase: RoastPhase,
) -> None:
    h = await make_human()
    if setup_phase is RoastPhase.ROASTING_PRE_FIRST_CRACK:
        await h.turn(bean=180)
        await h.turn(bean=185, t0=True)
    elif setup_phase is RoastPhase.FAULTED:
        h.controller.transition_to(RoastPhase.FAULTED)
    h.actuator.calls.clear()
    callback = cast("Callable[[], Awaitable[None]]", getattr(h.controller, method))
    owner = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert owner is not None and h.controller.phase is setup_phase

    def forbidden() -> bool:
        pytest.fail("ordinary NON_HEAT checked a newly added admission predicate")

    async def invoke() -> None:
        await callback()

    with monkeypatch.context() as guarded:
        async with h.arbiter._condition:  # pyright: ignore[reportPrivateUsage]
            pending = asyncio.create_task(invoke())
            await asyncio.sleep(0)
            assert not pending.done()
            h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
            guarded.setattr(
                HumanCallAdmissionFence, "closed_reason", PropertyMock(side_effect=forbidden)
            )
            guarded.setattr(h.controller, "_teardown_unconfirmed", forbidden)
            if uncertain:
                owner.command = CommandDisposition.UNKNOWN
        await pending
    assert h.commands == ([] if uncertain else [command])
    assert h.controller.phase is (setup_phase if uncertain else settled_phase)


async def test_te20_automatic_routine_dispatch_does_not_consult_human_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actuator = FakeMCPClient()
    fence = HumanCallAdmissionFence()
    sink = RecordingSnapshotSink([])
    controller = RoastController(
        config=ControllerConfig(),
        safety=SafetyPolicy(SafetyLimits()),
        state_reader=actuator,
        command_executor=actuator,
        snapshot_sink=sink,
        event_emitter=EventSink(),
        clock=FakeClock(),
        authority=ControlAuthority.AUTOMATIC,
        human_admission=fence,
    )
    await controller.start_run(PROFILE)
    assert controller.phase is RoastPhase.PREHEATING
    actuator.calls.clear()
    fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)

    def forbidden() -> bool:
        pytest.fail("AUTOMATIC routine dispatch consulted guarded HUMAN admission")

    admission = Mock(side_effect=forbidden)
    fence_read = PropertyMock(side_effect=forbidden)
    monkeypatch.setattr(controller, "_human_actuation_admitted", admission)
    monkeypatch.setattr(controller, "_human_routine_permitted", admission)
    monkeypatch.setattr(controller, "_teardown_unconfirmed", admission)
    monkeypatch.setattr(HumanCallAdmissionFence, "closed_reason", fence_read)
    await controller.operator_mark_beans_added()
    assert actuator.calls == [("mark_beans_added", {})]
    assert sink.evaluations[-1].verdict is SafetyVerdict.ALLOW
    admission.assert_not_called()
    fence_read.assert_not_called()


@pytest.mark.parametrize("target", [RoastPhase.IDLE, RoastPhase.COMPLETE, RoastPhase.COOLING])
async def test_human_nonheating_continuation_transitions_without_hardware_command(
    target: RoastPhase,
) -> None:
    h = await make_human()
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    h.actuator.calls.clear()
    assert await h.controller.human_operator_continue(target)
    assert h.controller.phase is target
    assert h.actuator.calls == []
    assert not h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("inhibited", [False, True])
@pytest.mark.parametrize("command", list(CommandDisposition))
@pytest.mark.parametrize("endpoint", list(EndpointDisposition))
async def test_t45_emergency_is_independent_of_every_human_ownership_value(
    monkeypatch: pytest.MonkeyPatch,
    inhibited: bool,
    command: CommandDisposition,
    endpoint: EndpointDisposition,
) -> None:
    h = await make_human()
    owner = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    owner.endpoint = endpoint
    owner.command = command
    h.controller._routine_inhibited = inhibited  # pyright: ignore[reportPrivateUsage]
    h.actuator.calls.clear()
    h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)

    def forbidden() -> bool:
        pytest.fail("an independent emergency consulted guarded HUMAN admission")

    monkeypatch.setattr(
        HumanCallAdmissionFence, "closed_reason", PropertyMock(side_effect=forbidden)
    )
    monkeypatch.setattr(h.controller, "_teardown_unconfirmed", forbidden)
    assert await h.controller.operator_emergency_stop("independent synthetic e-stop")
    assert h.commands == ["emergency_stop"]
    assert h.controller.phase is RoastPhase.FAULTED
    assert owner.endpoint is endpoint
    assert owner.command is command


async def test_t43_legacy_resume_cannot_bypass_human_continuation() -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    h.actuator.calls.clear()
    with pytest.raises(InvalidTransitionError):
        h.controller.operator_resume(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.actuator.calls == []


async def test_t38_operator_drop_rechecks_generation_after_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.actuator.calls.clear()
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.sink.persist_evaluation

    async def audit(row: SafetyEvaluation) -> int | None:
        if row.rule == "command_phase_validity":
            assert row.verdict is SafetyVerdict.ALLOW
            entered.set()
            await release.wait()
        return await original(row)

    monkeypatch.setattr(h.sink, "persist_evaluation", audit)
    request = asyncio.create_task(h.controller.operator_drop_beans())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert h.controller.snapshot().human_endpoint is EndpointDisposition.NONE
        assert await h.controller.operator_emergency_stop("superseded pending drop")
    finally:
        release.set()
        await request
    assert h.commands == ["emergency_stop"]
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.NONE
    assert h.actuator.containments == 0


@pytest.mark.parametrize(
    ("admitted", "completed", "expected"),
    [
        (RoastPhase.FAULTED, RoastPhase.FAULTED, RoastPhase.FAULTED),
        (
            RoastPhase.OPERATOR_RECOVERY_REQUIRED,
            RoastPhase.OPERATOR_RECOVERY_REQUIRED,
            RoastPhase.COOLING,
        ),
        (RoastPhase.OPERATOR_RECOVERY_REQUIRED, RoastPhase.FAULTED, RoastPhase.FAULTED),
        (
            RoastPhase.FAULTED,
            RoastPhase.OPERATOR_RECOVERY_REQUIRED,
            RoastPhase.OPERATOR_RECOVERY_REQUIRED,
        ),
        (RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.FAULTED, RoastPhase.FAULTED),
    ],
)
async def test_tf3_tf4_human_drop_phase_is_bound_at_admission_and_completion(
    monkeypatch: pytest.MonkeyPatch,
    admitted: RoastPhase,
    completed: RoastPhase,
    expected: RoastPhase,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    if h.controller.phase is not admitted:
        h.controller.transition_to(admitted)

    async def operation() -> None:
        if h.controller.phase is not completed:
            # The shared arbiter prevents this interleaving in production.
            monkeypatch.setattr(h.controller, "_phase", completed)

    if admitted is RoastPhase.ROASTING_PRE_FIRST_CRACK:
        original_drop = h.actuator.drop_beans

        async def protective_drop() -> AppliedRoasterState:
            applied = await original_drop()
            assert applied is not None
            await operation()
            return applied

        monkeypatch.setattr(h.actuator, "drop_beans", protective_drop)
        assert await h.controller._execute_human_drop(  # pyright: ignore[reportPrivateUsage]
            EndpointOwner.PROTECTION, DropReason.CEILING_GUARD, generation=h.arbiter.generation
        )
    else:
        assert await h.controller.execute_human_fault_drop(
            h.actuator.session_id,
            generation=h.arbiter.generation,
            lease_current=lambda: True,
            operation=operation,
        )
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.ISSUED
    assert h.controller.phase is expected


@pytest.mark.parametrize("outcome", ["issued", "withheld", "unknown"])
@pytest.mark.parametrize(
    "latch", [None, SafetyVerdict.RECOVERY, SafetyVerdict.FAULT, SafetyVerdict.EMERGENCY_STOP]
)
async def test_tf15_fault_drop_recovery_does_not_lower_latch_or_leave_fault(
    monkeypatch: pytest.MonkeyPatch, outcome: str, latch: SafetyVerdict | None
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.FAULTED)
    monkeypatch.setattr(h.controller, "_latched_verdict", latch)
    if outcome == "withheld":
        h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)

    async def operation() -> None:
        if outcome == "unknown":
            raise TimeoutError("unconfirmed drop")

    dispatched = await h.controller.execute_human_fault_drop(
        h.actuator.session_id,
        generation=h.arbiter.generation,
        lease_current=lambda: True,
        operation=operation,
    )

    assert dispatched is (outcome == "issued")
    snapshot = h.controller.snapshot()
    assert snapshot.phase is RoastPhase.FAULTED
    assert snapshot.human_endpoint is EndpointDisposition(outcome)
    actual = h.controller._latched_verdict  # pyright: ignore[reportPrivateUsage]
    assert actual is (latch if dispatched or latch is not None else SafetyVerdict.RECOVERY)
    assert (RoastEventKind.RECOVERY_REQUIRED in h.emitter.kinds()) is not dispatched


@pytest.mark.parametrize("durable", [False, True])
async def test_tf21_closure_refuses_before_join_or_task_registration(
    monkeypatch: pytest.MonkeyPatch, durable: bool
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.FAULTED)
    assert h.controller.human_recovery_drop_allowed()
    if durable:
        h.controller.note_human_run_durably_closed()
    else:
        h.controller.begin_human_completion()
    assert not h.controller.human_recovery_drop_allowed()

    async def unexpected(*_args: object) -> None:
        pytest.fail("closed DROP reached persistence or dispatch")

    def unexpected_join() -> bool:
        pytest.fail("closed DROP reached the join")

    monkeypatch.setattr(h.sink, "persist_evaluation", unexpected)
    monkeypatch.setattr(h.controller, "_join_human_drop", unexpected_join)
    assert not await h.controller.execute_human_fault_drop(
        h.actuator.session_id,
        generation=h.arbiter.generation,
        lease_current=lambda: True,
        operation=unexpected,
    )
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.NONE

    async def unexpected_wait(*_args: object) -> None:
        pytest.fail("empty evaluation registry awaited a task")

    monkeypatch.setattr(asyncio, "wait", unexpected_wait)
    assert await h.controller.settle_human_drop_evaluations()


@pytest.mark.parametrize("cancellation", ["hook", "barrier", "task", "error"])
async def test_tf22_drop_evaluation_remains_owned_across_failure_and_cancellation(
    monkeypatch: pytest.MonkeyPatch, cancellation: str
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.FAULTED)
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold(_evaluation: SafetyEvaluation) -> int:
        entered.set()
        await release.wait()
        if cancellation == "error":
            raise RuntimeError("synthetic evaluation COMMIT failure")
        settled.set()
        return 1

    async def unexpected() -> None:
        pytest.fail("failed or closed evaluation authorised a DROP")

    monkeypatch.setattr(h.sink, "persist_evaluation", hold)
    hook = asyncio.create_task(
        h.controller.execute_human_fault_drop(
            h.actuator.session_id,
            generation=h.arbiter.generation,
            lease_current=lambda: True,
            operation=unexpected,
        )
    )
    registry = h.controller._human_drop_evaluation_tasks  # pyright: ignore[reportPrivateUsage]
    await asyncio.wait_for(entered.wait(), 1)
    (persistence,) = registry
    h.controller.begin_human_completion()
    barrier = asyncio.create_task(h.controller.settle_human_drop_evaluations())
    await asyncio.sleep(0)
    assert not barrier.done()
    try:
        if cancellation == "hook":
            hook.cancel()
            with pytest.raises(asyncio.CancelledError):
                await hook
            assert persistence in registry and not persistence.done() and not barrier.done()
        elif cancellation == "barrier":
            barrier.cancel()
            with pytest.raises(asyncio.CancelledError):
                await barrier
            assert persistence in registry and not persistence.done()
        elif cancellation == "task":
            persistence.cancel()
        release.set()
        if cancellation == "error":
            with pytest.raises(RuntimeError, match="synthetic evaluation COMMIT failure"):
                await hook
        elif cancellation == "task":
            with pytest.raises(asyncio.CancelledError):
                await hook
        elif cancellation == "barrier":
            assert not await hook
        if cancellation != "barrier":
            assert (await barrier) is (cancellation == "hook")
        assert settled.is_set() is (cancellation in {"hook", "barrier"})
        assert h.controller.human_drop_closed()
        assert h.controller.snapshot().human_endpoint is EndpointDisposition.NONE
        assert (await h.controller.settle_human_drop_evaluations()) is (
            cancellation in {"hook", "barrier"}
        )
    finally:
        release.set()
        await asyncio.gather(hook, barrier, return_exceptions=True)


@pytest.mark.parametrize(
    ("source", "outcome"),
    [
        ("exact fault rearm", EndpointDisposition.ISSUED),
        ("exact fault rearm", EndpointDisposition.WITHHELD),
        ("exact fault rearm", EndpointDisposition.UNKNOWN),
        ("raising operator stop", EndpointDisposition.ISSUED),
        ("cancelled operator stop", EndpointDisposition.ISSUED),
        ("latched escalation", EndpointDisposition.ISSUED),
    ],
)
async def test_tf5_emergency_retry_survives_each_fault_drop_outcome(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    outcome: EndpointDisposition,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    entered_stop = asyncio.Event()

    async def failed_stop(*, reason: str) -> AppliedRoasterState:
        entered_stop.set()
        if source == "cancelled operator stop":
            await asyncio.Future[None]()
        raise TimeoutError(reason)

    monkeypatch.setattr(h.actuator, "emergency_stop", failed_stop)
    if source == "exact fault rearm":
        h.controller.transition_to(RoastPhase.FAULTED)
        h.controller.rearm_emergency_stop_fail_safe()
    elif source == "cancelled operator stop":
        stop = asyncio.create_task(h.controller.operator_emergency_stop("retry source"))
        await asyncio.wait_for(entered_stop.wait(), 1)
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop
    elif source == "latched escalation":
        h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
        monkeypatch.setattr(h.controller, "_latched_verdict", SafetyVerdict.RECOVERY)
        h.clock.advance(0.25)
        h.actuator.bean = 260.0
        await h.controller.human_urgent_stage()
    else:
        assert not await h.controller.operator_emergency_stop("retry source")

    h.clock.advance(0.25)
    h.actuator.bean = 185.0
    await h.controller.human_urgent_stage()
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]
    assert h.controller.human_recovery_drop_allowed()
    h.actuator.calls.clear()
    held, release = asyncio.Event(), asyncio.Event()
    generation = h.arbiter.generation

    async def occupy() -> None:
        held.set()
        await release.wait()

    async def drop() -> None:
        assert outcome is not EndpointDisposition.WITHHELD
        h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
        if outcome is EndpointDisposition.UNKNOWN:
            raise TimeoutError("strict write unconfirmed")

    blocker = (
        asyncio.create_task(h.arbiter.run_routine(generation, occupy))
        if outcome is EndpointDisposition.WITHHELD
        else None
    )

    async def observe_admission() -> None:
        while h.controller.snapshot().human_endpoint is not EndpointDisposition.ADMITTED:
            await asyncio.sleep(0)

    if blocker is not None:
        await asyncio.wait_for(held.wait(), 1)
    task = asyncio.create_task(
        h.controller.execute_human_fault_drop(
            h.actuator.session_id,
            generation=generation,
            lease_current=lambda: True,
            operation=drop,
        )
    )
    try:
        if blocker is not None:
            await asyncio.wait_for(observe_admission(), 1)
            h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
    finally:
        release.set()
        if blocker is not None:
            assert (await blocker)[0]
        assert (await task) is (outcome is EndpointDisposition.ISSUED)
    assert h.controller.phase is RoastPhase.FAULTED
    assert h.controller.snapshot().human_endpoint is outcome
    assert h.controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]
    await h.controller.tick()
    assert h.actuator.calls == [
        ("set_heat", {"heat_percent": 0}),
        ("set_fan", {"fan_percent": SafetyLimits().overrun_safe_fan_percent}),
    ]
    assert not await h.controller._set_targets(heat_percent=0, fan_percent=0)  # pyright: ignore[reportPrivateUsage]
    assert h.commands == ["set_heat", "set_fan"]


@pytest.mark.parametrize("revoked", ["freshness", "fence", "teardown", "D212"])
async def test_tf14_queued_fault_drop_is_withheld_when_dispatch_authority_changes(
    revoked: str,
) -> None:
    teardown = False
    h = await make_human(teardown_unconfirmed=lambda: teardown)
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.FAULTED)
    held, release = asyncio.Event(), asyncio.Event()
    generation = h.arbiter.generation

    async def occupy() -> None:
        held.set()
        await release.wait()

    async def forbidden() -> None:
        pytest.fail("revoked fault drop reached the exact-session write")

    async def observe_admission() -> None:
        while h.controller.snapshot().human_endpoint is not EndpointDisposition.ADMITTED:
            await asyncio.sleep(0)

    blocker = asyncio.create_task(h.arbiter.run_routine(generation, occupy))
    await asyncio.wait_for(held.wait(), 1)
    requested = asyncio.create_task(
        h.controller.execute_human_fault_drop(
            h.actuator.session_id,
            generation=generation,
            lease_current=lambda: True,
            operation=forbidden,
        )
    )
    try:
        await asyncio.wait_for(observe_admission(), 1)
        if revoked == "freshness":
            h.clock.advance(20)
        elif revoked == "fence":
            h.fence.close(AdmissionCloseReason.APPLICATION_TEARDOWN)
        elif revoked == "teardown":
            teardown = True
        else:
            assert h.arbiter.reserve_close(generation)
    finally:
        release.set()
        assert (await blocker)[0]
        assert not await requested
    snapshot = h.controller.snapshot()
    assert snapshot.phase is RoastPhase.FAULTED
    assert snapshot.human_endpoint is EndpointDisposition.WITHHELD
    assert snapshot.human_command_disposition is CommandDisposition.SETTLED
    assert snapshot.physical_intervention_required
    assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
    assert h.emitter.kinds().count(RoastEventKind.RECOVERY_REQUIRED) == 1
    assert h.actuator.containments <= 1


@pytest.mark.parametrize(
    ("condition", "expected", "projects"),
    [
        ("no_record", EndpointDisposition.NONE, False),
        ("other_session", EndpointDisposition.ISSUED, False),
        ("other_run_same_session", EndpointDisposition.ISSUED, False),
        ("different_endpoint", EndpointDisposition.WITHHELD, False),
        ("different_owner", EndpointDisposition.ISSUED, False),
        ("readback_mismatch", EndpointDisposition.ISSUED, False),
        ("non_bool", EndpointDisposition.ISSUED, False),
        ("superseded", EndpointDisposition.UNKNOWN, False),
        ("closing", EndpointDisposition.UNKNOWN, False),
        ("closed", EndpointDisposition.UNKNOWN, False),
        ("current", EndpointDisposition.UNKNOWN, True),
    ],
)
async def test_tf9_resolver_keys_run_identity_and_never_reprojects_closed_or_stale_records(
    condition: str,
    expected: EndpointDisposition,
    projects: bool,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.FAULTED)
    session_id = h.actuator.session_id
    owner = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert owner is not None

    async def write() -> None:
        pass

    if condition != "no_record":
        assert await h.controller.execute_human_fault_drop(
            session_id,
            generation=h.arbiter.generation,
            lease_current=lambda: True,
            operation=write,
        )
        assert h.controller.has_pending_fault_drop_readback()
    if condition == "other_run_same_session":
        owner.durably_closed = True
        replacement = h.ownership.claim_run("replacement")
        replacement.session_id = session_id
    elif condition == "different_endpoint":
        owner.endpoint = EndpointDisposition.WITHHELD
    elif condition == "different_owner":
        owner.endpoint_owner = EndpointOwner.OPERATOR_RECOVERY
    elif condition == "superseded":
        h.controller.admit_emergency_stop()
    elif condition == "closing":
        h.controller.begin_human_completion()
    elif condition == "closed":
        h.controller.note_human_run_durably_closed()

    observed = FaultControlState(
        session_id="other" if condition == "readback_mismatch" else session_id,
        mcp_phase="fault",
        active=False,
        device_connected=True,
        heat_level_percent=0,
        fan_level_percent=0,
        cooling_on=False,
        beans_added=True,
        beans_dropped=False,
    )
    if condition == "non_bool":
        observed = observed.model_copy(update={"beans_dropped": 0})
    h.emitter.events.clear()
    h.controller.resolve_fault_drop_readback(
        "other" if condition == "other_session" else session_id,
        observed,
    )
    assert not h.controller.has_pending_fault_drop_readback()
    assert owner.endpoint is expected
    assert owner.command is (
        CommandDisposition.UNKNOWN
        if expected is EndpointDisposition.UNKNOWN
        else CommandDisposition.SETTLED
    )
    assert h.controller.phase is RoastPhase.FAULTED
    expected_events = (
        [RoastEventKind.COMMAND_FAILED, RoastEventKind.RECOVERY_REQUIRED]
        if projects
        else [RoastEventKind.COMMAND_FAILED]
        if condition == "superseded"
        else []
    )
    assert h.emitter.kinds() == expected_events


@pytest.mark.parametrize("invalidated_by", ["identity", "epoch", "freshness"])
async def test_l1_accepted_continuation_stays_inhibited_after_evidence_changes(
    invalidated_by: str,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    h.actuator.calls.clear()

    assert await h.controller.human_operator_continue(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert h.sink.evaluations[-1].rule == "human_continuation"
    assert h.sink.evaluations[-1].verdict is SafetyVerdict.ALLOW
    if invalidated_by == "identity":
        h.actuator.session_id = "different-synthetic-session"
    elif invalidated_by == "epoch":
        h.actuator.epoch += 1
    else:
        h.clock.advance(20)

    await h.turn(bean=185)

    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
    assert h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]
    assert h.commands == []
    outcome = h.sink.evaluations[-1]
    if invalidated_by == "identity":
        assert outcome.rule == "human_continuation"
        assert outcome.verdict is SafetyVerdict.REJECT
    else:
        assert outcome.rule == "human_reading_invalid"
        assert outcome.verdict is SafetyVerdict.RECOVERY
        freshness = "invalid" if invalidated_by == "epoch" else "unproven"
        assert f"reading is {freshness}" in outcome.reason


@pytest.mark.parametrize("bean", [196.0, 197.0])
@pytest.mark.parametrize("unknown", [False, True], ids=["applied", "unknown"])
async def test_l2_continuation_at_threshold_stays_inhibited_through_drop(
    monkeypatch: pytest.MonkeyPatch, bean: float, unknown: bool
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    assert await h.controller.human_operator_continue(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    h.actuator.calls.clear()
    entered, release = asyncio.Event(), asyncio.Event()
    actual_drop = h.actuator.drop_beans

    async def hold_drop() -> AppliedRoasterState | None:
        entered.set()
        await release.wait()
        result = await actual_drop()
        return None if unknown else result

    monkeypatch.setattr(h.actuator, "drop_beans", hold_drop)
    task = asyncio.create_task(h.turn(bean=bean))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert h.controller.snapshot().human_endpoint is EndpointDisposition.IN_FLIGHT
        assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
        assert h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]
        assert h.commands == []
    finally:
        release.set()
        await task

    assert h.commands == ["drop_beans"]
    assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
    assert h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]
    snapshot = h.controller.snapshot()
    assert snapshot.phase is (
        RoastPhase.OPERATOR_RECOVERY_REQUIRED if unknown else RoastPhase.COOLING
    )
    assert snapshot.human_endpoint is (
        EndpointDisposition.UNKNOWN if unknown else EndpointDisposition.ISSUED
    )


@pytest.mark.parametrize(
    "endpoint", [EndpointDisposition.UNKNOWN, EndpointDisposition.HISTORY_UNKNOWN]
)
async def test_l6_direct_session_start_refuses_uncertain_endpoint(
    endpoint: EndpointDisposition,
) -> None:
    h = await make_human(start=False)
    owner = h.controller._human_run  # pyright: ignore[reportPrivateUsage]
    assert owner is not None
    owner.endpoint = endpoint
    assert owner.command is CommandDisposition.SETTLED

    await h.controller.start_run(PROFILE)

    assert h.commands == []
    assert h.ownership.is_current(owner)
    assert owner.endpoint is endpoint
    assert owner.session_id is None
    assert RoastEventKind.RUN_STARTED not in h.emitter.kinds()


async def test_l9_repeated_charge_input_in_pre_fc_commits_one_t0() -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK

    await h.turn(bean=190, t0=True)

    assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert h.controller.snapshot().charge_detected
    assert h.emitter.kinds().count(RoastEventKind.T0_DETECTED) == 1
    assert "drop_beans" not in h.commands


@pytest.mark.parametrize("interruption", ["cancel", "emergency"])
async def test_protective_drop_interrupted_before_dispatch_has_no_interval(
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.actuator.calls.clear()
    routine_entered, release_routine, drop_submitted = (asyncio.Event() for _ in range(3))

    async def occupy_arbiter() -> None:
        routine_entered.set()
        await release_routine.wait()

    condition = h.arbiter._condition  # pyright: ignore[reportPrivateUsage]
    actual_wait = condition.wait

    async def observe_wait() -> bool:
        drop_submitted.set()
        return await actual_wait()

    monkeypatch.setattr(condition, "wait", observe_wait)
    routine = asyncio.create_task(h.arbiter.run_routine(h.arbiter.generation, occupy_arbiter))
    pending: asyncio.Task[None] | None = None
    stop: asyncio.Task[bool] | None = None
    try:
        await asyncio.wait_for(routine_entered.wait(), 1)
        pending = asyncio.create_task(h.turn(bean=196))
        await asyncio.wait_for(drop_submitted.wait(), 1)
        assert h.controller.snapshot().human_endpoint is EndpointDisposition.ADMITTED
        if interruption == "cancel":
            pending.cancel()
        else:
            h.controller.admit_emergency_stop()
            stop = asyncio.create_task(
                h.controller.operator_emergency_stop(
                    "superseded queued drop", already_admitted=True
                )
            )
    finally:
        release_routine.set()
        try:
            await routine
        finally:
            try:
                if pending is not None:
                    if interruption == "cancel":
                        pending.cancel()
                        with suppress(asyncio.CancelledError):
                            await pending
                    else:
                        await pending
            finally:
                if stop is not None:
                    assert await stop

    if interruption == "cancel":
        assert pending is not None and pending.cancelled()
        assert h.controller.snapshot().human_endpoint is EndpointDisposition.UNKNOWN
        assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
        assert h.commands == []
        event = _drop_outcome_event(h, "unknown")
        assert "dispatch_started_at_utc" not in event
        assert "dispatch_ended_at_utc" not in event
        assert "dispatch_duration_seconds" not in event
    else:
        assert h.controller.phase is RoastPhase.FAULTED
        assert h.commands == ["emergency_stop"]


@pytest.mark.parametrize("missing", ["session", "child"])
async def test_human_start_with_incomplete_identity_cannot_authorize_recovery_drop(
    missing: str,
) -> None:
    h = await make_human(bind_session=missing != "session", bind_child=missing != "child")
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.FAULTED)
    h.actuator.calls.clear()

    assert not h.controller.human_recovery_drop_allowed()
    await h.controller.operator_drop_beans()
    assert h.commands == []
    assert h.controller.phase is RoastPhase.FAULTED


async def test_unwired_human_reader_faults_without_falling_back_to_legacy_read() -> None:
    h = await make_human(read_port=False)
    h.actuator.calls.clear()
    h.log.clear()
    for _ in range(SafetyLimits().max_consecutive_mcp_failures):
        await h.controller.tick()

    assert h.controller.phase is RoastPhase.FAULTED
    assert h.sink.evaluations[-1].rule == "mcp_read_failures_exhausted"
    assert h.sink.evaluations[-1].verdict is SafetyVerdict.FAULT
    assert h.actuator.reads == 0
    assert "read" not in h.log
    assert h.commands == []


async def test_automatic_rejects_direct_human_urgent_stage_without_adapter_io() -> None:
    log: list[str] = []
    clock = FakeClock()
    actuator = HumanRoaster(clock, log)
    sink = RecordingSnapshotSink(log)
    emitter = EventSink(log)
    controller = RoastController(
        config=ControllerConfig(),
        safety=SafetyPolicy(SafetyLimits()),
        state_reader=actuator,
        command_executor=actuator,
        snapshot_sink=sink,
        event_emitter=emitter,
        human_reader=actuator,
    )

    with pytest.raises(RuntimeError, match="HUMAN urgent stage requires HUMAN authority"):
        await controller.human_urgent_stage()

    assert log == []
    assert actuator.reads == 0
    assert actuator.calls == []
    assert controller.phase is RoastPhase.IDLE


async def test_recovery_drop_unknown_keeps_existing_recovery_phase() -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    prior_phase_events = h.emitter.kinds().count(RoastEventKind.PHASE_CHANGED)
    h.actuator.drop_applied_state = None
    h.actuator.calls.clear()

    await h.controller.operator_drop_beans()

    assert h.commands == ["drop_beans"]
    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.UNKNOWN
    assert h.emitter.kinds().count(RoastEventKind.PHASE_CHANGED) == prior_phase_events
    assert h.controller.snapshot().physical_intervention_required


async def test_l1_pending_continuation_sees_recovery_entered_during_read() -> None:
    h = await make_human()
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    assert await h.controller.human_operator_continue(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    h.actuator.calls.clear()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_read() -> None:
        entered.set()
        await release.wait()

    h.actuator.collect = hold_read
    turn = asyncio.create_task(h.turn(bean=185))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        h.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
        phase_events = h.emitter.kinds().count(RoastEventKind.PHASE_CHANGED)
    finally:
        release.set()
        await turn

    assert h.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert h.controller._routine_inhibited  # pyright: ignore[reportPrivateUsage]
    assert h.controller._protection_check_pending  # pyright: ignore[reportPrivateUsage]
    assert h.emitter.kinds().count(RoastEventKind.PHASE_CHANGED) == phase_events
    assert h.sink.evaluations[-1].rule == "human_continuation"
    assert h.sink.evaluations[-1].verdict is SafetyVerdict.REJECT
    assert h.commands == []


async def test_human_protective_drop_honours_injected_policy_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = SafetyPolicy(SafetyLimits())
    rejection = SafetyEvaluation(
        rule="human_protective_drop",
        verdict=SafetyVerdict.REJECT,
        reason="test policy rejects the protective drop",
    )
    evaluate = Mock(return_value=rejection)
    monkeypatch.setattr(policy, "evaluate_human_protective_drop", evaluate)
    h = await make_human(safety=policy)
    await h.turn(bean=180)
    await h.turn(bean=185, t0=True)
    h.actuator.calls.clear()

    await h.turn(bean=196)

    evaluate.assert_called_once_with(
        authority=ControlAuthority.HUMAN,
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        charged=True,
        freshness=ReadingFreshness.FRESH,
        bean_temp_c=196,
        endpoint=EndpointDisposition.NONE,
        command=CommandDisposition.SETTLED,
    )
    assert rejection in h.sink.evaluations
    assert "drop_beans" not in h.commands
    assert h.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert h.controller.snapshot().human_endpoint is EndpointDisposition.NONE
