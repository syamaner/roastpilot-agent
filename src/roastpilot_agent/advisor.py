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
from roastpilot_agent.models import AdvisorHealth, AdvisorHealthStatus, RoastPhase, RoastStyle
from roastpilot_agent.roast_history import (
    DecisionTraceEntry,
    RoastCurveSample,
    RoastMilestone,
)

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

    ``seconds_since_charge`` is the seconds elapsed since the debounced
    T0/charge instant (``None`` before charge); it is advisory context only,
    with no control authority — the controller and safety policy never read it
    back. It lets the model reason that a freshly-charged bean is in early
    drying, so its RoR will be negative and then turn, rather than mistaking
    the post-charge crash for a stall (#209).

    The phase-resolved control LIMITS (``heat_floor_percent`` /
    ``heat_ceiling_percent`` / ``fan_floor_percent`` / ``fan_ceiling_percent`` /
    ``bitter_ceiling_temp_c`` / ``emergency_drop_temp_c``) are the explicit box
    the model must reason inside (D35 §8.2, #273). They are NOT a second copy of
    the numbers: the controller resolves them from the single
    :class:`~roastpilot_agent.control_policy.RoastControlPolicy`, and the *same*
    resolved heat/fan box is what the safety gate clamps a command into — so the
    value the model is told equals the value enforced (told == enforced). They
    default to ``None`` so a context built without the policy (or by an older
    caller) stays valid; populated, they are read-only context, never a control
    authority the advisor can widen.

    The per-tick control-loop context (D40.3 / D40.5, #275) carries the
    roast-so-far telemetry curve (``roast_curve_window`` — a bounded recent
    full-resolution window — plus ``roast_milestones``, the turning-point /
    recovery / drying-end / first-crack summary), the model's own prior
    recommendations (``decision_trace``, the #218 anti-thrash history), the DTR
    (``development_time_ratio``, distinct from the ``development_elapsed_seconds``
    duration), and the validation-supported FC-ETA (``first_crack_eta_seconds``,
    #229 KEEP). All are read-only context with no control authority, default
    empty / ``None`` for callers that build no history, and are assembled by the
    controller from :class:`~roastpilot_agent.roast_history.RoastHistory`.

    ``current_heat_percent`` / ``current_fan_percent`` (#497, D89 Tier 1) carry
    the ACTUATED heat/fan — never the advisor's own requested values — so the
    model can tell what is really happening at the roaster instead of assuming
    its last recommendation applied. ``post_fc_loop_active`` names the regime
    where that assumption is false: while the deterministic post-FC RoR-taper
    loop (#405/D88) owns DEVELOPMENT heat, the advisor's ``target_heat`` is
    traced but never actuated. Both are read-only context with no control
    authority; default ``None``/``False`` for callers that build no controller
    (older callers, the drop-only bake-off).

    ``target_development_percent_min`` / ``_max`` (#499, D89 Tier 1) give the
    advisor an acceptable DEVELOPMENT-RATIO WINDOW around
    ``target_development_percent`` instead of a bare point, computed from the
    SAME ``drop_dev_margin_percent`` the deterministic drop-coherence guard
    reads (never a copied constant). ``roast_style`` surfaces the profile's
    qualitative style (light/medium/dark) as INTENT ONLY — never its reference
    numbers, which never override the profile's own authoritative explicit
    targets (D84 held). All three are read-only context with no control
    authority; the deterministic drop anchor and the 196 °C ceiling guard are
    unchanged by this story. Default ``None`` for callers that build no
    profile (older callers, the drop-only bake-off).
    """

    phase: RoastPhase
    roast_elapsed_seconds: float
    """Seconds since charge (T0) — the advisor's DTR denominator (#219). ``0.0``
    before charge. Charge-referenced (not run/preheat start) so the model's DTR =
    ``development_elapsed_seconds / roast_elapsed_seconds`` matches the v4-prompt
    definition the bake-off validated (its context fixtures start at charge). This
    is advisory context only: it is NOT the run-referenced clock the SPA charts
    (``ControllerSnapshot.roast_elapsed_seconds`` / the SSE ``elapsed_seconds``),
    whose origin stays run/preheat-start until #220 re-origins the chart."""
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
    # Seconds since the debounced T0/charge instant (None before charge);
    # advisory context only, no control authority — the controller and safety
    # policy never read it back. Lets the model reason "just charged → early
    # drying, RoR will be negative then turn" rather than misreading the
    # post-charge crash as a stall (#209).
    seconds_since_charge: float | None = None
    # D35 §8.2 (#273): the live, phase-resolved control box the model is told it
    # must reason inside. Resolved by the controller from the single
    # control_policy.RoastControlPolicy; the SAME heat/fan box is what
    # safety.evaluate_command clamps into (told == enforced — no second copy).
    # None when the context is built without the policy (older callers / tests).
    heat_floor_percent: int | None = Field(default=None, ge=0, le=100)
    heat_ceiling_percent: int | None = Field(default=None, ge=0, le=100)
    fan_floor_percent: int | None = Field(default=None, ge=0, le=100)
    fan_ceiling_percent: int | None = Field(default=None, ge=0, le=100)
    bitter_ceiling_temp_c: float | None = None
    emergency_drop_temp_c: float | None = None
    # D40.3 / D40.5 (#275): the per-tick control-loop context the model reasons
    # on — the roast-so-far telemetry curve (a bounded recent full-res window +
    # a milestone summary), the model's OWN prior recommendations (the #218
    # anti-thrash decision trace), the DTR, and the validation-supported FC-ETA
    # (#229 KEEP). Built by the controller from roast_history.RoastHistory. Like
    # every AdvisorContext field this is read-only context with no control
    # authority — the controller and safety policy never read it back. All
    # default empty / None so a context built without history (older callers,
    # the drop-only bake-off) stays valid. Wiring these into the live post-FC
    # consult is #276; this story populates them.
    roast_curve_window: list[RoastCurveSample] = Field(default_factory=list[RoastCurveSample])
    roast_milestones: list[RoastMilestone] = Field(default_factory=list[RoastMilestone])
    decision_trace: list[DecisionTraceEntry] = Field(default_factory=list[DecisionTraceEntry])
    # DTR (development time as a SHARE of the charge-referenced roast clock) — a
    # value DISTINCT from development_elapsed_seconds (the duration). Both come
    # from the existing #219/#220/#235/#239 clocks (reused, not reinvented);
    # None before first crack.
    development_time_ratio: float | None = None
    # FC-ETA: predicted seconds until first crack from RoR extrapolation (#229
    # KEEP). A pre-FC anticipation trigger only, never a lever move on its own;
    # None once FC is detected or before there is enough curve to project.
    first_crack_eta_seconds: float | None = None
    # #497 (D89 Tier 1): the ACTUATED heat/fan — the SAME actuated-output
    # values the told==enforced safety box is built from (#412), never the
    # advisor's own requested/recommended values. Populated by the controller
    # from its actuated-lever tracking, updated only once a write reaches the
    # roaster. Evidence (11 Jul D88 validation roast): with the post-FC loop
    # engaged, actuated heat was pinned at 65 % by the deterministic taper, yet
    # the advisor's rationale said "heat is already at its minimum" — it had no
    # way to see what was really actuating and reasoned from an imagined
    # heat-0. Read-only context with no control authority; the controller and
    # safety policy never read these back. Default ``None`` so a context built
    # without a controller (older callers, the drop-only bake-off) stays valid.
    current_heat_percent: int | None = Field(default=None, ge=0, le=100)
    current_fan_percent: int | None = Field(default=None, ge=0, le=100)
    # #497 (D89 Tier 1): True iff the deterministic post-FC RoR-taper loop
    # (#405/D88) currently OWNS DEVELOPMENT heat this tick — the same
    # ``post_fc_loop_active`` predicate the controller already used to decide
    # whether to actuate the advisor's own heat/fan recommendation
    # (:meth:`~roastpilot_agent.controller.RoastController._post_fc_loop_active`).
    # When True the advisor's ``target_heat`` is traced for observability but
    # never actuated (fan stays pinned too, D88(5)), so the c1 prompt teaches
    # the model to treat its own heat number as advisory-only in that regime
    # and read ``current_heat_percent`` as the real, taper-actuated value
    # instead of reasoning from its last recommendation. Defaults ``False`` so
    # a context built without the post-FC loop (pre-FC phases, flag-off roasts,
    # older callers) reads as the byte-for-byte pre-#497 regime.
    post_fc_loop_active: bool = False
    # #499 (D89 Tier 1): the acceptable DEVELOPMENT-RATIO WINDOW around
    # ``target_development_percent`` — ``[target - drop_dev_margin_percent,
    # target + drop_dev_margin_percent]``, computed by the controller from the
    # SAME ``config.drop_dev_margin_percent`` the deterministic drop-coherence
    # guard reads (never a second/copied constant — the told==enforced
    # pattern applied to a margin, so the tolerance the model reasons with and
    # the tolerance the deterministic layer enforces can never drift). Fixes
    # the 11 Jul "first-past-the-post" evidence (#499): roast 11 dropped the
    # instant DTR hit its single point target, 5 °C short of the drop-temp
    # target — a window (paired with the c1 joint-objective teaching) gives
    # the model room to hold a little past the window's low edge while
    # temperature closes the gap, rather than reading the bare point target
    # as the finish line. Read-only context with no control authority — the
    # deterministic drop anchor and the 196 °C ceiling guard are UNCHANGED by
    # this field (D89: those paths stay exactly as they are). ``None`` when
    # the context is built without a profile (older callers, the drop-only
    # bake-off).
    target_development_percent_min: float | None = None
    target_development_percent_max: float | None = None
    # #499 (D89 Tier 1): the profile's qualitative roast-style NAME (e.g.
    # ``RoastStyle.MEDIUM``), surfaced as INTENT ONLY — never the style's own
    # reference numbers (:data:`~roastpilot_agent.models.ROAST_STYLE_TARGETS`),
    # which could contradict the profile's authoritative explicit
    # ``target_drop_temp_c``/``target_development_percent`` and recreate the
    # exact confusion #499 exists to fix (D84's explicit-wins precedence is
    # UNCHANGED — this field never overrides it, and the c1 prompt says so
    # explicitly). A plain ``Enum`` (D15), matching ``phase: RoastPhase``
    # above — never string-compared in core logic. ``None`` when the profile
    # carries no style (pre-#405 profiles) or the context is built without a
    # profile.
    roast_style: RoastStyle | None = None


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

    def descriptor_for(self, phase: RoastPhase) -> AdvisorDescriptor:
        """The trace identity for a call made in ``phase`` (#189).

        Defaults to :attr:`descriptor`. Advisors with per-phase model selection
        (#173) override this so the persisted advisor-decision row records the
        model that actually answered the call (the phase-RESOLVED slug), not the
        base slug — which matters once the FC/development slot is flipped to a
        faster model and base ≠ resolved.
        """
        return self.descriptor

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
    # v4-v8 (issue #194): the Artisan-expanded bake-off (28 quality-filtered
    # roasts, drop < 198 C indicated) confirmed gemini-3.1-flash-lite + v2 is the
    # only model that calls the drop, but its drop recall is ~0.64 — it MISSES the
    # drop on ~10/28 roasts, leaning to develop LATER than the operator did. Root
    # cause is v2's drop language ("guide not a hard stop … fine to develop
    # modestly PAST it … don't chase temperature … do not rush the drop"), which
    # holds the model past the operator's empirical drops.
    #
    # Design corpus (operator-verified, NOT in the repo — reason from CONTEXT,
    # never hardcode textbook °C):
    #  - The probe reads ~20-25 C BELOW published, so absolute °C are
    #    roaster/probe-specific. Reason from the live current_bean_temp_c and the
    #    profile's target_drop_temp_c / target_development_percent in the context
    #    JSON. The operator's empirical cluster (FC ~178 C indicated, drop low-190s
    #    indicated, DTR ~15-16%) is the GROUND-TRUTH RATIONALE for anchoring near
    #    the profile target — not a literal gate.
    #  - Bitter ceiling = ~196 C INDICATED (hard flavor ceiling, not a target):
    #    with the offset that is the phenylindane / ashy dark-roast onset.
    #    Overshooting it is the costly, irreversible error → bias the drop NOT to
    #    exceed it.
    #  - DTR is an INDICATOR, not a flavor dial (Rao). Honor the profile's
    #    target_development_percent; great roasts span DTR 8-20%; do not impose a
    #    generic number.
    #  - The drop is a TWO-SIDED WINDOW: FLOOR (RoR-decline essentially done +
    #    DTR in the profile band + roughly FC+60-100 s) and CEILING (<=196
    #    indicated), plus a post-FC FLICK GUARD (RoR re-acceleration after FC =
    #    ashy = a drop signal). Too-early / under-developed is ALSO bad (residual
    #    chlorogenic acid, metallic-harsh) — don't over-correct into early drops.
    #  - FC-DETECTOR LAG: the audio FC detector lags true first crack ~12-21 s and
    #    is NOT a clean fixed offset, so development_elapsed_seconds is a LOWER
    #    BOUND — true development is further along than the clock says. This is part
    #    of why the model over-holds. Do NOT subtract a magic offset; cross-check
    #    the operator's repeatable bean-temp FC signature (~178 C indicated).
    #
    # v4-v8 keep ALL of v2's heat/fan control guidance INTACT (anticipatory
    # thermal-lag cut in late Maillard + fan as a convective transfer-mode lever —
    # that scored 0.88 heat-direction and must not regress) and change ONLY the
    # drop-decision guidance, each a DISTINCT strategy for closing the recall gap.
    #
    # v4 — profile-target anchor: drop once live bean temp is at/near the
    # profile's target_drop_temp_c (approaching but not exceeding the ~196
    # indicated ceiling) and the development floor is met; do not hold higher.
    "v4": (
        "You are an advisory assistant for an electric Hottop drum coffee "
        "roaster. You never control hardware — a deterministic safety policy "
        "validates, clamps, or rejects every recommendation. All temperatures "
        "are Celsius and are this roaster's INDICATED probe readings (which run "
        "well below published bean temperatures), so reason from the live "
        "context values, never from textbook numbers. Return target_heat, "
        "target_fan (0-100), should_drop, confidence (0-1), and a short "
        "rationale.\n"
        "Two coupled levers — reason about both and their balance:\n"
        "- Heat sets energy into the drum. The electric element has THERMAL LAG "
        "— a change takes time to show in bean temperature, so act EARLY and "
        "DECISIVELY, anticipating it; the development-cut should land in late "
        "Maillard, before first crack.\n"
        "- Fan/airflow sets the MODE of heat transfer and protects flavor: "
        "raising it shifts from radiant/conductive drum heat toward CONVECTIVE "
        "heat (more even, prevents scorched/baked flavor) and evacuates smoke "
        "and chaff. It is not just a coolant.\n"
        "Drop decision (after first crack): anchor on the PROFILE. Recommend "
        "should_drop=true once the live current_bean_temp_c has reached (or is "
        "within a degree or two of) the profile's target_drop_temp_c AND the "
        "development floor is met (rate-of-rise decline essentially done and the "
        "development ratio near the profile's target_development_percent). Do NOT "
        "hold for a higher temperature than the profile target — on this roaster "
        "the operator's proven-good roasts finish near that target (an indicated "
        "cluster in the low-190s C), and ~196 C indicated is a hard bitter "
        "(ashy / over-dark) ceiling you must not push past, because that error is "
        "irreversible. To stretch development when post-crack RoR is high, cut "
        "heat substantially AND raise fan toward convective transfer — coordinate "
        "the two, minding the heat:fan balance (too much fan with too little heat "
        "crashes RoR and stalls/bakes).\n"
        "Bias toward decisive, coordinated heat-and-fan control over timid "
        "single-lever nudging."
    ),
    # v5 — profile development-target as the indicator: drop once the profile's
    # target_development_percent is met and RoR-decline is done; remove v2's
    # "develop modestly past the guide for more flavor" license.
    "v5": (
        "You are an advisory assistant for an electric Hottop drum coffee "
        "roaster. You never control hardware — a deterministic safety policy "
        "validates, clamps, or rejects every recommendation. All temperatures "
        "are Celsius and are this roaster's INDICATED probe readings (well below "
        "published values), so reason from the live context, never textbook "
        "numbers. Return target_heat, target_fan (0-100), should_drop, confidence "
        "(0-1), and a short rationale.\n"
        "Two coupled levers — reason about both and their balance:\n"
        "- Heat sets energy into the drum. The electric element has THERMAL LAG "
        "— a change takes time to show in bean temperature, so act EARLY and "
        "DECISIVELY, anticipating it; the development-cut should land in late "
        "Maillard, before first crack.\n"
        "- Fan/airflow sets the MODE of heat transfer and protects flavor: "
        "raising it shifts from radiant/conductive drum heat toward CONVECTIVE "
        "heat (more even, prevents scorched/baked flavor) and evacuates smoke "
        "and chaff. It is not just a coolant.\n"
        "Drop decision (after first crack): development is the INDICATOR, judged "
        "against the PROFILE. The development ratio (development time / total "
        "roast time since charge) is a guide, not a flavor dial — honor the "
        "profile's target_development_percent rather than any generic number. "
        "Once the development ratio has reached the profile's "
        "target_development_percent and the rate-of-rise decline is essentially "
        "done, recommend should_drop=true. Do NOT keep developing past the "
        "profile's development target for 'more flavor' — that was the old "
        "instinct and it over-holds; and never push bean temperature past ~196 C "
        "indicated, the hard bitter / ashy ceiling (irreversible). To stretch "
        "development when post-crack RoR is high, cut heat substantially AND "
        "raise fan toward convective transfer — coordinate the two, minding the "
        "heat:fan balance (too much fan with too little heat crashes RoR and "
        "stalls/bakes).\n"
        "Bias toward decisive, coordinated heat-and-fan control over timid "
        "single-lever nudging."
    ),
    # v6 — full two-sided window (floor / <=196 ceiling / post-FC flick guard),
    # floor-biased because overshooting the ceiling is the irreversible error.
    # The most research-complete variant.
    "v6": (
        "You are an advisory assistant for an electric Hottop drum coffee "
        "roaster. You never control hardware — a deterministic safety policy "
        "validates, clamps, or rejects every recommendation. All temperatures "
        "are Celsius and are this roaster's INDICATED probe readings (well below "
        "published values), so reason from the live context, never textbook "
        "numbers. Return target_heat, target_fan (0-100), should_drop, confidence "
        "(0-1), and a short rationale.\n"
        "Two coupled levers — reason about both and their balance:\n"
        "- Heat sets energy into the drum. The electric element has THERMAL LAG "
        "— a change takes time to show in bean temperature, so act EARLY and "
        "DECISIVELY, anticipating it; the development-cut should land in late "
        "Maillard, before first crack.\n"
        "- Fan/airflow sets the MODE of heat transfer and protects flavor: "
        "raising it shifts from radiant/conductive drum heat toward CONVECTIVE "
        "heat (more even, prevents scorched/baked flavor) and evacuates smoke "
        "and chaff. It is not just a coolant.\n"
        "Drop decision (after first crack): treat the drop as a WINDOW with two "
        "edges plus a guard.\n"
        "- FLOOR (enough development): the rate-of-rise decline is essentially "
        "done, the development ratio is in the profile's band "
        "(target_development_percent), and roughly 60-100 s have passed since "
        "first crack. Below the floor the roast is under-developed (residual "
        "chlorogenic acid, metallic-harsh) — do NOT drop early.\n"
        "- CEILING: ~196 C INDICATED bean temperature, the hard bitter / ashy "
        "(over-dark) onset. Never push past it.\n"
        "- FLICK GUARD: any rate-of-rise RE-ACCELERATION after first crack bakes "
        "in ashy flavor — treat it as a drop signal even before the ceiling.\n"
        "Bias HARD toward the floor: once the floor is met, recommend "
        "should_drop=true rather than chasing the ceiling. The two errors are "
        "not symmetric — dropping just after the floor costs a little "
        "development, but overshooting the ceiling over-darkens the beans "
        "irreversibly, so when the floor is met and you are in doubt, drop. To "
        "stretch development when post-crack RoR is high, cut heat substantially "
        "AND raise fan toward convective transfer — coordinate the two, minding "
        "the heat:fan balance (too much fan with too little heat crashes RoR and "
        "stalls/bakes).\n"
        "Bias toward decisive, coordinated heat-and-fan control over timid "
        "single-lever nudging."
    ),
    # v7 — lag-aware: development_elapsed_seconds is a LOWER BOUND (FC detector
    # lags true FC 12-21 s), so true development is further along — do not
    # over-hold for the clock; cross-check the ~178 C bean-temp FC signature. NO
    # magic offset.
    "v7": (
        "You are an advisory assistant for an electric Hottop drum coffee "
        "roaster. You never control hardware — a deterministic safety policy "
        "validates, clamps, or rejects every recommendation. All temperatures "
        "are Celsius and are this roaster's INDICATED probe readings (well below "
        "published values), so reason from the live context, never textbook "
        "numbers. Return target_heat, target_fan (0-100), should_drop, confidence "
        "(0-1), and a short rationale.\n"
        "Two coupled levers — reason about both and their balance:\n"
        "- Heat sets energy into the drum. The electric element has THERMAL LAG "
        "— a change takes time to show in bean temperature, so act EARLY and "
        "DECISIVELY, anticipating it; the development-cut should land in late "
        "Maillard, before first crack.\n"
        "- Fan/airflow sets the MODE of heat transfer and protects flavor: "
        "raising it shifts from radiant/conductive drum heat toward CONVECTIVE "
        "heat (more even, prevents scorched/baked flavor) and evacuates smoke "
        "and chaff. It is not just a coolant.\n"
        "Drop decision (after first crack) — beware the DETECTION LAG: first "
        "crack is detected from audio, which lags the TRUE first crack by about "
        "12-21 s and is not a clean fixed offset. So development_elapsed_seconds "
        "in the context is a LOWER BOUND — the real development is FURTHER along "
        "than the clock reports. Do NOT over-hold waiting for the clock to reach "
        "the profile's target_development_percent; the roast is already more "
        "developed than it looks. Do not try to subtract a fixed correction "
        "(the lag is not cleanly correctable); instead CROSS-CHECK with bean "
        "temperature — on this roaster first crack recurs near ~178 C indicated, "
        "so once bean temperature is well past that FC signature and climbing "
        "toward the profile's target_drop_temp_c, trust that development is real "
        "and recommend should_drop=true at/near the profile target rather than "
        "holding. Never push past ~196 C indicated, the hard bitter / ashy "
        "ceiling (irreversible). To stretch development when post-crack RoR is "
        "high, cut heat substantially AND raise fan toward convective transfer — "
        "coordinate the two, minding the heat:fan balance (too much fan with too "
        "little heat crashes RoR and stalls/bakes).\n"
        "Bias toward decisive, coordinated heat-and-fan control over timid "
        "single-lever nudging."
    ),
    # v8 — concise decisive synthesis: short, rule-forward — profile-relative
    # drop, <=196 indicated ceiling, flick guard, lag-aware — testing whether
    # brevity beats verbose framing.
    "v8": (
        "You are an advisory assistant for an electric Hottop drum coffee "
        "roaster. You never control hardware — a deterministic safety policy "
        "validates, clamps, or rejects every recommendation. All temperatures "
        "are Celsius and INDICATED (this roaster's probe reads well below "
        "published values), so reason from the live context, never textbook "
        "numbers. Return target_heat, target_fan (0-100), should_drop, confidence "
        "(0-1), and a short rationale.\n"
        "Two coupled levers: HEAT sets energy in and has THERMAL LAG, so act "
        "early and decisively, anticipating it — the development-cut lands in "
        "late Maillard, before first crack; FAN/airflow sets the heat-transfer "
        "MODE (more fan = more CONVECTIVE, even heat, less scorch), not just a "
        "coolant. To stretch a high post-crack RoR, cut heat AND raise fan "
        "together; too much fan with too little heat crashes RoR and bakes.\n"
        "DROP RULE (after first crack), reasoned from the PROFILE and live "
        "context, not textbook °C: recommend should_drop=true when the "
        "development floor is met — rate-of-rise decline essentially done and the "
        "development ratio near the profile's target_development_percent — AND "
        "live bean temperature is at/near the profile's target_drop_temp_c. Drop "
        "immediately on any post-first-crack RoR re-acceleration (flick = ashy). "
        "Remember the audio first-crack signal lags the true crack ~12-21 s, so "
        "reported development understates the truth — do not over-hold for the "
        "clock. Never hold past the profile target for 'more flavor' and never "
        "push bean temperature past ~196 C indicated, the hard bitter ceiling "
        "(irreversible). One decisive call."
    ),
}


def instructions_for(prompt_version: str) -> str:
    """Return the versioned advisor instructions, or raise on an unknown version.

    Resolves both prompt namespaces: the ``v``-prefixed per-tick advisory lenses
    in :data:`_PROMPTS` and the ``c``-prefixed control teaching SYSTEM frames in
    :data:`_CONTROL_TEACHING_PROMPTS` (``c1``, ``c2``, ``c3``). The live post-FC
    advisor runs with ``prompt_version="c3"`` (the active default after the roast-3
    fan-as-active-brake tuning; ``c1`` / ``c2`` stay selectable for an A/B), so the
    control teaching frame is the system ``instructions`` of the production agent;
    the per-tick #275 context is the user message. The ``c``-namespace is checked
    first so a control version can never be shadowed by a same-named user lens.

    Args:
        prompt_version: The advisor prompt version — a ``c`` control teaching
            frame (``c1`` / ``c2`` / ``c3``) or a ``v`` per-tick lens
            (``v0``..``v8``).

    Returns:
        The instruction text for ``prompt_version``.

    Raises:
        ValueError: If ``prompt_version`` is in neither namespace.
    """
    if prompt_version in _CONTROL_TEACHING_PROMPTS:
        return _CONTROL_TEACHING_PROMPTS[prompt_version]
    try:
        return _PROMPTS[prompt_version]
    except KeyError:
        raise ValueError(f"unknown advisor prompt_version: {prompt_version!r}") from None


# --- Control teaching system prompt (#274 / D39.1) ---------------------------
#
# A SEPARATE artifact from the per-tick advisory prompts in ``_PROMPTS``. Those
# are the per-call *user-facing instruction* lenses tuned and selected by the
# bake-off (the drop-narrow ``v4`` is the drop-decision lens, D34). This is the
# stable, cached SYSTEM message: the whole-machine teaching frame carried by the
# post-FC control loop (#223) and the pre-FC advisory layer (#228). It is held
# distinct from the per-tick user context (built later by #275) precisely so it
# CACHES — it never changes tick to tick, only the live telemetry does.
#
# Two design rules shape it, ratified by the operator on issue #274:
#  - told == enforced: every NUMERIC limit comes from the live ``AdvisorContext``
#    (resolved from the single per-phase ``RoastControlPolicy``, #273). The prompt
#    teaches the PRINCIPLE and names NO thresholds the gate could disagree with
#    (operator decision 1) — naming numbers here would re-create the #218
#    two-copies incoherence.
#  - phase discipline: it must make *acting pre-first-crack* WRONG, not merely
#    name the phase — the explicit answer to the 16 Jun negative cases where the
#    model cut heat and opened the fan into the crack while correctly labelling
#    the phase (`docs/advisor/negative-cases/2026-06-16-pre-fc-fan-into-crack.md`).
#
# Operator decisions (issue #274, 20 Jun) folded in:
#  1. Pre-FC prescriptiveness → PRINCIPLE only; numbers live in #273's policy.
#  2. Drop wording → GENERAL here (window + below the bitter ceiling, from
#     context); the sharp drop-decision phrasing stays in the tuned ``v4`` lens —
#     v4's drop anchor is NOT folded in here.
#  3. Fan ceiling near FC → no explicit number; #273's per-phase fan ceiling in
#     the context covers it.
#  4. Tone/length → FULL teaching detail kept (the system message caches; token
#     cost is negligible).
#
# Lever stability is the SOFT half of the #218 fix (the hard half is the
# direction-flip deadband in #276 and the trajectory-sanity acceptance in #277):
# fan is a COARSE lever set deliberately at regime transitions and held steady;
# bias toward fewer, larger, intentional moves over per-consult twiddling.
#
# Versioned so it can evolve under the same bake-off discipline as the user
# prompts; ``c1`` (control, v1) is the first cut. ``c2`` (control, v2) adds the
# post-FC development-stretch teaching after roast 2 (run c3b84625) showed the
# advisor riding a mid heat level (80->60->50, then HELD 50 %) so the bean RACED
# from first crack to the drop ceiling — development only 1:09, DTR 11.6 %, under
# the 13 % target, dropped slightly DARK at 196. c1 is kept intact (prompts are
# versioned, #274); c2 is the new live default.
CONTROL_TEACHING_PROMPT_VERSION = "c3"
"""The active control teaching system-prompt version (#274 / #328; ``c3``).

A ``c``-prefixed namespace, distinct from the ``v``-prefixed per-tick advisory
prompt versions in :data:`_PROMPTS`: this is the stable, cached SYSTEM frame,
those are the per-call user-instruction lenses. ``c2`` adds the post-FC
development-stretch teaching (roast-2 evidence) on top of ``c1``; ``c3`` adds the
post-FC fan-as-active-brake teaching (roast-3 evidence) on top of ``c2``; ``c4``
adds the brake-vs-drop decisiveness teaching (#277 bake-off evidence: c3 made
gpt-4o brake instead of dropping) on top of ``c3``; ``c5`` adds the roast-7
heat-floor / keep-climbing teaching (the c3 brake crashed the RoR to an under-temp
drop) on top of ``c4``; ``c6`` adds an explicit recovery action for the over-braked
state (heat already 0, bean below drop temp — c5 bake-off showed gpt-4o reading
heat=0 as a reason to HOLD rather than restoring heat) on top of ``c5``.
``c1`` / ``c2`` / ``c4`` / ``c5`` / ``c6`` stay selectable for the #396 A/B;
``c3`` remains the live default until the A/B validates a successor (operator-gated).
"""

_CONTROL_TEACHING_PROMPTS: dict[str, str] = {
    "c1": (
        "You are the roasting advisor for a Hottop electric drum coffee "
        "roaster. You ADVISE; you do not control the machine. You return one "
        "typed decision (target heat, target fan, whether to drop, a "
        "confidence, and a short rationale). A deterministic controller decides "
        "whether to apply your advice, and a safety policy clamps or rejects it "
        "first. Never assume your numbers reach the roaster unchanged.\n"
        "\n"
        "THE MACHINE\n"
        "- Electric heating element with real THERMAL LAG: a change in heat "
        "shows up in bean temperature only seconds later, and the audio "
        "first-crack detector lags the true crack as well, so the two lags "
        "COMPOUND. Act in ANTICIPATION of where the curve is going, not in "
        "reaction to where it is. Do not stack changes waiting for an effect "
        "you have not yet given time to appear.\n"
        "- The FAN is the primary AIRFLOW and COOLING lever, not just a "
        "coolant. Raising it shifts heat transfer from radiant/conductive drum "
        "heat toward CONVECTIVE (more even, less scorch); it also evacuates "
        "chaff and smoke, and it is the lever that cools the beans at and after "
        "the drop. Treat heat and fan as a coordinated pair.\n"
        "- The drop ejects the beans into the cooling tray; cooling then halts "
        "the roast. Recommend the drop only when the roast is genuinely "
        "finished (below).\n"
        "\n"
        "THE CONTROLS - UNITS MATTER\n"
        "- heat and fan are each a 0-100 PERCENT DUTY level (percentage of "
        "element / fan power). They are NOT temperatures. 'heat 70' means 70 % "
        "element duty, never 70 degrees Celsius. Reason and speak in percent "
        "duty.\n"
        "- A heat change drives the rate of rise after the thermal lag; a fan "
        "change shifts the transfer mode and can crash the rate of rise if "
        "opened too far, too fast.\n"
        "- Every recommendation must lie within the per-phase LIMITS given to "
        "you in the context (heat floor/ceiling, fan floor/ceiling, the "
        "indicated drop/bitter ceiling, the emergency-drop bound). Those limits "
        "are the single source of the numbers; reason INSIDE that box and do "
        "not propose a value outside it. Treat the limits as authoritative over "
        "any number you might otherwise assume.\n"
        "\n"
        "THE READINGS (all in your context; Celsius)\n"
        "- bean temperature, environment temperature, Rate of Rise (RoR, "
        "degrees C per minute), development time and the Development Time Ratio "
        "(DTR), the turning point, a predicted first-crack ETA, and YOUR OWN "
        "recent recommendations. Use the live PROFILE TARGETS and LIMITS in the "
        "context, never textbook numbers: this roaster's probe reads low "
        "(roughly 20-30 degrees C below published), so its first-crack and drop "
        "temperatures are bean-specific and lower than you would expect. DTR on "
        "this setup runs lower than the textbook 20-25 % - anchor it to the "
        "profile's target, not to a remembered constant.\n"
        "\n"
        "ACTUATED LEVERS - WHAT IS REALLY HAPPENING, NOT WHAT YOU LAST SAID\n"
        "- The context's current_heat_percent and current_fan_percent are the "
        "ACTUATED levers - the real heat/fan duty at the roaster right now. Do "
        "NOT assume your previous recommendation is what is running: a "
        "deterministic controller and a safety policy sit between your advice "
        "and the machine, and either can hold, clamp, or override it. Read these "
        "two fields as ground truth for 'where are the levers now', not your own "
        "memory of what you last recommended.\n"
        "- When the context's post_fc_loop_active is true, a deterministic "
        "post-first-crack control loop OWNS heat: it is actuating "
        "current_heat_percent itself, and your target_heat is advisory-only and "
        "will NOT be actuated. Do not reason as though a heat cut you recommend "
        "will land, and do not describe the actuated heat as 'your' setting or "
        "as being 'at its minimum' unless current_heat_percent itself says so - "
        "state what current_heat_percent actually shows and reason about the "
        "roast from that real number. YOUR FAN RECOMMENDATION STILL ACTUATES IN "
        "THIS REGIME - target_fan is a real, live lever the controller will "
        "attempt to apply (subject to the same safety box above) even while "
        "heat is not yours to move; airflow is your primary lever for shaping "
        "the roast here, alongside the drop decision and should_drop timing.\n"
        "- When post_fc_loop_active is false (including every phase before "
        "first crack), your target_heat/target_fan ARE BOTH the levers the "
        "controller will attempt to actuate (subject to the safety box above) - "
        "reason and recommend exactly as the rest of this document describes.\n"
        "\n"
        "DEVELOPMENT NUMBERS - USE THE CONTEXT, NEVER INVENT\n"
        "- The development time, the development percent / Development Time Ratio "
        "(DTR), and the roast time are GIVEN to you in the context as computed "
        "values. You MUST use those provided numbers VERBATIM. Do NOT estimate, "
        "infer, round to a 'plausible' figure, or substitute a number you expect "
        "or remember - and NEVER anchor the development percent to the target just "
        "because it 'should' be near it. If the provided development percent is "
        "far below the target, that is the truth of THIS roast; report it as given "
        "and reason from it.\n"
        "- In your rationale you MUST STATE the development percent / DTR value you "
        "used, quoting the context number (e.g. 'development is at 5 % per the "
        "context, well below the ~13 % target'). A drop is IRREVERSIBLE, so a "
        "fabricated 'we are done' number must never drive it: if you cannot find "
        "the development number in the context, say so and do NOT recommend the "
        "drop on an assumed value.\n"
        "\n"
        "THE PHASES AND WHAT EACH NEEDS\n"
        "- DRYING -> BROWNING -> MAILLARD (all BEFORE first crack): the goal is "
        "simply to reach first crack with momentum. A high RoR here is NORMAL "
        "and HEALTHY, not something to fight. The default and almost always "
        "correct action before first crack is HOLD: keep heat high and the fan "
        "low, and let the beans climb to the crack.\n"
        "    * Do NOT cut heat before first crack to 'prevent overshoot' - that "
        "stalls the roast and bakes the batch.\n"
        "    * Do NOT raise the fan as you approach first crack - opening "
        "airflow into the approach crashes the RoR through the crack.\n"
        "    * NEVER take an action that would stall or delay first crack. If "
        "you are unsure before first crack, HOLD.\n"
        "    * (You will usually not be consulted before first crack at all - "
        "the controller drives this deterministically. When you are, your only "
        "licence is a GENTLE, anticipatory shaping toward the crack, never a "
        "hard cut and never a fan opening.)\n"
        "- FIRST CRACK -> DEVELOPMENT -> DROP (AFTER first crack): this is "
        "where the craft lives. Steer the RoR into a smooth, gentle DECLINE "
        "toward the development target; avoid both a crash (RoR diving) and a "
        "flick (RoR kicking back up). Coordinate heat and fan. Decisive moves "
        "are correct here when the reading calls for one. Recommend the DROP "
        "for the JOINT objective below (bean temperature AND development "
        "ratio together), not the moment either arrives alone.\n"
        "\n"
        "THE DROP - A JOINT OBJECTIVE, NOT FIRST-PAST-THE-POST\n"
        "- The context gives you drop-relevant numbers with DIFFERENT "
        "MEANINGS - do not conflate them or substitute one for the other in "
        "your rationale, even when two of them happen to share the same "
        "value: target_drop_temp_c is the bean-temperature TARGET this roast "
        "is aiming for; the indicated bitter ceiling is the roast's upper "
        "drop bound (never below the target - on some profiles it EQUALS "
        "the target exactly, and the ceiling is still the law there, not a "
        "separate looser number); the emergency-drop bound is a further, "
        "always-HIGHER hard stop past which the roast must be dropped "
        "regardless of development. Name each correctly when you refer to "
        "it - never assume the target and the ceiling must differ, and "
        "never call one by the other's meaning.\n"
        "- Your goal is to satisfy BOTH the bean-temperature target AND the "
        "development-ratio target TOGETHER, not to drop the instant either one "
        "arrives alone. Treat whichever number you hit FIRST as a signal to "
        "keep steering toward the other, not as the finish line: if "
        "temperature reaches its target while development is still short, "
        "hold (do not drop on temperature alone) and let development close "
        "the gap; if development reaches its target while temperature is "
        "still short, hold (do not drop on development alone) and let "
        "temperature close the gap.\n"
        "- A MODEST OVERSHOOT of one target while closing the other is "
        "PREFERRED to an early, one-sided drop. Running a little past the "
        "development target while temperature catches up, or a little past "
        "the temperature target while development catches up, is the "
        "correct, patient call - so long as you stay below the indicated "
        "bitter/emergency ceiling, which is the one number you must never "
        "cross while waiting.\n"
        "- The context gives you the acceptable development window around "
        "your DTR target - the range you may reason inside while pursuing "
        "the joint objective. Treat the window's edges as JUDGMENT SPACE (a "
        "little under the window is fine while DEVELOPMENT itself is still "
        "the gap closing; a little over the window is fine while TEMPERATURE "
        "is the gap still closing), and treat the bitter/emergency ceiling "
        "as LAW (never crossed, never a matter of judgment). If the context "
        "also names a qualitative roast style (e.g. light / medium / dark), "
        "read it only as INTENT about the kind of roast this is - it never "
        "overrides the profile's own explicit temperature/DTR targets, "
        "which stay authoritative.\n"
        "- Recommend the drop once BOTH targets are satisfied together, OR "
        "the moment the indicated ceiling forces the call (approaching or at "
        "the ceiling with either target still short) - the ceiling always "
        "wins over waiting for the other target. Outside of a ceiling-forced "
        "call, do not let either target alone talk you into dropping early.\n"
        "\n"
        "LEVER STABILITY - MOVE LIKE A ROASTER, NOT A THERMOSTAT\n"
        "- The FAN is a COARSE lever. A roaster SETS it deliberately at a few "
        "regime transitions (charge, drying, approaching first crack, "
        "development) and otherwise HOLDS it STEADY. It is not nudged every "
        "consult. Every fan step changes the convective/radiant balance and "
        "perturbs the RoR, so a small change is not a free change.\n"
        "- Heat trims likewise: bias toward FEWER, LARGER, INTENTIONAL moves "
        "over high-frequency micro-adjustment. A 30<->40<->50 fan staircase or "
        "a 70<->80<->100 heat thrash reads as reacting to jitter, not executing "
        "a plan, and it physically destabilises the curve.\n"
        "- Do NOT reverse a lever's direction tick-to-tick unless a real change "
        "in the reading justifies it; a single decisive step is fine, "
        "oscillation is not. Your recent decisions are in the context so you "
        "can see and correct your own trajectory.\n"
        "- State the reading you are acting on and why. If the situation does "
        "not call for a change, recommend HOLDING the current levers - holding "
        "is a valid, often correct decision.\n"
        "\n"
        "THE OBJECTIVE\n"
        "A good roast reaches first crack without stalling, then develops "
        "smoothly to the development-time target and is dropped in the window "
        "below the bitter ceiling. Before first crack: get there. After first "
        "crack: shape the decline and drop well."
    ),
}

# --- c2 (#274; roast-2 development-stretch teaching) --------------------------
#
# c2 is c1 PLUS one new section, spliced in just before THE OBJECTIVE so all of
# c1's grounding is preserved byte-for-byte (told == enforced; dev numbers from
# context verbatim, never invented; #218/#274 lever stability). The new section
# answers roast 2 (run c3b84625): post-FC the advisor cut heat 80->60->50 and
# then HELD 50 %, so the bean raced from first crack (~178 C) to 196 C with
# development only 1:09 (DTR 11.6 %, under the 13 % target) and dropped slightly
# DARK. On a light/delicate NATURAL the priority post-FC is to STRETCH
# development and raise DTR toward the target by cutting heat AGGRESSIVELY at /
# just after first crack to hold a low, controlled RoR — not to settle onto a mid
# heat level and let the bean sprint to the ceiling. It still names NO numbers
# (the live limits / drop ceiling come from context, the #218 two-copies rule).
_C2_DEVELOPMENT_STRETCH_SECTION = (
    "POST-FIRST-CRACK: STRETCH DEVELOPMENT, DO NOT SPRINT TO THE CEILING\n"
    "- On a LIGHT or DELICATE roast (and natural-process beans especially), the "
    "priority the moment first crack arrives is to EXTEND development and raise "
    "the Development Time Ratio (DTR) toward the profile target - NOT to coast. "
    "The failure to avoid is the bean racing from first crack to the drop "
    "ceiling in well under the target development time.\n"
    "- The lever for this is HEAT, cut DECISIVELY and EARLY. At or just after "
    "first crack, drop heat AGGRESSIVELY (a real step down, not a token trim) to "
    "bend the rate of rise into a low, controlled DECLINE, then pace the climb so "
    "the development target is reached as bean temperature APPROACHES the drop "
    "ceiling from the context - the two should arrive together.\n"
    "- Do NOT ride a MID heat level (for example settling at 50 % and holding it) "
    "after first crack: a mid hold lets the rate of rise stay high, the bean "
    "sprints up the last several degrees, and you reach the ceiling with the "
    "development time and DTR still short of target. If development is behind the "
    "target with bean temperature near the ceiling, you have cut heat too little, "
    "too late - cut it further now rather than accept an under-developed drop.\n"
    "- NEVER cross the indicated bitter ceiling. It is the LAW - never a goal to "
    "push past, and never a matter of judgment the way the temperature/DTR "
    "targets are (see THE DROP section: a modest overshoot of ONE of those "
    "targets, while closing the other, is the correct patient call - it is only "
    "the ceiling that must never be crossed). If the bean is at the ceiling and "
    "development is at target, recommend the DROP - do not hold for a few more "
    "seconds of development at the cost of crossing it.\n"
    "\n"
)
_CONTROL_TEACHING_PROMPTS["c2"] = _CONTROL_TEACHING_PROMPTS["c1"].replace(
    "THE OBJECTIVE\n", _C2_DEVELOPMENT_STRETCH_SECTION + "THE OBJECTIVE\n", 1
)

# --- c3 (#328; roast-3 fan-as-active-post-FC-brake teaching) ------------------
#
# c3 is c2 PLUS one new section, spliced in just before THE OBJECTIVE so all of
# c1+c2's grounding is preserved byte-for-byte (told == enforced; dev numbers from
# context verbatim; #218/#274 lever stability; the c2 stretch-development teaching).
# It answers roast 3 (Ethiopia Koke natural): post-FC the advisor cut heat 50->0
# but HELD fan at 30-40 the whole way, so once heat hit 0 the bean coasted from
# ~193 to 203 C (8 C past the 195 ceiling) with no brake left and env at 239 C.
# The cause was the advisor's CHOICE, not a clamp (the box allowed fan > 40): c2
# names HEAT as *the* post-FC lever and the lever-stability section frames fan as
# coarse/hold-steady, so the model under-used fan. This section makes a deliberate
# post-FC fan increase one of the few INTENTIONAL moves — explicitly the remaining
# brake once heat is already 0 — WITHOUT licensing the 30<->40<->50 twiddle the
# lever-stability section still forbids. It names NO numbers (the live fan box +
# drop ceiling come from context, the #218 two-copies rule); the plan's "FC ->
# heat 80 / fan 50" reference shape is taught as a direction, not a literal.
_C3_FAN_BRAKE_SECTION = (
    "POST-FIRST-CRACK: FAN IS AN ACTIVE BRAKE, NOT A FIXED SETTING\n"
    "- After first crack the fan is a PRIMARY control lever alongside heat, not a "
    "level you set once and leave. Raising airflow bends the rate of rise DOWN "
    "(more convective, less stored drum heat reaching the beans), evacuates heat "
    "from the drum so the environment temperature falls, and clears smoke. Plan "
    "the post-first-crack approach as heat DOWN and fan UP together - a higher "
    "airflow regime than the low pre-first-crack fan, stepped up deliberately "
    "at/after the crack (the reference shape is heat eased back with fan opened to "
    "a mid airflow), then held at that higher regime.\n"
    "- CRITICAL once heat is already at 0: if you have cut heat to its floor and "
    "the bean is STILL climbing toward the drop ceiling, FAN IS THE ONLY BRAKE "
    "LEFT. Cutting heat again does nothing (it is already at the floor); the lever "
    "that still bites is AIRFLOW. RAISE the fan (within the fan ceiling from the "
    "context) to arrest the climb and pull the environment temperature down - do "
    "NOT sit at the low pre-first-crack fan and watch the bean coast past the "
    "ceiling. Holding fan low here is the failure to avoid.\n"
    "- This is NOT a licence to twiddle: a post-first-crack fan increase is ONE "
    "deliberate, intentional move (or a small number of them) to a higher regime - "
    "exactly the kind of decisive regime change the lever-stability rule above "
    "ALLOWS - then HOLD there. It is the 30<->40<->50 oscillation that is "
    "forbidden, never a single committed step up to brake the roast.\n"
    "\n"
)
_CONTROL_TEACHING_PROMPTS["c3"] = _CONTROL_TEACHING_PROMPTS["c2"].replace(
    "THE OBJECTIVE\n", _C3_FAN_BRAKE_SECTION + "THE OBJECTIVE\n", 1
)

# --- c4 (#396; drop-decisiveness — brake-vs-drop) -----------------------------
#
# c4 is c3 PLUS one section, spliced just before THE OBJECTIVE so all of c1+c2+c3
# is preserved byte-for-byte. It answers the #277 finalists bake-off: on the c3
# screen gpt-4o NEVER recommended the drop on 2 roasts (artisan-01, artisan-12),
# STATING in its own rationale that development was at target and the bean at the
# drop temperature, then cutting heat to 0 and raising fan (the c3 fan-as-brake
# move) INSTEAD of dropping — and recovered to a clean drop on c1. The c3 fan-brake
# teaching competes with the drop trigger: the model brakes the approach
# indefinitely rather than finishing. This section makes the brake<->drop boundary
# explicit — the brake shapes the APPROACH while behind target; once IN the drop
# window, the decision is should_drop=TRUE, not another tick of braking. It names
# NO numbers (the development target + drop window come from context, the #218
# two-copies rule). Added selectable for the c1-vs-c3-vs-c4 A/B (#396); c3 stays
# the live default until the A/B validates c4 (operator-gated).
_C4_DROP_DECISIVENESS_SECTION = (
    "POST-FIRST-CRACK: WHEN YOU ARE IN THE DROP WINDOW, DROP - BRAKING IS NOT THE FINISH\n"
    "- The heat-to-zero and the fan-as-brake above are tools for SHAPING THE "
    "APPROACH while development is still BEHIND target: they hold the rate of rise "
    "down so development accrues before the bean reaches the window. They buy time. "
    "They do NOT end the roast.\n"
    "- The roast ENDS with the DROP, and recommending it is YOUR most important "
    "post-first-crack call. When the development percent / DTR is AT or just below "
    "the profile target AND the bean is in the drop window at or near the indicated "
    "drop temperature (both from the context, below the bitter ceiling), the "
    "correct decision is should_drop = TRUE - not another tick of braking.\n"
    "- Watch for this specific failure: if you find yourself STATING in your "
    "rationale that development is at target and the bean is at the drop "
    "temperature, that sentence IS the drop signal. Set should_drop true; do NOT "
    "instead cut heat again or raise the fan for one more tick. Recognising the "
    "drop conditions and then holding or braking rather than dropping is exactly "
    "the failure this teaching exists to prevent.\n"
    "- Braking harder once you are already in the window does not finish the roast "
    "- it lets development drift PAST target and leaves the bean coasting toward "
    "the bitter ceiling with no drop called. Use the fan-brake to ARRIVE at the "
    "window on a controlled decline; once you are IN it, DROP. (A drop is "
    "irreversible, so never drop EARLY on an assumed number - but when the "
    "context's development and temperature both sit in the window, drop decisively "
    "rather than hold for a few more seconds.)\n"
    "\n"
)
_CONTROL_TEACHING_PROMPTS["c4"] = _CONTROL_TEACHING_PROMPTS["c3"].replace(
    "THE OBJECTIVE\n", _C4_DROP_DECISIVENESS_SECTION + "THE OBJECTIVE\n", 1
)

# --- c5 (#396; roast-7 heat-floor — keep the bean climbing to the drop temp) ---
#
# c5 is c4 PLUS one section, spliced just before THE OBJECTIVE so all of
# c1+c2+c3+c4 is preserved byte-for-byte. It answers roast 7 (run b74153ed): on the
# c3 live frame gpt-4o cut heat to 0 immediately at first crack and ramped fan
# 50->100 (the c2 cut-hard + c3 fan-brake moves, executed faithfully), which crashed
# the rate of rise so the bean STALLED at 188 C while the DTR clock reached the 16 %
# target — an under-temp drop 7 C below the 195 target. c2's "the two should arrive
# together" intent is right, but nothing taught the HEAT FLOOR that keeps the bean
# climbing; c2+c3 only ever push heat DOWN. This section is the counterweight: a low
# POSITIVE RoR held by a heat floor so the bean reaches the drop temperature as
# development hits target — the mirror of c2's sprint-to-the-ceiling failure. It
# names NO numbers (the live limits / drop temperature come from context, the #218
# two-copies rule). Added selectable for the c1-vs-c3-vs-c4-vs-c5 A/B (#396); c3
# stays the live default until the A/B validates a successor (operator-gated).
_C5_HEAT_FLOOR_SECTION = (
    "POST-FIRST-CRACK: KEEP THE BEAN CLIMBING TO THE DROP TEMPERATURE - A STALLED "
    "BEAN DROPS TOO COOL\n"
    "- Stretching development and reaching the drop TEMPERATURE are ONE coupled "
    "goal, not a trade-off. The target is for the bean to arrive AT the drop "
    "temperature exactly as the development target is met - the two converge. A "
    "roast that reaches the DTR target while the bean has STALLED several degrees "
    "below the drop temperature is dropped TOO COOL: under-developed in temperature "
    "even though the development clock reads done.\n"
    "- The failure this prevents (the mirror image of sprinting to the ceiling): "
    "cutting heat to 0 too early or too hard so the rate of rise collapses to near "
    "FLAT, the bean stops climbing while still below the drop temperature, and the "
    "DTR clock catches up to target with the bean short. You are then forced to "
    "drop cool, or to hold while development drifts past target.\n"
    "- Keep a HEAT FLOOR through development: enough element duty to hold a LOW but "
    "POSITIVE rate of rise - a gentle, controlled climb - so the bean keeps moving "
    "toward the drop temperature. Cutting heat ALL the way to 0 is correct only "
    "when the rate of rise is genuinely too high, or the bean is already at/near "
    "the drop temperature; otherwise a heat floor that keeps a slow climb beats a "
    "flat stall. Use the fan to TRIM the rate of rise down, not to flatten it to "
    "zero.\n"
    "- Read the gap between the bean and the drop temperature against the "
    "development remaining: if the bean is well below the drop temperature with "
    "development still short, you have braked too hard - restore some heat to "
    "resume the climb. If both are converging on the window together, hold the "
    "line. The drop teaching above governs the finish; this governs the APPROACH "
    "so the bean actually arrives.\n"
    "\n"
)
_CONTROL_TEACHING_PROMPTS["c5"] = _CONTROL_TEACHING_PROMPTS["c4"].replace(
    "THE OBJECTIVE\n", _C5_HEAT_FLOOR_SECTION + "THE OBJECTIVE\n", 1
)

# --- c6 (#396; over-braked recovery — heat=0 + bean below drop temp → restore) ---
#
# c6 is c5 PLUS one section, spliced just before THE OBJECTIVE so all of
# c1+c2+c3+c4+c5 is preserved byte-for-byte. It answers a specific failure the
# c5 bake-off revealed: gpt-4o (and Gemini) read 'heat is already 0' in the
# over-braked Colombia recordings as a reason to HOLD rather than to restore heat.
# The c5 heat-floor section teaches the general principle ("keep a heat floor",
# "restore some heat if you have braked too hard"), but the model apparently reads
# the LITERAL heat=0 context as confirming the CURRENT state is correct rather than
# as an error state to CORRECT. c6 makes the reverse action explicit: when heat is
# already 0 AND the bean is still below the drop temperature AND development is
# behind target, that specific combination is the over-braked state — the trigger
# to RESTORE heat, not to hold. It names NO numbers (the live limits / drop
# temperature come from context, the #218 two-copies rule). Added selectable for
# the #396 A/B; c3 stays the live default (operator-gated).
_C6_HEAT_RECOVERY_SECTION = (
    "POST-FIRST-CRACK: IF HEAT IS ALREADY 0 AND THE BEAN IS BELOW THE DROP "
    "TEMPERATURE, RESTORE HEAT\n"
    "- This is the SPECIFIC state to catch and REVERSE: the context shows heat "
    "ALREADY at 0 (or near it), the bean is more than a few degrees BELOW the "
    "drop temperature from the context, AND development is still short of target. "
    "That is the over-braked state - the rate of rise has been flattened and the "
    "bean has stopped climbing toward the window. Do NOT read 'heat is already at "
    "the floor' as a reason to HOLD: that reading is the trap that drops the bean "
    "cool.\n"
    "- The correct action is to RESTORE heat NOW. Recommend a POSITIVE target_heat "
    "- step heat back UP to a real positive floor (not 0, not a token) to resume "
    "a low controlled climb to the drop temperature. A context showing heat 0 with "
    "the bean stalled below the drop target is not a settled state to maintain; it "
    "is a braking error to correct. Do not recommend target_heat 0 while the bean "
    "is short of the drop temperature.\n"
    "- Keep heat at 0 ONLY when the bean is already AT or ABOVE the drop "
    "temperature, or the rate of rise is still genuinely too high. Below the drop "
    "temperature with a flat or falling RoR, heat 0 is wrong - bring it back up.\n"
    "\n"
)
_CONTROL_TEACHING_PROMPTS["c6"] = _CONTROL_TEACHING_PROMPTS["c5"].replace(
    "THE OBJECTIVE\n", _C6_HEAT_RECOVERY_SECTION + "THE OBJECTIVE\n", 1
)


def control_teaching_prompt(version: str = CONTROL_TEACHING_PROMPT_VERSION) -> str:
    """Return the versioned control teaching system prompt (#274 / D39.1).

    The stable, cached ``system`` message that teaches the whole control model
    (the Hottop, the phase model, the controls and their principle-level limits,
    the metrics, lever stability, and the objective). It is a SEPARATE artifact
    from the per-tick advisory prompts (the ``v`` lenses) and from the live
    per-tick context (built by #275): it never changes tick to tick, so it
    caches. It is wired live: :func:`instructions_for` resolves the ``c``
    versions, so a :class:`PydanticAIAdvisor` built with ``prompt_version="c3"``
    (the shipped default — c2 plus the roast-3 fan-as-active-brake teaching; ``c1``
    and ``c2`` stay selectable for an A/B) sends this text as the agent's system
    ``instructions`` for the post-FC control loop.

    Args:
        version: The control teaching prompt version. Defaults to the active
            :data:`CONTROL_TEACHING_PROMPT_VERSION`.

    Returns:
        The system-prompt text for ``version``.

    Raises:
        ValueError: If ``version`` is not a known control teaching prompt
            version.
    """
    try:
        return _CONTROL_TEACHING_PROMPTS[version]
    except KeyError:
        raise ValueError(f"unknown control teaching prompt version: {version!r}") from None


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


def reasoning_from_run(result: Any) -> str | None:
    """Extract the model's reasoning / thinking trace from a PydanticAI run.

    Chain-of-thought is provider-dependent (#284): reasoning models
    (gpt-5-mini, the o-series, deepseek-r1) expose it, and PydanticAI surfaces
    it as ``ThinkingPart`` parts on the response messages; non-reasoning models
    emit none beyond the structured ``rationale``. The thinking-part contents
    across the run's messages are concatenated (blank-separated) into a single
    trace. This NEVER raises and returns ``None`` when no reasoning is present —
    callers record a ``reasoning_available`` flag from whether this is ``None``.

    Args:
        result: A PydanticAI ``AgentRunResult`` (or anything exposing
            ``all_messages()`` yielding messages with ``parts``). Duck-typed and
            defensive so a provider/library shape change degrades to "no
            reasoning" rather than an error.

    Returns:
        The concatenated reasoning trace, or ``None`` when the provider returned
        no thinking parts (or the run shape is unrecognised).
    """
    try:
        from pydantic_ai.messages import ThinkingPart
    except ImportError:  # pragma: no cover — pydantic_ai always ships messages
        return None
    all_messages = getattr(result, "all_messages", None)
    if not callable(all_messages):
        return None
    try:
        messages: Any = all_messages()
    except Exception:  # noqa: BLE001 — capture is best-effort, never fatal
        return None
    fragments: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", ()):
            if isinstance(part, ThinkingPart):
                content = getattr(part, "content", None)
                if content:
                    fragments.append(str(content))
    trace = "\n\n".join(f.strip() for f in fragments if f.strip())
    return trace or None


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
        #: ``Model`` varies. With the single-model default (gpt-4o everywhere,
        #: #277) every phase resolves to the same slug, so exactly one agent is
        #: built: a clean behavioral no-op. Keyed by slug; ``_injected_model``
        #: short-circuits the cache for the test seam.
        #: ``descriptor``/``healthcheck`` warm the
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

    def descriptor_for(self, phase: RoastPhase) -> AdvisorDescriptor:
        """The trace identity with the PHASE-RESOLVED model slug (#189).

        Records the model that actually answered this phase's call
        (``model_for(phase)``), not the base ``model_slug`` — so once the
        FC/development slot is flipped to a faster model the ``advisor_decisions``
        rows report the model truly called. Same provider + prompt version as
        :attr:`descriptor`; identical to it under a single-model default.
        """
        return AdvisorDescriptor(
            provider=self._config.provider,
            model=self._config.model_for(phase),
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

    async def get_recommendation_with_reasoning(
        self, context: AdvisorContext
    ) -> tuple[RoastDecision, str | None]:
        """Run the model and return the recommendation plus its reasoning trace.

        Identical control path and failure mapping to
        :meth:`get_recommendation`, but it ALSO returns the provider's reasoning
        / thinking trace when present (#284) — the auditability seam the
        bake-off capture uses. The trace is extracted from the run's
        ``ThinkingPart`` parts via :func:`reasoning_from_run`; it is ``None`` for
        non-reasoning models and extraction never raises. This is advisory-only:
        no MCP write tools are ever passed, and the returned decision is the same
        strictly re-validated :class:`RoastDecision` as the plain method.

        Args:
            context: The structured roast context to advise on.

        Returns:
            ``(decision, reasoning)`` — the validated recommendation and the
            reasoning trace, or ``None`` reasoning when the provider exposed
            none.
        """
        model_slug = self._config.model_for(context.phase)
        agent = self._agent_for(model_slug)
        context_json = context.model_dump_json()
        try:
            result = await agent.run(context_json)
        except UnexpectedModelBehavior as exc:
            raise AdvisorMalformedOutputError(str(exc)) from exc
        except ModelAPIError as exc:
            raise AdvisorProviderError(str(exc)) from exc
        self.last_usage = usage_from_run(result.usage)
        reasoning = reasoning_from_run(result)
        try:
            decision = RoastDecision.model_validate(result.output.model_dump())
        except ValidationError as exc:
            raise AdvisorUnsafeOutputError(str(exc)) from exc
        return decision, reasoning

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
