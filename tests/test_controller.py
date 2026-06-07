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

from roastpilot_agent.advisor import AdvisorContext, FakeAdvisor, RoastAdvisor, RoastDecision
from roastpilot_agent.config import ControllerConfig
from roastpilot_agent.controller import (
    TRANSITION_TABLE,
    UNIVERSAL_TARGETS,
    AdvisoryCallPolicy,
    AdvisoryTrigger,
    InvalidTransitionError,
    RoastController,
    RoastPhase,
    TickScheduler,
)
from roastpilot_agent.models import RoastEventKind, RoastProfile, RoastTelemetry
from roastpilot_agent.safety import (
    SafetyEvaluation,
    SafetyLimits,
    SafetyPolicy,
    SafetyVerdict,
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


def controller_in(phase: RoastPhase) -> RoastController:
    """A controller manoeuvred into ``phase`` through legal edges only."""
    controller = make_harness().controller
    if phase is RoastPhase.IDLE:
        return controller
    for step in NORMAL_PATH:
        controller.transition_to(step)
        if step is phase:
            return controller
    controller.transition_to(phase)  # FAULTED / RECOVERY via universal edge
    return controller


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
        async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    config = ControllerConfig(advisory_timeout_seconds=0.05)
    harness = harness_in_development(readings=[reading()], advisor=NeverAdvisor(), config=config)
    harness.controller.request_advisory()
    await asyncio.wait_for(harness.controller.tick(), timeout=1.0)
    assert harness.executor.targets == []
    assert [e.rule for e in harness.sink.evaluations] == ["all_clear", "advisor_timeout"]
    assert harness.sink.evaluations[-1].verdict is SafetyVerdict.REJECT


@pytest.mark.asyncio
async def test_crashing_advisor_is_a_provider_error() -> None:
    class CrashingAdvisor(RoastAdvisor):
        async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
            raise RuntimeError("boom")

    harness = harness_in_development(readings=[reading()], advisor=CrashingAdvisor())
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.sink.evaluations[-1].rule == "advisor_provider_error"
    assert harness.executor.targets == []


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
    RoR 2.0 °C/min, interval 15 s)."""
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
    policy = _policy()
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=reading(bean=200.0), now=0.0)
    # +0.5 °C, well inside the interval: no trigger.
    assert (
        policy.evaluate(
            phase=RoastPhase.DEVELOPMENT,
            telemetry=reading(bean=200.5),
            now=1.0,
            manual_request=False,
        )
        is None
    )
    # +1.0 °C reaches the threshold.
    assert (
        policy.evaluate(
            phase=RoastPhase.DEVELOPMENT,
            telemetry=reading(bean=201.0),
            now=1.0,
            manual_request=False,
        )
        is AdvisoryTrigger.BEAN_TEMP_DELTA
    )


def test_policy_ror_delta_triggers_at_threshold() -> None:
    policy = _policy()
    policy.note_call(
        phase=RoastPhase.DEVELOPMENT,
        telemetry=reading(bean=200.0, bean_ror_c_per_min=5.0),
        now=0.0,
    )
    # Same bean temp, RoR jumps +2.0 °C/min: RoR is the live trigger.
    trigger = policy.evaluate(
        phase=RoastPhase.DEVELOPMENT,
        telemetry=reading(bean=200.0, bean_ror_c_per_min=7.0),
        now=1.0,
        manual_request=False,
    )
    assert trigger is AdvisoryTrigger.ROR_DELTA


def test_policy_min_interval_heartbeat() -> None:
    policy = _policy()
    flat = reading(bean=200.0, bean_ror_c_per_min=5.0)
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=0.0)
    # Flat telemetry just shy of the interval: silent (no per-tick spam).
    assert (
        policy.evaluate(
            phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=14.9, manual_request=False
        )
        is None
    )
    # At the interval the heartbeat fires.
    assert (
        policy.evaluate(
            phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=15.0, manual_request=False
        )
        is AdvisoryTrigger.MIN_INTERVAL
    )


def test_policy_does_not_fire_every_tick_on_flat_telemetry() -> None:
    """The whole point: a stable roast between heartbeats is silent."""
    policy = _policy()
    flat = reading(bean=200.0, bean_ror_c_per_min=5.0)
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=0.0)
    fired = [
        policy.evaluate(phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=t, manual_request=False)
        for t in (1.0, 2.0, 3.0, 10.0, 14.0)
    ]
    assert fired == [None, None, None, None, None]


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
    201 °C, a further +0.5 °C is below threshold again."""
    policy = _policy()
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=reading(bean=200.0), now=0.0)
    assert (
        policy.evaluate(
            phase=RoastPhase.DEVELOPMENT,
            telemetry=reading(bean=201.0),
            now=1.0,
            manual_request=False,
        )
        is AdvisoryTrigger.BEAN_TEMP_DELTA
    )
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=reading(bean=201.0), now=1.0)
    assert (
        policy.evaluate(
            phase=RoastPhase.DEVELOPMENT,
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

    async def mark_first_crack(self) -> None:
        if "mark_first_crack" in self._failing:
            raise RuntimeError("write failed")
        await super().mark_first_crack()

    async def drop_beans(self) -> None:
        if "drop_beans" in self._failing:
            raise RuntimeError("write failed")
        await super().drop_beans()

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
