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
    PostFirstCrackControl,
    PreFirstCrackLevers,
    SafetyLimits,
)
from roastpilot_agent.control_policy import PhaseControlLimits, TrimSignal
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
    recording_origin_slug,
)
from roastpilot_agent.mcp_client import RoasterControlAdapter, RoasterMCPClient
from roastpilot_agent.models import (
    AdvisorHealth,
    AdvisorHealthStatus,
    AppliedRoasterState,
    DropReason,
    OperatorAction,
    PostFcHeatAuthorityState,
    ReferenceCurveSample,
    ReferenceLandmarks,
    ReferenceRoast,
    RoastCommand,
    RoastEventKind,
    RoastProfile,
    RoastStyle,
    RoastTelemetry,
)
from roastpilot_agent.roast_history import DecisionTraceEntry, RoastMilestoneKind
from roastpilot_agent.safety import (
    COMMAND_PHASE_MATRIX,
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


#: The harness's own implicit-config baseline (12 Jul D88/D89 promotion):
#: ``ControllerConfig()``'s OWN defaults flipped ``post_first_crack_control.
#: enabled`` / ``ceiling_guard_drop_enabled`` to ``True`` (the operator-ratified
#: promotion, #495) — but the overwhelming majority of this suite's ~150+
#: ``make_harness()``/``harness_in_development()`` call sites were written
#: BEFORE the promotion to deliberately exercise the advisor-driven baseline
#: path (direct heat/fan actuation, no deterministic taper, no ceiling-guard
#: auto-drop) and never intended to exercise the taper/guard at all. Rather
#: than touch every one of those call sites individually (which would either
#: balloon this diff far beyond reason or silently change what dozens of
#: unrelated tests exercise), the harness's OWN implicit default is pinned
#: here to the pre-promotion baseline explicitly — a deliberate test-fixture
#: choice, not a stale copy of the production default. Every test that DOES
#: want the new post-promotion default constructs one explicitly (see
#: :func:`_post_fc_config` / :func:`_ceiling_guard_config`, and any test using
#: bare ``ControllerConfig()`` directly to assert the NEW production default).
_BASELINE_POST_FC_CONFIG = ControllerConfig(
    post_first_crack_control=PostFirstCrackControl(enabled=False, ceiling_guard_drop_enabled=False)
)


def make_harness(
    *,
    readings: list[RoastTelemetry | None | Exception] | None = None,
    advisor: RoastAdvisor | None = None,
    config: ControllerConfig | None = None,
    limits: SafetyLimits | None = None,
    executor: RecordingExecutor | None = None,
    reference_roast: ReferenceRoast | None = None,
) -> Harness:
    log: list[str] = []
    clock = FakeClock()
    reader = ScriptedStateReader(readings, log)
    executor = executor if executor is not None else RecordingExecutor(log)
    sink = RecordingSnapshotSink(log)
    events = EventSink(log)
    controller = RoastController(
        config=config or _BASELINE_POST_FC_CONFIG,
        safety=SafetyPolicy(limits or SafetyLimits()),
        state_reader=reader,
        command_executor=executor,
        snapshot_sink=sink,
        event_emitter=events,
        advisor=advisor,
        clock=clock,
        reference_roast=reference_roast,
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
    executor: RecordingExecutor | None = None,
    limits: SafetyLimits | None = None,
) -> Harness:
    """A DEVELOPMENT harness whose SYSTEM development percent is a known value.

    Stamps the controller's charge (T0) clock and walks the first-crack edge with
    the :class:`FakeClock` advanced so that ``_development_percent`` returns
    ``system_dev_percent`` exactly: development time / charge-referenced roast
    time = the requested fraction. Used to drive the deterministic drop coherence
    guard (#312) from a precisely-known ground-truth development figure.

    ``executor`` is an optional command-executor override (e.g. a subclass that
    raises on ``drop_beans``) — defaults to the standard recording fake so
    existing callers are unaffected (#405 Slice C).

    ``limits`` is an optional :class:`SafetyLimits` override (#563) — defaults
    to :func:`make_harness`'s own default (``SafetyLimits()``) so existing
    callers are unaffected. A caller that raises ``ceiling_guard_temp_c`` well
    above ``PROFILE.target_drop_temp_c`` to isolate a different anchor (the
    D96/#560 recovery tests) must raise ``emergency_drop_temp_c`` (and thus the
    box's ``bitter_ceiling_temp_c``, since the told ceiling now reads
    ``ceiling_guard_temp_c`` directly, #563) to match, or the resolved
    ``PhaseControlLimits`` box fails its own ``emergency_drop_temp_c >
    bitter_ceiling_temp_c`` validator.
    """
    harness = make_harness(
        readings=[reading()], advisor=advisor, config=config, executor=executor, limits=limits
    )
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
    """#327 fail-closed: with NO warming slope from the start the FC-ETA is unknown
    (the estimator returns None) and the window never opens, so the trim never
    engages or latches — even a hot bean (above the late-Maillard floor) holds the
    flat 100 floor (the always-on guarantee FC still arrives, §8.4)."""
    harness = make_harness()
    # Charge with the bean ALREADY flat at 165 °C (above the 155 floor) so there is
    # never a warming slope to project an FC-ETA from — the fail-closed case with no
    # prior in-window engage that could have latched the trim (hysteresis untouched).
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    t0 = reading(bean=165.0, t0_detected=True, bean_ror_c_per_min=0.0)
    harness.reader.readings = [t0]
    for _ in range(3):  # debounce → pre-FC, all at a flat 165
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    for _ in range(10):
        harness.reader.readings = [reading(bean=165.0, bean_ror_c_per_min=0.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.snapshot().current_heat == 100  # fail closed to the floor
    assert harness.controller.snapshot().current_fan == 30


# --- #351: pre-FC drying-end signal (observability) ---


def _drying_end_payloads(harness: Harness) -> list[dict[str, object]]:
    """Every DRYING_END event payload emitted on ``harness``, in order."""
    return [
        cast(dict[str, object], p)
        for k, p in harness.events.events
        if k is RoastEventKind.DRYING_END
    ]


async def _charge_into_pre_fc_below_drying_end(harness: Harness) -> None:
    """Charge into pre-FC with the turning point recorded and the bean BELOW the
    drying-end threshold, so a later rising cross is a clean first crossing (#351).

    Charges via a debounced T0 with an initially NEGATIVE RoR (post-charge crash),
    then crosses to positive so the P2-3 witness gate is satisfied and the turning
    point milestone is recorded — the noise-robust gate the drying-end signal sits
    behind — while the bean is still well under 150 °C.  Without the negative
    sample the witness gate prevents the milestone from ever recording (#409 P2-3).
    """
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    t0 = reading(bean=120.0, t0_detected=True, bean_ror_c_per_min=-10.0)
    harness.reader.readings = [t0]
    for _ in range(3):  # debounce → pre-FC; begins with negative RoR (crash phase)
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # Now cross the RoR from negative to positive — satisfies the witness gate and
    # records the turning-point milestone (required for drying_end to fire).
    harness.reader.readings = [reading(bean=122.0, bean_ror_c_per_min=5.0)]
    await harness.controller.tick()
    harness.clock.advance(1.0)
    harness.log.clear()
    harness.events.events.clear()


@pytest.mark.asyncio
async def test_drying_end_emits_once_on_first_threshold_cross() -> None:
    """#351: the DRYING_END event fires exactly once, the tick the bean probe first
    reaches the configured threshold (default 150 °C), carrying the bean temp at the
    cross + the threshold. A one-way latch: further above-threshold ticks emit no
    second event."""
    harness = make_harness()
    await _charge_into_pre_fc_below_drying_end(harness)
    # Below threshold: no signal yet.
    harness.reader.readings = [reading(bean=148.0, bean_ror_c_per_min=15.0)]
    await harness.controller.tick()
    assert _drying_end_payloads(harness) == []
    # First cross (rising through 150): one event with the real payload.
    harness.clock.advance(1.0)
    harness.reader.readings = [reading(bean=151.0, bean_ror_c_per_min=15.0)]
    await harness.controller.tick()
    payloads = _drying_end_payloads(harness)
    assert payloads == [{"bean_temp_c": 151.0, "threshold_c": 150.0}]
    # Latched: subsequent above-threshold ticks emit no second event.
    harness.clock.advance(1.0)
    harness.reader.readings = [reading(bean=158.0, bean_ror_c_per_min=15.0)]
    await harness.controller.tick()
    assert _drying_end_payloads(harness) == payloads  # unchanged — exactly one


@pytest.mark.asyncio
async def test_drying_end_is_noise_robust_before_the_turning_point() -> None:
    """#351: the signal is gated behind the turning point (bean minimum). A
    transient above-threshold sample before the curve has turned — e.g. a hot
    charge/probe reading during the post-charge crash — does NOT fire it; only the
    genuine rising cross of a turned bean does."""
    harness = make_harness()
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    # Charge with a FALLING bean (negative RoR): the turning point never arms while
    # the bean is still crashing, even though the bean reading is above threshold.
    t0 = reading(bean=155.0, t0_detected=True, bean_ror_c_per_min=-20.0)
    harness.reader.readings = [t0]
    for _ in range(3):
        await harness.controller.tick()
        harness.clock.advance(1.0)
    harness.events.events.clear()
    # A still-crashing above-threshold sample: turning point not yet armed → no fire.
    harness.reader.readings = [reading(bean=152.0, bean_ror_c_per_min=-5.0)]
    await harness.controller.tick()
    assert _drying_end_payloads(harness) == []


@pytest.mark.asyncio
async def test_drying_end_is_not_recorded_as_an_advisor_milestone() -> None:
    """#351 invariant: the drying-end signal is observability-only — it is emitted as
    an event but NEVER recorded as a ``RoastMilestone``, so it never enters the
    advisor context (``AdvisorContext.roast_milestones``). The advisor/safety path is
    untouched by it."""
    harness = make_harness()
    await _charge_into_pre_fc_below_drying_end(harness)
    harness.reader.readings = [reading(bean=151.0, bean_ror_c_per_min=15.0)]
    await harness.controller.tick()
    # The event fired…
    assert _drying_end_payloads(harness)
    # …but no DRYING_END milestone was recorded (it stays out of the advisor curve
    # summary). Reach into the in-memory history the advisor context reads.
    history = harness.controller._history  # pyright: ignore[reportPrivateUsage]
    assert not history.has_milestone(RoastMilestoneKind.DRYING_END)
    assert RoastMilestoneKind.DRYING_END not in {m.kind for m in history.milestones()}


@pytest.mark.asyncio
async def test_drying_end_does_not_fire_after_first_crack() -> None:
    """#351: the signal is pre-FC only. Once the run is in development (post-FC) the
    drying-end check is not reached, so a bean above threshold there emits nothing."""
    harness = make_harness()
    await _charge_into_pre_fc_below_drying_end(harness)
    # Cross FC into development (the drying-end window closes at FC).
    harness.reader.readings = [reading(bean=178.0, first_crack_detected=True)]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    harness.events.events.clear()
    # A post-FC above-threshold reading: no drying-end event.
    harness.clock.advance(1.0)
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=10.0)]
    await harness.controller.tick()
    assert _drying_end_payloads(harness) == []


@pytest.mark.asyncio
async def test_drying_end_re_arms_on_a_new_run() -> None:
    """#351: the one-way latch resets on a new run/preheat, so the next roast emits
    its own drying-end signal rather than staying latched from the prior run."""
    harness = make_harness()
    await _charge_into_pre_fc_below_drying_end(harness)
    harness.reader.readings = [reading(bean=151.0, bean_ror_c_per_min=15.0)]
    await harness.controller.tick()
    assert len(_drying_end_payloads(harness)) == 1
    # A new run/preheat re-arms the latch (enter PREHEATING clears _drying_end_emitted).
    harness.controller.transition_to(RoastPhase.COOLING)
    harness.controller.transition_to(RoastPhase.COMPLETE)
    harness.controller.transition_to(RoastPhase.IDLE)
    harness.events.events.clear()
    await _charge_into_pre_fc_below_drying_end(harness)
    harness.reader.readings = [reading(bean=151.0, bean_ror_c_per_min=15.0)]
    await harness.controller.tick()
    assert len(_drying_end_payloads(harness)) == 1  # the new run's own signal


# --- #409: post-charge bean-temp minimum — turning-point SSE event (observability) ---


def _turning_point_payloads(harness: Harness) -> list[dict[str, object]]:
    """Every TURNING_POINT event payload emitted on ``harness``, in order."""
    return [
        cast(dict[str, object], p)
        for k, p in harness.events.events
        if k is RoastEventKind.TURNING_POINT
    ]


@pytest.mark.asyncio
async def test_turning_point_emits_once_on_ror_zero_cross() -> None:
    """#409: the TURNING_POINT event fires exactly once, the tick the bean RoR first
    turns non-negative after the post-charge crash (the curve minimum), carrying
    bean_temp_c + elapsed_since_charge_seconds. A one-way latch: subsequent ticks
    do NOT re-emit it."""
    harness = make_harness()
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    # Charge into pre-FC with a FALLING bean (no turning point yet).
    t0 = reading(bean=165.0, t0_detected=True, bean_ror_c_per_min=-15.0)
    harness.reader.readings = [t0]
    for _ in range(3):
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    harness.events.events.clear()
    # Still falling: no turning-point event.
    harness.reader.readings = [reading(bean=140.0, bean_ror_c_per_min=-5.0)]
    await harness.controller.tick()
    assert _turning_point_payloads(harness) == []
    # RoR first crosses zero (the minimum): one event with the real payload.
    harness.clock.advance(1.0)
    harness.reader.readings = [reading(bean=141.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()
    payloads = _turning_point_payloads(harness)
    assert len(payloads) == 1
    assert payloads[0]["bean_temp_c"] == 141.0
    assert isinstance(payloads[0]["elapsed_since_charge_seconds"], float)
    # Latched: a subsequent above-zero-RoR tick emits no second event.
    harness.clock.advance(1.0)
    harness.reader.readings = [reading(bean=144.0, bean_ror_c_per_min=8.0)]
    await harness.controller.tick()
    assert _turning_point_payloads(harness) == payloads  # unchanged — exactly one


@pytest.mark.asyncio
async def test_turning_point_is_observability_only_not_an_advisor_milestone() -> None:
    """#409 invariant: the turning-point SSE event is observability-only — the
    TURNING_POINT *milestone* (already tracked internally for the noise-robust
    drying-end gate) is unchanged; this test confirms the SSE event fires without
    adding a SECOND milestone entry or disturbing the advisor path."""
    harness = make_harness()
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:
        harness.controller.transition_to(step)
    t0 = reading(bean=165.0, t0_detected=True, bean_ror_c_per_min=-10.0)
    harness.reader.readings = [t0]
    for _ in range(3):
        await harness.controller.tick()
        harness.clock.advance(1.0)
    harness.events.events.clear()
    # Cross the turning point.
    harness.clock.advance(1.0)
    harness.reader.readings = [reading(bean=142.0, bean_ror_c_per_min=0.5)]
    await harness.controller.tick()
    assert _turning_point_payloads(harness), "expected a TURNING_POINT event"
    # Exactly one TURNING_POINT milestone is recorded — the same one that was always
    # tracked for the drying-end noise-robust gate.
    history = harness.controller._history  # pyright: ignore[reportPrivateUsage]
    assert history.has_milestone(RoastMilestoneKind.TURNING_POINT)
    tp_milestones = [m for m in history.milestones() if m.kind is RoastMilestoneKind.TURNING_POINT]
    assert len(tp_milestones) == 1, "must record the milestone exactly once"


@pytest.mark.asyncio
async def test_turning_point_does_not_fire_after_first_crack() -> None:
    """#409: the turning-point window is pre-FC only. Once the run is in development
    (post-FC) the RoR history check is bypassed; a rising-RoR reading there emits
    nothing (the branch exits before reaching the check)."""
    harness = make_harness()
    await _charge_into_pre_fc_below_drying_end(harness)
    # Cross FC into development (the turning-point window closes at FC).
    harness.reader.readings = [reading(bean=178.0, first_crack_detected=True)]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    harness.events.events.clear()
    # Post-FC reading with positive RoR: no turning-point event.
    harness.clock.advance(1.0)
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=10.0)]
    await harness.controller.tick()
    assert _turning_point_payloads(harness) == []


@pytest.mark.asyncio
async def test_turning_point_requires_prior_negative_ror() -> None:
    """#409 P2-3: a first post-charge sample with RoR already ≥ 0 (no dip observed)
    must NOT fire the turning_point event — emitting on a first-sample-already-≥0
    tick would be a false landmark with no actual bean-temp minimum in the data.

    Two scenarios:
      A) First post-charge sample has RoR ≥ 0 with no prior negative → NO event.
      B) A prior negative sample followed by ≥ 0 → event fires exactly once (the
         real dip, already covered by test_turning_point_emits_once_on_ror_zero_cross;
         repeated here for contrast and to assert the gate does not over-suppress).
    """
    # Scenario A — no prior negative: straight-to-positive RoR after charge.
    harness = make_harness()
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:
        harness.controller.transition_to(step)
    # Charge into pre-FC with RoR already non-negative on the first sample.
    t0 = reading(bean=165.0, t0_detected=True, bean_ror_c_per_min=1.0)
    harness.reader.readings = [t0]
    for _ in range(3):
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    harness.events.events.clear()
    # Positive RoR on every subsequent tick — still no turning-point (no dip ever).
    for _ in range(3):
        harness.clock.advance(1.0)
        harness.reader.readings = [reading(bean=167.0, bean_ror_c_per_min=2.0)]
        await harness.controller.tick()
    assert _turning_point_payloads(harness) == [], (
        "no turning_point event when first post-charge sample is already ≥ 0 (no dip)"
    )

    # Scenario B — witness gate does NOT over-suppress a real dip.
    harness2 = make_harness()
    harness2.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:
        harness2.controller.transition_to(step)
    t0b = reading(bean=165.0, t0_detected=True, bean_ror_c_per_min=-10.0)
    harness2.reader.readings = [t0b]
    for _ in range(3):
        await harness2.controller.tick()
        harness2.clock.advance(1.0)
    harness2.events.events.clear()
    harness2.reader.readings = [reading(bean=140.0, bean_ror_c_per_min=-3.0)]
    await harness2.controller.tick()  # negative — witnesses the dip
    harness2.clock.advance(1.0)
    harness2.reader.readings = [reading(bean=142.0, bean_ror_c_per_min=2.0)]
    await harness2.controller.tick()  # cross: must fire exactly once
    payloads = _turning_point_payloads(harness2)
    assert len(payloads) == 1, "turning_point must fire on a real negative→≥0 cross"
    assert payloads[0]["bean_temp_c"] == 142.0


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
async def test_trim_latch_holds_through_eta_bounce_no_flip_flop() -> None:
    """#327 hysteresis: once the window opens, a momentary FC-ETA bounce back above
    the window does NOT snap heat back to 100 — the latch holds the trim at 65 and
    NO extra set_targets fires (no 100↔65 thrash). This is the controller-level
    proof of the fix for the replay event-stream churn."""
    harness = make_harness()
    await _charge_into_pre_fc(harness)
    # Warm into the window to ENGAGE + latch the trim (bean ≥155, FC-ETA ≤60 s).
    bean = 162.0
    for _ in range(6):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=30.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.5
    assert harness.controller.snapshot().current_heat == 65  # engaged
    writes_after_engage = len(harness.executor.targets)
    # Now FLATTEN the bean: the FC-ETA goes unknown (the boundary "bounce"). Pre-latch
    # this snapped heat back to 100; with the latch it stays trimmed and re-writes
    # nothing.
    for _ in range(5):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=0.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.snapshot().current_heat == 65  # held — no snap-back
    assert len(harness.executor.targets) == writes_after_engage  # no extra writes


@pytest.mark.asyncio
async def test_trim_latch_resets_on_new_run() -> None:
    """#327: the trim latch is per-run — a fresh run/preheat clears it, so the next
    roast re-arms from the flat floor and only re-engages on its OWN clean FC-ETA
    (never inherits the prior roast's latch)."""
    harness = make_harness()
    await _charge_into_pre_fc(harness)
    bean = 162.0
    for _ in range(6):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=30.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.5
    assert harness.controller._trim_latched is True  # pyright: ignore[reportPrivateUsage]
    # A new run/preheat clears the latch (same reset block as the other per-run
    # state). Reach PREHEATING via the legal recovery edge (the "back before charge"
    # resume): pre-FC →(universal) operator_recovery_required → preheating.
    harness.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    assert harness.controller._trim_latched is False  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_trim_latch_clears_on_recovery_resume_straight_into_pre_fc() -> None:
    """#327 (safety-reviewer low): the latch also clears on the same-process
    ``operator_recovery_required -> roasting_pre_first_crack`` resume that BYPASSES
    preheating. Without the entry reset a fault/recovery mid-pre-FC then resume would
    carry a STALE latch and trim a now-cooler bean (below the 155 °C floor) where a
    fresh window never opens — weakening the §8.4 FC-arrival floor. The latch must be
    clear after the resume, and a cold-bean tick must hold the flat 100 floor."""
    harness = make_harness()
    await _charge_into_pre_fc(harness)
    # Engage + latch the trim in the late-Maillard window.
    bean = 162.0
    for _ in range(6):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=30.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.5
    assert harness.controller._trim_latched is True  # pyright: ignore[reportPrivateUsage]
    # Fault/recovery mid-pre-FC, then the operator resumes STRAIGHT back into pre-FC
    # (the legal recovery→pre-FC edge that bypasses preheating).
    harness.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    harness.controller.operator_resume(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # The latch is clear (reset on entry to pre-FC), NOT inherited from before the fault.
    assert harness.controller._trim_latched is False  # pyright: ignore[reportPrivateUsage]
    # A now-COLD bean (140 °C, below the 155 °C floor) must NOT trim — a fresh window
    # can't open below the floor, and the stale latch is gone, so heat holds at 100.
    harness.reader.readings = [reading(bean=140.0, bean_ror_c_per_min=30.0)]
    await harness.controller.tick()
    assert harness.controller.snapshot().current_heat == 100  # flat floor — no stale trim
    assert harness.controller._trim_latched is False  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Adaptive-depth damping (#412)
# ---------------------------------------------------------------------------


def test_damp_trim_depth_unit_deadband_and_slew() -> None:
    """``_damp_trim_depth`` is a PURE function: applies slew + deadband (#412).

    The function reads ``_trim_depth_applied`` but does NOT mutate it; the
    caller advances state only after an accepted write.  This test simulates
    the caller's role by manually setting ``_trim_depth_applied`` to the
    returned value (mimicking a successful ALLOW write).

    Exercises every branch:
    - First call (prev=None): commits unconditionally.
    - Within-deadband hold (no write → prev unchanged).
    - Slew-DOWN: large negative step capped to prev-slew; commit each tick.
    - Slew-UP: large positive jump capped to prev+slew.
    - Residual within deadband after final slew step: held.
    - Rejected-tick model: calling without advancing state keeps the same anchor.
    """
    trim = LateMaillardTrim(
        adaptive_depth_enabled=True,
        trim_depth_deadband_pp=2,
        trim_depth_slew_pp_per_tick=3,
    )
    harness = make_harness()
    ctrl = harness.controller
    damp = ctrl._damp_trim_depth  # pyright: ignore[reportPrivateUsage]

    # Helper: simulate "caller accepted the write at depth d".
    def accept(d: int) -> None:
        ctrl._trim_depth_applied = d  # pyright: ignore[reportPrivateUsage]

    # First call — prev=None → unconditional commit (returns raw).
    assert damp(65, trim) == 65
    assert ctrl._trim_depth_applied is None  # pyright: ignore[reportPrivateUsage]  # pure: state NOT mutated by damp
    accept(65)  # caller commits after ALLOW

    # Within deadband: target 66 → slew min(66,65+3)=66; |66-65|=1 ≤ 2 → HOLD.
    assert damp(66, trim) == 65  # deadband holds; no write
    # Caller does NOT advance state on a hold (simulating REJECT / no-new-value).
    assert ctrl._trim_depth_applied == 65  # pyright: ignore[reportPrivateUsage]

    # Slew-DOWN, 10 pp (65→55): slew caps to 65-3=62; |62-65|=3 > deadband → commit.
    assert damp(55, trim) == 62
    accept(62)
    # Next: 62-3=59; |59-62|=3 > 2 → commit.
    assert damp(55, trim) == 59
    accept(59)
    # Next: 59-3=56; |56-59|=3 > 2 → commit.
    assert damp(55, trim) == 56
    accept(56)
    # Final step: max(55, 56-3)=55; |55-56|=1 ≤ 2 → deadband holds at 56.
    assert damp(55, trim) == 56  # held, no accept

    # REJECTED-tick model: calling damp again with the SAME prev=56 (no accept
    # in between) returns the same candidate → rejected tick consumes no budget.
    assert damp(55, trim) == 56  # still holds; prev still 56
    assert ctrl._trim_depth_applied == 56  # pyright: ignore[reportPrivateUsage]

    # Slew-UP: 56→75 → min(75, 56+3)=59; |59-56|=3 > 2 → commit.
    assert damp(75, trim) == 59
    accept(59)
    # Another slew-UP: 59→62; |62-59|=3 > 2 → commit.
    assert damp(75, trim) == 62
    accept(62)
    # Within-deadband slew-UP: min(63,62+3)=63; |63-62|=1 ≤ 2 → hold.
    assert damp(63, trim) == 62  # held
    assert ctrl._trim_depth_applied == 62  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_adaptive_depth_damping_bounds_jittery_ror_series() -> None:
    """Replaying a jittery RoR series (simulating roast-7 thrash) with adaptive
    depth ON produces ACTUATED commands whose consecutive values never differ by
    more than ``trim_depth_slew_pp_per_tick`` pp (#412).

    Asserts on ``executor.targets`` — the actual MCP set_targets calls — NOT on
    ``current_heat`` (which reflects the idempotent-skip path).  The rate-limit
    means not every tick produces a write; the bound applies to consecutive
    ACCEPTED writes (across any number of REJECT ticks in between).

    The raw ``depth_for`` output for RoR alternating 8 ↔ 16 °C/min at k_ror=1.5
    swings 65 ↔ 53 pp (12 pp per tick) — the roast-7 symptom.  The damped
    command stream must stay within the 3 pp/accepted-write slew cap.
    """
    trim = LateMaillardTrim(
        adaptive_depth_enabled=True,
        base_trim=65,
        k_ror=1.5,
        ror_ref=8.0,
        k_eta=0.0,  # ETA term off so RoR jitter is the only driver
        min_trim=45,
        max_trim=75,
        trim_depth_deadband_pp=2,
        trim_depth_slew_pp_per_tick=3,
    )
    config = ControllerConfig(pre_first_crack_levers=PreFirstCrackLevers(late_maillard_trim=trim))
    harness = make_harness(config=config)
    await _charge_into_pre_fc(harness)

    # Jittery RoR alternating 8 and 16 °C/min:
    #   ror=8:  depth_for → 65 - 1.5*(8-8)  = 65
    #   ror=16: depth_for → 65 - 1.5*(16-8) = 53
    # That is a 12 pp swing every tick — the roast-7 thrash.
    ror_jitter = [8.0, 16.0] * 10  # 20 ticks of alternating RoR
    bean = 162.0
    targets_before = len(harness.executor.targets)

    for ror in ror_jitter:
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=ror)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.3

    # Collect only trim-engaged writes (heat < 95 %, i.e. below the flat floor).
    trim_commands = [h for (h, _f) in harness.executor.targets[targets_before:] if h < 95]

    # Guard: the trim must have actually engaged and produced accepted writes.
    assert len(trim_commands) >= 2, (
        f"fewer than 2 trim-engaged writes produced — trim may not have engaged "
        f"or all writes were rate-limited; "
        f"targets: {harness.executor.targets[targets_before:]}"
    )

    # The slew bound applies to consecutive ACCEPTED writes.
    for i in range(1, len(trim_commands)):
        prev_cmd, this_cmd = trim_commands[i - 1], trim_commands[i]
        delta = abs(this_cmd - prev_cmd)
        assert delta <= trim.trim_depth_slew_pp_per_tick, (
            f"consecutive ACTUATED commands differ by {delta} pp "
            f"(exceeds slew cap {trim.trim_depth_slew_pp_per_tick} pp): "
            f"{prev_cmd} -> {this_cmd}; full trim command stream: {trim_commands}"
        )


@pytest.mark.asyncio
async def test_rate_limited_reject_does_not_advance_slew_budget() -> None:
    """Rate-limited REJECT ticks must not consume slew budget (#412 Fix 2).

    With ``min_seconds_between_commands=2.0`` and 1-s ticks, every other trim
    tick is rejected.  Between two consecutive ACCEPTED writes the depth change
    must still be ≤ ``trim_depth_slew_pp_per_tick`` — a rejected intermediate
    tick must not shift the anchor and double the effective step.

    Setup: use RoR=30 (same warm-up as other trim tests) for 6 ticks to bring
    the trim into the late-Maillard window and establish the first unconditional
    write, then switch to alternating 8/16 so the raw depth keeps changing and
    we get multiple accepted writes to assert on.  The slew bound must hold on
    each pair of consecutive ACCEPTED writes regardless of how many REJECT ticks
    separate them.
    """
    trim = LateMaillardTrim(
        adaptive_depth_enabled=True,
        base_trim=65,
        k_ror=1.5,
        ror_ref=8.0,
        k_eta=0.0,
        min_trim=45,
        max_trim=75,
        trim_depth_deadband_pp=2,
        trim_depth_slew_pp_per_tick=3,
    )
    # Default SafetyLimits has min_seconds_between_commands=2.0; 1-s ticks
    # mean some ticks are rate-limited (REJECT).
    config = ControllerConfig(pre_first_crack_levers=PreFirstCrackLevers(late_maillard_trim=trim))
    harness = make_harness(config=config)
    await _charge_into_pre_fc(harness)

    # Warm-up phase: 6 ticks at RoR=30 to bring the trim into the window (same
    # as test_trim_engages_in_late_maillard_window_through_safety_path).
    # This establishes the first unconditional commit.
    bean = 162.0
    for _ in range(6):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=30.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.5

    targets_before = len(harness.executor.targets)

    # Now drive alternating ror=8/16 for 20 ticks to produce raw-depth swings.
    # With min_seconds_between_commands=2.0 and 1-s ticks, some ticks are
    # rate-limited (REJECT).  The slew bound must hold on all accepted writes.
    for ror in [8.0, 16.0] * 10:
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=ror)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.3

    trim_commands = [h for (h, _f) in harness.executor.targets[targets_before:] if h < 95]
    assert len(trim_commands) >= 2, (
        f"not enough trim commands to test rate-limit interaction; got: {trim_commands}; "
        f"all targets post-warmup: {harness.executor.targets[targets_before:]}"
    )

    # Between any two consecutive ACCEPTED trim writes, the jump must be ≤ slew cap —
    # even though there may be rejected ticks in between.
    for i in range(1, len(trim_commands)):
        prev_cmd, this_cmd = trim_commands[i - 1], trim_commands[i]
        delta = abs(this_cmd - prev_cmd)
        assert delta <= trim.trim_depth_slew_pp_per_tick, (
            f"accepted-write delta {delta} pp exceeds slew cap "
            f"{trim.trim_depth_slew_pp_per_tick} pp (rejected ticks must not "
            f"consume slew budget): {prev_cmd} -> {this_cmd}; "
            f"full trim stream: {trim_commands}"
        )


@pytest.mark.asyncio
async def test_adaptive_depth_off_resolved_trim_equals_trim_heat_percent_exactly() -> None:
    """When ``adaptive_depth_enabled=False`` (the default), the trim resolves to
    exactly ``trim_heat_percent`` — byte-for-byte the proven roast-6 behaviour.
    No damping state is involved; the non-adaptive path is completely unaffected.
    """
    trim = LateMaillardTrim(
        adaptive_depth_enabled=False,
        trim_heat_percent=65,
        # Deliberately non-default damping coefficients — must be ignored.
        # (deadband=1, slew=2 satisfies the deadband < slew invariant.)
        trim_depth_deadband_pp=1,
        trim_depth_slew_pp_per_tick=2,
    )
    config = ControllerConfig(pre_first_crack_levers=PreFirstCrackLevers(late_maillard_trim=trim))
    harness = make_harness(config=config)
    await _charge_into_pre_fc(harness)

    # Drive into the late-Maillard window with a warming slope.
    bean = 162.0
    for _ in range(6):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=30.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.5

    # Trim engaged (latch set), adaptive OFF → heat must equal exactly trim_heat_percent.
    assert harness.controller._trim_latched is True  # pyright: ignore[reportPrivateUsage]
    assert harness.controller.snapshot().current_heat == trim.trim_heat_percent
    # Damping state is None — the non-adaptive path never touches it.
    assert harness.controller._trim_depth_applied is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_damp_trim_depth_state_resets_on_pre_fc_entry() -> None:
    """The damping state resets when the controller re-enters pre-FC (#412).

    After a fault/recovery resume into pre-FC, ``_trim_depth_applied`` must be
    ``None`` so the first adaptive tick commits unconditionally (no stale anchor
    from the prior pre-FC entry biasing the deadband).
    """
    trim = LateMaillardTrim(
        adaptive_depth_enabled=True,
        trim_depth_deadband_pp=2,
        trim_depth_slew_pp_per_tick=3,
    )
    config = ControllerConfig(pre_first_crack_levers=PreFirstCrackLevers(late_maillard_trim=trim))
    harness = make_harness(config=config)
    await _charge_into_pre_fc(harness)

    # Engage the trim + accumulate damping state.
    bean = 162.0
    for _ in range(6):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=30.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.5

    assert harness.controller._trim_latched is True  # pyright: ignore[reportPrivateUsage]

    # Fault, then recovery → pre-FC resume.
    harness.controller.transition_to(RoastPhase.OPERATOR_RECOVERY_REQUIRED)
    harness.controller.operator_resume(RoastPhase.ROASTING_PRE_FIRST_CRACK)

    # Both the latch and the damping state are cleared.
    assert harness.controller._trim_latched is False  # pyright: ignore[reportPrivateUsage]
    assert harness.controller._trim_depth_applied is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_damp_trim_depth_state_does_not_advance_on_actuator_failure() -> None:
    """Codex finding (fix round 2, #405 Slice B2): ``_trim_depth_applied`` must
    advance only when the write ACTUALLY REACHED the roaster
    (``_execute_targets``'s return), not merely on an ALLOW/CLAMP verdict. A
    transient actuator failure (``set_targets`` raises) must leave
    ``_trim_depth_applied`` exactly as it was before the failed tick — the
    same principle the post-FC PI loop's fix applies, extended to the sibling
    pre-FC deterministic path.

    Drives a jittery RoR series (mirrors
    ``test_adaptive_depth_damping_bounds_jittery_ror_series``) so consecutive
    ticks resolve genuinely DIFFERENT depth candidates (not a deadband hold),
    with the clock advanced 3 s between ticks to clear the 2 s rate limit —
    otherwise every second write is REJECTed before ``set_targets`` is even
    attempted, which would not exercise the actuator-failure path at all.
    """
    trim = LateMaillardTrim(
        adaptive_depth_enabled=True,
        base_trim=65,
        k_ror=1.5,
        ror_ref=8.0,
        k_eta=0.0,
        min_trim=45,
        max_trim=75,
        trim_depth_deadband_pp=2,
        trim_depth_slew_pp_per_tick=3,
    )
    config = ControllerConfig(pre_first_crack_levers=PreFirstCrackLevers(late_maillard_trim=trim))
    log: list[str] = []
    flaky_executor = _ArmableFlakySetTargetsExecutor(log)
    harness = make_harness(config=config, executor=flaky_executor)
    await _charge_into_pre_fc(harness)

    # Engage the trim + accumulate real damping state via a normal (non-flaky)
    # jittery-RoR sequence first, so there is a genuine prior anchor to
    # protect (and the SAME jitter continues below, so the armed tick resolves
    # a genuinely different candidate, not a deadband hold).
    bean = 162.0
    ror_jitter = [8.0, 16.0] * 3
    for ror in ror_jitter:
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=ror)]
        await harness.controller.tick()
        harness.clock.advance(3.0)  # clear the 2 s rate limit each tick
        bean += 0.5
    assert harness.controller._trim_latched is True  # pyright: ignore[reportPrivateUsage]
    applied_before_failure = harness.controller._trim_depth_applied  # pyright: ignore[reportPrivateUsage]
    assert applied_before_failure is not None

    # Arm exactly one failure, then continue the SAME jitter (the opposite RoR
    # from the last tick, a genuine new candidate) so the method reaches the
    # `set_targets` call and it raises.
    flaky_executor.arm_next_failure()
    next_ror = 8.0 if ror_jitter[-1] == 16.0 else 16.0
    harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=next_ror)]
    await harness.controller.tick()
    assert RoastEventKind.COMMAND_FAILED in harness.events.kinds()
    # The anchor is UNCHANGED — the failed write never advanced it.
    assert harness.controller._trim_depth_applied == applied_before_failure  # pyright: ignore[reportPrivateUsage]

    # The next tick's write succeeds; the resulting depth is computed from the
    # SAME anchor the failed tick should have left in place — proof no
    # phantom advance leaked through.
    harness.clock.advance(3.0)
    harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=next_ror)]
    await harness.controller.tick()
    assert harness.controller._trim_depth_applied is not None  # pyright: ignore[reportPrivateUsage]
    assert harness.controller._trim_depth_applied != applied_before_failure  # pyright: ignore[reportPrivateUsage]


def test_damping_deadband_gte_slew_rejected_at_construction() -> None:
    """``LateMaillardTrim`` must reject ``deadband >= slew`` at construction (#412 Fix 3).

    ``deadband >= slew`` silently disables adaptive movement after the first tick:
    every slew candidate satisfies ``|candidate - prev| <= slew <= deadband`` and
    the deadband-hold fires unconditionally.  The validator catches this before the
    controller ever runs.
    """
    import pytest as _pytest

    # Equal: deadband == slew must be rejected.
    with _pytest.raises(ValueError, match="trim_depth_deadband_pp must be strictly less"):
        LateMaillardTrim(trim_depth_deadband_pp=3, trim_depth_slew_pp_per_tick=3)

    # Greater: deadband > slew must also be rejected.
    with _pytest.raises(ValueError, match="trim_depth_deadband_pp must be strictly less"):
        LateMaillardTrim(trim_depth_deadband_pp=5, trim_depth_slew_pp_per_tick=3)

    # Boundary: deadband == slew-1 is valid (strictly less).
    trim = LateMaillardTrim(trim_depth_deadband_pp=2, trim_depth_slew_pp_per_tick=3)
    assert trim.trim_depth_deadband_pp == 2
    assert trim.trim_depth_slew_pp_per_tick == 3


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
async def test_latch_skips_escalation_read_after_fault_acknowledged() -> None:
    """#332: once the operator acknowledges the fault (``note_fault_acknowledged``),
    the latched tick SKIPS the upward-escalation re-read — the run is finalising and
    heat is already off, so the re-read is pointless and (on a wedged child) is the
    "slow to clear" latency. Crucially the SKIP is gated on the acknowledge, NOT a
    general weakening: a hard-ceiling breach that WOULD have escalated does not, only
    because the operator has acknowledged. The heat-off retry still runs (unchanged).

    Mirror of ``test_latch_auto_escalates_fault_to_emergency_stop_once`` but with an
    acknowledge before the breach tick: the e-stop must NOT fire."""
    stale_low = reading(bean=180.0, env=200.0, age_seconds=10.0)  # stale → FAULT
    over_ceiling = reading(bean=235.0, env=200.0, age_seconds=0.0)  # > 230 → would e-stop
    harness = harness_in_development(readings=[stale_low, over_ceiling])

    await harness.controller.tick()  # entry: stale FAULT → FAULTED (latched FAULT)
    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.executor.estop_reasons == []
    # The operator acknowledges (the runner calls this on the ack drain).
    harness.controller.note_fault_acknowledged()
    # A breach tick that WOULD escalate (see the auto-escalate test) now does NOT —
    # the acknowledge gates the escalation re-read off.
    for _ in range(10):
        await harness.controller.tick()
    assert harness.executor.estop_reasons == []  # escalation skipped post-acknowledge
    assert harness.controller.phase is RoastPhase.FAULTED  # controller stays faulted
    # New-run reset re-arms the flag so the next roast escalates normally.
    harness.controller.transition_to(RoastPhase.IDLE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    assert harness.controller._fault_acknowledged is False  # pyright: ignore[reportPrivateUsage]


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
        async def emergency_stop(self, *, reason: str) -> AppliedRoasterState:
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
        async def emergency_stop(self, *, reason: str) -> AppliedRoasterState:
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


# --- #337: honour MCP's backdated T0/FC event timestamp ---


async def _drive_to_pre_fc(harness: Harness, t0: RoastTelemetry) -> None:
    """Debounce three T0 ticks into pre-first-crack; advance 1 s per tick."""
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    for _ in range(harness.controller._config.t0_debounce_ticks):  # pyright: ignore[reportPrivateUsage]
        harness.reader.readings = [t0]
        await harness.controller.tick()
        harness.clock.advance(1.0)


@pytest.mark.asyncio
async def test_t0_backdate_anchors_charge_clock_earlier_and_moves_dtr() -> None:
    """#337: a T0 ``t0_backdate_seconds`` delta anchors the charge clock at the
    backdated turning point, so the charge-referenced roast clock (the DTR
    denominator) reads LARGER than the receive-tick baseline by the delta.

    Two harnesses driven identically except for the backdate delta: the backdated
    one's ``_charge_elapsed_seconds`` is exactly the delta greater, and that lifts
    the DTR (a larger denominator at equal development is a *smaller* dev%, so the
    assertion checks the charge clock and dev% actually move with the backdating —
    not merely that the field is plumbed)."""
    backdate = 17.0
    plain_t0 = reading(bean=160.0, t0_detected=True)
    backdated_t0 = reading(bean=160.0, t0_detected=True, t0_backdate_seconds=backdate)

    plain = make_harness()
    await _drive_to_pre_fc(plain, plain_t0)
    assert plain.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK

    shifted = make_harness()
    await _drive_to_pre_fc(shifted, backdated_t0)
    assert shifted.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK

    # Both clocks are at the same value (driven identically); the only difference
    # is the backdated charge anchor, so the elapsed-since-charge differs by the
    # delta exactly.
    plain_elapsed = plain.controller._charge_elapsed_seconds()  # pyright: ignore[reportPrivateUsage]
    shifted_elapsed = shifted.controller._charge_elapsed_seconds()  # pyright: ignore[reportPrivateUsage]
    assert shifted_elapsed == pytest.approx(plain_elapsed + backdate)

    # The backdated charge instant is in the PAST, never the future: elapsed > 0.
    assert shifted_elapsed > 0.0
    # And the snapshot's operator-facing roast clock reflects the shift too.
    assert shifted.controller.snapshot().charge_elapsed_seconds == pytest.approx(shifted_elapsed)


@pytest.mark.asyncio
async def test_t0_backdate_absent_anchors_at_first_detect() -> None:
    """#174: no ``t0_backdate_seconds`` (a manual mark / pre-0.1.7 payload) still
    origins the charge clock on the FIRST detect tick of the debounce streak, not
    the later debounced transition. There is no MCP backdate to apply, but the
    first-detect anchor removes the agent's own debounce lag (here the streak
    started at clock=0.0 and confirmed on the 3rd tick at clock=2.0)."""
    harness = make_harness()
    t0 = reading(bean=160.0, t0_detected=True)  # no backdate field
    await _drive_to_pre_fc(harness, t0)
    # Charge origins on the first detect (clock=0.0), NOT the debounced transition
    # at clock=2.0 — the debounce confirms T0 but must not define the charge moment.
    charge_monotonic = harness.controller._charge_monotonic  # pyright: ignore[reportPrivateUsage]
    assert charge_monotonic == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_t0_backdate_latched_when_it_arrives_mid_streak() -> None:
    """#174: the MCP turning-point backdate can arrive a tick AFTER the first
    detect (the ``beans_added`` event racing into ``state.events`` late — the
    roast-4 root cause). It is latched the first tick it appears and applied at the
    transition even though the first-detect tick carried no delta."""
    backdate = 6.0
    harness = make_harness()
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    harness.clock.advance(100.0)  # first detect lands at a positive instant
    first_detect = 100.0
    # First detect: backdate NOT yet present.
    harness.reader.readings = [reading(bean=160.0, t0_detected=True)]
    await harness.controller.tick()  # streak 1, first_detect=100.0, no latch yet
    harness.clock.advance(1.0)
    # The backdate races in on the 2nd streak tick.
    harness.reader.readings = [reading(bean=158.0, t0_detected=True, t0_backdate_seconds=backdate)]
    await harness.controller.tick()  # streak 2 → latch the delta
    harness.clock.advance(1.0)
    harness.reader.readings = [reading(bean=156.0, t0_detected=True)]
    await harness.controller.tick()  # streak 3 → transition
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # Charge origins at first_detect − the LATCHED backdate, despite the delta being
    # absent on the first-detect tick (100 − 6 = 94) — not the transition tick.
    charge = harness.controller._charge_monotonic  # pyright: ignore[reportPrivateUsage]
    assert charge == pytest.approx(first_detect - backdate)


@pytest.mark.asyncio
async def test_t0_lands_at_turning_point_on_realistic_charge_crash() -> None:
    """#387: a dry-run replay of the roast-6 charge crash proves T0 stamps at the
    bean-temp turning point, NOT ~11 s late.

    Reproduces the roast-6 shape (trace ``~/roasts/roastpilot.sqlite3`` run
    ``d251013e…``, ``auto_t0_drop_threshold_c=25``): the bean peaks at 174 °C (the
    turning point) at MCP-elapsed 351.759, then crashes on the charge; the MCP
    confirms auto-T0 8 s later (at 359.758) when the bean has dropped 25 °C, and
    reports a turning-point backdate of ``confirmed − turning_point = 8.0`` s.

    The agent receives ``t0_detected`` at ≈ the MCP confirmation instant, so its
    first-detect anchor equals that confirmation tick; subtracting the MCP backdate
    (confirmation − turning point) lands the charge origin EXACTLY on the turning
    point — the agent applies the FULL delta and does not double-count its own
    debounce anchor against the MCP backdate. That is why roast-6's stamped T0 sat
    ~2 s (sampling granularity) from the 174 °C peak, NOT 11 s late.
    """
    harness = make_harness()
    debounce = harness.controller._config.t0_debounce_ticks  # pyright: ignore[reportPrivateUsage]
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)

    # Climb to the 174 °C peak; the LAST rising sample is the turning point.
    rising = [150.0, 157.0, 163.0, 167.0, 170.0, 172.0, 174.0]
    for bean in rising:
        harness.reader.readings = [reading(bean=bean, t0_detected=False)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
    turning_point_monotonic = harness.clock.now - 1.0  # the 174 °C tick instant

    # The charge crash. The MCP only confirms once the bean has dropped past the
    # threshold, so the agent first sees ``t0_detected`` on the confirmation tick;
    # the agent debounce then needs ``debounce`` consecutive detected ticks. Every
    # detected tick carries the SAME backdate the MCP computed at confirmation:
    # ``mcp_confirmation − turning_point``. The first detected tick lands at the
    # current clock, so that backdate is exactly ``now − turning_point``.
    mcp_backdate = harness.clock.now - turning_point_monotonic
    bean = 166.0
    for _ in range(debounce):
        harness.reader.readings = [
            reading(bean=bean, t0_detected=True, t0_backdate_seconds=mcp_backdate)
        ]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean -= 6.0

    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # The charge clock origins on the 174 °C turning point — the agent's debounce
    # anchor (first detect) minus the MCP backdate composes to the peak instant,
    # never the later confirmation/transition tick.
    charge = harness.controller._charge_monotonic  # pyright: ignore[reportPrivateUsage]
    assert charge == pytest.approx(turning_point_monotonic)


@pytest.mark.asyncio
async def test_t0_charge_clock_re_anchors_after_a_broken_streak() -> None:
    """#174: a broken debounce streak discards the stale candidate charge instant;
    the charge clock origins on the POST-break first detect, never the pre-break
    one (no cross-streak anchor bleed)."""
    harness = make_harness()
    harness.controller.load_profile(PROFILE)
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    harness.clock.advance(50.0)
    # A first (stale) streak that breaks before it can confirm.
    harness.reader.readings = [reading(bean=160.0, t0_detected=True)]
    await harness.controller.tick()  # stale first_detect=50.0
    harness.clock.advance(1.0)
    harness.reader.readings = [reading(bean=160.0, t0_detected=False)]
    await harness.controller.tick()  # break → reset, stale first_detect cleared
    harness.clock.advance(1.0)
    post_break_first_detect = 52.0
    for _ in range(harness.controller._config.t0_debounce_ticks):  # pyright: ignore[reportPrivateUsage]
        harness.reader.readings = [reading(bean=158.0, t0_detected=True)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # Anchored to the POST-break first detect (52.0), NOT the stale 50.0.
    charge = harness.controller._charge_monotonic  # pyright: ignore[reportPrivateUsage]
    assert charge == pytest.approx(post_break_first_detect)


@pytest.mark.asyncio
async def test_fc_backdate_anchors_development_clock_earlier_and_moves_dtr() -> None:
    """#337: an FC ``first_crack_backdate_seconds`` delta anchors the development
    clock at the crack ONSET, so the development-elapsed (and the dev% it drives)
    read LARGER by the delta than the receive-tick baseline. Asserts the dev%
    actually moves, not just that the field is set."""
    backdate = 15.0
    fc_plain = reading(bean=185.0, first_crack_detected=True)
    fc_backdated = reading(
        bean=185.0, first_crack_detected=True, first_crack_backdate_seconds=backdate
    )

    async def run(fc: RoastTelemetry) -> RoastController:
        harness = make_harness()
        await _drive_to_pre_fc(harness, reading(bean=160.0, t0_detected=True))
        harness.reader.readings = [fc]
        await harness.controller.tick()  # FC edge → DEVELOPMENT
        assert harness.controller.phase is RoastPhase.DEVELOPMENT
        harness.clock.advance(30.0)  # 30 s of development
        return harness.controller

    plain = await run(fc_plain)
    shifted = await run(fc_backdated)

    plain_elapsed = plain._development_elapsed_seconds()  # pyright: ignore[reportPrivateUsage]
    shifted_elapsed = shifted._development_elapsed_seconds()  # pyright: ignore[reportPrivateUsage]
    assert plain_elapsed is not None and shifted_elapsed is not None
    # The crack-onset anchor is `backdate` seconds earlier ⇒ that much more
    # development elapsed at the same clock.
    assert shifted_elapsed == pytest.approx(plain_elapsed + backdate)
    # Dev% (DTR) rises with the larger development numerator.
    plain_pct = plain.snapshot().development_percent
    shifted_pct = shifted.snapshot().development_percent
    assert plain_pct is not None and shifted_pct is not None
    assert shifted_pct > plain_pct
    # Never future: the backdated development instant stays in the past.
    assert shifted_elapsed > 0.0


@pytest.mark.asyncio
async def test_fc_backdate_stage_is_consumed_and_does_not_leak() -> None:
    """#337: the staged FC delta is cleared after the development stamp, so it
    never re-applies to a later, unrelated FC-edge transition (e.g. a recovery
    resume into development)."""
    harness = make_harness()
    await _drive_to_pre_fc(harness, reading(bean=160.0, t0_detected=True))
    harness.reader.readings = [
        reading(bean=185.0, first_crack_detected=True, first_crack_backdate_seconds=12.0)
    ]
    await harness.controller.tick()  # FC edge consumes the stage
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    pending = harness.controller._pending_first_crack_backdate  # pyright: ignore[reportPrivateUsage]
    assert pending is None


@pytest.mark.parametrize("bad_delta", [-5.0, float("nan"), float("inf")])
def test_backdated_now_falls_back_for_invalid_delta(bad_delta: float) -> None:
    """#337: a negative / non-finite delta falls back to the receive-tick clock —
    never fabricating a future or garbage-referenced milestone instant."""
    harness = make_harness()
    harness.clock.now = 100.0
    anchored = harness.controller._backdated_now(bad_delta)  # pyright: ignore[reportPrivateUsage]
    assert anchored == pytest.approx(100.0)
    # The contract: the anchored instant is never in the future (<= now).
    assert anchored <= harness.clock.now


def test_backdated_now_subtracts_valid_delta() -> None:
    """#337: a valid non-negative delta backdates the milestone instant in the
    agent's own clock domain (now - delta), keeping it in the past."""
    harness = make_harness()
    harness.clock.now = 100.0
    anchored = harness.controller._backdated_now(17.0)  # pyright: ignore[reportPrivateUsage]
    assert anchored == pytest.approx(83.0)
    assert anchored < harness.clock.now


async def _development_harness_via_fc_backdate(
    *, charge_offset: float, fc_offset: float, dev_seconds: float, fc_backdate: float
) -> Harness:
    """A DEVELOPMENT harness whose FC clock is anchored via the real #337 backdate
    path. Charge at t=0, first crack ``fc_offset`` s later through the genuine
    MCP-detected FC tick (carrying ``fc_backdate``), then ``dev_seconds`` of
    development. ``_development_percent`` then reflects the backdated FC origin."""
    # Several identical drop decisions: the FC-edge tick may run a development
    # consult, so the queue must not run dry before the asserted drop consult.
    advisor = FakeAdvisor([decision(heat=50, fan=60, drop=True) for _ in range(4)])
    harness = make_harness(readings=[reading()], advisor=advisor)
    controller = harness.controller
    controller.load_profile(PROFILE)
    controller.transition_to(RoastPhase.STARTING)
    controller.transition_to(RoastPhase.PREHEATING)
    controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    harness.clock.advance(charge_offset + fc_offset)
    # Genuine MCP-detected FC tick: stages + applies the backdate delta at the
    # development stamp (the real path, not a hand-set clock).
    harness.reader.readings = [
        reading(bean=185.0, first_crack_detected=True, first_crack_backdate_seconds=fc_backdate)
    ]
    await controller.tick()
    assert controller.phase is RoastPhase.DEVELOPMENT
    harness.clock.advance(dev_seconds)
    harness.log.clear()
    harness.events.events.clear()
    harness.sink.evaluations.clear()  # drop the FC-edge tick's setup evaluations
    return harness


@pytest.mark.asyncio
async def test_fc_backdate_moves_drop_coherence_guard_release() -> None:
    """#337 control coupling (the safety-review correction): the backdated FC clock
    flows through ``_development_percent`` into the #313 drop-coherence guard, so
    honouring the backdate releases an advisor drop the receive-tick origin would
    REJECT — a genuine (fail-safe) control effect, NOT display-only.

    Same roast twice. Charge at 0, FC at 300 s, 53 s of development. Without
    backdating dev% = 53/353 ≈ 15.0 % — below the 17 % floor (target 20 − margin
    3) ⇒ the advisor drop is BLOCKED. A 17 s FC backdate moves the FC origin 17 s
    earlier ⇒ development 70 s, dev% = 70/353 ≈ 19.8 % — above the floor ⇒ the SAME
    advisor drop is HONOURED. The guard's release point genuinely moved with the
    backdate."""
    blocked = await _development_harness_via_fc_backdate(
        charge_offset=0.0, fc_offset=300.0, dev_seconds=53.0, fc_backdate=0.0
    )
    blocked.controller.request_advisory()
    await blocked.controller.tick()
    # Receive-tick origin: dev% below the floor ⇒ drop REJECTed, stays DEVELOPMENT.
    assert blocked.controller.phase is RoastPhase.DEVELOPMENT
    assert blocked.executor.commands.count("drop_beans") == 0
    assert [e for e in blocked.sink.evaluations if e.rule == "advisor_drop_coherence"]

    released = await _development_harness_via_fc_backdate(
        charge_offset=0.0, fc_offset=300.0, dev_seconds=53.0, fc_backdate=17.0
    )
    released.controller.request_advisory()
    await released.controller.tick()
    # Backdated FC origin lifts dev% over the floor ⇒ the SAME drop is HONOURED.
    assert released.controller.phase is RoastPhase.COOLING
    assert released.executor.commands.count("drop_beans") == 1
    assert not [e for e in released.sink.evaluations if e.rule == "advisor_drop_coherence"]


@pytest.mark.asyncio
async def test_fc_backdate_keeps_advisor_context_window_overshoot_onset_referenced() -> None:
    """#499 part 2 clock-semantics insurance: the D95 falsification was an
    arithmetic bug (a NEW remaining-dwell-budget computation forgot to
    subtract the ~22 s FC-confirmation gap between crack onset and the
    receive-tick decision). c7's teaching adds no new arithmetic — it
    reasons entirely on the EXISTING ``development_time_ratio`` /
    ``target_development_percent_min``/``_max`` fields, which are already
    onset-referenced via the real #337 backdate path
    (``_first_crack_monotonic`` stamped through ``_backdated_now``). This
    pins that a roast-13-shaped scenario (DTR pushed past the #499 window's
    top edge while the bean is still materially below the drop target) keeps
    reading a LARGER, onset-correct DTR under a genuine backdate — never a
    receive-tick-inflated or silently-unadjusted one — so the context c7
    reasons over cannot itself hide a D95-class clock error."""
    plain = await _development_harness_via_fc_backdate(
        charge_offset=0.0, fc_offset=300.0, dev_seconds=90.0, fc_backdate=0.0
    )
    backdated = await _development_harness_via_fc_backdate(
        charge_offset=0.0, fc_offset=300.0, dev_seconds=90.0, fc_backdate=22.0
    )
    limits = plain.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    plain_ctx = plain.controller._build_advisor_context(  # pyright: ignore[reportPrivateUsage]
        reading(bean=190.0, first_crack_detected=True), limits
    )
    backdated_ctx = backdated.controller._build_advisor_context(  # pyright: ignore[reportPrivateUsage]
        reading(bean=190.0, first_crack_detected=True), limits
    )
    assert plain_ctx.development_time_ratio is not None
    assert backdated_ctx.development_time_ratio is not None
    # Both scenarios put the bean materially below the profile's 205 C target
    # while DTR is already past the #499 window top — the roast-13 shape c7
    # reasons about.
    assert plain_ctx.target_development_percent_max is not None
    assert plain_ctx.development_time_ratio * 100.0 > plain_ctx.target_development_percent_max
    assert plain_ctx.current_bean_temp_c < plain_ctx.target_drop_temp_c - 5.0
    # The backdated onset is 22 s EARLIER, so development (and DTR) reads
    # LARGER by exactly that much more elapsed time — never smaller, never
    # unadjusted (which would silently reproduce the D95 omission in this
    # context, even though c7 adds no arithmetic of its own).
    assert backdated_ctx.development_time_ratio > plain_ctx.development_time_ratio
    # The DTR window itself (target ± drop_dev_margin_percent) is a profile/
    # config-derived constant, NOT clock-dependent — it must be identical
    # across both scenarios; only the ratio being compared against it moves.
    assert backdated_ctx.target_development_percent_min == plain_ctx.target_development_percent_min
    assert backdated_ctx.target_development_percent_max == plain_ctx.target_development_percent_max


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
        async def start_session(
            self,
            *,
            recording_origin: str | None = None,
            recording_roast_num: int | None = None,
        ) -> None:
            raise RuntimeError("mcp down")

    harness = make_harness(readings=[reading()], executor=FailingStartExecutor())
    await harness.controller.start_run(PROFILE)
    assert harness.controller.phase is RoastPhase.FAULTED
    assert harness.executor.targets == []  # no half-started run, no writes


def test_recording_origin_slug_joins_identity_and_dedupes_repeats() -> None:
    profile = RoastProfile(
        name="Roast 5",
        bean_origin="Huila",
        country="Colombia",
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=205.0,
        target_development_percent=20.0,
    )
    # country + origin + name, lowercased and hyphen-slugged.
    assert recording_origin_slug(profile) == "colombia-huila-roast-5"
    # No country: falls back to origin + name.
    assert recording_origin_slug(PROFILE) == "ethiopia-harness"
    # Repeated words across country / bean_origin / name are deduped — the real
    # Colombia seed has country == bean_origin == "Colombia" and a "Colombia ..."
    # name, which without dedup slugs to "colombia-colombia-colombia-...".
    redundant = RoastProfile(
        name="Colombia Excelso Huila (Washed)",
        bean_origin="Colombia",
        country="Colombia",
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=195.0,
        target_development_percent=13.0,
    )
    assert recording_origin_slug(redundant) == "colombia-excelso-huila-washed"


@pytest.mark.asyncio
async def test_start_run_sets_recording_metadata_before_session() -> None:
    """The v0.1.9 recording metadata (#176) is derived from the profile and
    handed to start_session (which fires set_recording_metadata FIRST)."""
    harness = make_harness(readings=[reading()])
    await harness.controller.start_run(PROFILE)
    assert harness.controller.phase is RoastPhase.PREHEATING
    # origin slug derived from the profile; roast_num is the per-process counter.
    assert harness.executor.start_session_metadata == [("ethiopia-harness", 1)]


@pytest.mark.asyncio
async def test_start_run_uses_store_derived_recording_roast_num() -> None:
    """#385: a store-derived per-origin roast number passed to start_run is used
    in the recording metadata, overriding the per-process counter (which on a
    fresh controller would be 1)."""
    harness = make_harness(readings=[reading()])
    await harness.controller.start_run(PROFILE, recording_roast_num=7)
    assert harness.controller.phase is RoastPhase.PREHEATING
    assert harness.executor.start_session_metadata == [("ethiopia-harness", 7)]


@pytest.mark.asyncio
async def test_start_run_falls_back_to_per_process_roast_num_when_none() -> None:
    """#385: with no store-derived number (a direct caller / a count failure), the
    per-process counter still advances and is used — best-effort, never blocking."""
    harness = make_harness(readings=[reading(), reading()])
    await harness.controller.start_run(PROFILE, recording_roast_num=None)
    # Drive back to idle so a second run is legal, then start again.
    harness.controller.transition_to(RoastPhase.FAULTED)
    harness.controller.transition_to(RoastPhase.IDLE)
    await harness.controller.start_run(PROFILE, recording_roast_num=None)
    assert harness.executor.start_session_metadata == [
        ("ethiopia-harness", 1),
        ("ethiopia-harness", 2),
    ]


@pytest.mark.asyncio
async def test_start_run_per_process_counter_syncs_to_store_derived_number() -> None:
    """#385 auggie: when a store-derived number is used, the per-process counter
    is advanced to at least that value so a subsequent fallback (None) cannot produce
    a number lower than an already-used per-origin recording filename."""
    harness = make_harness(readings=[reading(), reading()])
    # First run uses store-derived 5 (5 prior completed roasts of this origin).
    await harness.controller.start_run(PROFILE, recording_roast_num=5)
    # Drive back to idle for a second run.
    harness.controller.transition_to(RoastPhase.FAULTED)
    harness.controller.transition_to(RoastPhase.IDLE)
    # Second run: no store-derived number (simulates a count failure). The
    # per-process counter must be ≥ 5 so it never collides with files 1–5.
    await harness.controller.start_run(PROFILE, recording_roast_num=None)
    # The fallback counter advanced to max(1, 5)=5 after the first run, then +1 → 6.
    assert harness.executor.start_session_metadata == [
        ("ethiopia-harness", 5),
        ("ethiopia-harness", 6),
    ]


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
    # Charge origins on the FIRST detect (clock=300.0, #174 — not the debounced
    # transition at 302.0); the first development consult fires on the FC tick at
    # clock=304.0 → 4.0 s (the ~5 s post-FC dwell, #276, means the very next tick
    # does NOT re-consult), NOT ~304 s from run start (the old #219 bug).
    assert ctx.roast_elapsed_seconds == pytest.approx(4.0)
    assert ctx.roast_elapsed_seconds < 300.0
    # It matches seconds_since_charge (same charge instant, the bake-off convention).
    assert ctx.roast_elapsed_seconds == pytest.approx(ctx.seconds_since_charge)


@pytest.mark.asyncio
async def test_advisor_dtr_is_charge_referenced_post_fc() -> None:
    """The DTR the advisor computes (development_elapsed / roast_elapsed) is now
    charge-referenced end to end (#219), matching the v4-prompt definition the
    bake-off validated. The charge origins on the first detect (#174, 2 s before
    the debounced transition), so 102 s pre-FC → FC → 25 s development gives DTR =
    25 / 127, charge-referenced — NOT 25 / (preheat + 127)."""
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
    # #174: charge origins on the first detect (2 s before the debounced transition),
    # so the charge-referenced clock is 127 s, NOT 125 — and still NOT 600 + 125.
    assert ctx.roast_elapsed_seconds == pytest.approx(127.0)
    dtr = ctx.development_elapsed_seconds / ctx.roast_elapsed_seconds
    assert dtr == pytest.approx(25.0 / 127.0)


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


# --- #497 (D89 Tier 1): the advisor context carries the ACTUATED heat/fan
# (never the advisor's own requested values) plus a loop-mode flag, so the
# model knows what is really actuating instead of assuming its last
# recommendation applied.


@pytest.mark.asyncio
async def test_advisor_context_carries_actuated_heat_fan_baseline_mode() -> None:
    """Baseline (post-FC loop flag off / not engaged): ``current_heat_percent``/
    ``current_fan_percent`` mirror the controller's actuated-output tracking
    (``_current_heat``/``_current_fan``) — the SAME fields the told==enforced
    safety box is built from (#412) — and ``post_fc_loop_active`` is False."""
    advisor = FakeAdvisor([decision(heat=65, fan=50)])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()
    ctx = advisor.contexts[-1]
    # harness_in_development bare-transitions (no ticked pre-FC actuation), so
    # the actuated levers are still the __init__ zero state going INTO this tick.
    assert ctx.current_heat_percent == 0
    assert ctx.current_fan_percent == 0
    assert ctx.post_fc_loop_active is False
    # And the actuated state afterward matches this tick's write.
    assert harness.controller.snapshot().current_heat == 65
    assert harness.controller.snapshot().current_fan == 50


@pytest.mark.asyncio
async def test_advisor_context_actuated_heat_reflects_prior_write_not_this_ticks_request() -> None:
    """The context built THIS tick reports the PRIOR tick's actuated value —
    not the advisor's request for the tick under construction, and not a
    number the model is merely assumed to have caused. Two sequential
    consults with distinct requested levers pin the field to the real,
    already-landed lever state (the #412 told-vs-requested distinction #497
    depends on), rather than to whatever the advisor is about to ask for."""
    advisor = FakeAdvisor([decision(heat=65, fan=50), decision(heat=99, fan=20)])
    harness = harness_in_development(readings=[reading(), reading()], advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()  # heat=65/fan=50 requested and actuated
    assert harness.controller.snapshot().current_heat == 65
    assert harness.controller.snapshot().current_fan == 50
    harness.clock.advance(5.0)  # well past the command rate-limit window
    harness.controller.request_advisory()
    await harness.controller.tick()  # this tick's OWN request is heat=99/fan=20
    ctx = advisor.contexts[-1]
    # The context built for THIS call still reports the PRIOR tick's actuated
    # 65/50 — never this tick's own not-yet-actuated 99/20 request.
    assert ctx.current_heat_percent == 65
    assert ctx.current_fan_percent == 50
    assert harness.controller.snapshot().current_heat == 99  # now actuated post-tick


def test_advisor_context_dtr_window_derives_from_the_shared_margin_config() -> None:
    """#499: the DTR window is [target - margin, target + margin], computed
    from the SAME ``config.drop_dev_margin_percent`` the deterministic
    drop-coherence guard reads (never a second/copied constant) — the
    told==enforced pattern applied to a margin, so the tolerance the model
    reasons with and the tolerance the deterministic layer enforces can
    never drift apart."""
    config = ControllerConfig(drop_dev_margin_percent=5.0)
    harness = make_harness(config=config)
    harness.controller.load_profile(PROFILE)  # target_development_percent=20.0
    for step in NORMAL_PATH[:3]:
        harness.controller.transition_to(step)
    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    assert ctx.target_development_percent_min == 15.0
    assert ctx.target_development_percent_max == 25.0


def test_advisor_context_roast_style_is_intent_only_no_style_set() -> None:
    """A profile with no ``roast_style`` (pre-#405, or a style-agnostic
    profile) reports ``None`` — the advisor sees no style label, only the
    profile's own explicit targets."""
    harness = make_harness()
    harness.controller.load_profile(PROFILE)  # roast_style defaults to None
    for step in NORMAL_PATH[:3]:
        harness.controller.transition_to(step)
    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    assert ctx.roast_style is None


def _sample_reference_roast() -> ReferenceRoast:
    """A minimal but non-trivial #567 reference for controller-level tests."""
    return ReferenceRoast(
        source_run_id="ref-run-1",
        origin_slug="ethiopia-harness",
        landmarks=ReferenceLandmarks(
            first_crack_temp_c=182.0,
            first_crack_elapsed_s=600.0,
            drop_temp_c=190.0,
            drop_development_percent=15.1,
            operator_rating=4,
        ),
        curve=[
            ReferenceCurveSample(t_s=600.0, bean_c=182.0, env_c=195.0, ror_c_min=7.0),
            ReferenceCurveSample(t_s=715.0, bean_c=190.0, env_c=200.0, ror_c_min=4.0),
        ],
    )


def test_advisor_context_reference_fields_default_empty_with_no_reference_roast() -> None:
    """#567 Slice B invariant: a controller constructed with no
    ``reference_roast`` (the default, ``None``) — every existing caller/test
    that predates this story — builds an ``AdvisorContext`` with the exact
    pre-#567 empty/``None`` reference fields."""
    harness = make_harness()  # no reference_roast kwarg — the default path
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:
        harness.controller.transition_to(step)
    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    assert ctx.reference_curve == []
    assert ctx.reference_landmarks is None


def test_advisor_context_reference_fields_populated_from_cached_reference_roast() -> None:
    """A controller constructed with a ``reference_roast`` (the caller's own
    once-per-run, fail-soft retrieval) copies its ``curve``/``landmarks``
    VERBATIM into every ``AdvisorContext`` built for the run's lifetime —
    never re-derived, never re-retrieved per tick."""
    reference = _sample_reference_roast()
    harness = make_harness(reference_roast=reference)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:
        harness.controller.transition_to(step)
    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    first_ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    assert first_ctx.reference_curve == reference.curve
    assert first_ctx.reference_landmarks == reference.landmarks

    # A second tick's context build reads the SAME cached instance — no
    # per-tick re-retrieval, no drift between calls.
    harness.clock.advance(5.0)
    second_ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    assert second_ctx.reference_curve == reference.curve
    assert second_ctx.reference_landmarks == reference.landmarks


def test_advisor_context_roast_style_forwards_the_profiles_style_name() -> None:
    """#499: when a profile carries a ``roast_style``, the context forwards
    the STYLE NAME as qualitative intent — never the style's own reference
    numbers, which stay unread here (the profile's own explicit
    ``target_development_percent``/``target_drop_temp_c`` remain the sole
    numeric source, D84's explicit-wins precedence untouched)."""
    styled_profile = PROFILE.model_copy(update={"roast_style": RoastStyle.LIGHT})
    harness = make_harness()
    harness.controller.load_profile(styled_profile)
    for step in NORMAL_PATH[:3]:
        harness.controller.transition_to(step)
    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    assert ctx.roast_style is RoastStyle.LIGHT
    # The numeric targets stay the PROFILE's own (20.0), never LIGHT's corpus
    # reference (15.0, per ROAST_STYLE_TARGETS) — style never overrides.
    assert ctx.target_development_percent == 20.0
    assert ctx.target_development_percent_min == 17.0
    assert ctx.target_development_percent_max == 23.0


def test_dtr_window_low_edge_matches_coherence_guard_floor() -> None:
    """Safety-reviewer LOW (#499): pins the TOLD window's low edge and the
    ENFORCED drop-coherence floor to move together — not merely that both
    read the same config constant today, but that the edge value itself is
    genuinely the guard's permitted boundary. A future change to the guard's
    inequality (>= -> >) or to either edge expression would desync the taught
    window from the enforced floor; this test fails the moment that happens,
    rather than only catching a drifted CONSTANT."""
    config = ControllerConfig(drop_dev_margin_percent=5.0)
    harness = make_harness(config=config)
    harness.controller.load_profile(PROFILE)  # target_development_percent=20.0
    for step in NORMAL_PATH[:3]:
        harness.controller.transition_to(step)
    limits = harness.controller._control_limits()  # pyright: ignore[reportPrivateUsage]
    ctx = harness.controller._build_advisor_context(reading(), limits)  # pyright: ignore[reportPrivateUsage]
    edge = ctx.target_development_percent_min
    assert edge is not None
    expected_edge = PROFILE.target_development_percent - config.drop_dev_margin_percent
    assert edge == expected_edge == 15.0
    # The taught edge is genuinely ALLOWED by the enforced guard (inclusive —
    # >= not >), and one hundredth of a point below it is genuinely BLOCKED.
    assert harness.controller._drop_development_is_coherent(edge) is True  # pyright: ignore[reportPrivateUsage]
    assert (
        harness.controller._drop_development_is_coherent(edge - 0.01) is False  # pyright: ignore[reportPrivateUsage]
    )


@pytest.mark.asyncio
async def test_advisor_context_post_fc_loop_active_true_when_taper_owns_heat() -> None:
    """Flag ON + true FC edge: ``post_fc_loop_active`` is True,
    ``current_heat_percent`` reports the TAPER'S actuated value (never the
    advisor's own traced-but-not-actuated heat recommendation), and
    ``current_fan_percent`` reports the advisor's OWN actuated fan (#498:
    fan is the advisor's lever in loop mode, revising D88(5)'s pinned fan) —
    the #497 evidence (11 Jul validation roast: actuated heat pinned at 65 %
    by the taper, advisor reasoned as if heat were 0).

    #498 coalesced-writer note (BLOCKER-1 fix): in loop mode the advisor's
    fan consult only sets a DESIRED fan target; the taper's own write (which
    runs FIRST each tick, per ``tick()``'s order) applies the desired fan
    from the PRIOR tick's consult. So the FC-edge tick's fan=60 decision does
    not land until the NEXT tick's taper write — this test drives one extra
    tick past the FC edge to observe it."""
    config = _post_fc_config(control_interval_seconds=5.0)
    advisor = FakeAdvisor(
        [decision(heat=50, fan=60), decision(heat=99, fan=85)],
        default_decision=decision(heat=99, fan=85),
    )
    harness = make_harness(config=config, advisor=advisor)
    await _charge_through_fc(harness)
    # The FC-edge tick's advisor consult only set the DESIRED fan (60); the
    # taper's write on that SAME tick ran BEFORE the consult, so fan is
    # unchanged from the pre-FC lever's value (30) at this point.
    assert harness.controller.snapshot().current_fan == 30
    harness.clock.advance(5.0)  # past the command rate-limit + post-FC control cadence
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=4.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    ctx = advisor.contexts[-1]
    assert ctx.post_fc_loop_active is True
    # The taper held the bumpless handoff value (100) going into this tick —
    # NOT the advisor's traced 99 heat recommendation.
    assert ctx.current_heat_percent == 100
    # This tick's own taper write ran BEFORE the advisor consult and applied
    # the FC-edge tick's desired fan (60) — so the context (built AFTER that
    # write, within the same tick) reports 60, the real actuated value.
    assert ctx.current_fan_percent == 60
    assert harness.controller.snapshot().current_heat == 100
    assert harness.controller.snapshot().current_fan == 60
    # This tick's OWN advisor consult (fan=85) only updates the desired fan
    # for the NEXT tick's taper write — it does not land yet.
    assert harness.executor.targets == [(100, 60)]


@pytest.mark.asyncio
async def test_advisor_context_post_fc_loop_active_false_on_operator_resume() -> None:
    """Flag ON but reached DEVELOPMENT via an operator resume (no true FC edge):
    ``_post_fc_engaged`` is False, so the loop is inert and
    ``post_fc_loop_active`` must read False — the advisor resumes driving
    post-FC heat/fan directly (the pre-B2 fallback), so it must be told its
    numbers ARE the actuated levers in that regime."""
    config = _post_fc_config()
    advisor = FakeAdvisor([decision(heat=61, fan=52)], default_decision=decision(heat=61, fan=52))
    harness = make_harness(config=config, advisor=advisor)
    harness.controller.load_profile(PROFILE)
    await harness.controller.recover_from_restart(RoastPhase.DEVELOPMENT)
    harness.controller.operator_resume(RoastPhase.DEVELOPMENT)
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=5.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    ctx = advisor.contexts[-1]
    assert ctx.post_fc_loop_active is False


def test_post_fc_loop_active_helper_matches_run_advisory_gate() -> None:
    """:meth:`RoastController._post_fc_loop_active` is the single source the
    context-builder and the advisor-actuation gate in ``_run_advisory`` both
    read — a regression guard against the two re-diverging into independently
    maintained (and driftable) boolean expressions."""
    config = _post_fc_config()
    harness = make_harness(config=config)
    harness.controller.load_profile(PROFILE)
    assert harness.controller._post_fc_loop_active() is False  # pyright: ignore[reportPrivateUsage]
    harness.controller._post_fc_engaged = True  # pyright: ignore[reportPrivateUsage]
    harness.controller.transition_to(RoastPhase.STARTING)
    harness.controller.transition_to(RoastPhase.PREHEATING)
    harness.controller.transition_to(RoastPhase.ROASTING_PRE_FIRST_CRACK)
    harness.controller._post_fc_engaged = True  # pyright: ignore[reportPrivateUsage]
    harness.controller.transition_to(RoastPhase.DEVELOPMENT)
    assert harness.controller._post_fc_loop_active() is True  # pyright: ignore[reportPrivateUsage]


# --- D96 slice 2 (#559): AdvisorContext gains post_fc_setpoint_c_per_min /
# post_fc_heat_authority_state, copied VERBATIM from the SAME
# PostFcControlOutput the controller's own tick already computed (told ==
# enforced, the #497 precedent extended to these two fields). -----------------


@pytest.mark.asyncio
async def test_advisor_context_post_fc_fields_none_before_loop_ever_computes() -> None:
    """Baseline (post-FC loop flag off / not yet engaged): both new fields
    default ``None`` — mirroring ``current_heat_percent``'s own ``None``
    default exactly (the pre-D96 regime, byte-for-byte unaffected)."""
    advisor = FakeAdvisor([decision()])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    harness.controller.request_advisory()
    await harness.controller.tick()
    ctx = advisor.contexts[-1]
    assert ctx.post_fc_setpoint_c_per_min is None
    assert ctx.post_fc_heat_authority_state is None


@pytest.mark.asyncio
async def test_advisor_context_post_fc_fields_populated_told_equals_enforced() -> None:
    """Flag ON + true FC edge: both new fields are populated from the SAME
    ``PostFcControlOutput`` the loop's write this tick actually used to build
    the safety box — verified by comparing the context's fields against the
    controller's OWN stashed ``_last_post_fc_output`` directly (told ==
    enforced: not just "some plausible value", the EXACT same object's
    fields), rather than independently re-deriving what they "should" be."""
    config = _post_fc_config(control_interval_seconds=5.0)
    advisor = FakeAdvisor([decision(heat=50, fan=60)], default_decision=decision(heat=99, fan=85))
    harness = make_harness(config=config, advisor=advisor)
    await _charge_through_fc(harness)
    harness.clock.advance(5.0)  # past the command rate-limit + post-FC control cadence
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=4.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()

    ctx = advisor.contexts[-1]
    stashed = harness.controller._last_post_fc_output  # pyright: ignore[reportPrivateUsage]
    assert stashed is not None
    assert ctx.post_fc_setpoint_c_per_min == stashed.setpoint_c_per_min
    assert ctx.post_fc_heat_authority_state == stashed.heat_authority_state
    # Not a vacuous comparison against itself: pin real, non-default values.
    assert ctx.post_fc_setpoint_c_per_min is not None
    assert ctx.post_fc_heat_authority_state is not None


@pytest.mark.asyncio
async def test_advisor_context_post_fc_fields_none_on_operator_resume() -> None:
    """Flag ON but reached DEVELOPMENT via an operator resume (no true FC
    edge, mirroring ``test_advisor_context_post_fc_loop_active_false_on_
    operator_resume`` exactly): the loop never computed anything this
    engagement (``_post_fc_engaged`` False, ``transition_to`` clears
    ``_last_post_fc_output`` unconditionally on every transition), so both
    new fields must read ``None`` — the advisor is driving post-FC heat/fan
    directly in this regime and has no post-FC-loop state to be told about."""
    config = _post_fc_config()
    advisor = FakeAdvisor([decision(heat=61, fan=52)], default_decision=decision(heat=61, fan=52))
    harness = make_harness(config=config, advisor=advisor)
    harness.controller.load_profile(PROFILE)
    await harness.controller.recover_from_restart(RoastPhase.DEVELOPMENT)
    harness.controller.operator_resume(RoastPhase.DEVELOPMENT)
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=5.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    ctx = advisor.contexts[-1]
    assert ctx.post_fc_setpoint_c_per_min is None
    assert ctx.post_fc_heat_authority_state is None


@pytest.mark.asyncio
async def test_advisor_context_post_fc_fields_not_stashed_on_recovery_suppressed_tick() -> None:
    """D96 (#559): on a tick where the recovery-raise skip fires (PR #560
    rounds 1/2/4 — the SAME-tick guard/drop-eligible raise suppression), the
    tentative ``PostFcControlOutput`` is fully discarded (mirrors the PI
    state restore) — it must NOT be stashed as ``_last_post_fc_output``.
    ``AdvisorContext`` must never see a setpoint/heat-authority-state this
    tick's write never actually used.

    Uses the DETERMINISTIC-DROP path with a FAILING ``drop_beans()`` (rather
    than the ceiling guard) so the phase stays DEVELOPMENT — a ceiling-guard
    scenario transitions to COOLING on the same tick, which clears the stash
    via ``transition_to``'s own unconditional reset regardless of whether the
    suppression logic itself worked, confounding this specific assertion."""

    class AlwaysFailingDropExecutor(RecordingExecutor):
        async def drop_beans(self) -> AppliedRoasterState:
            raise RuntimeError("serial write dropped")

    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=220.0,  # stay clear of the guard; isolate the anchor
        recovery_confirm_ticks=1,
    )
    advisor = FakeAdvisor([decision()], default_decision=decision())
    harness = make_harness(
        config=config,
        advisor=advisor,
        executor=AlwaysFailingDropExecutor(),
        limits=_ISOLATED_CEILING_GUARD_LIMITS,
    )
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    harness.controller._charge_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    fc_monotonic = harness.controller._first_crack_monotonic  # pyright: ignore[reportPrivateUsage]
    assert fc_monotonic is not None
    harness.controller._first_crack_monotonic = fc_monotonic - 10_000.0  # pyright: ignore[reportPrivateUsage]
    stash_before = harness.controller._last_post_fc_output  # pyright: ignore[reportPrivateUsage]

    # A tick with a shortfall large enough to confirm entry immediately
    # (recovery_confirm_ticks=1) AND bean/dev% both at the deterministic
    # anchor's target -- the exact same-tick suppression scenario, with the
    # drop then failing so the phase stays DEVELOPMENT.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=2.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.DEVELOPMENT  # the drop failed, stayed here
    stash_after = harness.controller._last_post_fc_output  # pyright: ignore[reportPrivateUsage]
    assert stash_after == stash_before  # the suppressed tick's output never landed


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
    long preheat, the charge origins on the first detect (#174, 2 s before the
    debounced transition): 102 s pre-FC → FC → 25 s development, DTR =
    25 / 127 * 100 ≈ 19.7% (NOT 25 / (preheat + 127))."""
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
    # #174: charge origins 2 s earlier (first detect) → 127 s denominator.
    assert snap.development_percent == pytest.approx(25.0 / 127.0 * 100.0)
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
        # This capstone is a deliberately advisor-driven scenario (the #312
        # coherence guard on the ADVISOR's own should_drop), so both post-FC
        # flags are explicit OFF here — the 12 Jul D88/D89 promotion flipped
        # their config-field defaults to True, and the ceiling guard would
        # otherwise auto-drop the instant the FC frame's bean=196.0 reaches
        # its default ceiling_guard_temp_c, short-circuiting the very
        # coherence-guard behaviour this test demonstrates.
        config=ControllerConfig(
            post_first_crack_control=PostFirstCrackControl(
                enabled=False, ceiling_guard_drop_enabled=False
            )
        ),
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

    async def drop_beans(self) -> AppliedRoasterState | None:
        if "drop_beans" in self._failing:
            raise RuntimeError("write failed")
        return await super().drop_beans()

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


# --- Direct unit tests for _execute_targets's bool return (Codex finding,
# fix round 2, #405 Slice B2): callers with their own control-loop state must
# key state-advance on this return value, not on ``evaluation.verdict`` alone
# (an ALLOW/CLAMP verdict whose ``set_targets`` call then raises is NOT an
# executed command). These tests exercise the method directly, isolating its
# return contract from any calling context. ---------------------------------


@pytest.mark.asyncio
async def test_execute_targets_returns_true_on_success() -> None:
    harness = make_harness()
    evaluation = SafetyEvaluation(
        rule="all_clear",
        verdict=SafetyVerdict.ALLOW,
        adjusted_heat=70,
        adjusted_fan=50,
        reason="test",
    )
    executed = await harness.controller._execute_targets(evaluation)  # pyright: ignore[reportPrivateUsage]
    assert executed is True
    assert harness.executor.targets == [(70, 50)]
    assert harness.controller.snapshot().current_heat == 70
    assert harness.controller.snapshot().current_fan == 50
    assert RoastEventKind.COMMAND_EXECUTED in harness.events.kinds()


@pytest.mark.asyncio
async def test_execute_targets_returns_false_on_non_executable_verdict() -> None:
    """REJECT (and any verdict other than ALLOW/CLAMP, or a missing adjusted
    value) never attempts ``set_targets`` — no write, no state change."""
    harness = make_harness()
    evaluation = SafetyEvaluation(
        rule="command_rate_limited",
        verdict=SafetyVerdict.REJECT,
        reason="test",
    )
    executed = await harness.controller._execute_targets(evaluation)  # pyright: ignore[reportPrivateUsage]
    assert executed is False
    assert harness.executor.targets == []
    assert harness.controller.snapshot().current_heat == 0  # unchanged
    assert RoastEventKind.COMMAND_EXECUTED not in harness.events.kinds()
    assert RoastEventKind.COMMAND_FAILED not in harness.events.kinds()


@pytest.mark.asyncio
async def test_execute_targets_returns_false_when_set_targets_raises() -> None:
    """The core case this fix round adds: an ALLOW/CLAMP verdict whose
    ``set_targets`` call raises (a transient actuator/serial failure) returns
    ``False`` — the write never reached the roaster, so
    ``current_heat``/``current_fan``/``_last_command_monotonic`` all stay
    unchanged, even though the verdict itself was ALLOW."""

    class RaisingExecutor(RecordingExecutor):
        async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
            raise RuntimeError("serial write dropped")

    harness = make_harness(executor=RaisingExecutor())
    evaluation = SafetyEvaluation(
        rule="all_clear",
        verdict=SafetyVerdict.ALLOW,
        adjusted_heat=70,
        adjusted_fan=50,
        reason="test",
    )
    executed = await harness.controller._execute_targets(evaluation)  # pyright: ignore[reportPrivateUsage]
    assert executed is False
    assert harness.executor.targets == []  # never actually written
    assert harness.controller.snapshot().current_heat == 0  # unchanged, NOT 70
    assert harness.controller.snapshot().current_fan == 0  # unchanged, NOT 50
    assert harness.controller._last_command_monotonic is None  # pyright: ignore[reportPrivateUsage]
    assert RoastEventKind.COMMAND_FAILED in harness.events.kinds()
    assert RoastEventKind.COMMAND_EXECUTED not in harness.events.kinds()


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
async def test_operator_drop_beans_in_faulted_does_not_transition() -> None:
    """#210: from faulted, DROP BEANS dumps the beans out of the hot drum and
    issues the MCP write WITHOUT a phase transition — the run stays faulted (heat
    off) until acknowledged. The operator must be able to safe the beans after an
    e-stop/fault so they stop scorching, without re-enabling heat or auto-resuming
    anything."""
    harness = make_harness()
    harness.controller.transition_to(RoastPhase.FAULTED)
    harness.events.events.clear()
    targets_before = list(harness.executor.targets)
    await harness.controller.operator_drop_beans()
    # The drop was issued, and the run STAYS faulted (no cooling transition).
    assert "drop_beans" in harness.executor.commands
    assert harness.controller.phase is RoastPhase.FAULTED
    assert RoastEventKind.RUN_COMPLETED not in harness.events.kinds()
    # Heat stays off: the drop issued NO set_targets write at all (no re-enable).
    assert harness.executor.targets == targets_before
    # It went through the safety path (a command_phase_validity ALLOW was persisted).
    assert any(
        e.rule == "command_phase_validity" and e.verdict is SafetyVerdict.ALLOW
        for e in harness.sink.evaluations
    )


@pytest.mark.asyncio
async def test_operator_drop_beans_in_development_still_transitions_to_cooling() -> None:
    """#210 regression: the NORMAL drop is unchanged — from development it issues
    the drop AND transitions to cooling (only the faulted case is no-transition)."""
    harness = make_harness()
    _to(harness, 4)  # → DEVELOPMENT
    await harness.controller.operator_drop_beans()
    assert "drop_beans" in harness.executor.commands
    assert harness.controller.phase is RoastPhase.COOLING


@pytest.mark.asyncio
async def test_operator_drop_beans_rejected_in_preheating() -> None:
    """#210: DROP is still rejected where there are no beans (preheating) — the
    faulted addition does not loosen the no-beans guard."""
    harness = make_harness()
    _to(harness, 2)  # → PREHEATING
    await harness.controller.operator_drop_beans()
    assert "drop_beans" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.PREHEATING
    assert any(
        e.rule == "command_phase_validity" and e.verdict is SafetyVerdict.REJECT
        for e in harness.sink.evaluations
    )


@pytest.mark.asyncio
async def test_emergency_stop_still_available_from_faulted_after_drop() -> None:
    """#210: e-stop stays available from faulted even after a drop — the safe-ing
    additions never reduce the always-available e-stop (the E3-S4 invariant)."""
    harness = make_harness()
    harness.controller.transition_to(RoastPhase.FAULTED)
    await harness.controller.operator_drop_beans()
    harness.events.events.clear()
    await harness.controller.operator_emergency_stop(reason="manual after drop")
    assert harness.executor.estop_reasons  # e-stop executed from faulted


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
        config=ControllerConfig(
            post_fc_deadband_threshold_percent=15,
            post_first_crack_control=PostFirstCrackControl(
                enabled=False, ceiling_guard_drop_enabled=False
            ),
        ),
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
        config=ControllerConfig(
            post_fc_deadband_threshold_percent=15,
            post_first_crack_control=PostFirstCrackControl(
                enabled=False, ceiling_guard_drop_enabled=False
            ),
        ),
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
        config=ControllerConfig(
            post_fc_deadband_threshold_percent=15,
            post_first_crack_control=PostFirstCrackControl(
                enabled=False, ceiling_guard_drop_enabled=False
            ),
        ),
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


# --- D82/D83 (#405 Slice B2): deterministic post-FC RoR-target PI loop wiring ---


async def _charge_through_fc(
    harness: Harness, *, fc_bean_temp_c: float = 178.0, fc_ror_c_per_min: float | None = None
) -> None:
    """Drive ``harness`` from PREHEATING through a real (ticked) pre-FC lever
    actuation into DEVELOPMENT, via first-crack MCP detection.

    Unlike ``harness_in_development`` (which walks ``transition_to`` directly
    and leaves ``current_heat`` at 0), this ticks through pre-FC so
    ``current_heat`` is a real ACTUATED value (100, the deterministic pre-FC
    lever's default target) at the moment first crack fires — the precondition
    the bumpless-handoff test needs.

    Args:
        harness: The test harness to drive.
        fc_bean_temp_c: The bean temperature on the FC-detection reading.
        fc_ror_c_per_min: The ``bean_ror_c_per_min`` on that SAME reading —
            the D88 taper's engagement anchor (``PostFcRorController.reset``'s
            ``ror_at_engagement_c_per_min``). ``None`` (default) leaves the FC
            reading's RoR unset, exercising the controller's own
            RoR-unavailable-at-engagement fallback (floors to
            ``taper_end_ror_c_per_min``) — most callers of this helper only
            care about reaching DEVELOPMENT with a real actuated pre-FC heat,
            not the taper's exact seed.
    """
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    t0 = reading(bean=150.0, t0_detected=True, bean_ror_c_per_min=20.0)
    harness.reader.readings = [t0]
    for _ in range(3):  # three consecutive T0 ticks debounce → pre-FC
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert harness.controller.snapshot().current_heat == 100  # the actuated pre-FC lever
    fc_reading = (
        reading(bean=fc_bean_temp_c, first_crack_detected=True, bean_ror_c_per_min=fc_ror_c_per_min)
        if fc_ror_c_per_min is not None
        else reading(bean=fc_bean_temp_c, first_crack_detected=True)
    )
    harness.reader.readings = [fc_reading]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    harness.log.clear()
    harness.events.events.clear()
    harness.sink.evaluations.clear()
    # Clear the pre-FC writes accumulated so far: every test using this helper
    # asserts on writes issued FROM the DEVELOPMENT phase onward only.
    harness.executor.targets.clear()


def _post_fc_config(**overrides: object) -> ControllerConfig:
    """A ``ControllerConfig`` with the post-FC PI loop ENABLED and the
    ceiling-guard drop explicitly OFF, unless the caller overrides either.

    The guard pin (12 Jul D88/D89 promotion, #495 flipped its OWN default to
    ``True``) keeps this helper's original isolated-testing intent: exercise
    the RoR-taper loop ALONE, without the now-default-on ceiling guard also
    firing and confusing a taper-only assertion (e.g. a drop the test expects
    NOT to fire, that the guard would otherwise trigger independently — D88
    amendment A1's whole point is that it fires regardless of the taper
    flag)."""
    overrides.setdefault("enabled", True)
    overrides.setdefault("ceiling_guard_drop_enabled", False)
    return ControllerConfig(
        post_first_crack_control=PostFirstCrackControl(**overrides)  # type: ignore[arg-type]
    )


def _ceiling_guard_config(**overrides: object) -> ControllerConfig:
    """A ``ControllerConfig`` with the D88 ceiling-guard drop ENABLED (own
    flag) and the RoR-taper loop explicitly OFF, unless the caller overrides
    either — deliberately NOT bundled with :func:`_post_fc_config`, since the
    guard's whole point (D88 amendment A1) is that it fires independently of
    the taper flag. The taper is pinned OFF here (not left at the config's
    OWN default) because the 12 Jul D88/D89 promotion flipped that default
    to ``True`` — this helper's whole test purpose is isolating the guard's
    behaviour from the taper, which needs an explicit pin now that the two
    are no longer both-off by construction."""
    overrides.setdefault("ceiling_guard_drop_enabled", True)
    overrides.setdefault("enabled", False)
    return ControllerConfig(
        post_first_crack_control=PostFirstCrackControl(**overrides)  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_post_fc_loop_flag_off_is_byte_for_byte_unchanged() -> None:
    """Flag OFF (default): the advisor's levers still actuate post-FC, and the
    PI loop never writes — a regression test pinning today's behaviour."""
    advisor = FakeAdvisor([decision(heat=70, fan=55)])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    assert harness.controller._config.post_first_crack_control.enabled is False  # pyright: ignore[reportPrivateUsage]
    await harness.controller.tick()
    assert advisor.contexts, "the advisor should have been consulted"
    assert harness.executor.targets == [(70, 55)]
    assert harness.controller.snapshot().current_heat == 70
    assert harness.controller.snapshot().current_fan == 55


@pytest.mark.asyncio
async def test_post_fc_loop_enabled_actuates_heat_deterministically_fan_from_advisor() -> None:
    """Flag ON, DEVELOPMENT: the PI loop drives heat deterministically (the
    advisor's heat recommendation is traced but never actuated); the advisor's
    FAN recommendation DOES actuate (#498, D89 Tier 1 — revises D88(5)'s
    pinned fan: fan is the advisor's lever in loop mode, same as baseline, both
    still through the same safety path).

    #498 coalesced-writer note (BLOCKER-1 fix): loop mode has exactly ONE
    writer (the taper), which applies the advisor's DESIRED fan from the
    PRIOR tick's consult — so a fan decision takes one extra tick to land
    versus the pre-fix design. This test drives two ticks past the FC edge:
    the first tick's taper write is a no-op (the FC-edge desire, 30, already
    equals the current fan), and that SAME tick's consult sets the new
    desire (10); the second tick's taper write finally applies it."""
    config = _post_fc_config(control_interval_seconds=5.0)
    advisor = FakeAdvisor([decision(heat=50, fan=30)], default_decision=decision(heat=99, fan=10))
    harness = make_harness(config=config, advisor=advisor)
    await _charge_through_fc(harness)
    assert harness.controller.snapshot().current_fan == 30  # unchanged from the pre-FC lever
    for _ in range(2):
        harness.clock.advance(5.0)  # past the command rate-limit + post-FC control cadence
        harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=4.0)]
        harness.controller.request_advisory()
        await harness.controller.tick()
    # The loop wrote — heat HOLDS at the 100% handoff (a D88 bumpless hold,
    # not a positive-error climb): this FC edge's reading carries no RoR, so
    # the engagement RoR falls back to taper_end_ror_c_per_min (4.0), which is
    # also where r0 floors — the taper setpoint is flat at 4.0. Feeding that
    # same 4.0 back in here gives zero error, so heat=100 is the seeded
    # handoff value held exactly (effective_ceiling is 100, so nothing clamps
    # it down either) — the advisor's 99 never lands. Fan actuates the
    # advisor's OWN 10 (a decisive move from 30, not damped).
    assert harness.executor.targets == [(100, 10)]
    assert harness.controller.snapshot().current_heat == 100
    assert harness.controller.snapshot().current_fan == 10
    # The advisor WAS consulted (still traced), and only its heat recommendation
    # (99) never actuated — the write on the wire carries the loop's heat and
    # the advisor's own fan, never the advisor's 99.
    assert advisor.contexts, "the advisor is still consulted (traced)"
    assert (99, 10) not in harness.executor.targets


@pytest.mark.asyncio
async def test_post_fc_loop_enabled_fan_clamp_actuates_clamped_value_heat_still_holds() -> None:
    """#498: a loop-mode fan CLAMP actuates the CLAMPED value (never the raw
    request) and records a CLAMP verdict — the SAME safety path as baseline —
    while heat still holds at the taper's value regardless of the clamp.

    DEVELOPMENT's phase box is [0, 100] for both levers with no config knob to
    narrow it (control_policy.py), so a fan CLAMP is manufactured the same way
    the #273 told==enforced proof does it for the pre-FC box
    (``test_pre_fc_deterministic_box_told_equals_enforced``): resolve the real
    box, narrow ONLY fan_ceiling_percent, and have the controller hand that box
    to both the advisor context and the gate for this one consult.

    #498 coalesced-writer note (BLOCKER-1 fix): the advisor's CLAMP is
    recorded at the FIRST tick under the narrowed box (its bounds-only
    evaluation, which sets the desired fan) but the taper's single writer
    does not APPLY the clamped fan until the NEXT tick — two ticks are
    driven to observe both the recorded verdict and the eventual write."""
    config = _post_fc_config(control_interval_seconds=5.0)
    advisor = FakeAdvisor([decision(heat=50, fan=30)], default_decision=decision(heat=99, fan=95))
    harness = make_harness(config=config, advisor=advisor)
    await _charge_through_fc(harness)
    assert harness.controller.snapshot().current_fan == 30  # unchanged from the pre-FC lever
    narrowed_box = harness.controller._control_limits().model_copy(  # pyright: ignore[reportPrivateUsage]
        update={"fan_ceiling_percent": 70}
    )
    harness.controller._control_limits = lambda *a, **k: narrowed_box  # type: ignore[method-assign] # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    harness.clock.advance(5.0)  # past the command rate-limit + post-FC control cadence
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=4.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    # This tick's advisor consult CLAMPS the requested 95 to the narrowed
    # ceiling (70) and sets it as the desired fan — the taper's own write
    # this tick was a no-op (fan already 30, matching the FC-edge desire).
    command_evals = [
        e for e in harness.sink.evaluations if e.rule in ("all_clear", "command_bounds")
    ]
    assert command_evals[-1].verdict is SafetyVerdict.CLAMP
    assert command_evals[-1].adjusted_fan == 70  # the CLAMPED value, never the raw 95
    assert harness.executor.targets == []
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=4.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    # The clamped fan is what actually reaches the roaster on the NEXT tick's
    # taper write — heat holds at the taper's value (100) regardless of the
    # fan clamp.
    assert harness.executor.targets == [(100, 70)]
    assert harness.controller.snapshot().current_heat == 100
    assert harness.controller.snapshot().current_fan == 70


@pytest.mark.asyncio
async def test_post_fc_loop_heat_never_actuates_even_when_fan_does_same_decision() -> None:
    """#498, isolated minimal case: ONE constant advisor decision recommending
    a drastic heat cut (1 %) alongside a drastic fan cut (1 %) — heat must
    NEVER land (the taper's bumpless-handoff value, 100, is the only thing on
    the wire for heat) while fan DOES eventually land at the advisor's own 1 %.

    #498 coalesced-writer note (BLOCKER-1 fix): fan lands one tick after the
    decision that requested it (the taper's single writer applies the PRIOR
    tick's desired fan) — this test keeps the SAME decision constant across
    two ticks so the one-tick fan lag is the only thing separating "requested"
    from "landed", proving heat is NEVER on that path regardless."""
    config = _post_fc_config(control_interval_seconds=5.0)
    advisor = FakeAdvisor([decision(heat=1, fan=1)], default_decision=decision(heat=1, fan=1))
    harness = make_harness(config=config, advisor=advisor)
    await _charge_through_fc(harness)  # sets the desired fan (1) — does not land yet
    assert harness.controller.snapshot().current_heat == 100  # never the advisor's 1
    assert harness.controller.snapshot().current_fan == 30  # unchanged from the pre-FC lever
    harness.clock.advance(5.0)  # past the command rate-limit + post-FC control cadence
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=4.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.controller.snapshot().current_heat == 100  # STILL never the advisor's 1
    assert harness.controller.snapshot().current_fan == 1  # the advisor's own fan now lands


@pytest.mark.asyncio
async def test_post_fc_loop_taper_heat_move_and_advisor_fan_move_both_land_same_tick() -> None:
    """Safety-reviewer BLOCKER-1 regression proof (#498): a tick where the
    taper's heat GENUINELY MOVES (rising RoR) and the advisor's fan recommends
    a DISTINCT value must land BOTH in ONE ``set_targets`` call, with NO
    ``command_rate_limited`` REJECT among that tick's evaluations.

    Pre-fix (two independent writers per tick, both on a 5 s cadence): the
    taper's write ran first and consumed the tick's ONE
    ``min_seconds_between_commands`` slot, so the advisor's fan evaluation
    hit ``command_rate_limited`` and its fan was silently dropped — same-tick
    collision was the COMMON case, not an edge case, because both cadences
    default to the identical interval. This test FAILS on the pre-fix design
    (fan never lands — see the finding's empirical trace: a heat-moving tick
    landed only ``(new_heat, pinned_fan)``, the advisor's fan REJECTed) and
    PASSES on the coalesced single-writer fix.

    #498's one-tick lag (the fix's own, intentional, and tested-elsewhere
    property — see the isolated single-decision test above): the fan value
    that lands THIS tick is the DESIRED fan the PRIOR tick's consult set, not
    this tick's own request (which only updates the desire for the NEXT
    tick). That lag is orthogonal to what THIS test proves: regardless of
    which tick's desire it is, a fan value and a genuinely-moving heat value
    are applied TOGETHER, in one write, with no rate-limit collision between
    them — the defect this regression guards against."""
    config = _post_fc_config(ror_smoothing_alpha=1.0, control_interval_seconds=5.0)
    advisor = FakeAdvisor([decision(heat=50, fan=40)], default_decision=decision(heat=50, fan=90))
    harness = make_harness(config=config, advisor=advisor)
    await _charge_through_fc(harness, fc_ror_c_per_min=6.1)
    assert harness.controller.snapshot().current_heat == 100  # the bumpless handoff
    assert harness.controller.snapshot().current_fan == 30  # unchanged from the pre-FC lever
    harness.clock.advance(5.0)  # past the command rate-limit + post-FC control cadence
    # A rising RoR (9.0, well above the taper's declining setpoint) forces the
    # PI loop to cut heat — a GENUINE heat move, not an idempotent hold.
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=9.0)]
    harness.controller.request_advisory()
    harness.sink.evaluations.clear()
    await harness.controller.tick()
    # Heat moved (proving this is not a vacuous idempotent-heat tick).
    assert harness.controller.snapshot().current_heat != 100
    # BOTH land in ONE write: heat at its new taper value, fan at the FC-edge
    # consult's desire (40) — not the pinned config value, not left un-actuated.
    assert harness.executor.targets == [(harness.controller.snapshot().current_heat, 40)]
    assert harness.controller.snapshot().current_fan == 40
    # No rate-limit collision anywhere in this tick's evaluations — the defect
    # this test regresses against.
    assert not [e for e in harness.sink.evaluations if e.rule == "command_rate_limited"]
    # This tick's OWN advisor consult (fan=90) only updates the desire for the
    # NEXT tick — the #498 one-tick lag, asserted elsewhere, not this test's point.
    assert harness.controller._post_fc_desired_fan_percent == 90  # pyright: ignore[reportPrivateUsage]


async def _post_fc_heat_trajectory(fan_script: list[int]) -> list[int]:
    """Drive 3 post-FC ticks with rising RoR (so the taper's heat genuinely
    moves) while the advisor's fan recommendation follows ``fan_script`` —
    a helper for the #498 heat/fan-decoupling equivalence test below."""
    config = _post_fc_config(ror_smoothing_alpha=1.0, control_interval_seconds=5.0)
    decisions = [decision(heat=50, fan=f) for f in fan_script]
    advisor = FakeAdvisor(decisions, default_decision=decisions[-1])
    harness = make_harness(config=config, advisor=advisor)
    await _charge_through_fc(harness, fc_ror_c_per_min=6.1)
    heats = [harness.controller.snapshot().current_heat]
    for ror in (9.0, 9.5, 10.0):
        harness.clock.advance(5.0)  # past the command rate-limit + control cadence
        harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=ror)]
        harness.controller.request_advisory()
        await harness.controller.tick()
        heats.append(harness.controller.snapshot().current_heat)
    return heats


@pytest.mark.asyncio
async def test_post_fc_taper_heat_trajectory_unaffected_by_advisor_fan_swings() -> None:
    """#498: the taper's heat trajectory is IDENTICAL whether the advisor's fan
    recommendation swings wildly tick to tick or stays constant — the PI loop
    computes heat purely from measured RoR (``PostFcRorController.compute``
    takes no fan input at all), so a concurrent advisor fan write can
    structurally never perturb it. A real, MOVING heat trajectory (not a flat
    100) makes this equivalence load-bearing rather than vacuous."""
    varying_fan = await _post_fc_heat_trajectory([10, 90, 20])
    constant_fan = await _post_fc_heat_trajectory([55, 55, 55])
    assert varying_fan == constant_fan
    assert len(set(varying_fan)) > 1, "the heat trajectory must actually move to be meaningful"


@pytest.mark.asyncio
async def test_post_fc_loop_enabled_should_drop_still_honored() -> None:
    """Flag ON: the loop owns heat/fan, but the advisor's ``should_drop`` drop
    coherence path is UNCHANGED — a coherent drop still fires the drop."""
    config = _post_fc_config()
    advisor = FakeAdvisor(
        [decision(heat=50, fan=40, drop=True)],
        default_decision=decision(heat=50, fan=40, drop=True),
    )
    harness = _development_harness_with_dev_percent(
        system_dev_percent=25.0, advisor=advisor, config=config
    )
    harness.reader.readings = [reading(bean_ror_c_per_min=8.0)]
    await harness.controller.tick()
    assert "drop_beans" in harness.executor.commands
    assert harness.controller.phase is RoastPhase.COOLING


@pytest.mark.asyncio
async def test_post_fc_loop_bumpless_handoff_holds_pre_fc_heat() -> None:
    """Bumpless handoff: heat at the first DEVELOPMENT control tick equals the
    pre-FC actuated heat (100) — no dip or jump — when RoR sits at the D88
    taper's r0 (anchored to the SAME RoR the FC-edge reading measured, so
    error is exactly zero at the first control tick)."""
    config = _post_fc_config(ror_smoothing_alpha=1.0)
    harness = make_harness(config=config)
    # r0 = clamp(6.1, 4.0, 8.0) = 6.1 (the engagement RoR, in-range) — feed the
    # SAME RoR at the FC edge and at the first DEVELOPMENT tick for zero error.
    await _charge_through_fc(harness, fc_ror_c_per_min=6.1)
    assert harness.controller.snapshot().current_heat == 100  # unchanged going INTO the tick
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=6.1)]
    await harness.controller.tick()
    # Zero error at the handoff heat (100) => the loop holds exactly 100 (no
    # dip/jump), even though 100 is also the pre-FC value — this proves the
    # SEEDED loop reproduces it, not merely "heat never changed".
    assert harness.controller.snapshot().current_heat == 100


@pytest.mark.asyncio
async def test_post_fc_loop_bumpless_handoff_from_a_lower_pre_fc_heat() -> None:
    """Bumpless handoff, non-trivial case: pre-FC heat trimmed below 100 (the
    late-Maillard trim) still hands off with no dip/jump at zero error."""
    config = _post_fc_config(ror_smoothing_alpha=1.0)
    harness = make_harness(config=config)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:
        harness.controller.transition_to(step)
    t0 = reading(bean=150.0, t0_detected=True, bean_ror_c_per_min=20.0)
    harness.reader.readings = [t0]
    for _ in range(3):
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    # Warm into the late-Maillard trim window so pre-FC heat trims below 100.
    bean = 162.0
    for _ in range(6):
        harness.reader.readings = [reading(bean=bean, bean_ror_c_per_min=30.0)]
        await harness.controller.tick()
        harness.clock.advance(1.0)
        bean += 0.5
    handoff_heat = harness.controller.snapshot().current_heat
    assert 0 < handoff_heat < 100  # the trim engaged: a genuine non-100 handoff value
    # r0 = clamp(6.1, 4.0, 8.0) = 6.1 — feed the SAME RoR at the FC edge and at
    # the first DEVELOPMENT tick for zero error.
    harness.reader.readings = [
        reading(bean=178.0, first_crack_detected=True, bean_ror_c_per_min=6.1)
    ]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    harness.log.clear()
    harness.events.events.clear()
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=6.1)]
    await harness.controller.tick()
    assert harness.controller.snapshot().current_heat == handoff_heat


@pytest.mark.asyncio
async def test_post_fc_loop_engagement_ror_unavailable_falls_back_to_taper_end() -> None:
    """qa gap: the FC-edge reading can legitimately carry no RoR sample (e.g.
    not enough history yet). ``controller.py``'s fallback (the true-FC-edge
    branch of ``transition_to``) must seed ``ror_at_engagement_c_per_min``
    with ``taper_end_ror_c_per_min`` in that case — the same value the loop's
    own B1 floor would apply to a genuinely degenerate reading, not a
    separate/looser special case. This was previously exercised only
    incidentally (several ``_charge_through_fc(harness)`` calls hit this path
    without an RoR override) with no assertion on the resulting r0/setpoint;
    this test asserts it directly and proves tick-1 is a bumpless HOLD, not a
    cut."""
    config = _post_fc_config(ror_smoothing_alpha=1.0)
    harness = make_harness(config=config)
    # No fc_ror_c_per_min override -> the FC-edge reading carries no RoR.
    await _charge_through_fc(harness)
    assert harness.controller.snapshot().current_heat == 100  # the actuated pre-FC lever

    snapshot = harness.controller._post_fc_controller.snapshot_state()  # pyright: ignore[reportPrivateUsage]
    assert snapshot.taper_r0_c_per_min == pytest.approx(
        harness.controller._config.post_first_crack_control.taper_end_ror_c_per_min  # pyright: ignore[reportPrivateUsage]
    )
    assert snapshot.heat_engage_percent == 100

    # Feeding the SAME fallback RoR (4.0, the default taper_end) back in at
    # the first DEVELOPMENT tick gives zero error -> a bumpless HOLD at the
    # 100% handoff, not an instant cut.
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=4.0)]
    await harness.controller.tick()
    assert harness.controller.snapshot().current_heat == 100


@pytest.mark.asyncio
async def test_post_fc_loop_never_crashes_below_configured_floor() -> None:
    """No crash-to-0 (the roast-7 failure): even when RoR runs far hotter than
    target for a sustained post-FC sequence, commanded heat never drops below
    the configured floor (>= 1, default 25)."""
    config = _post_fc_config(heat_floor_percent=25, control_interval_seconds=5.0)
    harness = make_harness(config=config)
    await _charge_through_fc(harness)
    heats: list[int] = []
    for _ in range(10):
        harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=40.0)]
        await harness.controller.tick()
        harness.clock.advance(5.0)  # exactly the control cadence each step
        heats.append(harness.controller.snapshot().current_heat)
    assert all(h >= 25 for h in heats), heats
    assert all(h != 0 for h in heats), heats


@pytest.mark.asyncio
async def test_post_fc_loop_cadence_actuates_every_control_interval_not_every_tick() -> None:
    """The loop actuates roughly every ``control_interval_seconds`` (5 s
    default), not every 1 s controller tick. RoR is held moderately ABOVE
    target throughout, so the loop keeps easing heat down a little at each
    cadence tick — never saturating — which makes each new cadence actuation
    produce a genuinely DIFFERENT (not merely idempotent-repeat) command."""
    config = _post_fc_config(
        control_interval_seconds=5.0, kp_percent_per_ror=1.0, ki_percent_per_ror_second=0.02
    )
    harness = make_harness(config=config)
    await _charge_through_fc(harness)
    assert harness.executor.targets == []  # nothing written yet this DEVELOPMENT phase
    # First DEVELOPMENT tick actuates immediately (no prior actuation to pace
    # against).
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=12.0)]
    await harness.controller.tick()
    assert len(harness.executor.targets) == 1
    # Ticks at 1 s, 2 s, 3 s, 4 s after that (< the 5 s cadence): no new writes.
    for _ in range(4):
        harness.clock.advance(1.0)
        harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=12.0)]
        await harness.controller.tick()
    assert len(harness.executor.targets) == 1  # still just the first
    # The 5th second crosses the cadence: a new (lower) write is issued.
    harness.clock.advance(1.0)
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=12.0)]
    await harness.controller.tick()
    assert len(harness.executor.targets) == 2
    assert harness.executor.targets[1][0] < harness.executor.targets[0][0]


@pytest.mark.asyncio
async def test_post_fc_loop_rejected_write_does_not_advance_integrator_or_cadence() -> None:
    """State-advance-only-on-accepted-write: a REJECTed (rate-limited) tick does
    NOT advance the integrator/EMA or the cadence timer — the NEXT accepted
    write is computed from the SAME pre-reject state, so it produces the exact
    output a normal (non-interrupted) cadence sequence would have produced at
    its OWN first actuation (proven below by running both scenarios and
    comparing the resulting command)."""
    post_fc_kwargs: dict[str, object] = {
        "control_interval_seconds": 5.0,
        "kp_percent_per_ror": 1.0,
        "ki_percent_per_ror_second": 0.02,
    }
    ror_reading = reading(bean=185.0, bean_ror_c_per_min=12.0)

    # --- Control: a normal, never-rejected cadence sequence. ---
    normal_config = _post_fc_config(**post_fc_kwargs)
    normal_harness = make_harness(config=normal_config)
    await _charge_through_fc(normal_harness)
    normal_harness.reader.readings = [ror_reading]
    await normal_harness.controller.tick()
    assert len(normal_harness.executor.targets) == 1
    first_accepted_heat = normal_harness.executor.targets[0][0]

    # --- Scenario under test: the SAME sequence, but the first cadence tick
    # is REJECTed (rate-limited) before the loop ever gets an accepted write. ---
    config = _post_fc_config(**post_fc_kwargs)
    limits = SafetyLimits(min_seconds_between_commands=100.0)  # force a rate-limit REJECT
    harness = make_harness(config=config, limits=limits)
    await _charge_through_fc(harness)
    # Seed a fresh rate-limit baseline explicitly via the harness clock (the
    # pre-FC lever writes already set ``_last_command_monotonic``, so this
    # keeps the REJECT deterministic regardless of the pre-FC dwell length).
    harness.controller._last_command_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    harness.reader.readings = [ror_reading]
    harness.clock.advance(5.0)  # cadence elapsed
    await harness.controller.tick()
    assert len(harness.executor.targets) == 0  # command_rate_limited REJECT: no write
    rejects = [e for e in harness.sink.evaluations if e.verdict is SafetyVerdict.REJECT]
    assert rejects and rejects[-1].rule == "command_rate_limited"
    cadence_after_reject = harness.controller._post_fc_last_actuation_monotonic  # pyright: ignore[reportPrivateUsage]
    assert cadence_after_reject is None  # never advanced past the bumpless-reset None
    integrator_after_reject = harness.controller._post_fc_controller._integrator  # pyright: ignore[reportPrivateUsage]
    assert integrator_after_reject == pytest.approx(100.0 / 0.02)  # restored: handoff seed only

    # Advance past the rate limit and repeat the SAME reading: since the
    # rejected tick's state was fully undone, this next accepted write must
    # equal the control scenario's FIRST accepted write exactly.
    harness.clock.advance(100.0)
    harness.reader.readings = [ror_reading]
    await harness.controller.tick()
    assert len(harness.executor.targets) == 1
    assert harness.executor.targets[0][0] == first_accepted_heat


class _ArmableFlakySetTargetsExecutor(RecordingExecutor):
    """A ``RecordingExecutor`` whose ``set_targets`` raises exactly once per
    ``arm_next_failure()`` call, then delegates normally (mirrors the
    established ``FlakyExecutor``/``FailingTargetsExecutor`` patterns already
    used elsewhere in this module for actuator-failure tests).

    Armed explicitly (rather than a fixed ``fail_first`` call count) so a test
    can drive the harness through an arbitrary number of PRIOR writes (e.g. the
    pre-FC lever's retry-until-success writes inside ``_charge_through_fc``)
    without those consuming the scripted failure — only the tick AFTER
    ``arm_next_failure()`` fails.
    """

    def __init__(self, log: list[str]) -> None:
        super().__init__(log)
        self._armed = False

    def arm_next_failure(self) -> None:
        """The NEXT ``set_targets`` call raises; every other call succeeds."""
        self._armed = True

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        if self._armed:
            self._armed = False
            raise RuntimeError("serial write dropped")
        await super().set_targets(heat_percent=heat_percent, fan_percent=fan_percent)


@pytest.mark.asyncio
async def test_post_fc_loop_actuator_failure_does_not_advance_integrator_or_cadence() -> None:
    """Codex finding (fix round 2, #405 Slice B2): an ALLOW/CLAMP verdict whose
    ``set_targets`` call then raises (a transient actuator/serial failure)
    must NOT advance the PI integrator/EMA or the cadence timer — exactly like
    a REJECT. Proven the same way as the reject test: the accepted write AFTER
    the actuator failure must equal a normal (never-interrupted) scenario's
    first accepted write exactly, proving no phantom advance leaked through."""
    post_fc_kwargs: dict[str, object] = {
        "control_interval_seconds": 5.0,
        "kp_percent_per_ror": 1.0,
        "ki_percent_per_ror_second": 0.02,
    }
    ror_reading = reading(bean=185.0, bean_ror_c_per_min=12.0)

    # --- Control: a normal, never-failed cadence sequence. ---
    normal_config = _post_fc_config(**post_fc_kwargs)
    normal_harness = make_harness(config=normal_config)
    await _charge_through_fc(normal_harness)
    normal_harness.reader.readings = [ror_reading]
    await normal_harness.controller.tick()
    assert len(normal_harness.executor.targets) == 1
    first_accepted_heat = normal_harness.executor.targets[0][0]

    # --- Scenario under test: the SAME sequence, but the first cadence tick's
    # set_targets call raises (a transient actuator failure) — the verdict is
    # still ALLOW/CLAMP (never REJECT), so this exercises the NEW `executed`
    # check, not the pre-existing REJECT branch. ---
    config = _post_fc_config(**post_fc_kwargs)
    log: list[str] = []
    flaky_executor = _ArmableFlakySetTargetsExecutor(log)
    harness = make_harness(config=config, executor=flaky_executor)
    await _charge_through_fc(harness)  # the pre-FC lever write succeeds (not yet armed)
    # Seed a fresh rate-limit baseline explicitly via the harness clock (mirrors
    # the reject test): the pre-FC lever write already set
    # ``_last_command_monotonic``, so this keeps the intended tick's rate-limit
    # check independent of the pre-FC dwell length.
    harness.controller._last_command_monotonic = harness.clock.now  # pyright: ignore[reportPrivateUsage]
    flaky_executor.arm_next_failure()  # ONLY the upcoming DEVELOPMENT write fails
    harness.reader.readings = [ror_reading]
    harness.clock.advance(5.0)  # cadence elapsed
    await harness.controller.tick()
    assert harness.executor.targets == []  # the write never reached the roaster
    assert RoastEventKind.COMMAND_FAILED in harness.events.kinds()
    cadence_after_failure = harness.controller._post_fc_last_actuation_monotonic  # pyright: ignore[reportPrivateUsage]
    assert cadence_after_failure is None  # never advanced past the bumpless-reset None
    integrator_after_failure = harness.controller._post_fc_controller._integrator  # pyright: ignore[reportPrivateUsage]
    assert integrator_after_failure == pytest.approx(100.0 / 0.02)  # restored: handoff seed only

    # The NEXT tick's set_targets call succeeds (the one-shot arm was
    # consumed); since the failed tick's state was fully undone, this accepted
    # write must equal the control scenario's FIRST accepted write exactly —
    # proving the loop retries from the un-advanced state, not a
    # phantom-advanced one.
    harness.clock.advance(100.0)  # clear of both the rate limit and the cadence
    harness.reader.readings = [ror_reading]
    await harness.controller.tick()
    assert len(harness.executor.targets) == 1
    assert harness.executor.targets[0][0] == first_accepted_heat


@pytest.mark.asyncio
async def test_post_fc_loop_ror_unavailable_does_not_actuate() -> None:
    """RoR unavailable (``bean_ror_c_per_min is None``): the loop fails closed —
    it does not actuate, heat stays wherever it was."""
    config = _post_fc_config()
    harness = make_harness(config=config)
    await _charge_through_fc(harness)
    handoff_heat = harness.controller.snapshot().current_heat
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=None)]
    await harness.controller.tick()
    assert harness.executor.targets == []  # no write at all
    assert harness.controller.snapshot().current_heat == handoff_heat


@pytest.mark.asyncio
async def test_post_fc_loop_cannot_actuate_in_operator_recovery_required() -> None:
    """Restart/recovery: the loop cannot actuate in ``operator_recovery_required``
    — a restart never auto-resumes heat/fan, and the phase guard means the loop
    is inert there regardless of the flag."""
    config = _post_fc_config()
    harness = make_harness(readings=[reading(bean_ror_c_per_min=5.0)], config=config)
    harness.controller.load_profile(PROFILE)
    await harness.controller.recover_from_restart(RoastPhase.DEVELOPMENT)
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    await harness.controller._apply_deterministic_post_fc_levers(  # pyright: ignore[reportPrivateUsage]
        reading(bean_ror_c_per_min=5.0)
    )
    assert harness.executor.targets == []


@pytest.mark.asyncio
async def test_post_fc_loop_cannot_actuate_in_faulted() -> None:
    """Restart/recovery: the loop cannot actuate in ``faulted`` either."""
    config = _post_fc_config()
    harness = make_harness(readings=[reading(bean_ror_c_per_min=5.0)], config=config)
    harness.controller.load_profile(PROFILE)
    await harness.controller.recover_into_faulted(RoastPhase.FAULTED)
    assert harness.controller.phase is RoastPhase.FAULTED
    await harness.controller._apply_deterministic_post_fc_levers(  # pyright: ignore[reportPrivateUsage]
        reading(bean_ror_c_per_min=5.0)
    )
    assert harness.executor.targets == []


def test_set_heat_already_valid_in_development_no_matrix_change_needed() -> None:
    """The command×phase matrix already allows SET_HEAT in DEVELOPMENT (the
    advisor has always actuated heat there) — Slice B2 needs no change to
    ``COMMAND_PHASE_MATRIX``. This test pins that precondition so a future
    matrix edit cannot silently break the post-FC loop's ability to write."""
    assert RoastPhase.DEVELOPMENT in COMMAND_PHASE_MATRIX[RoastCommand.SET_HEAT]


# --- Safety-review fix (post-B2, Opus finding, MEDIUM): the loop must not
# engage from a phantom (non-bumpless) PI state on an
# ``operator_recovery_required -> DEVELOPMENT`` operator-resume edge; the
# advisor must resume driving post-FC heat/fan there instead. ---------------


@pytest.mark.asyncio
async def test_post_fc_loop_true_fc_edge_engages_the_loop() -> None:
    """A normal FC-edge DEVELOPMENT dwell sets ``_post_fc_engaged`` True — the
    precondition every other loop-actuation test in this module relies on."""
    config = _post_fc_config()
    harness = make_harness(config=config)
    await _charge_through_fc(harness)
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert harness.controller._post_fc_engaged is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_post_fc_engaged_clears_on_development_to_cooling() -> None:
    """``_post_fc_engaged`` does not survive past the DEVELOPMENT dwell it was
    set for — it clears the moment the phase leaves DEVELOPMENT (COOLING)."""
    config = _post_fc_config()
    harness = make_harness(config=config)
    await _charge_through_fc(harness)
    assert harness.controller._post_fc_engaged is True  # pyright: ignore[reportPrivateUsage]
    harness.controller.transition_to(RoastPhase.COOLING)
    assert harness.controller._post_fc_engaged is False  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_post_fc_desired_fan_clears_on_disengage_mirrors_post_fc_engaged() -> None:
    """#498 D88-C2 discipline: ``_post_fc_desired_fan_percent`` (the advisor's
    held loop-mode fan target) is per-engagement state exactly like
    ``_post_fc_engaged`` — it must NOT survive past the DEVELOPMENT dwell it
    was set for, so a LATER engagement (a subsequent roast, or any resume)
    never inherits a stale desired fan from an earlier one."""
    config = _post_fc_config(control_interval_seconds=5.0)
    advisor = FakeAdvisor([decision(heat=50, fan=60)], default_decision=decision(heat=50, fan=60))
    harness = make_harness(config=config, advisor=advisor)
    await _charge_through_fc(harness)
    assert harness.controller._post_fc_desired_fan_percent == 60  # pyright: ignore[reportPrivateUsage]
    harness.controller.transition_to(RoastPhase.COOLING)
    assert harness.controller._post_fc_desired_fan_percent is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_post_fc_loop_does_not_engage_on_operator_resume_into_development() -> None:
    """The MEDIUM finding this fixes: a restart -> recovery -> operator-resume
    sequence also reaches DEVELOPMENT, but NOT via the true FC edge — no
    bumpless seed ran, so the loop must NOT engage from that phantom PI state.
    Instead the advisor resumes driving post-FC heat/fan (the pre-B2
    fallback), so post-FC control is never silently absent after a resume."""
    config = _post_fc_config()
    advisor = FakeAdvisor([decision(heat=61, fan=52)], default_decision=decision(heat=61, fan=52))
    harness = make_harness(config=config, advisor=advisor)
    harness.controller.load_profile(PROFILE)

    await harness.controller.recover_from_restart(RoastPhase.DEVELOPMENT)
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    harness.controller.operator_resume(RoastPhase.DEVELOPMENT)
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert harness.controller._post_fc_engaged is False  # pyright: ignore[reportPrivateUsage]
    assert harness.executor.targets == []  # resume itself never writes hardware

    harness.log.clear()
    harness.events.events.clear()
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=5.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()

    # The PI loop did NOT write (engaged False, regardless of the flag being on).
    # The advisor's levers DID actuate — the resume fallback is restored.
    assert advisor.contexts, "the advisor should have been consulted on resume"
    assert harness.executor.targets == [(61, 52)]
    assert harness.controller.snapshot().current_heat == 61
    assert harness.controller.snapshot().current_fan == 52
    # #498: the resume path is NOT loop mode, so the desired-fan state is
    # never touched — the advisor's fan actuated DIRECTLY above, exactly as
    # baseline, with no desired-fan indirection involved.
    assert harness.controller._post_fc_desired_fan_percent is None  # pyright: ignore[reportPrivateUsage]


# --- D84 (#405 Slice C): deterministic drop anchor + LLM-earlier-only ---


@pytest.mark.asyncio
async def test_deterministic_drop_anchor_flag_off_is_a_regression_guard() -> None:
    """Flag OFF (default): the anchor never fires — a run reaching
    bean >= target_drop AND dev% >= target with the advisor SILENT does NOT
    auto-drop (today's fully advisor-only drop is unaffected)."""
    advisor = FakeAdvisor([decision(heat=60, fan=50, drop=False)])
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent, advisor=advisor
    )
    assert harness.controller._config.post_first_crack_control.enabled is False  # pyright: ignore[reportPrivateUsage]
    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=5.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert "drop_beans" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_deterministic_drop_anchor_fires_with_advisor_silent() -> None:
    """Flag ON + engaged, advisor SILENT (``should_drop=False``): the anchor
    fires ``drop_beans`` and transitions to COOLING once
    bean_temp >= target_drop_temp_c AND system_dev% >= target_development_percent."""
    config = _post_fc_config()
    advisor = FakeAdvisor([decision(heat=60, fan=50, drop=False)])
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent, advisor=advisor, config=config
    )
    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=5.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.executor.commands.count("drop_beans") == 1
    assert harness.controller.phase is RoastPhase.COOLING
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.DEVELOPMENT_TARGET.value,
    } in executed
    # qa finding: the shared drop path must be AUDITABLE — the drop routes
    # through evaluate_drop_recommendation and the verdict is persisted like
    # every other roaster write (#167). Pin a real evaluation row exists for
    # this drop, not merely that the command reached the executor.
    drop_evals = [e for e in harness.sink.evaluations if e.rule == "drop_eligibility"]
    assert drop_evals and drop_evals[-1].verdict is SafetyVerdict.ALLOW


@pytest.mark.asyncio
async def test_deterministic_drop_anchor_does_not_fire_on_temp_alone() -> None:
    """Temp >= target but dev% < target: the anchor does NOT fire (both
    conditions are required)."""
    config = _post_fc_config()
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent - 5.0, config=config
    )
    harness.reader.readings = [
        reading(bean=PROFILE.target_drop_temp_c + 5.0, bean_ror_c_per_min=5.0)
    ]
    await harness.controller.tick()
    assert "drop_beans" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_deterministic_drop_anchor_does_not_fire_on_dev_percent_alone() -> None:
    """Dev% >= target but temp < target: the anchor does NOT fire (both
    conditions are required)."""
    config = _post_fc_config()
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent + 5.0, config=config
    )
    harness.reader.readings = [
        reading(bean=PROFILE.target_drop_temp_c - 5.0, bean_ror_c_per_min=5.0)
    ]
    await harness.controller.tick()
    assert "drop_beans" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_deterministic_drop_anchor_uses_system_dev_percent_not_advisor_claim() -> None:
    """The anchor uses the SYSTEM dev% (:meth:`_development_percent`), never the
    advisor's claimed number: an advisor claiming a high dev% while the
    SYSTEM's real dev% is below target does not trigger the anchor."""
    config = _post_fc_config()
    advisor = FakeAdvisor(
        [decision(heat=60, fan=50, drop=False)],
        default_decision=decision(heat=60, fan=50, drop=False),
    )
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent - 5.0,
        advisor=advisor,
        config=config,
    )
    # Bean temp is at/above target, so temp alone would satisfy the anchor —
    # only the (below-target) SYSTEM dev% should block it, regardless of
    # anything an advisor might claim about its own view of development.
    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=5.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert "drop_beans" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_deterministic_drop_anchor_llm_earlier_only_advisor_drops_first() -> None:
    """LLM-earlier still works: with the flag ON, an advisor ``should_drop=True``
    at dev% within [target - margin, target) drops EARLIER than the anchor —
    the existing #313 coherence path is intact and unmodified."""
    config = _post_fc_config()
    margin = ControllerConfig().drop_dev_margin_percent
    within_margin_dev_percent = PROFILE.target_development_percent - margin / 2.0
    advisor = FakeAdvisor(
        [decision(heat=60, fan=50, drop=True)],
        default_decision=decision(heat=60, fan=50, drop=True),
    )
    harness = _development_harness_with_dev_percent(
        system_dev_percent=within_margin_dev_percent, advisor=advisor, config=config
    )
    # Bean temp is BELOW target_drop_temp_c, so the anchor itself could not
    # have fired this tick — only the advisor's earlier-drop coherence path
    # can produce the drop here.
    harness.reader.readings = [
        reading(bean=PROFILE.target_drop_temp_c - 10.0, bean_ror_c_per_min=5.0)
    ]
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.executor.commands.count("drop_beans") == 1
    assert harness.controller.phase is RoastPhase.COOLING
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {"command": "drop_beans", "source": "advisor"} in executed


@pytest.mark.asyncio
async def test_deterministic_drop_anchor_does_not_fire_when_not_engaged() -> None:
    """``_post_fc_engaged`` False with BOTH eligibility conditions otherwise
    met (a non-``None`` system dev% at/above target, bean temp at/above
    target): the anchor does NOT fire — the advisor drives post-FC instead,
    consistent with the RoR loop's own gate.

    Isolates the ``not self._post_fc_engaged`` clause specifically (qa
    mutation finding): the harness first drives a normal run to DEVELOPMENT
    via the TRUE FC edge (``_development_harness_with_dev_percent``), which
    stamps the charge/FC clocks — so ``_development_percent()`` is genuinely
    non-``None`` and at target here, unlike the operator-resume path (which
    never stamps those clocks and would trip the SEPARATE None-dev fail-closed
    branch instead, masking this guard). ``_post_fc_engaged`` is then poked
    directly to ``False`` (the same private-poke style the None-dev test uses
    on ``_charge_monotonic``) so this test exercises ONLY the engaged check:
    deleting the ``not self._post_fc_engaged`` clause from the guard makes
    this test fail.
    """
    config = _post_fc_config()
    advisor = FakeAdvisor([decision(heat=60, fan=50, drop=False)])
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent, advisor=advisor, config=config
    )
    harness.controller._post_fc_engaged = False  # pyright: ignore[reportPrivateUsage]
    assert harness.controller._development_percent() == pytest.approx(  # pyright: ignore[reportPrivateUsage]
        PROFILE.target_development_percent
    )
    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=5.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert "drop_beans" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_deterministic_drop_anchor_no_op_when_development_percent_is_none() -> None:
    """``_development_percent()`` returning ``None`` ⇒ no anchor drop — fail
    safe, never drop on unknown development.

    This guard is defensive: in a normally-progressing engaged DEVELOPMENT
    dwell ``_development_percent()`` is never ``None`` (the charge/FC clocks
    are always stamped together by the true FC edge that sets
    ``_post_fc_engaged``). The unreachable-by-normal-transitions precondition
    (``_charge_monotonic`` cleared) is forced directly, mirroring the existing
    test-harness pattern of poking the charge clock (see
    ``_development_harness_with_dev_percent``), to prove the guard itself
    fails closed rather than assuming it.
    """
    config = _post_fc_config()
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent, config=config
    )
    harness.controller._charge_monotonic = None  # pyright: ignore[reportPrivateUsage]
    assert harness.controller._development_percent() is None  # pyright: ignore[reportPrivateUsage]
    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=5.0)]
    await harness.controller.tick()
    assert "drop_beans" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_operator_drop_beans_still_works_unmodified_in_development() -> None:
    """Manual ``operator_drop_beans`` stays un-gated in DEVELOPMENT — the
    backstop is unaffected by the deterministic anchor, flag on or off."""
    config = _post_fc_config()
    harness = _development_harness_with_dev_percent(system_dev_percent=1.0, config=config)
    # Neither anchor condition is met (dev% far below target, bean temp at the
    # harness default), yet the manual drop still executes immediately.
    await harness.controller.operator_drop_beans()
    assert "drop_beans" in harness.executor.commands
    assert harness.controller.phase is RoastPhase.COOLING
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {"command": "drop_beans", "source": "operator"} in executed


@pytest.mark.asyncio
async def test_deterministic_drop_anchor_emits_command_failed_on_actuator_failure() -> None:
    """A transient actuator failure (``drop_beans`` raises) emits
    ``COMMAND_FAILED`` and does NOT transition to COOLING — mirroring the
    advisor-drop and operator-drop failure handling exactly (fail closed, no
    FSM/hardware divergence)."""

    class RaisingDropExecutor(RecordingExecutor):
        async def drop_beans(self) -> AppliedRoasterState:
            raise RuntimeError("serial write dropped")

    config = _post_fc_config()
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent,
        config=config,
        executor=RaisingDropExecutor(),
    )
    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=5.0)]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.DEVELOPMENT_TARGET.value,
    } in failed
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {"command": "drop_beans", "source": "policy"} not in executed


# --- D88 amendment A1/A2 (#405 Slice C2): the decoupled ceiling-guard drop ---


@pytest.mark.asyncio
async def test_ceiling_guard_flag_off_is_a_regression_guard() -> None:
    """Flag OFF (default): the guard never fires — a bean at/above
    ``ceiling_guard_temp_c`` in DEVELOPMENT does NOT auto-drop (today's fully
    advisor-owned 196 °C boundary is unaffected)."""
    advisor = FakeAdvisor([decision(heat=0, fan=80, drop=False)])
    harness = harness_in_development(readings=[reading()], advisor=advisor)
    assert harness.controller._config.post_first_crack_control.ceiling_guard_drop_enabled is False  # pyright: ignore[reportPrivateUsage]
    harness.reader.readings = [reading(bean=199.0, bean_ror_c_per_min=5.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert "drop_beans" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_ceiling_guard_fires_with_ror_loop_off() -> None:
    """The A1 point: the guard fires at bean >= ceiling_guard_temp_c in
    DEVELOPMENT with the RoR-taper loop OFF (its own flag defaults False) —
    the decoupling IS the feature. Uses the SAME safety path as every drop
    (``evaluate_drop_recommendation`` + the executor) and carries the typed
    ``DropReason.CEILING_GUARD`` (not a bare string) in the event payload."""
    config = _ceiling_guard_config(ceiling_guard_temp_c=196.0)
    assert config.post_first_crack_control.enabled is False  # the RoR-taper loop stays OFF
    advisor = FakeAdvisor([decision(heat=0, fan=80, drop=False)])
    harness = harness_in_development(readings=[reading()], advisor=advisor, config=config)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=5.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.executor.commands.count("drop_beans") == 1
    assert harness.controller.phase is RoastPhase.COOLING
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in executed
    # qa finding: the guard shares the SAME safety path as every other drop
    # (evaluate_drop_recommendation, persisted like every roaster write,
    # #167) — pin a real evaluation row exists, not just that the command
    # reached the executor.
    drop_evals = [e for e in harness.sink.evaluations if e.rule == "drop_eligibility"]
    assert drop_evals and drop_evals[-1].verdict is SafetyVerdict.ALLOW


@pytest.mark.asyncio
async def test_ceiling_guard_does_not_fire_below_the_ceiling() -> None:
    """Bean strictly below ``ceiling_guard_temp_c``: the guard does not fire,
    even with the flag on."""
    config = _ceiling_guard_config(ceiling_guard_temp_c=196.0)
    harness = harness_in_development(readings=[reading()], config=config)
    harness.reader.readings = [reading(bean=195.9, bean_ror_c_per_min=5.0)]
    await harness.controller.tick()
    assert "drop_beans" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.DEVELOPMENT


@pytest.mark.asyncio
async def test_ceiling_guard_fires_after_a_recovery_resume_into_development() -> None:
    """The A1 point again, from the other direction: the guard fires even on
    a restart -> recovery -> operator-resume sequence into DEVELOPMENT, where
    ``_post_fc_engaged`` is False (mirrors
    ``test_post_fc_loop_does_not_engage_on_operator_resume_into_development``'s
    setup) — the guard's own gate never reads ``_post_fc_engaged`` at all, so
    a post-recovery resume is not a gap in bitter-line protection the way it
    would be for the RoR-taper loop and the D84 dev%/temp anchor (both
    deliberately inert on this exact edge)."""
    config = _ceiling_guard_config(ceiling_guard_temp_c=196.0)
    advisor = FakeAdvisor(
        [decision(heat=0, fan=80, drop=False)],
        default_decision=decision(heat=0, fan=80, drop=False),
    )
    harness = make_harness(config=config, advisor=advisor)
    harness.controller.load_profile(PROFILE)

    await harness.controller.recover_from_restart(RoastPhase.DEVELOPMENT)
    assert harness.controller.phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    harness.controller.operator_resume(RoastPhase.DEVELOPMENT)
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert harness.controller._post_fc_engaged is False  # pyright: ignore[reportPrivateUsage]

    harness.log.clear()
    harness.events.events.clear()
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=5.0)]
    harness.controller.request_advisory()
    await harness.controller.tick()

    assert harness.executor.commands.count("drop_beans") == 1
    assert harness.controller.phase is RoastPhase.COOLING
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in executed


@pytest.mark.asyncio
async def test_ceiling_guard_does_not_fire_outside_development() -> None:
    """The guard is gated on phase DEVELOPMENT — a bean at/above the ceiling
    in ROASTING_PRE_FIRST_CRACK does not trigger it (the guard is a
    post-FC/DEVELOPMENT anchor only, matching D88's scope)."""
    config = _ceiling_guard_config(ceiling_guard_temp_c=196.0)
    harness = make_harness(config=config)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:3]:  # …→ ROASTING_PRE_FIRST_CRACK
        harness.controller.transition_to(step)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=5.0)]
    await harness.controller.tick()
    assert "drop_beans" not in harness.executor.commands
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK


@pytest.mark.asyncio
async def test_ceiling_guard_emits_command_failed_on_actuator_failure() -> None:
    """A transient actuator failure (``drop_beans`` raises) emits
    ``COMMAND_FAILED`` (with the typed ceiling-guard reason) and does NOT
    transition to COOLING — mirroring the D84 anchor's failure handling
    exactly, since both share :meth:`RoastController._execute_deterministic_drop`."""

    class RaisingDropExecutor(RecordingExecutor):
        async def drop_beans(self) -> AppliedRoasterState:
            raise RuntimeError("serial write dropped")

    config = _ceiling_guard_config(ceiling_guard_temp_c=196.0)
    harness = make_harness(config=config, executor=RaisingDropExecutor())
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT
        harness.controller.transition_to(step)
    harness.log.clear()
    harness.events.events.clear()
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=5.0)]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in failed
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {"command": "drop_beans", "source": "policy"} not in executed


@pytest.mark.asyncio
async def test_ceiling_guard_takes_precedence_over_the_dev_percent_anchor_same_tick() -> None:
    """Both the ceiling guard AND the D84 dev%/temp anchor are eligible the
    SAME tick (bean above the guard's ceiling, dev% at/above target, both
    flags on) — the guard runs first each tick (see the ``tick()`` ordering
    comment), fires, and transitions to COOLING; the anchor's own call the
    same tick then sees phase is no longer DEVELOPMENT and no-ops. Exactly
    ONE ``drop_beans`` reaches the executor, tagged with the GUARD's reason,
    not the anchor's.

    ``enabled=True`` (the RoR-taper/anchor flag) is DELIBERATE here (qa
    finding): with it False the anchor's own first gate
    (``not config.enabled``) already short-circuits it before eligibility is
    ever checked, so the test cannot distinguish "the guard ran first" from
    "the anchor was never in play at all" — an ordering swap in ``tick()``
    (guard after the anchor instead of before) would then pass this test
    for the wrong reason. With the anchor flag True AND
    ``_development_harness_with_dev_percent`` walking the TRUE FC edge
    (``_post_fc_engaged`` True) AND dev% stamped at target, the anchor's
    FULL eligibility bundle is genuinely satisfied this tick, so an ordering
    swap is caught: the anchor would fire (and transition to COOLING) BEFORE
    the guard ever runs, tagging the drop ``DEVELOPMENT_TARGET`` instead of
    ``CEILING_GUARD`` — this test's assertions on the exact reason value
    catch that."""
    config = _post_fc_config(
        ceiling_guard_drop_enabled=True, ceiling_guard_temp_c=196.0, enabled=True
    )
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent, config=config
    )
    harness.reader.readings = [
        reading(bean=max(196.0, PROFILE.target_drop_temp_c), bean_ror_c_per_min=5.0)
    ]
    await harness.controller.tick()
    assert harness.executor.commands.count("drop_beans") == 1
    assert harness.controller.phase is RoastPhase.COOLING
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in executed
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.DEVELOPMENT_TARGET.value,
    } not in executed
    # qa finding: the shared drop path must be AUDITABLE — pin a real
    # ALLOW evaluation exists for the drop that actually fired (the guard's),
    # not just that the reason-tagged event landed.
    drop_evals = [e for e in harness.sink.evaluations if e.rule == "drop_eligibility"]
    assert drop_evals and drop_evals[-1].verdict is SafetyVerdict.ALLOW


# --- #507: drop/emergency_stop adopt the applied heat/fan into the mirrors ---
#
# roastpilot-agent#507 (supersedes coffee-roaster-mcp#189, closed wrong-premise):
# _current_heat/_current_fan — the mirrors ControllerSnapshot.current_heat/
# current_fan expose, which telemetry_snapshots rows and the SSE frame read —
# were never updated on ANY drop or emergency_stop path, so they held the last
# pre-drop set_targets values (91/40 in the roast 12 trace) through all of
# cooling. The fix adopts the executor's returned AppliedRoasterState (sourced
# from the MCP command's own event payload) into the mirrors, exactly once,
# only after the write is confirmed.

#: A deliberately NON-default applied state distinct from every existing
#: fixture's pre-drop set_targets values (65/50, 100/30, …) and from the
#: driver's own real 0/100 constant — proves the controller actually ADOPTS
#: whatever the executor returns rather than a coincidental match or a
#: hardcoded 0/100 (the #507 direction: "never hardcode the driver's
#: post-drop constants in the controller").
_DISTINCTIVE_APPLIED_STATE = AppliedRoasterState(
    heat_level_percent=7, fan_level_percent=88, cooling_on=True
)


def _development_harness_with_executor(
    *,
    readings: list[RoastTelemetry | None | Exception],
    advisor: RoastAdvisor | None,
    executor: RecordingExecutor,
) -> Harness:
    """A DEVELOPMENT harness with an explicit (non-default) executor.

    Mirrors :func:`harness_in_development` exactly — that helper does not
    accept an ``executor`` override, so this rebuilds its body via
    :func:`make_harness` directly, which does."""
    harness = make_harness(readings=readings, advisor=advisor, executor=executor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT
        harness.controller.transition_to(step)
    harness.log.clear()
    harness.events.events.clear()
    return harness


@pytest.mark.asyncio
async def test_advisor_drop_adopts_applied_state_into_mirrors() -> None:
    """The roast-12 trace pattern, reproduced and pinned: pre-drop the mirrors
    hold the last commanded heat/fan (65/50, an ordinary advisory write);
    after an advisor-triggered drop they must show the DRIVER's applied
    state, not the stale pre-drop command — the same tick the drop lands,
    not "a few ticks later". ``cooling_on`` is scripted True on the drop
    tick's reading (mirroring the real MCP, which reports the fresh
    post-drop device state the SAME poll the agent reads it — see
    ``_read_current_driver_state`` + ``get_roast_state`` in mcp_server.py)
    to reproduce the roast-12 divergence directly: real ``cooling_on`` true
    alongside the (pre-fix stale / post-fix adopted) heat/fan mirrors."""
    executor = RecordingExecutor(drop_applied_state=_DISTINCTIVE_APPLIED_STATE)
    advisor = FakeAdvisor([decision(heat=65, fan=50, drop=False), decision(drop=True)])
    harness = _development_harness_with_executor(
        readings=[reading()], advisor=advisor, executor=executor
    )

    harness.controller.request_advisory()
    await harness.controller.tick()  # pre-drop: mirrors land at 65/50
    assert harness.controller.snapshot().current_heat == 65
    assert harness.controller.snapshot().current_fan == 50

    harness.reader.readings = [reading(cooling_on=True)]
    harness.controller.request_advisory()
    await harness.controller.tick()  # the drop tick
    assert harness.controller.phase is RoastPhase.COOLING
    assert "drop_beans" in executor.commands
    # The mirrors now show the DRIVER's applied state (7/88), not the stale
    # pre-drop 65/50 — proving genuine adoption of a non-default, non-0/100
    # value, not a hardcoded constant or a coincidental pass-through.
    assert harness.controller.snapshot().current_heat == 7
    assert harness.controller.snapshot().current_fan == 88
    # cooling_on is unaffected (it was already correct pre-fix, sourced live
    # from the per-tick MCP read, never from the mirrors) — assert it stays
    # true so a regression there would also be caught here (the roast-12
    # trace's "cooling_on=1 alongside stale heat/fan" divergence, now closed).
    telemetry = harness.controller.snapshot().telemetry
    assert telemetry is not None
    assert telemetry.cooling_on is True


@pytest.mark.asyncio
async def test_operator_drop_adopts_applied_state_into_mirrors() -> None:
    """The operator-drop path (:meth:`RoastController.operator_drop_beans`)
    adopts the applied state exactly like the advisor path — the fix routes
    through the ONE shared ``_adopt_applied_state`` helper, but this pins
    that per-path wiring directly rather than relying on the advisor test
    alone to cover it."""
    executor = RecordingExecutor(drop_applied_state=_DISTINCTIVE_APPLIED_STATE)
    harness = _development_harness_with_executor(
        readings=[reading()], advisor=None, executor=executor
    )
    harness.controller.request_advisory()
    await harness.controller.tick()  # ordinary tick: mirrors land at the profile floor
    heat_before = harness.controller.snapshot().current_heat
    fan_before = harness.controller.snapshot().current_fan
    assert (heat_before, fan_before) != (7, 88)  # sanity: distinct from the applied state

    await harness.controller.operator_drop_beans()
    assert harness.controller.phase is RoastPhase.COOLING
    assert harness.controller.snapshot().current_heat == 7
    assert harness.controller.snapshot().current_fan == 88


@pytest.mark.asyncio
async def test_deterministic_drop_anchor_adopts_applied_state_into_mirrors() -> None:
    """The deterministic drop anchor (D84, :meth:`_maybe_deterministic_drop`,
    shared with the ceiling guard via :meth:`_execute_deterministic_drop`)
    adopts the applied state exactly like the advisor/operator paths."""
    executor = RecordingExecutor(drop_applied_state=_DISTINCTIVE_APPLIED_STATE)
    config = _post_fc_config()
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent,
        config=config,
        executor=executor,
    )
    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=5.0)]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.COOLING
    assert "drop_beans" in executor.commands
    assert harness.controller.snapshot().current_heat == 7
    assert harness.controller.snapshot().current_fan == 88


@pytest.mark.asyncio
async def test_ceiling_guard_drop_adopts_applied_state_into_mirrors() -> None:
    """The decoupled ceiling-guard drop (#405 D88 amendment A1,
    :meth:`_maybe_ceiling_guard_drop`) adopts the applied state exactly like
    every other drop path — same shared executor/adoption call."""
    executor = RecordingExecutor(drop_applied_state=_DISTINCTIVE_APPLIED_STATE)
    config = _ceiling_guard_config(ceiling_guard_temp_c=196.0)
    harness = make_harness(config=config, executor=executor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT
        harness.controller.transition_to(step)
    harness.log.clear()
    harness.events.events.clear()
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=5.0)]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.COOLING
    assert "drop_beans" in executor.commands
    assert harness.controller.snapshot().current_heat == 7
    assert harness.controller.snapshot().current_fan == 88


@pytest.mark.asyncio
async def test_emergency_stop_adopts_applied_state_into_mirrors() -> None:
    """The direct hard-ceiling e-stop path (:meth:`_act_on_safety`) adopts the
    applied state into the mirrors — the same #412-shaped gap as the drop
    paths, on a completely different write (``emergency_stop``, not
    ``drop_beans``)."""
    executor = RecordingExecutor(emergency_stop_applied_state=_DISTINCTIVE_APPLIED_STATE)
    over_ceiling = reading(bean=235.0, env=200.0, age_seconds=0.0)  # > 230 hard ceiling
    harness = make_harness(readings=[over_ceiling], executor=executor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT
        harness.controller.transition_to(step)
    harness.log.clear()
    harness.events.events.clear()

    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.FAULTED
    assert len(executor.estop_reasons) == 1
    assert harness.controller.snapshot().current_heat == 7
    assert harness.controller.snapshot().current_fan == 88


@pytest.mark.asyncio
async def test_escalation_emergency_stop_adopts_applied_state_into_mirrors() -> None:
    """The latched-escalation e-stop path (:meth:`_escalate_to_emergency_stop`,
    reached from a FAULT/RECOVERY latch on a strictly-more-severe re-read)
    adopts the applied state too — the second of the two ``emergency_stop``
    call sites the fix touches."""
    executor = RecordingExecutor(emergency_stop_applied_state=_DISTINCTIVE_APPLIED_STATE)
    stale_low = reading(bean=180.0, env=200.0, age_seconds=10.0)  # stale → FAULT (latched)
    over_ceiling = reading(bean=235.0, env=200.0, age_seconds=0.0)  # > 230 → escalate
    harness = make_harness(readings=[stale_low, over_ceiling], executor=executor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT
        harness.controller.transition_to(step)
    harness.log.clear()
    harness.events.events.clear()

    await harness.controller.tick()  # entry: stale FAULT → FAULTED (latched FAULT)
    assert harness.controller.phase is RoastPhase.FAULTED
    assert executor.estop_reasons == []  # no hardware e-stop yet (heat-off via set_targets)

    await harness.controller.tick()  # escalation: hard-ceiling breach → e-stop
    assert len(executor.estop_reasons) == 1
    assert harness.controller.snapshot().current_heat == 7
    assert harness.controller.snapshot().current_fan == 88


# --- #507 / #412 discipline: a FAILED write must NOT advance the mirrors ---


@pytest.mark.asyncio
async def test_failed_advisor_drop_does_not_advance_mirrors() -> None:
    """A ``drop_beans`` write that raises must leave the mirrors exactly where
    they were before the attempt — mirrors the #412 told==enforced discipline
    (a failed write is never treated as if it landed)."""
    advisor = FakeAdvisor([decision(heat=65, fan=50, drop=False), decision(drop=True)])
    executor = FailingCommandExecutor({"drop_beans"})
    harness = _development_harness_with_executor(
        readings=[reading()], advisor=advisor, executor=executor
    )
    harness.controller.request_advisory()
    await harness.controller.tick()  # pre-drop write lands: mirrors at 65/50
    assert harness.controller.snapshot().current_heat == 65
    assert harness.controller.snapshot().current_fan == 50

    harness.controller.request_advisory()
    await harness.controller.tick()  # the drop attempt raises
    assert harness.controller.phase is RoastPhase.DEVELOPMENT  # never transitioned
    assert RoastEventKind.COMMAND_FAILED in harness.events.kinds()
    # Mirrors held exactly at their pre-attempt values — not advanced to any
    # applied/adopted state, and not zeroed either.
    assert harness.controller.snapshot().current_heat == 65
    assert harness.controller.snapshot().current_fan == 50


@pytest.mark.asyncio
async def test_failed_operator_drop_does_not_advance_mirrors() -> None:
    executor = FailingCommandExecutor({"drop_beans"})
    harness = _development_harness_with_executor(
        readings=[reading()], advisor=None, executor=executor
    )
    harness.controller.request_advisory()
    await harness.controller.tick()  # ordinary tick lands the profile floor
    heat_before = harness.controller.snapshot().current_heat
    fan_before = harness.controller.snapshot().current_fan

    await harness.controller.operator_drop_beans()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT  # never transitioned
    assert harness.controller.snapshot().current_heat == heat_before
    assert harness.controller.snapshot().current_fan == fan_before


@pytest.mark.asyncio
async def test_failed_deterministic_drop_anchor_does_not_advance_mirrors() -> None:
    """Mirrors :func:`test_deterministic_drop_anchor_emits_command_failed_on_actuator_failure`
    (the pre-existing phase/event coverage) with the #507 mirror assertion added."""

    class RaisingDropExecutor(RecordingExecutor):
        async def drop_beans(self) -> AppliedRoasterState:
            raise RuntimeError("serial write dropped")

    config = _post_fc_config()
    executor = RaisingDropExecutor()
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent,
        config=config,
        executor=executor,
    )
    heat_before = harness.controller.snapshot().current_heat
    fan_before = harness.controller.snapshot().current_fan

    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT  # never transitioned
    assert harness.controller.snapshot().current_heat == heat_before
    assert harness.controller.snapshot().current_fan == fan_before


@pytest.mark.asyncio
async def test_failed_emergency_stop_does_not_advance_mirrors() -> None:
    """Mirrors :func:`test_failed_emergency_stop_still_faults` with the #507
    mirror assertion added: a raising ``emergency_stop`` still faults closed
    via the ``set_targets`` fail-safe retry latch, but the mirrors must not
    ALSO be advanced from the (failed) ``emergency_stop`` call itself."""

    class FailingEstopExecutor(RecordingExecutor):
        async def emergency_stop(self, *, reason: str) -> AppliedRoasterState:
            raise RuntimeError("serial port dead")

    over_ceiling = reading(bean=235.0, env=200.0, age_seconds=0.0)
    executor = FailingEstopExecutor()
    harness = make_harness(readings=[over_ceiling], executor=executor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT
        harness.controller.transition_to(step)
    harness.log.clear()
    harness.events.events.clear()

    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.FAULTED
    assert RoastEventKind.COMMAND_FAILED in harness.events.kinds()
    # The failed emergency_stop must not have advanced the mirrors to the
    # driver's real applied state — only the SEPARATE set_targets fail-safe
    # retry (heat 0 / safe fan) may do that, on its own successful write.
    heat_after = harness.controller.snapshot().current_heat
    fan_after = harness.controller.snapshot().current_fan
    assert (heat_after, fan_after) != (7, 88)


# --- #507 safety-review MEDIUM: a hardware-successful drop/e-stop whose
# result payload cannot be parsed must NOT diverge the FSM from physical
# reality. mcp_client.RoasterControlAdapter degrades a malformed payload to
# ``None`` (never raises) — these tests drive the CONTROLLER side of that
# contract: a ``None`` applied state is a genuine no-op, indistinguishable
# from a normal success except that the mirrors are not adopted. The
# hardware already dropped/stopped, so the caller must transition, emit
# COMMAND_EXECUTED, and (for drop) NEVER re-fire drop_beans on the next tick.


@pytest.mark.asyncio
async def test_advisor_drop_with_malformed_payload_still_transitions_and_does_not_refire() -> None:
    """A drop whose result payload is unparseable (``applied=None``) still
    transitions to COOLING and emits COMMAND_EXECUTED — the drop physically
    happened — and the mirrors simply stay at their pre-drop values
    (stale-but-honest) rather than raising or advancing to a fabricated
    value. A follow-up tick must not re-fire ``drop_beans`` (COOLING is not
    an advisory phase, so the advisor drop path is naturally inert there —
    this pins that it stays inert, not just that it happens to today)."""
    executor = RecordingExecutor(drop_applied_state=None)
    advisor = FakeAdvisor([decision(heat=65, fan=50, drop=False), decision(drop=True)])
    harness = _development_harness_with_executor(
        readings=[reading()], advisor=advisor, executor=executor
    )

    harness.controller.request_advisory()
    await harness.controller.tick()  # pre-drop: mirrors land at 65/50
    assert harness.controller.snapshot().current_heat == 65
    assert harness.controller.snapshot().current_fan == 50

    harness.controller.request_advisory()
    await harness.controller.tick()  # the drop tick: applied=None (malformed payload)
    assert harness.controller.phase is RoastPhase.COOLING  # the drop DID happen
    assert executor.commands.count("drop_beans") == 1
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {"command": "drop_beans", "source": "advisor"} in executed
    assert RoastEventKind.COMMAND_FAILED not in harness.events.kinds()
    # Mirrors held at their pre-drop values — not advanced, not zeroed, not
    # a crash — because there was nothing valid to adopt.
    assert harness.controller.snapshot().current_heat == 65
    assert harness.controller.snapshot().current_fan == 50

    # A follow-up tick must NOT re-fire drop_beans (would be an FSM/hardware
    # divergence: the machine already dropped).
    await harness.controller.tick()
    assert executor.commands.count("drop_beans") == 1


@pytest.mark.asyncio
async def test_operator_drop_with_malformed_payload_still_transitions() -> None:
    """The operator-drop path handles ``applied=None`` identically to the
    advisor path — transitions, emits COMMAND_EXECUTED, mirrors unchanged."""
    executor = RecordingExecutor(drop_applied_state=None)
    harness = _development_harness_with_executor(
        readings=[reading()], advisor=None, executor=executor
    )
    harness.controller.request_advisory()
    await harness.controller.tick()
    heat_before = harness.controller.snapshot().current_heat
    fan_before = harness.controller.snapshot().current_fan

    await harness.controller.operator_drop_beans()
    assert harness.controller.phase is RoastPhase.COOLING
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {"command": "drop_beans", "source": "operator"} in executed
    assert RoastEventKind.COMMAND_FAILED not in harness.events.kinds()
    assert harness.controller.snapshot().current_heat == heat_before
    assert harness.controller.snapshot().current_fan == fan_before


@pytest.mark.asyncio
async def test_deterministic_drop_anchor_with_malformed_payload_still_transitions() -> None:
    """The deterministic drop anchor handles ``applied=None`` identically."""
    executor = RecordingExecutor(drop_applied_state=None)
    config = _post_fc_config()
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent,
        config=config,
        executor=executor,
    )
    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=5.0)]

    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.COOLING
    assert "drop_beans" in executor.commands
    assert RoastEventKind.COMMAND_FAILED not in harness.events.kinds()
    # The SAME tick's RoR-loop set_targets write (D82/D84 ordering: the loop
    # runs BEFORE the anchor) still lands normally — that write is unrelated
    # to the drop's own applied-state parsing. The mirrors must equal exactly
    # THAT loop write, proving the drop's None did not additionally zero or
    # otherwise perturb them beyond what the loop itself set this tick.
    assert executor.targets, "the RoR loop must have written this tick"
    loop_heat, loop_fan = executor.targets[-1]
    assert harness.controller.snapshot().current_heat == loop_heat
    assert harness.controller.snapshot().current_fan == loop_fan

    # No re-fire on the next tick.
    drop_count_before = executor.commands.count("drop_beans")
    await harness.controller.tick()
    assert executor.commands.count("drop_beans") == drop_count_before


@pytest.mark.asyncio
async def test_emergency_stop_with_malformed_payload_still_faults() -> None:
    """A hardware-successful e-stop whose result payload is unparseable
    (``applied=None``) still faults closed and emits no COMMAND_FAILED — the
    stop DID happen; only the mirror adoption is skipped. This dissolves the
    reviewer's LOW-2 by construction: a malformed-but-successful e-stop no
    longer queues a needless heat-off retry (the ``except`` branch is never
    reached — ``emergency_stop`` did not raise, it returned ``None``)."""
    executor = RecordingExecutor(emergency_stop_applied_state=None)
    over_ceiling = reading(bean=235.0, env=200.0, age_seconds=0.0)
    harness = make_harness(readings=[over_ceiling], executor=executor)
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT
        harness.controller.transition_to(step)
    harness.log.clear()
    harness.events.events.clear()

    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.FAULTED
    assert len(executor.estop_reasons) == 1
    # No COMMAND_FAILED: the e-stop write itself succeeded (only its payload
    # was unparseable), so this must not be treated as a write failure.
    assert RoastEventKind.COMMAND_FAILED not in harness.events.kinds()
    # No pending fail-safe retry latched — the malformed-payload path is NOT
    # the raising-exception path, so it must not queue the emergency_stop
    # retry evaluation (LOW-2).
    assert harness.controller._pending_fail_safe is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_advisor_drop_with_out_of_range_payload_still_transitions_and_does_not_refire() -> (
    None
):
    """Codex follow-up on #509/#507, end-to-end through the REAL
    ``RoasterControlAdapter`` (not the pre-degraded ``drop_applied_state=None``
    shortcut every other malformed-payload test uses): the MCP's own
    ``drop_beans`` result carries a well-typed but out-of-range
    ``heat_level_percent=101``. Proves the full pipeline —
    ``RoasterMCPClient`` → ``applied_state_from_event`` →
    ``RoasterControlAdapter._applied_state_or_none`` → the controller's
    ``except Exception`` around ``self._executor.drop_beans()`` — never sees a
    raw ``pydantic.ValidationError`` reach the controller: the drop still
    transitions to COOLING, emits COMMAND_EXECUTED (never COMMAND_FAILED),
    and does not re-fire on a follow-up tick."""

    drop_beans_calls = 0

    async def caller(tool: str, arguments: dict[str, object]) -> object:
        nonlocal drop_beans_calls
        if tool == "drop_beans":
            drop_beans_calls += 1
            return {
                "session_id": "abc",
                "phase": "dropped",
                "event": {
                    "kind": "beans_dropped",
                    "recorded_at_utc": "2026-06-07T12:19:00.000000+00:00",
                    "monotonic_seconds": 1228.9,
                    # Well-typed but out-of-range: the Codex-reported gap.
                    "payload": {
                        "heat_level_percent": 101,
                        "fan_level_percent": 100,
                        "cooling_on": True,
                    },
                },
                "event_count": 3,
            }
        if tool == "set_heat":
            return {
                "session_id": "abc",
                "phase": "development",
                "heat_level_percent": arguments["heat_level_percent"],
                "fan_level_percent": 50,
                "cooling_on": False,
            }
        if tool == "set_fan":
            return {
                "session_id": "abc",
                "phase": "development",
                "heat_level_percent": 65,
                "fan_level_percent": arguments["fan_level_percent"],
                "cooling_on": False,
            }
        raise AssertionError(f"unexpected tool call in this test: {tool}")

    executor = RoasterControlAdapter(RoasterMCPClient(caller))
    advisor = FakeAdvisor([decision(heat=65, fan=50, drop=False), decision(drop=True)])
    log: list[str] = []
    clock = FakeClock()
    reader = ScriptedStateReader([reading()], log)
    sink = RecordingSnapshotSink(log)
    events = EventSink(log)
    controller = RoastController(
        config=_BASELINE_POST_FC_CONFIG,
        safety=SafetyPolicy(SafetyLimits()),
        state_reader=reader,
        command_executor=executor,
        snapshot_sink=sink,
        event_emitter=events,
        advisor=advisor,
        clock=clock,
    )
    controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:4]:  # …→ DEVELOPMENT
        controller.transition_to(step)
    events.events.clear()

    controller.request_advisory()
    await controller.tick()  # pre-drop advisory write: mirrors land at 65/50
    assert controller.snapshot().current_heat == 65
    assert controller.snapshot().current_fan == 50

    controller.request_advisory()
    await controller.tick()  # the drop tick: the real adapter parses heat=101
    assert controller.phase is RoastPhase.COOLING  # the drop DID happen
    assert drop_beans_calls == 1
    executed = [p for k, p in events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {"command": "drop_beans", "source": "advisor"} in executed
    assert RoastEventKind.COMMAND_FAILED not in [k for k, _ in events.events]
    # Mirrors held at their pre-drop values (out-of-range → nothing valid to
    # adopt), never a crash, never a fabricated 101.
    assert controller.snapshot().current_heat == 65
    assert controller.snapshot().current_fan == 50

    # A follow-up tick must NOT re-fire drop_beans — any further call to the
    # scripted tool caller (drop_beans or otherwise) raises AssertionError.
    await controller.tick()
    assert drop_beans_calls == 1


# ---------------------------------------------------------------------------
# D96 (#559): bounded-bidirectional heat recovery — controller wiring
# ---------------------------------------------------------------------------


def _recovery_config(
    *, pre_fc_heat_target_percent: int = 100, **overrides: object
) -> ControllerConfig:
    """A ``ControllerConfig`` with D96 recovery ENABLED (and the ceiling
    guard forced on alongside it, the config-level precondition), unless the
    caller overrides either. Deliberately separate from :func:`_post_fc_config`
    (whose default is ``ceiling_guard_drop_enabled=False``, incompatible with
    ``recovery_enabled=True``).

    ``pre_fc_heat_target_percent`` (default 100, the flat pre-FC floor) lets
    a recovery test lower the FC-ENGAGEMENT heat below the static
    ``heat_ceiling_percent`` (also 100 by default) — recovery has no headroom
    to demonstrate a raise if entry heat already sits at the static ceiling
    (``min(100, 100 + headroom) == 100``, no room above it). 60 mirrors the
    roast-15 trim-60 recipe (#559) that motivated this law. Use
    :func:`_charge_through_fc_at_heat` (NOT the shared ``_charge_through_fc``,
    which hardcodes an ``== 100`` assertion) to drive a harness built with a
    non-default ``pre_fc_heat_target_percent`` through to DEVELOPMENT."""
    overrides.setdefault("enabled", True)
    overrides.setdefault("ceiling_guard_drop_enabled", True)
    overrides.setdefault("recovery_enabled", True)
    return ControllerConfig(
        pre_first_crack_levers=PreFirstCrackLevers(
            heat_target_percent=pre_fc_heat_target_percent,
            # LateMaillardTrim's own default trim_heat_percent (65) must not
            # exceed heat_target_percent (a validator enforces this) — clamp
            # it down to match whenever the caller lowers heat_target_percent
            # below 65 (it never engages in these tests anyway, since
            # _charge_through_fc_at_heat's scripted single T0 reading never
            # resolves an FC-ETA for the trim's window to open).
            late_maillard_trim=LateMaillardTrim(
                trim_heat_percent=min(65, pre_fc_heat_target_percent)
            ),
        ),
        post_first_crack_control=PostFirstCrackControl(**overrides),  # type: ignore[arg-type]
    )


#: A :class:`SafetyLimits` companion for ``_recovery_config(ceiling_guard_temp_c=220.0, ...)``
#: (#563). The told bitter ceiling now reads ``ceiling_guard_temp_c`` directly
#: (:meth:`~roastpilot_agent.control_policy.RoastControlPolicy._bitter_ceiling_temp_c`),
#: so a guard raised to 220.0 to "stay clear of the guard" and isolate a
#: DIFFERENT anchor (the deterministic-drop / recovery-raise tests below, all of
#: which top out at ``PROFILE.target_drop_temp_c`` == 205.0) must also raise
#: ``emergency_drop_temp_c`` above it, or the resolved ``PhaseControlLimits`` box
#: fails its own ``emergency_drop_temp_c > bitter_ceiling_temp_c`` validator (the
#: guard temp was never plumbed into that box before #563, so this collision did
#: not exist previously). ``max_bean_temp_c`` is raised alongside it to keep the
#: existing ``emergency_drop_temp_c < max_bean_temp_c`` ordering satisfied.
_ISOLATED_CEILING_GUARD_LIMITS = SafetyLimits(emergency_drop_temp_c=225.0, max_bean_temp_c=230.0)


async def _charge_through_fc_at_heat(
    harness: Harness,
    *,
    expected_pre_fc_heat: int,
    fc_bean_temp_c: float = 183.0,
    fc_ror_c_per_min: float | None = None,
) -> None:
    """Like :func:`_charge_through_fc`, but for a harness whose
    ``pre_first_crack_levers.heat_target_percent`` is NOT the default 100
    (D96/#559 recovery tests need engagement heat below the static
    ``heat_ceiling_percent`` to have any raise headroom at all) —
    ``_charge_through_fc`` hardcodes ``current_heat == 100`` and must not be
    weakened for every OTHER caller that legitimately relies on the default.
    """
    harness.controller.load_profile(PROFILE)
    for step in NORMAL_PATH[:2]:  # …→ PREHEATING
        harness.controller.transition_to(step)
    t0 = reading(bean=150.0, t0_detected=True, bean_ror_c_per_min=20.0)
    harness.reader.readings = [t0]
    for _ in range(3):  # three consecutive T0 ticks debounce → pre-FC
        await harness.controller.tick()
        harness.clock.advance(1.0)
    assert harness.controller.phase is RoastPhase.ROASTING_PRE_FIRST_CRACK
    assert harness.controller.snapshot().current_heat == expected_pre_fc_heat
    fc_reading = (
        reading(bean=fc_bean_temp_c, first_crack_detected=True, bean_ror_c_per_min=fc_ror_c_per_min)
        if fc_ror_c_per_min is not None
        else reading(bean=fc_bean_temp_c, first_crack_detected=True)
    )
    harness.reader.readings = [fc_reading]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    harness.log.clear()
    harness.events.events.clear()
    harness.sink.evaluations.clear()
    harness.executor.targets.clear()


@pytest.mark.asyncio
async def test_recovery_raises_heat_above_entry_end_to_end() -> None:
    """End-to-end through ``controller.tick()`` (not the algorithm directly,
    ``test_post_fc_control.py`` already covers that): replay roast 15's
    crashed-RoR shape through the real controller wiring and confirm heat
    actually rises above the FC-engagement value via a real ``set_targets``
    write, not just in the isolated algorithm."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,  # roast-15-shaped: engagement heat well below 100
        control_interval_seconds=5.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=3,
        recovery_headroom_percentage_points=15,
    )
    harness = make_harness(config=config)
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    entry_heat = harness.controller.snapshot().current_heat
    assert entry_heat == 60  # the actuated pre-FC lever, the roast-15 trim-60 recipe

    # Drive enough ticks with a sustained RoR shortfall for entry to confirm
    # (3 confirm ticks at the 5s cadence).
    for ror in [6.0, 5.0, 4.0, 3.0, 3.0, 3.0]:
        harness.clock.advance(5.0)
        harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=ror)]
        await harness.controller.tick()

    assert harness.controller.snapshot().current_heat > entry_heat
    assert harness.controller.snapshot().current_heat <= entry_heat + 15


@pytest.mark.asyncio
async def test_recovery_stays_inert_when_flag_off_default() -> None:
    """Regression: with ``recovery_enabled`` left at its default (False), a
    sustained RoR shortfall through the real controller tick loop must never
    raise heat above the FC-engagement value — the byte-for-byte D88
    behaviour, now proven through the SAME code path recovery uses when on."""
    config = _post_fc_config(control_interval_seconds=5.0)
    assert config.post_first_crack_control.recovery_enabled is False
    harness = make_harness(config=config)
    await _charge_through_fc(harness, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0)
    entry_heat = harness.controller.snapshot().current_heat

    for ror in [6.0, 5.0, 4.0, 3.0, 3.0, 3.0, 3.0, 3.0]:
        harness.clock.advance(5.0)
        harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=ror)]
        await harness.controller.tick()

    assert harness.controller.snapshot().current_heat <= entry_heat


@pytest.mark.asyncio
async def test_estop_precedence_over_recovery_raise_same_tick() -> None:
    """A tick whose telemetry qualifies for BOTH a recovery raise (sustained
    RoR shortfall) AND the hard-ceiling emergency stop (bean > 230 °C, the
    ``SafetyLimits.max_bean_temp_c`` default) must fire the emergency stop —
    ``_evaluate_safety``/``_act_on_safety`` run BEFORE
    ``_apply_deterministic_post_fc_levers`` in ``tick()``'s documented order
    and return early on a fail-closed verdict, so the recovery law's
    ``compute`` is never even reached that tick. This is the D96 safety
    review's mandatory drop/e-stop-precedence test."""
    config = _recovery_config(pre_fc_heat_target_percent=60, control_interval_seconds=5.0)
    harness = make_harness(config=config)
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )

    # Build up a sustained shortfall for two ticks (short of the 3-tick
    # confirm bar) with bean safely below the hard ceiling...
    for ror in [4.0, 3.0]:
        harness.clock.advance(5.0)
        harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=ror)]
        await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT

    # ...then the THIRD tick (which would otherwise confirm recovery entry)
    # ALSO crosses the hard 230 °C ceiling on the SAME reading.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=235.0, bean_ror_c_per_min=3.0)]
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.FAULTED
    assert len(harness.executor.estop_reasons) == 1
    # No recovery raise reached the wire on the e-stop tick: every non-zero
    # heat write recorded across the whole test stays within the D96
    # recovery cap (60 entry + 15 headroom = 75) -- the e-stop path itself
    # writes heat=0 via the fail-safe, never a SET_HEAT above the cap.
    heat_writes = [t for t in harness.executor.targets if t[0] != 0]
    assert not any(h > 75 for h, _ in heat_writes), heat_writes


@pytest.mark.asyncio
async def test_ceiling_guard_drop_takes_precedence_over_recovery_raise_same_tick() -> None:
    """PR #560 Codex finding (P1, the guard-eligible same-tick raise): a tick
    whose telemetry qualifies for BOTH a recovery raise AND the 196 °C
    ceiling-guard drop must NOT let the raise reach hardware at all —
    ``_apply_deterministic_post_fc_levers`` (this method) runs BEFORE
    ``_maybe_ceiling_guard_drop`` in ``tick()``'s order, so without an
    explicit skip the raised/gliding heat command would still be WRITTEN via
    ``set_targets`` a few lines before the guard's drop fires. The fix skips
    this tick's write entirely (restoring the tentative ``compute`` step)
    whenever the ceiling is elevated (RECOVERING or GLIDING) AND the same
    tick is independently guard-eligible — asserted here by inspecting the
    FAKE MCP CALL LOG (``harness.executor.targets``) directly, not just the
    phase outcome (which the drop alone would already satisfy even if a
    raise write had snuck out moments earlier)."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=196.0,
        recovery_confirm_ticks=1,
    )
    harness = make_harness(config=config)
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    targets_before_the_tick = list(harness.executor.targets)

    # One tick with a shortfall large enough to confirm entry immediately
    # (recovery_confirm_ticks=1) AND a bean temperature already at the
    # ceiling-guard threshold.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.COOLING
    # No heat/fan write reached the roaster this tick at all — the recovery
    # law's tentative raise was fully suppressed, not merely superseded by a
    # later drop.
    assert harness.executor.targets == targets_before_the_tick
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in executed


@pytest.mark.asyncio
async def test_zero_elevation_recovery_does_not_suppress_a_drop_tick_write() -> None:
    """PR #560 round 3 Codex finding: entry heat ALREADY AT
    ``heat_ceiling_percent`` (the default 100, via ``_charge_through_fc``'s
    pre-FC lever) means the recovery ceiling can never actually rise above
    the D88 base — the entry/exit COUNTERS can still confirm (a genuine
    sustained RoR shortfall), but ``heat_authority_state`` correctly reports
    ``HOLDING`` throughout (the round-3 fix), so the guard/drop-eligibility
    skip in ``_apply_deterministic_post_fc_levers`` (rounds 1/2) must NOT
    fire on a tick that is ALSO guard-eligible — there is no real elevation
    to protect the drop from suppressing hardware access for.

    Both heat AND the drop firing look IDENTICAL whether the skip
    incorrectly fires or not here (heat is pinned at 100 either way by the
    static ceiling, and the guard's drop is unconditional on bean
    temperature regardless of the post-FC lever's own skip) — so this test
    reads ``_post_fc_last_actuation_monotonic`` directly, the one INTERNAL
    signal that actually distinguishes the two code paths: the skip's
    ``restore_state`` branch returns WITHOUT ever touching this cadence
    timer, while the (correct) idempotent-write branch always advances it.
    Verified this test genuinely catches the round-3 mutant (re-deleting the
    fix locally reproduces this assertion failing, matching the algorithm-
    level tests' own fail-then-pass verification)."""
    config = _recovery_config(
        ceiling_guard_temp_c=196.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=0,
    )
    harness = make_harness(config=config)
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=100, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    cadence_before = harness.controller._post_fc_last_actuation_monotonic  # pyright: ignore[reportPrivateUsage]

    # A sustained RoR shortfall confirms entry (recovery_confirm_ticks=1),
    # AND the same tick's bean temperature is at the ceiling-guard line —
    # both conditions the round-1/2 skip watches for. With ZERO headroom,
    # heat_authority_state must stay HOLDING (round-3 fix), so the skip must
    # not suppress this tick's write.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.COOLING
    # The guard's drop still fires (unaffected by any of this).
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in executed
    # THE assertion that actually distinguishes correct behaviour from the
    # round-3 bug: the cadence timer ADVANCED past its pre-tick value (the
    # idempotent-write branch ran, meaning the skip did NOT fire) — a
    # phantom RECOVERING state would have hit the restore-and-return branch
    # instead, leaving the cadence timer exactly where it was before.
    cadence_after = harness.controller._post_fc_last_actuation_monotonic  # pyright: ignore[reportPrivateUsage]
    assert cadence_after != cadence_before


@pytest.mark.asyncio
async def test_deterministic_drop_takes_precedence_over_recovery_raise_same_tick() -> None:
    """PR #560 round 2 Codex finding (P2, the same class as round 1's P1 but
    for the DETERMINISTIC drop anchor): with the guard set safely ABOVE the
    profile's target_drop_temp_c (so `_maybe_ceiling_guard_drop` never fires
    here — this test isolates the `_maybe_deterministic_drop` mirror), a tick
    whose telemetry qualifies for BOTH a recovery raise AND the dev%/temp
    anchor (bean >= target_drop_temp_c AND system dev% >= target_development_
    percent) must NOT let the raise reach hardware — the same restore-and-
    skip this method already applies for the ceiling guard now ALSO covers
    the deterministic-drop anchor's own eligibility condition."""
    config = _recovery_config(
        ceiling_guard_temp_c=220.0,  # safely above PROFILE.target_drop_temp_c (205)
        recovery_confirm_ticks=1,
    )
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent,
        config=config,
        limits=_ISOLATED_CEILING_GUARD_LIMITS,
    )
    targets_before_the_tick = list(harness.executor.targets)

    # One tick with a shortfall large enough to confirm entry immediately
    # (recovery_confirm_ticks=1) AND bean/dev% both at the deterministic
    # anchor's target — eligible for the anchor, NOT for the (much higher)
    # guard.
    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.COOLING
    # No heat/fan write reached the roaster this tick at all.
    assert harness.executor.targets == targets_before_the_tick
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.DEVELOPMENT_TARGET.value,
    } in executed


@pytest.mark.asyncio
async def test_deterministic_drop_precedence_holds_even_when_drop_beans_fails() -> None:
    """The specific failure case PR #560's round-2 finding names: if
    `drop_beans()` itself then FAILS (a transient actuator failure) on a tick
    that was ALSO recovery-raise-eligible, the raise must STILL not have been
    written — otherwise the roast would be left sitting in DEVELOPMENT with
    recovery-raised heat past the drop's own target point, a strictly WORSE
    outcome than a raise-then-successful-drop. The skip in
    `_apply_deterministic_post_fc_levers` fires on ELIGIBILITY alone, before
    `_maybe_deterministic_drop` ever attempts (and here fails) the actual
    ``drop_beans()`` call, so this failure mode cannot compound the earlier
    one."""

    class RaisingDropExecutor(RecordingExecutor):
        async def drop_beans(self) -> AppliedRoasterState:
            raise RuntimeError("serial write dropped")

    config = _recovery_config(
        ceiling_guard_temp_c=220.0,
        recovery_confirm_ticks=1,
    )
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent,
        config=config,
        executor=RaisingDropExecutor(),
        limits=_ISOLATED_CEILING_GUARD_LIMITS,
    )
    targets_before_the_tick = list(harness.executor.targets)

    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()

    # The drop attempt failed (still DEVELOPMENT, a COMMAND_FAILED event) —
    # but critically, no raise write reached the roaster either.
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert harness.executor.targets == targets_before_the_tick
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.DEVELOPMENT_TARGET.value,
    } in failed


@pytest.mark.asyncio
async def test_gliding_heat_descends_through_repeated_failed_drops_same_tick() -> None:
    """PR #560 round 4 Codex finding (P1): the round-3 skip
    (``heat_authority_state is not HOLDING``) suppressed EVERY write on a
    drop-eligible tick, including a LOWERING move during the exit/glide
    tail — inverting the mechanism's own intent (it exists to stop a RAISE
    landing before a drop, not to freeze already-elevated heat). This test
    drives the exact "stuck at raised heat" scenario the finding describes:
    recovery raises heat, exit/glide begins its descent, the SAME ticks are
    ALSO deterministic-drop-eligible, and ``drop_beans()`` keeps failing
    across MULTIPLE consecutive ticks — heat writes must still DESCEND with
    the glide (asserted directly off the fake MCP call log), never freeze at
    the raised value, while the phase stays DEVELOPMENT with a
    ``COMMAND_FAILED`` event each tick."""

    class AlwaysFailingDropExecutor(RecordingExecutor):
        async def drop_beans(self) -> AppliedRoasterState:
            raise RuntimeError("serial write dropped")

    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=220.0,  # stay clear of the guard; isolate the anchor
        recovery_trigger_margin_c_per_min=1.0,
        recovery_exit_margin_c_per_min=0.5,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
        recovery_exit_glide_pp_per_tick=5,
    )
    harness = make_harness(
        config=config, executor=AlwaysFailingDropExecutor(), limits=_ISOLATED_CEILING_GUARD_LIMITS
    )
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    # Back-date the FIRST-CRACK clock (not the charge clock — that would
    # SHRINK dev%, since dev% = development_elapsed / charge_elapsed and
    # charge_elapsed is the denominator) so `_development_percent()` reads
    # comfortably above `PROFILE.target_development_percent` (20) for the
    # WHOLE test, without needing to hand-tune per-tick timing the way
    # `_development_harness_with_dev_percent` does for a single stamped tick.
    fc_monotonic = harness.controller._first_crack_monotonic  # pyright: ignore[reportPrivateUsage]
    assert fc_monotonic is not None  # set by the FC edge _charge_through_fc_at_heat just drove
    harness.controller._first_crack_monotonic = fc_monotonic - 10_000.0  # pyright: ignore[reportPrivateUsage]

    # Tick 1: confirm recovery ENTRY (a genuine RoR shortfall well past the
    # trigger margin, accounting for the EMA's smoothing lag against the FC
    # engagement's own bumpless-handoff tick) — this tick is NOT yet
    # drop-eligible (bean below target_drop_temp_c), so the raise writes
    # normally.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()
    entry_heat = harness.controller.snapshot().current_heat
    assert entry_heat > 60  # a genuine raise landed
    assert harness.controller.phase is RoastPhase.DEVELOPMENT

    # Tick 2: confirm recovery EXIT (RoR back within the exit margin) — bean
    # is now AT target_drop_temp_c, so this tick and every one after it is
    # drop-eligible. The exit-confirming tick's ceiling has already started
    # gliding down (D96: ticks_since_exit starts at 1 on the confirming tick
    # itself).
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=8.0)]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT  # drop failed, stays here
    heat_after_exit_confirm = harness.controller.snapshot().current_heat
    assert heat_after_exit_confirm < entry_heat  # the glide's first descending step landed

    # Ticks 3-5: RoR held near the (declining) setpoint so recovery stays in
    # its exit/glide/settled progression rather than re-triggering entry.
    # Still drop-eligible every tick, drop_beans() still failing — heat must
    # keep DESCENDING with the glide, never freeze at the raised (or any
    # intermediate) value.
    heats = [heat_after_exit_confirm]
    for _ in range(3):
        harness.clock.advance(5.0)
        harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=6.5)]
        await harness.controller.tick()
        assert harness.controller.phase is RoastPhase.DEVELOPMENT
        heats.append(harness.controller.snapshot().current_heat)

    # Non-increasing throughout (the glide descending, then holding flat at
    # the D88 base) — never flat at the RAISED value, never climbing back up.
    assert all(a >= b for a, b in zip(heats, heats[1:], strict=False)), heats
    assert heats[-1] < entry_heat
    assert heats[-1] == 60  # settles at the D88 base (heat_engage_percent)

    # COMMAND_FAILED fired every drop-eligible tick (ticks 2 through 5 -- 4
    # total; tick 1 was not yet drop-eligible).
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    expected_failed_payload = {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.DEVELOPMENT_TARGET.value,
    }
    failed_count = failed.count(expected_failed_payload)
    assert failed_count == 4


@pytest.mark.asyncio
async def test_genuine_raise_on_drop_due_tick_still_suppressed_round4() -> None:
    """The round-4 fix's OTHER half (the mandate's "unchanged" assertion,
    made explicit as its own test): a tick whose tentative write IS a
    genuine RAISE above actuated heat (recovery ENTRY, not exit/glide) and
    is ALSO drop-eligible must still be suppressed exactly as rounds 1/2
    established — the round-4 fix only stopped suppressing LOWERING writes,
    it did not weaken the original raise-suppression at all."""
    config = _recovery_config(
        ceiling_guard_temp_c=220.0,
        recovery_confirm_ticks=1,
    )
    harness = _development_harness_with_dev_percent(
        system_dev_percent=PROFILE.target_development_percent,
        config=config,
        limits=_ISOLATED_CEILING_GUARD_LIMITS,
    )
    targets_before_the_tick = list(harness.executor.targets)

    harness.reader.readings = [reading(bean=PROFILE.target_drop_temp_c, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.COOLING
    # No heat/fan write reached the roaster this tick at all -- the genuine
    # raise was fully suppressed.
    assert harness.executor.targets == targets_before_the_tick
    executed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_EXECUTED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.DEVELOPMENT_TARGET.value,
    } in executed


# --- #561 (D96 slice 1.5): post-failure heat-to-base clamp -----------------
#
# PR #560's own raise-suppression (rounds 1/2/4, tested above) covers the
# DETERMINISTIC drop paths' eligibility window — a genuine raise on a tick
# that is ALSO ceiling-guard- or dev%/temp-anchor-eligible is fully
# suppressed before it ever reaches `set_targets`, so `_last_post_fc_output`
# is never even updated on that tick (see `_apply_deterministic_post_fc_
# levers`'s own "do NOT stash it" comment) and there is nothing for the
# clamp below to undo. The residual PR #560 round 3 named, and #561 exists to
# close, is the ADVISOR drop path: `_run_advisory`'s own should_drop branch
# has NO analogous pre-write suppression, so a genuine recovery raise CAN
# land (updating `_current_heat` and `_last_post_fc_output` to a
# RECOVERING/GLIDING state) on an earlier tick, and a LATER tick's advisor
# `should_drop=True` can then fail its `drop_beans()` call while heat is
# still sitting at that raised value — exactly the "one-tick raise lands
# before an advisor drop attempt" gap. These tests drive that scenario (and
# the deterministic paths' own idempotent/inert edges) directly against the
# clamp.


class _AlwaysFailingDropExecutor(RecordingExecutor):
    """Every ``drop_beans()`` call raises (a transient actuator failure)."""

    async def drop_beans(self) -> AppliedRoasterState:
        raise RuntimeError("serial write dropped")


# #563/#570 sibling-PR interaction (pr-preflight, the #453 class): _bitter_
# ceiling_temp_c() now feeds ceiling_guard_temp_c straight into
# PhaseControlLimits (see _ISOLATED_CEILING_GUARD_LIMITS's own comment above,
# ~:7850) rather than capping it first — a guard raised well above the drop
# target to "stay clear of the guard and isolate a different anchor" (the
# pattern every test below uses) now also needs emergency_drop_temp_c raised
# above IT, or the resolved box fails its own emergency_drop_temp_c >
# bitter_ceiling_temp_c validator. Reusing _ISOLATED_CEILING_GUARD_LIMITS
# (emergency_drop_temp_c=225.0) covers every 220.0 guard temp below; one test
# uses 230.0 (to stay clear of the guard while STILL driving the run into
# FAULTED via a bean reading past max_bean_temp_c), which needs its own wider
# fixture — reused here rather than duplicated per call site.
_ISOLATED_CEILING_GUARD_LIMITS_230 = SafetyLimits(
    emergency_drop_temp_c=231.0, max_bean_temp_c=234.0
)


async def _confirm_recovery_raise(harness: Harness, *, entry_heat: int) -> int:
    """Drive one DEVELOPMENT tick with a sustained RoR shortfall large enough
    to confirm D96 recovery entry immediately (``recovery_confirm_ticks=1``
    on every caller below) — NOT drop-eligible (bean held well below
    ``PROFILE.target_drop_temp_c``), so the raise writes normally (no round-4
    suppression applies). Returns the resulting actuated heat, asserting it
    genuinely rose above ``entry_heat``."""
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()
    raised_heat = harness.controller.snapshot().current_heat
    assert raised_heat > entry_heat
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    return raised_heat


@pytest.mark.asyncio
async def test_advisor_drop_failure_clamps_heat_to_base_when_recovery_elevated() -> None:
    """The #561 residual itself: a recovery raise lands (via the loop, on a
    tick that is NOT drop-eligible), then a LATER tick's advisor
    ``should_drop=True`` fails its ``drop_beans()`` call while heat is still
    at the raised value — the clamp must land a heat-only write back to the
    D88 base (``heat_engage_percent``, 60 here) THROUGH THE SAFETY PATH
    (asserted via the fake MCP call log), phase stays DEVELOPMENT, and
    ``COMMAND_FAILED`` is emitted for the failed drop."""
    # The FIRST TWO consults — the FC-transition tick inside
    # `_charge_through_fc_at_heat` itself, then the raise-confirming tick
    # driven by `_confirm_recovery_raise` below — must NOT recommend a drop,
    # otherwise the raise and the drop-failure/clamp would collide on the
    # SAME tick, which this test deliberately drives as two SEPARATE ticks
    # (a dedicated same-tick-collision test follows this one). Every consult
    # AFTER that (the default) recommends the drop that then fails.
    advisor = FakeAdvisor(
        [decision(heat=50, fan=40, drop=False), decision(heat=50, fan=40, drop=False)],
        default_decision=decision(heat=50, fan=40, drop=True),
    )
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=220.0,  # stay clear of the guard entirely
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    harness = make_harness(
        config=config,
        advisor=advisor,
        executor=_AlwaysFailingDropExecutor(),
        limits=_ISOLATED_CEILING_GUARD_LIMITS,
    )
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    raised_heat = await _confirm_recovery_raise(harness, entry_heat=60)
    assert raised_heat <= 75  # within the 60+15 recovery cap

    # A later tick: the loop itself now holds/lowers (not a NEW raise, so it
    # is unaffected by anything drop-eligibility related — the advisor's own
    # should_drop path is the one under test), and the advisor is consulted
    # and recommends a drop that then FAILS.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=6.0)]
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {"command": "drop_beans", "source": "advisor"} in failed
    # The clamp's own write landed: the LAST set_targets call on the fake MCP
    # log commanded heat back down to the D88 base (60), fan held at whatever
    # was last actuated (unaffected by the heat-only clamp).
    last_heat, _last_fan = harness.executor.targets[-1]
    assert last_heat == 60
    assert harness.controller.snapshot().current_heat == 60


@pytest.mark.asyncio
async def test_advisor_drop_failure_clamp_is_inert_when_recovery_never_elevated() -> None:
    """Flag-off/HOLDING byte-for-byte inertness: with recovery never
    triggered this engagement (``_last_post_fc_output`` is either ``None`` or
    reports ``HOLDING``), a failed advisor drop issues NO extra clamp write —
    the existing (pre-#561) failure behaviour is completely unchanged."""
    advisor = FakeAdvisor(
        [decision(heat=50, fan=40, drop=True)],
        default_decision=decision(heat=50, fan=40, drop=True),
    )
    config = _post_fc_config()
    assert config.post_first_crack_control.recovery_enabled is False
    harness = make_harness(config=config, advisor=advisor, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc(harness, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0)
    targets_before_the_tick = list(harness.executor.targets)

    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=6.0)]
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {"command": "drop_beans", "source": "advisor"} in failed
    # No clamp write: the only writes this tick (if any) are the ordinary
    # post-FC loop's own actuation, never a SECOND heat-only write chasing it.
    new_targets = harness.executor.targets[len(targets_before_the_tick) :]
    assert len(new_targets) <= 1


@pytest.mark.asyncio
async def test_deterministic_drop_failure_clamps_heat_when_a_prior_raise_already_landed() -> None:
    """The deterministic-drop-path analogue: recovery raises on an earlier,
    NOT-yet-drop-eligible tick (so the round-4 suppression never applied and
    the raise genuinely reached the roaster), and only on a LATER tick does
    the bean cross into ceiling-guard-drop eligibility while ``drop_beans()``
    keeps failing — this tick's own tentative write is a HOLD/LOWER (not a
    fresh raise), so the round-4 skip does not suppress it either, and the
    loop's own write already lands the actuated heat at (or below) the raise.
    The clamp must independently drive heat to the D88 base and stay there,
    exactly like the advisor path."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=196.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    harness = make_harness(config=config, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    await _confirm_recovery_raise(harness, entry_heat=60)

    # A later tick crosses the ceiling-guard line while RoR has recovered
    # (so this tick's own tentative output no longer exceeds actuated heat —
    # a hold/lower, not a fresh raise) — `drop_beans()` fails.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=8.0)]
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in failed
    assert harness.controller.snapshot().current_heat == 60


@pytest.mark.asyncio
async def test_clamp_already_at_base_issues_no_redundant_write() -> None:
    """Idempotence: once heat has already settled at the D88 base (a prior
    clamp, or the glide, already brought it there), a further failed drop
    must not issue a redundant ``set_targets`` write — heat is already at the
    floor there is nothing to clamp down to."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=196.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_exit_margin_c_per_min=0.5,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
        recovery_exit_glide_pp_per_tick=50,  # the max: steep glide for a short test
    )
    harness = make_harness(config=config, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    await _confirm_recovery_raise(harness, entry_heat=60)

    # Exit confirms (RoR back within the exit margin) on a drop-eligible
    # tick; the glide is steep enough (50 pp/tick, the config max) that the
    # effective ceiling — and the clamp's base target — are both 60 already
    # this tick.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=8.0)]
    await harness.controller.tick()
    assert harness.controller.snapshot().current_heat == 60
    targets_after_first_clamp = list(harness.executor.targets)

    # A further drop-eligible tick, still failing, with heat already AT base:
    # no redundant clamp write.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=6.5)]
    await harness.controller.tick()
    assert harness.controller.snapshot().current_heat == 60
    # Any new entries must be the loop's OWN idempotent path (no write at
    # current_heat==target) — never a second write commanding 60 again.
    new_targets = harness.executor.targets[len(targets_after_first_clamp) :]
    assert (60, harness.controller.snapshot().current_fan) not in new_targets or not new_targets


@pytest.mark.asyncio
async def test_clamp_idempotent_while_still_gliding_below_base() -> None:
    """The idempotence branch's OTHER reachable shape: ``self._current_heat``
    can already sit AT OR BELOW the D88 base while ``heat_authority_state``
    is STILL ``GLIDING`` (not yet settled to ``HOLDING``) — the PI's own
    computed output can undercut the base well before the tick-counter-driven
    glide state itself reaches ``HOLDING`` (a high measured RoR relative to
    the taper's declining setpoint drives heat down hard on its own, entirely
    independent of the ceiling's own descent). A ceiling-guard-eligible tick
    landing in that window, with ``drop_beans()`` failing, must take the
    IDEMPOTENT path (no redundant write) while still forcing the recovery
    counters to a full exit — covering the shape the flag-off/HOLDING test
    above does not reach."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=190.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_exit_margin_c_per_min=0.5,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
        recovery_exit_glide_pp_per_tick=1,  # slow: GLIDING persists across ticks
    )
    harness = make_harness(config=config, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    await _confirm_recovery_raise(harness, entry_heat=60)

    # Probe (snapshot/restore, no side effect) that THIS tick's RoR would
    # genuinely compute a GLIDING, already-at/below-base output — confirming
    # the scenario is real before driving it through the actual tick below.
    pfc = harness.controller._post_fc_controller  # pyright: ignore[reportPrivateUsage]
    probe_state = pfc.snapshot_state()
    probe_output = pfc.compute(measured_ror_c_per_min=12.0, dt_seconds=5.0)
    pfc.restore_state(probe_state)
    assert probe_output.heat_authority_state is PostFcHeatAuthorityState.GLIDING
    assert probe_output.heat_percent <= 60

    # A single tick: bean is already at the (deliberately low) guard line, and
    # a high measured RoR relative to the taper's declining setpoint drives
    # the PI's own output well below the base in one step — the SAME tick
    # the ceiling-guard drop fires and then fails.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=12.0)]
    await harness.controller.tick()

    # Codex round-1 finding #1: the clamp clears the stashed output on this
    # (idempotent, forced-exit) path too — the advisor must never see a
    # stale GLIDING/RECOVERING authority state the controller just reset.
    assert harness.controller._last_post_fc_output is None  # pyright: ignore[reportPrivateUsage]
    heat_after = harness.controller.snapshot().current_heat
    assert heat_after <= 60  # already at/below the base
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in failed
    # No SECOND write landed for the clamp's own idempotence branch: the
    # only heat write this tick was the loop's own (already at/below base).
    heat_writes_this_tick = [h for h, _ in harness.executor.targets]
    assert heat_writes_this_tick[-1] == heat_after
    assert heat_writes_this_tick.count(heat_after) == 1
    # The recovery counters were still forced to a full exit (not left
    # "still gliding" for the next tick to reason from a stale ceiling).
    state = harness.controller._post_fc_controller.snapshot_state()  # pyright: ignore[reportPrivateUsage]
    assert state.recovery_active is False
    assert state.recovery_ticks_since_exit is None
    assert state.recovery_ticks_above_trigger == 0
    assert state.recovery_ticks_within_exit == 0


@pytest.mark.asyncio
async def test_repeated_clamp_failures_bound_heat_at_base_no_thrash() -> None:
    """Repeated-failure sequence (the team-lead brief's bounded-behaviour
    requirement): recovery raises, then EVERY subsequent tick is
    drop-eligible and ``drop_beans()`` keeps failing across many ticks.
    Heat must descend to (or, once the ordinary D88 taper itself takes over
    with ``heat_authority_state`` back to ``HOLDING``, legitimately below)
    the base and NEVER climb back up again — no raise/clamp thrash cycle,
    which is what a broken re-arm (re-triggering recovery as an artifact of
    the failed drop rather than a fresh RoR-shortfall confirmation) would
    produce."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=196.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_exit_margin_c_per_min=0.5,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
        recovery_exit_glide_pp_per_tick=5,
    )
    harness = make_harness(config=config, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    await _confirm_recovery_raise(harness, entry_heat=60)

    heats: list[int] = []
    for _ in range(10):
        harness.clock.advance(5.0)
        # Held near the taper's own end-value RoR (not a fresh sustained
        # shortfall) — a broken re-arm would re-confirm recovery and thrash
        # heat back up; the correct behaviour is a monotonic descent (via
        # the clamp, then the ordinary D88 taper once HOLDING) that never
        # re-ascends.
        harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=6.9)]
        await harness.controller.tick()
        assert harness.controller.phase is RoastPhase.DEVELOPMENT
        heats.append(harness.controller.snapshot().current_heat)

    # Heat is NON-INCREASING across the whole repeated-failure sequence —
    # the clamp brings it down to the base and the ordinary (HOLDING) taper
    # may continue lowering it further, but it never climbs back up (the
    # no-thrash bound).
    assert all(a >= b for a, b in zip(heats, heats[1:], strict=False)), heats
    assert heats[-1] < 60  # strictly below entry by the end of the sequence
    # It DID pass through (or land exactly on) the D88 base at some point on
    # its way down — the clamp's own contribution, not just the taper's.
    assert 60 in heats, heats


@pytest.mark.asyncio
async def test_clamp_forces_recovery_state_machine_to_exit() -> None:
    """The re-arm design decision, pinned directly: after a successful clamp
    write, the D96 recovery state machine's internal counters must be fully
    reset to HOLDING (``recovery_active=False``,
    ``recovery_ticks_since_exit=None``, both confirm counters at 0) — NOT
    left as "confirmed active/gliding" — so a subsequent RoR sample sitting
    exactly at the OLD trigger margin does not instantly re-confirm recovery
    (it must run a full FRESH ``recovery_confirm_ticks`` window from
    scratch)."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=196.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=2,  # >1 so re-arm-from-scratch is observable
        recovery_headroom_percentage_points=15,
    )
    harness = make_harness(config=config, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    # Confirm entry across 2 ticks (recovery_confirm_ticks=2), neither
    # drop-eligible.
    for _ in range(2):
        harness.clock.advance(5.0)
        harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=2.0)]
        await harness.controller.tick()
    raised_heat = harness.controller.snapshot().current_heat
    assert raised_heat > 60

    # A drop-eligible tick with the SAME sustained shortfall (still above the
    # trigger margin) — if the recovery counters were left "confirmed" rather
    # than reset, this single tick would be enough to look like ongoing
    # active recovery; the clamp must have reset them so this tick's own
    # entry counter starts back at zero.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert harness.controller.snapshot().current_heat == 60

    post_fc_controller = harness.controller._post_fc_controller  # pyright: ignore[reportPrivateUsage]
    state = post_fc_controller.snapshot_state()
    assert state.recovery_active is False
    assert state.recovery_ticks_since_exit is None
    assert state.recovery_ticks_above_trigger == 0
    assert state.recovery_ticks_within_exit == 0

    # A SECOND consecutive drop-eligible/failing tick with the identical
    # shortfall is only tick 1 of a FRESH confirm window (confirm_ticks=2) —
    # heat must still be pinned at the base, not already re-raised.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()
    assert harness.controller.snapshot().current_heat == 60


class _AlwaysFailingDropAndArmableSetTargetsExecutor(RecordingExecutor):
    """``drop_beans()`` always raises; ``set_targets()`` raises exactly once
    per :meth:`arm_next_set_targets_failure` call, then delegates normally
    (mirrors the established ``_ArmableFlakySetTargetsExecutor`` pattern) —
    lets a test drive the CLAMP's own corrective write into a transient
    actuator failure, independent of the drop failure that triggered it."""

    def __init__(self) -> None:
        super().__init__()
        self._armed = False

    def arm_next_set_targets_failure(self) -> None:
        """The NEXT ``set_targets`` call raises; every other call succeeds."""
        self._armed = True

    async def drop_beans(self) -> AppliedRoasterState:
        raise RuntimeError("serial write dropped")

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        if self._armed:
            self._armed = False
            raise RuntimeError("serial write dropped")
        await super().set_targets(heat_percent=heat_percent, fan_percent=fan_percent)


class _PersistentlyFailingDropAndSetTargetsExecutor(RecordingExecutor):
    """``drop_beans()`` always raises; ``set_targets()`` raises on EVERY call
    once :meth:`start_failing_set_targets` is called (never before — the
    pre-FC lever's own writes, and the recovery-raise write, must land
    normally during setup) — a PERSISTENT (not transient) MCP failure, for
    Codex round-2 finding #1's reproduction: the clamp's own corrective write
    never lands, no matter how many times it is attempted."""

    def __init__(self) -> None:
        super().__init__()
        self._fail_set_targets = False

    def start_failing_set_targets(self) -> None:
        """Every subsequent ``set_targets`` call raises, permanently."""
        self._fail_set_targets = True

    async def drop_beans(self) -> AppliedRoasterState:
        raise RuntimeError("serial write dropped")

    async def set_targets(self, *, heat_percent: int, fan_percent: int) -> None:
        if self._fail_set_targets:
            raise RuntimeError("serial write dropped")
        await super().set_targets(heat_percent=heat_percent, fan_percent=fan_percent)


@pytest.mark.asyncio
async def test_clamp_write_itself_failing_leaves_recovery_state_untouched() -> None:
    """#412 told==enforced extended to the clamp's own write: when the
    CLAMP's corrective ``set_targets`` call is itself a transient actuator
    failure (on top of the ``drop_beans()`` failure that triggered it), the
    recovery state machine must be left EXACTLY as it was — no forced exit —
    mirroring every other actuator-failure path in this module (the roaster's
    real heat is unchanged, so there is no state/reality gap yet to close).
    The NEXT tick gets another chance to clamp (and re-arm) once a write
    actually lands."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=196.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    executor = _AlwaysFailingDropAndArmableSetTargetsExecutor()
    harness = make_harness(config=config, executor=executor)
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    await _confirm_recovery_raise(harness, entry_heat=60)

    # Settle (bean below the guard line, so no drop attempt yet) until the
    # loop's own write becomes IDEMPOTENT (current_heat == this tick's
    # output) while the ceiling is still elevated (GLIDING, not yet
    # HOLDING) -- otherwise the loop's OWN write would consume the armed
    # set_targets failure before the clamp ever gets a turn.
    for _ in range(2):
        harness.clock.advance(5.0)
        harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=6.0)]
        await harness.controller.tick()
    raised_heat = harness.controller.snapshot().current_heat
    assert raised_heat > 60
    out = harness.controller._last_post_fc_output  # pyright: ignore[reportPrivateUsage]
    assert out is not None
    assert out.heat_authority_state is not PostFcHeatAuthorityState.HOLDING
    assert out.heat_percent == raised_heat  # this tick's own write was idempotent
    pre_tick_state = harness.controller._post_fc_controller.snapshot_state()  # pyright: ignore[reportPrivateUsage]
    targets_before = list(harness.executor.targets)

    # Arm the clamp's OWN corrective set_targets call to fail — the SAME RoR
    # (so the loop's own tentative write this tick is idempotent and issues
    # no set_targets call of its own), now with the bean at the guard line.
    executor.arm_next_set_targets_failure()
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=6.0)]
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in failed
    assert {"command": "set_targets"} in failed
    # The clamp's own write failed: heat is UNCHANGED from before this tick
    # (the actuator never actually moved), and no NEW write reached the fake
    # MCP call log.
    assert harness.controller.snapshot().current_heat == raised_heat
    assert harness.executor.targets == targets_before
    # The clamp's OWN forced-exit did NOT run on a write that never landed
    # (mirrors every other actuator-failure path in this module) -- the
    # loop's own ordinary `_advance_recovery_state` book-keeping (EMA, exit
    # counters) still advances every tick regardless (that machinery is
    # untouched by this fix), so what distinguishes "the clamp forced an
    # exit" from "the ordinary loop's own bookkeeping advanced" is
    # `heat_engage_percent` (the clamp's re-arm never changes it, since
    # `_force_recovery_exit` only overwrites the four recovery-specific
    # fields) staying put AND the counters NOT being the clamp's exact
    # forced-exit shape (all zeroed / None simultaneously) -- here
    # `recovery_ticks_since_exit` is 1 (the loop's own exit-confirmation
    # this tick), never reset to `None` by a clamp that never wrote.
    post_tick_state = harness.controller._post_fc_controller.snapshot_state()  # pyright: ignore[reportPrivateUsage]
    assert post_tick_state.heat_engage_percent == pre_tick_state.heat_engage_percent
    forced_exit_shape = (
        post_tick_state.recovery_active is False
        and post_tick_state.recovery_ticks_since_exit is None
        and post_tick_state.recovery_ticks_above_trigger == 0
        and post_tick_state.recovery_ticks_within_exit == 0
    )
    assert not forced_exit_shape

    # The NEXT tick: set_targets now succeeds normally (no longer armed), so
    # the clamp's write lands and the recovery state machine is forced to
    # exit as usual.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=6.0)]
    await harness.controller.tick()
    assert harness.controller.snapshot().current_heat < raised_heat
    settled_state = harness.controller._post_fc_controller.snapshot_state()  # pyright: ignore[reportPrivateUsage]
    if harness.controller.snapshot().current_heat <= 60:
        assert settled_state.recovery_active is False
        assert settled_state.recovery_ticks_since_exit is None


@pytest.mark.asyncio
async def test_clamp_box_stays_valid_under_a_hypothetically_narrowed_development_ceiling() -> None:
    """safety-561 (Opus) Low-1 hardening: the clamp box's own
    ``heat_ceiling_percent`` is not independently pinned — DEVELOPMENT
    resolves it to 100 TODAY, so ``base <= ceiling`` always holds in
    practice, but nothing in ``_clamp_heat_after_failed_drop`` PROVES that
    for any future config that might narrow DEVELOPMENT's own heat ceiling.
    This test forces exactly that hypothetical (patching
    ``_control_limits`` to return a box whose ``heat_ceiling_percent`` sits
    BELOW the D88 base the clamp needs to write) and asserts the clamp
    still constructs a VALID box (no ``PhaseControlLimits`` validator
    ``ValueError`` escaping ``tick()``) and still lands the corrective
    write — proving the ``max(base, resolved_ceiling)`` widening genuinely
    holds regardless of what DEVELOPMENT's own box resolves to, not just
    for today's fixed 100."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=196.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    harness = make_harness(config=config, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    await _confirm_recovery_raise(harness, entry_heat=60)
    raised_heat = harness.controller.snapshot().current_heat
    assert raised_heat > 60  # the base

    # Patch _control_limits (bound method, this instance only) so DEVELOPMENT
    # resolves a heat ceiling of 50 -- BELOW the base (60) the clamp needs to
    # write. A real (unwidened) `model_copy(update={"heat_floor_percent":
    # 60, ...})` against this box would leave heat_ceiling_percent=50,
    # constructing an invalid (floor > ceiling) PhaseControlLimits and
    # raising uncaught inside tick().
    real_control_limits = harness.controller._control_limits  # pyright: ignore[reportPrivateUsage]
    narrowed_box = real_control_limits().model_copy(update={"heat_ceiling_percent": 50})

    def _narrowed_control_limits(*, trim_signal: TrimSignal | None = None) -> PhaseControlLimits:
        return narrowed_box

    harness.controller._control_limits = _narrowed_control_limits  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    try:
        harness.clock.advance(5.0)
        harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=8.0)]
        await harness.controller.tick()  # must not raise
    finally:
        harness.controller._control_limits = real_control_limits  # pyright: ignore[reportPrivateUsage]

    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in failed
    # No PhaseControlLimits ValueError escaped tick() (the assertions above
    # already prove this implicitly -- an uncaught raise would have failed
    # the whole test before reaching here), and heat landed at or below the
    # base despite the artificially narrowed ceiling -- never above it, and
    # never left at the raised value. The narrowed box's own ceiling (50)
    # legitimately pulls the LOOP's own write below the base first (heat
    # <= 50 here); the safety property under test is that this constructs a
    # VALID box and never re-raises heat, not the exact settled value.
    assert harness.controller.snapshot().current_heat <= 60
    assert harness.controller.snapshot().current_heat < raised_heat


# --- Codex round 1 on PR #569: 3 real findings ------------------------------


@pytest.mark.asyncio
async def test_operator_drop_beans_failure_clamps_heat_when_recovery_elevated() -> None:
    """Codex round-1 finding #2: the FOURTH drop path. A transient
    ``drop_beans()`` failure on the OPERATOR's own drop (an early abort, or
    an operator retry attempt) is the identical fail-safe-down condition the
    other three drop paths clamp for — DEVELOPMENT can be left holding D96
    recovery-raised heat with no further deterministic drop guaranteed to
    retry promptly. The clamp must fire here too."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=220.0,  # stay clear -- isolate the operator path
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    executor = _AlwaysFailingDropAndArmableSetTargetsExecutor()
    harness = make_harness(config=config, executor=executor, limits=_ISOLATED_CEILING_GUARD_LIMITS)
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    raised_heat = await _confirm_recovery_raise(harness, entry_heat=60)
    assert raised_heat <= 75

    await harness.controller.operator_drop_beans()

    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {"command": "drop_beans"} in failed
    # The clamp's own write landed: heat is back at the D88 base, not left
    # sitting at the raised value.
    assert harness.controller.snapshot().current_heat == 60
    last_heat, _last_fan = harness.executor.targets[-1]
    assert last_heat == 60


@pytest.mark.asyncio
async def test_operator_drop_beans_failure_in_faulted_never_clamps() -> None:
    """The operator-drop clamp is scoped to the non-``FAULTED`` case
    (``will_transition``) only: from ``FAULTED`` heat is already off
    (``_apply_fail_safe``) and ``SET_HEAT`` is not even in that phase's
    command-phase-matrix row, so clamping there would be meaningless (and
    the call site deliberately does not attempt it) — this pins that scope
    directly, asserting no SET_HEAT write of any kind reaches the fake MCP
    call log on a failed FAULTED-phase drop."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=230.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    executor = _AlwaysFailingDropAndArmableSetTargetsExecutor()
    harness = make_harness(
        config=config, executor=executor, limits=_ISOLATED_CEILING_GUARD_LIMITS_230
    )
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    await _confirm_recovery_raise(harness, entry_heat=60)

    # Drive the run into FAULTED via the hard e-stop ceiling (never through
    # the clamp under test) — a bean reading past `max_bean_temp_c` (234.0
    # here, the isolated fixture's own wider ceiling — still well below this
    # reading, so the e-stop still fires exactly as before).
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=235.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()
    assert harness.controller.phase is RoastPhase.FAULTED
    targets_before = list(harness.executor.targets)

    await harness.controller.operator_drop_beans()

    assert harness.controller.phase is RoastPhase.FAULTED  # no transition, per #210
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {"command": "drop_beans"} in failed
    # No SET_HEAT write of any kind was attempted for this failed drop.
    assert harness.executor.targets == targets_before


@pytest.mark.asyncio
async def test_clamp_clears_stale_advisor_context_on_forced_exit() -> None:
    """Codex round-1 finding #1: after ``_force_recovery_exit`` resets
    authority to HOLDING, the advisor context (and decision trace) built
    LATER the same tick must never see a stale RECOVERING/GLIDING
    ``post_fc_heat_authority_state`` — ``_last_post_fc_output`` must be
    cleared, not left holding the pre-reset output."""
    advisor = FakeAdvisor(
        [decision(heat=50, fan=40, drop=False), decision(heat=50, fan=40, drop=False)],
        default_decision=decision(heat=50, fan=40, drop=False),
    )
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=196.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    harness = make_harness(config=config, advisor=advisor, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    await _confirm_recovery_raise(harness, entry_heat=60)

    # A drop-eligible tick: the ceiling-guard drop fires and fails, forcing
    # the clamp + recovery exit — the SAME tick's advisory consult (later in
    # tick()'s order) must read HOLDING, never a stale RECOVERING/GLIDING
    # value from before the clamp ran.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=8.0)]
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in failed
    assert len(advisor.contexts) >= 1
    last_context = advisor.contexts[-1]
    assert last_context.post_fc_heat_authority_state is None
    assert last_context.post_fc_setpoint_c_per_min is None
    # The decision trace records the same tick — its own authority reasoning
    # (if any) must not disagree with the context the advisor was actually
    # shown; this is the told==enforced proof for this field.
    assert harness.controller._last_post_fc_output is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_repeated_failed_drops_starting_below_base_never_reascend() -> None:
    """Codex round-1 finding #3 (the subtle one): a repeated-failed-drop
    sequence that starts with heat ALREADY below the D88 base (reached via
    the loop's own PI action, independent of the ceiling) must never let a
    later low-RoR sample raise heat back toward the base while the SAME drop
    keeps failing every tick. Without the persistent
    ``_post_fc_raise_suppressed_after_clamp`` latch, forcing the recovery
    state machine fully to HOLDING on the first clamp would clear
    ``heat_authority_state``'s own "elevated" signal, and a sustained RoR
    shortfall could re-confirm entry the very next tick and re-raise heat —
    reproducing exactly the failure this test pins against."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=190.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
        recovery_exit_glide_pp_per_tick=1,
    )
    harness = make_harness(config=config, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    await _confirm_recovery_raise(harness, entry_heat=60)

    # Drive heat below the base via the loop's own PI action (a high RoR vs.
    # the declining setpoint) on a guard-eligible, failing-drop tick — the
    # clamp's idempotence branch fires and forces a full recovery exit.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=12.0)]
    await harness.controller.tick()
    heat_below_base = harness.controller.snapshot().current_heat
    assert heat_below_base < 60
    assert harness.controller._post_fc_raise_suppressed_after_clamp is True  # pyright: ignore[reportPrivateUsage]

    # Now feed a SUSTAINED low-RoR shortfall (the exact condition that would
    # re-confirm recovery entry within a single `recovery_confirm_ticks`
    # window) across many subsequent ticks, still guard-eligible, still
    # failing the drop every tick. Heat must NEVER re-ascend above where it
    # settled -- the latch must keep suppressing the raise regardless of
    # `heat_authority_state` re-confirming RECOVERING internally.
    heats = [heat_below_base]
    for _ in range(8):
        harness.clock.advance(5.0)
        harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=1.0)]
        await harness.controller.tick()
        assert harness.controller.phase is RoastPhase.DEVELOPMENT
        heats.append(harness.controller.snapshot().current_heat)

    assert all(a >= b for a, b in zip(heats, heats[1:], strict=False)), heats
    assert max(heats) == heat_below_base  # never climbed back up at all
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert (
        failed.count(
            {
                "command": "drop_beans",
                "source": "policy",
                "reason": DropReason.CEILING_GUARD.value,
            }
        )
        >= 8
    )


@pytest.mark.asyncio
async def test_raise_suppression_latch_clears_on_successful_drop() -> None:
    """The latch is per-DEVELOPMENT-dwell state (mirrors ``_post_fc_engaged``
    / ``_last_post_fc_output``'s own discipline): once a drop actually
    SUCCEEDS, ``transition_to`` clears it unconditionally, and a later fresh
    DEVELOPMENT dwell (a new FC edge) starts with it unset."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=190.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    harness = make_harness(config=config, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    await _confirm_recovery_raise(harness, entry_heat=60)

    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=12.0)]
    await harness.controller.tick()
    assert harness.controller._post_fc_raise_suppressed_after_clamp is True  # pyright: ignore[reportPrivateUsage]

    # A SUCCESSFUL manual drop clears it via transition_to(COOLING) -- swap
    # in a plain (succeeding) executor for this one call, since the
    # harness's own executor class always fails drop_beans.
    original_executor = harness.controller._executor  # pyright: ignore[reportPrivateUsage]
    harness.controller._executor = RecordingExecutor()  # pyright: ignore[reportPrivateUsage]
    try:
        await harness.controller.operator_drop_beans()
    finally:
        harness.controller._executor = original_executor  # pyright: ignore[reportPrivateUsage]

    assert harness.controller.phase is RoastPhase.COOLING
    assert harness.controller._post_fc_raise_suppressed_after_clamp is False  # pyright: ignore[reportPrivateUsage]


# --- Codex round 2 on PR #569: 2 coherent findings --------------------------


@pytest.mark.asyncio
async def test_advisor_path_arms_latch_even_when_corrective_write_also_fails() -> None:
    """Codex round-2 finding #1 (the failed-corrective-write asymmetry):
    reproduced first (see the reproduction notes in the commit message),
    then fixed. The advisor's drop path is cadence-gated
    (``AdvisoryCallPolicy``) — unlike the two deterministic drop paths,
    which fire on EVERY DEVELOPMENT tick with no cadence gate and so retry
    (and re-attempt the clamp) automatically next tick — so a failed
    corrective write here has no guaranteed retry coming. The latch
    (``_post_fc_raise_suppressed_after_clamp``) must arm regardless of
    whether the write lands, or a persisting shortfall could re-raise heat
    on a later, non-consulted tick with nothing left to catch it."""
    advisor = FakeAdvisor(
        [decision(heat=50, fan=40, drop=False), decision(heat=50, fan=40, drop=False)],
        default_decision=decision(heat=50, fan=40, drop=True),
    )
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=220.0,  # stay clear -- isolate the advisor path
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    executor = _PersistentlyFailingDropAndSetTargetsExecutor()
    harness = make_harness(
        config=config,
        advisor=advisor,
        executor=executor,
        limits=_ISOLATED_CEILING_GUARD_LIMITS,
    )
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    raised_heat = await _confirm_recovery_raise(harness, entry_heat=60)
    assert raised_heat > 60

    # NOW the persistent MCP failure starts: the advisor's should_drop=True
    # fails its drop_beans() call, and the clamp's OWN corrective set_targets
    # call ALSO fails.
    executor.start_failing_set_targets()
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=6.0)]
    await harness.controller.tick()

    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert harness.controller.snapshot().current_heat == raised_heat  # write failed, unchanged
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {"command": "drop_beans", "source": "advisor"} in failed
    assert {"command": "set_targets"} in failed
    # The fix: the latch arms even though the corrective write itself
    # failed (the pre-fix behaviour left this False).
    assert harness.controller._post_fc_raise_suppressed_after_clamp is True  # pyright: ignore[reportPrivateUsage]

    # Prove the latch actually PROTECTS: several further sub-cadence ticks
    # (the advisor is NOT re-consulted — no phase/temp/RoR-delta trigger
    # fires) with a persisting RoR shortfall must never re-raise heat, even
    # though nothing ever retries the drop or the clamp again.
    heats = [harness.controller.snapshot().current_heat]
    for _ in range(5):
        harness.clock.advance(0.5)  # well under the advisory min-interval
        harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=1.0)]
        await harness.controller.tick()
        heats.append(harness.controller.snapshot().current_heat)
    assert max(heats) == raised_heat  # never climbed any higher


@pytest.mark.asyncio
async def test_operator_path_arms_latch_even_when_corrective_write_also_fails() -> None:
    """Codex round-2 finding #1, the operator-drop mirror: the operator's
    drop is ONE-SHOT — never automatically retried by the controller at
    all — so the latch must arm here too, even when the clamp's own
    corrective write also fails."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=220.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    executor = _PersistentlyFailingDropAndSetTargetsExecutor()
    harness = make_harness(config=config, executor=executor, limits=_ISOLATED_CEILING_GUARD_LIMITS)
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    raised_heat = await _confirm_recovery_raise(harness, entry_heat=60)
    assert raised_heat > 60

    executor.start_failing_set_targets()
    await harness.controller.operator_drop_beans()

    assert harness.controller.phase is RoastPhase.DEVELOPMENT
    assert harness.controller.snapshot().current_heat == raised_heat
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {"command": "drop_beans"} in failed
    assert {"command": "set_targets"} in failed
    assert harness.controller._post_fc_raise_suppressed_after_clamp is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_deterministic_path_does_not_arm_latch_on_failed_corrective_write() -> None:
    """The `self_healing=True` half of the same fix, pinned directly: the
    deterministic ceiling-guard/dev%-anchor paths do NOT arm the latch on a
    failed corrective write — they retry the drop (and the clamp) every
    tick with no cadence gate, so the pre-existing "arm only on a landed
    write" behaviour is deliberately UNCHANGED for them (this is what
    safety-561's earlier `test_clamp_write_itself_failing_leaves_recovery_
    state_untouched` already pinned; restated here explicitly against the
    latch field itself, alongside the two non-self-healing tests above, so
    the asymmetry is visible in one place)."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=196.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    executor = _AlwaysFailingDropAndArmableSetTargetsExecutor()
    harness = make_harness(config=config, executor=executor)
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    await _confirm_recovery_raise(harness, entry_heat=60)

    # Settle into an idempotent hold with the ceiling still elevated (the
    # same construction the earlier test uses), then arm the clamp's OWN
    # corrective write to fail on the guard-eligible tick.
    for _ in range(2):
        harness.clock.advance(5.0)
        harness.reader.readings = [reading(bean=190.0, bean_ror_c_per_min=6.0)]
        await harness.controller.tick()

    executor.arm_next_set_targets_failure()
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=6.0)]
    await harness.controller.tick()

    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in failed
    assert {"command": "set_targets"} in failed
    # Deliberately UNCHANGED: the latch does not arm on this path's failed
    # corrective write (the next tick's own retry is the mechanism instead).
    assert harness.controller._post_fc_raise_suppressed_after_clamp is False  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_suppressed_tick_still_applies_a_fan_only_move() -> None:
    """Codex round-2 finding #2 (the suppression must be HEAT-ONLY):
    reproduced first, then fixed. On a tick where the raise-suppression
    fires (elevated authority, drop-eligible, a genuine heat raise wanted),
    a fan move the advisor's SAME-dwell consult already holds as the
    desired target must still reach the roaster — in loop mode this method
    is the sole writer (#498), so blocking the WHOLE write here would
    strand a safe fan move alongside the unsafe heat raise, colliding with
    the D96 doctrine ("fan is valuable and should be used to control")."""
    # tick0 (FC transition): fan holds at 30. tick1 (raise-confirm, NOT
    # drop-eligible): fan holds at 30 -- heat raises alone. tick2
    # (drop-eligible, suppressed): the advisor's SAME-dwell consult (from
    # tick1) already requested fan=90 -- STILL stranded at the START of
    # tick2 (advisory runs AFTER the loop in tick()'s order), so tick2
    # proves the strand; tick3 (also suppressed) proves it PERSISTS without
    # the fix and is FIXED by it.
    advisor = FakeAdvisor(
        [decision(heat=50, fan=30, drop=False), decision(heat=50, fan=30, drop=False)],
        default_decision=decision(heat=50, fan=90, drop=False),
    )
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=196.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    harness = make_harness(config=config, advisor=advisor, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()
    assert harness.controller.snapshot().current_fan == 30

    # tick2: drop-eligible, the raise-suppression fires (heat held), the
    # SAME tick's advisor consult requests fan=90 for the FIRST time -- that
    # request lands in `_post_fc_desired_fan_percent` too late to be applied
    # THIS tick (advisory runs after the loop), so fan is still 30 leaving
    # this tick — expected, not the bug under test.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()
    heat_after_tick2 = harness.controller.snapshot().current_heat
    assert harness.controller._post_fc_desired_fan_percent == 90  # pyright: ignore[reportPrivateUsage]

    # tick3: STILL drop-eligible/suppressed (heat still wants to raise), and
    # NOW desired_fan (90) differs from current_fan (30) at the START of
    # the tick -- this is the exact stranding window. The fix must let the
    # fan-only move through while heat stays held.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()

    assert harness.controller.snapshot().current_fan == 90  # the fan move landed
    assert harness.controller.snapshot().current_heat == heat_after_tick2  # heat held, not raised
    failed = [p for k, p in harness.events.events if k is RoastEventKind.COMMAND_FAILED]
    assert {
        "command": "drop_beans",
        "source": "policy",
        "reason": DropReason.CEILING_GUARD.value,
    } in failed
    last_heat, last_fan = harness.executor.targets[-1]
    assert last_heat == heat_after_tick2
    assert last_fan == 90


@pytest.mark.asyncio
async def test_suppressed_tick_with_fan_already_correct_issues_no_write() -> None:
    """The suppression's idempotence counterpart to the fan-permeable fix:
    when fan is ALREADY at its desired value on a suppressed tick, no write
    at all is issued (there is genuinely nothing to do — heat is correctly
    held and fan needs no correction) — proves the fix did not turn every
    suppressed tick into an unconditional write."""
    config = _recovery_config(
        pre_fc_heat_target_percent=60,
        control_interval_seconds=5.0,
        ceiling_guard_temp_c=196.0,
        recovery_trigger_margin_c_per_min=1.0,
        recovery_confirm_ticks=1,
        recovery_headroom_percentage_points=15,
    )
    harness = make_harness(config=config, executor=_AlwaysFailingDropExecutor())
    await _charge_through_fc_at_heat(
        harness, expected_pre_fc_heat=60, fc_bean_temp_c=183.0, fc_ror_c_per_min=7.0
    )
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=185.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()

    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()
    targets_before = list(harness.executor.targets)

    # Fan is already at its (never-changed, default-held) desired value —
    # a further suppressed tick must issue NO write at all.
    harness.clock.advance(5.0)
    harness.reader.readings = [reading(bean=196.0, bean_ror_c_per_min=2.0)]
    await harness.controller.tick()

    assert harness.executor.targets == targets_before
