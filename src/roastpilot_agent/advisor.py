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

import asyncio
import hashlib
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import Enum
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import (
    Agent,
    ModelAPIError,
    ModelSettings,
    UnexpectedModelBehavior,
)
from pydantic_ai.models import Model

from roastpilot_agent.config import AdvisorConfig
from roastpilot_agent.models import AdvisorHealth, AdvisorHealthStatus, RoastPhase

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

    ``target_development_percent`` and the charge guidance band
    (``charge_guidance_min_c`` / ``charge_guidance_max_c``) are copied from the
    frozen profile so the stage-tuned prompt (``v3``) has explicit targets to
    aim at — the development-ratio goal for the FC/development section and the
    charge-temperature band for the preheat section. They are context only and
    carry no control authority; the controller and safety policy never read
    them back. They default to ``None`` so a context built without a profile
    (or by an older caller) stays valid.
    """

    phase: RoastPhase
    roast_elapsed_seconds: float
    development_elapsed_seconds: float | None
    current_bean_temp_c: float
    current_env_temp_c: float
    bean_ror_c_per_min: float | None
    env_ror_c_per_min: float | None
    target_drop_temp_c: float
    target_development_percent: float | None = None
    charge_guidance_min_c: float | None = None
    charge_guidance_max_c: float | None = None
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


class AdvisorDescriptor(BaseModel):
    """Stable identity of an advisor for the decision trace (#167).

    The provider/model/prompt-version triple that the controller persists with
    every advisor row so post-roast diagnosis can read *which* configuration
    produced (or failed to produce) a decision — the field that the #134
    failure could not be recovered from the database. It is identity metadata,
    not a provider concept the controller reasons over: the controller only
    reads it to forward it to the store.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    prompt_version: str


class RoastAdvisor(ABC):
    """Advisor interface — the controller never depends on provider concepts."""

    @property
    @abstractmethod
    def descriptor(self) -> AdvisorDescriptor:
        """The advisor's identity for the decision trace (#167).

        Provider, model slug, and prompt version — persisted with every
        advisor decision so the trace records which configuration ran. Static
        per advisor instance; no provider round trip.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Return a typed advisory recommendation."""
        raise NotImplementedError

    @abstractmethod
    async def healthcheck(self) -> AdvisorHealth:
        """Probe advisor reachability (issue #168).

        A cheap, read-only liveness probe run at ``serve`` startup so the
        operator learns the advisor is dead *before* charging beans, rather
        than after every in-roast call fails (the #134 expired-key failure).
        It returns reachable-or-error and never raises: a probe failure (auth
        401/402, model 404, transport, or timeout) is captured into an
        ``UNREACHABLE`` :class:`~roastpilot_agent.models.AdvisorHealth` so a
        hung or rejecting provider can never wedge or abort startup. The probe
        is advisory-only — it never receives MCP write tools.

        Returns:
            The reachability result with the configured provider/model and, on
            failure, the captured provider error message.
        """
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
        health: "AdvisorHealth | BaseException | None" = None,
    ) -> None:
        self._script: list[FakeAdvisorStep] = list(script or [])
        self._default_decision = default_decision
        self._log = log if log is not None else []
        self.contexts: list[AdvisorContext] = []
        #: Scriptable :meth:`healthcheck` outcome (issue #168). An
        #: :class:`~roastpilot_agent.models.AdvisorHealth` is returned as-is; a
        #: ``BaseException`` is raised (to exercise the probe wrapper's
        #: non-blocking, error-capturing guarantee); ``None`` defaults to a
        #: deterministic ``REACHABLE`` so a no-key test just works.
        self._health = health

    @property
    def descriptor(self) -> AdvisorDescriptor:
        """The fake advisor's fixed trace identity (#167)."""
        return AdvisorDescriptor(provider="fake", model="fake-model", prompt_version="fake")

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

    async def healthcheck(self) -> AdvisorHealth:
        """Return the scripted reachability outcome (deterministic, no key).

        Defaults to a ``REACHABLE`` result so a test needs no API key; a
        configured ``AdvisorHealth`` is returned as-is, and a configured
        ``BaseException`` is raised so a test can exercise the probe wrapper's
        bounded, error-capturing guarantee.
        """
        self._log.append("advisor.healthcheck")
        if isinstance(self._health, BaseException):
            raise self._health
        if self._health is not None:
            return self._health
        return AdvisorHealth(
            status=AdvisorHealthStatus.REACHABLE,
            provider="fake",
            model_slug="fake-model",
        )


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
    # v3 (issue #172): Option 2 — ONE prompt with explicit per-stage sections.
    # v2 is a single generalist block and leaves the model to infer which
    # advice applies to the current phase; v3 keeps v2's electric Hottop
    # framing (two coupled levers, thermal lag, development = duration) but
    # organizes the guidance into PREHEAT / DRYING-MAILLARD / FC-DEVELOPMENT
    # sections so the model follows the one matching context.phase, and it aims
    # the sections at the new context targets (charge guidance band, target
    # development percent). FIRST DRAFT — content is pending bake-off validation
    # (#173) before it can become the default; v2 stays the default until then.
    "v3": (
        "You are an advisory assistant for an electric Hottop drum coffee "
        "roaster. You never control hardware — a deterministic safety policy "
        "validates, clamps, or rejects every recommendation. All temperatures "
        "are Celsius. Return target_heat, target_fan (0-100), should_drop, "
        "confidence (0-1), and a short rationale.\n"
        "Two coupled levers, true in every stage — reason about both and their "
        "balance:\n"
        "- Heat sets energy into the drum. The electric element has THERMAL LAG "
        "— a change takes time to show in bean temperature, so act EARLY and "
        "DECISIVELY, anticipating it; timid trims react too late.\n"
        "- Fan/airflow sets the MODE of heat transfer and protects flavor: "
        "raising it shifts from radiant/conductive drum heat toward CONVECTIVE "
        "heat (more even, prevents scorched/baked flavor) and evacuates smoke "
        "and chaff. It is not just a coolant. Too much fan with too little heat "
        "crashes the rate-of-rise (RoR) and stalls/bakes the roast.\n"
        "The context JSON includes the current phase — follow the matching "
        "section below.\n"
        "PREHEAT (before the beans are charged): the goal is to bring the drum "
        "to the charge band given by charge_guidance_min_c / "
        "charge_guidance_max_c and hold it steady there. Guide heat/fan to reach "
        "and stabilize inside that band; advise on charge readiness and timing "
        "via the rationale (ready to charge once stable in band, hold or trim if "
        "over/under). Do not recommend should_drop in preheat.\n"
        "DRYING / MAILLARD (charged through to just before first crack): manage "
        "the RoR DECLINE. The charge dunks the drum temperature, then bean "
        "temperature climbs and RoR should ease smoothly downward toward first "
        "crack — never flatten to a stall (baking) and never flick back upward. "
        "Because of the thermal lag this is the stage that needs EARLY, often "
        "DRASTIC heat cuts: reduce heat well before the RoR is visibly wrong, "
        "and coordinate fan to steer convective transfer. Do not drop here.\n"
        "FIRST CRACK / DEVELOPMENT (first crack detected onward): the objective "
        "is DURATION, not a temperature. Aim for a development ratio "
        "(development time / total roast time since charge) near "
        "target_development_percent; a ratio in roughly the 10-20% range makes "
        "an excellent roast and around 10% can be plenty. target_drop_temp_c is "
        "a GUIDE, not a hard stop — it is fine to develop modestly past it to "
        "hit the duration target (the safety policy owns the true ceiling), but "
        "beans can turn too dark pushed well past ~195 C, and that threshold is "
        "bean-dependent; favor the development-ratio target and don't chase "
        "temperature. To stretch development when post-crack RoR is high, cut "
        "heat substantially AND raise fan toward convective transfer. Judge "
        "should_drop primarily on the development ratio and resulting flavor; "
        "recommend the drop decisively once the ratio target is met — do not "
        "rush it because the temperature guide is reached, nor dither once it is "
        "developed. FC consults are rapid: be concise and decisive.\n"
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


def usage_from_run(usage: Any) -> AdvisorUsage:
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


def build_model(config: AdvisorConfig, *, model_slug: str | None = None) -> Model:
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

    Args:
        config: The advisor configuration (provider, base URL, key env var).
        model_slug: The model slug to construct. Defaults to
            ``config.model_slug``; the per-phase advisor (#173) passes the
            phase-resolved slug here so one provider config can serve several
            models. The provider is always ``config.provider`` — per-phase
            selection varies the model, not the provider.

    Returns:
        The constructed PydanticAI ``Model`` for the given slug and provider.
    """
    slug = model_slug if model_slug is not None else config.model_slug
    api_key = os.environ.get(config.api_key_env)
    provider = config.provider
    try:
        if provider == "openai":
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            return OpenAIChatModel(slug, provider=OpenAIProvider(api_key=api_key))
        if provider == "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return AnthropicModel(slug, provider=AnthropicProvider(api_key=api_key))
        if provider == "google":
            from pydantic_ai.models.google import GoogleModel
            from pydantic_ai.providers.google import GoogleProvider

            return GoogleModel(slug, provider=GoogleProvider(api_key=api_key))
        if provider in ("ollama", "openai_compatible"):
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            # The OpenAI client requires a non-empty key even for a keyless
            # local Ollama endpoint; fall back to a placeholder so a LAN
            # Ollama with no auth still constructs.
            return OpenAIChatModel(
                slug,
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
        #: An injected model (the recorded-response test seam) pins every phase
        #: to that one model — per-phase resolution is bypassed so a test
        #: double drives all calls. When ``None``, each phase-resolved slug
        #: (#173) gets its own lazily-built, cached agent.
        self._injected_model = model
        #: Token usage from the most recent model response (cost/observability);
        #: ``None`` until the first one. Captured as soon as the provider
        #: returns, so it reflects a call whose output later fails strict
        #: re-validation (``AdvisorUnsafeOutputError``) — the tokens were still
        #: spent. It is *not* updated when the call itself fails before
        #: returning (malformed/provider error), so it keeps the last good
        #: reading.
        self.last_usage: AdvisorUsage | None = None
        settings = ModelSettings(temperature=config.temperature)
        extra_body = reasoning_extra_body(config.reasoning_effort)
        if extra_body is not None:
            settings["extra_body"] = extra_body
        self._model_settings = settings
        self._instructions = instructions_for(config.prompt_version)
        #: Per-slug agent cache (#173). One agent per distinct model slug —
        #: instructions and settings are slug-independent, only the underlying
        #: ``Model`` varies. With the Opus-everywhere default every phase
        #: resolves to the same slug, so exactly one agent is built: a clean
        #: behavioral no-op. Keyed by slug; ``_injected_model`` short-circuits
        #: the cache for the test seam. ``descriptor``/``healthcheck`` warm the
        #: base ``model_slug`` entry eagerly so the prior single-agent eager
        #: construction (and its import-error surface) is preserved.
        self._agents: dict[str, Agent[None, _RawRoastDecision]] = {}
        self._agent_for(config.model_slug)

    def _agent_for(self, model_slug: str) -> "Agent[None, _RawRoastDecision]":
        """Return the cached agent for ``model_slug``, building it on first use.

        An injected model (the test seam) is used for every slug; otherwise the
        model is built once per slug via :func:`build_model` and cached. The
        agent's instructions and settings are slug-independent — only the
        underlying ``Model`` varies — so this is the per-phase model selection
        seam (#173) with no other behavior change.

        Args:
            model_slug: The phase-resolved model slug to get an agent for.

        Returns:
            The cached (or newly built) agent for ``model_slug``.
        """
        agent = self._agents.get(model_slug)
        if agent is None:
            model = (
                self._injected_model
                if self._injected_model is not None
                else build_model(self._config, model_slug=model_slug)
            )
            agent = Agent(
                model,
                output_type=_RawRoastDecision,
                instructions=self._instructions,
                model_settings=self._model_settings,
            )
            self._agents[model_slug] = agent
        return agent

    @property
    def descriptor(self) -> AdvisorDescriptor:
        """The configured provider/model/prompt-version trace identity (#167).

        The ``model`` is the base :attr:`AdvisorConfig.model_slug`. Per-phase
        selection (#173) varies which model actually runs a given call; the
        descriptor stays the stable advisor-level identity (every phase
        resolves to this slug under the Opus-everywhere default, so it is
        accurate today, and it remains the advisor's configured-model identity
        once the FC slot is flipped).
        """
        return AdvisorDescriptor(
            provider=self._config.provider,
            model=self._config.model_slug,
            prompt_version=self._config.prompt_version,
        )

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Run the phase-resolved model and return a validated recommendation.

        The model slug is selected by ``context.phase`` via
        :meth:`AdvisorConfig.model_for` (#173) — with the Opus-everywhere
        default this is the single configured model in every phase. The
        per-phase agent is cached, so flipping the FC/development slot to a
        faster model after the bake-off changes only which agent runs, not the
        call path.
        """
        model_slug = self._config.model_for(context.phase)
        agent = self._agent_for(model_slug)
        context_json = context.model_dump_json()
        context_hash = hashlib.sha256(context_json.encode()).hexdigest()
        _log.info(
            "advisory request",
            extra={
                "context_hash": context_hash,
                "provider": self._config.provider,
                "model_slug": model_slug,
                "phase": context.phase.value,
                "prompt_version": self._config.prompt_version,
            },
        )
        try:
            result = await agent.run(context_json)
        except UnexpectedModelBehavior as exc:
            raise AdvisorMalformedOutputError(str(exc)) from exc
        except ModelAPIError as exc:
            raise AdvisorProviderError(str(exc)) from exc
        self.last_usage = usage_from_run(result.usage)
        try:
            return RoastDecision.model_validate(result.output.model_dump())
        except ValidationError as exc:
            raise AdvisorUnsafeOutputError(str(exc)) from exc

    async def healthcheck(self) -> AdvisorHealth:
        """Probe reachability with a cheap, bounded completion (issue #168).

        Runs one minimal structured completion against the configured provider
        and model. The point is the *transport*: an expired/invalid key
        (401/402), an unavailable model slug (404), or an unreachable endpoint
        fails before any output is produced — exactly the #134 failure that
        "advisor configured" hid until mid-roast. A malformed/unsafe *output*
        still counts as REACHABLE (the provider answered; the round trip
        works). The call is bounded by ``config.healthcheck_timeout_seconds``
        and never raises — a timeout or any provider error is captured into an
        ``UNREACHABLE`` result so it can never wedge or abort ``serve``
        startup. Advisory-only: no MCP write tools are ever passed.

        Returns:
            ``REACHABLE`` with provider/model when the probe round-trips, else
            ``UNREACHABLE`` carrying the provider error (or timeout) message.
        """
        provider = self._config.provider
        model_slug = self._config.model_slug
        agent = self._agent_for(model_slug)
        try:
            async with asyncio.timeout(self._config.healthcheck_timeout_seconds):
                # A trivial prompt: reachability is decided by the transport
                # (auth/model/endpoint), not the content. The structured
                # output_type is the advisor's own — still advisory-only. The
                # probe uses the base model_slug — the descriptor's identity and
                # the model every phase resolves to under the default (#173).
                await agent.run("ping")
        except TimeoutError:
            return AdvisorHealth(
                status=AdvisorHealthStatus.UNREACHABLE,
                provider=provider,
                model_slug=model_slug,
                error=(
                    f"reachability probe timed out after "
                    f"{self._config.healthcheck_timeout_seconds:g}s"
                ),
            )
        except UnexpectedModelBehavior as exc:
            # The provider answered but the output was malformed — the round
            # trip works, so the advisor IS reachable.
            _log.warning("advisor reachable but probe output was malformed: %s", exc)
            return AdvisorHealth(
                status=AdvisorHealthStatus.REACHABLE,
                provider=provider,
                model_slug=model_slug,
            )
        except Exception as exc:  # noqa: BLE001 — probe must never raise (best-effort)
            return AdvisorHealth(
                status=AdvisorHealthStatus.UNREACHABLE,
                provider=provider,
                model_slug=model_slug,
                error=str(exc),
            )
        return AdvisorHealth(
            status=AdvisorHealthStatus.REACHABLE,
            provider=provider,
            model_slug=model_slug,
        )
