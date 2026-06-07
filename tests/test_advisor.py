"""Advisor tests (component plan §8; orchestration plan § Advisor tests).

Two layers:

1. ``FakeAdvisor`` as a unit — deterministic script consumption, context
   recording, exhaustion behaviour, and the failure-mode → typed-exception
   mapping.
2. The five advisory outcomes (valid / malformed / unsafe / timeout /
   provider error) driven through the controller's advisory step, asserting
   each failure becomes a REJECT with the deterministic hold-current-targets
   fallback, persisted and emitted, while a valid decision is applied. This
   is the architecture invariant in action: advisor output never reaches a
   write without a SafetyEvaluation.

The OpenRouter implementation behind a recorded-response double lands in
E8-S2.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorFailureMode,
    AdvisorMalformedOutputError,
    AdvisorProviderError,
    AdvisorUnsafeOutputError,
    FakeAdvisor,
    RoastAdvisor,
    RoastDecision,
)
from roastpilot_agent.config import ControllerConfig
from roastpilot_agent.controller import RoastController, RoastPhase
from roastpilot_agent.models import RoastEventKind, RoastProfile, RoastTelemetry
from roastpilot_agent.safety import SafetyLimits, SafetyPolicy, SafetyVerdict
from tests.conftest import (
    EventSink,
    FakeClock,
    RecordingExecutor,
    RecordingSnapshotSink,
    ScriptedStateReader,
)

# --- shared builders ---

PROFILE = RoastProfile(
    name="advisor-suite",
    bean_origin="Ethiopia",
    bean_weight_grams=250.0,
    initial_heat_percent=70,
    initial_fan_percent=40,
    target_drop_temp_c=205.0,
    target_development_percent=20.0,
)

# The legal edge sequence idle → … → development; the advisory step only
# runs in a phase where SET_HEAT is permitted (development qualifies).
_TO_DEVELOPMENT = [
    RoastPhase.STARTING,
    RoastPhase.PREHEATING,
    RoastPhase.ROASTING_PRE_FIRST_CRACK,
    RoastPhase.DEVELOPMENT,
]


def decision(heat: int = 65, fan: int = 50, drop: bool = False) -> RoastDecision:
    """A well-typed advisory recommendation."""
    return RoastDecision(
        target_heat=heat,
        target_fan=fan,
        should_drop=drop,
        confidence=0.9,
        rationale="fixture",
    )


def context(phase: RoastPhase = RoastPhase.DEVELOPMENT) -> AdvisorContext:
    """A minimal advisor context for unit-level FakeAdvisor tests."""
    return AdvisorContext(
        phase=phase,
        roast_elapsed_seconds=300.0,
        development_elapsed_seconds=30.0,
        current_bean_temp_c=200.0,
        current_env_temp_c=210.0,
        bean_ror_c_per_min=5.0,
        env_ror_c_per_min=4.0,
        target_drop_temp_c=205.0,
        profile_name=PROFILE.name,
    )


def reading(bean: float = 200.0, env: float = 210.0, **kwargs: object) -> RoastTelemetry:
    return RoastTelemetry.model_validate({"bean_temp_c": bean, "env_temp_c": env, **kwargs})


@dataclass
class Harness:
    controller: RoastController
    executor: RecordingExecutor
    sink: RecordingSnapshotSink
    events: EventSink
    log: list[str] = field(default_factory=list[str])


def harness_in_development(
    *,
    advisor: RoastAdvisor,
    readings: list[RoastTelemetry | None | Exception] | None = None,
    config: ControllerConfig | None = None,
) -> Harness:
    """A controller advanced to ``development`` with ``advisor`` wired in."""
    log: list[str] = []
    executor = RecordingExecutor(log)
    sink = RecordingSnapshotSink(log)
    events = EventSink(log)
    controller = RoastController(
        config=config or ControllerConfig(),
        safety=SafetyPolicy(SafetyLimits()),
        state_reader=ScriptedStateReader(readings or [reading()], log),
        command_executor=executor,
        snapshot_sink=sink,
        event_emitter=events,
        advisor=advisor,
        clock=FakeClock(),
    )
    controller.load_profile(PROFILE)
    for step in _TO_DEVELOPMENT:
        controller.transition_to(step)
    log.clear()
    events.events.clear()
    return Harness(controller, executor, sink, events, log)


# --- FakeAdvisor as a unit ---


@pytest.mark.asyncio
async def test_fake_advisor_returns_scripted_decisions_in_order() -> None:
    advisor = FakeAdvisor([decision(heat=60), decision(heat=70)])
    first = await advisor.get_recommendation(context())
    second = await advisor.get_recommendation(context())
    assert (first.target_heat, second.target_heat) == (60, 70)


@pytest.mark.asyncio
async def test_fake_advisor_is_deterministic() -> None:
    """Same script, same contexts ⇒ identical decisions across instances."""
    ctxs = [context(), context()]
    a = FakeAdvisor([decision(heat=60), decision(heat=70)])
    b = FakeAdvisor([decision(heat=60), decision(heat=70)])
    out_a = [(await a.get_recommendation(c)).model_dump() for c in ctxs]
    out_b = [(await b.get_recommendation(c)).model_dump() for c in ctxs]
    assert out_a == out_b


@pytest.mark.asyncio
async def test_fake_advisor_records_received_contexts() -> None:
    advisor = FakeAdvisor([decision(), decision()])
    preheat, dev = context(RoastPhase.PREHEATING), context(RoastPhase.DEVELOPMENT)
    await advisor.get_recommendation(preheat)
    await advisor.get_recommendation(dev)
    assert advisor.contexts == [preheat, dev]


@pytest.mark.asyncio
async def test_fake_advisor_exhausted_raises_provider_error() -> None:
    advisor = FakeAdvisor([decision()])
    await advisor.get_recommendation(context())
    with pytest.raises(AdvisorProviderError):
        await advisor.get_recommendation(context())


@pytest.mark.asyncio
async def test_fake_advisor_default_decision_after_exhaustion() -> None:
    """A configured default makes the advisor an infinite deterministic
    source — the demo/constant-advice mode."""
    advisor = FakeAdvisor([], default_decision=decision(heat=42))
    for _ in range(3):
        result = await advisor.get_recommendation(context())
        assert result.target_heat == 42


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (AdvisorFailureMode.MALFORMED, AdvisorMalformedOutputError),
        (AdvisorFailureMode.UNSAFE, AdvisorUnsafeOutputError),
        (AdvisorFailureMode.PROVIDER_ERROR, AdvisorProviderError),
        (AdvisorFailureMode.TIMEOUT, TimeoutError),
    ],
)
async def test_fake_advisor_failure_modes_raise_typed_errors(
    mode: AdvisorFailureMode, expected: type[Exception]
) -> None:
    advisor = FakeAdvisor([mode])
    with pytest.raises(expected):
        await advisor.get_recommendation(context())


# --- the five outcomes through the controller's advisory step ---


@pytest.mark.asyncio
async def test_valid_decision_is_evaluated_and_applied() -> None:
    """A valid decision within bounds: ALLOW, executed, current targets move."""
    harness = harness_in_development(advisor=FakeAdvisor([decision(heat=60, fan=50)]))
    harness.controller.request_advisory()
    await harness.controller.tick()

    assert harness.executor.targets == [(60, 50)]
    assert harness.sink.evaluations[-1].verdict is SafetyVerdict.ALLOW
    assert RoastEventKind.COMMAND_EXECUTED in harness.events.kinds()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "rule"),
    [
        (AdvisorFailureMode.MALFORMED, "advisor_malformed"),
        (AdvisorFailureMode.UNSAFE, "advisor_unsafe"),
        (AdvisorFailureMode.TIMEOUT, "advisor_timeout"),
        (AdvisorFailureMode.PROVIDER_ERROR, "advisor_provider_error"),
    ],
)
async def test_failure_outcomes_reject_and_hold_current_targets(
    mode: AdvisorFailureMode, rule: str
) -> None:
    """Every failure mode ⇒ REJECT with the named rule, no write issued, the
    rejected recommendation persisted and an ADVISORY event emitted."""
    harness = harness_in_development(advisor=FakeAdvisor([mode]))
    harness.controller.request_advisory()
    await harness.controller.tick()

    rejection = harness.sink.evaluations[-1]
    assert rejection.rule == rule
    assert rejection.verdict is SafetyVerdict.REJECT
    # Deterministic fallback: hold the current targets (idle development: 0/0).
    assert (rejection.adjusted_heat, rejection.adjusted_fan) == (0, 0)
    assert harness.executor.targets == []
    assert f"persist_evaluation:{rule}" in harness.log
    assert RoastEventKind.ADVISORY in harness.events.kinds()


@pytest.mark.asyncio
async def test_failure_holds_previously_applied_targets() -> None:
    """The hold fallback echoes whatever heat/fan is actually in effect, not
    a constant — a valid decision applies, then a failure holds those."""
    advisor = FakeAdvisor([decision(heat=55, fan=45), AdvisorFailureMode.PROVIDER_ERROR])
    harness = harness_in_development(advisor=advisor)

    harness.controller.request_advisory()
    await harness.controller.tick()
    assert harness.executor.targets == [(55, 45)]

    harness.controller.request_advisory()
    await harness.controller.tick()
    rejection = harness.sink.evaluations[-1]
    assert rejection.verdict is SafetyVerdict.REJECT
    assert (rejection.adjusted_heat, rejection.adjusted_fan) == (55, 45)
    # No second write — the failure holds, it does not re-issue.
    assert harness.executor.targets == [(55, 45)]


@pytest.mark.asyncio
async def test_real_timeout_path_never_blocks_the_tick() -> None:
    """The elapsed-time timeout (not the scripted shortcut): an advisor that
    never resolves is bounded by ``advisory_timeout_seconds`` and the tick
    still completes with the hold fallback."""

    class NeverAdvisor(RoastAdvisor):
        async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    harness = harness_in_development(
        advisor=NeverAdvisor(),
        config=ControllerConfig(advisory_timeout_seconds=0.05),
    )
    harness.controller.request_advisory()
    await asyncio.wait_for(harness.controller.tick(), timeout=1.0)

    assert harness.sink.evaluations[-1].rule == "advisor_timeout"
    assert harness.sink.evaluations[-1].verdict is SafetyVerdict.REJECT
    assert harness.executor.targets == []
