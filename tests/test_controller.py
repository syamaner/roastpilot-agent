"""E4-S1/E4-S2: transition table, tick scheduler, tick pipeline
(component plan §3, §8; orchestration plan § State Machine,
§ Controller Loop).

T0 debounce + add-beans guidance (E4-S3) and restart recovery (E4-S4)
extend this suite.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from roastpilot_agent.coherence import LeverDirection
from roastpilot_agent.config import (
    ControllerConfig,
    LateMaillardTrim,
    PreFirstCrackLevers,
    SafetyLimits,
)
from roastpilot_agent.control_policy import PhaseControlLimits
from roastpilot_agent.controller import (
    TERMINAL_LATCH_PHASES,
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
from roastpilot_agent.roast_history import DecisionTraceEntry, RoastMilestoneKind
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


def _advisory_harness_in_phase(phase: RoastPhase, *, advisor: RoastAdvisor) -> Harness:
    """A profile-loaded harness manoeuvred into an advisory ``phase``.

    Walks the normal path to ``phase`` (one of the advisory phases) with
    ``PROFILE`` loaded, clearing the log/event buffers so a single consult is
    isolated — the generalisation of ``harness_in_development`` over the other
    advisory phases (#294).
    """
    harness = make_harness(readings=[reading()], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH:
        harness.controller.transition_to(step)
        if step is phase:
            break
    harness.log.clear()
    harness.events.events.clear()
    return harness


@pytest.mark.asyncio
async def test_advisor_context_box_equals_gate_box_told_equals_enforced() -> None:
    """#273/#294: the box the controller TELLS the model is the box it ENFORCES.

    Drives a real consult in DEVELOPMENT (the only advisory phase under D35/#222 —
    pre-FC is deterministic, the advisor gated out) and asserts the control box on
    the ``AdvisorContext`` the controller actually built (the told side) is the
    same box ``_control_limits`` resolves for the gate (the enforced side) — after
    #294 these share a single ``PhaseControlLimits`` instance per tick. The pre-FC
    told==enforced proof is the deterministic-path test below
    (``test_pre_fc_deterministic_box_told_equals_enforced``).
    """
    advisor = FakeAdvisor([decision()])
    harness = _advisory_harness_in_phase(RoastPhase.DEVELOPMENT, advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()

    assert advisor.contexts, "the advisor should have been consulted"
    context = advisor.contexts[-1]
    enforced = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    assert context.phase is RoastPhase.DEVELOPMENT
    assert context.heat_floor_percent == enforced.heat_floor_percent
    assert context.heat_ceiling_percent == enforced.heat_ceiling_percent
    assert context.fan_floor_percent == enforced.fan_floor_percent
    assert context.fan_ceiling_percent == enforced.fan_ceiling_percent
    assert context.bitter_ceiling_temp_c == enforced.bitter_ceiling_temp_c
    assert context.emergency_drop_temp_c == enforced.emergency_drop_temp_c
    # DEVELOPMENT keeps the full 0–100 box (the post-FC LLM's box, #223); the
    # profile-aware bitter ceiling: PROFILE drops at 205 °C, above 196, so the
    # told ceiling stays the hard 196.
    assert (context.heat_floor_percent, context.heat_ceiling_percent) == (0, 100)
    assert (context.fan_floor_percent, context.fan_ceiling_percent) == (0, 100)
    assert context.bitter_ceiling_temp_c == 196.0
    assert context.emergency_drop_temp_c == 198.0


# --- #312: trustworthy drop — dev% computation + drop coherence guard ---


def _development_harness_with_dev_percent(
    *,
    system_dev_percent: float,
    advisor: RoastAdvisor | None = None,
    config: ControllerConfig | None = None,
) -> Harness:
    """A DEVELOPMENT harness whose SYSTEM development percent is a known value.

    Stamps the controller's charge (T0) clock and walks the first-crack edge with
    the :class:`FakeClock` advanced so that ``_development_percent`` returns
    ``system_dev_percent`` exactly: development time / charge-referenced roast
    time = the requested fraction. Used to drive the deterministic drop coherence
    guard (#312) from a precisely-known ground-truth development figure.
    """
    harness = make_harness(readings=[reading()], advisor=advisor, config=config)
    controller = harness.controller
    controller.load_profile(PROFILE)
    controller.transition_to(RoastPhase.STARTING)
    controller.transition_to(RoastPhase.PREHEATING)
    controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    # Stamp the charge/T0 clock at t=0 (the DTR denominator origin), then advance
    # to first crack so a known development window can follow.
    controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    fc_offset_seconds = 300.0
    harness.clock.advance(fc_offset_seconds)
    controller.transition_to(RoastPhase.DEVELOPMENT)  # stamps the FC clock at now
    # Choose a development window so dev% == system_dev_percent exactly:
    #   dev% = dev_time / (fc_offset + dev_time) * 100
    #   => dev_time = fc_offset * f / (1 - f),  f = system_dev_percent / 100
    fraction = system_dev_percent / 100.0
    dev_time_seconds = fc_offset_seconds * fraction / (1.0 - fraction)
    harness.clock.advance(dev_time_seconds)
    snapshot_percent = controller.snapshot().development_percent
    assert snapshot_percent is not None
    assert snapshot_percent == pytest.approx(system_dev_percent)
    harness.log.clear()
    harness.events.events.clear()
    return harness


def _advisory_payloads(harness: Harness) -> list[dict[str, object]]:
    """All ADVISORY event payloads emitted on ``harness``, in order."""
    return [
        cast(dict[str, object], p) for k, p in harness.events.events if k is RoastEventKind.ADVISORY
    ]


@pytest.mark.asyncio
async def test_advisor_drop_blocked_when_system_development_below_target() -> None:
    """#312 (the first-roast failure): an advisor ``should_drop=true`` is BLOCKED
    when the SYSTEM's real development percent is materially below the target
    window — the drop is irreversible and the model's claimed number is not
    trusted. The drop is not executed, no COOLING transition happens, a rejection
    note is surfaced, and the same consult's heat/fan advice still applies.

    PROFILE targets 20 % development; the system is at 5 % (well below the 3 pp
    margin), reproducing the fabricated-"we're done" early drop.
    """
    advisor = FakeAdvisor([decision(heat=60, fan=55, drop=True)])
    harness = _development_harness_with_dev_percent(system_dev_percent=5.0, advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()

    # The drop was NOT executed and the phase stayed in DEVELOPMENT.
    assert harness.executor.commands.count("drop_beans") == 0
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    # The heat/fan advice from the SAME consult still applied.
    assert harness.executor.targets == [(60, 55)]
    # A rejection note was surfaced with the system (not claimed) development.
    rejections = [p for p in _advisory_payloads(harness) if "drop_rejected" in p]
    assert rejections, "expected a drop_rejected advisory note"
    note = rejections[-1]
    assert note["drop_rejected"] == "development_incoherent"
    assert note["source"] == "advisor"
    assert note["target_development_percent"] == PROFILE.target_development_percent
    assert cast(float, note["system_development_percent"]) == pytest.approx(5.0)
    assert note["drop_dev_margin_percent"] == ControllerConfig().drop_dev_margin_percent
    # Trace parity (#312 review): the blocked drop persists a REJECT
    # SafetyEvaluation, not just an event note, so it shows in the
    # safety_evaluations trace like the low-confidence reject does.
    drop_blocks = [e for e in harness.sink.evaluations if e.rule == "advisor_drop_coherence"]
    assert len(drop_blocks) == 1
    assert drop_blocks[0].verdict is SafetyVerdict.REJECT
    # Held the current targets (no lever write from the reject itself).
    assert drop_blocks[0].adjusted_heat is not None
    assert drop_blocks[0].adjusted_fan is not None
    assert "below the drop window floor" in drop_blocks[0].reason


@pytest.mark.asyncio
async def test_advisor_drop_executes_when_system_development_within_margin() -> None:
    """#312: a legitimate advisor drop — ``should_drop=true`` AND the system's real
    development is within the margin of the target — is HONOURED: the drop executes
    and the controller transitions to COOLING. PROFILE targets 20 %; the system is
    at 18 % (within the default 3 pp margin)."""
    advisor = FakeAdvisor([decision(heat=50, fan=60, drop=True)])
    harness = _development_harness_with_dev_percent(system_dev_percent=18.0, advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()

    assert harness.executor.commands.count("drop_beans") == 1
    assert harness.controller.phase is RoastPhase.COOLING
    # No incoherence rejection was emitted, and no drop-coherence REJECT persisted.
    assert not [p for p in _advisory_payloads(harness) if "drop_rejected" in p]
    assert not [e for e in harness.sink.evaluations if e.rule == "advisor_drop_coherence"]


@pytest.mark.asyncio
async def test_operator_manual_drop_overrides_low_development() -> None:
    """#312: the operator's MANUAL drop is a separate, un-gated operator path — it
    drops regardless of the system development percent. The coherence guard gates
    the ADVISOR drop only; an operator who decides to drop at 5 % development is
    obeyed."""
    harness = _development_harness_with_dev_percent(system_dev_percent=5.0)
    await harness.controller.operator_drop_beans()

    assert harness.executor.commands.count("drop_beans") == 1
    assert harness.controller.phase is RoastPhase.COOLING


@pytest.mark.asyncio
async def test_advisor_drop_executes_exactly_at_the_window_floor() -> None:
    """#312 boundary: the guard is ``system_percent >= floor`` (floor = target -
    margin = 20 - 3 = 17). A drop with the system EXACTLY at the floor (17 %) is
    HONOURED — pins ``>=`` (not ``>``), mirroring
    ``test_confidence_exactly_at_floor_proceeds``."""
    advisor = FakeAdvisor([decision(heat=50, fan=60, drop=True)])
    harness = _development_harness_with_dev_percent(system_dev_percent=17.0, advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()

    assert harness.executor.commands.count("drop_beans") == 1
    assert harness.controller.phase is RoastPhase.COOLING
    assert not [e for e in harness.sink.evaluations if e.rule == "advisor_drop_coherence"]


@pytest.mark.asyncio
async def test_advisor_drop_blocked_just_below_the_window_floor() -> None:
    """#312 boundary: a drop with the system just BELOW the floor (16.99 %, floor
    17 %) is BLOCKED — the strict ``>=`` test fails by a hundredth of a point, so
    the guard withholds the drop and persists the REJECT."""
    advisor = FakeAdvisor([decision(heat=50, fan=60, drop=True)])
    harness = _development_harness_with_dev_percent(system_dev_percent=16.99, advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()

    assert harness.executor.commands.count("drop_beans") == 0
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    drop_blocks = [e for e in harness.sink.evaluations if e.rule == "advisor_drop_coherence"]
    assert len(drop_blocks) == 1
    assert drop_blocks[0].verdict is SafetyVerdict.REJECT


def test_drop_coherence_guard_fails_open_without_a_profile() -> None:
    """#312: the drop coherence guard fails OPEN when it cannot be evaluated — no
    loaded profile means no target to check against, so it does not block (the
    safety drop evaluation still owns the phase boundary). The live advisory path
    always carries a profile; this pins the defensive branch directly."""
    controller = make_harness().controller
    # No profile loaded: fails open even for a real (would-block) development value
    # — the guard takes the percent the caller computed once (#294 compute-once).
    assert controller._drop_development_is_coherent(5.0) is True  # pyright: ignore[reportPrivateUsage]


def test_drop_coherence_guard_fails_open_before_first_crack() -> None:
    """#312: the guard also fails OPEN with a profile loaded but development not yet
    started (``_development_percent()`` is ``None`` pre-FC) — the second defensive
    branch. The guard is only invoked from the DEVELOPMENT-gated advisor drop path,
    so this branch never blocks a real drop; pinned directly for coverage."""
    controller = make_harness().controller
    controller.load_profile(PROFILE)
    controller.transition_to(RoastPhase.STARTING)
    controller.transition_to(RoastPhase.PREHEATING)
    controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    # Pre-FC: development percent is None, so the guard fails open. The caller
    # computes it once and passes it in (#294 compute-once).
    system_percent = controller._development_percent()  # pyright: ignore[reportPrivateUsage]
    assert system_percent is None
    assert controller._drop_development_is_coherent(system_percent) is True  # pyright: ignore[reportPrivateUsage]


def test_development_percent_is_zero_at_first_crack_and_charge_referenced() -> None:
    """#308: the development percent is FC-referenced (0 % at first crack) and the
    DTR denominator is the CHARGE/T0 clock, not serve start.

    Charge at t=0, first crack 300 s later, then 60 s of development: development
    time = 60 s, roast time (since charge) = 360 s, so dev% = 60/360 = 16.67 %.
    At the first-crack instant itself dev% is exactly 0 %.
    """
    harness = make_harness(readings=[reading()])
    controller = harness.controller
    controller.load_profile(PROFILE)
    controller.transition_to(RoastPhase.STARTING)
    controller.transition_to(RoastPhase.PREHEATING)
    controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    harness.clock.advance(300.0)  # 300 s charge → first crack
    controller.transition_to(RoastPhase.DEVELOPMENT)  # FC edge

    # 0 % at the first-crack instant (development time is zero).
    assert controller.snapshot().development_percent == pytest.approx(0.0)
    assert controller.snapshot().development_elapsed_seconds == pytest.approx(0.0)

    harness.clock.advance(60.0)  # 60 s of development
    snapshot = controller.snapshot()
    # Development time is since-FC; DTR denominator is since-CHARGE (360 s total).
    assert snapshot.development_elapsed_seconds == pytest.approx(60.0)
    assert snapshot.development_percent == pytest.approx(60.0 / 360.0 * 100.0)


# --- #222: deterministic pre-FC control policy ---


@pytest.mark.parametrize(
    "phase",
    [RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK],
)
def test_pre_fc_box_is_narrowed_with_deterministic_target(phase: RoastPhase) -> None:
    """D35 §3 (#222): the two pre-FC phases resolve a NARROWED box with a
    deterministic lever target — heat pinned high (floor == the n8n heat 100
    target, so a momentum-killing cut is impossible) and fan capped low (≤ the
    n8n fan 30). The development phase by contrast keeps the full 0–100 box."""
    harness = make_harness()
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH:
        harness.controller.transition_to(step)
        if step is phase:
            break
    box = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    assert box.has_deterministic_target
    # Defaults: heat 100 / fan 30 (the operator's proven n8n pre-FC values).
    assert box.heat_target_percent == 100
    assert box.fan_target_percent == 30
    # Heat floor pinned to the target → no cut below 100 is executable pre-FC.
    assert box.heat_floor_percent == 100
    assert box.heat_ceiling_percent == 100
    # Fan capped low.
    assert box.fan_floor_percent == 0
    assert box.fan_ceiling_percent == 30


@pytest.mark.parametrize(
    "phase",
    [RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK],
)
def test_pre_fc_deterministic_box_told_equals_enforced(phase: RoastPhase) -> None:
    """Carry-forward B (#222 / #273 review): with the pre-FC box genuinely
    narrower than 0–100, the told==enforced proof is load-bearing. The narrowed
    box is the SAME one the gate clamps an out-of-box request into — a request
    below the heat floor (a would-be momentum-killing cut) clamps back up to the
    floor, and a fan request above the low ceiling clamps down to it."""
    harness = make_harness()
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH:
        harness.controller.transition_to(step)
        if step is phase:
            break
    box = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    gate = harness.controller._safety  # pyright: ignore[reportPrivateUsage]
    # A would-be heat crash (20 %) clamps UP to the pinned floor (100), and a
    # high fan (80 %) clamps DOWN to the low ceiling (30) — told == enforced.
    evaluation = gate.evaluate_command(
        requested_heat=20,
        requested_fan=80,
        seconds_since_last_command=None,
        bounds=box,
    )
    assert evaluation.verdict is SafetyVerdict.CLAMP
    assert evaluation.adjusted_heat == box.heat_floor_percent == 100
    assert evaluation.adjusted_fan == box.fan_ceiling_percent == 30


def _pre_fc_harness(phase: RoastPhase, *, advisor: RoastAdvisor | None = None) -> Harness:
    """A profile-loaded harness in a deterministic pre-FC phase, ready to tick."""
    harness = make_harness(readings=[reading(bean=150.0)], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH:
        harness.controller.transition_to(step)
        if step is phase:
            break
    harness.log.clear()
    harness.events.events.clear()
    return harness


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK],
)
async def test_deterministic_pre_fc_levers_actuate_through_safety_path(
    phase: RoastPhase,
) -> None:
    """D35 §3 (#222): pre-FC the controller deterministically sets heat 100 / fan
    30 each tick, and the write passes the existing safety path (a command_bounds
    or all_clear evaluation is persisted before the set_targets). The target is
    the operator's proven n8n value; the heat floor is pinned to it."""
    harness = _pre_fc_harness(phase)
    await harness.controller.tick()
    # The deterministic lever was actuated: heat 100 / fan 30.
    assert harness.executor.targets == [(100, 30)]
    assert harness.controller.snapshot().current_heat == 100
    assert harness.controller.snapshot().current_fan == 30
    # It went through the safety gate (an evaluation persisted for the write).
    rules = [e.rule for e in harness.sink.evaluations]
    assert "all_clear" in rules  # the command evaluation (target sits inside its box)


@pytest.mark.asyncio
async def test_deterministic_pre_fc_lever_is_idempotent_after_first_write() -> None:
    """Once at the deterministic target the controller does not re-write each tick
    (#222): no rate-limit churn, no redundant serial writes — the target is
    constant pre-FC, so only the first tick (or a divergence) writes."""
    harness = _pre_fc_harness(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    await harness.controller.tick()
    assert harness.executor.targets == [(100, 30)]
    harness.clock.advance(5.0)  # well past the rate-limit window
    await harness.controller.tick()
    # No second write — already at the target.
    assert harness.executor.targets == [(100, 30)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK],
)
async def test_advisor_not_consulted_pre_fc_even_on_manual_request(
    phase: RoastPhase,
) -> None:
    """D35 §3 (#222): the free-form advisor is gated out of pre-FC entirely — not
    even a manual operator request reaches it before first crack. The
    deterministic lever still actuates; the advisor sees nothing."""
    advisor = FakeAdvisor([decision(heat=40, fan=70)])
    harness = _pre_fc_harness(phase, advisor=advisor)
    harness.controller.request_advisory()  # manual request pre-FC
    await harness.controller.tick()
    assert advisor.contexts == []  # advisor NOT consulted pre-FC
    # The deterministic lever drove the roast, not the advisor's 40/70.
    assert harness.executor.targets == [(100, 30)]


@pytest.mark.asyncio
async def test_pre_fc_heat_crash_is_structurally_impossible() -> None:
    """The #218 failure mode (heat 70→40→20→0 pre-FC) cannot recur (#222): even if
    something requested a heat cut, the pinned heat floor (100) clamps it back up.
    Here the advisor — which would have advised a cut — is never consulted pre-FC,
    AND the box floor would clamp any cut, so heat holds at 100 across the roast."""
    advisor = FakeAdvisor([decision(heat=20, fan=40)])  # a would-be crash
    harness = _pre_fc_harness(RoastPhase.ROASTING_PRE_FIRST_CRACK, advisor=advisor)
    heats: list[int] = []
    for bean in (150.0, 160.0, 170.0):
        harness.reader.readings = [reading(bean=bean)]
        harness.clock.advance(3.0)
        await harness.controller.tick()
        heats.append(harness.controller.snapshot().current_heat)
    # Heat held high the whole pre-FC window — never crashed below the floor.
    assert all(h == 100 for h in heats), heats
    assert advisor.contexts == []  # advisor never ran pre-FC


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK],
)
async def test_restart_into_pre_fc_never_auto_resumes_heat(phase: RoastPhase) -> None:
    """Restart safety (#222 / the architecture invariant): a restart with a
    possibly-active pre-FC run — in EITHER deterministic pre-FC phase (mid-preheat
    or mid-pre-first-crack) — enters operator_recovery_required and the
    deterministic lever policy does NOT auto-resume heat/fan — no MCP write is
    issued. The policy only actuates during a normally-progressing run, never as a
    side effect of restart/recovery."""
    harness = make_harness(readings=[reading(bean=150.0)])
    harness.controller.load_profile(PROFILE)
    await harness.controller.recover_from_restart(phase)
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert harness.executor.targets == []  # no auto-resume
    # A tick in recovery still issues NO deterministic lever write (the recovery
    # phase carries no deterministic target and the tick fails closed before it).
    await harness.controller.tick()
    assert harness.executor.targets == []
    assert harness.controller.snapshot().current_heat == 0  # heat stays off


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK],
)
async def test_operator_resume_into_pre_fc_re_engages_deterministic_levers(
    phase: RoastPhase,
) -> None:
    """After a restart→operator_recovery_required, the EXPLICIT operator resume
    into EITHER deterministic pre-FC phase re-engages the deterministic lever
    policy on the next tick (#222). This is the operator's choice (operator_resume
    is the explicit action), not an auto-resume — heat is 0 until the resumed tick
    actuates it."""
    harness = make_harness(readings=[reading(bean=150.0)])
    harness.controller.load_profile(PROFILE)
    await harness.controller.recover_from_restart(phase)
    assert harness.controller.snapshot().current_heat == 0
    harness.controller.operator_resume(phase)
    # Heat still 0 immediately after resume — operator_resume never writes hardware.
    assert harness.executor.targets == []
    assert harness.controller.snapshot().current_heat == 0
    # The next tick re-engages the deterministic lever (the resumed run progresses).
    await harness.controller.tick()
    assert harness.executor.targets == [(100, 30)]


# --- Anticipatory late-Maillard heat trim (#327) -----------------------------


async def _charge_into_pre_fc(harness: Harness) -> None:
    """Drive the harness from PREHEATING into ROASTING_PRE_FIRST_CRACK via a
    debounced T0 so the charge clock is stamped (the curve only accumulates after
    charge, and the FC-ETA the trim keys on needs that curve)."""
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    t0 = reading(bean=150.0, t0_detected=True, bean_ror_c_per_min=20.0)
    harness.reader.readings = [t0]
    for _ in range(3):  # three consecutive T0 ticks debounce → pre-FC
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    harness.log.clear()
    harness.events.events.clear()


@pytest.mark.asyncio
async def test_trim_engages_in_late_maillard_window_through_safety_path() -> None:
    """#327: in the late-Maillard window (bean above the floor, FC-ETA inside the
    window) the controller deterministically trims heat to 65 % — NOT the flat 100
    floor — and the trimmed write passes the normal lever→safety path (an
    evaluation is persisted before the set_targets). The advisor is never consulted
    (pre-FC stays deterministic)."""
    advisor = FakeAdvisor([decision(heat=40, fan=70)])
    harness = make_harness(advisor=advisor)
    await _charge_into_pre_fc(harness)
    # A warming ramp: bean +0.5 °C/s, so from 165 °C the FC-ETA to the 176 °C FC
    # target is ~22 s (inside the 60 s window) and the bean is above the 155 °C
    # late-Maillard floor. Five samples build the slope the estimator needs.
    bean = 162.0
    for _ in range(6):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=30.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.5
    # The last tick trimmed heat to 65 (not the flat 100 floor); fan held at 30.
    assert harness.controller.snapshot().current_heat == 65
    assert harness.controller.snapshot().current_fan == 30
    assert harness.executor.targets[-1] == (65, 30)
    # Through the safety gate (an evaluation persisted for the trimmed write).
    assert "all_clear" in [e.rule for e in harness.sink.evaluations]
    # Advisor never ran pre-FC — the trim is controller-owned, deterministic.
    assert advisor.contexts == []


@pytest.mark.asyncio
async def test_early_maillard_holds_the_flat_floor_before_the_window() -> None:
    """#327: before the window opens (early Maillard — the bean is still well below
    the late-Maillard floor, even with a warming slope), heat holds at the flat 100
    floor. The trim engages only once the bean climbs into the late-Maillard band."""
    harness = make_harness()
    await _charge_into_pre_fc(harness)
    # A warming ramp that stays BELOW the 155 °C floor: FC-ETA may resolve but the
    # bean-temp guard keeps the window shut → the flat floor holds.
    bean = 140.0
    for _ in range(6):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=30.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.5
    assert harness.controller.snapshot().current_heat == 100  # flat floor, not trimmed
    assert harness.controller.snapshot().current_fan == 30


@pytest.mark.asyncio
async def test_unknown_fc_eta_fails_closed_to_flat_floor() -> None:
    """#327 fail-closed: with no warming slope the FC-ETA is unknown (the estimator
    returns None), so even a hot bean (above the late-Maillard floor) holds the flat
    100 floor — the always-on guarantee FC still arrives (§8.4)."""
    harness = make_harness()
    await _charge_into_pre_fc(harness)
    # A FLAT bean above the floor: bean ≥ 155 but a zero slope ⇒ no FC-ETA. Enough
    # ticks that the estimator's recent-sample window is entirely flat (the earlier
    # charge ramp has aged out), so the slope is non-positive and the ETA is None.
    for _ in range(10):
        harness.reader.readings = [reading(bean=165.0, bean_ror_c_per_min=0.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.snapshot().current_heat == 100  # fail closed to the floor
    assert harness.controller.snapshot().current_fan == 30


@pytest.mark.asyncio
async def test_trim_signal_keys_on_last_curve_sample_when_read_fails() -> None:
    """#327: on a tolerated FAILED read (telemetry None) the trim signal keys the
    bean-temp guard on the last accumulated curve sample (the FC-ETA is always
    curve-derived), so a transient read miss does not collapse the signal — it is
    ``None`` only when no curve sample exists at all."""
    harness = make_harness()
    await _charge_into_pre_fc(harness)
    # Build a warming curve into the window, then read the signal with a failed read.
    bean = 162.0
    for _ in range(6):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=30.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.5
    signal = harness.controller._trim_signal(None)  # pyright: ignore[reportPrivateUsage]
    assert signal is not None
    # Keyed on the last curve sample's bean temp (the prior tick's reading).
    assert signal.bean_temp_c == pytest.approx(bean - 0.5)


@pytest.mark.asyncio
async def test_trim_disabled_holds_flat_floor_in_window() -> None:
    """#327: with the trim disabled in config the controller holds the flat 100
    floor even inside what would be the late-Maillard window — the pure #222
    behaviour, the explicit off-switch."""
    config = ControllerConfig(
        pre_first_crack_levers=PreFirstCrackLevers(
            late_maillard_trim=LateMaillardTrim(enabled=False)
        )
    )
    harness = make_harness(config=config)
    await _charge_into_pre_fc(harness)
    bean = 162.0
    for _ in range(6):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=30.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.5
    assert harness.controller.snapshot().current_heat == 100  # trim off → flat floor


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


class _PhaseModelAdvisor(FakeAdvisor):
    """A fake advisor whose per-phase descriptor tags the model with the phase
    — to prove the controller records the phase-RESOLVED model (#189)."""

    def descriptor_for(self, phase: RoastPhase) -> AdvisorDescriptor:
        return AdvisorDescriptor(
            provider="fake", model=f"resolved-{phase.value}", prompt_version="t"
        )


@pytest.mark.asyncio
async def test_advisory_records_phase_resolved_model() -> None:
    """#189: the advisor_decisions row records descriptor_for(phase) — the model
    that actually answered this phase's call — not the base descriptor, so the
    trace stays honest once the FC/development slot is flipped to a faster model."""
    advisor = _PhaseModelAdvisor([decision()])
    harness = harness_in_development(readings=[reading()], advisor=advisor)  # phase=DEVELOPMENT
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.sink.advisor_decisions[0].descriptor.model == "resolved-development"


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
async def test_sustained_dead_mcp_emits_exactly_one_fault_and_holds() -> None:
    """The #206 infinite-error-loop fix (roast 2, attempt 2).

    A sustained dead-MCP read (the child segfaulted) must fault closed ONCE and
    then LATCH: subsequent ticks emit no further FAULT events, do not re-read the
    dead child, and do not re-evaluate safety — while the fail-closed posture
    (heat 0 %) stays enforced and operator recovery actions stay available.
    """
    harness = harness_in_development(
        readings=[RuntimeError("segfault")],  # repeats every tick
    )
    # Tolerate-then-fault: default threshold is 3 consecutive read failures.
    await harness.controller.tick()
    await harness.controller.tick()
    await harness.controller.tick()  # third failure → FAULTED
    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.events.kinds().count(RoastEventKind.FAULT) == 1
    # The safe posture applied on entry: heat 0, overrun-safe fan.
    fail_safe_targets = list(harness.executor.targets)
    assert fail_safe_targets, "fail-safe must have written heat 0 / safe fan on entry"
    assert fail_safe_targets[-1][0] == 0  # heat 0 %

    # Snapshot the persisted evaluations + applied targets at the latch boundary
    # so a post-latch tick can be proven to add NEITHER (the anti-spam guarantee).
    evals_at_fault = len(harness.sink.evaluations)
    targets_at_fault = len(harness.executor.targets)

    # Twenty more ticks against the still-dead MCP. The latch now attempts an
    # upward-only escalation re-read each tick, but the dead read raises and is
    # held SILENTLY — so the spam guarantee holds: still exactly one FAULT event,
    # no new persisted evaluation, no re-write of the posture, no re-fire.
    for _ in range(20):
        await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.FAULTED
    # EXACTLY ONE fault event across the whole dead-MCP stretch (the bug emitted N).
    assert harness.events.kinds().count(RoastEventKind.FAULT) == 1
    # A failed re-read produced NO new evaluation and NO re-write of the posture.
    assert len(harness.sink.evaluations) == evals_at_fault
    assert len(harness.executor.targets) == targets_at_fault
    # Operator recovery stays available throughout the latch (#206 operable-faulted):
    # emergency-stop, start/stop cooling, and acknowledge are enabled in faulted.
    enabled = enabled_operator_actions(harness.controller.phase)
    assert OperatorAction.EMERGENCY_STOP in enabled
    assert OperatorAction.START_COOLING in enabled
    assert OperatorAction.STOP_COOLING in enabled
    assert OperatorAction.ACKNOWLEDGE_FAULT in enabled


@pytest.mark.asyncio
async def test_recovery_phase_latches_and_holds() -> None:
    """A controller latched into operator_recovery_required holds: the upward-only
    escalation re-read sees a below-ceiling reading (no escalation), so it never
    re-emits recovery_required, persists no new evaluation, and never escalates."""
    # 205 °C is the pre-T0 charge bound (→ recovery) but well below the 230 °C hard
    # ceiling, so the latched escalation re-read returns ALLOW: nothing to escalate.
    harness = make_harness(readings=[reading(bean=205.0)])
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    harness.events.events.clear()
    harness.log.clear()
    await harness.controller.tick()  # pre-T0 overrun → operator_recovery_required
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert harness.events.kinds().count(RoastEventKind.RECOVERY_REQUIRED) == 1
    evals = len(harness.sink.evaluations)

    for _ in range(10):
        await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert harness.events.kinds().count(RoastEventKind.RECOVERY_REQUIRED) == 1
    # No FAULT escalation (the reading is below the hard ceiling) and no new
    # persisted evaluation while latched on a below-ceiling reading.
    assert RoastEventKind.FAULT not in harness.events.kinds()
    assert len(harness.sink.evaluations) == evals


def test_terminal_latch_phases_is_the_universal_targets_set() -> None:
    """The latch set is bound to UNIVERSAL_TARGETS by identity, not duplicated
    (PR review): the phases the tick latches in ARE the universal terminal HOLD
    phases. Pinning this keeps them from drifting apart if one is ever extended."""
    assert TERMINAL_LATCH_PHASES is UNIVERSAL_TARGETS
    expected = frozenset({RoastPhase.FAULTED, RoastPhase.OPERATOR_RECOVERY_REQUIRED})
    assert expected == TERMINAL_LATCH_PHASES


@pytest.mark.asyncio
async def test_latch_retries_fail_safe_after_a_transient_write_failure() -> None:
    """PR-review blocker: the latch must NOT strand the roaster hot if the
    fail-safe heat-off write fails transiently on the entry tick.

    A flaky executor fails the first N set_targets writes (the entry write + the
    first latched retry), then succeeds. The fail-closed posture must be RETRIED
    on subsequent latched ticks until it confirms (heat 0), without re-emitting
    the fault, re-reading the dead MCP, or re-evaluating safety.
    """

    class FlakyExecutor(RecordingExecutor):
        def __init__(self, log: list[str], fail_first: int) -> None:
            super().__init__(log)
            self._remaining_failures = fail_first

        async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
                raise RuntimeError("serial write dropped")
            await super().set_targets(heat_percent=heat_percent, fan_percent=fan_percent)

    log: list[str] = []
    # The entry write fails AND the first retry fails; the second retry succeeds.
    executor = FlakyExecutor(log, fail_first=2)
    # Missing telemetry during an active roast faults closed via _apply_fail_safe
    # (the FAULT verdict carries adjusted heat 0 / safe fan — the set_targets path
    # the flaky executor disrupts).
    harness = make_harness(readings=[None], executor=executor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT
        harness.controller.transition_to(step)
    harness.events.events.clear()

    await harness.controller.tick()  # entry: FAULT; fail-safe write fails → latched
    assert harness.controller.phase is RoastPhase.FAULTED
    fault_count = harness.events.kinds().count(RoastEventKind.FAULT)
    assert fault_count == 1
    # The entry write failed → posture not yet confirmed (no successful target).
    assert executor.targets == []

    evals_before_retry = len(harness.sink.evaluations)

    # Latched ticks retry the heat-off write: first retry fails, second succeeds.
    await harness.controller.tick()  # retry 1 fails, still latched
    assert executor.targets == []
    await harness.controller.tick()  # retry 2 succeeds → heat 0 applied
    assert executor.targets, "fail-safe heat-off must eventually be applied"
    assert executor.targets[-1][0] == 0  # heat 0 %
    await harness.controller.tick()  # confirmed → no further writes

    # Posture confirmed once; not re-applied every tick thereafter.
    assert len(executor.targets) == 1
    # Still exactly one fault event. The escalation re-read sees None (no session)
    # and holds silently, so it persists no new evaluation and never escalates.
    assert harness.events.kinds().count(RoastEventKind.FAULT) == 1
    assert len(harness.sink.evaluations) == evals_before_retry


@pytest.mark.asyncio
async def test_latch_auto_escalates_fault_to_emergency_stop_once() -> None:
    """Safety-reviewer carry-forward (#206): a controller latched in `faulted`
    with a still-LIVE MCP must still AUTO-escalate to the hardware emergency stop
    if the MCP then reports a hard-ceiling breach — exactly once, with no re-fire
    or spam on subsequent identical ticks.

    Entry: a STALE-telemetry reading (live MCP, bean below the ceiling) faults
    closed → FAULTED latched on FAULT. Then a FRESH reading crosses the 230 °C
    hard bean ceiling → the latched escalation re-read fires `emergency_stop` once
    and emits one escalation FAULT; later identical ticks neither re-fire nor
    re-emit (EMERGENCY_STOP is the max severity).
    """
    stale_low = reading(bean=180.0, env=200.0, age_seconds=10.0)  # stale → FAULT
    over_ceiling = reading(bean=235.0, env=200.0, age_seconds=0.0)  # > 230 → e-stop
    harness = harness_in_development(readings=[stale_low, over_ceiling])

    await harness.controller.tick()  # entry: stale FAULT → FAULTED (latched FAULT)
    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.events.kinds().count(RoastEventKind.FAULT) == 1
    assert harness.executor.estop_reasons == []  # no hardware e-stop yet

    await harness.controller.tick()  # escalation: hard-ceiling breach → e-stop
    assert harness.controller.phase is RoastPhase.FAULTED
    assert len(harness.executor.estop_reasons) == 1  # fired exactly ONCE
    assert harness.events.kinds().count(RoastEventKind.FAULT) == 2  # +1 escalation

    # Subsequent identical over-ceiling ticks: already at EMERGENCY_STOP (max
    # severity) → no re-fire, no further escalation event.
    for _ in range(10):
        await harness.controller.tick()
    assert len(harness.executor.estop_reasons) == 1  # still once
    assert harness.events.kinds().count(RoastEventKind.FAULT) == 2  # still two
    # Operator escalation stays available throughout.
    assert OperatorAction.EMERGENCY_STOP in enabled_operator_actions(harness.controller.phase)


@pytest.mark.asyncio
async def test_latch_does_not_escalate_on_a_same_or_lesser_verdict() -> None:
    """The escalation is upward-ONLY: a FAULT-latched controller whose live MCP
    keeps reporting a below-ceiling (ALLOW) or same-severity reading must NOT
    fire a hardware e-stop or emit any further event."""
    stale_low = reading(bean=180.0, env=200.0, age_seconds=10.0)  # stale → FAULT
    fresh_ok = reading(bean=190.0, env=200.0, age_seconds=0.0)  # below ceiling → ALLOW
    harness = harness_in_development(readings=[stale_low, fresh_ok])

    await harness.controller.tick()  # entry → FAULTED (latched FAULT)
    assert harness.controller.phase is RoastPhase.FAULTED
    faults_after_entry = harness.events.kinds().count(RoastEventKind.FAULT)

    for _ in range(10):
        await harness.controller.tick()  # fresh ALLOW reading: nothing to escalate

    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.executor.estop_reasons == []  # no escalation fired
    assert harness.events.kinds().count(RoastEventKind.FAULT) == faults_after_entry


@pytest.mark.asyncio
async def test_latch_escalates_recovery_to_emergency_stop_and_faults() -> None:
    """A controller latched in operator_recovery_required (the lower-severity
    RECOVERY latch) that then sees a hard-ceiling breach escalates upward to the
    hardware emergency stop AND crosses into FAULTED — the universal `* → faulted`
    edge — firing the e-stop once."""
    pre_t0_overrun = reading(bean=205.0, env=200.0)  # > 200 pre-T0 bound → RECOVERY
    over_ceiling = reading(bean=235.0, env=200.0)  # > 230 hard ceiling → e-stop
    harness = make_harness(readings=[pre_t0_overrun, over_ceiling])
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING (no confirmed T0)
        harness.controller.transition_to(step)
    harness.events.events.clear()

    await harness.controller.tick()  # pre-T0 overrun → operator_recovery_required
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert harness.executor.estop_reasons == []

    await harness.controller.tick()  # hard-ceiling breach → escalate to e-stop
    assert harness.controller.phase is RoastPhase.FAULTED  # crossed into faulted
    assert len(harness.executor.estop_reasons) == 1
    assert RoastEventKind.FAULT in harness.events.kinds()
    # Max severity now: no re-fire on subsequent identical ticks.
    for _ in range(5):
        await harness.controller.tick()
    assert len(harness.executor.estop_reasons) == 1


@pytest.mark.asyncio
async def test_latch_escalation_estop_failure_is_surfaced_and_latches_retry() -> None:
    """A raising e-stop during the latched escalation must not crash the tick: it
    surfaces COMMAND_FAILED, latches a heat-off retry, still emits the escalation
    FAULT, and re-latches at EMERGENCY_STOP so it does not re-fire."""

    class FailingEstopExecutor(RecordingExecutor):
        async def emergency_stop(self, *, reason: str) -> None:
            raise RuntimeError("serial port dead")

    log: list[str] = []
    stale_low = reading(bean=180.0, env=200.0, age_seconds=10.0)  # stale → FAULT
    over_ceiling = reading(bean=235.0, env=200.0, age_seconds=0.0)  # > 230 → e-stop
    harness = make_harness(readings=[stale_low, over_ceiling], executor=FailingEstopExecutor(log))
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT
        harness.controller.transition_to(step)
    harness.events.events.clear()

    await harness.controller.tick()  # entry: stale FAULT → FAULTED
    assert harness.controller.phase is RoastPhase.FAULTED

    await harness.controller.tick()  # escalation: e-stop raises but is handled
    assert harness.controller.phase is RoastPhase.FAULTED
    kinds = harness.events.kinds()
    assert RoastEventKind.COMMAND_FAILED in kinds  # surfaced the failed e-stop
    assert kinds.count(RoastEventKind.FAULT) == 2  # entry + escalation
    # A failed escalation e-stop latched a heat-off retry (fail-closed), and the
    # latch is now at EMERGENCY_STOP so it does not re-fire.
    for _ in range(5):
        await harness.controller.tick()
    assert harness.events.kinds().count(RoastEventKind.FAULT) == 2  # no re-fire


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
    RoR 2.0 °C/min, phase-keyed floors: pre-FC NOT an advice phase (D35/#222) /
    development heartbeat 5 s, post-FC consult dwell 5 s, #276).

    The default post-FC dwell (``post_fc_min_consult_interval_seconds`` 5 s, #276)
    gates EVERY automatic development trigger — including the change-based ones —
    so a test that wants a change trigger to fire on the next tick must advance
    ``now`` past the dwell (use :func:`_no_dwell_development_policy` for the
    isolated delta-logic tests)."""
    return AdvisoryCallPolicy(ControllerConfig())


def _no_dwell_development_policy(*, heartbeat_seconds: float = 1000.0) -> AdvisoryCallPolicy:
    """A development policy with the post-FC dwell effectively off (#276).

    The default post-FC dwell (5 s, #276) gates the change-based triggers too, so
    isolating the delta logic on consecutive 1 s ticks needs the dwell out of the
    way: a near-zero dwell (``gt=0`` forbids exactly 0) lets a change trigger fire
    on the next tick, while a long heartbeat floor suppresses MIN_INTERVAL so the
    test can assert a sub-threshold delta is genuinely silent and an at-threshold
    one fires the change-based trigger."""
    return AdvisoryCallPolicy(
        ControllerConfig(
            advisory_min_interval_seconds={RoastPhase.DEVELOPMENT: heartbeat_seconds},
            post_fc_min_consult_interval_seconds=0.001,
        )
    )


def test_policy_first_consult_in_advice_phase_is_phase_change() -> None:
    policy = _policy()
    # Development is the only auto-advice phase under D35 (#222): pre-FC is
    # deterministic, the advisor is not consulted there.
    trigger = policy.evaluate(
        phase=RoastPhase.DEVELOPMENT,
        telemetry=reading(),
        now=0.0,
        manual_request=False,
    )
    assert trigger is AdvisoryTrigger.PHASE_CHANGE


def test_policy_phase_transition_triggers() -> None:
    policy = _policy()
    # A transition INTO development (the post-FC advice phase) fires PHASE_CHANGE.
    policy.note_call(phase=RoastPhase.PREHEATING, telemetry=reading(), now=0.0)
    trigger = policy.evaluate(
        phase=RoastPhase.DEVELOPMENT,
        telemetry=reading(),
        now=1.0,
        manual_request=False,
    )
    assert trigger is AdvisoryTrigger.PHASE_CHANGE


def test_policy_bean_temp_delta_triggers_at_threshold_not_below() -> None:
    # Development with a long heartbeat floor (#222): isolate the change-based
    # bean-temp trigger from the per-tick (0 floor) heartbeat.
    policy = _no_dwell_development_policy()
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=reading(bean=150.0), now=0.0)
    # +0.5 °C, below the delta threshold, well inside the heartbeat floor: silent.
    assert (
        policy.evaluate(
            phase=RoastPhase.DEVELOPMENT,
            telemetry=reading(bean=150.5),
            now=1.0,
            manual_request=False,
        )
        is None
    )
    # +1.0 °C reaches the threshold (the change-based trigger fires).
    assert (
        policy.evaluate(
            phase=RoastPhase.DEVELOPMENT,
            telemetry=reading(bean=151.0),
            now=1.0,
            manual_request=False,
        )
        is AdvisoryTrigger.BEAN_TEMP_DELTA
    )


def test_policy_ror_delta_triggers_at_threshold() -> None:
    # Development with a long heartbeat floor (#222): the RoR delta is the only
    # reason to fire (the heartbeat is suppressed).
    policy = _no_dwell_development_policy()
    policy.note_call(
        phase=RoastPhase.DEVELOPMENT,
        telemetry=reading(bean=150.0, bean_ror_c_per_min=5.0),
        now=0.0,
    )
    # Same bean temp, RoR jumps +2.0 °C/min: RoR is the live trigger.
    trigger = policy.evaluate(
        phase=RoastPhase.DEVELOPMENT,
        telemetry=reading(bean=150.0, bean_ror_c_per_min=7.0),
        now=1.0,
        manual_request=False,
    )
    assert trigger is AdvisoryTrigger.ROR_DELTA


def test_policy_development_consults_at_the_post_fc_dwell_cadence() -> None:
    """#276 (supersedes the #171 back-to-back behaviour): development consults run
    at the deliberate post-FC dwell (~5 s, D40.5), not every tick.

    A flat roast within the 5 s dwell is silent — the every-tick heartbeat is
    gated by the post-FC dwell — and the heartbeat (MIN_INTERVAL) fires once the
    dwell has elapsed. This deliberate cadence is the floor the deadband judges
    the model's trajectory across (the #218 anti-thrash fix)."""
    policy = _policy()
    flat = reading(bean=200.0, bean_ror_c_per_min=5.0)
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=0.0)
    # 1 s later (one tick), inside the 5 s dwell: silent (no every-tick spam).
    assert (
        policy.evaluate(phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=1.0, manual_request=False)
        is None
    )
    # At the 5 s dwell the heartbeat fires.
    assert (
        policy.evaluate(phase=RoastPhase.DEVELOPMENT, telemetry=flat, now=5.0, manual_request=False)
        is AdvisoryTrigger.MIN_INTERVAL
    )


def test_policy_post_fc_dwell_suppresses_delta_triggers_not_just_heartbeat() -> None:
    """#276 Fix 2: the post-FC dwell suppresses BOTH the change-based delta triggers
    AND the heartbeat — not the heartbeat alone.

    A large +5 °C bean-temp jump INSIDE the 5 s dwell would fire BEAN_TEMP_DELTA if
    only the heartbeat were gated; under the dwell it must stay silent (``None``).
    The companion test (``..._consults_at_the_post_fc_dwell_cadence``) covers a flat
    roast; this one proves the dwell beats a live delta too."""
    policy = _policy()
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=reading(bean=185.0), now=0.0)
    # +5 °C (well past the 1.0 °C delta threshold) but only 1 s in — inside the 5 s
    # dwell: the delta trigger is suppressed, not just the heartbeat.
    assert (
        policy.evaluate(
            phase=RoastPhase.DEVELOPMENT,
            telemetry=reading(bean=190.0),
            now=1.0,
            manual_request=False,
        )
        is None
    )


def test_policy_pre_first_crack_is_never_an_automatic_advice_phase() -> None:
    """D35 (#222): the advisor is NOT consulted pre-FC at all. Even a large
    bean-temp jump and a flat roast both stay silent in ROASTING_PRE_FIRST_CRACK
    — the deterministic controller owns the pre-FC levers, so no automatic
    trigger ever fires there (the former drying/near-FC cadence is retired)."""
    policy = _policy()
    flat = reading(bean=150.0, bean_ror_c_per_min=5.0)
    policy.note_call(phase=RoastPhase.ROASTING_PRE_FIRST_CRACK, telemetry=flat, now=0.0)
    # A flat roast over a long span: silent.
    flat_fired = [
        policy.evaluate(
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            telemetry=flat,
            now=t,
            manual_request=False,
        )
        for t in (1.0, 10.0, 30.0, 120.0, 600.0)
    ]
    assert flat_fired == [None, None, None, None, None]
    # A large bean-temp jump (would have fired BEAN_TEMP_DELTA pre-#222): also
    # silent now — pre-FC is gated out of automatic advice entirely.
    assert (
        policy.evaluate(
            phase=RoastPhase.ROASTING_PRE_FIRST_CRACK,
            telemetry=reading(bean=200.0, bean_ror_c_per_min=5.0),
            now=601.0,
            manual_request=False,
        )
        is None
    )


def test_policy_post_fc_dwell_only_applies_to_development() -> None:
    """#276: the post-FC consult dwell gates DEVELOPMENT only. Every other phase
    returns no dwell (the pre-FC phases are deterministic and never consulted; the
    lifecycle states are not advice phases)."""
    policy = _policy()
    assert (
        policy._phase_min_consult_interval(RoastPhase.DEVELOPMENT)  # pyright: ignore[reportPrivateUsage]
        == ControllerConfig().post_fc_min_consult_interval_seconds
    )
    for phase in RoastPhase:
        if phase is RoastPhase.DEVELOPMENT:
            continue
        assert (
            policy._phase_min_consult_interval(phase) is None  # pyright: ignore[reportPrivateUsage]
        )


def test_policy_change_trigger_fires_early_within_phase_interval() -> None:
    """Once the post-FC dwell has elapsed, the change-based triggers are evaluated
    before the heartbeat floor — a large bean-temp jump reports BEAN_TEMP_DELTA
    (not MIN_INTERVAL), so the trace records the *reason* it fired (#276 keeps the
    delta reason once the dwell clears; the dwell itself is tested separately)."""
    policy = _no_dwell_development_policy()
    policy.note_call(phase=RoastPhase.DEVELOPMENT, telemetry=reading(bean=150.0), now=0.0)
    # Dwell out of the way (near-zero), heartbeat floor long: just past the dwell a
    # +5 °C jump reports the change-based trigger rather than the heartbeat.
    trigger = policy.evaluate(
        phase=RoastPhase.DEVELOPMENT,
        telemetry=reading(bean=155.0),
        now=0.01,
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


def test_policy_manual_in_preheat_returns_manual_in_isolation() -> None:
    """Isolated AdvisoryCallPolicy logic ONLY — NOT the controller's runtime path.

    D32 (#191): in isolation the policy treats a manual operator request in preheat
    as MANUAL (manual bypasses the auto-advice-phase scope). But under D35 (#222)
    the controller short-circuits in `_maybe_run_advisory` and never calls
    `policy.evaluate()` for the pre-FC phases, so this branch is unreachable at
    runtime — the advisor is gated out of pre-FC entirely. The system-level
    invariant is `test_advisor_not_consulted_pre_fc_even_on_manual_request`. The
    policy itself is retained (and exercised) for POST-FC cadence."""
    policy = _policy()
    trigger = policy.evaluate(
        phase=RoastPhase.PREHEATING, telemetry=reading(), now=0.0, manual_request=True
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
        RoastPhase.PREHEATING,  # D35 (#222): pre-FC is deterministic, advisor gated out
        RoastPhase.ROASTING_PRE_FIRST_CRACK,  # D35 (#222): pre-FC, advisor gated out
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
    """Deltas measure from the last call, not the start: after a call at 201 °C,
    a further +0.5 °C is below threshold again. A long development heartbeat floor
    suppresses MIN_INTERVAL so the sub-threshold tick is silent — isolating the
    baseline advance (#222)."""
    policy = _no_dwell_development_policy()
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
    temp baseline — the next real delta still measures from the prior call. The
    post-FC dwell is out of the way (#276) so the delta logic is isolated."""
    policy = _no_dwell_development_policy()
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
async def test_charge_guidance_does_not_trigger_on_environment_alone() -> None:
    """#211: the cue keys on the BEAN probe only. With env inside the band but
    the bean probe still below it (the empty-drum case where env leads bean),
    no guidance fires — the cue must track the reading the operator watches."""
    harness = harness_preheating(readings=[reading(bean=120.0, env=180.0)])
    await harness.controller.tick()
    assert RoastEventKind.CHARGE_GUIDANCE not in harness.events.kinds()


@pytest.mark.asyncio
async def test_charge_guidance_triggers_on_bean_with_env_out_of_band() -> None:
    """Bean inside the band fires the cue even when env is out of band — the
    bean probe is the sole trigger (#211)."""
    harness = harness_preheating(readings=[reading(bean=180.0, env=120.0)])
    await harness.controller.tick()
    assert harness.events.kinds().count(RoastEventKind.CHARGE_GUIDANCE) == 1


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
async def test_debounced_t0_stamps_charge_clock_and_advisor_silent_pre_fc() -> None:
    """Controller wiring (D35 / #222 + #219): the debounced T0 transition stamps
    the charge clock, and the advisor is NOT consulted anywhere pre-FC (the
    deterministic controller owns the levers). (a) Through the whole post-charge,
    pre-FC window the advisor stays silent. (b) Once first crack fires and the
    roast reaches DEVELOPMENT, the advisor IS consulted and the context carries
    seconds_since_charge ≈ elapsed since the T0 stamp (the #219 charge clock,
    stamped pre-FC, survives into the post-FC consult)."""
    crashing = reading(bean=160.0, t0_detected=True, bean_ror_c_per_min=-80.0)
    advisor = FakeAdvisor([decision(heat=40, fan=60)])
    harness = make_harness(readings=[crashing], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    harness.log.clear()
    harness.events.events.clear()
    # Three consecutive T0 ticks debounce → transition into pre-first-crack on
    # the third; that tick stamps the charge clock (at clock=2.0).
    for _ in range(3):
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # (a) The advisor is NOT consulted pre-FC — deterministic control, even on a
    # crashing post-charge bean, and even after several more pre-FC ticks.
    assert advisor.contexts == []
    await harness.controller.tick()
    harness.clock.advance(1.0)
    assert advisor.contexts == []
    # (b) First crack → DEVELOPMENT; the advisor is now consulted, and the
    # charge clock stamped pre-FC populates seconds_since_charge. FC fires here
    # via the MCP first_crack_detected path so the charge clock is untouched.
    harness.reader.readings = [
        reading(bean=185.0, bean_ror_c_per_min=5.0, first_crack_detected=True)
    ]
    await harness.controller.tick()  # transitions to DEVELOPMENT (no consult this tick yet)
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    harness.clock.advance(1.0)
    harness.reader.readings = [reading(bean=186.0, bean_ror_c_per_min=4.0)]
    await harness.controller.tick()  # first development consult
    assert advisor.contexts  # consulted post-FC
    ctx = advisor.contexts[-1]
    assert ctx.seconds_since_charge is not None
    # Charge stamped at clock=2.0; this consult runs after further ticks, so the
    # charge clock has kept counting from the pre-FC stamp (it is > 0).
    assert ctx.seconds_since_charge > 0.0


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
async def test_start_run_clamps_initial_targets_to_pre_fc_box() -> None:
    """Carry-forward A (#222 / #273 review): run-start lands in PREHEATING and
    passes the PREHEATING control box as bounds, so the profile's initial heat/fan
    (70/40) are clamped to the narrowed pre-FC box (heat floor 100, fan ceiling
    30) — told == enforced at the very first roast command, not enforcing 0–100
    while the policy says the narrowed box."""
    harness = make_harness(readings=[reading()])
    await harness.controller.start_run(PROFILE)
    # 70 → 100 (clamped UP to the pinned heat floor), 40 → 30 (clamped DOWN to the
    # low fan ceiling): the deterministic pre-FC levers, applied at run start.
    assert harness.executor.targets == [(100, 30)]
    rules = [e.rule for e in harness.sink.evaluations]
    assert "command_bounds" in rules  # the clamp verdict was recorded


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
async def test_restore_charge_clock_survives_restart_into_resumed_advice() -> None:
    """#235: a restart→operator-resume restores the advisory DTR clock.

    Before this fix the controller never restored ``_charge_monotonic`` on a
    recovery resume, so ``_charge_elapsed_seconds`` read ``None`` → the advisor
    context's ``roast_elapsed_seconds`` (the DTR denominator from #219) collapsed
    to ``0.0`` for the rest of the resumed run. The store persists the *absolute*
    charge instant; ``restore_charge_clock`` reconstructs the monotonic anchor
    from it so the seconds-since-charge denominator survives.

    Under D35 (#222) the advisor is not consulted pre-FC, so the restored clock is
    asserted post-FC: after restore + resume into pre-first-crack, first crack
    advances to DEVELOPMENT and the advisor consult there carries the restored
    elapsed (≈120 s + the pre-FC dwell), NOT 0.0. Advisory/display-only — heat/fan
    are never auto-resumed and the resume gate is unchanged.
    """
    # The persisted absolute charge instant: 120 s before "now".
    charged_at = datetime.now(UTC) - timedelta(seconds=120.0)
    advisor = FakeAdvisor([decision(heat=50, fan=55)])
    harness = make_harness(readings=[reading()], advisor=advisor)
    harness.controller.load_profile(PROFILE)

    # Restart recovery: restore the charge clock from the persisted instant, then
    # classify into operator_recovery_required (no heat/fan write).
    harness.controller.restore_charge_clock(charged_at.isoformat())
    await harness.controller.recover_from_restart(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert harness.executor.targets == []  # restart never auto-resumes heat/fan

    # Operator resumes into pre-first-crack (deterministic control, no consult),
    # then first crack advances to DEVELOPMENT where the advisor IS consulted.
    harness.controller.operator_resume(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=4.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()

    assert advisor.contexts, "advisor should have been consulted after resume + FC"
    ctx = advisor.contexts[-1]
    # The DTR denominator is the restored charge clock — non-zero and ≈120 s
    # (the wall-clock gap between the persisted instant and now), NOT 0.0.
    assert ctx.roast_elapsed_seconds > 0.0
    assert ctx.roast_elapsed_seconds == pytest.approx(120.0, abs=5.0)
    # And the snapshot's charge-referenced reads agree (display-only parity).
    assert harness.controller.snapshot().charge_detected is True


def test_restore_charge_clock_ignores_malformed_timestamp() -> None:
    """#235: a malformed persisted timestamp leaves the charge clock unset
    (the conservative pre-fix behaviour — a missing breadcrumb degrades the DTR,
    it never raises)."""
    harness = make_harness(readings=[reading()])
    harness.controller.restore_charge_clock("not-a-timestamp")
    assert harness.controller.snapshot().charge_detected is False


def test_restore_charge_clock_ignores_naive_timestamp_without_crashing() -> None:
    """#235 (recovery-path crash guard): a timezone-NAIVE ISO string is valid ISO
    and parses without ``ValueError``, but the aware-minus-naive subtraction would
    raise ``TypeError`` — which, uncaught, would propagate through ``recover`` and
    crash the recovery lifespan. The broadened guard catches it: the call does NOT
    raise and the charge clock stays unset (the conservative path). Production only
    ever writes ``+00:00`` (``_utc_now``), but recovery must never crash on a bad
    stored value."""
    harness = make_harness(readings=[reading()])
    # No assertion of "raises" — the whole point is that it must NOT raise.
    harness.controller.restore_charge_clock("2026-06-15T10:00:00")  # naive: no offset
    assert harness.controller.snapshot().charge_detected is False


def test_restore_charge_clock_clamps_future_instant() -> None:
    """#235: clock skew that would place charge in the future clamps to
    "charge now" (elapsed 0) rather than fabricating a future-referenced clock —
    the charge clock is stamped but reads ~0 seconds elapsed."""
    future = (datetime.now(UTC) + timedelta(seconds=60.0)).isoformat()
    harness = make_harness(readings=[reading()])
    harness.controller.restore_charge_clock(future)
    snapshot = harness.controller.snapshot()
    assert snapshot.charge_detected is True
    # roast_elapsed (charge-referenced) is clamped to ~0, never negative.
    assert snapshot.roast_elapsed_seconds >= 0.0
    assert harness.controller._charge_elapsed_seconds() == pytest.approx(  # pyright: ignore[reportPrivateUsage]
        0.0, abs=1.0
    )


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


def test_development_elapsed_none_before_first_crack() -> None:
    """The advisor context carries ``development_elapsed_seconds=None`` until
    first crack arms the development clock.

    The advisor is not consulted pre-FC (#222), so the context *mapping* is
    asserted directly via ``_build_advisor_context`` (the field a pre-FC build
    would carry), rather than via a pre-FC consult that no longer happens."""
    harness = make_harness(readings=[reading()])
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:  # …→ ROASTING_PRE_FIRST_CRACK
        harness.controller.transition_to(step)
    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    assert ctx.development_elapsed_seconds is None


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


def test_development_clock_resets_on_new_run() -> None:
    """A new run/preheat clears the development clock, so a stale FC time from
    a prior run never leaks into the next run's advisor context. The advisor is
    not consulted pre-FC (#222), so the reset is asserted on the context mapping
    (``_build_advisor_context``) once the fresh run is back in pre-first-crack."""
    harness = make_harness(readings=[reading()])
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
    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    assert ctx.development_elapsed_seconds is None


@pytest.mark.asyncio
async def test_development_clock_survives_recovery_resume() -> None:
    """A recovery resume into development is NOT a fresh first crack: it must
    not restamp the development clock, or an already-developed run resumed from
    ``operator_recovery_required`` would read elapsed≈0. Within one process the
    original FC time is preserved, so the clock keeps elapsing."""
    advisor = FakeAdvisor([decision()])
    harness = make_harness(readings=[reading()], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:
        harness.controller.transition_to(step)
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)  # FC edge arms the clock
    harness.clock.advance(60.0)
    # Recovery, then resume back into development — a non-FC re-entry.
    harness.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    harness.clock.advance(30.0)
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    harness.clock.advance(10.0)
    harness.controller.request_advisory()
    await harness.controller.tick()
    # Elapsed since the ORIGINAL FC (60+30+10), not reset to ~10.
    assert advisor.contexts[-1].development_elapsed_seconds == 100.0


# --- #239: development time + DTR freeze at the drop (no climb into cooling) ---


def _charged_developed_harness() -> Harness:
    """A harness driven (charge clock stamped) → development, FC armed.

    Stamps the charge clock the real way (it is set only on the debounced-T0
    path / on the ROASTING_PRE_FIRST_CRACK entry from PREHEATING), so the
    charge-referenced DTR denominator is non-zero — a bare ``transition_to``
    into pre-FC would leave it 0.0 and make the DTR undefined.
    """
    harness = make_harness(readings=[reading()])
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    # Stamp the charge clock explicitly at the current instant (the debounced-T0
    # transition does this in _apply_phase_rules; a bare transition_to does not).
    harness.controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    harness.controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    return harness


def test_development_and_dtr_freeze_at_drop_on_snapshot() -> None:
    """#239: once the beans are dropped (transition into COOLING), the snapshot's
    development_elapsed_seconds and development_percent (the #220 display fields)
    FREEZE at their drop values instead of climbing into cooling.

    Before the fix ``_development_elapsed_seconds`` returned ``now - fc`` unbounded,
    so the post-drop readout kept counting. Advisory/display-only: no
    transition/verdict/executor/drop-gate reads these clocks."""
    harness = _charged_developed_harness()
    harness.clock.advance(120.0)  # pre-FC roast time (charge → FC)
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)  # arms the FC clock
    harness.clock.advance(60.0)  # 60 s of development before the drop

    at_drop = harness.controller.snapshot()
    assert at_drop.development_elapsed_seconds == 60.0
    # DTR = development / charge = 60 / (120 + 60) = 33.33…%.
    assert at_drop.development_percent == pytest.approx(60.0 / 180.0 * 100.0)

    # Drop the beans → COOLING stamps the drop instant.
    harness.controller.transition_to(RoastPhase.COOLING)
    # Cooling runs on for two more minutes — the readouts must NOT move.
    harness.clock.advance(120.0)

    frozen = harness.controller.snapshot()
    assert frozen.development_elapsed_seconds == 60.0  # frozen, not 180.0
    assert frozen.development_percent == pytest.approx(60.0 / 180.0 * 100.0)
    # Identical to the values AT the drop — the whole point of #239.
    assert frozen.development_elapsed_seconds == at_drop.development_elapsed_seconds
    assert frozen.development_percent == at_drop.development_percent


def test_snapshot_charge_elapsed_is_none_before_charge() -> None:
    """#308: the snapshot's ``charge_elapsed_seconds`` is ``None`` before charge.

    The operator-facing ROAST TIME source: ``None`` during preheat (the SPA shows
    '—', not a misleading ``0:00``), so the header reads no roast time until the
    bean is on the drum. Distinct from ``roast_elapsed_seconds`` (serve-referenced)
    which keeps climbing through preheat for the chart's raw x lead-in."""
    harness = make_harness(readings=[reading()])
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    harness.clock.advance(45.0)  # preheat time elapses

    snap = harness.controller.snapshot()
    assert snap.charge_elapsed_seconds is None  # no charge yet
    # The serve-referenced chart clock still advanced through preheat.
    assert snap.roast_elapsed_seconds == pytest.approx(45.0)


def test_snapshot_charge_elapsed_is_since_charge_after_t0() -> None:
    """#308: after charge (T0) the snapshot's ``charge_elapsed_seconds`` counts
    since charge, NOT since serve/run start (the #308 header re-origin).

    Charge is stamped, then 90 s elapse: the charge clock reads 90 s while the
    serve clock includes the prior preheat lead-in."""
    harness = _charged_developed_harness()  # preheat → charge stamped → pre-FC
    harness.clock.advance(90.0)

    snap = harness.controller.snapshot()
    charge_elapsed = snap.charge_elapsed_seconds
    assert charge_elapsed is not None
    assert charge_elapsed == pytest.approx(90.0)  # since charge
    # Distinct from the serve clock, which is >= the charge clock (includes preheat).
    assert snap.roast_elapsed_seconds >= charge_elapsed


def test_snapshot_charge_elapsed_freezes_at_drop() -> None:
    """#308: once the beans are dropped (COOLING), ``charge_elapsed_seconds`` freezes
    at its drop value instead of climbing into cooling (via ``_effective_now``,
    mirroring the development clock freeze of #239). Display-only."""
    harness = _charged_developed_harness()
    harness.clock.advance(120.0)  # charge → FC
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    harness.clock.advance(60.0)  # 60 s development before the drop

    at_drop = harness.controller.snapshot()
    assert at_drop.charge_elapsed_seconds == pytest.approx(180.0)  # 120 + 60

    harness.controller.transition_to(RoastPhase.COOLING)  # drop stamps the instant
    harness.clock.advance(120.0)  # cooling runs on — the clock must NOT move

    frozen = harness.controller.snapshot()
    assert frozen.charge_elapsed_seconds == pytest.approx(180.0)  # frozen, not 300.0
    assert frozen.charge_elapsed_seconds == at_drop.charge_elapsed_seconds


def test_advisor_context_development_clock_frozen_after_drop() -> None:
    """#239: the advisor context's development_elapsed_seconds AND
    roast_elapsed_seconds (the DTR numerator + denominator) freeze at their drop
    values. The advisor is not consulted in cooling, so this asserts the context
    *mapping* directly (``_build_advisor_context``): the field a post-drop build
    would carry holds the drop figures, not values climbing into cooling.
    Advisory/display-only — the context feeds no control/safety path."""
    harness = _charged_developed_harness()
    harness.clock.advance(120.0)
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    harness.clock.advance(60.0)
    harness.controller.transition_to(RoastPhase.COOLING)  # drop
    harness.clock.advance(90.0)  # cooling time that must not leak into the clocks

    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    assert ctx.development_elapsed_seconds == 60.0  # frozen at drop, not 150.0
    assert ctx.roast_elapsed_seconds == 180.0  # charge clock frozen at drop, not 270.0


def test_drop_clock_resets_on_new_run_so_next_roast_runs_live() -> None:
    """#239: a new run/preheat un-freezes the clocks — the drop instant is cleared
    so the next roast's development time runs live again rather than staying frozen
    at the prior roast's drop value."""
    harness = _charged_developed_harness()
    harness.clock.advance(120.0)
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    harness.clock.advance(60.0)
    harness.controller.transition_to(RoastPhase.COOLING)  # freezes the clocks
    # Finish and start a fresh roast through the legal path.
    harness.controller.transition_to(RoastPhase.COMPLETE)
    harness.controller.transition_to(RoastPhase.IDLE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    # The new run's clocks are live again (drop instant cleared) — no leak.
    harness.controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    harness.controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    harness.clock.advance(25.0)
    snap = harness.controller.snapshot()
    assert snap.development_elapsed_seconds == 25.0  # live, not the prior 60.0


# --- #219: the advisor's DTR clock is charge-referenced (T0), server-side only.
# The chart/readout clock (ControllerSnapshot.roast_elapsed_seconds) stays
# run/preheat-referenced — re-origining the chart at charge is deferred to #220.


def test_advisor_roast_elapsed_zero_before_charge() -> None:
    """The advisor context's ``roast_elapsed_seconds`` (the DTR denominator) is
    0.0 before charge (#219): it zeros at charge by roasting convention, and
    there is no DTR before there is a bean on the drum. Holds even after preheat
    time has elapsed (the context mapping pre-charge sees 0.0, not the preheat
    duration). Asserted on the mapping (``_build_advisor_context``) — the advisor
    is not consulted pre-FC (#222)."""
    harness = make_harness(readings=[reading()])
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    harness.clock.advance(300.0)  # five minutes of preheat, no charge yet
    # A bare transition_to does NOT stamp the charge clock — only the debounced-T0
    # path in _apply_phase_rules does. So the advisor's charge-referenced clock is
    # still 0.0 even though the run clock is 300 s.
    harness.controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    assert ctx.roast_elapsed_seconds == 0.0


@pytest.mark.asyncio
async def test_advisor_roast_elapsed_counts_from_charge_not_run_start() -> None:
    """Regression for the live 'test 6' defect (#219): the advisor's DTR clock was
    referenced to run/preheat start, so a roast that charged late fed an inflated
    roast duration → understated DTR → late drop past the bitter ceiling. The
    advisor context must count from the debounced T0/charge instant. Here ~5 min
    of preheat precedes charge; the advisor (consulted post-FC under #222) sees
    time-since-charge only, NOT preheat + time-since-charge."""
    crashing = reading(bean=160.0, t0_detected=True, bean_ror_c_per_min=-80.0)
    advisor = FakeAdvisor([decision(heat=40, fan=60)])
    harness = make_harness(readings=[crashing], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    harness.clock.advance(300.0)  # the preheat the OLD advisor clock wrongly counted
    # Debounce three T0 ticks → charge transition stamps the charge clock on the
    # third (at clock=302.0: 300 + two 1.0 advances inside the loop).
    for _ in range(3):
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # First crack → DEVELOPMENT (advisor consulted post-FC). The pre-FC advisor is
    # gated out (#222), so the charge-referenced clock is asserted at the first
    # development consult, two ticks after the charge stamp at clock=302.0.
    harness.reader.readings = [
        reading(bean=185.0, bean_ror_c_per_min=5.0, first_crack_detected=True)
    ]
    harness.clock.advance(1.0)  # clock → 304.0
    await harness.controller.tick()  # → DEVELOPMENT, first (PHASE_CHANGE) consult
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    ctx = advisor.contexts[-1]
    # Charge stamped at clock=302.0; the first development consult fires on the FC
    # tick at clock=304.0 → 2.0 s (the ~5 s post-FC dwell, #276, means the very
    # next tick does NOT re-consult), NOT ~304 s from run start (the old bug).
    assert ctx.roast_elapsed_seconds == pytest.approx(2.0)
    assert ctx.roast_elapsed_seconds < 300.0
    # It matches seconds_since_charge (same charge instant, the bake-off convention).
    assert ctx.roast_elapsed_seconds == pytest.approx(ctx.seconds_since_charge)


@pytest.mark.asyncio
async def test_advisor_dtr_is_charge_referenced_post_fc() -> None:
    """The DTR the advisor computes (development_elapsed / roast_elapsed) is now
    charge-referenced end to end (#219), matching the v4-prompt definition the
    bake-off validated. Charge → 100 s pre-FC → FC → 25 s development gives DTR =
    25 / 125 = 0.20, not 25 / (preheat + 125)."""
    crashing = reading(bean=160.0, t0_detected=True, bean_ror_c_per_min=-80.0)
    advisor = FakeAdvisor([decision()])
    harness = make_harness(readings=[crashing], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    harness.clock.advance(600.0)  # long preheat — would dominate a run-start clock
    for _ in range(3):  # debounce → charge stamp on the 3rd tick (clock=602.0)
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    harness.clock.advance(100.0 - 1.0)  # 100 s total since the charge stamp at FC
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)  # FC edge arms dev clock
    harness.clock.advance(25.0)  # development time
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=4.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    ctx = advisor.contexts[-1]
    assert ctx.development_elapsed_seconds is not None
    assert ctx.development_elapsed_seconds == pytest.approx(25.0)
    assert ctx.roast_elapsed_seconds == pytest.approx(125.0)  # NOT 600 + 125
    dtr = ctx.development_elapsed_seconds / ctx.roast_elapsed_seconds
    assert dtr == pytest.approx(0.20)


@pytest.mark.asyncio
async def test_snapshot_clock_stays_run_referenced_not_charge() -> None:
    """The chart/readout clock the SPA renders — ``ControllerSnapshot.
    roast_elapsed_seconds`` — stays run/preheat-referenced (#219 re-scope). The
    dashboard plots each point at ``t = elapsed_seconds`` and must keep showing
    the pre-charge preheat/RoR curve (#165); a charge-referenced value would
    collapse every pre-charge row onto x=0. Charge-referencing is confined to the
    advisor context; re-origining the chart at charge is deferred to #220."""
    crashing = reading(bean=160.0, t0_detected=True, bean_ror_c_per_min=-80.0)
    harness = make_harness(readings=[crashing])
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    harness.clock.advance(180.0)  # preheat
    # Pre-charge the snapshot clock already counts the preheat (run-referenced),
    # NOT a flat charge-referenced 0.0 — so pre-charge telemetry charts/persists.
    assert harness.controller.snapshot().roast_elapsed_seconds == pytest.approx(180.0)
    # Charge, then a tick: the snapshot clock keeps counting from run start
    # (~181 s), it does NOT reset to ~0 at charge.
    for _ in range(3):
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert harness.controller.snapshot().roast_elapsed_seconds == pytest.approx(183.0)


@pytest.mark.asyncio
async def test_advisor_charge_clock_resets_on_new_run() -> None:
    """A new run/preheat clears the charge clock (#219), so a stale charge time
    from a prior roast never inflates the next roast's advisor DTR clock — it
    reads 0.0 again until the next charge stamps it. The advisor is consulted
    post-FC (#222); the fresh-run reset is asserted on the context mapping
    (``_build_advisor_context``) back in pre-first-crack."""
    crashing = reading(bean=160.0, t0_detected=True, bean_ror_c_per_min=-80.0)
    advisor = FakeAdvisor([decision()])
    harness = make_harness(readings=[crashing], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    for _ in range(3):  # charge the first roast
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # The charge clock is stamped (> 0 since charge) — read via the context mapping.
    first_limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    first_ctx = harness.controller._build_advisor_context(  # pyright: ignore[reportPrivateUsage]
        reading(), first_limits
    )
    assert first_ctx.roast_elapsed_seconds > 0.0
    # Finish and start a fresh run along the legal path.
    for step in [
        RoastPhase.DEVELOPMENT,
        RoastPhase.COOLING,
        RoastPhase.COMPLETE,
        RoastPhase.IDLE,
        RoastPhase.STARTING,
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
    ]:
        harness.controller.transition_to(step)
    harness.clock.advance(50.0)
    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    # The fresh run never re-charged (bare transition), so the advisor clock is 0.0.
    assert ctx.roast_elapsed_seconds == 0.0


# --- #220: the snapshot surfaces development time + DTR for the live readouts.
# Read-only projections of the already-computed advisor clocks; charge-referenced
# DTR, NOT a chart re-origin (the snapshot roast clock above stays run-referenced).


def test_snapshot_development_fields_none_before_first_crack() -> None:
    """Before first crack the snapshot carries no development time / DTR (#220):
    there is no development yet, so both readouts render '—'. Holds even after
    charge + pre-FC roast time has elapsed."""
    crashing = reading(bean=160.0, t0_detected=True, bean_ror_c_per_min=-80.0)
    harness = make_harness(readings=[crashing])
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    snap = harness.controller.snapshot()
    assert snap.development_elapsed_seconds is None
    assert snap.development_percent is None


@pytest.mark.asyncio
async def test_snapshot_development_fields_post_fc_are_charge_referenced() -> None:
    """Post-FC the snapshot exposes BOTH live readouts (#220): development time
    (seconds since FC) and DTR as a percentage of the WHOLE roast, computed on
    the charge-referenced clock (consistent with the advisor's DTR, #219). With a
    long preheat, charge → 100 s pre-FC → FC → 25 s development, DTR =
    25 / 125 * 100 = 20% (NOT 25 / (preheat + 125))."""
    crashing = reading(bean=160.0, t0_detected=True, bean_ror_c_per_min=-80.0)
    harness = make_harness(readings=[crashing])
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    harness.clock.advance(600.0)  # long preheat — would dominate a run-start clock
    for _ in range(3):  # debounce → charge stamp on the 3rd tick (clock=602.0)
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    harness.clock.advance(100.0 - 1.0)  # 100 s since the charge stamp at FC
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)  # FC edge arms dev clock
    harness.clock.advance(25.0)  # development time
    snap = harness.controller.snapshot()
    assert snap.development_elapsed_seconds == pytest.approx(25.0)
    assert snap.development_percent == pytest.approx(20.0)  # 25 / 125 * 100
    # The two readouts are DISTINCT (a duration vs a ratio), and the snapshot's
    # chart clock still counts from run start (~600 preheat + ~100 + 25, NOT the
    # charge-referenced ~125) — the chart origin is unchanged by #220.
    assert snap.roast_elapsed_seconds > 700.0


@pytest.mark.asyncio
async def test_snapshot_dtr_matches_advisor_dtr() -> None:
    """The operator's DTR readout and the advisor's DTR are the SAME number (#220):
    the snapshot's ``development_percent`` is exactly the advisor context's
    ``development_elapsed / roast_elapsed`` * 100, so the dashboard never disagrees
    with the loop driving the drop."""
    crashing = reading(bean=160.0, t0_detected=True, bean_ror_c_per_min=-80.0)
    advisor = FakeAdvisor([decision()])
    harness = make_harness(readings=[crashing], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    harness.clock.advance(600.0)
    for _ in range(3):
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    harness.clock.advance(100.0 - 1.0)
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    harness.clock.advance(25.0)
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=4.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    ctx = advisor.contexts[-1]
    assert ctx.development_elapsed_seconds is not None
    advisor_dtr_percent = ctx.development_elapsed_seconds / ctx.roast_elapsed_seconds * 100.0
    snap = harness.controller.snapshot()
    assert snap.development_percent == pytest.approx(advisor_dtr_percent)


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
    drop-eligibility honours the recommendation only once development begins.
    Under the #312 drop coherence guard the drop fires not the instant
    development opens (dev% ≈ 0) but once the SYSTEM's real development reaches
    the target window — every earlier ``drop=True`` (preheating, pre-FC, and the
    too-early development ticks) is safely withheld. This is the architecture
    invariant in motion: the advisor keeps advising, the controller decides when
    it is safe to obey."""
    warm = reading(bean=120.0, env=140.0)
    charge = reading(bean=178.0, env=185.0)
    t0 = reading(bean=95.0, t0_detected=True)  # charge drop, T0 reported
    fc = reading(bean=196.0, t0_detected=True, first_crack_detected=True)
    dev = reading(bean=200.0, t0_detected=True, first_crack_detected=True)
    log: list[str] = []
    # Extra development frames so the roast can DEVELOP into the target window
    # before the coherence guard lets the advisor drop fire (#312).
    mcp = FakeMCPClient([warm, charge, t0, t0, t0, fc, dev, dev, dev, dev, dev], log)
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
    # The FC frame enters development; the drop is NOT honoured yet (#312) —
    # development just opened, so the system's real development is ~0 %, far below
    # the profile's 20 % target window, and the coherence guard withholds it.
    await controller.tick()
    clock.advance(2.5)
    assert controller.phase is RoastPhase.DEVELOPMENT
    # Develop into the target window: advance the development clock so the system
    # dev% climbs to within the margin of target, then the standing drop=True is
    # honoured and the roast drops → cooling.
    clock.advance(60.0)
    for _ in range(4):
        await controller.tick()
        clock.advance(2.5)
        if controller.phase is RoastPhase.COOLING:
            break
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
    # Clamped to the pre-FC box at run start (carry-forward A, #222): 70/40 → 100/30.
    assert harness.executor.targets == [(100, 30)]
    # Finish the run instantly (no clock advance) and start the next.
    for step in NORMAL_PATH[2:6]:
        harness.controller.transition_to(step)
    harness.controller.operator_acknowledge_fault()  # complete → idle
    await harness.controller.start_run(PROFILE)
    assert harness.executor.targets == [(100, 30), (100, 30)]  # second write happened


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

    # stop_cooling's drift guard now fires only when it would COMPLETE the run —
    # i.e. only from COOLING (#206: from faulted it issues the cooling write with
    # no transition, so there is no unreachable-COMPLETE to guard). COOLING →
    # COMPLETE is structurally always reachable in the transition table, so the
    # guard's raising branch is unreachable in practice (the foundation tags it
    # `# pragma: no cover`); the drop/FC cells above still exercise the pattern.


class RejectingCommandPolicy(SafetyPolicy):
    def evaluate_command(
        self,
        *,
        requested_heat: int,
        requested_fan: int,
        seconds_since_last_command: float | None,
        bounds: PhaseControlLimits | None = None,
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
async def test_operator_start_cooling_in_faulted_does_not_transition() -> None:
    """#206: from faulted, start_cooling engages cooling on a hot faulted machine
    and issues the MCP write WITHOUT a phase transition — the run stays faulted
    (heat off) until the operator acknowledges it."""
    harness = make_harness()
    harness.controller.transition_to(RoastPhase.FAULTED)
    harness.events.events.clear()
    await harness.controller.operator_start_cooling()
    assert "start_cooling" in harness.executor.commands
    assert harness.controller.phase is RoastPhase.FAULTED
    # No completion event — the faulted run is not finalised by cooling.
    assert RoastEventKind.RUN_COMPLETED not in harness.events.kinds()


@pytest.mark.asyncio
async def test_operator_stop_cooling_in_faulted_does_not_transition() -> None:
    """#206: from faulted, stop_cooling stops the cooling fan an e-stop/fault
    engaged and issues the MCP write WITHOUT completing the run — the run stays
    faulted until acknowledged (no power cycle needed to stop the fan)."""
    harness = make_harness()
    harness.controller.transition_to(RoastPhase.FAULTED)
    harness.events.events.clear()
    await harness.controller.operator_stop_cooling()
    assert "stop_cooling" in harness.executor.commands
    assert harness.controller.phase is RoastPhase.FAULTED
    assert RoastEventKind.RUN_COMPLETED not in harness.events.kinds()


@pytest.mark.asyncio
async def test_recover_into_faulted_re_enters_faulted_heat_off_no_write() -> None:
    """#206: a restart finding a persisted FAULTED run re-enters the operable-
    faulted state — no MCP write, heat/fan not auto-resumed — distinct from the
    active-roast → operator_recovery_required path. A FAULT event records it."""
    harness = make_harness()
    harness.events.events.clear()
    await harness.controller.recover_into_faulted(RoastPhase.FAULTED)
    assert harness.controller.phase is RoastPhase.FAULTED
    # No hardware write on recovery (restart-never-auto-resumes).
    assert harness.executor.commands == []
    snapshot = harness.controller.snapshot()
    assert (snapshot.current_heat, snapshot.current_fan) == (0, 0)
    assert RoastEventKind.FAULT in harness.events.kinds()


@pytest.mark.asyncio
async def test_recover_into_faulted_noop_for_non_faulted_phase() -> None:
    """recover_into_faulted only acts on a persisted FAULTED phase; an active-roast
    phase is a no-op here (the caller routes it to recover_from_restart)."""
    harness = make_harness()
    harness.events.events.clear()
    await harness.controller.recover_into_faulted(RoastPhase.DEVELOPMENT)
    assert harness.controller.phase is RoastPhase.IDLE
    assert harness.executor.commands == []
    assert RoastEventKind.FAULT not in harness.events.kinds()


@pytest.mark.asyncio
async def test_recover_into_faulted_is_idempotent_when_already_faulted() -> None:
    """Calling recover_into_faulted when the controller is already FAULTED is a
    no-op transition (the defensive `if self._phase is not FAULTED` guard): it
    re-records the fault but never attempts an illegal FAULTED→FAULTED move."""
    harness = make_harness()
    await harness.controller.recover_into_faulted(RoastPhase.FAULTED)
    assert harness.controller.phase is RoastPhase.FAULTED
    harness.events.events.clear()
    # Second call: already FAULTED → the guard skips the transition (no raise).
    await harness.controller.recover_into_faulted(RoastPhase.FAULTED)
    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.executor.commands == []
    assert RoastEventKind.FAULT in harness.events.kinds()


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
    * ``acknowledge_fault`` (#206): the drain
      (``RoastRunner._dispatch_acknowledge_fault``) acts iff the controller is in
      ``faulted`` (it flips the runner's finalise flag); the
      controller-observable acceptance condition is exactly "phase is FAULTED".
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
    if action is OperatorAction.ACKNOWLEDGE_FAULT:
        # The acknowledge-fault drain (RoastRunner._dispatch_acknowledge_fault)
        # acts — flips the runner's _fault_acknowledged flag so the run finalises —
        # iff the controller is in FAULTED; any other phase records a failed action.
        # The controller-observable condition is exactly "phase is FAULTED".
        return phase is RoastPhase.FAULTED
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


def test_acknowledge_fault_enabled_only_in_faulted() -> None:
    """acknowledge_fault (#206) mirrors acknowledge_recovery: enabled iff the
    phase is faulted — the only phase the drain acts on it."""
    for phase in RoastPhase:
        expected = phase is RoastPhase.FAULTED
        assert (OperatorAction.ACKNOWLEDGE_FAULT in enabled_operator_actions(phase)) is expected


def test_emergency_stop_enabled_in_every_phase() -> None:
    """E-stop is always available — its matrix row is the full phase set."""
    for phase in RoastPhase:
        assert OperatorAction.EMERGENCY_STOP in enabled_operator_actions(phase)


# --- #275: per-tick control-loop context (roast-so-far curve + DTR + trace) ---


@pytest.mark.asyncio
async def test_context_carries_roast_so_far_curve_after_charge() -> None:
    """#275 (D40.3): once charged, the per-tick context curve window accumulates
    the roast-so-far telemetry (bean/env/heat/fan + RoR) and the model's own
    decision trace — and the advisor context surfaces both post-FC."""
    advisor = FakeAdvisor([], default_decision=decision(heat=70, fan=40))
    t0 = reading(bean=120.0, t0_detected=True, bean_ror_c_per_min=-40.0)
    harness = make_harness(readings=[t0], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    # Debounce T0 → ROASTING_PRE_FIRST_CRACK (charge clock stamped on tick 3).
    for _ in range(3):
        harness.reader.readings = [t0]
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # A few pre-FC ticks: the bean turns and warms (TP then recovery), the
    # advisor stays silent (deterministic pre-FC), but the curve accumulates.
    pre_fc = [
        reading(bean=118.0, bean_ror_c_per_min=-5.0),  # still crashing
        reading(bean=119.0, bean_ror_c_per_min=2.0),  # turning point (RoR >= 0)
        reading(bean=125.0, bean_ror_c_per_min=10.0),  # recovery
        reading(bean=140.0, bean_ror_c_per_min=12.0),
    ]
    for r in pre_fc:
        harness.reader.readings = [r]
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert advisor.contexts == []  # advisor silent pre-FC
    # First crack → DEVELOPMENT, then a development consult.
    harness.reader.readings = [
        reading(bean=176.0, bean_ror_c_per_min=8.0, first_crack_detected=True)
    ]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    harness.clock.advance(5.0)  # clear the post-FC dwell (#276) so the next consult fires
    harness.reader.readings = [reading(bean=180.0, bean_ror_c_per_min=5.0)]
    await harness.controller.tick()
    assert advisor.contexts
    ctx = advisor.contexts[-1]
    # The roast-so-far curve is present and carries the FULL reading per sample
    # (bean/env/RoR), not just bean temp — the model reads the whole curve.
    assert len(ctx.roast_curve_window) > 3
    last = ctx.roast_curve_window[-1]
    assert last.bean_temp_c == 180.0
    assert last.env_temp_c == 200.0  # reading()'s default env
    assert last.bean_ror_c_per_min == 5.0  # the RoR this tick read
    # Paired action+response: the sample carries the levers the controller
    # ACTUALLY commanded this tick (the default-decision advice heat=70/fan=40,
    # applied by the ALLOW path), not a placeholder zero. A regression that
    # stored 0 or a stale lever would fail here.
    assert last.heat_percent == 70
    assert last.fan_percent == 40
    assert (last.heat_percent, last.fan_percent) == harness.executor.targets[-1]
    # Milestones: turning point + recovery (pre-FC) and first crack.
    kinds = {m.kind for m in ctx.roast_milestones}
    assert RoastMilestoneKind.TURNING_POINT in kinds
    assert RoastMilestoneKind.RECOVERY in kinds
    assert RoastMilestoneKind.FIRST_CRACK in kinds
    fc = next(m for m in ctx.roast_milestones if m.kind is RoastMilestoneKind.FIRST_CRACK)
    assert fc.bean_temp_c == 176.0  # bean temp at the crack
    # The RECOVERY milestone carries the post-turning-point bean RoR as its
    # scalar value (#229 KEEP) — the recovery reading was the bean_ror=10.0 tick.
    recovery = next(m for m in ctx.roast_milestones if m.kind is RoastMilestoneKind.RECOVERY)
    assert recovery.value == 10.0
    # Development time AND DTR are two DISTINCT, correctly-computed values: the
    # DTR is dev_elapsed / charge_elapsed (a fraction), NOT the duration and NOT
    # the percent. A dropped /100 (leaving a percent) would fail this.
    assert ctx.development_elapsed_seconds is not None
    assert ctx.development_time_ratio is not None
    expected_dtr = ctx.development_elapsed_seconds / ctx.roast_elapsed_seconds
    assert abs(ctx.development_time_ratio - expected_dtr) < 1e-9
    assert ctx.development_time_ratio < 1.0  # a fraction, not a percent
    # FC-ETA is None post-FC (the detector owns FC now).
    assert ctx.first_crack_eta_seconds is None


@pytest.mark.asyncio
async def test_context_decision_trace_records_model_own_recommendations() -> None:
    """#275 (D40.5 / #218): the model's own prior recommendations are encoded in
    the decision trace, so the NEXT consult sees its trajectory."""
    advisor = FakeAdvisor(
        [decision(heat=70, fan=40), decision(heat=55, fan=45)],
        default_decision=decision(heat=50, fan=50),
    )
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    # First development consult: no prior trace yet.
    await harness.controller.tick()
    assert advisor.contexts[-1].decision_trace == []
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=185.0)]
    # Second consult: the first recommendation is now in the trace.
    await harness.controller.tick()
    trace = advisor.contexts[-1].decision_trace
    assert len(trace) == 1
    assert trace[0].target_heat == 70
    assert trace[0].target_fan == 40
    assert trace[0].should_drop is False


@pytest.mark.asyncio
async def test_decision_trace_records_requested_not_gate_adjusted() -> None:
    """#275 / #218: the trace must carry what the MODEL asked for, NOT what the
    safety gate then applied. Exercised via a rate-limit REJECT: the second
    consult fires before min_seconds_between_commands so the gate REJECTs it (no
    command executes, the levers do not change), yet the rejected recommendation
    must still land in the trace with its REQUESTED values — that is the model's
    own move history the next tick reasons from. A trace that recorded the
    applied/clamped value (or skipped the rejected consult) would fail here."""
    advisor = FakeAdvisor([decision(heat=80, fan=35), decision(heat=20, fan=70)])
    # min_seconds_between_commands=2.0 (default); the second consult is a MANUAL
    # request 1.0 s later — manual bypasses the post-FC consult dwell (#276) so it
    # fires inside the rate-limit window and the gate REJECTs it (nothing executes).
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    await harness.controller.tick()  # consult 1: heat=80/fan=35 ALLOWed + executed
    assert harness.executor.targets[-1] == (80, 35)
    harness.clock.advance(1.0)  # < 2.0 s → next command is rate-limited
    harness.reader.readings = [reading(bean=188.0)]
    harness.controller.request_advisory()  # manual: bypasses the dwell, still rate-limited
    await harness.controller.tick()  # consult 2: heat=20/fan=70 REJECTed (no exec)
    # No second command executed (still the consult-1 levers).
    assert harness.executor.targets[-1] == (80, 35)
    # …but the REJECTed recommendation is recorded with its REQUESTED values.
    trace = harness.controller._history.decision_trace()  # pyright: ignore[reportPrivateUsage]
    assert len(trace) == 2
    assert (trace[1].target_heat, trace[1].target_fan) == (20, 70)  # requested, not applied
    assert (trace[1].target_heat, trace[1].target_fan) != harness.executor.targets[-1]


@pytest.mark.asyncio
async def test_fc_eta_present_pre_fc_when_advisor_context_built() -> None:
    """#275 / #229: the FC-ETA is a pre-FC anticipation scalar. Built directly
    on a charged, warming curve it projects a positive ETA; post-FC it is None.
    (Pre-FC the advisor is not consulted, so this exercises the builder via a
    manual context build to assert the scalar without a live consult.)"""
    advisor = FakeAdvisor([], default_decision=decision())
    t0 = reading(bean=150.0, t0_detected=True, bean_ror_c_per_min=2.0)
    harness = make_harness(readings=[t0], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:
        harness.controller.transition_to(step)
    for _ in range(3):  # debounce charge
        harness.reader.readings = [t0]
        await harness.controller.tick()
        harness.clock.advance(1.0)
    # Warm steadily toward the FC target so the extrapolation has a slope.
    for bean in (155.0, 160.0, 165.0, 170.0):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=10.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
    # Build the context directly (pre-FC the advisor is gated out by design).
    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = harness.controller._build_advisor_context(  # pyright: ignore[reportPrivateUsage]
        reading(bean=171.0, bean_ror_c_per_min=10.0), limits
    )
    assert ctx.first_crack_eta_seconds is not None
    assert ctx.first_crack_eta_seconds > 0.0


@pytest.mark.asyncio
async def test_history_resets_on_new_run() -> None:
    """#275: a new run/preheat clears the per-tick context history (per-roast)."""
    advisor = FakeAdvisor([], default_decision=decision())
    t0 = reading(bean=120.0, t0_detected=True, bean_ror_c_per_min=2.0)
    harness = make_harness(readings=[t0], advisor=advisor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:
        harness.controller.transition_to(step)
    for _ in range(3):
        harness.reader.readings = [t0]
        await harness.controller.tick()
        harness.clock.advance(1.0)
    history = harness.controller._history  # pyright: ignore[reportPrivateUsage]
    assert history.curve_window()  # accumulated something
    # Seed a decision so the reset's clearing of the trace is a real trap (the
    # pre-FC harness never consults, so the trace would be empty by default).
    history.record_decision(
        DecisionTraceEntry(
            elapsed_since_charge_seconds=1.0,
            target_heat=60,
            target_fan=40,
            should_drop=False,
            confidence=0.5,
        )
    )
    assert history.decision_trace()  # seeded
    # A fresh preheat (a new run) resets ALL history (curve, milestones, trace).
    harness.controller.transition_to(RoastPhase.COOLING)
    harness.controller.transition_to(RoastPhase.COMPLETE)
    harness.controller.transition_to(RoastPhase.IDLE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    assert history.curve_window() == []
    assert history.milestones() == []
    assert history.decision_trace() == []


# --- #276: post-FC coherence/deadband gate + drop + fail-closed ---


def _advisory_events(harness: Harness) -> list[dict[str, object]]:
    """Every ADVISORY event payload, in order (the decision-trace stream)."""
    return [
        cast(dict[str, object], p) for k, p in harness.events.events if k is RoastEventKind.ADVISORY
    ]


@pytest.mark.asyncio
async def test_deadband_damps_a_flip_flop_but_allows_a_decisive_move() -> None:
    """#276: a sub-threshold direction reversal across consecutive post-FC consults
    is damped to a HOLD (the #218 30<->40<->30 thrash), while a decisive move
    (>= the deadband threshold) is applied.

    Three consults, each past the 5 s post-FC dwell so they fire on their own:
      1. heat 70 / fan 50 from 0/0 — first move (UP/UP), executed.
      2. heat 60 / fan 45 — both a -10 reversal (< 15 threshold) — DAMPED: no
         write, levers held at (70, 50), and a COHERENCE_DAMPED note emitted.
      3. heat 50 / fan 50 — heat -20 (>= 15, decisive reversal) — applied; fan
         unchanged from the held 50, so the executed pair is (50, 50).

    Pins an explicit threshold of 15 (not the tuned default 10, #277) so the -10
    moves are genuinely sub-threshold — this exercises the damping MECHANISM,
    decoupled from the data-tuned production default.
    """
    advisor = FakeAdvisor(
        [decision(heat=70, fan=50), decision(heat=60, fan=45), decision(heat=50, fan=50)]
    )
    harness = harness_in_development(
        readings=[reading()],
        advisor=advisor,
        config=ControllerConfig(post_fc_deadband_threshold_percent=15),
    )
    # Consult 1: first move, executed.
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (70, 50)
    # Consult 2: sub-threshold reversal on both levers — damped, no new write.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=185.0)]
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (70, 50)  # still the consult-1 levers
    damped = [p for p in _advisory_events(harness) if "coherence_damped" in p]
    assert damped, "the damped reversal must surface a COHERENCE_DAMPED note"
    assert set(cast(list[str], damped[-1]["coherence_damped"])) == {"heat", "fan"}
    # Consult 3: a decisive heat cut (>= threshold) IS applied.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0)]
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (50, 50)


@pytest.mark.asyncio
async def test_deadband_allows_a_single_decisive_first_move() -> None:
    """#276: the deadband never damps a first or same-direction move — a single
    decisive step on entry to development is applied unchanged (it only damps an
    incoherent reversal)."""
    advisor = FakeAdvisor([decision(heat=80, fan=60)])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (80, 60)


@pytest.mark.asyncio
async def test_every_post_fc_lever_write_traces_to_a_safety_evaluation() -> None:
    """#276 invariant: an executed post-FC lever pair is preceded by an ALLOW/CLAMP
    command evaluation — the coherence gate sits AFTER safety and can only hold,
    never bypass it."""
    advisor = FakeAdvisor([decision(heat=65, fan=50)])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (65, 50)
    command_evals = [
        e for e in harness.sink.evaluations if e.rule in ("all_clear", "command_bounds")
    ]
    assert command_evals, "the executed lever write must trace to a command evaluation"
    assert command_evals[-1].verdict in (SafetyVerdict.ALLOW, SafetyVerdict.CLAMP)


@pytest.mark.asyncio
async def test_low_confidence_recommendation_fails_closed_to_hold() -> None:
    """#276 fail-closed: a below-floor-confidence recommendation does NOT actuate —
    it holds the current levers, records a REJECT (advisor_low_confidence) verdict,
    and still traces the decision for diagnosis. The drop is not evaluated."""
    # First consult (confident) sets a baseline; second is low-confidence + drop.
    confident = RoastDecision(
        target_heat=70, target_fan=50, should_drop=False, confidence=0.9, rationale="ok"
    )
    unsure = RoastDecision(
        target_heat=20, target_fan=90, should_drop=True, confidence=0.05, rationale="unsure"
    )
    advisor = FakeAdvisor([confident, unsure])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (70, 50)
    writes_before = len(harness.executor.targets)
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0)]
    await harness.controller.tick()
    # No new lever write and NO drop on the unsure recommendation.
    assert len(harness.executor.targets) == writes_before
    assert "drop_beans" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert harness.sink.evaluations[-1].rule == "advisor_low_confidence"
    assert harness.sink.evaluations[-1].verdict is SafetyVerdict.REJECT
    # The unsure decision is still traced (honest diagnosis).
    assert harness.sink.advisor_decisions[-1].decision is not None
    assert harness.sink.advisor_decisions[-1].decision.confidence == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_silent_advisor_does_not_actuate_post_fc() -> None:
    """#276 fail-closed: an erroring advisor never moves the levers — after a
    confident first move, a provider error holds (no new write, no drop)."""
    advisor = FakeAdvisor([decision(heat=70, fan=50), AdvisorFailureMode.PROVIDER_ERROR])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (70, 50)
    writes_before = len(harness.executor.targets)
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0)]
    await harness.controller.tick()
    assert len(harness.executor.targets) == writes_before  # held, no actuation
    assert "drop_beans" not in harness.executor.commands
    # Fail-closed = NO state change: an erroring consult must not advance the phase.
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_sustained_sub_threshold_cut_actuates_within_two_consults() -> None:
    """#276 Fix 1 (controller-level): a SUSTAINED sub-threshold heat cut must NOT
    latch the lever high forever — it actuates the cut by the second consult.

    The over-roast failure mode: after a confident heat RAISE (UP), the advisor
    keeps asking for the SAME small cut (a -10 DOWN, below the 15 deadband). The
    first cut is damped (held), but the damp advances the recorded direction DOWN,
    so the identical second request is a same-direction move and the cut executes.

    Pins an explicit threshold of 15 (not the tuned default 10, #277) so the -10
    cut is genuinely sub-threshold — this exercises the convergence MECHANISM,
    decoupled from the data-tuned production default.
    """
    advisor = FakeAdvisor(
        [decision(heat=80, fan=50), decision(heat=70, fan=50), decision(heat=70, fan=50)]
    )
    harness = harness_in_development(
        readings=[reading()],
        advisor=advisor,
        config=ControllerConfig(post_fc_deadband_threshold_percent=15),
    )
    # Consult 1: a decisive first move UP — executed (heat 80).
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (80, 50)
    # Consult 2: 80 -> 70 is a -10 sub-threshold reversal — damped, held at 80.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=185.0)]
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (80, 50)  # still held — one damped cycle
    # Consult 3: the SAME 70 request — now a same-direction (DOWN) move: the cut
    # actuates. The sustained intent converged; the lever did not stay pinned high.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0)]
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (70, 50)


@pytest.mark.asyncio
async def test_mixed_partial_damp_executes_only_the_decisive_lever() -> None:
    """#276 Fix 4: in one consult, a sub-threshold heat reversal (DAMP) alongside a
    decisive fan move (ALLOW) executes ONLY the fan — the heat is held, only the
    fan direction updates, and the COHERENCE_DAMPED note lists only the heat lever.

    Pins an explicit threshold of 15 (not the tuned default 10, #277) so the -10
    heat reversal is genuinely sub-threshold — this exercises the per-lever
    partial-damp MECHANISM, decoupled from the data-tuned production default.
    """
    advisor = FakeAdvisor([decision(heat=70, fan=50), decision(heat=60, fan=80)])
    harness = harness_in_development(
        readings=[reading()],
        advisor=advisor,
        config=ControllerConfig(post_fc_deadband_threshold_percent=15),
    )
    # Consult 1: first move (heat UP, fan UP) — executed at (70, 50).
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (70, 50)
    # Consult 2: heat 70 -> 60 (-10, sub-threshold reversal of UP) DAMPED; fan
    # 50 -> 80 (+30, decisive) ALLOWED. Only the fan moves: executed (70, 80).
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=185.0)]
    await harness.controller.tick()
    assert harness.executor.targets[-1] == (70, 80)
    # Only the heat lever is reported damped (not both).
    damped = [p for p in _advisory_events(harness) if "coherence_damped" in p]
    assert damped, "the damped heat reversal must surface a COHERENCE_DAMPED note"
    assert set(cast(list[str], damped[-1]["coherence_damped"])) == {"heat"}
    # The heat direction stayed (held value 70); the fan direction advanced UP.
    assert harness.controller._fan_direction is LeverDirection.UP  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_confidence_exactly_at_floor_proceeds() -> None:
    """#276 Fix 3: the low-confidence gate is ``confidence < floor`` (strict), so a
    confidence EXACTLY at the floor PROCEEDS (a normal ALLOW lever write, not the
    fail-closed REJECT). Pins the ``<`` vs ``<=`` boundary against a refactor."""
    floor = ControllerConfig().post_fc_min_confidence
    at_floor = RoastDecision(
        target_heat=70, target_fan=50, should_drop=False, confidence=floor, rationale="boundary"
    )
    advisor = FakeAdvisor([at_floor])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    await harness.controller.tick()
    # Proceeds: the levers actuate and the last evaluation is the command box, not
    # the low-confidence REJECT.
    assert harness.executor.targets[-1] == (70, 50)
    assert harness.sink.evaluations[-1].rule != "advisor_low_confidence"
    assert harness.sink.evaluations[-1].verdict in (SafetyVerdict.ALLOW, SafetyVerdict.CLAMP)


@pytest.mark.asyncio
async def test_advisor_drop_executes_only_on_allow_in_development() -> None:
    """#276: a should_drop recommendation drops the beans only through the ALLOW
    drop-eligibility gate (development). The executed drop traces to that ALLOW
    evaluation, then the controller transitions to COOLING."""
    advisor = FakeAdvisor(
        [
            RoastDecision(
                target_heat=60, target_fan=50, should_drop=True, confidence=0.9, rationale="drop"
            )
        ]
    )
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    await harness.controller.tick()
    assert "drop_beans" in harness.executor.commands
    assert harness.controller.phase is RoastPhase.COOLING
    drop_evals = [e for e in harness.sink.evaluations if e.rule == "drop_eligibility"]
    assert drop_evals and drop_evals[-1].verdict is SafetyVerdict.ALLOW


def test_advisor_drop_outside_development_is_rejected() -> None:
    """#276: the drop-eligibility gate REJECTs a should_drop anywhere but
    development — the safety rule the post-FC drop path passes through."""
    policy = SafetyPolicy(SafetyLimits())
    for phase in RoastPhase:
        evaluation = policy.evaluate_drop_recommendation(phase=phase)
        if phase is RoastPhase.DEVELOPMENT:
            assert evaluation.verdict is SafetyVerdict.ALLOW
        else:
            assert evaluation.verdict is SafetyVerdict.REJECT


@pytest.mark.asyncio
async def test_post_fc_lever_direction_resets_on_a_new_run() -> None:
    """#276: the coherence trajectory is per-roast — a new run clears the recorded
    lever directions, so the first post-FC move of the next roast is judged as a
    first move (never damped against the previous roast's direction)."""
    advisor = FakeAdvisor([decision(heat=70, fan=50)], default_decision=decision(heat=70, fan=50))
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    await harness.controller.tick()
    assert harness.controller._heat_direction is LeverDirection.UP  # pyright: ignore[reportPrivateUsage]
    # End this roast and start a fresh one.
    harness.controller.transition_to(RoastPhase.COOLING)
    harness.controller.transition_to(RoastPhase.COMPLETE)
    harness.controller.transition_to(RoastPhase.IDLE)
    harness.controller.transition_to(RoastPhase.STARTING)
    assert harness.controller._heat_direction is LeverDirection.NONE  # pyright: ignore[reportPrivateUsage]
    assert harness.controller._fan_direction is LeverDirection.NONE  # pyright: ignore[reportPrivateUsage]
