"""Advisory layer (component plan §4; orchestration plan § PydanticAI
Advisory Layer).

The advisor never receives MCP write tools. It receives structured context
and returns typed data only; safety policy validates, clamps, or rejects
every recommendation before any hardware write.

Failure vocabulary (plan §4 failure handling): an advisor call ends in one
of five outcomes — valid, malformed, unsafe, timeout, or provider error.
At this boundary *malformed* means the provider output could not be parsed
into the ``RoastDecision`` shape, and *unsafe* means it parsed but violated
the field constraints (e.g. heat 150 %). Advice that is well-typed but
rejected by safety policy (rate-limited, drop in the wrong phase) is not an
advisor failure — that is the normal policy path. Every failure becomes a
rejected recommendation with the deterministic hold-current-targets
fallback (``SafetyPolicy.evaluate_advisor_failure``).
"""

import hashlib
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import Enum
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import (
    Agent,
    ModelAPIError,
    ModelSettings,
    UnexpectedModelBehavior,
)
from pydantic_ai.models import Model

from roastpilot_agent.config import AdvisorConfig
from roastpilot_agent.models import RoastPhase

_log = logging.getLogger(__name__)


class AdvisorError(Exception):
    """Base class for advisor-layer failures (component plan §4)."""


class AdvisorMalformedOutputError(AdvisorError):
    """Provider output could not be parsed into the ``RoastDecision`` shape."""


class AdvisorUnsafeOutputError(AdvisorError):
    """Provider output parsed but violated ``RoastDecision`` constraints."""


class AdvisorProviderError(AdvisorError):
    """Transport or API failure reaching the advisory provider."""


class AdvisorFailureMode(Enum):
    """Scriptable advisor failure modes — one per failure status.

    Plain ``Enum``, never ``StrEnum`` (D15): values are wire forms, and a
    string comparison against one is a pyright strict error in core logic.
    """

    MALFORMED = "malformed"
    UNSAFE = "unsafe"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"


class AdvisorContext(BaseModel):
    """Structured context provided to the advisory layer.

    Built from the MCP roast state, the frozen profile, and recent
    decisions; ``reference_roasts`` stays empty until M2 (component plan §4).
    """

    phase: RoastPhase
    roast_elapsed_seconds: float
    development_elapsed_seconds: float | None
    current_bean_temp_c: float
    current_env_temp_c: float
    bean_ror_c_per_min: float | None
    env_ror_c_per_min: float | None
    target_drop_temp_c: float
    profile_name: str
    recent_telemetry_samples: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    first_crack_detected: bool = False
    first_crack_timestamp_seconds: float | None = None


class RoastDecision(BaseModel):
    """Typed advisory recommendation returned by the advisor."""

    target_heat: int = Field(ge=0, le=100)
    target_fan: int = Field(ge=0, le=100)
    should_drop: bool
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class AdvisorUsage(BaseModel):
    """Token usage from one advisory call (cost/observability — E8-S4).

    ``output_tokens`` includes any reasoning tokens (providers bill reasoning
    as completion), so it is the right basis for cost; ``reasoning_tokens`` is
    surfaced separately when the provider reports it, to expose the reasoning
    'tax'.
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    reasoning_tokens: int | None = None


class RoastAdvisor(ABC):
    """Advisor interface — the controller never depends on provider concepts."""

    @abstractmethod
    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Return a typed advisory recommendation."""
        raise NotImplementedError


FakeAdvisorStep = RoastDecision | AdvisorFailureMode
"""One scripted FakeAdvisor outcome: a decision to return, or a failure to raise."""


class FakeAdvisor(RoastAdvisor):
    """Deterministic, scriptable advisor for tests and demos (E8-S1).

    Script steps are consumed in order. A ``RoastDecision`` step is returned
    as-is; an ``AdvisorFailureMode`` step raises the matching typed error so
    the controller exercises the rejected-recommendation fallback.
    ``AdvisorFailureMode.TIMEOUT`` raises ``TimeoutError`` directly — the
    deterministic equivalent of the controller's ``asyncio.wait_for``
    expiring, without consuming the configured timeout; the real
    elapsed-time path is covered separately by a never-resolving advisor.

    When the script is exhausted, ``default_decision`` is returned if
    configured (demo-friendly: constant deterministic advice), otherwise
    ``AdvisorProviderError`` is raised so a test with an underscripted
    advisor fails loudly. Received contexts are recorded on ``contexts``;
    an optional shared ``log`` list records call order across
    collaborators (the conftest fake convention).
    """

    def __init__(
        self,
        script: Sequence[FakeAdvisorStep] | None = None,
        *,
        default_decision: RoastDecision | None = None,
        log: list[str] | None = None,
    ) -> None:
        self._script: list[FakeAdvisorStep] = list(script or [])
        self._default_decision = default_decision
        self._log = log if log is not None else []
        self.contexts: list[AdvisorContext] = []

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Return the next scripted decision or raise the next scripted failure."""
        self._log.append("advisor")
        self.contexts.append(context)
        if not self._script:
            if self._default_decision is not None:
                return self._default_decision
            raise AdvisorProviderError(
                "FakeAdvisor script exhausted and no default_decision configured"
            )
        step = self._script.pop(0)
        if isinstance(step, AdvisorFailureMode):
            if step is AdvisorFailureMode.MALFORMED:
                raise AdvisorMalformedOutputError("scripted malformed-output failure")
            if step is AdvisorFailureMode.UNSAFE:
                raise AdvisorUnsafeOutputError("scripted unsafe-output failure")
            if step is AdvisorFailureMode.TIMEOUT:
                raise TimeoutError("scripted advisory timeout")
            if step is AdvisorFailureMode.PROVIDER_ERROR:
                raise AdvisorProviderError("scripted provider failure")
            # Exhaustive by intent: a mode added in a later story must be
            # handled here, not silently funnelled into provider error.
            raise AssertionError(  # pragma: no cover — exhaustiveness guard
                f"unhandled AdvisorFailureMode: {step}"
            )
        return step


class _RawRoastDecision(BaseModel):
    """Permissive structured-output shape for the model (E8-S2/D18).

    Deliberately unconstrained: the provider only has to return the right
    *shape* (a shape failure surfaces as malformed). The strict
    ``RoastDecision`` (with its 0–100 / 0–1 bounds) is validated separately
    so an out-of-range value surfaces as *unsafe*, not malformed — keeping
    the two failure modes distinct as the controller expects.
    """

    target_heat: int
    target_fan: int
    should_drop: bool
    confidence: float
    rationale: str


# Versioned prompts (component plan §4: keep prompts versioned). The active
# version is ``AdvisorConfig.prompt_version``; a context hash, never the raw
# context, is logged per call.
_PROMPTS: dict[str, str] = {
    "v0": (
        "You are an advisory assistant for a coffee roaster. You never control "
        "hardware: you return a single recommendation and a deterministic safety "
        "policy decides whether to apply it.\n"
        "Given the current roast context (JSON), return target_heat and target_fan "
        "as integer percentages 0-100, should_drop as a boolean, confidence in "
        "0.0-1.0, and a short rationale. All temperatures are Celsius. Prefer small, "
        "conservative adjustments; recommend should_drop=true only when development "
        "is genuinely complete."
    ),
    # v1 (E8-S4): tuned for an electric drum roaster, whose heating element has
    # real thermal lag — a heat change takes time to show in bean temperature.
    # v0's "small, conservative adjustments" is wrong for this hardware: it
    # reacts too late and lets a high post-first-crack RoR burn through the
    # short first-crack→drop window, cutting development time. v1 asks the
    # model to act early and decisively to maximize development time.
    "v1": (
        "You are an advisory assistant for an ELECTRIC drum coffee roaster. You "
        "never control hardware: you return a single recommendation and a "
        "deterministic safety policy validates, clamps, or rejects it before any "
        "write. All temperatures are Celsius.\n"
        "Given the current roast context (JSON), return target_heat and target_fan "
        "as integer percentages 0-100, should_drop as a boolean, confidence in "
        "0.0-1.0, and a short rationale.\n"
        "Hardware reality — act on it:\n"
        "- The electric element has THERMAL LAG: a heat change takes time to show "
        "in bean temperature. Anticipate it. Act EARLY and DECISIVELY rather than "
        "waiting for the rate-of-rise (RoR) to already be wrong — by then it is "
        "too late to correct cleanly.\n"
        "- Your primary goal in development (after first crack) is to MAXIMIZE "
        "DEVELOPMENT TIME within the first-crack-to-drop window. That window is "
        "narrow (often ~10 C of bean temperature), so a high RoR after first "
        "crack burns through it too fast and under-develops the roast. When the "
        "bean RoR is high right after first crack, make a LARGE heat reduction to "
        "flatten the curve and stretch development — small trims are not enough "
        "given the lag.\n"
        "- Use the provided target_drop_temp_c as the drop target. Recommend "
        "should_drop=true only at or very near it; otherwise keep developing. "
        "Weigh bean and environment temperature and their RoR trends.\n"
        "Bias toward decisive, anticipatory heat control over timid nudging."
    ),
    # v2 (E8-S4 fan+duration refinement): v1 treated heat as the only lever and
    # the drop temp as a hard stop. On a Hottop the fan is a primary,
    # flavor-coupled lever (it sets the heat-transfer mode and prevents
    # scorch/bake), and the real development objective is *duration* (a 10-20%
    # development ratio), not hitting a temperature. v2 asks the model to
    # coordinate heat AND fan and to judge the drop on development ratio.
    "v2": (
        "You are an advisory assistant for an electric Hottop drum coffee "
        "roaster. You never control hardware — a deterministic safety policy "
        "validates, clamps, or rejects every recommendation. All temperatures "
        "are Celsius. Return target_heat, target_fan (0-100), should_drop, "
        "confidence (0-1), and a short rationale.\n"
        "Two coupled levers — reason about both and their balance:\n"
        "- Heat sets energy into the drum. The electric element has THERMAL LAG "
        "— a change takes time to show in bean temperature, so act EARLY and "
        "DECISIVELY, anticipating it.\n"
        "- Fan/airflow sets the MODE of heat transfer and protects flavor: "
        "raising it shifts from radiant/conductive drum heat toward CONVECTIVE "
        "heat (more even, prevents scorched/baked flavor) and evacuates smoke "
        "and chaff. It is not just a coolant.\n"
        "In development (after first crack), the objective is DURATION, not a "
        "temperature. Aim for a development ratio (development time / total "
        "roast time since charge) in roughly the 10-20% range — around 10% can "
        "make an excellent roast. target_drop_temp_c is a GUIDE, not a hard "
        "stop: it is fine to develop modestly past it to hit the duration "
        "target (the safety policy owns the true ceiling), but beans can turn "
        "too dark if pushed well past ~195 C, and that threshold is "
        "bean-dependent — favor the development-ratio target and don't chase "
        "temperature. Judge should_drop primarily on the development ratio and "
        "resulting flavor; do not rush the drop just because the temperature "
        "guide is reached. To stretch development when post-crack RoR is high, "
        "cut heat substantially AND raise fan toward convective transfer — "
        "coordinate the two, minding the heat:fan balance (too much fan with "
        "too little heat crashes RoR and stalls/bakes).\n"
        "Bias toward decisive, coordinated heat-and-fan control over timid "
        "single-lever nudging."
    ),
}


def instructions_for(prompt_version: str) -> str:
    """Return the versioned advisor instructions, or raise on an unknown version."""
    try:
        return _PROMPTS[prompt_version]
    except KeyError:
        raise ValueError(f"unknown advisor prompt_version: {prompt_version!r}") from None


class AdvisorDependencyError(AdvisorError):
    """A configured provider needs an optional dependency extra that is absent."""


def _usage_from_run(usage: Any) -> AdvisorUsage:
    """Normalize a PydanticAI run usage into :class:`AdvisorUsage`.

    ``reasoning_tokens`` is read from the provider ``details`` when present
    (OpenRouter reports it for reasoning models); otherwise it stays ``None``.
    """
    raw_details = getattr(usage, "details", None)
    details: dict[str, Any] = (
        cast("dict[str, Any]", raw_details) if isinstance(raw_details, dict) else {}
    )
    reasoning: Any = details.get("reasoning_tokens")
    return AdvisorUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        reasoning_tokens=int(reasoning) if reasoning is not None else None,
    )


def reasoning_extra_body(
    reasoning_effort: Literal["off", "minimal", "low", "medium", "high"] | None,
) -> dict[str, Any] | None:
    """Map ``reasoning_effort`` to the OpenRouter ``reasoning`` request body.

    ``None`` → no override (provider default); ``"off"`` → reasoning disabled;
    an effort level → ``reasoning.effort``. OpenRouter normalizes this across
    providers; native anthropic/google ignore ``extra_body``.
    """
    if reasoning_effort is None:
        return None
    if reasoning_effort == "off":
        return {"reasoning": {"enabled": False}}
    return {"reasoning": {"effort": reasoning_effort}}


def build_model(config: AdvisorConfig) -> Model:
    """Build the PydanticAI ``Model`` for ``config.provider`` (D18).

    One factory, one advisor — only model construction varies per provider.
    Native ``openai`` / ``anthropic`` / ``google`` go direct via PydanticAI's
    provider classes; ``ollama`` / ``openai_compatible`` use an
    OpenAI-compatible model pointed at ``config.provider_base_url``
    (OpenRouter by default, or a LAN Ollama URL). The API key is read here
    from the env var named by ``config.api_key_env`` and handed to the
    provider — never stored. Provider SDK imports are lazy so a lean install
    only needs the extra for the provider it actually uses; a missing extra
    raises :class:`AdvisorDependencyError` with the install hint.
    """
    api_key = os.environ.get(config.api_key_env)
    provider = config.provider
    try:
        if provider == "openai":
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            return OpenAIChatModel(config.model_slug, provider=OpenAIProvider(api_key=api_key))
        if provider == "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return AnthropicModel(config.model_slug, provider=AnthropicProvider(api_key=api_key))
        if provider == "google":
            from pydantic_ai.models.google import GoogleModel
            from pydantic_ai.providers.google import GoogleProvider

            return GoogleModel(config.model_slug, provider=GoogleProvider(api_key=api_key))
        if provider in ("ollama", "openai_compatible"):
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            # The OpenAI client requires a non-empty key even for a keyless
            # local Ollama endpoint; fall back to a placeholder so a LAN
            # Ollama with no auth still constructs.
            return OpenAIChatModel(
                config.model_slug,
                provider=OpenAIProvider(
                    base_url=config.provider_base_url, api_key=api_key or "not-required"
                ),
            )
    except ImportError as exc:  # pragma: no cover — needs the extra uninstalled
        # Only the native providers have their own extra; openai / ollama /
        # openai_compatible share the openai-compatible core dependency.
        extra = {"anthropic": "anthropic", "google": "google"}.get(provider)
        hint = (
            f"pip install 'roastpilot-agent[{extra}]'"
            if extra is not None
            else "reinstall roastpilot-agent — its openai-compatible core dependency is missing"
        )
        raise AdvisorDependencyError(
            f"advisor provider {provider!r} needs an optional dependency: {hint}"
        ) from exc
    # Unreachable while ``provider`` stays a closed Literal; pyright treats the
    # branches above as exhaustive, so this is a defensive backstop.
    raise AdvisorError(f"unsupported advisor provider: {provider!r}")  # pragma: no cover


class PydanticAIAdvisor(RoastAdvisor):
    """Provider-agnostic PydanticAI advisor (D5 + D18).

    One advisor over any provider: it consumes the :class:`Model` from
    :func:`build_model` (or an injected model — the recorded-response test
    seam) and owns everything provider-independent — structured output via
    PydanticAI, versioned prompts, context-hash logging, and the typed-error
    mapping. Failures map to the controller's vocabulary: a shape the model
    could not produce ⇒ :class:`AdvisorMalformedOutputError`; a well-shaped
    output that violates the ``RoastDecision`` bounds ⇒
    :class:`AdvisorUnsafeOutputError`; any transport/API failure ⇒
    :class:`AdvisorProviderError`. ``asyncio``-level ``TimeoutError`` is left
    to propagate so the controller's ``wait_for`` owns the timeout.
    """

    def __init__(self, config: AdvisorConfig, *, model: Model | None = None) -> None:
        self._config = config
        self._model = model if model is not None else build_model(config)
        #: Token usage from the most recent call (cost/observability); ``None``
        #: until the first successful call.
        self.last_usage: AdvisorUsage | None = None
        settings = ModelSettings(temperature=config.temperature)
        extra_body = reasoning_extra_body(config.reasoning_effort)
        if extra_body is not None:
            settings["extra_body"] = extra_body
        self._agent: Agent[None, _RawRoastDecision] = Agent(
            self._model,
            output_type=_RawRoastDecision,
            instructions=instructions_for(config.prompt_version),
            model_settings=settings,
        )

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Run the configured model and return a validated recommendation."""
        context_json = context.model_dump_json()
        context_hash = hashlib.sha256(context_json.encode()).hexdigest()
        _log.info(
            "advisory request",
            extra={
                "context_hash": context_hash,
                "provider": self._config.provider,
                "model_slug": self._config.model_slug,
                "prompt_version": self._config.prompt_version,
            },
        )
        try:
            result = await self._agent.run(context_json)
        except UnexpectedModelBehavior as exc:
            raise AdvisorMalformedOutputError(str(exc)) from exc
        except ModelAPIError as exc:
            raise AdvisorProviderError(str(exc)) from exc
        self.last_usage = _usage_from_run(result.usage)
        try:
            return RoastDecision.model_validate(result.output.model_dump())
        except ValidationError as exc:
            raise AdvisorUnsafeOutputError(str(exc)) from exc
