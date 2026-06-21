"""Typed application configuration (component plan §4; orchestration plan
§ Configuration Model).

Finalized at E2-S3. Controller timing defaults are the documented
hardware-aligned values from the orchestration plan; safety limits are
deliberately conservative software ceilings pending supervised hardware
validation at E12 (E12-S1).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator
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
    # Post-FC the loop is consulted at a deliberate ~5 s cadence (D35 §4-A,
    # D40.5, #276), not every tick: a steady dwell between consults is what lets
    # the deadband judge the model's trajectory across consecutive moves rather
    # than chase per-tick jitter (the #218 thrash). The change-based triggers
    # additionally respect this floor in development (see
    # ``ControllerConfig.post_fc_min_consult_interval_seconds`` and
    # ``AdvisoryCallPolicy.evaluate``), so the development cadence is the floor,
    # not back-to-back at advisor latency.
    RoastPhase.DEVELOPMENT: 5.0,
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


class PreFirstCrackLevers(BaseModel):
    """Deterministic pre-first-crack heat/fan lever parameters (D35 §3/§4-A, #222).

    D35 splits control at first crack: before FC the controller deterministically
    drives the levers (the free-form advisor is NOT consulted), porting the
    operator's proven n8n decision-tree values — **heat 100 / fan low to FC**
    (max heat, low fan until browning; no momentum-killing cuts). These are
    PARAMETERS, not constants in code (plan §4-A.3 / §7.1): the defaults are the
    operator's proven values, and the shape lets a learned per-bean plan (D42
    §7.1) later supply different ramps without a code change.

    The values feed :class:`~roastpilot_agent.control_policy.RoastControlPolicy`,
    which resolves them into the narrowed pre-FC box (heat floor pinned to the
    target so a cut is impossible; fan capped at the low ceiling) AND the
    deterministic target the controller actuates each tick — told == enforced
    from one source (D35 §8.3).

    All values are percentages (0–100); temperatures are out of scope here (the
    box ceilings live in :class:`SafetyLimits`).
    """

    #: The deterministic heat the controller holds through preheat → FC. Default
    #: 100 — the operator's proven n8n pre-FC heat (steady high heat to drive the
    #: roast to first crack; do not extend roast time). The policy pins the heat
    #: *floor* to this same value so a momentum-killing cut (#218's 70→40→20→0)
    #: is structurally impossible pre-FC.
    heat_target_percent: int = Field(default=100, ge=0, le=100)
    #: The deterministic fan the controller holds through preheat → FC. Default
    #: 30 — the operator's proven n8n pre-FC fan (low airflow until browning).
    fan_target_percent: int = Field(default=30, ge=0, le=100)
    #: The pre-FC fan box ceiling. Default 30 — equal to the fan target (the
    #: operator's low-fan method admits no higher pre-FC airflow). Must be >= the
    #: fan target so the deterministic write sits inside its own box (a validator
    #: pins it); raise it to leave the policy room above the target if a profile
    #: later wants a small browning-entry fan opening (plan §3).
    fan_ceiling_percent: int = Field(default=30, ge=0, le=100)

    @model_validator(mode="after")
    def _check_fan_ceiling(self) -> "PreFirstCrackLevers":
        """The fan ceiling must not sit below the fan target.

        A ceiling below the target would make the deterministic fan write fall
        outside its own box (the gate would clamp the policy's own target),
        breaking told == enforced for the deterministic path.

        Returns:
            The validated levers instance.

        Raises:
            ValueError: If ``fan_ceiling_percent`` is below ``fan_target_percent``.
        """
        if self.fan_ceiling_percent < self.fan_target_percent:
            raise ValueError(
                "fan_ceiling_percent must not be below fan_target_percent "
                f"({self.fan_ceiling_percent} < {self.fan_target_percent})"
            )
        return self


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
    # Phase-keyed consult floors (D32 / #191, refining #171): seconds between
    # automatic consults, by agent phase. Defaults: preheat OFF (absent from
    # the map — not an automatic-advice phase), pre-first-crack ``None`` (no
    # fixed heartbeat — change-based + near-FC boost only), development 0.0 =
    # unthrottled. A phase absent from the map returns 0.0 (unthrottled). A
    # mapped ``None`` disables the MIN_INTERVAL heartbeat for that phase entirely
    # (distinct from 0: ``None`` → skip the check; 0 → check fires every tick).
    # Values are ``ge=0`` or ``None`` (a negative floor would be meaningless).
    #
    # NOTE (#171 → D32): replaced the prior scalar ``float`` (15 s). An env var
    # of the old scalar shape no longer coerces; supply per-phase values keyed
    # by ``RoastPhase`` value
    # (e.g. ``{"roasting_pre_first_crack": null, "development": 0}``).
    advisory_min_interval_seconds: dict[RoastPhase, Annotated[float, Field(ge=0)] | None] = Field(
        default_factory=lambda: dict(DEFAULT_ADVISORY_MIN_INTERVAL_SECONDS)
    )
    # RETIRED under D35 (#222) — these shaped *pre-FC* advisory cadence, and the
    # advisor is no longer consulted before first crack (the deterministic
    # controller owns the pre-FC levers). Kept as inert, defaulted fields so a
    # frozen ``roast_runs`` config from before #222 still deserializes unchanged;
    # nothing reads them now. Remove once no persisted config references them.
    #
    # Near-FC cadence boost (D32 / #191, retired): once boosted the advisory
    # heartbeat as the bean neared the FC band so the anticipatory cut wasn't
    # missed if RoR flattened into the crack.
    advisory_near_fc_bean_temp_c: float = Field(default=170.0, gt=0)
    advisory_near_fc_interval_seconds: float = Field(default=10.0, gt=0)
    # Post-charge SETTLE window (#209, retired): suppressed the first automatic
    # post-charge consult on the crashing not-yet-turned bean until the turning
    # point (RoR >= threshold) or a fallback timeout. Now a no-op (D35 §2): with
    # no pre-FC advice at all there is nothing to suppress.
    advisory_post_charge_settle_max_seconds: float = Field(default=90.0, gt=0)
    advisory_post_charge_turning_point_ror_c_per_min: float = Field(default=0.0)
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
    # Deterministic pre-first-crack heat/fan levers (D35 §3/§4-A, #222): the
    # operator's proven n8n pre-FC values (heat 100 / fan low), resolved by
    # RoastControlPolicy into the narrowed pre-FC box + the deterministic target
    # the controller actuates each tick. Parameterised, not hardcoded — a learned
    # plan (D42 §7.1) can supply a different ramp. (Parameterized factory, not a
    # bare model default, per the repo's pyright-strict typed-default idiom.)
    pre_first_crack_levers: PreFirstCrackLevers = Field(default_factory=PreFirstCrackLevers)
    # D40.3 / D40.5 (#275): per-tick control-loop CONTEXT payload bounds. The
    # context builder (roast_history.RoastHistory) keeps the roast-so-far curve
    # as a bounded recent FULL-RESOLUTION window plus a milestone summary, and the
    # model's own recommendations as a bounded decision trace — never a raw
    # whole-roast dump. These set the bounds; defaults mirror the module's named
    # constants (~60 s of 1 s curve, ~1 min of 5 s consults). Context-only — they
    # never touch the safety path.
    curve_window_samples: int = Field(default=60, ge=1)
    decision_trace_entries: int = Field(default=12, ge=1)
    # FC-ETA projection target (#229 KEEP): the FC-band bean temperature the
    # builder extrapolates the recent bean RoR toward to estimate the first-crack
    # ETA. Default 176.0 °C = the #229-validated FC band midpoint (171-180 °C) on
    # this roaster's indicated probe. Anticipation context only; never a lever.
    first_crack_target_bean_temp_c: float = Field(default=176.0, gt=0)
    # D35 §4-A / D40.5 (#276): the post-FC control-loop cadence + coherence gate.
    #
    # ``post_fc_min_consult_interval_seconds`` — the minimum seconds between
    # AUTOMATIC post-FC consults, applied to the change-based triggers too (not
    # just the MIN_INTERVAL heartbeat), so development consults run at a deliberate
    # ~5 s dwell rather than back-to-back at advisor latency (the steady cadence
    # the deadband judges the model's trajectory across, #218). A manual operator
    # request still bypasses it. Default 5.0 = the D40.5 post-FC cadence.
    post_fc_min_consult_interval_seconds: float = Field(default=5.0, gt=0)
    # ``post_fc_deadband_threshold_percent`` — the reversal magnitude (percentage
    # points) at or above which a lever direction-reversal is DECISIVE and allowed;
    # a reversal BELOW it is incoherent thrash and is damped to a hold
    # (roastpilot_agent.coherence.evaluate_lever_coherence). Damps the #218
    # 30<->40<->30 twiddle (steps of ~10) while letting a real cut/raise through.
    # Default 15 — above the observed twiddle step, below a decisive move; the
    # exact value is tuned on the replay harness (#277), so it is a named config
    # constant, not a literal in the gate.
    post_fc_deadband_threshold_percent: int = Field(default=15, ge=1, le=100)
    # ``post_fc_min_confidence`` — the advisor confidence floor below which a
    # post-FC recommendation is treated as "I don't know" and fails closed to a
    # deterministic HOLD (no actuation), alongside the silent/slow/error/rejected
    # paths (#276). Default 0.2 — a near-zero-confidence move holds; legitimate
    # advice (the FakeAdvisor scripts at 0.9) passes. Tuned on the replay harness
    # (#277). Set 0.0 to disable the floor.
    post_fc_min_confidence: float = Field(default=0.2, ge=0.0, le=1.0)

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
    - ``bitter_ceiling_temp_c`` 196 °C (D35 §3, the operator's empirical drop
      ceiling): the ≤196 °C *indicated* bean temperature past which a medium
      roast turns bitter / ashy (irreversible). This is the **drop/bitter
      ceiling** the D35 control box and the post-FC LLM (#223) reason inside —
      surfaced through :class:`~roastpilot_agent.control_policy.RoastControlPolicy`
      as a *told* limit. It is NOT a new hard-stop verdict in this story (the
      enforced emergency ceiling stays ``max_bean_temp_c``); #223 owns wiring
      the drop box. Recorded here as the single source so the value the model is
      told equals the value the gate will enforce.
    - ``emergency_drop_temp_c`` 198 °C (D35 §3): the emergency-drop bound — bean
      temperature above which the roast must be dropped immediately regardless
      of development. Above the ≤196 °C bitter ceiling by design (a 2 °C margin
      that is the last-resort bound, not the target). Same single-source role as
      ``bitter_ceiling_temp_c``: a *told* limit surfaced through the policy, not
      a new verdict in this story.
    """

    max_bean_temp_c: float = Field(default=230.0, gt=0)
    max_env_temp_c: float = Field(default=240.0, gt=0)
    pre_t0_max_bean_temp_c: float = Field(default=200.0, gt=0)
    overrun_safe_fan_percent: int = Field(default=100, ge=0, le=100)
    pre_t0_overrun_severity: Literal["recovery", "fault"] = "recovery"
    min_seconds_between_commands: float = Field(default=2.0, gt=0)
    max_consecutive_mcp_failures: int = Field(default=3, ge=1)
    max_consecutive_advisor_failures: int = Field(default=3, ge=1)
    # D35 §3 drop/bitter ceilings (Celsius), the single source the control
    # policy surfaces into both the advisor context and the (future #223) gate.
    # Conservative software values from the operator's empirical Hottop profile;
    # validate at E12. ``emergency_drop_temp_c`` must sit above
    # ``bitter_ceiling_temp_c`` (a model validator pins it) — the bitter ceiling
    # is the *target* ceiling, the emergency-drop bound is the last-resort one.
    bitter_ceiling_temp_c: float = Field(default=196.0, gt=0)
    emergency_drop_temp_c: float = Field(default=198.0, gt=0)

    @model_validator(mode="after")
    def _check_drop_ceiling_order(self) -> "SafetyLimits":
        """The drop/bitter ceilings must order correctly under the hard ceiling.

        Guards the D35 §3 invariants:

        - ``emergency_drop_temp_c`` is the last-resort bound *above* the ≤196 °C
          ``bitter_ceiling_temp_c`` target ceiling — an inverted pair would make
          the emergency bound fire before the bitter ceiling the model is told to
          respect.
        - Both told ceilings sit *below* ``max_bean_temp_c``, the hard enforced
          bean-temp ceiling. A told ceiling at or above the hard ceiling is a
          misconfiguration the gate can never honour (the gate would fault on the
          hard ceiling first), so the value the model is told would never be the
          value enforced.

        Returns:
            The validated limits instance.

        Raises:
            ValueError: If ``emergency_drop_temp_c <= bitter_ceiling_temp_c`` or
                if either told ceiling is ``>= max_bean_temp_c``.
        """
        if self.emergency_drop_temp_c <= self.bitter_ceiling_temp_c:
            raise ValueError(
                "emergency_drop_temp_c must be above bitter_ceiling_temp_c "
                f"({self.emergency_drop_temp_c} <= {self.bitter_ceiling_temp_c})"
            )
        if self.bitter_ceiling_temp_c >= self.max_bean_temp_c:
            raise ValueError(
                "bitter_ceiling_temp_c must be below max_bean_temp_c "
                f"({self.bitter_ceiling_temp_c} >= {self.max_bean_temp_c})"
            )
        if self.emergency_drop_temp_c >= self.max_bean_temp_c:
            raise ValueError(
                "emergency_drop_temp_c must be below max_bean_temp_c "
                f"({self.emergency_drop_temp_c} >= {self.max_bean_temp_c})"
            )
        return self


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


# Access-log mode (issue #267): how the ``serve`` uvicorn access logger treats
# SUCCESSFUL (status < 400) requests on the chatty paths.
#   - ``quiet`` (default): drop 2xx/3xx on the quiet-path set (the SSE stream,
#     the per-tick telemetry series, the health poll) so a live roast's console
#     isn't buried; keep every 4xx/5xx (any path) and every other path.
#   - ``full``: log all requests (today's behaviour) — no filter installed.
#   - ``off``: disable the uvicorn access log entirely (``access_log=False``).
HttpAccessLogMode = Literal["quiet", "full", "off"]

# The default quiet-path set (issue #267). These MIRROR the route-path constants
# in ``api.py`` (``HEALTH_PATH`` / ``TELEMETRY_PATH`` / ``EVENTS_PATH``) — the
# real route templates, so a future route rename can't silently un-quiet them.
# config.py must not import api.py (api.py imports config.py — an import cycle),
# so the values are duplicated here and pinned equal to the api.py source of
# truth by a drift test (``tests/test_logging_config.py``). The ``{run_id}``
# segment is matched as a prefix+suffix pattern by the CLI filter, catching any
# run id.
DEFAULT_HTTP_ACCESS_LOG_QUIET_PATHS: tuple[str, ...] = (
    "/api/roasts/{run_id}/events",
    "/api/roasts/{run_id}/telemetry",
    "/api/health",
)


class LoggingConfig(BaseModel):
    """Serve-time HTTP access-log verbosity controls (issue #267).

    Logging configuration only — it changes what the ``uvicorn.access`` logger
    emits during ``serve``, never any API behaviour, the controller, the
    advisor, or the SSE contract. The operator-facing #157 startup readout and
    all app/domain logs are unaffected.

    Resolution precedence (applied in ``cli.py``): a CLI flag wins over the
    ``ROASTPILOT_*`` environment variable, which wins over these config
    defaults — mirroring the ``--db`` > ``ROASTPILOT_DB`` > default pattern.
    """

    #: Master access-log mode: ``quiet`` (default), ``full``, or ``off``.
    http_access_log_mode: HttpAccessLogMode = "quiet"
    #: Route paths whose SUCCESSFUL (status < 400) requests are dropped in
    #: ``quiet`` mode. Templates with ``{run_id}`` are matched as prefix+suffix
    #: patterns so any run id is caught. Defaults to the three chatty paths.
    http_access_log_quiet_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_HTTP_ACCESS_LOG_QUIET_PATHS)
    )
    #: The uvicorn ``log_level`` for ``serve`` (default ``info``, today's value).
    log_level: str = Field(default="info", min_length=1)


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
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
