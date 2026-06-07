"""E4-S1/E4-S2: transition table, tick scheduler, tick pipeline
(component plan §3, §8; orchestration plan § State Machine,
§ Controller Loop).

T0 debounce + add-beans guidance (E4-S3) and restart recovery (E4-S4)
extend this suite.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from roastpilot_agent.advisor import AdvisorContext, RoastAdvisor, RoastDecision
from roastpilot_agent.config import ControllerConfig
from roastpilot_agent.controller import (
    TRANSITION_TABLE,
    UNIVERSAL_TARGETS,
    InvalidTransitionError,
    RoastController,
    RoastPhase,
    TickScheduler,
)
from roastpilot_agent.models import RoastEventKind, RoastProfile, RoastTelemetry
from roastpilot_agent.safety import SafetyLimits, SafetyPolicy, SafetyVerdict
from tests.conftest import (
    EventSink,
    FakeClock,
    RecordingExecutor,
    RecordingSnapshotSink,
    ScriptedAdvisor,
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
    advisor = ScriptedAdvisor([decision()])
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
    advisor = ScriptedAdvisor([decision()])
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
    advisor = ScriptedAdvisor([decision(), decision(heat=70, fan=55)])
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
async def test_drop_recommendation_recorded_not_executed() -> None:
    """E4-S2 records the drop-eligibility verdict; drop execution wires in
    E4-S3/S4."""
    advisor = ScriptedAdvisor([decision(drop=True)])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()
    drop_evaluations = [e for e in harness.sink.evaluations if e.rule == "drop_eligibility"]
    assert len(drop_evaluations) == 1
    assert drop_evaluations[0].verdict is SafetyVerdict.ALLOW
    assert harness.controller.phase is RoastPhase.DEVELOPMENT  # unchanged


@pytest.mark.asyncio
async def test_advisory_skipped_without_profile_or_telemetry() -> None:
    harness = make_harness(readings=[None], advisor=ScriptedAdvisor([decision()]))
    harness.controller.request_advisory()
    await harness.controller.tick()  # idle, no profile, no telemetry: no crash
    assert harness.executor.targets == []


# --- E4-S2: claude-review regression fixes ---


@pytest.mark.asyncio
async def test_stale_advisory_request_does_not_survive_a_fault() -> None:
    """Review finding 1: an advisory requested before a fail-closed tick
    must not fire on a later tick (it would run in FAULTED phase)."""
    advisor = ScriptedAdvisor([decision()])
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
    advisor = ScriptedAdvisor([decision()])
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
