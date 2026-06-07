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
from typing import Any

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
}


def instructions_for(prompt_version: str) -> str:
    """Return the versioned advisor instructions, or raise on an unknown version."""
    try:
        return _PROMPTS[prompt_version]
    except KeyError:
        raise ValueError(f"unknown advisor prompt_version: {prompt_version!r}") from None


class AdvisorDependencyError(AdvisorError):
    """A configured provider needs an optional dependency extra that is absent."""


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
        self._agent: Agent[None, _RawRoastDecision] = Agent(
            self._model,
            output_type=_RawRoastDecision,
            instructions=instructions_for(config.prompt_version),
            model_settings=ModelSettings(temperature=config.temperature),
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
        try:
            return RoastDecision.model_validate(result.output.model_dump())
        except ValidationError as exc:
            raise AdvisorUnsafeOutputError(str(exc)) from exc
