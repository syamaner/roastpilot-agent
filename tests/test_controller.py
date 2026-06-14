"""E4-S1/E4-S2: transition table, tick scheduler, tick pipeline
(component plan §3, §8; orchestration plan § State Machine,
§ Controller Loop).

T0 debounce + add-beans guidance (E4-S3) and restart recovery (E4-S4)
extend this suite.
"""

import asyncio
from dataclasses import dataclass, field
from typing import cast

import pytest

from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorDescriptor,
    AdvisorFailureMode,
    FakeAdvisor,
    RoastAdvisor,
    RoastDecision,
)
from roastpilot_agent.config import ControllerConfig, SafetyLimits
from roastpilot_agent.controller import (
    TRANSITION_TABLE,
    UNIVERSAL_TARGETS,
    AdvisoryCallPolicy,
    AdvisoryTrigger,
    ControllerSnapshot,
    InvalidTransitionError,
    RoastController,
    RoastPhase,
    TickScheduler,
)
from roastpilot_agent.models import (
    AdvisorHealth,
    AdvisorHealthStatus,
    OperatorAction,
    RoastEventKind,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.safety import (
    OPERATOR_ACTION_COMMAND,
    SafetyEvaluation,
    SafetyPolicy,
    SafetyVerdict,
    enabled_operator_actions,
)
from tests.conftest import (
    EventSink,
    FakeClock,
    FakeMCPClient,
    RecordingExecutor,
    RecordingSnapshotSink,
    ScriptedStateReader,
)

# --- harness ---


@dataclass
class Harness:
    controller: RoastController
    reader: ScriptedStateReader
    executor: RecordingExecutor
    sink: RecordingSnapshotSink
    events: EventSink
    clock: FakeClock
    log: list[str] = field(default_factory=list[str])


PROFILE = RoastProfile(
    name="harness",
    bean_origin="Ethiopia",
    bean_weight_grams=250.0,
    initial_heat_percent=70,
    initial_fan_percent=40,
    target_drop_temp_c=205.0,
    target_development_percent=20.0,
)


def make_harness(
    *,
    readings: list[RoastTelemetry | None | Exception] | None = None,
    advisor: RoastAdvisor | None = None,
    config: ControllerConfig | None = None,
    limits: SafetyLimits | None = None,
    executor: RecordingExecutor | None = None,
) -> Harness:
    log: list[str] = []
    clock = FakeClock()
    reader = ScriptedStateReader(readings, log)
    executor = executor if executor is not None else RecordingExecutor(log)
    sink = RecordingSnapshotSink(log)
    events = EventSink(log)
    controller = RoastController(
        config=config or ControllerConfig(),
        safety=SafetyPolicy(limits or SafetyLimits()),
        state_reader=reader,
        command_executor=executor,
        snapshot_sink=sink,
        event_emitter=events,
        advisor=advisor,
        clock=clock,
    )
    return Harness(controller, reader, executor, sink, events, clock, log)


def reading(bean: float = 180.0, env: float = 200.0, **kwargs: object) -> RoastTelemetry:
    return RoastTelemetry.model_validate({"bean_temp_c": bean, "env_temp_c": env, **kwargs})


def _harness_in_phase(phase: RoastPhase) -> Harness:
    """A harness whose controller is manoeuvred into ``phase`` through legal edges
    only — exposes the event sink so a test can assert observable output."""
    harness = make_harness()
    controller = harness.controller
    if phase is RoastPhase.IDLE:
        return harness
    for step in NORMAL_PATH:
        controller.transition_to(step)
        if step is phase:
            return harness
    controller.transition_to(phase)  # FAULTED / RECOVERY via universal edge
    return harness


def controller_in(phase: RoastPhase) -> RoastController:
    """A controller manoeuvred into ``phase`` through legal edges only."""
    return _harness_in_phase(phase).controller


NORMAL_PATH = [
    RoastPhase.STARTING,
    RoastPhase.PREHEATING,
    RoastPhase.ROASTING_PRE_FIRST_CRACK,
    RoastPhase.DEVELOPMENT,
    RoastPhase.COOLING,
    RoastPhase.COMPLETE,
    RoastPhase.IDLE,
]


# --- E4-S1: transition table ---


def test_table_covers_every_phase() -> None:
    assert set(TRANSITION_TABLE) == set(RoastPhase)


def test_valid_normal_roast_path() -> None:
    controller = make_harness().controller
    assert controller.phase is RoastPhase.IDLE
    for step in NORMAL_PATH:
        assert controller.can_transition(step)
        controller.transition_to(step)
        assert controller.phase is step


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RoastPhase.IDLE, RoastPhase.DEVELOPMENT),
        (RoastPhase.IDLE, RoastPhase.PREHEATING),
        (RoastPhase.STARTING, RoastPhase.DEVELOPMENT),
        (RoastPhase.PREHEATING, RoastPhase.COOLING),
        (RoastPhase.PREHEATING, RoastPhase.DEVELOPMENT),
        (RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.COMPLETE),
        (RoastPhase.DEVELOPMENT, RoastPhase.PREHEATING),
        (RoastPhase.COOLING, RoastPhase.DEVELOPMENT),
        (RoastPhase.COMPLETE, RoastPhase.COOLING),
        (RoastPhase.FAULTED, RoastPhase.DEVELOPMENT),
    ],
)
def test_invalid_transitions_rejected(current: RoastPhase, target: RoastPhase) -> None:
    controller = controller_in(current)
    assert not controller.can_transition(target)
    with pytest.raises(InvalidTransitionError) as excinfo:
        controller.transition_to(target)
    assert excinfo.value.current is current
    assert excinfo.value.target is target
    assert controller.phase is current


@pytest.mark.parametrize("phase", list(RoastPhase))
def test_self_transition_is_not_a_transition(phase: RoastPhase) -> None:
    controller = controller_in(phase)
    assert not controller.can_transition(phase)
    with pytest.raises(InvalidTransitionError):
        controller.transition_to(phase)


UNIVERSAL_SORTED: list[RoastPhase] = sorted(UNIVERSAL_TARGETS, key=lambda p: p.value)


@pytest.mark.parametrize("universal", UNIVERSAL_SORTED)
@pytest.mark.parametrize("phase", list(RoastPhase))
def test_universal_edges_from_every_phase(phase: RoastPhase, universal: RoastPhase) -> None:
    controller = controller_in(phase)
    if phase is universal:
        assert not controller.can_transition(universal)
    else:
        controller.transition_to(universal)
        assert controller.phase is universal


def test_faulted_exits_only_to_idle_or_recovery() -> None:
    controller = controller_in(RoastPhase.FAULTED)
    legal = {target for target in RoastPhase if controller.can_transition(target)}
    assert legal == {RoastPhase.IDLE, RoastPhase.OPERATOR_RECOVERY_REQUIRED}


def test_recovery_exits_cover_operator_choices() -> None:
    controller = controller_in(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    legal = {target for target in RoastPhase if controller.can_transition(target)}
    assert legal == {
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
        RoastPhase.DEVELOPMENT,
        RoastPhase.COOLING,
        RoastPhase.COMPLETE,
        RoastPhase.IDLE,
        RoastPhase.FAULTED,
    }
    assert RoastPhase.STARTING not in legal


def test_transitions_emit_phase_changed_events() -> None:
    harness = make_harness()
    harness.controller.transition_to(RoastPhase.STARTING)
    assert harness.events.kinds() == [RoastEventKind.PHASE_CHANGED]


def test_no_transition_api_accepts_advisor_output() -> None:
    """The advisor cannot trigger transitions — structurally: no public
    RoastController method takes an advisor type."""
    import inspect

    from roastpilot_agent import advisor as advisor_module

    advisor_types = {
        obj
        for _, obj in inspect.getmembers(advisor_module, inspect.isclass)
        if obj.__module__ == advisor_module.__name__
    }
    for name, method in inspect.getmembers(RoastController, inspect.isfunction):
        if name.startswith("_"):
            continue
        for parameter in inspect.signature(method).parameters.values():
            assert parameter.annotation not in advisor_types, (
                f"RoastController.{name} accepts advisor type {parameter.annotation}"
            )


# --- E4-S2: scheduler ---


def run_scheduler(
    *, interval: float, work_seconds: list[float], clock: FakeClock
) -> tuple[TickScheduler, list[float]]:
    starts: list[float] = []
    work = list(work_seconds)

    async def tick() -> None:
        starts.append(clock.now)
        clock.advance(work.pop(0))
        if not work:
            scheduler.stop()

    async def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)

    scheduler = TickScheduler(interval_seconds=interval, tick=tick, clock=clock, sleep=fake_sleep)
    asyncio.run(scheduler.run())
    return scheduler, starts


def test_scheduler_does_not_accumulate_drift() -> None:
    """Five ticks of 0.3 s work at a 1.0 s interval start at exact
    multiples of the interval — work time never shifts the schedule."""
    scheduler, starts = run_scheduler(interval=1.0, work_seconds=[0.3] * 5, clock=FakeClock())
    assert starts == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert scheduler.tick_count == 5
    assert scheduler.max_jitter_seconds == 0.0


def test_slow_tick_recovers_onto_schedule() -> None:
    """A 1.5 s tick at a 1.0 s interval delays the next tick (jitter
    measured), then the schedule catches up — no permanent drift."""
    scheduler, starts = run_scheduler(
        interval=1.0, work_seconds=[1.5, 0.1, 0.1, 0.1], clock=FakeClock()
    )
    assert starts == [0.0, 1.5, 2.0, 3.0]
    assert abs(scheduler.max_jitter_seconds - 0.5) < 1e-9
    assert scheduler.last_jitter_seconds == 0.0


def test_scheduler_stop_ends_run() -> None:
    scheduler, starts = run_scheduler(interval=1.0, work_seconds=[0.1], clock=FakeClock())
    assert starts == [0.0]
    assert scheduler.tick_count == 1
    assert not scheduler.running


# --- E4-S2: tick pipeline ---


def decision(heat: int = 65, fan: int = 50, drop: bool = False) -> RoastDecision:
    return RoastDecision(
        target_heat=heat,
        target_fan=fan,
        should_drop=drop,
        confidence=0.9,
        rationale="scripted",
    )


def harness_in_development(
    *,
    readings: list[RoastTelemetry | None | Exception],
    advisor: RoastAdvisor | None = None,
    config: ControllerConfig | None = None,
) -> Harness:
    harness = make_harness(readings=readings, advisor=advisor, config=config)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT
        harness.controller.transition_to(step)
    harness.log.clear()
    harness.events.events.clear()
    return harness


@pytest.mark.asyncio
async def test_tick_order_with_advisory() -> None:
    """The documented tick order: read → persist snapshot → safety →
    persist evaluation → advisory → validate/persist → execute → emit."""
    advisor = FakeAdvisor([decision()])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    advisor._log = harness.log  # share the order log  # pyright: ignore[reportPrivateUsage]
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.log == [
        "read",
        "persist_snapshot",
        "persist_evaluation:all_clear",
        "advisor",
        "persist_evaluation:all_clear",
        "persist_advisor_decision:ok",
        "emit:advisory",
        "set_targets",
        "emit:command_executed",
    ]
    assert harness.executor.targets == [(65, 50)]


@pytest.mark.asyncio
async def test_safety_evaluated_before_advisory_and_blocks_it() -> None:
    """A hard-ceiling breach e-stops and faults before the advisor is ever
    consulted — safety always precedes advisory calls and execution."""
    advisor = FakeAdvisor([decision()])
    harness = harness_in_development(readings=[reading(bean=231.0)], advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert "advisor" not in harness.log
    assert harness.executor.targets == []
    assert harness.executor.estop_reasons  # e-stop executed
    assert harness.controller.phase is RoastPhase.FAULTED
    assert RoastEventKind.FAULT in harness.events.kinds()


@pytest.mark.asyncio
async def test_slow_advisor_never_blocks_the_tick() -> None:
    """An advisor that never resolves times out; the tick completes with a
    rejected recommendation and the deterministic hold fallback."""

    class NeverAdvisor(RoastAdvisor):
        @property
        def descriptor(self) -> AdvisorDescriptor:
            return AdvisorDescriptor(provider="test", model="never", prompt_version="t")

        async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def healthcheck(self) -> AdvisorHealth:
            return AdvisorHealth(status=AdvisorHealthStatus.REACHABLE)

    config = ControllerConfig(advisory_timeout_seconds=0.05)
    harness = harness_in_development(readings=[reading()], advisor=NeverAdvisor(), config=config)
    harness.controller.request_advisory()
    await asyncio.wait_for(harness.controller.tick(), timeout=1.0)
    assert harness.executor.targets == []
    # A single (1st) timeout is an availability failure below the fail-closed
    # threshold (D30): tolerated REJECT, deterministic hold — heat unchanged.
    assert [e.rule for e in harness.sink.evaluations] == [
        "all_clear",
        "advisor_unavailable_tolerated",
    ]
    assert harness.sink.evaluations[-1].verdict is SafetyVerdict.REJECT
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_crashing_advisor_is_a_provider_error() -> None:
    class CrashingAdvisor(RoastAdvisor):
        @property
        def descriptor(self) -> AdvisorDescriptor:
            return AdvisorDescriptor(provider="test", model="crash", prompt_version="t")

        async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
            raise RuntimeError("boom")

        async def healthcheck(self) -> AdvisorHealth:
            return AdvisorHealth(status=AdvisorHealthStatus.REACHABLE)

    harness = harness_in_development(readings=[reading()], advisor=CrashingAdvisor())
    harness.controller.request_advisory()
    await harness.controller.tick()
    # A single crash is a provider_error availability failure below the
    # fail-closed threshold (D30): tolerated REJECT, deterministic hold. The
    # trace still records it as a provider_error (#167); the safety rule is the
    # availability-tolerated rule.
    assert harness.sink.evaluations[-1].rule == "advisor_unavailable_tolerated"
    assert harness.sink.evaluations[-1].verdict is SafetyVerdict.REJECT
    assert harness.sink.advisor_decisions[-1].status == "provider_error"
    assert harness.executor.targets == []
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_advisory_success_persists_decision_linked_to_verdict() -> None:
    """The success path persists one advisor_decisions row (#167): status 'ok',
    the RoastDecision, and the safety_evaluation_id of the verdict it produced —
    the controller wiring that was missing (zero call sites)."""
    advisor = FakeAdvisor([decision(heat=65, fan=50)])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert len(harness.sink.advisor_decisions) == 1
    recorded = harness.sink.advisor_decisions[0]
    assert recorded.status == "ok"
    assert recorded.decision is not None and recorded.decision.target_heat == 65
    assert recorded.descriptor.provider == "fake"
    # The link points at the command evaluation persisted just before it (the
    # second evaluation: all_clear → the advisory verdict). RecordingSnapshotSink
    # hands back monotonic ids, so the linked id is that second evaluation.
    assert recorded.safety_evaluation_id == len(harness.sink.evaluations)
    assert recorded.latency_ms is not None and recorded.latency_ms >= 0


@pytest.mark.asyncio
async def test_advisory_failure_persists_null_decision_linked_to_reject() -> None:
    """The failure path persists one advisor_decisions row (#167): the failure
    status, decision=None, and the safety_evaluation_id of the REJECT it
    produced — so the #134 provider_error trace is no longer thrown away."""
    advisor = FakeAdvisor([AdvisorFailureMode.PROVIDER_ERROR])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert len(harness.sink.advisor_decisions) == 1
    recorded = harness.sink.advisor_decisions[0]
    assert recorded.status == "provider_error"
    assert recorded.decision is None
    assert recorded.safety_evaluation_id == len(harness.sink.evaluations)
    assert harness.sink.evaluations[-1].verdict is SafetyVerdict.REJECT


@pytest.mark.asyncio
async def test_advisory_unsafe_output_persists_as_malformed() -> None:
    """An unsafe (out-of-bounds) advisor output has no distinct trace status; it
    persists as 'malformed' with decision=None (#167), while the safety verdict
    keeps its own 'advisor_unsafe' rule for the verdict stream."""
    advisor = FakeAdvisor([AdvisorFailureMode.UNSAFE])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert len(harness.sink.advisor_decisions) == 1
    recorded = harness.sink.advisor_decisions[0]
    assert recorded.status == "malformed"
    assert recorded.decision is None
    assert harness.sink.evaluations[-1].rule == "advisor_unsafe"


# --- D30 (#166): advisor sustained-unavailability fail-closed ---


@pytest.mark.asyncio
async def test_advisor_availability_failures_below_threshold_hold() -> None:
    """Two consecutive availability failures (default threshold 3) stay below
    the stop: the roast holds in development, no recovery, heat unchanged."""
    advisor = FakeAdvisor([AdvisorFailureMode.PROVIDER_ERROR, AdvisorFailureMode.TIMEOUT])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    for _ in range(2):
        harness.controller.request_advisory()
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert RoastEventKind.RECOVERY_REQUIRED not in harness.events.kinds()
    advisor_rules = [e.rule for e in harness.sink.evaluations if e.rule.startswith("advisor_")]
    assert advisor_rules == [
        "advisor_unavailable_tolerated",
        "advisor_unavailable_tolerated",
    ]


@pytest.mark.asyncio
async def test_nth_availability_failure_fails_closed_to_recovery() -> None:
    """The N-th (default 3rd) consecutive availability failure drives heat→0
    through the safety path and transitions to operator_recovery_required — not
    a fault. Heat/fan are never auto-resumed: only an explicit operator action
    leaves recovery."""
    advisor = FakeAdvisor(
        [
            AdvisorFailureMode.PROVIDER_ERROR,
            AdvisorFailureMode.PROVIDER_ERROR,
            AdvisorFailureMode.TIMEOUT,
        ]
    )
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    # Pre-seed a non-zero commanded heat so the heat→0 write is observable.
    harness.controller._current_heat = 80  # pyright: ignore[reportPrivateUsage]
    harness.controller._current_fan = 20  # pyright: ignore[reportPrivateUsage]
    for _ in range(3):
        harness.controller.request_advisory()
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert RoastEventKind.RECOVERY_REQUIRED in harness.events.kinds()
    assert RoastEventKind.FAULT not in harness.events.kinds()
    assert harness.sink.evaluations[-1].rule == "advisor_unavailable_exhausted"
    assert harness.sink.evaluations[-1].verdict is SafetyVerdict.RECOVERY
    # Heat was driven to 0 through the safety path (fan to the safe value).
    assert harness.executor.targets[-1] == (0, SafetyLimits().overrun_safe_fan_percent)
    snap = harness.controller.snapshot()
    assert snap.current_heat == 0
    # No auto-resume: a further tick stays in recovery and issues no heat write.
    harness.executor.targets.clear()
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert harness.executor.targets == []


@pytest.mark.asyncio
async def test_ok_decision_resets_availability_streak() -> None:
    """N-1 availability failures, then a successful ``ok`` decision, then more
    failures must not trip early: the ``ok`` resets the counter."""
    advisor = FakeAdvisor(
        [
            AdvisorFailureMode.PROVIDER_ERROR,
            AdvisorFailureMode.TIMEOUT,
            decision(heat=60, fan=40),  # ok → resets the streak
            AdvisorFailureMode.PROVIDER_ERROR,
            AdvisorFailureMode.TIMEOUT,
        ]
    )
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    for _ in range(5):
        harness.controller.request_advisory()
        await harness.controller.tick()
    # Five consults: 2 fail, 1 ok (reset), 2 fail — never 3 consecutive, so no
    # fail-closed; the roast is still developing.
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert RoastEventKind.RECOVERY_REQUIRED not in harness.events.kinds()
    rules = [e.rule for e in harness.sink.evaluations]
    assert "advisor_unavailable_exhausted" not in rules


@pytest.mark.asyncio
async def test_malformed_and_unsafe_do_not_count_toward_stop() -> None:
    """malformed / unsafe are provider-reachable (a different class): they never
    accrue toward the availability stop. Three of them in a row hold, they do
    not fail closed."""
    advisor = FakeAdvisor(
        [
            AdvisorFailureMode.MALFORMED,
            AdvisorFailureMode.UNSAFE,
            AdvisorFailureMode.MALFORMED,
        ]
    )
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    for _ in range(3):
        harness.controller.request_advisory()
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert RoastEventKind.RECOVERY_REQUIRED not in harness.events.kinds()
    advisor_rules = [e.rule for e in harness.sink.evaluations if e.rule.startswith("advisor_")]
    assert advisor_rules == [
        "advisor_malformed",
        "advisor_unsafe",
        "advisor_malformed",
    ]


@pytest.mark.asyncio
async def test_malformed_interleaved_does_not_reset_availability_streak() -> None:
    """A reachable-but-misbehaving (malformed) outcome between two availability
    failures neither counts toward nor resets the streak: provider_error,
    malformed, two more provider_errors → the 3rd availability failure trips."""
    advisor = FakeAdvisor(
        [
            AdvisorFailureMode.PROVIDER_ERROR,  # availability #1
            AdvisorFailureMode.MALFORMED,  # reachable: no count, no reset
            AdvisorFailureMode.PROVIDER_ERROR,  # availability #2
            AdvisorFailureMode.TIMEOUT,  # availability #3 → fail closed
        ]
    )
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    for _ in range(4):
        harness.controller.request_advisory()
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert harness.sink.evaluations[-1].rule == "advisor_unavailable_exhausted"


@pytest.mark.asyncio
async def test_paused_advisor_does_not_accrue_failures() -> None:
    """A paused advisor is never consulted, so its absent calls cannot accrue
    toward the stop. Pause, run three ticks (advisor would have crashed every
    time), then resume and fail twice more: still below the threshold."""
    advisor = FakeAdvisor([AdvisorFailureMode.PROVIDER_ERROR] * 5)
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    harness.controller.operator_pause_advisory()
    for _ in range(3):
        harness.controller.request_advisory()
        await harness.controller.tick()
    # No advisor consult happened while paused → no availability evaluations.
    assert not any(e.rule.startswith("advisor_unavailable") for e in harness.sink.evaluations)
    harness.controller.operator_resume_advisory()
    for _ in range(2):
        harness.controller.request_advisory()
        await harness.controller.tick()
    # Only two failures accrued post-resume → below the default threshold of 3.
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert RoastEventKind.RECOVERY_REQUIRED not in harness.events.kinds()


@pytest.mark.asyncio
async def test_operator_resume_resets_availability_streak() -> None:
    """After the stop trips and the operator resumes into development, the
    counter is clear: it takes a full N failures again to re-trip, not one."""
    advisor = FakeAdvisor([AdvisorFailureMode.PROVIDER_ERROR] * 6)
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    for _ in range(3):
        harness.controller.request_advisory()
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    harness.controller.operator_resume(RoastPhase.DEVELOPMENT)
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    # Two more failures: still below threshold thanks to the resume reset.
    for _ in range(2):
        harness.controller.request_advisory()
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_start_run_resets_availability_streak() -> None:
    """A new run starts the availability counter clean (D30): after failures
    accrued in a prior session, ``start_run`` clears them so the next run takes
    a full N failures to trip — not a single one."""
    advisor = FakeAdvisor([AdvisorFailureMode.PROVIDER_ERROR] * 4)
    harness = make_harness(readings=[reading()], advisor=advisor)
    # Pre-seed a stale streak from a prior (acknowledged) session.
    harness.controller._consecutive_advisor_failures = 2  # pyright: ignore[reportPrivateUsage]
    await harness.controller.start_run(PROFILE)  # idle → preheating; resets the streak
    assert harness.controller.phase is RoastPhase.PREHEATING
    # Two failures in the fresh run: with a clean reset this stays below the
    # default threshold of 3, so the roast keeps preheating. Without the reset
    # the pre-seeded 2 + these 2 would have tripped on the first failure.
    for _ in range(2):
        harness.controller.request_advisory()
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.PREHEATING
    assert RoastEventKind.RECOVERY_REQUIRED not in harness.events.kinds()


@pytest.mark.asyncio
async def test_read_failures_tolerated_then_fault_closed() -> None:
    """Two read faults are tolerated (ticks continue); the third (default
    threshold) fails closed into FAULTED."""
    harness = harness_in_development(
        readings=[RuntimeError("read fault")],  # repeats
    )
    await harness.controller.tick()
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    rules = [e.rule for e in harness.sink.evaluations]
    assert rules == ["mcp_read_failure_tolerated", "mcp_read_failure_tolerated"]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.sink.evaluations[-1].rule == "mcp_read_failures_exhausted"
    assert RoastEventKind.FAULT in harness.events.kinds()


@pytest.mark.asyncio
async def test_recovery_verdict_enters_recovery_phase() -> None:
    """Pre-T0 overrun (default severity) lands in operator_recovery_required
    and emits recovery_required."""
    harness = make_harness(readings=[reading(bean=205.0)])
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    harness.events.events.clear()
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    kinds = harness.events.kinds()
    assert RoastEventKind.RECOVERY_REQUIRED in kinds
    assert harness.sink.evaluations[-1].rule == "pre_t0_overrun"


@pytest.mark.asyncio
async def test_second_command_inside_rate_limit_rejected() -> None:
    advisor = FakeAdvisor([decision(), decision(heat=70, fan=55)])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.executor.targets == [(65, 50)]
    harness.clock.advance(0.5)  # inside min_seconds_between_commands (2.0)
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.executor.targets == [(65, 50)]  # no second write
    assert harness.sink.evaluations[-1].rule == "command_rate_limited"


@pytest.mark.asyncio
async def test_advisory_skipped_without_profile_or_telemetry() -> None:
    advisor = FakeAdvisor([decision()])
    harness = make_harness(readings=[None], advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()  # idle, no profile, no telemetry: no crash
    assert harness.executor.targets == []
    # No active run (no profile): the advisor is never reached, so a stray
    # pre-run manual request cannot burn the policy's interval baseline.
    assert advisor.contexts == []


# --- E4-S2: claude-review regression fixes ---


@pytest.mark.asyncio
async def test_stale_advisory_request_does_not_survive_a_fault() -> None:
    """Review finding 1: an advisory requested before a fail-closed tick
    must not fire on a later tick (it would run in FAULTED phase)."""
    advisor = FakeAdvisor([decision()])
    harness = harness_in_development(
        readings=[reading(bean=231.0), reading()],  # hot tick, then normal
        advisor=advisor,
    )
    harness.controller.request_advisory()
    await harness.controller.tick()  # e-stop + FAULTED; advisory must be dropped
    assert harness.controller.phase is RoastPhase.FAULTED
    await harness.controller.tick()  # normal reading, but faulted phase
    assert "advisor" not in harness.log
    assert harness.executor.targets == []


@pytest.mark.asyncio
async def test_advisory_gated_by_command_phase_matrix() -> None:
    """Review finding 2: the advisory path consults the E3-S5 matrix —
    SET_HEAT is invalid in cooling, so advice is rejected before the
    advisor is even called."""
    advisor = FakeAdvisor([decision()])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    harness.controller.transition_to(RoastPhase.COOLING)
    harness.log.clear()
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert "advisor" not in harness.log
    assert harness.executor.targets == []
    assert harness.sink.evaluations[-1].rule == "command_phase_validity"
    assert harness.sink.evaluations[-1].verdict is SafetyVerdict.REJECT


@pytest.mark.asyncio
async def test_failed_emergency_stop_still_faults() -> None:
    """Review finding 4: a raising e-stop command must not crash the tick
    loop or leave the phase pre-fault."""

    class FailingEstopExecutor(RecordingExecutor):
        async def emergency_stop(self, *, reason: str) -> None:
            raise RuntimeError("serial port dead")

    log: list[str] = []
    harness = make_harness(readings=[reading(bean=231.0)], executor=FailingEstopExecutor(log))
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:
        harness.controller.transition_to(step)
    harness.events.events.clear()
    await harness.controller.tick()  # must not raise
    assert harness.controller.phase is RoastPhase.FAULTED
    kinds = harness.events.kinds()
    assert RoastEventKind.COMMAND_FAILED in kinds
    assert RoastEventKind.FAULT in kinds


# --- E8-S3: change-based call-frequency policy ---


def _policy() -> AdvisoryCallPolicy:
    """A policy with the default ControllerConfig thresholds (temp 1.0 °C,
    RoR 2.0 °C/min, phase-keyed floors: preheat 30 s / pre-FC 10 s /
    development 0 = unthrottled, #171)."""
    return AdvisoryCallPolicy(ControllerConfig())


def test_policy_first_consult_in_advice_phase_is_phase_change() -> None:
    policy = _policy()
    trigger = policy.evaluate(
        phase=RoastPhase.PREHEATING, telemetry=reading(), now=0.0, manual_request=False
    )
    assert trigger is AdvisoryTrigger.PHASE_CHANGE


def test_policy_phase_transition_triggers() -> None:
    policy = _policy()
    policy.note_call(phase=RoastPhase.PREHEATING, telemetry=reading(), now=0.0)
    trigger = policy.evaluate(
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        telemetry=reading(),
        now=1.0,
        manual_request=False,
    )
    assert trigger is AdvisoryTrigger.PHASE_CHANGE


def test_policy_bean_temp_delta_triggers_at_threshold_not_below() -> None:
    # A throttled phase (10 s floor) so the sub-threshold case is genuinely
    # silent; under #171 DEVELOPMENT is unthrottled and would heartbeat.
    policy = _policy()
    policy.note_call(
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK, telemetry=reading(bean=200.0), now=0.0
    )
    # +0.5 °C, well inside the interval: no trigger.
    assert (
        policy.evaluate(
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            telemetry=reading(bean=200.5),
            now=1.0,
            manual_request=False,
        )
        is None
    )
    # +1.0 °C reaches the threshold.
    assert (
        policy.evaluate(
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            telemetry=reading(bean=201.0),
            now=1.0,
            manual_request=False,
        )
        is AdvisoryTrigger.BEAN_TEMP_DELTA
    )


def test_policy_ror_delta_triggers_at_threshold() -> None:
    # Throttled phase (10 s floor): the RoR delta is the *only* reason to fire
    # at 1 s, not the interval — under #171 DEVELOPMENT would heartbeat here.
    policy = _policy()
    policy.note_call(
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        telemetry=reading(bean=200.0, bean_ror_c_per_min=5.0),
        now=0.0,
    )
    # Same bean temp, RoR jumps +2.0 °C/min: RoR is the live trigger.
    trigger = policy.evaluate(
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
        telemetry=reading(bean=200.0, bean_ror_c_per_min=7.0),
        now=1.0,
        manual_request=False,
    )
    assert trigger is AdvisoryTrigger.ROR_DELTA


def test_policy_min_interval_heartbeat_preheat_is_30s() -> None:
    """#171: preheating's consult floor is 30 s — silent before, fires at."""
    policy = _policy()
    flat = reading(bean=200.0, bean_ror_c_per_min=5.0)
    policy.note_call(phase=RoastPhase.PREHEATING, telemetry=flat, now=0.0)
    # Flat telemetry just shy of the 30 s floor: silent (no per-tick spam).
    assert (
        policy.evaluate(phase=RoastPhase.PREHEATING, telemetry=flat, now=29.9, manual_request=False)
        is None
    )
    # At the floor the heartbeat fires.
    assert (
        policy.evaluate(phase=RoastPhase.PREHEATING, telemetry=flat, now=30.0, manual_request=False)
        is AdvisoryTrigger.MIN_INTERVAL
    )


def test_policy_min_interval_heartbeat_charged_is_10s() -> None:
    """#171: beans-charged / pre-FC roasting floor is 10 s."""
    policy = _policy()
    flat = reading(bean=200.0, bean_ror_c_per_min=5.0)
    policy.note_call(phase=RoastPhase.ROASTING_PRE_FIRST_CRACK, telemetry=flat, now=0.0)
    assert (
        policy.evaluate(
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            telemetry=flat,
            now=9.9,
            manual_request=False,
        )
        is None
    )
    assert (
        policy.evaluate(
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            telemetry=flat,
            now=10.0,
            manual_request=False,
        )
        is AdvisoryTrigger.MIN_INTERVAL
    )


def test_policy_development_is_unthrottled_back_to_back() -> None:
    """#171: development (first crack onward) has a 0 floor — the heartbeat
    fires on the very next tick after a consult returns, so consults run
    back-to-back, bounded only by advisor latency (calls are serial)."""
    policy = _policy()
    flat = reading(bean=200.0, bean_ror_c_per_min=5.0)
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=0.0)
    # 1 s later (one tick), with no temp/RoR change, the consult fires again.
    assert (
        policy.evaluate(phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=1.0, manual_request=False)
        is AdvisoryTrigger.MIN_INTERVAL
    )
    # And again the next tick after recording the prior call.
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=1.0)
    assert (
        policy.evaluate(phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=2.0, manual_request=False)
        is AdvisoryTrigger.MIN_INTERVAL
    )


def test_policy_does_not_fire_every_tick_on_flat_telemetry_when_throttled() -> None:
    """The whole point of a non-zero floor: a stable roast between heartbeats
    is silent. In charged/pre-FC (10 s floor) flat ticks before 10 s are
    quiet."""
    policy = _policy()
    flat = reading(bean=200.0, bean_ror_c_per_min=5.0)
    policy.note_call(phase=RoastPhase.ROASTING_PRE_FIRST_CRACK, telemetry=flat, now=0.0)
    fired = [
        policy.evaluate(
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            telemetry=flat,
            now=t,
            manual_request=False,
        )
        for t in (1.0, 2.0, 3.0, 5.0, 9.0)
    ]
    assert fired == [None, None, None, None, None]


def test_policy_change_trigger_fires_early_within_phase_interval() -> None:
    """#171: the interval is a floor, not a gate — a large bean-temp jump
    consults *before* the phase interval elapses (preheat's 30 s floor here),
    preserving the responsive change-based behavior."""
    policy = _policy()
    policy.note_call(phase=RoastPhase.PREHEATING, telemetry=reading(bean=200.0), now=0.0)
    # 1 s in, far short of the 30 s floor, a +5 °C jump fires immediately.
    trigger = policy.evaluate(
        phase=RoastPhase.PREHEATING,
        telemetry=reading(bean=205.0),
        now=1.0,
        manual_request=False,
    )
    assert trigger is AdvisoryTrigger.BEAN_TEMP_DELTA


def test_policy_manual_bypasses_phase_scoping_and_interval() -> None:
    policy = _policy()
    # Cooling is not an advice phase and no telemetry — manual still wins.
    trigger = policy.evaluate(
        phase=RoastPhase.COOLING, telemetry=None, now=0.0, manual_request=True
    )
    assert trigger is AdvisoryTrigger.MANUAL


def test_policy_manual_takes_precedence_over_automatic_trigger() -> None:
    policy = _policy()
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=reading(bean=200.0), now=0.0)
    # A big delta would fire BEAN_TEMP_DELTA, but manual is reported instead.
    trigger = policy.evaluate(
        phase=RoastPhase.DEVELOPMENT,
        telemetry=reading(bean=250.0),
        now=1.0,
        manual_request=True,
    )
    assert trigger is AdvisoryTrigger.MANUAL


@pytest.mark.parametrize(
    "phase",
    [
        RoastPhase.IDLE,
        RoastPhase.STARTING,
        RoastPhase.COOLING,
        RoastPhase.COMPLETE,
        RoastPhase.FAULTED,
        RoastPhase.OPERATOR_RECOVERY_REQUIRED,
    ],
)
def test_policy_no_automatic_call_outside_advice_phases(phase: RoastPhase) -> None:
    policy = _policy()
    trigger = policy.evaluate(
        phase=phase, telemetry=reading(bean=999.0), now=999.0, manual_request=False
    )
    assert trigger is None


def test_policy_baseline_advances_on_each_call() -> None:
    """Deltas measure from the last call, not the start: after a call at
    201 °C, a further +0.5 °C is below threshold again. Uses a throttled
    phase (10 s floor) so the below-threshold tick is silent under #171."""
    policy = _policy()
    policy.note_call(
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK, telemetry=reading(bean=200.0), now=0.0
    )
    assert (
        policy.evaluate(
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            telemetry=reading(bean=201.0),
            now=1.0,
            manual_request=False,
        )
        is AdvisoryTrigger.BEAN_TEMP_DELTA
    )
    policy.note_call(
        phase=RoastPhase.ROASTING_PRE_FIRST_CRACK, telemetry=reading(bean=201.0), now=1.0
    )
    assert (
        policy.evaluate(
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            telemetry=reading(bean=201.5),
            now=2.0,
            manual_request=False,
        )
        is None
    )


def test_policy_manual_consult_with_no_telemetry_keeps_delta_baseline() -> None:
    """A manual consult mid-roast with a dropped reading must not blank the
    temp baseline — the next real delta still measures from the prior call."""
    policy = _policy()
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=reading(bean=200.0), now=0.0)
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=None, now=1.0)
    trigger = policy.evaluate(
        phase=RoastPhase.DEVELOPMENT,
        telemetry=reading(bean=201.0),
        now=2.0,
        manual_request=False,
    )
    assert trigger is AdvisoryTrigger.BEAN_TEMP_DELTA


@pytest.mark.asyncio
async def test_automatic_advisory_fires_without_manual_request() -> None:
    """Integration: entering development consults the advisor automatically
    (no request_advisory), applies the advice, and tags the ADVISORY event
    with the phase-change trigger."""
    advisor = FakeAdvisor([decision(heat=60, fan=50)])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    await harness.controller.tick()
    assert advisor.contexts  # advisor was consulted with no manual trigger
    assert harness.executor.targets == [(60, 50)]
    advisory_events = [
        cast(dict[str, object], p) for k, p in harness.events.events if k is RoastEventKind.ADVISORY
    ]
    assert advisory_events
    assert advisory_events[-1]["trigger"] == AdvisoryTrigger.PHASE_CHANGE.value


@pytest.mark.asyncio
async def test_advisor_context_carries_profile_stage_targets() -> None:
    """The advisor context is populated with the frozen profile's stage targets
    (#172): the development-ratio target and the charge guidance band, so the
    stage-tuned prompt has explicit goals to aim at. Context-population only —
    no control logic reads these back."""
    advisor = FakeAdvisor([decision(heat=60, fan=50)])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    await harness.controller.tick()
    assert advisor.contexts
    ctx = advisor.contexts[-1]
    assert ctx.target_development_percent == PROFILE.target_development_percent
    assert ctx.charge_guidance_min_c == PROFILE.charge_guidance_min_c
    assert ctx.charge_guidance_max_c == PROFILE.charge_guidance_max_c


@pytest.mark.asyncio
async def test_no_automatic_advisory_in_cooling() -> None:
    """The automatic policy is silent outside advice phases — cooling gets no
    advisory call or rejection-spam (manual would still be answered)."""
    advisor = FakeAdvisor([], default_decision=decision())
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    harness.controller.transition_to(RoastPhase.COOLING)
    harness.log.clear()
    harness.events.events.clear()
    await harness.controller.tick()
    assert "advisor" not in harness.log
    assert RoastEventKind.ADVISORY not in harness.events.kinds()


@pytest.mark.asyncio
async def test_manual_advisory_without_telemetry_emits_skipped_event() -> None:
    """Review follow-up (#61): a manual request that reaches the advisory step
    with no telemetry (the residual terminal-phase case — advice phases fault
    on missing telemetry first) must not be silently swallowed. The advisor is
    not consulted, but a skipped ADVISORY event surfaces that the request
    landed."""
    advisor = FakeAdvisor([decision()])
    harness = make_harness(readings=[None], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:6]:  # …→ COMPLETE (a non-active-roast phase)
        harness.controller.transition_to(step)
    assert harness.controller.phase is RoastPhase.COMPLETE
    assert harness.executor.targets == []  # precondition: no writes from transitions
    harness.events.events.clear()
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert advisor.contexts == []  # advisor never reached
    assert harness.executor.targets == []  # and still no write after the skip
    advisory_events = [
        cast(dict[str, object], p) for k, p in harness.events.events if k is RoastEventKind.ADVISORY
    ]
    assert advisory_events == [{"trigger": AdvisoryTrigger.MANUAL.value, "skipped": "no_telemetry"}]


# --- E4-S3: T0 debounce and add-beans guidance ---


def harness_preheating(*, readings: list[RoastTelemetry | None | Exception]) -> Harness:
    harness = make_harness(readings=readings)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    harness.log.clear()
    harness.events.events.clear()
    return harness


@pytest.mark.asyncio
async def test_charge_guidance_emitted_exactly_once() -> None:
    harness = harness_preheating(
        readings=[
            reading(bean=150.0, env=150.0),
            reading(bean=175.0, env=150.0),
            reading(bean=185.0, env=150.0),
        ]
    )
    await harness.controller.tick()  # below range: nothing
    assert RoastEventKind.CHARGE_GUIDANCE not in harness.events.kinds()
    await harness.controller.tick()  # enters range: emitted
    assert harness.events.kinds().count(RoastEventKind.CHARGE_GUIDANCE) == 1
    await harness.controller.tick()  # still in range: not repeated
    assert harness.events.kinds().count(RoastEventKind.CHARGE_GUIDANCE) == 1


@pytest.mark.asyncio
async def test_charge_guidance_triggers_on_environment_temperature() -> None:
    """Plan: bean OR environment entering the band triggers guidance."""
    harness = harness_preheating(readings=[reading(bean=120.0, env=180.0)])
    await harness.controller.tick()
    assert RoastEventKind.CHARGE_GUIDANCE in harness.events.kinds()


@pytest.mark.asyncio
async def test_charge_guidance_only_in_preheating() -> None:
    harness = harness_in_development(readings=[reading(bean=185.0)])
    await harness.controller.tick()
    assert RoastEventKind.CHARGE_GUIDANCE not in harness.events.kinds()


@pytest.mark.asyncio
async def test_t0_debounce_confirms_after_three_consecutive_ticks() -> None:
    t0 = reading(bean=160.0, t0_detected=True)
    harness = harness_preheating(readings=[t0, t0, t0])
    await harness.controller.tick()
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.PREHEATING  # 2 < 3
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    kinds = harness.events.kinds()
    assert kinds.count(RoastEventKind.T0_DETECTED) == 1
    # Cause before effect: T0_DETECTED precedes the PHASE_CHANGED it explains.
    assert kinds.index(RoastEventKind.T0_DETECTED) < kinds.index(RoastEventKind.PHASE_CHANGED)


@pytest.mark.asyncio
async def test_t0_debounce_resets_when_t0_absent() -> None:
    t0 = reading(bean=160.0, t0_detected=True)
    plain = reading(bean=160.0)
    harness = harness_preheating(readings=[t0, t0, plain, t0, t0, t0])
    for _ in range(5):
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.PREHEATING  # streak broken at tick 3
    await harness.controller.tick()  # third consecutive after the break
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK


@pytest.mark.asyncio
async def test_t0_debounce_resets_on_read_fault() -> None:
    """Plan §2: flapping originates from read faults — a tolerated failed
    read breaks the confirmation window."""
    t0 = reading(bean=160.0, t0_detected=True)
    harness = harness_preheating(readings=[t0, t0, RuntimeError("transient"), t0, t0, t0])
    for _ in range(5):
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.PREHEATING
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK


@pytest.mark.asyncio
async def test_debounced_t0_replaces_phase_proxy_in_safety() -> None:
    """Carry-forward (E4-S2 review): the safety evaluation now receives the
    real debounced confirmation. During the debounce window the overrun
    rule still sees t0_confirmed=False — a hot unconfirmed preheat is an
    overrun even while T0 is being observed."""
    hot_t0 = reading(bean=205.0, t0_detected=True)
    harness = harness_preheating(readings=[hot_t0])
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert harness.sink.evaluations[-1].rule == "pre_t0_overrun"


@pytest.mark.asyncio
async def test_per_run_latches_reset_on_new_run() -> None:
    t0 = reading(bean=175.0, t0_detected=True)
    harness = harness_preheating(readings=[t0, t0, t0])
    for _ in range(3):
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # Finish the run and start a new one: latches must be fresh.
    for step in (
        RoastPhase.DEVELOPMENT,
        RoastPhase.COOLING,
        RoastPhase.COMPLETE,
        RoastPhase.IDLE,
        RoastPhase.STARTING,
    ):
        harness.controller.transition_to(step)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    harness.events.events.clear()
    harness.reader.readings = [t0, t0, t0]
    await harness.controller.tick()
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.PREHEATING  # streak restarted
    assert harness.events.kinds().count(RoastEventKind.CHARGE_GUIDANCE) == 1  # re-armed
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK


@pytest.mark.asyncio
async def test_overrun_guard_rearms_on_recovery_resume_into_preheating() -> None:
    """Safety review (E4-S3): a recovery-resume into preheating declares
    'back before charge' — a stale T0 confirmation must not disarm the
    pre-T0 overrun rule."""
    t0 = reading(bean=175.0, t0_detected=True)
    harness = harness_preheating(readings=[t0, t0, t0, reading(bean=205.0)])
    for _ in range(3):
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK  # T0 confirmed
    harness.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    harness.controller.transition_to(RoastPhase.PREHEATING)  # operator resume
    await harness.controller.tick()  # hot unconfirmed preheat
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert harness.sink.evaluations[-1].rule == "pre_t0_overrun"


# --- E4-S4: run lifecycle, restart recovery, fake-MCP full roast ---


@pytest.mark.asyncio
async def test_start_run_is_serialized() -> None:
    harness = make_harness(readings=[reading()])
    await harness.controller.start_run(PROFILE)
    assert harness.controller.phase is RoastPhase.PREHEATING
    assert harness.executor.commands.count("start_session") == 1
    with pytest.raises(InvalidTransitionError):
        await harness.controller.start_run(PROFILE)  # second start: rejected
    assert harness.executor.commands.count("start_session") == 1  # no second MCP call


@pytest.mark.asyncio
async def test_start_run_applies_validated_initial_targets() -> None:
    harness = make_harness(readings=[reading()])
    await harness.controller.start_run(PROFILE)
    assert harness.executor.targets == [(70, 40)]  # profile initials via safety policy
    rules = [e.rule for e in harness.sink.evaluations]
    assert "all_clear" in rules


@pytest.mark.asyncio
async def test_failed_start_session_faults_cleanly() -> None:
    class FailingStartExecutor(RecordingExecutor):
        async def start_session(self) -> None:
            raise RuntimeError("mcp down")

    harness = make_harness(readings=[reading()], executor=FailingStartExecutor())
    await harness.controller.start_run(PROFILE)
    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.executor.targets == []  # no half-started run, no writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "persisted",
    [
        RoastPhase.STARTING,
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
        RoastPhase.DEVELOPMENT,
        RoastPhase.COOLING,
        RoastPhase.FAULTED,
        RoastPhase.OPERATOR_RECOVERY_REQUIRED,
    ],
)
async def test_restart_with_possibly_active_run_enters_recovery(
    persisted: RoastPhase,
) -> None:
    harness = make_harness(readings=[reading()])
    await harness.controller.recover_from_restart(persisted)
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert harness.executor.targets == []  # heat/fan never auto-resumed
    assert harness.executor.commands == []  # no MCP write of any kind
    assert harness.sink.evaluations[-1].rule == "restart_recovery"
    assert RoastEventKind.RECOVERY_REQUIRED in harness.events.kinds()


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [None, RoastPhase.IDLE, RoastPhase.COMPLETE])
async def test_restart_with_no_active_run_stays_idle(persisted: RoastPhase | None) -> None:
    harness = make_harness(readings=[reading()])
    await harness.controller.recover_from_restart(persisted)
    assert harness.controller.phase is RoastPhase.IDLE


@pytest.mark.asyncio
async def test_emergency_stop_available_in_recovery() -> None:
    harness = make_harness(readings=[reading()])
    await harness.controller.recover_from_restart(RoastPhase.DEVELOPMENT)
    await harness.controller.operator_emergency_stop("operator pressed e-stop")
    assert harness.executor.estop_reasons
    assert harness.controller.phase is RoastPhase.FAULTED


@pytest.mark.asyncio
async def test_resume_requires_recovery_and_never_writes_heat() -> None:
    harness = make_harness(readings=[reading()])
    await harness.controller.recover_from_restart(RoastPhase.DEVELOPMENT)
    harness.controller.operator_resume(RoastPhase.DEVELOPMENT)
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert harness.executor.targets == []  # heat stays 0 until separately commanded
    # And resume is gated: not callable outside recovery.
    with pytest.raises(InvalidTransitionError):
        harness.controller.operator_resume(RoastPhase.COOLING)


@pytest.mark.asyncio
async def test_resume_to_starting_is_never_legal() -> None:
    harness = make_harness(readings=[reading()])
    await harness.controller.recover_from_restart(RoastPhase.PREHEATING)
    with pytest.raises(InvalidTransitionError):
        harness.controller.operator_resume(RoastPhase.STARTING)


@pytest.mark.asyncio
async def test_fault_entry_applies_hardware_off() -> None:
    """E3-S2 carry-forward: stale telemetry FAULT writes heat 0 / safe fan
    before the transition commits."""
    harness = harness_in_development(readings=[reading(age_seconds=10.0)])
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.executor.targets == [(0, 100)]


@pytest.mark.asyncio
async def test_failed_hardware_off_still_faults() -> None:
    class FailingTargetsExecutor(RecordingExecutor):
        async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
            raise RuntimeError("write failed")

    harness = make_harness(readings=[reading(age_seconds=10.0)], executor=FailingTargetsExecutor())
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:
        harness.controller.transition_to(step)
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.FAULTED
    assert RoastEventKind.COMMAND_FAILED in harness.events.kinds()


@pytest.mark.asyncio
async def test_operator_timeout_alerts_once_in_recovery_only() -> None:
    harness = make_harness(readings=[reading()])
    await harness.controller.recover_from_restart(RoastPhase.DEVELOPMENT)
    harness.clock.advance(601.0)
    await harness.controller.tick()
    await harness.controller.tick()
    kinds = harness.events.kinds()
    assert kinds.count(RoastEventKind.SAFETY_ALERT) == 1  # once, not per tick


@pytest.mark.asyncio
async def test_operator_timeout_never_fires_in_normal_phases() -> None:
    harness = harness_in_development(readings=[reading()])
    harness.clock.advance(10_000.0)
    await harness.controller.tick()
    assert RoastEventKind.SAFETY_ALERT not in harness.events.kinds()


@pytest.mark.asyncio
async def test_mcp_first_crack_transitions_with_source_stamp() -> None:
    fc = reading(bean=196.0, first_crack_detected=True)
    harness = make_harness(readings=[fc])
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:  # …→ ROASTING_PRE_FIRST_CRACK
        harness.controller.transition_to(step)
    harness.events.events.clear()
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    source_evals = [e for e in harness.sink.evaluations if e.rule == "event_source_validity"]
    assert len(source_evals) == 1
    assert source_evals[0].verdict is SafetyVerdict.ALLOW
    kinds = harness.events.kinds()
    assert kinds.index(RoastEventKind.FIRST_CRACK) < kinds.index(RoastEventKind.PHASE_CHANGED)


@pytest.mark.asyncio
async def test_operator_first_crack_override_stamped_and_gated() -> None:
    harness = make_harness(readings=[reading()])
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:
        harness.controller.transition_to(step)
    await harness.controller.operator_mark_first_crack()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert "mark_first_crack" in harness.executor.commands
    # And gated by the matrix outside roasting:
    harness2 = make_harness(readings=[reading()])
    harness2.controller.load_profile(PROFILE)
    harness2.controller.transition_to(RoastPhase.STARTING)
    harness2.controller.transition_to(RoastPhase.PREHEATING)
    await harness2.controller.operator_mark_first_crack()
    assert harness2.controller.phase is RoastPhase.PREHEATING  # rejected, no transition
    assert harness2.executor.commands == []


@pytest.mark.asyncio
async def test_development_elapsed_none_before_first_crack() -> None:
    """The advisor context carries ``development_elapsed_seconds=None`` until
    first crack arms the development clock."""
    advisor = FakeAdvisor([decision()])
    harness = make_harness(readings=[reading()], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:  # …→ ROASTING_PRE_FIRST_CRACK
        harness.controller.transition_to(step)
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert advisor.contexts[-1].development_elapsed_seconds is None


@pytest.mark.asyncio
async def test_development_elapsed_tracks_seconds_since_first_crack() -> None:
    """Once first crack transitions into development, the context's
    ``development_elapsed_seconds`` is the wall-clock time since that
    transition (the DTR clock the advisor reasons about)."""
    advisor = FakeAdvisor([decision()])
    harness = make_harness(readings=[reading()], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:
        harness.controller.transition_to(step)
    harness.clock.advance(300.0)  # pre-FC roast time
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)  # arms the FC clock
    harness.clock.advance(45.0)  # development time
    harness.controller.request_advisory()
    await harness.controller.tick()
    # FakeClock advances by exact floats, so the elapsed is exactly 45.0.
    assert advisor.contexts[-1].development_elapsed_seconds == 45.0


@pytest.mark.asyncio
async def test_development_clock_resets_on_new_run() -> None:
    """A new run/preheat clears the development clock, so a stale FC time from
    a prior run never leaks into the next run's advisor context."""
    advisor = FakeAdvisor([decision(), decision()])
    harness = make_harness(readings=[reading(), reading()], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT (arms the clock)
        harness.controller.transition_to(step)
    # Finish the run, then start a fresh one along the legal path. The
    # STARTING/PREHEATING entry must clear the development clock.
    for step in [
        RoastPhase.COOLING,
        RoastPhase.COMPLETE,
        RoastPhase.IDLE,
        RoastPhase.STARTING,
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
    ]:
        harness.controller.transition_to(step)
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert advisor.contexts[-1].development_elapsed_seconds is None


@pytest.mark.asyncio
async def test_advisor_drop_now_executes() -> None:
    advisor = FakeAdvisor([decision(drop=True)])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert "drop_beans" in harness.executor.commands
    assert harness.controller.phase is RoastPhase.COOLING


@pytest.mark.asyncio
async def test_full_mock_roast_with_fake_mcp() -> None:
    """E4-S4 capstone (updated E8-S3): a complete scripted roast through the
    FakeMCPClient — start → preheat → debounced T0 → MCP first crack →
    advisor drop → operator stop-cooling → complete.

    The advisor advises ``drop=True`` throughout, but the change-based
    call-frequency policy consults it automatically (no manual trigger), and
    drop-eligibility honours the recommendation only once development begins
    — so the drop fires on entering development, and every earlier
    ``drop=True`` in preheating/pre-FC is safely rejected. This is the
    architecture invariant in motion: the advisor keeps advising, the
    controller decides when it is safe to obey."""
    warm = reading(bean=120.0, env=140.0)
    charge = reading(bean=178.0, env=185.0)
    t0 = reading(bean=95.0, t0_detected=True)  # charge drop, T0 reported
    fc = reading(bean=196.0, t0_detected=True, first_crack_detected=True)
    dev = reading(bean=200.0, t0_detected=True, first_crack_detected=True)
    log: list[str] = []
    mcp = FakeMCPClient([warm, charge, t0, t0, t0, fc, dev], log)
    events = EventSink(log)
    sink = RecordingSnapshotSink(log)
    # Constant advice via default_decision: consulted automatically on every
    # meaningful change, drop honoured only in development.
    advisor = FakeAdvisor([], default_decision=decision(heat=40, fan=60, drop=True), log=log)
    clock = FakeClock()
    controller = RoastController(
        config=ControllerConfig(),
        safety=SafetyPolicy(SafetyLimits()),
        state_reader=mcp,
        command_executor=mcp,
        snapshot_sink=sink,
        event_emitter=events,
        advisor=advisor,
        clock=clock,
    )
    await controller.start_run(PROFILE)
    assert controller.phase is RoastPhase.PREHEATING
    for _ in range(2):  # warm + charge guidance frames
        await controller.tick()
        clock.advance(2.5)
    assert RoastEventKind.CHARGE_GUIDANCE in events.kinds()
    for _ in range(3):  # T0 debounce
        await controller.tick()
        clock.advance(2.5)
    assert controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # The FC frame enters development and, in the same tick, the automatic
    # advisory consult drops the beans (drop now eligible) → cooling.
    await controller.tick()
    clock.advance(2.5)
    assert controller.phase is RoastPhase.COOLING
    await controller.operator_stop_cooling()
    assert controller.phase is RoastPhase.COMPLETE
    commands = mcp.commands()
    # Lifecycle bookends and the one drop, in order — set_targets recurs as
    # advisory maintains targets across the roast, so assert the invariants,
    # not an exact count.
    assert commands[0] == "start_session"
    assert commands[-1] == "stop_cooling"
    assert commands.count("drop_beans") == 1
    assert commands.index("drop_beans") < commands.index("stop_cooling")
    assert commands.count("set_targets") >= 2  # profile initials + advisory
    kinds = events.kinds()
    for expected in (
        RoastEventKind.RUN_STARTED,
        RoastEventKind.CHARGE_GUIDANCE,
        RoastEventKind.T0_DETECTED,
        RoastEventKind.FIRST_CRACK,
        RoastEventKind.ADVISORY,
        RoastEventKind.RUN_COMPLETED,
    ):
        assert expected in kinds


@pytest.mark.asyncio
async def test_operator_early_abort_drop_from_roasting() -> None:
    """Safety review blocker (E4-S4): an operator early-abort drop during
    roasting_pre_first_crack must land in cooling — never fire the
    hardware drop and then fail the transition."""
    harness = make_harness(readings=[reading()])
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:  # …→ ROASTING_PRE_FIRST_CRACK
        harness.controller.transition_to(step)
    await harness.controller.operator_drop_beans()
    assert harness.controller.phase is RoastPhase.COOLING
    assert "drop_beans" in harness.executor.commands


# --- E4-S4: review fixes + coverage for failure branches ---


class FailingCommandExecutor(RecordingExecutor):
    """Fails the named commands; records everything else."""

    def __init__(self, failing: set[str], log: list[str] | None = None) -> None:
        super().__init__(log)
        self._failing = failing

    async def mark_beans_added(self) -> None:
        if "mark_beans_added" in self._failing:
            raise RuntimeError("write failed")
        await super().mark_beans_added()

    async def mark_first_crack(self) -> None:
        if "mark_first_crack" in self._failing:
            raise RuntimeError("write failed")
        await super().mark_first_crack()

    async def drop_beans(self) -> None:
        if "drop_beans" in self._failing:
            raise RuntimeError("write failed")
        await super().drop_beans()

    async def start_cooling(self) -> None:
        if "start_cooling" in self._failing:
            raise RuntimeError("write failed")
        await super().start_cooling()

    async def stop_cooling(self) -> None:
        if "stop_cooling" in self._failing:
            raise RuntimeError("write failed")
        await super().stop_cooling()

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        if "set_targets" in self._failing:
            raise RuntimeError("write failed")
        await super().set_targets(heat_percent=heat_percent, fan_percent=fan_percent)


def harness_in(phase_steps: int, executor: RecordingExecutor | None = None) -> Harness:
    harness = make_harness(readings=[reading()], executor=executor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:phase_steps]:
        harness.controller.transition_to(step)
    harness.events.events.clear()
    return harness


@pytest.mark.asyncio
async def test_acknowledge_fault_returns_to_idle() -> None:
    harness = make_harness(readings=[reading()])
    harness.controller.transition_to(RoastPhase.FAULTED)
    harness.controller.operator_acknowledge_fault()
    assert harness.controller.phase is RoastPhase.IDLE
    acked = [p for k, p in harness.events.events if k is RoastEventKind.RECOVERY_ACKNOWLEDGED]
    assert acked == [{"acknowledged": "faulted"}]


@pytest.mark.asyncio
async def test_acknowledge_also_resets_completed_run() -> None:
    """Review finding 4: complete → idle uses the same reset path."""
    harness = harness_in(6)  # …→ COMPLETE
    harness.controller.operator_acknowledge_fault()
    assert harness.controller.phase is RoastPhase.IDLE
    acked = [p for k, p in harness.events.events if k is RoastEventKind.RECOVERY_ACKNOWLEDGED]
    assert acked == [{"acknowledged": "complete"}]  # truthful payload, not "fault"


@pytest.mark.asyncio
async def test_failed_operator_writes_surface_and_hold_phase() -> None:
    """A failed MCP write emits COMMAND_FAILED and never moves the phase."""
    fc = harness_in(3, FailingCommandExecutor({"mark_first_crack"}))
    await fc.controller.operator_mark_first_crack()
    assert fc.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert RoastEventKind.COMMAND_FAILED in fc.events.kinds()

    drop = harness_in(4, FailingCommandExecutor({"drop_beans"}))
    await drop.controller.operator_drop_beans()
    assert drop.controller.phase is RoastPhase.DEVELOPMENT
    assert RoastEventKind.COMMAND_FAILED in drop.events.kinds()

    cool = harness_in(5, FailingCommandExecutor({"stop_cooling"}))
    await cool.controller.operator_stop_cooling()
    assert cool.controller.phase is RoastPhase.COOLING
    assert RoastEventKind.COMMAND_FAILED in cool.events.kinds()


@pytest.mark.asyncio
async def test_failed_advisor_drop_write_holds_phase() -> None:
    advisor = FakeAdvisor([decision(drop=True)])
    harness = make_harness(
        readings=[reading()],
        advisor=advisor,
        executor=FailingCommandExecutor({"drop_beans"}),
    )
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:
        harness.controller.transition_to(step)
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert RoastEventKind.COMMAND_FAILED in harness.events.kinds()


@pytest.mark.asyncio
async def test_failed_initial_targets_surface_but_run_continues() -> None:
    harness = make_harness(readings=[reading()], executor=FailingCommandExecutor({"set_targets"}))
    await harness.controller.start_run(PROFILE)
    assert harness.controller.phase is RoastPhase.PREHEATING  # run proceeds
    assert RoastEventKind.COMMAND_FAILED in harness.events.kinds()


@pytest.mark.asyncio
async def test_back_to_back_runs_are_not_rate_limited() -> None:
    """Review finding 1: a new run resets the rate-limit baseline, so the
    initial profile targets are never silently rejected."""
    harness = make_harness(readings=[reading()])
    await harness.controller.start_run(PROFILE)
    assert harness.executor.targets == [(70, 40)]
    # Finish the run instantly (no clock advance) and start the next.
    for step in NORMAL_PATH[2:6]:
        harness.controller.transition_to(step)
    harness.controller.operator_acknowledge_fault()  # complete → idle
    await harness.controller.start_run(PROFILE)
    assert harness.executor.targets == [(70, 40), (70, 40)]  # second write happened


@pytest.mark.asyncio
async def test_stop_cooling_matrix_rejected_outside_cooling() -> None:
    harness = harness_in(4)  # DEVELOPMENT — the D16 canonical invalid
    await harness.controller.operator_stop_cooling()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert harness.sink.evaluations[-1].rule == "command_phase_validity"
    assert harness.executor.commands == []


# --- E4-S4: the defense-in-depth guards, tested via drifted policies ---


class RejectingSourcePolicy(SafetyPolicy):
    """Simulates a future rule change where event sources get rejected —
    the controller must honor the verdict and hold the phase."""

    def evaluate_event_source(self, *, transition: str, source: object) -> SafetyEvaluation:  # pyright: ignore[reportIncompatibleMethodOverride]
        return SafetyEvaluation(
            rule="event_source_validity",
            verdict=SafetyVerdict.REJECT,
            reason="drifted policy rejects all sources",
        )


class PermissiveMatrixPolicy(SafetyPolicy):
    """Simulates matrix/table drift: the matrix allows everything — the
    controller's pre-write can_transition guards are the last line."""

    def evaluate_command_phase(self, *, command: object, phase: object) -> SafetyEvaluation:  # pyright: ignore[reportIncompatibleMethodOverride]
        return SafetyEvaluation(
            rule="command_phase_validity",
            verdict=SafetyVerdict.ALLOW,
            reason="drifted permissive matrix",
        )


def harness_with_policy(policy: SafetyPolicy, *, steps: int = 0) -> Harness:
    log: list[str] = []
    clock = FakeClock()
    reader = ScriptedStateReader([reading(t0_detected=True, first_crack_detected=True)], log)
    executor = RecordingExecutor(log)
    sink = RecordingSnapshotSink(log)
    events = EventSink(log)
    controller = RoastController(
        config=ControllerConfig(),
        safety=policy,
        state_reader=reader,
        command_executor=executor,
        snapshot_sink=sink,
        event_emitter=events,
        advisor=None,
        clock=clock,
    )
    harness = Harness(controller, reader, executor, sink, events, clock, log)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:steps]:
        harness.controller.transition_to(step)
    return harness


@pytest.mark.asyncio
async def test_rejected_t0_source_holds_preheating() -> None:
    harness = harness_with_policy(RejectingSourcePolicy(SafetyLimits()), steps=2)
    for _ in range(4):  # past the debounce threshold
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.PREHEATING  # verdict honored


@pytest.mark.asyncio
async def test_rejected_fc_source_holds_roasting() -> None:
    harness = harness_with_policy(RejectingSourcePolicy(SafetyLimits()), steps=3)
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK


@pytest.mark.asyncio
async def test_drift_guards_block_writes_when_matrix_is_permissive() -> None:
    """If the matrix ever allows a command whose resulting state is not
    reachable, the pre-write guard raises BEFORE any hardware write."""
    policy = PermissiveMatrixPolicy(SafetyLimits())
    drop = harness_with_policy(policy, steps=6)  # COMPLETE: cooling unreachable
    with pytest.raises(InvalidTransitionError):
        await drop.controller.operator_drop_beans()
    assert drop.executor.commands == []

    fc = harness_with_policy(policy)  # IDLE: development unreachable
    with pytest.raises(InvalidTransitionError):
        await fc.controller.operator_mark_first_crack()
    assert fc.executor.commands == []

    cool = harness_with_policy(policy)  # IDLE: complete unreachable
    with pytest.raises(InvalidTransitionError):
        await cool.controller.operator_stop_cooling()
    assert cool.executor.commands == []


class RejectingCommandPolicy(SafetyPolicy):
    def evaluate_command(
        self, *, requested_heat: int, requested_fan: int, seconds_since_last_command: float | None
    ) -> SafetyEvaluation:
        return SafetyEvaluation(
            rule="command_rate_limited",
            verdict=SafetyVerdict.REJECT,
            reason="drifted policy rejects all commands",
        )


@pytest.mark.asyncio
async def test_rejected_initial_targets_are_not_written() -> None:
    harness = harness_with_policy(RejectingCommandPolicy(SafetyLimits()))
    await harness.controller.start_run(PROFILE)
    assert harness.controller.phase is RoastPhase.PREHEATING
    assert harness.executor.targets == []  # REJECT: _execute_targets skips


class FaultWithoutValuesPolicy(SafetyPolicy):
    def evaluate_telemetry(
        self, *, phase: RoastPhase, bean_temp_c: float, env_temp_c: float, t0_confirmed: bool
    ) -> SafetyEvaluation:
        return SafetyEvaluation(
            rule="synthetic_fault",
            verdict=SafetyVerdict.FAULT,
            reason="drifted policy faults without adjusted values",
        )


@pytest.mark.asyncio
async def test_fault_without_adjusted_values_skips_hardware_off() -> None:
    """_apply_fail_safe tolerates evaluations with no adjusted command —
    the transition still commits."""
    harness = harness_with_policy(FaultWithoutValuesPolicy(SafetyLimits()), steps=4)
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.executor.targets == []


class ClampingTelemetryPolicy(SafetyPolicy):
    def evaluate_telemetry(
        self, *, phase: RoastPhase, bean_temp_c: float, env_temp_c: float, t0_confirmed: bool
    ) -> SafetyEvaluation:
        return SafetyEvaluation(
            rule="synthetic_clamp",
            verdict=SafetyVerdict.CLAMP,
            adjusted_heat=50,
            adjusted_fan=50,
            reason="drifted policy emits CLAMP from telemetry stage",
        )


@pytest.mark.asyncio
async def test_unexpected_telemetry_clamp_does_not_stop_the_tick() -> None:
    """CLAMP/REJECT never arise from telemetry rules today; if a drifted
    policy emits one, _act_on_safety treats it as non-blocking."""
    harness = harness_with_policy(ClampingTelemetryPolicy(SafetyLimits()), steps=4)
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT  # tick continued


@pytest.mark.asyncio
async def test_estop_while_already_faulted_does_not_retransition() -> None:
    harness = harness_in_development(readings=[reading(bean=231.0)])
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.FAULTED
    harness.events.events.clear()
    await harness.controller.tick()  # still hot, already faulted
    assert harness.executor.estop_reasons[-1]  # e-stop fired again
    assert RoastEventKind.PHASE_CHANGED not in harness.events.kinds()  # no re-transition


# --- E9: the four new operator handlers (D19) + snapshot ---


def _to(harness: Harness, steps: int) -> None:
    """Walk the harness controller down the normal path to ``steps`` edges in."""
    for step in NORMAL_PATH[:steps]:
        harness.controller.transition_to(step)


@pytest.mark.asyncio
async def test_operator_mark_beans_added_writes_without_transition() -> None:
    """Manual beans-added (manual-T0 fallback) is matrix-valid only in
    preheating, writes MCP, and never transitions — the T0 move stays on the
    debounced detection path."""
    harness = make_harness()
    _to(harness, 2)  # → PREHEATING
    await harness.controller.operator_mark_beans_added()
    assert "mark_beans_added" in harness.executor.commands
    assert harness.controller.phase is RoastPhase.PREHEATING
    assert RoastEventKind.COMMAND_EXECUTED in harness.events.kinds()


@pytest.mark.asyncio
async def test_operator_mark_beans_added_rejected_outside_preheating() -> None:
    harness = make_harness()
    _to(harness, 4)  # → DEVELOPMENT
    await harness.controller.operator_mark_beans_added()
    assert "mark_beans_added" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert any(
        e.rule == "command_phase_validity" and e.verdict is SafetyVerdict.REJECT
        for e in harness.sink.evaluations
    )


@pytest.mark.asyncio
async def test_operator_start_cooling_from_recovery_transitions_to_cooling() -> None:
    """From operator_recovery_required, start_cooling is a recovery resume into
    cooling (matrix-valid, transitions)."""
    harness = make_harness()
    harness.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    await harness.controller.operator_start_cooling()
    assert "start_cooling" in harness.executor.commands
    assert harness.controller.phase is RoastPhase.COOLING


@pytest.mark.asyncio
async def test_operator_start_cooling_in_cooling_does_not_transition() -> None:
    """In cooling it is the post-drop fallback — writes MCP, stays in cooling."""
    harness = make_harness()
    _to(harness, 5)  # → COOLING
    await harness.controller.operator_start_cooling()
    assert "start_cooling" in harness.executor.commands
    assert harness.controller.phase is RoastPhase.COOLING


@pytest.mark.asyncio
async def test_operator_start_cooling_rejected_in_development() -> None:
    harness = make_harness()
    _to(harness, 4)  # → DEVELOPMENT
    await harness.controller.operator_start_cooling()
    assert "start_cooling" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert any(
        e.rule == "command_phase_validity" and e.verdict is SafetyVerdict.REJECT
        for e in harness.sink.evaluations
    )


@pytest.mark.asyncio
async def test_pause_advisory_suppresses_consult_until_resume() -> None:
    """pause_advisory stops the advisor being consulted; the controller keeps
    ticking; resume_advisory restores consults. No MCP write, no safety eval."""
    advisor = FakeAdvisor([], default_decision=decision())
    harness = make_harness(
        readings=[reading(bean=200.0, env=205.0, t0_detected=True, first_crack_detected=True)],
        advisor=advisor,
    )
    harness.controller.load_profile(PROFILE)
    _to(harness, 4)  # → DEVELOPMENT (an advice phase)
    await harness.controller.tick()  # phase-change consult establishes the baseline
    harness.clock.advance(20.0)
    baseline = len(advisor.contexts)
    assert baseline >= 1

    harness.controller.operator_pause_advisory()
    assert harness.controller.snapshot().advisory_paused is True
    writes_before_pause = len(harness.executor.targets)
    await harness.controller.tick()
    harness.clock.advance(20.0)
    assert len(advisor.contexts) == baseline  # advisor not consulted
    # And the suppressed tick issues no heat/fan write of its own.
    assert len(harness.executor.targets) == writes_before_pause

    harness.controller.operator_resume_advisory()
    assert harness.controller.snapshot().advisory_paused is False
    await harness.controller.tick()
    assert len(advisor.contexts) > baseline  # consults resume


def test_snapshot_is_an_atomic_idle_read() -> None:
    harness = make_harness()
    snapshot = harness.controller.snapshot()
    assert isinstance(snapshot, ControllerSnapshot)
    assert snapshot.phase is RoastPhase.IDLE
    assert (snapshot.current_heat, snapshot.current_fan) == (0, 0)
    assert snapshot.telemetry is None
    assert snapshot.advisory_paused is False


@pytest.mark.asyncio
async def test_snapshot_carries_last_tick_telemetry() -> None:
    harness = make_harness(readings=[reading(bean=190.0, env=200.0)])
    harness.controller.load_profile(PROFILE)
    _to(harness, 2)  # → PREHEATING
    await harness.controller.tick()
    snapshot = harness.controller.snapshot()
    assert snapshot.telemetry is not None
    assert snapshot.telemetry.bean_temp_c == 190.0


@pytest.mark.asyncio
async def test_mark_beans_added_write_failure_surfaces_command_failed() -> None:
    executor = FailingCommandExecutor({"mark_beans_added"})
    harness = make_harness(executor=executor)
    _to(harness, 2)  # → PREHEATING
    harness.events.events.clear()
    await harness.controller.operator_mark_beans_added()
    assert RoastEventKind.COMMAND_FAILED in harness.events.kinds()
    assert harness.controller.phase is RoastPhase.PREHEATING


@pytest.mark.asyncio
async def test_start_cooling_write_failure_surfaces_command_failed_no_transition() -> None:
    executor = FailingCommandExecutor({"start_cooling"})
    harness = make_harness(executor=executor)
    harness.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    harness.events.events.clear()
    await harness.controller.operator_start_cooling()
    assert RoastEventKind.COMMAND_FAILED in harness.events.kinds()
    # A failed write must not transition into cooling.
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED


# --- E10 option (a) / D25: enabled_actions is a faithful permission mirror ---


def _acknowledge_resumes_phase(phase: RoastPhase) -> bool:
    """Whether the controller's recovery resume (the op the acknowledge drain
    calls) actually acts from ``phase`` — driven against the REAL controller.

    ``RoastRunner._dispatch_acknowledge`` resumes via
    ``controller.operator_resume(target)``; that method raises
    ``InvalidTransitionError`` unless the controller is in
    ``operator_recovery_required`` (its own guard). We try every recovery-row
    target and report whether the controller transitioned — so a widening of the
    controller's resume guard (or the recovery row) is caught, not assumed."""
    recovery_targets = TRANSITION_TABLE[RoastPhase.OPERATOR_RECOVERY_REQUIRED]
    for target in recovery_targets:
        controller = controller_in(phase)
        try:
            controller.operator_resume(target)
        except InvalidTransitionError:
            continue
        if controller.phase is target:
            return True
    return False


def _controller_accepts(action: OperatorAction, phase: RoastPhase) -> bool:
    """Whether the REAL controller would ACT on ``action`` in ``phase`` — the
    independent oracle the ``enabled_operator_actions`` projection is pinned
    against (no restatement of the projection itself).

    Computed from the controller's own behavior:

    * MCP-write actions: the controller's operator handlers gate on
      ``SafetyPolicy.evaluate_command_phase`` (returning early on non-ALLOW before
      any write), so acceptance is that verdict — the same matrix the drain
      enforces.
    * ``pause_advisory`` / ``resume_advisory``: unconditional toggles (no phase
      gate) — accepted in every phase; verified by observing the
      ``_advisory_paused`` flag actually flips.
    * ``acknowledge_recovery``: driven through the REAL drain
      (``RoastRunner._dispatch_acknowledge``) by ``_acknowledge_resumes_phase`` — it
      resumes (phase actually changes to the requested target) only from
      ``operator_recovery_required``; any other phase is a no-op. Executing the real
      drain catches a future *widening* of either the drain's phase guard or the
      recovery transition row — a static literal would silently pass.
    """
    command = OPERATOR_ACTION_COMMAND.get(action)
    if command is not None:
        policy = SafetyPolicy(SafetyLimits())
        return policy.evaluate_command_phase(command=command, phase=phase).verdict is (
            SafetyVerdict.ALLOW
        )
    if action in (OperatorAction.PAUSE_ADVISORY, OperatorAction.RESUME_ADVISORY):
        # Drive the real handler and confirm it took effect via its OBSERVABLE
        # output (the emitted ADVISORY event), proving "no phase gate": the toggle
        # fires from any phase. (No private-attr access — the event is the contract.)
        harness = _harness_in_phase(phase)
        harness.events.events.clear()
        want = action is OperatorAction.PAUSE_ADVISORY
        if want:
            harness.controller.operator_pause_advisory()
        else:
            harness.controller.operator_resume_advisory()
        advisory = [p for k, p in harness.events.events if k is RoastEventKind.ADVISORY]
        return any(
            isinstance(p, dict) and cast("dict[str, object]", p).get("advisory_paused") is want
            for p in advisory
        )
    return _acknowledge_resumes_phase(phase)


@pytest.mark.parametrize("action", list(OperatorAction))
@pytest.mark.parametrize("phase", list(RoastPhase))
def test_enabled_actions_mirror_controller_acceptance(
    action: OperatorAction, phase: RoastPhase
) -> None:
    """The biconditional pin (E10 option (a), D25): for every (action, phase),
    the server's ``enabled_actions`` projection includes the action IFF the real
    controller would accept it. Zero carve-outs — a controller change that
    diverges (phase-gating pause/resume, widening acknowledge, a matrix edit)
    fails here, keeping ``enabled_actions`` an honest permission mirror."""
    in_enabled = action in enabled_operator_actions(phase)
    assert in_enabled is _controller_accepts(action, phase), (
        f"{action.value} in {phase.value}: enabled={in_enabled} but "
        f"controller_accepts={_controller_accepts(action, phase)}"
    )


def test_pause_resume_advisory_enabled_in_every_phase() -> None:
    """The advisory toggles are ungated server-side, so the mirror lists them in
    every phase (the page may still hide a meaningless pause on a terminal roast —
    that is presentation, not the permission contract)."""
    for phase in RoastPhase:
        enabled = enabled_operator_actions(phase)
        assert OperatorAction.PAUSE_ADVISORY in enabled
        assert OperatorAction.RESUME_ADVISORY in enabled


def test_acknowledge_recovery_enabled_only_in_recovery() -> None:
    """acknowledge_recovery is the one controller-gated control action."""
    for phase in RoastPhase:
        expected = phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
        assert (OperatorAction.ACKNOWLEDGE_RECOVERY in enabled_operator_actions(phase)) is expected


def test_emergency_stop_enabled_in_every_phase() -> None:
    """E-stop is always available — its matrix row is the full phase set."""
    for phase in RoastPhase:
        assert OperatorAction.EMERGENCY_STOP in enabled_operator_actions(phase)
