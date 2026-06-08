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
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel

from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorFailureMode,
    AdvisorMalformedOutputError,
    AdvisorProviderError,
    AdvisorUnsafeOutputError,
    FakeAdvisor,
    PydanticAIAdvisor,
    RoastAdvisor,
    RoastDecision,
    build_model,
    instructions_for,
    reasoning_extra_body,
    usage_from_run,
)
from roastpilot_agent.config import AdvisorConfig, ControllerConfig
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
