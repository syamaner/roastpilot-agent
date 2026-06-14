"""Typed application configuration (component plan §4; orchestration plan
§ Configuration Model).

Finalized at E2-S3. Controller timing defaults are the documented
hardware-aligned values from the orchestration plan; safety limits are
deliberately conservative software ceilings pending supervised hardware
validation at E12 (E12-S1).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import RoastPhase

# Phase-keyed advisor consult floors — advisor cadence scales with first-crack
# proximity (D32 / #191, refining #171). The advisor is consulted where it adds
# optimization judgment, which ramps toward FC:
#   - preheating: OFF — preheat is NOT an automatic-advice phase (see
#     ``_AUTO_ADVICE_PHASES`` in controller.py). Reaching the charge band is a
#     deterministic ramp; charge is an operator action; charge-guidance is
#     already emitted. (Also removes the #134 preheat error-spam surface.) A
#     manual operator request still works — it bypasses phase scoping.
#   - roasting pre-first-crack (beans charged → FC): NO fixed heartbeat
#     (``inf`` floor) — change-based triggers (temp/RoR delta) only, PLUS the
#     near-FC boost below so the anticipatory cut isn't missed if RoR flattens.
#   - development (first crack onward): 0 = unthrottled — consult again as soon
#     as the previous call returns; the practical floor is advisor latency.
# Change-based triggers (temp/RoR delta, phase change, manual) still fire sooner.
#
# ``None`` (not ``math.inf``) encodes "no heartbeat floor": this map is frozen
# into the run config via ``model_dump(mode="json")`` + ``json.dumps``, and a
# float ``inf`` serializes to a bare ``Infinity`` token that is invalid JSON
# (SQLite ``json_valid`` and strict/JS readers reject it). ``None`` round-trips
# as ``null``; the disabled-heartbeat behavior lives in ``advisory_interval_for``
# + the controller (PR #201 / Codex).
DEFAULT_ADVISORY_MIN_INTERVAL_SECONDS: dict[RoastPhase, float | None] = {
    RoastPhase.ROASTING_PRE_FIRST_CRACK: None,
    RoastPhase.DEVELOPMENT: 0.0,
}

# The advisor model — the Artisan-expanded bake-off winner (D33, 14 Jun 2026):
# ``google/gemini-3.1-flash-lite`` via OpenRouter. On 28 real quality-filtered
# Hottop roasts it was the ONLY model that reliably makes the flavor-critical
# drop call (drop F1 0.63, called the drop on 18/28 with the best heat-direction
# agreement 0.88 and the fastest latency ~1.2 s); every other candidate —
# including the prior incumbent ``anthropic/claude-opus-4.8`` (D20/D21) and all
# the frontier/slow models — essentially never calls the drop (over-holds past
# the ≤196 °C bitter ceiling, the dangerous direction). See
# ``docs/advisor/bakeoff-artisan-summary.md``. This is the base slug AND the
# default for every phase; the per-phase mechanism (#173) is retained so a
# future fast/slow split can flip a slot without a behavior change until then.
DEFAULT_ADVISOR_MODEL = "google/gemini-3.1-flash-lite"

# Phase-keyed advisor MODEL selection (#173, operator 13 Jun): the model slug
# the advisor uses, by agent phase. The MECHANISM only — every phase defaults
# to ``DEFAULT_ADVISOR_MODEL`` (gemini-3.1-flash-lite everywhere, D33), which is
# fast enough for every phase including the tight FC gate, so there is no
# per-phase split today. The map is kept so a future re-run can flip a slot
# (e.g. a more capable preheat/pre-FC model) and record it as a new D-number. A
# phase absent from the map falls back to ``model_slug``.
DEFAULT_ADVISOR_MODEL_BY_PHASE: dict[RoastPhase, str] = {
    RoastPhase.PREHEATING: DEFAULT_ADVISOR_MODEL,
    RoastPhase.ROASTING_PRE_FIRST_CRACK: DEFAULT_ADVISOR_MODEL,
    RoastPhase.DEVELOPMENT: DEFAULT_ADVISOR_MODEL,
}


class ControllerConfig(BaseModel):
    """Controller timing and advisory-call thresholds.

    Defaults per orchestration plan § Configuration Model: the 1.0 s tick is
    set by the Hottop's K-type thermocouple response characteristics
    (§ Hardware Characteristics — sensors update at ~1 Hz; faster polling
    reads unchanged values).
    """

    tick_interval_seconds: float = Field(default=1.0, gt=0)
    advisory_min_temp_delta_c: float = Field(default=1.0, gt=0)
    advisory_min_ror_delta_c_per_min: float = Field(default=2.0, gt=0)
    # Phase-keyed consult floors (#171): seconds between automatic consults,
    # by agent phase. A phase absent from the map (or mapped to 0) is
    # unthrottled — the heartbeat never gates it; change-based triggers and
    # the advisor's own latency are the only limiter. Defaults: preheat 30 s,
    # charged/pre-FC 10 s, development (FC onward) 0 = unthrottled. Values are
    # ``ge=0`` (0 = unthrottled; a negative floor would be meaningless).
    #
    # NOTE (#171): this replaced the prior scalar ``float`` (15 s). An env var
    # of the old shape — ``ROASTPILOT_CONTROLLER__ADVISORY_MIN_INTERVAL_SECONDS=15``
    # — no longer coerces; supply per-phase values keyed by ``RoastPhase``
    # value (e.g. ``{"preheating": 30, "roasting_pre_first_crack": 10}``).
    advisory_min_interval_seconds: dict[RoastPhase, Annotated[float, Field(ge=0)] | None] = Field(
        default_factory=lambda: dict(DEFAULT_ADVISORY_MIN_INTERVAL_SECONDS)
    )
    # Near-FC cadence boost (D32 / #191): the Maillard-approach is the advisor's
    # highest-value window (the anticipatory heat cut that must precede FC, where
    # thermal + ~12–21 s detector lag compound). Since pre-first-crack has no
    # fixed heartbeat (change-based only), guarantee a heartbeat once the bean is
    # near the FC band so the pre-emptive cut isn't missed if RoR flattens into
    # the crack. ``advisory_near_fc_bean_temp_c`` is the approach threshold
    # (default 170 °C — within a few °C of the operator's empirical ~178 °C FC on
    # this probe; roaster/probe-specific, hence tunable); above it, in
    # pre-first-crack, the consult floor becomes ``advisory_near_fc_interval_seconds``.
    advisory_near_fc_bean_temp_c: float = Field(default=170.0, gt=0)
    advisory_near_fc_interval_seconds: float = Field(default=10.0, gt=0)
    advisory_timeout_seconds: float = Field(default=10.0, gt=0)
    t0_debounce_ticks: int = Field(default=3, ge=1)
    telemetry_log_interval_seconds: float = Field(default=5.0, gt=0)
    max_stale_telemetry_seconds: float = Field(default=3.0, gt=0)
    # D16: operator timeout applies ONLY in true operator-required states
    # (manual confirmation, hold, recovery) — never in normal phases. The
    # machine is already hardware-off in those states, so the timeout
    # raises a safety alert (a nag, not an actuation); 600 s gives an
    # operator a realistic window to return before the system complains.
    operator_timeout_seconds: float = Field(default=600.0, gt=0)

    def advisory_interval_for(self, phase: RoastPhase) -> float | None:
        """Return the minimum-interval consult floor for ``phase`` in seconds.

        Looks the phase up in :attr:`advisory_min_interval_seconds`. A phase
        absent from the map returns ``0.0`` — unthrottled: the interval never
        gates the heartbeat (``MIN_INTERVAL`` fires on every eligible tick),
        so the advisor is consulted as soon as the previous serial call
        returns, bounded only by advisor latency. This is the intended
        behavior for first-crack / development (#171). A mapped ``None`` means
        NO fixed heartbeat — ``MIN_INTERVAL`` never fires for that phase, which
        is left to its change-based / near-FC triggers (D32: pre-first-crack).

        Args:
            phase: The agent phase the controller is currently in.

        Returns:
            The consult floor in seconds; ``0.0`` means unthrottled, ``None``
            means no fixed heartbeat (the controller skips ``MIN_INTERVAL``).
        """
        return self.advisory_min_interval_seconds.get(phase, 0.0)


class AdvisorConfig(BaseModel):
    """Advisor provider configuration (D5 + D18: provider-agnostic via a
    config-selected PydanticAI model factory).

    D18 supersedes the OpenRouter-only reading of D5. ``provider`` selects
    how the advisor's PydanticAI ``Model`` is built (see
    ``advisor.build_model``): the native ``openai`` / ``anthropic`` /
    ``google`` providers go direct, while ``ollama`` and
    ``openai_compatible`` use an OpenAI-compatible endpoint at
    ``provider_base_url``. The default — ``openai_compatible`` + the
    OpenRouter ``provider_base_url`` — preserves the prior behavior.

    ``provider_base_url`` is used only for the OpenAI-compatible providers
    (OpenRouter via the default URL, or a LAN Ollama URL); it is inert for
    the native providers. The API key is always read at build time from the
    environment variable named by ``api_key_env`` and handed to the
    provider — it never lives in config or the database.

    The default ``model_slug`` and ``prompt_version`` are the bake-off's
    outcome. The Artisan-expanded re-run (D33, 14 Jun 2026 — 28 real
    quality-filtered Hottop roasts, fixing the original N=2 sample) picked
    ``google/gemini-3.1-flash-lite`` via OpenRouter: the only model that
    reliably makes the flavor-critical drop call (drop F1 0.63, 18/28, best
    heat-direction 0.88, fastest ~1.2 s), where the prior incumbent
    ``anthropic/claude-opus-4.8`` (D20/D21) and every frontier/slow model
    over-hold (never drop). The prompt is ``v4`` (D34, the #194 prompt bake-off):
    the profile-anchored drop prompt that closes v2's drop-recall gap on the same
    28 roasts (recall 0.68→1.0, F1 0.66→0.88) and generalizes 19/19 on the
    held-out roasts. See ``docs/advisor/experiment.md``. To
    run a model on its native provider (no OpenRouter hop/markup, per D18), set
    ``provider`` + the matching ``api_key_env``. ``OPENROUTER_API_KEY`` must be
    set in the environment at runtime; ``FakeAdvisor`` stays the test/CI default.

    Per-phase model selection (#173): ``model_slug`` is the base/default slug
    (the identity in the decision-trace descriptor and the reachability probe),
    and ``model_slug_by_phase`` is an optional per-phase override map resolved
    by :meth:`model_for`. By default every phase resolves to
    ``DEFAULT_ADVISOR_MODEL`` — gemini-3.1-flash-lite everywhere (D33) — so the
    map is retained additive plumbing with zero behavior change; a future re-run
    could flip a phase slot to a different model. A phase absent from the
    override map falls back to ``model_slug``.
    """

    provider: Literal["openai", "anthropic", "google", "ollama", "openai_compatible"] = (
        "openai_compatible"
    )
    provider_base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = Field(default="OPENROUTER_API_KEY", min_length=1)
    model_slug: str = Field(default=DEFAULT_ADVISOR_MODEL, min_length=1)
    # Phase-keyed model override map (#173). The MECHANISM for phase-dependent
    # model selection — defaults to ``DEFAULT_ADVISOR_MODEL`` for preheat /
    # pre-FC / development (gemini-3.1-flash-lite everywhere, D33), so
    # :meth:`model_for` resolves to the single pinned model in every phase. The
    # map is retained so a future re-run could flip a phase slot to a different
    # model. A phase absent from this map falls back to ``model_slug``. Each slug
    # is ``min_length=1``
    # (an empty model slug is meaningless). Parameterized factory, not a bare
    # ``dict`` default, per the repo's pyright-strict typed-default idiom.
    model_slug_by_phase: dict[RoastPhase, Annotated[str, Field(min_length=1)]] = Field(
        default_factory=lambda: dict(DEFAULT_ADVISOR_MODEL_BY_PHASE)
    )
    timeout_seconds: float = Field(default=10.0, gt=0)
    # Bound on the startup reachability probe (issue #168). Deliberately short
    # — the probe is a cheap pre-charge liveness check, not an advice call, and
    # it must never wedge ``serve`` startup: a hung provider trips this and the
    # readout reports UNREACHABLE so the operator can decide to proceed
    # advisory-paused. 5 s catches a slow-but-alive provider while still being
    # well inside an operator's pre-roast attention window.
    healthcheck_timeout_seconds: float = Field(default=5.0, gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    # v4 (D34, the #194 prompt bake-off): the profile-anchored drop prompt closes
    # v2's drop-recall gap (recall 0.68→1.0, F1 0.66→0.88, precision up, heat
    # direction held; generalizes 19/19 on held-out roasts). v2 told the model to
    # "develop past the guide, don't rush the drop" — exactly what made the pinned
    # gemini model over-hold; v4 anchors the drop on the profile target + a
    # development floor, ≤196 °C bitter ceiling. See docs/advisor/experiment.md.
    prompt_version: str = Field(default="v4", min_length=1)
    # Reasoning control for the OpenAI-compatible path (OpenRouter normalizes
    # the ``reasoning`` request param across providers). ``None`` leaves the
    # provider default; ``"off"`` disables reasoning; the effort levels set
    # ``reasoning.effort``. Native anthropic/google providers ignore this.
    # Default ``None`` preserves provider behavior; the advisor's interest is
    # fast structured advice inside the 10 s tick budget, so the bake-off
    # measures reasoning on-vs-off (E8-S4 cost/reasoning eval).
    reasoning_effort: Literal["off", "minimal", "low", "medium", "high"] | None = None

    def model_for(self, phase: RoastPhase) -> str:
        """Return the advisor model slug to use for ``phase`` (#173).

        Looks ``phase`` up in :attr:`model_slug_by_phase`, falling back to the
        base :attr:`model_slug` when the phase carries no override. With the
        default map (gemini-3.1-flash-lite for every phase, D33) this always
        resolves to :attr:`model_slug`, so per-phase selection is a behavioral
        no-op until a future re-run populates the map with a different per-phase
        model.

        Args:
            phase: The agent phase the controller is currently in.

        Returns:
            The model slug for ``phase`` — its override if present, else the
            base ``model_slug``.
        """
        return self.model_slug_by_phase.get(phase, self.model_slug)


class SafetyLimits(BaseModel):
    """Hard safety limits enforced by deterministic code (rule set: E3).

    All values are **conservative software ceilings**, deliberately below
    anything the hardware should ever reach; they require supervised Hottop
    validation at E12-S1 before any hardware-ready claim. Justifications:

    - ``max_bean_temp_c`` 230 °C: beyond the second-crack range (~224 °C);
      no roast in scope needs more, and it stays below the Hottop's built-in
      over-temperature protection.
    - ``max_env_temp_c`` 240 °C: environment readings above this indicate a
      fault (sensor, airflow, or heater), not a roast.
    - ``pre_t0_max_bean_temp_c`` 200 °C: the documented pre-T0 upper charge
      safety bound (orchestration plan § Safety Policy). Deliberately equals
      models.RoastProfile.charge_guidance_max_c — the guidance band must end
      at or below this hard bound; a scaffold test pins the relationship.
    - ``overrun_safe_fan_percent`` 100: on pre-T0 overrun the rule sets heat
      to 0 % and fan high to move air through the chamber.
    - ``pre_t0_overrun_severity``: whether the overrun rule lands in
      ``operator_recovery_required`` (default) or ``faulted`` — maps to
      SafetyVerdict.RECOVERY / FAULT in the E3 rule set.
    - ``min_seconds_between_commands`` 2.0: the Hottop serial/sensor loop
      runs at ~1 Hz (orchestration plan § Hardware Characteristics); writes
      more frequent than this cannot have an observable effect and only
      churn the serial protocol.
    - ``max_consecutive_mcp_failures`` 3: at the 1.0 s tick this tolerates a
      ~3 s blind window before faulting — the same scale as the T0 debounce,
      long enough to ride out a transient stdio hiccup, short enough that a
      hot machine is never uncontrolled for long.
    - ``max_consecutive_advisor_failures`` 3 (D30, #166): consecutive advisor
      *availability* failures (``provider_error`` / ``timeout``) tolerated
      before the controller fails closed — drives heat 0 % and enters
      ``operator_recovery_required`` (NOT a fault: the operator explicitly
      resumes / drops / cools). A single transient blip still just holds the
      current targets (the E3-S3 hold-current fallback, unchanged). 3 ≈ a few
      seconds of *sustained* outage at the advisor's consult cadence — long
      enough to ride out one blip, short enough that a stale static profile
      cannot run the roast up to the hard ceilings unattended (the #134
      failure mode). ``malformed`` / ``unsafe`` are provider-*reachable*
      (model misbehaving, a different class) and deliberately do NOT count
      toward this stop.
    """

    max_bean_temp_c: float = Field(default=230.0, gt=0)
    max_env_temp_c: float = Field(default=240.0, gt=0)
    pre_t0_max_bean_temp_c: float = Field(default=200.0, gt=0)
    overrun_safe_fan_percent: int = Field(default=100, ge=0, le=100)
    pre_t0_overrun_severity: Literal["recovery", "fault"] = "recovery"
    min_seconds_between_commands: float = Field(default=2.0, gt=0)
    max_consecutive_mcp_failures: int = Field(default=3, ge=1)
    max_consecutive_advisor_failures: int = Field(default=3, ge=1)


class MCPConfig(BaseModel):
    """coffee-roaster-mcp child-process settings (D6, E5-S2).

    - ``command`` + the fixed ``serve`` positional form the spawn argv
      (`coffee-roaster-mcp serve`, matching server.json packageArguments).
    - ``call_timeout_seconds`` 5.0: every MCP call — including
      ``emergency_stop`` — must raise rather than stall the tick loop
      (safety-reviewer carry-forward, E4-S2). Five seconds ≈ five stalled
      ticks worst case before the typed failure surfaces and the
      consecutive-failure rules take over; far below any human reaction
      window, far above any healthy stdio round trip.
    - ``startup_timeout_seconds`` 15.0: the bootstrap-safe mock server
      starts in well under a second; 15 s tolerates first-run environment
      slowness without masking a wedged child.
    """

    command: str = Field(default="coffee-roaster-mcp", min_length=1)
    call_timeout_seconds: float = Field(default=5.0, gt=0)
    startup_timeout_seconds: float = Field(default=15.0, gt=0)
    #: Environment overrides for the spawned child, merged over the agent's own
    #: environment at spawn (so ``PATH``/``HOME`` are preserved). Empty by
    #: default — production inherits the deployment environment (the real
    #: hardware driver). The coffee-roaster-mcp mock-driver selectors can be set
    #: here (``COFFEE_ROASTER_DRIVER=mock``, ``COFFEE_FIRST_CRACK_MODE=disabled``)
    #: to make the subprocess hardware-, audio-, and model-free. The E9-S2 slice
    #: points ``COFFEE_ROASTER_MCP_CONFIG`` at a temp YAML instead, because the
    #: one selector it needs — auto-T0 detection — is config-file-only, and the
    #: YAML carries the mock-driver + disabled-first-crack settings alongside it.
    #: (Parameterized factory, not bare ``dict``: the repo's pyright-strict
    #: idiom for typed defaults, as in ``advisor.AdvisorContext``.)
    env: dict[str, str] = Field(default_factory=dict[str, str])


class AppConfig(BaseSettings):
    """Top-level application settings, loadable from environment variables.

    Nested fields override via ``ROASTPILOT_`` + section + ``__`` + field,
    e.g. ``ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS=0.5``.
    """

    model_config = SettingsConfigDict(env_prefix="ROASTPILOT_", env_nested_delimiter="__")

    controller: ControllerConfig = Field(default_factory=ControllerConfig)
    advisor: AdvisorConfig = Field(default_factory=AdvisorConfig)
    safety: SafetyLimits = Field(default_factory=SafetyLimits)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
