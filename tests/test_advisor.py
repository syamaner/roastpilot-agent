"""Advisor tests (component plan §8; orchestration plan § Advisor tests).

Three layers:

1. ``FakeAdvisor`` as a unit — deterministic script consumption, context
   recording, exhaustion behaviour, and the failure-mode → typed-exception
   mapping.
2. The five advisory outcomes (valid / malformed / unsafe / timeout /
   provider error) driven through the controller's advisory step, asserting
   each failure becomes a REJECT with the deterministic hold-current-targets
   fallback, persisted and emitted, while a valid decision is applied. This
   is the architecture invariant in action: advisor output never reaches a
   write without a SafetyEvaluation.
3. ``PydanticAIAdvisor`` + the ``build_model`` factory (E8-S2 / D18): the
   provider→Model mapping for every enum value, and the advisor's
   structured-output / typed-error mapping exercised behind a PydanticAI
   recorded-response double (``FunctionModel``) — no live calls, no keys.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel

from roastpilot_agent.advisor import (
    CONTROL_TEACHING_PROMPT_VERSION,
    AdvisorContext,
    AdvisorDescriptor,
    AdvisorFailureMode,
    AdvisorMalformedOutputError,
    AdvisorProviderError,
    AdvisorUnsafeOutputError,
    FakeAdvisor,
    PydanticAIAdvisor,
    RoastAdvisor,
    RoastDecision,
    build_model,
    control_teaching_prompt,
    instructions_for,
    reasoning_extra_body,
    reasoning_from_run,
    usage_from_run,
)
from roastpilot_agent.config import (
    AdvisorConfig,
    ControllerConfig,
    PostFirstCrackControl,
    SafetyLimits,
)
from roastpilot_agent.controller import RoastController, RoastPhase
from roastpilot_agent.models import (
    AdvisorHealth,
    AdvisorHealthStatus,
    RoastEventKind,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.safety import SafetyPolicy, SafetyVerdict
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


#: This module's harness baseline (12 Jul D88/D89 promotion, mirrors
#: test_controller.py's ``_BASELINE_POST_FC_CONFIG``): every test in this file
#: predates the promotion and was written to exercise the advisor-driven
#: baseline post-FC path directly (no deterministic taper, no ceiling-guard
#: auto-drop) — a bare ``ControllerConfig()`` now defaults BOTH flags True
#: (#495), which would auto-drop several of these scenarios (e.g. a reading
#: at/above the default 196 °C ceiling guard) before the advisor consult this
#: test is actually about ever runs. Pinned here explicitly so a caller who
#: wants the new default constructs one deliberately instead.
_BASELINE_POST_FC_CONFIG = ControllerConfig(
    post_first_crack_control=PostFirstCrackControl(enabled=False, ceiling_guard_drop_enabled=False)
)


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
        config=config or _BASELINE_POST_FC_CONFIG,
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
        # Provider-reachable failures (model misbehaved): unchanged hold rule.
        (AdvisorFailureMode.MALFORMED, "advisor_malformed"),
        (AdvisorFailureMode.UNSAFE, "advisor_unsafe"),
        # Availability failures (1st, below the D30 fail-closed threshold):
        # tolerated REJECT, still the deterministic hold.
        (AdvisorFailureMode.TIMEOUT, "advisor_unavailable_tolerated"),
        (AdvisorFailureMode.PROVIDER_ERROR, "advisor_unavailable_tolerated"),
    ],
)
async def test_failure_outcomes_reject_and_hold_current_targets(
    mode: AdvisorFailureMode, rule: str
) -> None:
    """Every failure mode ⇒ REJECT with the named rule, no write issued, the
    rejected recommendation persisted and an ADVISORY event emitted (a single
    failure is below the D30 fail-closed threshold for every mode)."""
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
async def test_real_timeout_path_bounds_the_tick_and_holds_current_targets() -> None:
    """The elapsed-time timeout (not the scripted shortcut): an advisor that
    never resolves is bounded by ``advisory_timeout_seconds`` and the tick
    still completes with the hold fallback.

    Named "bounds", not "never blocks" (safety-reviewer finding, #747): the
    advisory call is awaited INLINE at the end of ``tick()``, so it genuinely
    delays the tick — it just cannot delay it forever. The companion
    ``test_a_slow_advisor_delays_the_tick_it_does_not_run_beside_it`` pins the
    delay itself, which this test's short timeout deliberately hides."""

    class NeverAdvisor(RoastAdvisor):
        @property
        def descriptor(self) -> AdvisorDescriptor:
            return AdvisorDescriptor(provider="test", model="never", prompt_version="t")

        async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def healthcheck(self) -> AdvisorHealth:
            return AdvisorHealth(status=AdvisorHealthStatus.REACHABLE)

    harness = harness_in_development(
        advisor=NeverAdvisor(),
        config=ControllerConfig(
            advisory_timeout_seconds=0.05,
            post_first_crack_control=PostFirstCrackControl(
                enabled=False, ceiling_guard_drop_enabled=False
            ),
        ),
    )
    harness.controller.request_advisory()
    await asyncio.wait_for(harness.controller.tick(), timeout=1.0)

    # 1st timeout: availability failure below the D30 threshold → tolerated.
    assert harness.sink.evaluations[-1].rule == "advisor_unavailable_tolerated"
    assert harness.sink.evaluations[-1].verdict is SafetyVerdict.REJECT
    assert harness.executor.targets == []


@pytest.mark.asyncio
async def test_a_slow_advisor_delays_the_tick_it_does_not_run_beside_it() -> None:
    """A slow advisor holds the tick for as long as it runs (#747 / D151).

    Written down because the opposite was believed and published: the #747
    rationale first claimed a slow model "cannot stall the loop". It can delay
    it. ``tick()`` awaits ``_maybe_run_advisory`` inline as its last statement,
    and ``RoastRunner`` runs drain-operator-queue → tick, so a call taking N
    seconds pushes out the next telemetry read, the next safety evaluation, and
    the next drain of the operator queue — which is where the in-UI EMERGENCY
    STOP is consumed. It is bounded (the test above) and fail-closed, and that
    bound is exactly why the FC-latency screen matters enough to warn about.

    Asserts the delay TRACKS advisor latency rather than asserting a wall-clock
    number, so it states the coupling without being a timing flake: the floor is
    the advisor's own sleep, and the ceiling is loose enough for a busy runner.
    """
    delay = 0.3

    class SlowAdvisor(RoastAdvisor):
        @property
        def descriptor(self) -> AdvisorDescriptor:
            return AdvisorDescriptor(provider="test", model="slow", prompt_version="t")

        async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
            await asyncio.sleep(delay)
            return RoastDecision(
                target_heat=55, target_fan=45, should_drop=False, confidence=0.9, rationale="slow"
            )

        async def healthcheck(self) -> AdvisorHealth:
            return AdvisorHealth(status=AdvisorHealthStatus.REACHABLE)

    harness = harness_in_development(
        advisor=SlowAdvisor(),
        config=ControllerConfig(
            advisory_timeout_seconds=10.0,
            post_first_crack_control=PostFirstCrackControl(
                enabled=False, ceiling_guard_drop_enabled=False
            ),
        ),
    )
    harness.controller.request_advisory()
    started = time.perf_counter()
    await harness.controller.tick()
    elapsed = time.perf_counter() - started

    # The tick cannot have finished before the advisor did: the await is inline.
    assert elapsed >= delay
    assert elapsed < delay + 2.0
    # And the advice DID land — this is the healthy path, not a timeout.
    assert harness.executor.targets == [(55, 45)]


# --- PydanticAIAdvisor + build_model factory (E8-S2 / D18) ---

# A valid structured-output payload the model "returns" via its output tool.
_VALID_OUTPUT = {
    "target_heat": 60,
    "target_fan": 50,
    "should_drop": False,
    "confidence": 0.9,
    "rationale": "steady",
}


def _function_model_returning(args: dict[str, Any]) -> FunctionModel:
    """A recorded-response double: the model always calls its output tool with
    ``args`` (the structured recommendation)."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool_name = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool_name, args)])

    return FunctionModel(respond)


def _function_model_text(text: str) -> FunctionModel:
    """A double that only ever returns prose — never the output tool, so
    structured-output extraction exhausts retries (a malformed shape)."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(respond)


def _advisor_with(model: FunctionModel, **config_kwargs: object) -> PydanticAIAdvisor:
    config = AdvisorConfig(**config_kwargs)  # type: ignore[arg-type]
    return PydanticAIAdvisor(config, model=model)


def test_pydantic_advisor_descriptor_reflects_config() -> None:
    """The advisor's trace descriptor (#167) carries the configured
    provider/model-slug/prompt-version — the identity persisted with every
    advisor decision so the #134-style failure is diagnosable from the DB."""
    advisor = _advisor_with(
        _function_model_text("{}"),
        provider="anthropic",
        model_slug="anthropic/claude-opus-4.8",
        prompt_version="v2",
    )
    descriptor = advisor.descriptor
    assert descriptor.provider == "anthropic"
    assert descriptor.model == "anthropic/claude-opus-4.8"
    assert descriptor.prompt_version == "v2"


# --- Per-phase model selection (#173) ---


def _context_in_phase(phase: RoastPhase) -> AdvisorContext:
    """A minimal valid context fixed to ``phase`` (per-phase selection seam)."""
    return AdvisorContext(
        phase=phase,
        roast_elapsed_seconds=120.0,
        development_elapsed_seconds=None,
        current_bean_temp_c=180.0,
        current_env_temp_c=190.0,
        bean_ror_c_per_min=4.0,
        env_ror_c_per_min=3.0,
        target_drop_temp_c=205.0,
        profile_name="suite",
    )


def test_advisor_selects_model_by_phase_via_recorded_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#173: ``get_recommendation`` builds (and caches) the agent for the slug
    that :meth:`AdvisorConfig.model_for` resolves for ``context.phase``.

    With no injected model, each distinct resolved slug is built offline via
    ``build_model`` (no network, no key) and cached. Asserting the cached
    agent's ``model_name`` per phase pins that the advisor selects the model by
    phase. The default would be Opus everywhere, so a custom map proves
    selection: fast post-FC, capable pre-FC — the eventual bake-off shape."""
    monkeypatch.setenv("ADVISOR_TEST_KEY", "dummy-key")
    config = AdvisorConfig(
        provider="anthropic",
        api_key_env="ADVISOR_TEST_KEY",
        model_slug="anthropic/claude-opus-4.8",
        model_slug_by_phase={
            RoastPhase.PREHEATING: "anthropic/claude-opus-4.8",
            RoastPhase.ROASTING_PRE_FIRST_CRACK: "anthropic/claude-opus-4.8",
            RoastPhase.DEVELOPMENT: "anthropic/claude-haiku-4.5",
        },
    )
    advisor = PydanticAIAdvisor(config)

    expected = {
        RoastPhase.PREHEATING: "anthropic/claude-opus-4.8",
        RoastPhase.ROASTING_PRE_FIRST_CRACK: "anthropic/claude-opus-4.8",
        RoastPhase.DEVELOPMENT: "anthropic/claude-haiku-4.5",
    }
    for phase, slug in expected.items():
        agent = advisor._agent_for(config.model_for(phase))  # type: ignore[reportPrivateUsage]
        model = agent.model
        assert isinstance(model, Model)
        assert model.model_name == slug


def test_descriptor_for_records_the_phase_resolved_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#189: descriptor_for(phase) carries the model that actually answers the
    phase's call (model_for(phase)), so the persisted advisor-decision row is
    honest once the FC/development slot is flipped — provider + prompt unchanged."""
    monkeypatch.setenv("ADVISOR_TEST_KEY", "dummy-key")
    config = AdvisorConfig(
        provider="anthropic",
        api_key_env="ADVISOR_TEST_KEY",
        model_slug="anthropic/claude-opus-4.8",
        prompt_version="v4",
        model_slug_by_phase={RoastPhase.DEVELOPMENT: "anthropic/claude-haiku-4.5"},
    )
    advisor = PydanticAIAdvisor(config)
    # Development resolves to the fast model; a phase absent from the map falls
    # back to the base slug.
    assert advisor.descriptor_for(RoastPhase.DEVELOPMENT).model == "anthropic/claude-haiku-4.5"
    assert advisor.descriptor_for(RoastPhase.PREHEATING).model == "anthropic/claude-opus-4.8"
    # Provider + prompt version stay the advisor-level identity.
    dev = advisor.descriptor_for(RoastPhase.DEVELOPMENT)
    assert dev.provider == "anthropic"
    assert dev.prompt_version == "v4"


def test_descriptor_for_defaults_to_base_descriptor() -> None:
    """The ABC default (advisors without per-phase selection) returns the base
    descriptor for every phase — e.g. FakeAdvisor."""
    advisor = FakeAdvisor([decision()])
    base = advisor.descriptor
    for phase in RoastPhase:
        assert advisor.descriptor_for(phase) == base


def test_advisor_default_pins_every_phase_to_one_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pinned-model-everywhere default (gpt-4o, #277 PIN) is a clean
    behavioral no-op: every phase resolves to the same slug, so exactly one agent
    is built (one cache entry)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    advisor = PydanticAIAdvisor(AdvisorConfig())
    for phase in (
        RoastPhase.PREHEATING,
        RoastPhase.ROASTING_PRE_FIRST_CRACK,
        RoastPhase.DEVELOPMENT,
    ):
        advisor._agent_for(advisor._config.model_for(phase))  # type: ignore[reportPrivateUsage]
    cache = advisor._agents  # type: ignore[reportPrivateUsage]
    assert set(cache) == {"openai/gpt-4o"}


@pytest.mark.asyncio
async def test_advisor_injected_model_pins_all_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded-response test seam (an injected model) drives every phase —
    per-phase resolution is bypassed so the double serves all calls, regardless
    of the per-phase map."""
    config = AdvisorConfig(
        model_slug_by_phase={
            RoastPhase.PREHEATING: "anthropic/claude-opus-4.8",
            RoastPhase.DEVELOPMENT: "anthropic/claude-haiku-4.5",
        },
    )
    advisor = PydanticAIAdvisor(config, model=_function_model_returning(_VALID_OUTPUT))
    pre = await advisor.get_recommendation(_context_in_phase(RoastPhase.PREHEATING))
    dev = await advisor.get_recommendation(_context_in_phase(RoastPhase.DEVELOPMENT))
    assert isinstance(pre, RoastDecision)
    assert isinstance(dev, RoastDecision)
    # The injected double is reused across both phases — distinct slugs in the
    # map do not build new agents when a model is injected.
    injected = advisor._injected_model  # type: ignore[reportPrivateUsage]
    assert injected is not None
    agents = advisor._agents.values()  # type: ignore[reportPrivateUsage]
    assert all(agent.model is injected for agent in agents)


# build_model factory: provider → Model for every enum value.


@pytest.mark.parametrize(
    ("provider", "expected_type"),
    [
        ("openai", OpenAIChatModel),
        ("anthropic", AnthropicModel),
        ("google", GoogleModel),
        ("ollama", OpenAIChatModel),
        ("openai_compatible", OpenAIChatModel),
    ],
)
def test_build_model_maps_every_provider(
    provider: str,
    expected_type: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construction is offline — only the model type and slug are asserted, no
    network or real key required."""
    monkeypatch.setenv("ADVISOR_TEST_KEY", "dummy-key")
    config = AdvisorConfig(
        provider=provider,  # type: ignore[arg-type]
        api_key_env="ADVISOR_TEST_KEY",
        model_slug="some-model",
    )
    model = build_model(config)
    assert isinstance(model, expected_type)
    assert model.model_name == "some-model"


def test_build_model_default_is_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default config (openai_compatible + OpenRouter base URL) preserves
    prior behavior: an OpenAI-compatible model."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    model = build_model(AdvisorConfig(model_slug="x"))
    assert isinstance(model, OpenAIChatModel)


def test_build_model_ollama_needs_no_key() -> None:
    """A keyless LAN Ollama endpoint still constructs (placeholder key)."""
    config = AdvisorConfig(
        provider="ollama",
        provider_base_url="http://localhost:11434/v1",
        api_key_env="DEFINITELY_UNSET_OLLAMA_KEY",
        model_slug="llama3",
    )
    assert isinstance(build_model(config), OpenAIChatModel)


@pytest.mark.parametrize("provider", ["openai", "openai_compatible", "ollama"])
def test_build_model_disable_transport_retries_zeroes_the_client(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#601: ``disable_transport_retries=True`` builds the underlying
    ``AsyncOpenAI`` client with ``max_retries=0`` -- removing an SDK-level
    transport retry so an experiment's request accounting is exact."""
    monkeypatch.setenv("ADVISOR_TEST_KEY", "dummy-key")
    config = AdvisorConfig(
        provider=provider,  # type: ignore[arg-type]
        api_key_env="ADVISOR_TEST_KEY",
        model_slug="some-model",
    )
    model = build_model(config, disable_transport_retries=True)
    assert isinstance(model, OpenAIChatModel)
    assert model.client.max_retries == 0


@pytest.mark.parametrize("provider", ["openai", "openai_compatible", "ollama"])
def test_build_model_disable_transport_retries_false_is_the_sdk_default(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default (``False``, and omitting the argument entirely) preserves
    today's behaviour exactly -- the SDK's own transport-retry default,
    never touched."""
    monkeypatch.setenv("ADVISOR_TEST_KEY", "dummy-key")
    config = AdvisorConfig(
        provider=provider,  # type: ignore[arg-type]
        api_key_env="ADVISOR_TEST_KEY",
        model_slug="some-model",
    )
    default_omitted = build_model(config)
    default_explicit = build_model(config, disable_transport_retries=False)
    assert isinstance(default_omitted, OpenAIChatModel)
    assert isinstance(default_explicit, OpenAIChatModel)
    assert default_omitted.client.max_retries == default_explicit.client.max_retries
    assert default_omitted.client.max_retries != 0  # the SDK default, not zeroed


def test_build_model_disable_transport_retries_pins_the_native_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#601 fold round 9, E FOLD 1 (P1): an ambient ``OPENAI_BASE_URL`` must
    NOT redirect the bespoke, retry-disabled client's requests (and its
    api_key) to an arbitrary proxy -- the #587-class credential-redirection
    hole. The native ``openai`` provider's bespoke client is pinned to the
    real endpoint regardless of the env var."""
    monkeypatch.setenv("ADVISOR_TEST_KEY", "dummy-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil-proxy.example/v1")
    config = AdvisorConfig(
        provider="openai",
        api_key_env="ADVISOR_TEST_KEY",
        model_slug="some-model",
    )
    model = build_model(config, disable_transport_retries=True)
    assert isinstance(model, OpenAIChatModel)
    assert str(model.client.base_url) == "https://api.openai.com/v1/"


# PydanticAIAdvisor behind the recorded-response double.


def _context() -> AdvisorContext:
    return AdvisorContext(
        phase=RoastPhase.DEVELOPMENT,
        roast_elapsed_seconds=300.0,
        development_elapsed_seconds=30.0,
        current_bean_temp_c=200.0,
        current_env_temp_c=210.0,
        bean_ror_c_per_min=5.0,
        env_ror_c_per_min=4.0,
        target_drop_temp_c=205.0,
        profile_name="suite",
    )


@pytest.mark.asyncio
async def test_pydanticai_advisor_returns_validated_decision() -> None:
    advisor = _advisor_with(_function_model_returning(_VALID_OUTPUT))
    decision = await advisor.get_recommendation(_context())
    assert isinstance(decision, RoastDecision)
    assert (decision.target_heat, decision.target_fan, decision.should_drop) == (60, 50, False)


@pytest.mark.asyncio
async def test_pydanticai_advisor_captures_last_usage() -> None:
    """Each successful call records token usage for cost/observability."""
    advisor = _advisor_with(_function_model_returning(_VALID_OUTPUT))
    assert advisor.last_usage is None
    await advisor.get_recommendation(_context())
    assert advisor.last_usage is not None
    assert advisor.last_usage.input_tokens > 0
    assert advisor.last_usage.total_tokens >= advisor.last_usage.output_tokens


def test_reasoning_extra_body_maps_effort_levels() -> None:
    assert reasoning_extra_body(None) is None
    assert reasoning_extra_body("off") == {"reasoning": {"enabled": False}}
    assert reasoning_extra_body("minimal") == {"reasoning": {"effort": "minimal"}}
    assert reasoning_extra_body("low") == {"reasoning": {"effort": "low"}}
    assert reasoning_extra_body("medium") == {"reasoning": {"effort": "medium"}}
    assert reasoning_extra_body("high") == {"reasoning": {"effort": "high"}}


def test_usage_from_run_extracts_reasoning_tokens() -> None:
    """Reasoning tokens are read from provider ``details`` when present, else
    ``None`` — the cost-tax signal."""
    with_reasoning = SimpleNamespace(
        input_tokens=100, output_tokens=50, total_tokens=150, details={"reasoning_tokens": 42}
    )
    usage = usage_from_run(with_reasoning)
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (100, 50, 150)
    assert usage.reasoning_tokens == 42

    without = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15, details={})
    assert usage_from_run(without).reasoning_tokens is None


@pytest.mark.asyncio
async def test_pydanticai_advisor_threads_reasoning_effort() -> None:
    """A configured reasoning_effort is wired into the agent's model_settings
    (extra_body) without breaking the call path."""
    advisor = _advisor_with(_function_model_returning(_VALID_OUTPUT), reasoning_effort="off")
    decision = await advisor.get_recommendation(_context())
    assert isinstance(decision, RoastDecision)


@pytest.mark.asyncio
async def test_pydanticai_advisor_out_of_range_is_unsafe() -> None:
    """Well-shaped output that violates the RoastDecision bounds (heat 150)
    maps to AdvisorUnsafeOutputError — not malformed."""
    advisor = _advisor_with(_function_model_returning({**_VALID_OUTPUT, "target_heat": 150}))
    with pytest.raises(AdvisorUnsafeOutputError):
        await advisor.get_recommendation(_context())


@pytest.mark.asyncio
async def test_pydanticai_advisor_unparseable_shape_is_malformed() -> None:
    """A model that never produces the output tool exhausts retries →
    AdvisorMalformedOutputError."""
    advisor = _advisor_with(_function_model_text("I cannot help with that."))
    with pytest.raises(AdvisorMalformedOutputError):
        await advisor.get_recommendation(_context())


@pytest.mark.asyncio
async def test_pydanticai_advisor_transport_failure_is_provider_error() -> None:
    """A transport/API failure (ModelHTTPError) maps to AdvisorProviderError."""

    def boom(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="x", body="upstream down")

    advisor = _advisor_with(FunctionModel(boom))
    with pytest.raises(AdvisorProviderError):
        await advisor.get_recommendation(_context())


@pytest.mark.asyncio
async def test_pydanticai_advisor_does_not_swallow_timeout() -> None:
    """asyncio TimeoutError propagates so the controller's wait_for owns the
    timeout (it must not be reclassified as a provider error)."""

    def hang(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise TimeoutError("simulated cancel")

    advisor = _advisor_with(FunctionModel(hang))
    with pytest.raises(TimeoutError):
        await advisor.get_recommendation(_context())


@pytest.mark.asyncio
async def test_pydanticai_advisor_logs_context_hash_not_raw_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The context is logged as a hash, never as the raw payload (plan policy:
    log prompt input hashes, not large payloads)."""
    advisor = _advisor_with(_function_model_returning(_VALID_OUTPUT))
    context = _context()
    with caplog.at_level("INFO", logger="roastpilot_agent.advisor"):
        await advisor.get_recommendation(context)
    records = [r for r in caplog.records if r.message == "advisory request"]
    assert records
    record = records[0]
    assert getattr(record, "context_hash", None)
    # The raw profile name (a context field) must not appear in the log text.
    assert context.profile_name not in caplog.text


@pytest.mark.asyncio
async def test_rendered_prompt_carries_actuated_heat_fan_and_loop_flag() -> None:
    """#497: the context JSON the model actually receives (the ``agent.run``
    user message, i.e. ``context.model_dump_json()``) includes the new
    actuated-lever fields — no separate template-rendering step to keep in
    sync, since the context IS the rendered user message."""
    captured: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        for part in last.parts:
            text = getattr(part, "content", None)
            if isinstance(text, str):
                captured.append(text)
        tool_name = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool_name, _VALID_OUTPUT)])

    advisor = _advisor_with(FunctionModel(respond))
    context = _context().model_copy(
        update={
            "current_heat_percent": 65,
            "current_fan_percent": 45,
            "post_fc_loop_active": True,
        }
    )
    await advisor.get_recommendation(context)
    assert captured, "the model must have received a user message"
    rendered = "\n".join(captured)
    assert '"current_heat_percent":65' in rendered
    assert '"current_fan_percent":45' in rendered
    assert '"post_fc_loop_active":true' in rendered


@pytest.mark.asyncio
async def test_rendered_prompt_baseline_mode_carries_loop_flag_false() -> None:
    """Baseline (loop inactive): the rendered context carries
    ``post_fc_loop_active: false`` — the model can tell the two regimes apart
    from the context alone."""
    captured: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        for part in last.parts:
            text = getattr(part, "content", None)
            if isinstance(text, str):
                captured.append(text)
        tool_name = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool_name, _VALID_OUTPUT)])

    advisor = _advisor_with(FunctionModel(respond))
    context = _context().model_copy(update={"current_heat_percent": 65, "current_fan_percent": 45})
    await advisor.get_recommendation(context)
    rendered = "\n".join(captured)
    assert '"post_fc_loop_active":false' in rendered


def test_c1_teaches_actuated_levers_are_ground_truth() -> None:
    """The c1 frame (and every derived control-teaching version, since c2-c6
    splice into c1) must teach the model that current_heat_percent /
    current_fan_percent — not its own memory of a prior recommendation — are
    the real actuated levers, and that in loop mode (post_fc_loop_active) its
    heat number is advisory-only (#497: the 11 Jul evidence where the advisor
    reasoned "heat is already at its minimum" while the taper actually held it
    at 65 %)."""
    for version in ("c1", "c3", "c6"):
        prompt = control_teaching_prompt(version)
        lower = prompt.lower()
        assert "current_heat_percent" in lower, version
        assert "current_fan_percent" in lower, version
        assert "post_fc_loop_active" in lower, version
        assert "advisory-only" in lower, version


def test_c1_actuated_levers_section_names_no_numbers() -> None:
    """Same #218 two-copies discipline as the rest of c1: the new section
    teaches the PRINCIPLE (read the actuated fields, don't assume) and names
    no hardcoded heat/fan duty numbers."""
    prompt = control_teaching_prompt("c1")
    section_start = prompt.index("ACTUATED LEVERS")
    section_end = prompt.index("DEVELOPMENT NUMBERS")
    section = prompt[section_start:section_end]
    lower = section.lower()
    assert re.search(r"\bfan\s+\d+\b", lower) is None
    assert re.search(r"\bheat\s+\d+\b", lower) is None


def test_c1_teaches_the_joint_drop_objective_not_first_past_the_post() -> None:
    """#499 (D89): the 11 Jul A/B showed the advisor treats whichever drop
    target arrives first as the finish line (roast 1 dropped on DTR alone at
    190 °C, 5 °C short; roast 2 dropped on temperature alone at 194 °C, 1.6 pp
    short of DTR). c1 (and every derived version) must teach the JOINT
    objective: satisfy both, prefer a modest overshoot of one while closing
    the other, and let the ceiling (never either target alone) force an early
    call."""
    for version in ("c1", "c3", "c6"):
        prompt = control_teaching_prompt(version)
        lower = prompt.lower()
        assert "joint objective" in lower, version
        assert "first-past-the-post" in lower, version
        assert "modest overshoot" in lower, version
        assert "ceiling forces the call" in lower, version


def test_c1_joint_drop_section_names_no_numbers() -> None:
    """Same #218 two-copies discipline: the joint-drop section teaches the
    PRINCIPLE and names no hardcoded temperature/DTR numbers — those come
    from context (target_drop_temp_c, the DTR window, the bitter ceiling)."""
    prompt = control_teaching_prompt("c1")
    section_start = prompt.index("THE DROP - A JOINT OBJECTIVE")
    section_end = prompt.index("LEVER STABILITY")
    section = prompt[section_start:section_end]
    assert not re.search(r"\d", section)


def test_c1_joint_drop_section_distinguishes_drop_target_from_bitter_ceiling() -> None:
    """#499: roast 2's final rationale conflated the drop target with the
    bitter ceiling (called it "195" when the profile's drop target was 195
    and the bitter ceiling was 196) — the prompt must explicitly teach the
    model these are DIFFERENT MEANINGS, never interchangeable.

    #563 REVISION: ``RoastControlPolicy._bitter_ceiling_temp_c`` no longer caps
    the told ceiling at the profile's ``target_drop_temp_c`` at all (the #499-era
    cap made the two numbers IDENTICAL on every seeded profile — 195 capped
    against 196 — which the #563 bake-off traced to a false "no overshoot room"
    inference in the c7/c8 gap arithmetic). The teaching must still not claim
    they always differ in VALUE (a config coincidence can still make them equal);
    it must teach that the MEANING differs regardless, and that the ceiling is a
    SEPARATE number that does not move with the target — the opposite emphasis
    from the pre-#563 text, which read as "they usually coincide" rather than
    "they usually differ". Anchor the always-distinct claim to the
    emergency-drop bound, unaffected by this change."""
    prompt = control_teaching_prompt("c1")
    section_start = prompt.index("THE DROP - A JOINT OBJECTIVE")
    section_end = prompt.index("LEVER STABILITY")
    section = prompt[section_start:section_end].lower()
    assert "target_drop_temp_c" in section
    assert "bitter" in section and "ceiling" in section
    assert "different meanings" in section
    # #563: the ceiling is taught as a SEPARATE number, not derived from /
    # moving with the target — the replacement for the pre-#563 "equals the
    # target exactly" framing that no longer matches the common case.
    assert "separate" in section
    assert "does not move with it" in section or "not derived from the target" in section
    assert "emergency" in section and "higher" in section
    # Must NOT claim any fixed ORDERING between the ceiling and the target —
    # safety-reviewer LOW fold: an earlier draft said the ceiling is "never
    # below the target", which is false whenever target_drop_temp_c is ABOVE
    # the ceiling (an unbounded profile field — this module's own PROFILE
    # fixture is target_drop_temp_c=205.0, above the 196 default ceiling, so
    # RoastControlPolicy resolves bitter_ceiling_temp_c=196.0, BELOW the
    # target). No relational claim survives that a real profile can falsify
    # either way.
    assert "never below the target" not in section
    assert "never above the target" not in section
    # Must NOT claim the two USUALLY coincide either — the #563-superseded
    # framing this test used to pin (they now usually differ: target 195 vs
    # ceiling 196 on every seeded profile).
    assert "equals the target exactly" not in section


def test_c1_joint_drop_section_makes_no_false_ceiling_ordering_for_a_high_target_profile() -> None:
    """Safety-reviewer LOW (#499 Codex follow-up): renders the REAL resolved
    control box for this module's own PROFILE (target_drop_temp_c=205.0,
    above the 196 °C default hard bitter ceiling) and confirms the actual
    told bitter_ceiling_temp_c is BELOW the target in this case — the exact
    scenario that falsified the earlier "(never below the target)" claim.
    The prompt section must carry no relational claim a profile like this
    one can falsify."""
    from roastpilot_agent.config import SafetyLimits as _SafetyLimits
    from roastpilot_agent.control_policy import RoastControlPolicy

    policy = RoastControlPolicy(_SafetyLimits(), PROFILE)
    limits = policy.limits_for(RoastPhase.DEVELOPMENT)
    assert limits.bitter_ceiling_temp_c < PROFILE.target_drop_temp_c, (
        "this test's premise requires a target ABOVE the hard bitter ceiling"
    )
    prompt = control_teaching_prompt("c1")
    section_start = prompt.index("THE DROP - A JOINT OBJECTIVE")
    section_end = prompt.index("LEVER STABILITY")
    section = prompt[section_start:section_end].lower()
    assert "never below the target" not in section
    assert "never above the target" not in section


def test_c1_joint_drop_section_teaches_window_as_judgment_ceiling_as_law() -> None:
    """#499 (operator riders): the DTR window's edges are JUDGMENT SPACE (a
    little under/over is fine while the other target closes the gap); the
    bitter/emergency ceiling is LAW (never a matter of judgment). A
    qualitative roast style, if present, is read as INTENT only — it never
    overrides the profile's own authoritative explicit targets (D84 held)."""
    prompt = control_teaching_prompt("c1")
    section_start = prompt.index("THE DROP - A JOINT OBJECTIVE")
    section_end = prompt.index("LEVER STABILITY")
    section = prompt[section_start:section_end].lower()
    assert "acceptable development window" in section
    assert "judgment space" in section
    assert "law" in section
    assert "never overrides" in section
    assert "authoritative" in section


def test_c1_dtr_window_examples_are_not_swapped() -> None:
    """Codex P2 follow-up (#499): the window-edge examples were originally
    swapped — the text said "a little under is fine while TEMPERATURE closes
    the gap" (wrong: under-DTR means DEVELOPMENT is the thing still closing)
    and "a little over is fine while DEVELOPMENT closes it" (wrong:
    over-DTR-while-temp-short means TEMPERATURE is the thing still closing).
    This taught backward boundary reasoning in the exact case #499 exists to
    fix. Pins the corrected clause order directly."""
    prompt = control_teaching_prompt("c1")
    section_start = prompt.index("THE DROP - A JOINT OBJECTIVE")
    section_end = prompt.index("LEVER STABILITY")
    section = prompt[section_start:section_end].lower()
    assert (
        "a little under the window is fine while development itself is still "
        "the gap closing" in section
    )
    assert "a little over the window is fine while temperature is the gap still closing" in section


def test_instructions_for_known_and_unknown_version() -> None:
    assert "coffee roaster" in instructions_for("v0").lower()
    with pytest.raises(ValueError):
        instructions_for("does-not-exist")


def test_v1_prompt_is_electric_roaster_tuned() -> None:
    """v1 (E8-S4) encodes the electric-roaster reality the bake-off tuned for:
    thermal lag, decisive early action, and maximizing development time."""
    v1 = instructions_for("v1").lower()
    assert "electric" in v1
    assert "thermal lag" in v1
    assert "development time" in v1
    # The advisory-only invariant survives the rewrite.
    assert "never control hardware" in v1


def test_v2_prompt_adds_fan_and_duration() -> None:
    """v2 (E8-S4 refinement) is the default prompt — it must encode the Hottop
    refinements: fan as a coupled heat-transfer-mode lever, and development
    duration (ratio) as the objective rather than a drop temperature."""
    v2 = instructions_for("v2").lower()
    # Fan as transfer-mode / flavor lever, not just a coolant.
    assert "fan" in v2
    assert "convective" in v2
    # Duration objective + the 10-20% ratio band.
    assert "duration" in v2
    assert "10-20%" in v2
    # Carried-forward invariants.
    assert "thermal lag" in v2
    assert "never control hardware" in v2


def test_v3_prompt_has_explicit_per_stage_sections() -> None:
    """v3 (#172, Option 2) is one prompt with explicit per-stage sections —
    PREHEAT / DRYING-MAILLARD / FC-DEVELOPMENT — each aimed at its target, while
    keeping v2's electric Hottop framing and the advisory-only invariant. It is
    a first draft pending bake-off validation (#173); v2 stays the default."""
    v3 = instructions_for("v3").lower()
    # All three stage sections are present and labelled.
    assert "preheat" in v3
    assert "maillard" in v3
    assert "first crack" in v3
    assert "development" in v3
    # PREHEAT aims at the charge guidance band.
    assert "charge_guidance_min_c" in v3
    assert "charge_guidance_max_c" in v3
    # FC/development aims at the development-ratio target.
    assert "target_development_percent" in v3
    # Carried-forward electric Hottop framing + advisory-only invariant.
    assert "thermal lag" in v3
    assert "convective" in v3
    assert "never control hardware" in v3


def test_c3_is_the_default_prompt_version() -> None:
    """c3 (the #274 control teaching SYSTEM frame + the roast-2 development-stretch
    section + the roast-3 fan-as-active-brake section) is the shipped default, wired
    live for the post-FC control loop. It resolves to the control teaching prompt
    and is distinct from the v* per-tick lenses; c1 and c2 stay selectable for an
    A/B."""
    assert AdvisorConfig().prompt_version == "c3"
    assert instructions_for("c3") == control_teaching_prompt("c3")
    # c1 and c2 are kept intact and selectable (prompts are versioned, #274/#328).
    assert instructions_for("c1") == control_teaching_prompt("c1")
    assert instructions_for("c2") == control_teaching_prompt("c2")
    assert instructions_for("c1") != instructions_for("c2")
    assert instructions_for("c2") != instructions_for("c3")
    # c4 is added selectable (the #396 drop-decisiveness A/B arm); c3 stays default.
    assert instructions_for("c4") == control_teaching_prompt("c4")
    assert instructions_for("c3") != instructions_for("c4")
    # c5 is added selectable (the #396 heat-floor A/B arm); c3 stays default.
    assert instructions_for("c5") == control_teaching_prompt("c5")
    assert instructions_for("c4") != instructions_for("c5")
    # c6 is added selectable (the #396 over-braked recovery A/B arm); c3 stays default.
    assert instructions_for("c6") == control_teaching_prompt("c6")
    assert instructions_for("c5") != instructions_for("c6")
    # c7 is added selectable (the #499 part-2 DTR-pace-mismatch A/B arm); c3 stays
    # default.
    assert instructions_for("c7") == control_teaching_prompt("c7")
    assert instructions_for("c6") != instructions_for("c7")
    assert instructions_for("c3") != instructions_for("v4")
    assert instructions_for("v4") != instructions_for("v2")
    assert instructions_for("v3") != instructions_for("v2")


def test_c2_extends_c1_with_post_fc_development_stretch() -> None:
    """c2 is c1 PLUS a post-FC stretch section, with all of c1's grounding kept.

    Roast 2 (run c3b84625): the advisor rode a mid heat level post-FC so the bean
    raced to the drop ceiling under-developed. c2 adds the teaching to cut heat
    aggressively at/after first crack to stretch development toward target, never
    ride a mid level, and never cross the bitter ceiling — while keeping c1's
    told==enforced grounding (uses context dev% verbatim / never invent) and the
    #218/#274 lever-stability framing. It names NO numbers (the live limits carry
    every threshold).

    Codex P2 follow-up (#499): this section originally said "NEVER overshoot
    the drop target" / called the ceiling "the LATEST acceptable drop" —
    directly contradicting #499's later joint-objective section (spliced
    EARLIER in the assembled text but read as if it were being refined/
    overridden by this LATER text), which explicitly prefers a modest
    overshoot of one target while closing the other. Reworded to target the
    BITTER CEILING specifically (the one number that is genuinely law,
    unaffected by #499) rather than the drop temperature target (which #499
    now teaches may be modestly overshot).
    """
    c1 = control_teaching_prompt("c1")
    c2 = control_teaching_prompt("c2")
    # c2 is a strict superset of c1's grounding: every c1 line survives.
    assert c1 in c2 or all(block in c2 for block in c1.split("\n\n")), (
        "c2 must preserve c1's grounding verbatim"
    )
    lowered = c2.lower()
    # The new post-FC stretch teaching.
    assert "stretch development" in lowered
    assert "development time ratio" in lowered or "dtr" in lowered
    assert "cut heat" in lowered  # the aggressive heat cut is the lever
    # Do NOT ride a mid heat level (the explicit roast-2 anti-pattern).
    assert "mid heat level" in lowered
    assert "50 %" in c2  # the concrete anti-pattern example
    # Never cross the bitter ceiling (reworded post-#499 — never the DROP
    # TARGET, which #499 teaches may be modestly overshot).
    assert "never cross the indicated bitter ceiling" in lowered
    assert "never overshoot the drop target" not in lowered
    # Still grounds dev numbers to context (c1's anti-hallucination language kept).
    assert "verbatim" in lowered and "never invent" in lowered
    # The new section names NO fixed drop/dev THRESHOLD numbers of its own
    # (told==enforced; the live limits carry every threshold). It must not bake in
    # the roast-2 figures (195 ceiling / 178 FC / 13 % DTR target).
    start = c2.index("POST-FIRST-CRACK")
    new_section = c2[start : c2.index("THE OBJECTIVE\n", start)]
    assert "195" not in new_section
    assert "178" not in new_section
    assert "13 %" not in new_section


def test_c3_extends_c2_with_post_fc_fan_brake() -> None:
    """c3 is c2 PLUS a post-FC fan-as-active-brake section, with all of c1+c2's
    grounding kept.

    Roast 3 (Ethiopia Koke): the advisor held fan at 30-40 while it cut heat to 0,
    so the bean coasted 193->203 (8 C past the 195 ceiling) with no brake left. c3
    teaches fan as an active post-FC lever — raise airflow at/after FC, and
    ESPECIALLY when heat is already 0 and the bean is still climbing (fan is then
    the only remaining brake) — while keeping the #218/#274 lever-stability
    discipline (a deliberate step up, NOT the 30<->40<->50 twiddle) and c2's
    stretch-development teaching. It names NO numbers (the live fan box / drop
    ceiling carry every threshold)."""
    c2 = control_teaching_prompt("c2")
    c3 = control_teaching_prompt("c3")
    # c3 is a strict superset of c2's grounding: every c2 line survives.
    assert c2 in c3 or all(block in c3 for block in c2.split("\n\n")), (
        "c3 must preserve c2's grounding verbatim"
    )
    lowered = c3.lower()
    # The new post-FC fan-brake teaching.
    assert "fan is an active brake" in lowered or "fan is the only brake left" in lowered
    assert "raise the fan" in lowered or "raise airflow" in lowered
    # The critical "heat already at 0 → fan is the only brake" teaching.
    assert "only brake left" in lowered
    assert "heat" in lowered and ("at 0" in lowered or "0:" in lowered or "floor" in lowered)
    # Keeps the lever-stability discipline: a deliberate step, NOT a twiddle.
    assert "twiddle" in lowered
    assert "deliberate" in lowered or "intentional" in lowered


def test_c4_extends_c3_with_drop_decisiveness() -> None:
    """c4 is c3 PLUS a brake-vs-drop decisiveness section, with all of c1+c2+c3
    grounding kept.

    #277 finalists bake-off: on c3 gpt-4o never recommended the drop on 2 roasts —
    it stated in its rationale that development was at target and the bean at the
    drop temperature, then cut heat to 0 and raised fan (the c3 fan-brake) instead
    of dropping, and recovered to a clean drop on c1. c4 makes the brake<->drop
    boundary explicit: the brake shapes the APPROACH while behind target; once in
    the drop window the decision is should_drop=TRUE, not more braking. It keeps
    c3's fan-brake + c2's stretch + c1's grounding, and names NO numbers."""
    c3 = control_teaching_prompt("c3")
    c4 = control_teaching_prompt("c4")
    # c4 is a strict superset of c3's grounding: every c3 line survives.
    assert c3 in c4 or all(block in c4 for block in c3.split("\n\n")), (
        "c4 must preserve c3's grounding verbatim"
    )
    lowered = c4.lower()
    # The new brake-vs-drop decisiveness teaching.
    assert "braking is not the finish" in lowered
    assert "should_drop = true" in lowered
    # The specific failure: stating the conditions then braking instead of dropping.
    assert "that sentence is the drop signal" in lowered
    assert "holding or braking rather than dropping" in lowered
    # Keeps c3's fan-brake teaching (c4 is a superset of c3).
    assert "fan is an active brake" in lowered or "only brake left" in lowered
    # The new section names NO fixed drop/dev threshold numbers of its own
    # (told==enforced; the live limits carry every threshold).
    start = c4.index("WHEN YOU ARE IN THE DROP WINDOW")
    new_section = c4[start : c4.index("THE OBJECTIVE\n", start)]
    assert "195" not in new_section
    assert "13 %" not in new_section
    # The c2 stretch teaching is still present (strict superset).
    assert "stretch development" in lowered
    # The new section names NO fixed temperature/limit THRESHOLD numbers of its own
    # (told==enforced; the live fan box / drop ceiling carry every threshold). It
    # must not bake in the roast-3 figures (195 ceiling, 203 overshoot, 239 env) nor
    # a literal fan target. (The "30<->40<->50" oscillation example is the SAME
    # anti-pattern illustration c1 already carries — an anti-pattern to avoid, not a
    # threshold to enforce — so those digits are allowed; the test below forbids only
    # the figures that would actually drift from the live limits.)
    start = c3.index("POST-FIRST-CRACK: FAN")
    new_section = c3[start : c3.index("THE OBJECTIVE\n", start)]
    for literal in ("195", "203", "239"):
        assert literal not in new_section, f"c3 fan section must not bake in {literal!r}"


def test_c5_extends_c4_with_heat_floor() -> None:
    """c5 is c4 PLUS a heat-floor / keep-climbing section, with all of c1+c2+c3+c4
    grounding kept.

    Roast 7 (run b74153ed): on c3 gpt-4o cut heat to 0 immediately at first crack and
    ramped fan 50->100, crashing the RoR so the bean stalled at 188 C while the DTR
    clock reached the 16 % target — an under-temp drop 7 C below the 195 target. c2's
    "the two arrive together" intent is right but nothing taught the HEAT FLOOR that
    keeps the bean climbing. c5 adds it: a low POSITIVE RoR held by a heat floor so
    the bean reaches the drop temperature as development hits target. It keeps c4's
    drop-decisiveness + c3's fan-brake + c2's stretch + c1's grounding, names NO
    numbers."""
    c4 = control_teaching_prompt("c4")
    c5 = control_teaching_prompt("c5")
    # c5 is a strict superset of c4's grounding: every c4 line survives.
    assert c4 in c5 or all(block in c5 for block in c4.split("\n\n")), (
        "c5 must preserve c4's grounding verbatim"
    )
    lowered = c5.lower()
    # The new heat-floor / keep-climbing teaching.
    assert "keep the bean climbing to the drop temperature" in lowered
    assert "heat floor" in lowered
    assert "drops too cool" in lowered
    assert "positive rate of rise" in lowered  # the low-but-positive RoR target
    assert "restore some heat" in lowered  # the corrective when over-braked
    assert "flatten it to zero" in lowered  # fan TRIMS the RoR, does not flatten it
    # Keeps c4's drop-decisiveness + c3's fan-brake + c2's stretch (strict superset).
    assert "braking is not the finish" in lowered
    assert "fan is an active brake" in lowered or "only brake left" in lowered
    assert "stretch development" in lowered
    # The new section names NO fixed drop/dev/temperature THRESHOLD numbers of its
    # own (told==enforced; the live limits carry every threshold). It must not bake
    # in the roast-7 figures (188 stall / 195 drop / 16 % DTR).
    start = c5.index("KEEP THE BEAN CLIMBING")
    new_section = c5[start : c5.index("THE OBJECTIVE\n", start)]
    for literal in ("188", "195", "16 %", "13 %"):
        assert literal not in new_section, f"c5 heat-floor section must not bake in {literal!r}"


def test_c6_extends_c5_with_recovery() -> None:
    """c6 is c5 PLUS an explicit over-braked recovery section, with all of
    c1+c2+c3+c4+c5 grounding kept.

    c5 bake-off evidence: gpt-4o (and Gemini) read 'heat is already 0' from
    the over-braked Colombia recordings as a reason to HOLD — the c5 heat-floor
    section's general "restore some heat if you have braked too hard" was not
    enough; the model treated the literal heat=0 context as confirming a settled
    state. c6 makes the REVERSE action explicit: heat=0 + bean below drop temp
    + development short = the over-braked state → RESTORE heat to a positive
    value now. It names NO fixed numbers (the live limits carry every threshold).
    """
    c5 = control_teaching_prompt("c5")
    c6 = control_teaching_prompt("c6")
    # c6 is a strict superset of c5's grounding: every c5 line survives.
    assert c5 in c6 or all(block in c6 for block in c5.split("\n\n")), (
        "c6 must preserve c5's grounding verbatim"
    )
    lowered = c6.lower()
    # The new over-braked recovery teaching.
    assert "restore heat" in lowered
    assert "recommend a positive target_heat" in lowered or "positive target_heat" in lowered
    assert "that reading is the trap" in lowered
    # Keeps c5's heat-floor teaching (strict superset).
    assert "heat floor" in lowered
    # Keeps c4's brake-vs-drop and c2's stretch (strict superset via c5).
    assert "braking is not the finish" in lowered
    # The new section names NO fixed temperature/limit threshold numbers of its own
    # (told==enforced; the live limits carry every threshold). It must not bake in
    # the Colombia fixture figures (188 / 195 / 16 % / 13 %).
    start = c6.index("POST-FIRST-CRACK: IF HEAT IS ALREADY 0")
    new_section = c6[start : c6.index("THE OBJECTIVE\n", start)]
    for literal in ("188", "195", "16 %", "13 %"):
        assert literal not in new_section, f"c6 recovery section must not bake in {literal!r}"


def test_c7_extends_c6_with_dtr_pace_mismatch() -> None:
    """c7 is c6 PLUS the #499 part-2 DTR-ahead-of-temperature pace-mismatch
    section, with all of c1+c2+c3+c4+c5+c6 grounding kept.

    Roast 13 (13 Jul, El Durazno white honey): the #499-part-1 joint-objective
    window teaching (already present in c1, inherited by c6) told the model to
    prefer a MODEST overshoot of one target while the other closes the gap,
    but had no notion of a LARGE overshoot signalling the DTR clock itself is
    unreliable evidence. The advisor read DTR past the window's top edge as a
    finish line and dropped at 190 C, 5 C short of the 195 C target (DTR had
    outrun temperature progress roughly 2:1). c7 adds the missing distinction:
    a DTR well past the window top NEXT TO a temperature still materially
    short is a pace mismatch, not a signal to drop — weight temperature
    progress as dominant and keep watching the ceiling, which stays law.
    """
    from roastpilot_agent.advisor import (
        _C7_DTR_PACE_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    c6 = control_teaching_prompt("c6")
    c7 = control_teaching_prompt("c7")
    # c7 is a strict superset of c6's grounding: every c6 line survives.
    assert c6 in c7 or all(block in c7 for block in c6.split("\n\n")), (
        "c7 must preserve c6's grounding verbatim"
    )
    lowered = c7.lower()
    # The new DTR-pace-mismatch teaching.
    assert "pace mismatch" in lowered
    assert "not a finish line" in lowered or "not permission to drop early" in lowered
    assert "well past the top" in lowered or "well past the window top" in lowered
    # Explicitly scoped to DTR-ahead-of-temperature only — never broadens into
    # the opposite pace direction (temperature/heat ahead, development behind,
    # #405/roast-14-shaped), which stays the c2/c5 sections' territory.
    assert "opposite case" in lowered
    # Keeps the #499 joint-objective framing it refines (strict superset via c6/c1).
    assert "joint objective" in lowered
    assert "modest overshoot" in lowered
    # Ceiling still forces the drop unconditionally in the new section too —
    # via an explicit NUMERIC COMPARISON, not a restated "it is LAW" emphasis
    # (the #499/#559 combined bake-off falsified the emphasis-repetition
    # phrasing: both c7 and c8 hallucinated "at the ceiling" on the roast-15
    # bottom-edge case at bean 187 / ceiling 195, a real 8 °C gap, while c3 —
    # which never repeats the ceiling emphasis — read the numbers correctly
    # 6/6). The revised teaching must make the model STATE the two numbers
    # and COMPUTE the gap, never reason from a felt sense of proximity.
    #
    # PR #564 round 1 (Codex P2, test scoping): asserted against the SECTION
    # CONSTANT directly (not the full assembled `lowered`), matching the
    # discipline c8's own test now uses — c7's new section does not itself
    # inherit this wording from an earlier version, so this specific test
    # was not reachable by the inheritance bug the reviewer found in c8, but
    # scoping it the same way is the more robust pattern regardless (a
    # future c9 splicing after c7 would otherwise create the identical risk
    # here).
    new_section_lowered = _C7_DTR_PACE_SECTION.lower()
    assert "compute the gap" in new_section_lowered
    assert "never on a felt sense" in new_section_lowered
    assert "ceiling" in new_section_lowered
    # PR #564 round 2 (Codex P2 x3, all one root cause -- the anticipatory
    # clause referenced quantities the model cannot evaluate from context):
    # the reframe drops the lag reference ENTIRELY (round-1's "the time a
    # drop takes to act" invited an invented constant), teaches the gap in
    # the SAME per-minute units as bean_ror_c_per_min (no seconds, no unit
    # conversion to botch), and names bitter_ceiling_temp_c EXPLICITLY --
    # never emergency_drop_temp_c, the separate e-stop bound, not the
    # planning ceiling. "Never waiting for the gap to literally hit zero"
    # stays: the round-1 anticipatory lesson (gap==0 is too late) is
    # preserved, just re-expressed without an unevaluable lag term.
    assert "bean_ror_c_per_min" in _C7_DTR_PACE_SECTION
    assert "bitter_ceiling_temp_c" in _C7_DTR_PACE_SECTION
    assert "emergency_drop_temp_c" in _C7_DTR_PACE_SECTION
    assert "small fraction" in new_section_lowered
    assert "per minute" in new_section_lowered or "per-minute" in new_section_lowered
    assert "never waiting for the gap to literally hit zero" in new_section_lowered
    # Finding 5 (round 2): no seconds/lag reference anywhere in the new
    # section -- the exact defect the reframe removes. A cheap, direct lock
    # against a future edit reintroducing an invented time constant.
    assert "second" not in new_section_lowered
    # The new section names NO fixed roast-13 fixture numbers of its own
    # (told==enforced; the live context carries every threshold/target).
    start = c7.index("POST-FIRST-CRACK: A LARGE DTR OVERSHOOT")
    new_section = c7[start : c7.index("THE OBJECTIVE\n", start)]
    for literal in ("190", "195", "205", "5 C", "87 s", "46.7"):
        assert literal not in new_section, f"c7 DTR-pace section must not bake in {literal!r}"


def test_c8_extends_c7_with_pace_bottom_edge_and_fan_coupling() -> None:
    """c8 is c7 PLUS the D96 slice 2 (#559) teaching, with all of
    c1+c2+c3+c4+c5+c6+c7 grounding kept.

    Roast 15 (15 Jul, Sumatra, run 8ac8a5e4): the advisor pushed fan 30->90
    in the first minute of development, crashing RoR 7->3 C/min; the D88
    (pre-D96) post-FC loop had zero authority to compensate (heat sat
    ceiling-locked at the trim-60 entry value), so the bean crawled 183->188 C
    over 115 s. At DTR 16.3 (window bottom 16 with the dev-19 target) the
    advisor dropped citing "in range" at 188 C, 7 C short of target — the
    bottom-edge mirror of c7's top-edge fix (roast 13). c8 adds: (1) a
    PACE-comparison framing that acts earlier than waiting for an edge
    crossing, (2) the explicit bottom-edge-is-not-a-finish-line teaching, and
    (3) fan->RoR coupling teaching that stays CONSISTENT with D96 slice 1's
    actual shipped mechanism (the heat loop's compensation is bounded and
    suppressed near a drop, never an unconditional rescue).

    #499/#559 combined bake-off amendment (issue #562): the ceiling sentence
    in (2) originally restated "ceiling stays LAW" — the SAME emphasis
    phrasing that (independently) falsified in c7 (below). Both c7 and c8
    hallucinated "at the ceiling" on the roast-15 bottom-edge case (bean 187,
    ceiling 195, an 8 C gap) while c3 read the numbers correctly 6/6. Revised
    to an explicit numeric-comparison instruction (state the two numbers,
    compute the gap) here too."""
    from roastpilot_agent.advisor import (
        _C8_PACE_BOTTOM_EDGE_AND_FAN_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    c7 = control_teaching_prompt("c7")
    c8 = control_teaching_prompt("c8")
    # c8 is c7 with the new section spliced in exactly before "THE OBJECTIVE"
    # (the SAME splice advisor.py itself performs) -- an EXACT-SPLICE equality,
    # not `c7 in c8` (which is always False: the splice makes c7 non-
    # contiguous in c8) and not the weaker block-wise fallback this test
    # originally used, which could silently pass even if the splice landed in
    # the wrong place or mangled surrounding text.
    assert c8 == c7.replace(
        "THE OBJECTIVE\n", _C8_PACE_BOTTOM_EDGE_AND_FAN_SECTION + "THE OBJECTIVE\n", 1
    ), "c8 must be EXACTLY c7 with the new section spliced before THE OBJECTIVE"
    lowered = c8.lower()
    # (1) Pace-comparison teaching, acting earlier than an edge crossing.
    assert "progress rates" in lowered
    assert "well before" in lowered
    # (2) Bottom-edge-is-not-a-finish-line teaching, the mirror of c7's fix.
    assert "window bottom" in lowered or "bottom of the acceptable" in lowered
    assert "not a finish line either" in lowered or "not a finish line" in lowered
    assert "no longer disqualif" in lowered  # "disqualifying"/"disqualified"
    assert "has not arrived" in lowered or "does not mean the roast has arrived" in lowered
    # Ceiling still forces the drop unconditionally — via an explicit NUMERIC
    # COMPARISON, not a restated "stays LAW" emphasis (the #562 bake-off
    # falsification: that phrasing pattern-matched into a hallucinated
    # "at the ceiling" on the roast-15 case, an 8 C real gap).
    #
    # PR #564 round 1 (Codex P2): asserting against `lowered` (the FULL
    # assembled c8 text) here is a test-scoping bug -- c8 INHERITS c7's
    # revised ceiling wording via the splice, so this exact phrase could
    # survive in the assembled text even if c8's OWN new section lost it
    # entirely (e.g. a future edit reverting c8's sentence back to a bare
    # "stays LAW" while c7's stayed fixed would still pass here, silently,
    # via c7's inherited copy). Assert against the SECTION CONSTANT directly
    # instead -- the same discipline the exact-splice equality test above
    # already uses.
    new_section_lowered = _C8_PACE_BOTTOM_EDGE_AND_FAN_SECTION.lower()
    assert "compute the gap" in new_section_lowered
    assert "never on a felt sense" in new_section_lowered
    # PR #564 round 2 (Codex P2 x3, all one root cause -- the anticipatory
    # clause referenced quantities the model cannot evaluate from context):
    # the reframe drops the lag reference ENTIRELY (round-1's "the time a
    # drop takes to act" invited an invented constant), teaches the gap in
    # the SAME per-minute units as bean_ror_c_per_min (no seconds, no unit
    # conversion to botch), and names bitter_ceiling_temp_c EXPLICITLY --
    # never emergency_drop_temp_c, the separate e-stop bound, not the
    # planning ceiling. "Never waiting for the gap to literally hit zero"
    # stays: the round-1 anticipatory lesson (gap==0 is too late) is
    # preserved, just re-expressed without an unevaluable lag term.
    assert "bean_ror_c_per_min" in _C8_PACE_BOTTOM_EDGE_AND_FAN_SECTION
    assert "bitter_ceiling_temp_c" in _C8_PACE_BOTTOM_EDGE_AND_FAN_SECTION
    assert "emergency_drop_temp_c" in _C8_PACE_BOTTOM_EDGE_AND_FAN_SECTION
    assert "small fraction" in new_section_lowered
    assert "per minute" in new_section_lowered or "per-minute" in new_section_lowered
    assert "never waiting for the gap to literally hit zero" in new_section_lowered
    # Finding 5 (round 2): no seconds/lag reference anywhere in the new
    # section -- the exact defect the reframe removes. A cheap, direct lock
    # against a future edit reintroducing an invented time constant.
    assert "second" not in new_section_lowered
    # (3) Fan->RoR coupling, consistent with D96 slice 1's shipped mechanism —
    # the compensation is bounded/conditional, NOT an assumed rescue, and is
    # explicitly suppressed near a drop (never promises a raise will land).
    assert "crash" in lowered and "rate of rise" in lowered
    assert "may compensate" in lowered
    assert "not guaranteed" in lowered
    assert "suppressed" in lowered
    assert "do not rely on an assumed heat rescue" in lowered
    # The indicated ceiling stays LAW throughout the new teaching too.
    assert "ceiling" in lowered
    # The new section names NO fixed roast-15 fixture numbers of its own
    # (told==enforced; the live context carries every threshold/target).
    start = c8.index("POST-FIRST-CRACK: COMPARE PROGRESS RATES")
    new_section = c8[start : c8.index("THE OBJECTIVE\n", start)]
    for literal in ("183", "188", "16.3", "115 s", "7 C", "30", "90", "8ac8a5e4"):
        assert literal not in new_section, f"c8's new section must not bake in {literal!r}"


def test_c9_is_exactly_c8_plus_the_reference_section() -> None:
    """c9 (#567) is c8 with the reference-roast section spliced in exactly
    before "THE OBJECTIVE" (the SAME splice advisor.py performs) — an
    EXACT-SPLICE equality, so all of c1..c8 is preserved byte-for-byte and the
    ONLY delta is the reference section. The reference teaching must not have
    leaked into c8 or the live default c3."""
    from roastpilot_agent.advisor import (
        _C9_REFERENCE_CURVE_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    c8 = control_teaching_prompt("c8")
    c9 = control_teaching_prompt("c9")
    assert c9 == c8.replace(
        "THE OBJECTIVE\n", _C9_REFERENCE_CURVE_SECTION + "THE OBJECTIVE\n", 1
    ), "c9 must be EXACTLY c8 with the reference section spliced before THE OBJECTIVE"
    assert "A REFERENCE ROAST" not in c8
    assert "A REFERENCE ROAST" not in control_teaching_prompt("c3")


def test_c9_reference_section_is_information_not_command_and_names_no_numbers() -> None:
    """#567 / design §3.3: the reference-roast section is DELIBERATELY minimal
    and non-imperative. It must (1) name the real reference_curve /
    reference_landmarks context fields as the operator's own best-rated past
    roast of the same bean; (2) teach that deviations are INFORMATION, not a
    command to steer back onto the reference; (3) subordinate the reference to
    the profile's own explicit targets (never overrides, stays authoritative);
    and (4) name NO new numbers — the #218 two-copies discipline, since the
    prompt-testing arc showed a baked constant in a prose rule misleads the
    model."""
    from roastpilot_agent.advisor import (
        _C9_REFERENCE_CURVE_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    section = _C9_REFERENCE_CURVE_SECTION
    lowered = section.lower()
    # (1) the real context fields, framed as the same-bean best-rated roast.
    assert "reference_curve" in section
    assert "reference_landmarks" in section
    assert "best-rated" in lowered and "same" in lowered
    # (2) information, not a command.
    assert "information" in lowered
    assert "not a command" in lowered
    assert "never as a command to steer back" in lowered
    # (3) subordinate to the profile targets + joint objective (never overrides).
    assert "target_drop_temp_c" in section
    assert "never overrides" in lowered
    assert "authoritative" in lowered
    # (4) no baked numbers — every threshold/target comes from context.
    assert not re.search(r"\d", section), "the reference section must name no hardcoded numbers"


def test_c9_registered_selectable_and_not_the_live_default() -> None:
    """c9 is a registered, selectable version wired the same as its siblings
    (``instructions_for`` resolves it to the same text), and adding it does not
    make it live — c3 stays the default (c9 is promoted only if the #567
    bake-off + a hardware roast validate it, the design's ship-disabled
    posture)."""
    assert instructions_for("c9") == control_teaching_prompt("c9")
    assert control_teaching_prompt("c9")  # non-empty
    assert CONTROL_TEACHING_PROMPT_VERSION == "c3"
    assert control_teaching_prompt("c9") != control_teaching_prompt(CONTROL_TEACHING_PROMPT_VERSION)


def test_c10_is_exactly_c9_plus_the_dtr_authority_section() -> None:
    """c10 (#705) is c9 with the read-the-provided-DTR section spliced in exactly
    before "THE OBJECTIVE" (the SAME splice advisor.py performs) — an
    EXACT-SPLICE equality, so all of c1..c9 is preserved byte-for-byte and the
    ONLY delta is the DTR-authority section. The teaching must not have leaked
    into c9 or the live default c3."""
    from roastpilot_agent.advisor import (
        _C10_DTR_AUTHORITY_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    c9 = control_teaching_prompt("c9")
    c10 = control_teaching_prompt("c10")
    assert c10 == c9.replace(
        "THE OBJECTIVE\n", _C10_DTR_AUTHORITY_SECTION + "THE OBJECTIVE\n", 1
    ), "c10 must be EXACTLY c9 with the DTR-authority section spliced before THE OBJECTIVE"
    assert "DO NOT RECOMPUTE" not in c9
    assert "DO NOT RECOMPUTE" not in control_teaching_prompt("c3")


def test_c10_dtr_section_teaches_use_the_field_not_recompute_and_names_no_numbers() -> None:
    """#705: the DTR-authority section must (1) name the real
    development_time_ratio context field as authoritative; (2) name the raw
    development_elapsed_seconds / roast_elapsed_seconds and teach NOT to divide
    them; (3) state the fraction-vs-percentage relationship so the model reads
    the field correctly; and (4) name NO new numbers (the #218 two-copies
    discipline) — every target still comes from context."""
    from roastpilot_agent.advisor import (
        _C10_DTR_AUTHORITY_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    section = _C10_DTR_AUTHORITY_SECTION
    lowered = section.lower()
    # (1) the authoritative context field.
    assert "development_time_ratio" in section
    assert "authoritative" in lowered
    # (2) the raw durations, taught as NOT to be divided by hand.
    assert "development_elapsed_seconds" in section
    assert "roast_elapsed_seconds" in section
    assert "do not" in lowered and "recompute" in lowered
    # (3) fraction vs percentage, so the field is read correctly.
    assert "fraction" in lowered and "percentage" in lowered
    # (4) no baked numbers — every threshold/target comes from context.
    assert not re.search(r"\d", section), "the DTR-authority section must name no hardcoded numbers"


def test_c10_registered_selectable_and_not_the_live_default() -> None:
    """c10 is a registered, selectable version wired the same as its siblings
    (``instructions_for`` resolves it to the same text), and adding it does not
    make it live — c3 stays the default (c10 is promoted only if the #705 part-2
    math-reliability bake-off validates it, routed through #396)."""
    assert instructions_for("c10") == control_teaching_prompt("c10")
    assert control_teaching_prompt("c10")  # non-empty
    assert CONTROL_TEACHING_PROMPT_VERSION == "c3"
    assert control_teaching_prompt("c10") != control_teaching_prompt(
        CONTROL_TEACHING_PROMPT_VERSION
    )


def test_c11_is_exactly_c10_plus_the_ambient_fan_section() -> None:
    """c11 (#709 / RP-B) is c10 with the ambient-fan section spliced in exactly
    before "THE OBJECTIVE" (the SAME splice advisor.py performs) — an
    EXACT-SPLICE equality, so all of c1..c10 is preserved byte-for-byte and the
    ONLY delta is the ambient-fan section. The doctrine must not have leaked
    into c10 or the live default c3."""
    from roastpilot_agent.advisor import (
        _C11_AMBIENT_FAN_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    c10 = control_teaching_prompt("c10")
    c11 = control_teaching_prompt("c11")
    assert c11 == c10.replace("THE OBJECTIVE\n", _C11_AMBIENT_FAN_SECTION + "THE OBJECTIVE\n", 1), (
        "c11 must be EXACTLY c10 with the ambient-fan section spliced before THE OBJECTIVE"
    )
    assert "MATCH FAN AGGRESSION TO THE ROOM" not in c10
    assert "MATCH FAN AGGRESSION TO THE ROOM" not in control_teaching_prompt("c3")


def test_c11_ambient_section_names_the_context_fields_and_no_numbers() -> None:
    """#709 / RP-B: the ambient-fan section must (1) name the real
    ambient_temp_c / ambient_humidity_pct context fields and the
    ambient_fan_threshold_c boundary it compares against; (2) carry BOTH
    regimes — aggressive airflow at/above the boundary, graduated steps below
    it; (3) name NO numbers, so the operator's ratified ~26 °C hypothesis
    re-fits from RP-D scores by changing config alone rather than by editing
    the prompt (the #218 two-copies discipline)."""
    from roastpilot_agent.advisor import (
        _C11_AMBIENT_FAN_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    section = _C11_AMBIENT_FAN_SECTION
    lowered = section.lower()
    # (1) the real context fields, including the boundary supplied as data.
    assert "ambient_temp_c" in section
    assert "ambient_humidity_pct" in section
    assert "ambient_fan_threshold_c" in section
    assert "ambient_fan_step_max_pp" in section
    # (2) both regimes are present and the boundary is an explicit comparison.
    assert "at or above" in lowered
    assert "below" in lowered
    assert "graduated steps" in lowered
    # (3) NO baked numbers — the boundary is data, never prose.
    assert not re.search(r"\d", section), "the ambient-fan section must name no hardcoded numbers"


def test_c11_qualifies_the_fan_brake_and_does_not_regress_high_ambient_fan() -> None:
    """#709 risk control (#498 must NOT regress). The doctrine is CONDITIONAL:
    it may never read as a blanket fan softening, because a soft fan in a
    genuinely hot room reproduces the smoke/scorching failure #498 fixed.

    So the section must (1) explicitly QUALIFY rather than cancel the
    fan-as-active-brake teaching spliced above it; (2) sanction an aggressive
    regime up to the context fan ceiling when at/above the boundary, and name
    the hot-room failure mode; (3) preserve every fan value up to the context
    ceiling below the boundary too — changing the PACE, not the destination;
    and (4) keep the c3 brake teaching itself byte-for-byte intact in the
    assembled c11 (the qualification is additive, not a rewrite)."""
    from roastpilot_agent.advisor import (
        _C3_FAN_BRAKE_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C11_AMBIENT_FAN_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    section = _C11_AMBIENT_FAN_SECTION
    lowered = section.lower()
    assembled = control_teaching_prompt("c11")
    # (1) the high-ambient regime stays aggressive, and the #498 failure it
    # exists to prevent is named rather than merely implied.
    assert "fan ceiling" in lowered
    assert "smoke" in lowered and "scorches" in lowered
    # (2) below the boundary only the pace changes within the context box.
    assert "up to the fan ceiling given in context" in lowered
    # (2b) the below-threshold bullet bounds the STEP by the context field
    # (D124's ~15 pp, as data) while explicitly leaving the destination open.
    # Without a bound, "graduated" permits a 30-to-85 jump and this release
    # ships no deterministic slew clamp to stop it.
    below = _bullet_starting_with(section, "- BELOW ambient_fan_threshold_c")
    assert "ambient_fan_step_max_pp" in below
    assert "bounds the STEP, not the destination" in below
    assert "including the ceiling itself" in below.lower()
    assert "ceiling is unchanged" not in below.lower()
    # (3) the c3 brake teaching survives VERBATIM and, critically, appears
    # BEFORE the qualifier — "the fan-brake rule above" only resolves if the
    # rule really is above it in the assembled text (the #499 splice-ordering
    # defect class: a later section silently overriding an earlier one).
    assert _C3_FAN_BRAKE_SECTION in assembled
    assert assembled.index(_C3_FAN_BRAKE_SECTION) < assembled.index(section)


def _bullet_starting_with(section: str, marker: str) -> str:
    """Return the single ``- `` bullet of ``section`` that starts with ``marker``.

    Prompt sections are one string of ``- ``-prefixed bullets. Asserting a
    clause lands in the RIGHT bullet (rather than anywhere in the section)
    matters whenever sibling bullets share vocabulary — c11's at/above and
    below branches both discuss stepping the fan, so a whole-section check can
    pass on the wrong bullet's text.

    Args:
        section: The prompt section text.
        marker: The bullet's opening substring, including its leading ``- ``.

    Returns:
        The bullet text, from ``marker`` up to the next bullet or section end.

    Raises:
        AssertionError: If no bullet starts with ``marker``.
    """
    start = section.find(marker)
    assert start != -1, f"no bullet starting {marker!r} in section"
    nxt = section.find("\n- ", start)
    return section[start:] if nxt == -1 else section[start:nxt]


def test_c11_never_rations_the_brake_when_fan_is_the_only_brake_left() -> None:
    """#709 safety review finding 1. In post-first-crack loop mode the
    deterministic loop owns heat and FAN IS THE ADVISOR'S LEVER, while the
    safety gate clamps fan only to [floor, ceiling] with NO slew limit — so
    this prose is the only thing shaping fan pace, and it shapes an actuated
    value.

    The graduated-steps preference must therefore carry an explicit PRECEDENCE
    carve-out for the state the c3 fan-brake rule exists to handle (heat at its
    floor, bean still climbing toward the ceiling), or the model is left to
    resolve the conflict itself — toward slower braking, in exactly the cool
    rooms this doctrine targets.

    Pinned three ways, none of which prose can satisfy by merely sounding
    urgent:

    (1) SLICE-SCOPED. The assertions run against the carve-out BULLET alone,
        not the whole section — the at/above bullet also talks about decisive
        stepping and would otherwise satisfy a loose whole-section check even
        if the carve-out were missing or malformed.
    (2) ANCHORED TO c3's OWN CONDITION. The carve-out must reuse c3's existing
        trigger language verbatim rather than inventing a fresh synonym for it.
        This is the #218 two-copies discipline applied to a CONDITION rather
        than a number: if a later edit rewords either copy of "heat at its
        floor and the bean still climbing toward the drop ceiling" without
        updating the other, the two rules silently stop describing the same
        state, and this test fails.
    (3) ORDERED. A precedence clause that precedes the rule it governs does not
        read as an override, so it must follow the graduated-steps bullet.

    Scope limit, stated honestly: this pins the AUTHORED ARTIFACT, not model
    behaviour at inference time. No unit test can show the model actually
    treats the clause as an override — that is what the offline decision-level
    bake-off and the hardware gate are for."""
    from roastpilot_agent.advisor import (
        _C3_FAN_BRAKE_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C11_AMBIENT_FAN_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    section = _C11_AMBIENT_FAN_SECTION
    lowered = section.lower()
    carve_out = _bullet_starting_with(section, "- THIS PACE GUIDANCE GOVERNS").lower()

    # (2) c3's own trigger phrases, required to appear in BOTH copies — so a
    # reword of either side breaks this rather than silently diverging.
    for anchor in ("climbing toward the drop ceiling", "the only brake left"):
        assert anchor in _C3_FAN_BRAKE_SECTION.lower(), (
            f"{anchor!r} is the c3 anchor this carve-out cross-references; "
            "if c3 was reworded, update the carve-out in the SAME commit"
        )
        assert anchor in carve_out
    # The carve-out is an override of the graduated default, and names it.
    assert "takes precedence" in carve_out
    assert "graduation" in carve_out
    # The emergency must escape the STEP BOUND as well as the pacing advice —
    # a bounded step is still a rationed brake.
    assert "without regard to ambient_fan_step_max_pp" in carve_out
    # It is unconditional — never itself re-gated on the room or on ambient
    # being readable at all (the finding-2 failure mode, applied here).
    assert "in any room" in carve_out
    # (3) and it must FOLLOW the bullet it overrides.
    assert lowered.index("takes precedence") > lowered.index("graduated steps")


def test_c11_does_not_soften_fan_doctrine_when_ambient_is_unavailable() -> None:
    """#709 safety review finding 2. The ambient probe is OPTIONAL and
    fail-soft — a disabled, unplugged, or mid-roast-dropped probe yields
    ``None``. Sensor availability must never flip fan doctrine in the unsafe
    direction, or every probe-less roast (including one in a genuinely hot
    room, where the smoke/scorching failure #498 fixed is the live risk) is
    quietly told to be gentler with the fan.

    Regression pin: the earlier draft ended the fallback with "and prefer the
    graduated approach when in doubt", which did exactly that."""
    from roastpilot_agent.advisor import (
        _C11_AMBIENT_FAN_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    lowered = _C11_AMBIENT_FAN_SECTION.lower()
    assert "prefer the graduated approach when in doubt" not in lowered
    # The fallback falls back to the UNQUALIFIED brake rule, and says so.
    assert "apply the fan-brake rule above exactly as written" in lowered
    assert "not treat a missing reading as a reason to be gentler" in lowered


def test_c11_keeps_the_fan_crash_mechanism_unconditional() -> None:
    """#709 safety review finding 8. The already-spliced c8 section teaches the
    fan-to-rate-of-rise crash mechanism UNCONDITIONALLY. c11 is newer and more
    specific, so if it attributes the crash to the room ("a fan slam in a cool
    room crashes the rate of rise") it implicitly NARROWS c8 to the
    below-threshold case — and the at/above branch would then sanction a
    decisive step with no watch-the-response discipline at all.

    Ten noisy corpus points establish only that a cool room makes the crash
    bite harder, NOT that the mechanism is ambient-gated, so the prompt must
    not imply it is."""
    from roastpilot_agent.advisor import (
        _C11_AMBIENT_FAN_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    section = _C11_AMBIENT_FAN_SECTION
    lowered = section.lower()
    # The crash is stated for ANY room; the room only changes how hard it bites.
    assert "crash the rate of rise in any room" in lowered
    # The at/above branch keeps the watch-the-response discipline too, so the
    # sanctioned decisive step is not an unwatched one.
    above = _bullet_starting_with(section, "- AT OR ABOVE ambient_fan_threshold_c").lower()
    assert "watch the rate of rise after the step" in above
    # Humidity is named with an explicit non-rule rather than left as context
    # the model must silently ignore (finding 5).
    assert "ambient_humidity_pct is background only" in lowered


def test_c11_registered_selectable_and_not_the_live_default() -> None:
    """c11 is a registered, selectable version wired the same as its siblings
    (``instructions_for`` resolves it to the same text), and adding it does not
    make it live — c3 stays the default. c11 is promoted only if the offline
    decision-level bake-off and a single-variable hardware roast scored by RP-D
    (#711) validate it, which is an operator-gated decision (#707/D122)."""
    assert instructions_for("c11") == control_teaching_prompt("c11")
    assert control_teaching_prompt("c11")  # non-empty
    assert CONTROL_TEACHING_PROMPT_VERSION == "c3"
    assert control_teaching_prompt("c11") != control_teaching_prompt(
        CONTROL_TEACHING_PROMPT_VERSION
    )


# --- c12 (#710 RP-C: the deterministic joint-window drop planner, T47-T55) --


def test_c12_chain_integrity_c1_through_c12_byte_identical_to_their_splices() -> None:
    """T47 (AC7, G21): each of c2..c12 equals ITS OWN documented splice from
    the version directly below it — reconstructed here independently of the
    module's own assembly order, so a splice performed from the wrong
    parent, on the wrong anchor, or more than once would be caught even if
    the module's internal wiring happened to still produce the right STRING
    by accident. Also pins that c1..c11 stay byte-for-byte unchanged by the
    c12 addition (AC7 rule 9)."""
    from roastpilot_agent.advisor import (
        _C2_DEVELOPMENT_STRETCH_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C3_FAN_BRAKE_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C4_DROP_DECISIVENESS_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C5_HEAT_FLOOR_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C6_HEAT_RECOVERY_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C7_DTR_PACE_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C8_PACE_BOTTOM_EDGE_AND_FAN_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C9_REFERENCE_CURVE_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C10_DTR_AUTHORITY_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C11_AMBIENT_FAN_SECTION,  # pyright: ignore[reportPrivateUsage]
        _C12_JOINT_WINDOW_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    c1 = control_teaching_prompt("c1")
    assert c1  # non-empty base

    chain = [
        ("c2", _C2_DEVELOPMENT_STRETCH_SECTION),
        ("c3", _C3_FAN_BRAKE_SECTION),
        ("c4", _C4_DROP_DECISIVENESS_SECTION),
        ("c5", _C5_HEAT_FLOOR_SECTION),
        ("c6", _C6_HEAT_RECOVERY_SECTION),
        ("c7", _C7_DTR_PACE_SECTION),
        ("c8", _C8_PACE_BOTTOM_EDGE_AND_FAN_SECTION),
        ("c9", _C9_REFERENCE_CURVE_SECTION),
        ("c10", _C10_DTR_AUTHORITY_SECTION),
        ("c11", _C11_AMBIENT_FAN_SECTION),
        ("c12", _C12_JOINT_WINDOW_SECTION),
    ]
    previous = c1
    for version, section in chain:
        expected = previous.replace("THE OBJECTIVE\n", section + "THE OBJECTIVE\n", 1)
        actual = control_teaching_prompt(version)
        assert actual == expected, f"{version} must be EXACTLY its parent plus its own section"
        previous = actual


def test_c12_section_absent_from_earlier_versions() -> None:
    """T48 (G21): the c12 section must not have leaked into any
    already-shipped earlier version, including the live default c3."""
    from roastpilot_agent.advisor import (
        _C12_JOINT_WINDOW_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    assert _C12_JOINT_WINDOW_SECTION not in control_teaching_prompt("c11")
    assert _C12_JOINT_WINDOW_SECTION not in control_teaching_prompt("c10")
    assert _C12_JOINT_WINDOW_SECTION not in control_teaching_prompt("c3")


def test_c12_section_names_no_numbers() -> None:
    """T49 (G20): the margin and horizon are DATA
    (``JointWindowPlanner.temp_margin_c`` / ``closing_horizon_seconds``),
    never prompt literals — the #218 two-copies discipline."""
    from roastpilot_agent.advisor import (
        _C12_JOINT_WINDOW_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    assert not re.search(r"\d", _C12_JOINT_WINDOW_SECTION), (
        "the joint-window section must name no hardcoded numbers"
    )


def test_c12_section_names_all_statuses_and_all_context_keys() -> None:
    """T50 (AC7): the section names all four wire-form status labels
    (``models.JointWindowStatus``'s serialised values) and all six
    ``joint_window_*`` context keys verbatim."""
    from roastpilot_agent.advisor import (
        _C12_JOINT_WINDOW_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    section = _C12_JOINT_WINDOW_SECTION
    for status_label in ("ahead", "on_track", "temp_short", "closing"):
        assert status_label in section
    for key in (
        "joint_window_status",
        "joint_window_effective_target_temp_c",
        "joint_window_open_runway_seconds",
        "joint_window_close_runway_seconds",
        "joint_window_projected_temp_at_open_c",
        "joint_window_projected_temp_at_close_c",
    ):
        assert key in section


def test_c12_section_states_temperature_first_and_the_ceiling_bound_together() -> None:
    """T51 (AC1, D176/D177): the temperature-first instruction and the
    ceiling-bound statement occur WITHIN the c12 section itself — not merely
    inherited from an earlier splice — so the SAME teaching both licenses
    the hold and bounds it."""
    from roastpilot_agent.advisor import (
        _C12_JOINT_WINDOW_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    section = _C12_JOINT_WINDOW_SECTION
    temp_short_bullet = _bullet_starting_with(section, "- temp_short means").lower()
    assert "keep steering toward" in temp_short_bullet
    assert "accept the development ratio running" in temp_short_bullet
    # The ceiling bound is stated in the SAME section (the very next bullet),
    # never merely assumed from an earlier splice.
    ceiling_bullet = _bullet_starting_with(
        section, "- joint_window_effective_target_temp_c is already"
    ).lower()
    assert "bitter ceiling" in ceiling_bullet
    assert "controller-owned" in ceiling_bullet
    assert "deterministic guard" in ceiling_bullet


def test_c12_section_has_an_absent_status_fallback_that_never_licenses_holding() -> None:
    """T52 (AC7 rule 6): an absent-``joint_window_status`` fallback exists and
    directs the model to apply the existing objective/DTR teaching exactly
    as written — never to infer a status from another value, and never to
    treat the absence as licence to hold indefinitely."""
    from roastpilot_agent.advisor import (
        _C12_JOINT_WINDOW_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    section = _C12_JOINT_WINDOW_SECTION
    fallback = _bullet_starting_with(section, "- If joint_window_status is absent").lower()
    assert "apply the objective and development-ratio" in fallback
    assert "exactly as written" in fallback
    assert "do not infer a status" in fallback
    assert "licence to hold indefinitely" in fallback


_SUPERSEDED_TEMP_SHORT_PHRASES = (
    "a temperature-short drop is preferable",
    "temperature-short drop is preferable to",
    "dropping temperature-short beats over-developing",
    "prefer to drop temperature-short",
    "better to drop temperature-short",
    "drop temperature-short rather than over-develop",
)


def test_c12_never_restores_the_superseded_drop_temp_short_framing() -> None:
    """T53 (AC1): the superseded framing D176 replaces — that dropping
    temperature-short beats over-developing — is absent from both the new
    section and the whole assembled c12 prompt (never a close-paraphrase
    reintroduction; only the ratified opposite appears)."""
    from roastpilot_agent.advisor import (
        _C12_JOINT_WINDOW_SECTION,  # pyright: ignore[reportPrivateUsage]
    )

    section_normalized = " ".join(_C12_JOINT_WINDOW_SECTION.lower().split())
    c12_normalized = " ".join(control_teaching_prompt("c12").lower().split())
    for phrase in _SUPERSEDED_TEMP_SHORT_PHRASES:
        assert phrase not in section_normalized
        assert phrase not in c12_normalized
    # The ratified OPPOSITE is what actually appears.
    assert "is never preferable to" in section_normalized


def test_c12_does_not_move_the_live_default() -> None:
    """T54 (G-default): adding c12 must never move the live default."""
    assert CONTROL_TEACHING_PROMPT_VERSION == "c3"
    assert AdvisorConfig().prompt_version == "c3"


def test_c12_registered_selectable_and_not_the_live_default() -> None:
    """T55 (AC7/AC3): c12 is a registered, selectable version wired the same
    as its siblings (``instructions_for`` resolves it to the same text, and
    the unknown-version ``ValueError`` is unaffected), and adding it does
    not make it live — c3 stays the default. c12 is promoted only if the
    D176 decision-level acceptance (both halves) and #707 supervised
    hardware validation or a validated #580 simulator validate it —
    operator-gated."""
    assert instructions_for("c12") == control_teaching_prompt("c12")
    assert control_teaching_prompt("c12")  # non-empty
    assert CONTROL_TEACHING_PROMPT_VERSION == "c3"
    assert control_teaching_prompt("c12") != control_teaching_prompt(
        CONTROL_TEACHING_PROMPT_VERSION
    )
    with pytest.raises(ValueError):
        instructions_for("does-not-exist")


# --- Codex P2 follow-up (#499): assert on the FINAL ASSEMBLED prompt, not
# just the c1 fragment. The splice chain (c1 -> c2 -> c3 -> c4 -> c5 -> c6)
# means a section added to c1 can be directly contradicted by a LATER-spliced
# section from c2+ in the fully assembled text the live model actually
# receives — a fragment-level test (asserting only against control_teaching_
# prompt("c1")) is structurally blind to that: c1 alone never contained the
# contradicting text in the first place. These tests assert on the ASSEMBLED
# c3 (the live default) and c6 (the newest/most-spliced version) so a future
# section addition that reintroduces a first-past-the-post phrase downstream
# of #499's joint-objective section is caught where it actually matters —
# see docs/recent-fixes.md for the general anti-pattern this class guards.
# c7 (#499 part 2) is included in both parametrizations below: it is spliced
# AFTER the joint-objective section (the newest/most-spliced version, mirroring
# how c6 was added to this same list), and it must not reintroduce the
# first-past-the-post phrasing Codex originally found either. c8 (D96 slice 2,
# #559) is now the newest/most-spliced version and is added the same way.


@pytest.mark.parametrize("version", ["c3", "c6", "c7", "c8", "c9", "c10", "c11", "c12"])
def test_assembled_prompt_carries_the_joint_objective_and_no_contradiction(
    version: str,
) -> None:
    """The FULLY ASSEMBLED prompt (what the live model receives) must contain
    #499's joint-objective section and must NOT contain any first-past-the-
    post phrasing spliced in by a later (c2+) section — the exact defect
    Codex found: c2's original "NEVER overshoot the drop target" / "LATEST
    acceptable drop" wording, spliced AFTER #499's section in assembly order,
    directly contradicted the joint objective's "modest overshoot... is
    preferred" teaching."""
    prompt = control_teaching_prompt(version)
    lowered = prompt.lower()
    assert "joint objective" in lowered
    assert "modest overshoot" in lowered
    # The specific contradiction Codex found must be gone everywhere in the
    # assembled text, not just absent from the c1 fragment.
    assert "never overshoot the drop target" not in lowered
    assert "latest acceptable drop" not in lowered


@pytest.mark.parametrize("version", ["c3", "c6", "c7", "c8", "c9", "c10", "c11", "c12"])
def test_assembled_prompt_joint_objective_precedes_every_later_section(
    version: str,
) -> None:
    """The joint-objective section must appear BEFORE every later-spliced
    section (c2+) in the assembled text — the ordering Codex's finding
    depended on (a later section reads as a refinement/override of an
    earlier one). Pins the STRUCTURE, not just the absence of one known bad
    phrase, so a differently-worded future contradiction is more likely to be
    caught by a careful reviewer reading the assembled text in order."""
    prompt = control_teaching_prompt(version)
    joint_index = prompt.index("THE DROP - A JOINT OBJECTIVE")
    for marker in (
        "POST-FIRST-CRACK: STRETCH DEVELOPMENT",  # c2
        "POST-FIRST-CRACK: FAN IS AN ACTIVE BRAKE",  # c3
        "POST-FIRST-CRACK: A LARGE DTR OVERSHOOT",  # c7
        "POST-FIRST-CRACK: COMPARE PROGRESS RATES",  # c8
        "POST-FIRST-CRACK: A REFERENCE ROAST",  # c9
        "POST-FIRST-CRACK: READ THE PROVIDED DEVELOPMENT RATIO",  # c10
        "POST-FIRST-CRACK: MATCH FAN AGGRESSION TO THE ROOM",  # c11
        "POST-FIRST-CRACK: THE JOINT-WINDOW READ",  # c12
    ):
        if marker in prompt:
            assert prompt.index(marker) > joint_index, (
                f"{marker!r} must be spliced AFTER the joint-objective section in {version}"
            )


def test_c1_grounds_development_numbers_to_context_no_invention() -> None:
    """#312: the c1 frame must instruct the model to use the development numbers
    FROM CONTEXT verbatim, never to estimate/invent them, and to STATE the dev%
    value it used in its rationale — the anti-hallucination fix after the first
    supervised roast (the model fabricated "14 %" at a true ~5.4 % to justify an
    irreversible early drop). A prompt-content assertion: this is the load-bearing
    grounding language the fix turns on.
    """
    prompt = control_teaching_prompt("c1")
    lowered = prompt.lower()
    # Use the context values verbatim — do NOT estimate / infer / invent.
    assert "verbatim" in lowered
    assert "do not estimate" in lowered
    assert "never invent" in lowered or "do not invent" in lowered
    # Must STATE the development percent / DTR value it used in the rationale.
    assert "must state the development percent" in lowered
    # Must not anchor the dev% to the target just because it "should" be near it.
    assert "anchor the development percent to the target" in lowered
    # The irreversible-drop framing is present so a fabricated number cannot drive
    # the drop.
    assert "irreversible" in lowered


def test_c1_pins_the_confidence_scale() -> None:
    """Issue #562 (the #499/#559 combined bake-off): both malformed decisions
    the bake-off surfaced were confidence-scale confusion (7.0, 85.0 returned
    on a 0-1 field). c1's own confidence mention (the only place in the
    control-teaching frame that names it) named no scale — pin it "between 0
    and 1" so the model is told the scale explicitly, inherited unchanged by
    every later c-version (c2 through c8)."""
    prompt = control_teaching_prompt("c1")
    lowered = prompt.lower()
    assert "confidence between 0 and 1" in lowered


def test_default_prompt_version_matches_control_teaching_version() -> None:
    """The config default and advisor.CONTROL_TEACHING_PROMPT_VERSION never drift.

    config.py uses the literal "c1" to avoid an advisor->config import cycle; this
    pins it equal to the canonical constant so a future control-version bump
    (c2, ...) cannot silently leave the live default behind.
    """
    assert AdvisorConfig().prompt_version == CONTROL_TEACHING_PROMPT_VERSION


def test_advisor_config_selects_c7_and_sends_its_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The D69 prompt-version discipline, applied to c7: selecting
    ``prompt_version="c7"`` on :class:`AdvisorConfig` (the same config field the
    Config UI's advisor.prompt_version selector writes, #499 part 2) actually
    wires c7's instructions into the constructed ``PydanticAIAdvisor`` — not
    just that ``control_teaching_prompt("c7")`` is byte-assembled correctly,
    but that the CONFIG-SELECTABLE path reaches it. c3 stays the untouched
    default (asserted separately above); this is additive, opt-in only.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    advisor = PydanticAIAdvisor(AdvisorConfig(prompt_version="c7"))
    instructions = advisor._instructions  # type: ignore[reportPrivateUsage]
    assert instructions == control_teaching_prompt("c7")
    assert instructions != control_teaching_prompt("c3")
    assert advisor.descriptor.prompt_version == "c7"


def test_advisor_config_selects_c8_and_sends_its_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The D69 prompt-version discipline, applied to c8 (D96 slice 2, #559):
    selecting ``prompt_version="c8"`` on :class:`AdvisorConfig` (the same
    config field the Config UI's advisor.prompt_version selector writes)
    actually wires c8's instructions into the constructed
    ``PydanticAIAdvisor`` — the CONFIG-SELECTABLE path reaches it, not just
    that ``control_teaching_prompt("c8")`` is byte-assembled correctly. c3
    stays the untouched default; this is additive, opt-in only."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    advisor = PydanticAIAdvisor(AdvisorConfig(prompt_version="c8"))
    instructions = advisor._instructions  # type: ignore[reportPrivateUsage]
    assert instructions == control_teaching_prompt("c8")
    assert instructions != control_teaching_prompt("c3")
    assert advisor.descriptor.prompt_version == "c8"


def test_live_post_fc_advisor_uses_the_c3_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production PydanticAIAdvisor sends c3 as its system instructions.

    The live post-FC control loop's advisor is constructed with the #274 control
    teaching frame (now c3, the roast-3 fan-as-active-brake tuning on top of c2's
    development-stretch) as the agent's system ``instructions``; the per-tick #275
    context is the user message (sent via ``agent.run(context_json)``, asserted
    elsewhere). Default config = gpt-4o + c3 (the model is unchanged — this was a
    prompt-only change).
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    advisor = PydanticAIAdvisor(AdvisorConfig())
    instructions = advisor._instructions  # type: ignore[reportPrivateUsage]
    assert instructions == control_teaching_prompt("c3")
    # The pinned post-FC (DEVELOPMENT) model is gpt-4o (unchanged by the c3 prompt).
    assert advisor._config.model_for(RoastPhase.DEVELOPMENT) == "openai/gpt-4o"  # type: ignore[reportPrivateUsage]


def test_live_advisor_passes_no_mcp_write_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advisory-only invariant survives the c1 + gpt-4o pin (#277).

    The agent the controller's post-FC loop runs is built with only a structured
    ``output_type`` and the c1 instructions — never any roaster-write tool. The
    advisor returns typed ``RoastDecision`` data; the controller owns the loop and
    every write still passes safety policy.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    advisor = PydanticAIAdvisor(AdvisorConfig())
    agent = advisor._agent_for("openai/gpt-4o")  # type: ignore[reportPrivateUsage]
    # No tools registered on the agent's toolset (advisory-only): the agent is
    # purely a structured-output recommender.
    toolset = agent._function_toolset  # type: ignore[reportPrivateUsage]
    assert not toolset.tools, "advisor agent must carry NO tools (advisory-only)"


def test_drop_recall_variants_keep_heat_fan_and_invariants() -> None:
    """v4-v8 (#194) target the gemini drop-recall gap. Each must KEEP v2's
    heat/fan control guidance (anticipatory thermal-lag cut + fan as a
    convective heat-transfer-mode lever — the 0.88 heat-direction must not
    regress) and the advisory-only / Celsius invariants, and must reason from
    the profile/live context rather than hardcoded textbook temperatures.
    Each must also be distinct from v2 and the others."""
    variants = {v: instructions_for(v) for v in ("v4", "v5", "v6", "v7", "v8")}
    texts = list(variants.values())
    # All distinct from one another and from v2 (five real strategies, not one).
    assert len({*texts, instructions_for("v2")}) == 6
    for version, prompt in variants.items():
        lower = prompt.lower()
        # Carried-forward heat/fan control guidance.
        assert "thermal lag" in lower, version
        assert "convective" in lower, version
        # Carried-forward invariants.
        assert "never control hardware" in lower, version
        assert "celsius" in lower, version
        # Reason from the profile/live context, not textbook numbers, and honor
        # the ~196 C indicated bitter ceiling (the irreversible-error guard).
        assert "indicated" in lower, version
        assert "196" in prompt, version
        assert "target_drop_temp_c" in prompt or "target_development_percent" in prompt, version


def test_drop_recall_variants_encode_distinct_strategies() -> None:
    """The five drop-recall variants are DISTINCT strategies, not rewordings:
    v4 anchors the profile drop target, v5 the profile development target, v6 the
    full two-sided window + flick guard, v7 is FC-detector-lag-aware, v8 is the
    concise rule-forward synthesis."""
    assert "target_drop_temp_c" in instructions_for("v4")
    assert "target_development_percent" in instructions_for("v5")
    v6 = instructions_for("v6").lower()
    assert "floor" in v6 and "ceiling" in v6 and "flick guard" in v6
    assert "12-21 s" in instructions_for("v7") and "lower bound" in instructions_for("v7").lower()
    assert "drop rule" in instructions_for("v8").lower()


# --- control teaching system prompt (#274 / D39.1) ---


def test_control_teaching_prompt_exists_and_is_stable() -> None:
    """The control teaching prompt is an importable, non-empty, versioned
    artifact, and the default-version call returns the active version's text.

    It is a SEPARATE artifact from the per-tick advisory prompts: it is NOT one
    of the ``v``-namespaced ``instructions_for`` versions, and it is not the
    drop-narrow ``v4`` drop lens. Two calls return the identical (stable) text."""
    assert CONTROL_TEACHING_PROMPT_VERSION == "c3"
    prompt = control_teaching_prompt()
    assert prompt
    # Operator decision 4: the FULL teaching detail is retained (the system
    # message caches, so token cost is negligible). The current text is ~5.4k
    # chars; a > 4000 floor catches a major trim while leaving room to edit. The
    # substantive content tests below do the real "what is taught" guarding.
    assert len(prompt) > 4000
    # Default arg resolves to the active version, and it is stable across calls.
    assert prompt == control_teaching_prompt(CONTROL_TEACHING_PROMPT_VERSION)
    assert control_teaching_prompt() == control_teaching_prompt()
    # Distinct from the per-tick advisory lenses (including the v4 drop lens).
    assert prompt != instructions_for("v4")


def test_control_teaching_prompt_unknown_version_raises() -> None:
    """An unknown control teaching version raises, like ``instructions_for``."""
    with pytest.raises(ValueError, match="control teaching prompt version"):
        control_teaching_prompt("does-not-exist")


def test_control_teaching_prompt_teaches_the_machine_and_phases() -> None:
    """The prompt carries the load-bearing teaching content: the Hottop machine
    (electric drum, thermal lag, fan as the primary airflow/cooling lever), the
    metrics (bean/env temp, RoR, DTR, dev-time, turning point, FC-ETA), and the
    full phase model (drying -> browning -> Maillard -> first crack ->
    development -> drop) with what each phase needs."""
    lower = control_teaching_prompt().lower()
    # The machine.
    assert "hottop" in lower
    assert "electric" in lower
    assert "thermal lag" in lower
    # Fan as the PRIMARY airflow/cooling lever, not just a coolant.
    assert "convective" in lower
    assert "airflow" in lower
    assert "cooling" in lower
    # The metrics (issue goal): bean/env temp, RoR, DTR, dev-time, turning
    # point, FC-ETA — the readings the model reasons over.
    assert "bean temperature" in lower
    assert "environment temperature" in lower
    assert "rate of rise" in lower or "ror" in lower
    assert "development time ratio" in lower or "dtr" in lower
    assert "development time" in lower
    assert "turning point" in lower
    assert "first-crack eta" in lower
    # The full phase model.
    for phase_word in ("drying", "browning", "maillard", "first crack", "development", "drop"):
        assert phase_word in lower, phase_word


def test_control_teaching_prompt_states_lever_units_as_percent_duty() -> None:
    """Lever-unit fix (the 16 Jun gpt-5-mini '70-80 degrees C' slip): heat/fan
    are 0-100 percent duty, explicitly NOT temperatures/setpoints."""
    prompt = control_teaching_prompt()
    lower = prompt.lower()
    assert "0-100" in prompt
    assert "percent duty" in lower
    assert "not temperatures" in lower


def test_control_teaching_prompt_carries_lever_stability_content() -> None:
    """Lever stability (the soft half of the #218 fix, folded into #274): fan is
    a COARSE lever set deliberately at regime transitions and held steady; bias
    toward fewer, larger, intentional moves over per-consult twiddling."""
    lower = control_teaching_prompt().lower()
    assert "coarse" in lower
    assert "hold" in lower and "steady" in lower
    assert "fewer, larger, intentional" in lower
    # The named anti-thrash patterns from the issue (staircase / thrash).
    assert "staircase" in lower or "thrash" in lower
    # Do not reverse a lever direction tick-to-tick without a real change.
    assert "tick-to-tick" in lower
    assert "oscillation" in lower


def test_control_teaching_prompt_is_principle_not_numbers() -> None:
    """Operator decision 1/3: numbers live in #273's policy (the live context
    limits), NOT in the prompt. The prompt teaches the model to reason inside the
    per-phase limits from the context and names no hardcoded thresholds.

    Guards against re-creating the #218 two-copies incoherence: it must NOT carry
    the pre-FC default numbers (heat 100 / fan 30) and must NOT carry the v4 drop
    anchor's '196' bitter-ceiling number (decision 2)."""
    prompt = control_teaching_prompt()
    lower = prompt.lower()
    # Teaches reasoning inside the context-provided limits.
    assert "limits" in lower
    assert "floor" in lower and "ceiling" in lower
    assert "context" in lower
    # No hardcoded control numbers (principle, not numbers).
    assert "196" not in prompt  # the v4 drop anchor's bitter ceiling is NOT folded in
    assert "heat 100" not in lower  # the #273 pre-FC default is NOT named here
    assert "fan 30" not in lower
    # Decision 3: no explicit fan-ceiling NUMBER near FC — rely on #273's
    # per-phase fan ceiling from the context. The abstract "fan floor/ceiling"
    # limits reference is fine; a literal "fan <number>" duty value is not. The
    # only digit-bearing 'heat 70' is the percent-duty *unit* illustration, so
    # match the lever followed by a number, excluding the 0-100 range itself.
    assert re.search(r"\bfan\s+\d+\b", lower) is None
    assert re.search(r"\bheat\s+(?!70\b)\d+\b", lower) is None


def test_control_teaching_prompt_encodes_pre_fc_discipline() -> None:
    """Binding acceptance from the 16 Jun negative cases: the prompt must make
    *acting* pre-first-crack WRONG (drive to FC, never stall, do not cut heat to
    prevent overshoot, do not open the fan into the crack), not merely name the
    phase."""
    lower = control_teaching_prompt().lower()
    # Drive to the crack; do not stall/delay it.
    assert "stall" in lower
    assert "never" in lower
    # The two banned pre-FC moves named.
    assert "do not cut heat" in lower
    assert "do not raise the fan" in lower
    # Hold is the pre-FC default; hold if unsure.
    assert "hold" in lower


# --- healthcheck reachability probe (issue #168) ---


@pytest.mark.asyncio
async def test_fake_advisor_healthcheck_defaults_to_reachable() -> None:
    """A bare FakeAdvisor probes REACHABLE deterministically — no key needed."""
    advisor = FakeAdvisor()
    health = await advisor.healthcheck()
    assert health.status is AdvisorHealthStatus.REACHABLE
    assert health.provider == "fake"
    assert health.model_slug == "fake-model"


@pytest.mark.asyncio
async def test_fake_advisor_healthcheck_returns_configured_health() -> None:
    """A configured AdvisorHealth is returned as-is (scriptable UNREACHABLE)."""
    scripted = AdvisorHealth(
        status=AdvisorHealthStatus.UNREACHABLE,
        provider="openai_compatible",
        model_slug="anthropic/claude-opus-4.8",
        error="401 Unauthorized",
    )
    advisor = FakeAdvisor(health=scripted)
    assert await advisor.healthcheck() == scripted


@pytest.mark.asyncio
async def test_fake_advisor_healthcheck_raises_configured_exception() -> None:
    """A configured BaseException is raised so the probe wrapper can be tested."""
    advisor = FakeAdvisor(health=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await advisor.healthcheck()


@pytest.mark.asyncio
async def test_pydanticai_healthcheck_reachable_on_round_trip() -> None:
    """A provider that answers the probe (even with a valid decision) is
    REACHABLE, carrying the configured provider + model."""
    advisor = _advisor_with(
        _function_model_returning(_VALID_OUTPUT),
        model_slug="some-model",
    )
    health = await advisor.healthcheck()
    assert health.status is AdvisorHealthStatus.REACHABLE
    assert health.model_slug == "some-model"
    assert health.error is None


@pytest.mark.asyncio
async def test_pydanticai_healthcheck_malformed_output_is_still_reachable() -> None:
    """A malformed *output* means the round trip works — the provider answered,
    so the advisor is reachable (auth/model/endpoint are fine)."""
    advisor = _advisor_with(_function_model_text("not a tool call"))
    health = await advisor.healthcheck()
    assert health.status is AdvisorHealthStatus.REACHABLE
    assert health.error is None


@pytest.mark.asyncio
async def test_pydanticai_healthcheck_transport_failure_is_unreachable() -> None:
    """A transport/auth failure (the #134 expired-key 401/503) is UNREACHABLE
    and carries the provider error — the probe itself never raises."""

    def boom(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=401, model_name="x", body="invalid api key")

    advisor = _advisor_with(FunctionModel(boom))
    health = await advisor.healthcheck()
    assert health.status is AdvisorHealthStatus.UNREACHABLE
    assert health.error is not None
    assert "401" in health.error


@pytest.mark.asyncio
async def test_pydanticai_healthcheck_times_out_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung provider trips the bounded probe timeout → UNREACHABLE, never a
    wedge. The timeout is driven low so the test is fast."""
    import asyncio as _asyncio

    async def _never(*_args: object, **_kwargs: object) -> object:
        await _asyncio.Event().wait()
        raise AssertionError("unreachable")

    advisor = _advisor_with(
        _function_model_returning(_VALID_OUTPUT),
        healthcheck_timeout_seconds=0.05,
    )
    # Make the agent run hang so only the timeout can resolve the probe. The
    # probe uses the base-slug agent, eagerly built in __init__ and cached.
    base_agent = advisor._agent_for(  # pyright: ignore[reportPrivateUsage]
        advisor._config.model_slug  # pyright: ignore[reportPrivateUsage]
    )
    monkeypatch.setattr(base_agent, "run", _never)
    health = await _asyncio.wait_for(advisor.healthcheck(), timeout=1.0)
    assert health.status is AdvisorHealthStatus.UNREACHABLE
    assert health.error is not None
    assert "timed out" in health.error


# --- Reasoning capture (#284) ---


def _function_model_with_thinking(args: dict[str, Any], thinking: str) -> FunctionModel:
    """A double that emits a ``ThinkingPart`` then calls its output tool.

    Mirrors a reasoning model: the response carries the reasoning trace as a
    thinking part alongside the structured output tool call.
    """

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool_name = info.output_tools[0].name
        return ModelResponse(parts=[ThinkingPart(content=thinking), ToolCallPart(tool_name, args)])

    return FunctionModel(respond)


def test_reasoning_from_run_returns_none_without_thinking_parts() -> None:
    """A run with no thinking parts yields ``None`` (non-reasoning model)."""

    class _Result:
        def all_messages(self) -> list[ModelResponse]:
            return [ModelResponse(parts=[TextPart("hello")])]

    assert reasoning_from_run(_Result()) is None


def test_reasoning_from_run_extracts_and_joins_thinking_parts() -> None:
    """Thinking-part contents across the run are concatenated into the trace.

    An empty thinking part is skipped (the ``if content`` guard), so only the
    non-empty parts join into the trace.
    """

    class _Result:
        def all_messages(self) -> list[ModelResponse]:
            return [
                ModelResponse(parts=[ThinkingPart(content="step one")]),
                # An empty thinking part is skipped — exercises the falsy-content
                # branch so it cannot smuggle a blank fragment into the trace.
                ModelResponse(parts=[ThinkingPart(content="")]),
                ModelResponse(parts=[ThinkingPart(content="step two"), TextPart("answer")]),
            ]

    assert reasoning_from_run(_Result()) == "step one\n\nstep two"


def test_reasoning_from_run_never_raises_on_bad_shape() -> None:
    """An unrecognised run shape degrades to ``None`` rather than erroring."""
    assert reasoning_from_run(object()) is None


def test_reasoning_from_run_swallows_all_messages_error() -> None:
    """An ``all_messages()`` that raises degrades to ``None`` (best-effort)."""

    class _Result:
        def all_messages(self) -> list[ModelResponse]:
            raise RuntimeError("library shape changed")

    assert reasoning_from_run(_Result()) is None


@pytest.mark.asyncio
async def test_get_recommendation_with_reasoning_captures_thinking() -> None:
    """The reasoning-aware method returns the decision AND the thinking trace."""
    advisor = _advisor_with(
        _function_model_with_thinking(_VALID_OUTPUT, "bean temp climbing; hold heat")
    )
    decision, reasoning = await advisor.get_recommendation_with_reasoning(
        _context_in_phase(RoastPhase.DEVELOPMENT)
    )
    assert decision.target_heat == _VALID_OUTPUT["target_heat"]
    assert reasoning is not None
    assert "hold heat" in reasoning


@pytest.mark.asyncio
async def test_get_recommendation_with_reasoning_none_when_absent() -> None:
    """A non-reasoning model returns the decision with ``None`` reasoning."""
    advisor = _advisor_with(_function_model_returning(_VALID_OUTPUT))
    decision, reasoning = await advisor.get_recommendation_with_reasoning(
        _context_in_phase(RoastPhase.DEVELOPMENT)
    )
    assert decision.should_drop == _VALID_OUTPUT["should_drop"]
    assert reasoning is None


@pytest.mark.asyncio
async def test_get_recommendation_with_reasoning_maps_failures() -> None:
    """The reasoning-aware method shares the typed-error mapping (malformed /
    unsafe / provider) with the plain method."""
    malformed = _advisor_with(_function_model_text("no tool here"))
    with pytest.raises(AdvisorMalformedOutputError):
        await malformed.get_recommendation_with_reasoning(_context_in_phase(RoastPhase.DEVELOPMENT))

    unsafe = _advisor_with(_function_model_returning({**_VALID_OUTPUT, "target_heat": 150}))
    with pytest.raises(AdvisorUnsafeOutputError):
        await unsafe.get_recommendation_with_reasoning(_context_in_phase(RoastPhase.DEVELOPMENT))

    def boom(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="x", body="upstream down")

    provider = _advisor_with(FunctionModel(boom))
    with pytest.raises(AdvisorProviderError):
        await provider.get_recommendation_with_reasoning(_context_in_phase(RoastPhase.DEVELOPMENT))
