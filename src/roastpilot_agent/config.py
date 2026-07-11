"""Typed application configuration (component plan §4; orchestration plan
§ Configuration Model).

Finalized at E2-S3. Controller timing defaults are the documented
hardware-aligned values from the orchestration plan; safety limits are
deliberately conservative software ceilings pending supervised hardware
validation at E12 (E12-S1).
"""

from pathlib import Path
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

# The advisor model — the #277 post-FC control bake-off PIN (21 Jun 2026):
# ``openai/gpt-4o`` via OpenRouter. Under D35 the live advisor is consulted only
# in the post-FC DEVELOPMENT phase (the controller drives pre-FC
# deterministically), so the #277 eval scored that scope across 17 known-good
# mediums. gpt-4o tracks the operator's real heat moves the closest (heat MAE
# ~7.5 pp vs ~22 for gemini-3.1-flash-lite and ~31 for gemini-3-flash-preview),
# has the best heat-direction agreement (~0.78), a reliable drop (F1 ~0.86), and
# live-viable latency (~2.0 s, inside the 10 s gate). It is ALSO the proven
# control model in the operator's working n8n autonomous roaster (D40.4), which
# de-risks the first supervised hardware roast. Runner-up
# ``google/gemini-3.1-flash-lite`` (faster + ~5x cheaper) lost on heat-magnitude
# fidelity; ``google/gemini-3-flash-preview`` was rejected (best drop but steers
# heat the wrong way). See ``docs/advisor/bakeoff-results-2026-06-21.md``. This is
# the base slug AND the default for every phase; the per-phase mechanism (#173)
# is retained so a future re-run can flip a slot without a behavior change.
DEFAULT_ADVISOR_MODEL = "openai/gpt-4o"

# Phase-keyed advisor MODEL selection (#173, operator 13 Jun): the model slug
# the advisor uses, by agent phase. The MECHANISM only — every phase defaults to
# ``DEFAULT_ADVISOR_MODEL`` (gpt-4o everywhere, #277 PIN). Under D35 the advisor
# is consulted only in DEVELOPMENT (post-FC), so DEVELOPMENT is the phase the PIN
# actually governs; preheat / pre-FC are deterministic and never consult. The map
# is kept so a future re-run can flip a slot (e.g. a faster/cheaper development
# model once the cloud feedback loop learns heat trims) and record it as a new
# D-number. A phase absent from the map falls back to ``model_slug``.
DEFAULT_ADVISOR_MODEL_BY_PHASE: dict[RoastPhase, str] = {
    RoastPhase.PREHEATING: DEFAULT_ADVISOR_MODEL,
    RoastPhase.ROASTING_PRE_FIRST_CRACK: DEFAULT_ADVISOR_MODEL,
    RoastPhase.DEVELOPMENT: DEFAULT_ADVISOR_MODEL,
}


class LateMaillardTrim(BaseModel):
    """Deterministic anticipatory heat-trim parameters, late Maillard → FC (D35 §3, #327).

    The §3 phase table specs a deterministic anticipatory heat **trim** in the
    late-Maillard → FC window (still pre-FC): a *moderate* heat reduction (the
    issue's "~60–70 %, not a crash"), fan held at the floor, to bend the RoR
    smoothly into first crack. #222 shipped only the flat ``heat 100 / fan 30 →
    FC`` floor; roast 3 (21 Jun 2026) proved the flat floor OVERSHOOTS — flat
    100 % drove a late FC, env reached 239 °C, and the bean coasted 193 → 203 °C
    even after the post-FC loop cut heat to 0, dropping 8 °C over the 195 °C
    ceiling (#327 evidence). Trimming heat in late Maillard cools the env so
    development accrues *before* the ceiling, and the drop lands ≤ ceiling.

    These are PARAMETERS, not constants in code (plan §8.3 single-source):
    :class:`~roastpilot_agent.control_policy.RoastControlPolicy` resolves them,
    keyed on the live bean temperature + the #229 predicted-FC ETA, into the
    pre-FC box (heat floor + target lowered to the trim level while the window is
    open; fan unchanged) AND the deterministic target the controller actuates.
    No LLM is consulted — D35 keeps pre-FC fully deterministic.

    **Fails closed to the flat floor.** The trim engages ONLY while the window is
    resolvable (bean ≥ ``min_bean_temp_c`` AND a positive FC-ETA at or below
    ``window_fc_eta_seconds``). Whenever the FC-ETA is unknown, any input is
    missing, or the bean is not yet in the window, the policy resolves the
    existing flat ``heat 100 / fan 30`` floor — the floor stays the always-on
    guarantee that FC still arrives (plan §8.4). The trim is a BOUNDED reduction:
    it never raises heat above the floor and is sized not to stall or delay FC.

    The window can be disabled wholesale with ``enabled=False`` (reverts to the
    pure #222 flat floor). All values are percentages, Celsius, or seconds.

    **Adaptive trim depth (#386).** The default ``adaptive_depth_enabled=False``
    keeps the current fixed-depth behaviour byte-for-byte — the roast-6-proven
    method stays the default and no hardware change is made without opt-in.  When
    enabled, :meth:`depth_for` applies the formula::

        clamp(base_trim − k_ror·max(0, ror − ror_ref)
              − k_eta·max(0, eta_ref − eta),
              min_trim, max_trim)

    A hotter approach (high RoR, short ETA) deepens the cut; a gentle approach
    (low RoR, long ETA near the window boundary) produces a shallower cut near
    ``base_trim``.  All coefficients are D42-learnable config fields; the
    conservative starting defaults are validated offline for monotonicity but
    tuned on hardware at roast 7.  ``base_trim=65`` means enabled-but-untuned ==
    the current fixed depth — a safe enable path with no change until the
    coefficients are dialled in.

    Fan is NEVER touched by this feature: no pre-FC fan raise (the #218
    anti-pattern).  The latch still governs *engagement*; depth eases with the
    signal but the trim does not thrash on/off.
    """

    #: Whether the anticipatory trim is active. ``False`` reverts to the pure
    #: #222 flat floor (heat 100 / fan 30 → FC) with no trim window — the
    #: fail-closed baseline, also the explicit off-switch for a profile/operator
    #: that wants the flat floor.
    enabled: bool = Field(default=True)
    #: The trimmed heat the controller holds once the window opens. Default 65 —
    #: the midpoint of the plan's "~60–70 %, not a crash" band (#327 / plan §3).
    #: A moderate reduction from the flat-floor 100 %: enough to cool the env and
    #: bend RoR into FC, NOT a momentum-killing cut (the #218 pre-FC crash). Must
    #: stay <= the flat-floor ``PreFirstCrackLevers.heat_target_percent`` (the
    #: trim only ever lowers heat — a validator on the parent pins this).
    #: Also the default ``base_trim`` for adaptive depth (#386): enabling adaptive
    #: mode with default coefficients preserves the proven fixed depth.
    trim_heat_percent: int = Field(default=65, ge=10, le=100)
    #: Seconds-before-predicted-FC at which the window OPENS. Default 60 — the
    #: trim engages in late Maillard, ~1 min ahead of the projected crack, so the
    #: env cools before FC rather than at it. Well inside the #229-validated
    #: FC-ETA accuracy horizon (the naive projection lands within ~half the
    #: detector-lag window by ~90 s out), so the trigger is on a trustworthy ETA.
    window_fc_eta_seconds: float = Field(default=60.0, gt=0)
    #: The bean-temperature floor below which the trim NEVER engages, even if a
    #: noisy RoR projects a spurious near-term FC. Default 155 °C — genuinely
    #: late Maillard on this roaster's indicated probe (FC band 171–180 °C,
    #: ``ControllerConfig.first_crack_target_bean_temp_c`` 176 °C), ~20 °C below
    #: the FC target. Guards the FC-ETA trigger against an early false positive.
    min_bean_temp_c: float = Field(default=155.0, gt=0)

    # ------------------------------------------------------------------
    # Adaptive trim-depth (#386) — opt-in, default OFF.
    # All fields below are TUNING PARAMETERS refined on hardware (roast 7+).
    # The conservative starting defaults ensure monotonicity offline but are
    # not thermally validated — see the offline choice-validation tests.
    # ------------------------------------------------------------------

    #: Whether adaptive trim depth is active (#386). Default ``False`` — the
    #: current fixed-depth method is the default; adaptive depth is opt-in.
    #: When ``False`` (or when RoR / FC-ETA is unavailable), ``depth_for``
    #: returns the fixed ``trim_heat_percent`` unchanged — byte-for-byte the
    #: proven roast-6 behaviour.
    adaptive_depth_enabled: bool = Field(default=False)
    #: The adaptive-depth baseline (percentage). Default 65 — equal to the
    #: fixed ``trim_heat_percent`` so that enabling adaptive mode without
    #: tuning the gain coefficients reproduces the fixed depth exactly. Must
    #: satisfy ``min_trim ≤ base_trim ≤ max_trim`` (a validator pins this).
    base_trim: int = Field(default=65, ge=10, le=100)
    #: RoR sensitivity coefficient (°C/min per pp of trim deepening). Default
    #: 1.5 — each extra °C/min above ``ror_ref`` deepens the cut by 1.5 pp,
    #: so a roast-3-style hot approach (+8 °C/min above ref) deepens by 12 pp
    #: (65 → 53, still above ``min_trim``). Conservative: offline validation
    #: confirms monotonicity; thermal outcome on hardware at roast 7.
    k_ror: float = Field(default=1.5, ge=0.0)
    #: ETA sensitivity coefficient (seconds per pp of trim deepening). Default
    #: 0.2 — each 1 s under ``eta_ref`` deepens by 0.2 pp, so a very short
    #: ETA (30 s vs. the 60 s ``eta_ref``) deepens by 6 pp. Modest by design;
    #: the RoR term is the primary dial. Conservative; tuned at roast 7.
    k_eta: float = Field(default=0.2, ge=0.0)
    #: RoR reference level (°C/min). Default 8.0 — below this the RoR term
    #: contributes 0; above it the cut deepens. Calibrated on the corpus
    #: (roast 3 hot approach ≈ 12–14 °C/min; roast 6 gentle ≈ 4–6 °C/min);
    #: 8 is the mid-point between roast-3 and roast-6 RoR in late Maillard.
    ror_ref: float = Field(default=8.0, ge=0.0)
    #: ETA reference (seconds). Default 60.0 — equal to ``window_fc_eta_seconds``
    #: so the ETA term is 0 at the window boundary and deepens only when the
    #: predicted crack is closer than the reference. Must be positive.
    eta_ref: float = Field(default=60.0, gt=0)
    #: Deepest permitted trim (percentage). Default 45 — the floor the adaptive
    #: formula cannot go below, preventing a stall of FC. This value must NOT
    #: be so low that the heat cut could arrest the exothermic first-crack
    #: reaction (plan §8.4 "FC always arrives"). Must satisfy
    #: ``min_trim ≤ base_trim`` (validator) and ``ge=10`` (same hard floor as
    #: ``trim_heat_percent`` itself).
    min_trim: int = Field(default=45, ge=10, le=100)
    #: Shallowest permitted trim (percentage). Default 75 — the ceiling above
    #: which the adaptive formula cannot go. The validator on the parent model
    #: pins ``max_trim ≤ heat_target_percent`` so adaptive depth is always a
    #: strict reduction even at its shallowest.
    max_trim: int = Field(default=75, ge=10, le=100)

    # ------------------------------------------------------------------
    # Adaptive-depth damping coefficients (#412) — applied in the
    # controller's trim path to suppress tick-to-tick RoR noise.
    # These are COEFFICIENTS only; the damping STATE (last applied depth)
    # lives in the controller so the config model stays pure/stateless.
    # Both coefficients are active only when ``adaptive_depth_enabled``
    # is ``True`` — the non-adaptive path is unaffected.
    # ------------------------------------------------------------------

    #: Deadband half-width (percentage points). The controller only commits
    #: a new adaptive depth if it differs from the last applied depth by
    #: more than this threshold.  Default 2 — absorbs the ~1–2 pp tick-to-
    #: tick RoR noise without hiding genuine signal; the roast-7 thrash was
    #: ~12 pp so a 2 pp deadband eliminates jitter while letting real
    #: run-ups through.  0 disables deadband (no hysteresis).
    trim_depth_deadband_pp: int = Field(default=2, ge=0, le=20)
    #: Maximum depth change per tick (percentage points).  The controller
    #: caps the per-tick move to this many pp so the trim ramps to a new
    #: level rather than stepping in one tick.  Default 3 — at the 1 s
    #: tick rate a sustained 9 pp run-up takes 3 ticks (~3 s), which is
    #: fast enough to track the genuine pre-FC acceleration and slow enough
    #: to suppress the ~12 pp/tick thrash observed on roast 7.
    trim_depth_slew_pp_per_tick: int = Field(default=3, ge=1, le=20)

    @model_validator(mode="after")
    def _check_adaptive_range(self) -> "LateMaillardTrim":
        """The adaptive depth range must be internally consistent (#386).

        Guards three invariants:

        - ``min_trim ≤ base_trim``: the deepest cut must not exceed the
          baseline (otherwise the formula could raise heat above ``base_trim``
          when both terms are zero, which contradicts the "baseline = no-signal
          depth" contract).
        - ``base_trim ≤ max_trim``: the baseline must not exceed the shallowest
          permitted cut (otherwise the formula's clamp would always override the
          baseline even when both gain terms are zero).
        - ``min_trim ≤ max_trim``: derived from the two above, but checked
          explicitly for a clear error message.

        These constraints are meaningful only when ``adaptive_depth_enabled``
        is ``True``; they are validated unconditionally so a misconfigured
        disabled object is caught at construction rather than silently at
        enable-time.

        Returns:
            The validated ``LateMaillardTrim`` instance.

        Raises:
            ValueError: If any of the three adaptive range invariants is
                violated.
        """
        if self.min_trim > self.base_trim:
            raise ValueError(
                f"min_trim must not exceed base_trim ({self.min_trim} > {self.base_trim})"
            )
        if self.base_trim > self.max_trim:
            raise ValueError(
                f"base_trim must not exceed max_trim ({self.base_trim} > {self.max_trim})"
            )
        if self.min_trim > self.max_trim:  # pragma: no cover - unreachable (min<=base<=max)
            raise ValueError(
                f"min_trim must not exceed max_trim ({self.min_trim} > {self.max_trim})"
            )
        # Damping cross-field: deadband must be strictly less than slew (#412).
        # When deadband >= slew, every slew candidate is within deadband of the
        # previous value (|candidate - prev| <= slew <= deadband), so the
        # deadband hold ALWAYS fires after the first tick — adaptive movement
        # is silently disabled.  The defaults (2 < 3) are valid.
        if self.trim_depth_deadband_pp >= self.trim_depth_slew_pp_per_tick:
            raise ValueError(
                "trim_depth_deadband_pp must be strictly less than "
                "trim_depth_slew_pp_per_tick to avoid silently disabling adaptive "
                f"movement ({self.trim_depth_deadband_pp} >= "
                f"{self.trim_depth_slew_pp_per_tick})"
            )
        return self

    def depth_for(
        self, bean_ror_c_per_min: float | None, first_crack_eta_seconds: float | None
    ) -> int:
        """Resolve the trim depth for this tick's signal (#386).

        Returns the adaptive trim depth when ``adaptive_depth_enabled`` is
        ``True`` AND both ``bean_ror_c_per_min`` and ``first_crack_eta_seconds``
        are non-``None``.  In every other case — flag off, or either signal
        value missing — returns the fixed ``trim_heat_percent`` unchanged.
        This is the *fail-closed* guarantee: any missing signal falls back to
        the proven fixed depth.

        The adaptive formula::

            clamp(
                base_trim
                    − k_ror · max(0, ror − ror_ref)
                    − k_eta · max(0, eta_ref − eta),
                min_trim,
                max_trim,
            )

        A hotter approach (high RoR or short ETA) deepens the cut (lower %);
        a gentle approach (RoR at or below ``ror_ref``, ETA at or above
        ``eta_ref``) contributes 0 from each term, yielding ``base_trim``
        exactly (clamped into [min_trim, max_trim]).

        Fan is NEVER modified by this method — it governs heat depth only.

        Args:
            bean_ror_c_per_min: The current bean rate of rise (°C/min), or
                ``None`` when unavailable. ``None`` ⇒ fixed depth.
            first_crack_eta_seconds: The predicted seconds until first crack,
                or ``None`` when unavailable. ``None`` ⇒ fixed depth.

        Returns:
            The trim depth as a heat percentage (integer, always in
            [min_trim, max_trim] when adaptive, else ``trim_heat_percent``).
        """
        if (
            not self.adaptive_depth_enabled
            or bean_ror_c_per_min is None
            or first_crack_eta_seconds is None
        ):
            return self.trim_heat_percent

        ror_term = self.k_ror * max(0.0, bean_ror_c_per_min - self.ror_ref)
        eta_term = self.k_eta * max(0.0, self.eta_ref - first_crack_eta_seconds)
        raw = self.base_trim - ror_term - eta_term
        # round() is ties-to-even (banker's), intentional here: the depth is a
        # coarse 1-pp control value and ties-to-even stays monotonic (non-increasing
        # in the subtracted terms), so it cannot invert the hotter→deeper ordering
        # the formula guarantees (#386 Augment low).
        return max(self.min_trim, min(self.max_trim, round(raw)))


class PostFirstCrackControl(BaseModel):
    """Deterministic post-FC RoR-taper PI control-loop parameters (D82/D88, #405 Slice B).

    D35 originally reserved DEVELOPMENT (post-first-crack) for the free-form
    advisor: the controller drove pre-FC deterministically, but post-FC heat/fan
    actuated straight from the advisor's ``target_heat``/``target_fan`` through
    the safety box. Roasts 7 and 8 showed that regime over-braking — a coherent
    *rationale* (cut heat, ramp fan, hold for the drop DTR) executed without the
    judgment to hold a heat floor to the drop temperature, landing an
    under-temp/under-developed drop (roast 7: 188 °C / 15.86 % DTR). D82 replaces
    the advisor-driven post-FC lever with a **deterministic closed loop**.

    D83's first control law (a fixed RoR-band target) was superseded after a
    hardware A/B (roasts 9/10, `docs/analysis/2026-07-09-roast9-10-postfc-ab.md`):
    a fixed 8.0 °C/min target sat ABOVE the measured post-FC engagement RoR
    (6.1 °C/min), so the loop read "too slow" from tick one and actuated heat
    72→91 % while the advisor recommended 0 % — a policy-legal runaway toward
    the bitter ceiling. **D88 is the current law**: the setpoint anchors to the
    MEASURED RoR at engagement and tapers DOWN to a lower end value over a fixed
    duration, and the loop's output can never exceed the heat the roast entered
    first crack with (the never-add-heat-beyond-entry clamp). This model encodes
    D88's parameters; the algorithm lives in
    :class:`~roastpilot_agent.post_fc_control.PostFcRorController`.

    **This config model is inert on its own (#405 Slice B1).** It is consumed by
    :class:`~roastpilot_agent.post_fc_control.PostFcRorController`, wired into
    the controller's DEVELOPMENT-phase tick (Slice B2) which builds the safety
    box from the loop's ACTUATED output (per the #412 told==enforced control-path
    rule: never an undamped setpoint) and routes every write through the
    existing safety gate. Nothing here talks to ``mcp_client`` directly, and
    nothing here replaces the safety box's hard ceilings (the 196 °C bitter /
    198 °C emergency-drop bounds keep clamping the loop's output exactly as
    they clamp the advisor today).

    ``enabled`` (the ``post_fc_ror_loop`` master flag) defaults ``False`` —
    today's advisor-driven post-FC regime is unchanged until a supervised
    hardware roast validates the loop and an operator flips the flag in a
    separately reviewed change. **A closed loop changes the trajectory it is
    steering, so replay cannot validate it — every parameter below is
    hardware-tuned at the validation roast, not offline-validated (n=2 going
    into D88).** All temperatures are Celsius; RoR is °C/min; heat/fan are
    percentages.
    """

    #: The master flag (``post_fc_ror_loop``). ``False`` (default) keeps
    #: today's advisor-driven post-FC heat/fan actuation byte-for-byte
    #: unchanged; Slice B2 must read this flag before routing DEVELOPMENT
    #: heat through the PI loop instead of the advisor.
    enabled: bool = Field(default=False)
    #: The taper's STARTING setpoint cap (°C/min), D88. The setpoint at
    #: engagement is ``clamp(ror_at_engagement, taper_end_ror_c_per_min,
    #: taper_start_max_ror_c_per_min)`` — anchored to the MEASURED RoR the
    #: roast actually had at the FC->DEVELOPMENT handoff, never a fixed value
    #: chosen ahead of time (D83's mistake). Default 8.0 — measured baseline
    #: engagement RoR was ~6.1 °C/min (roasts 9/10 A/B), so this cap rarely
    #: binds in practice; it exists to bound an unusually hot engagement.
    taper_start_max_ror_c_per_min: float = Field(default=8.0, gt=0)
    #: The taper's END setpoint (°C/min), D88 — the value the linear taper
    #: decays to and then holds. Default 4.0 — measured RoR at a clean
    #: advisor-driven drop (roast 9 baseline). Also the FLOOR the engagement
    #: setpoint clamp above never goes below: a degenerate low/negative
    #: engagement RoR (e.g. a post-charge-crash FC) starts the taper AT this
    #: value rather than under it, so the loop never over-cuts on tick 1.
    taper_end_ror_c_per_min: float = Field(default=4.0, gt=0)
    #: How long (seconds) the linear taper from the engagement setpoint down
    #: to ``taper_end_ror_c_per_min`` takes; held at the end value after.
    #: Default 90.0 — the measured baseline FC->drop window was 58-83 s across
    #: two data points (n=2, not a fitted curve); re-validate once more clean
    #: runs land.
    taper_duration_seconds: float = Field(default=90.0, gt=0)
    #: Half-width (°C/min) of the no-action band around the current taper
    #: setpoint. Default 1.0. Within ``±ror_deadband_c_per_min`` of the
    #: setpoint the loop HOLDS — no proportional push, no integral
    #: accumulation — so tick-to-tick RoR noise cannot thrash the heat lever
    #: (the #386/#412 lesson: a deadband that also freezes the integrator,
    #: not just the P term).
    ror_deadband_c_per_min: float = Field(default=1.0, ge=0)
    #: Proportional gain: %heat per (°C/min) of RoR error. Default 3.0 — a
    #: conservative starting value; MUST be tuned on hardware (a closed loop
    #: changes the trajectory it steers, so replay cannot validate it).
    kp_percent_per_ror: float = Field(default=3.0, ge=0)
    #: Integral gain: %heat per (°C/min·second) of accumulated RoR error.
    #: Default 0.1 — conservative; tuned on hardware alongside ``kp``.
    ki_percent_per_ror_second: float = Field(default=0.1, ge=0)
    #: The minimum post-FC heat the loop may command. Default 25, ``ge=1`` —
    #: deliberately > 0 so a crash-to-0 heat command (the roast-7 failure: the
    #: advisor cut heat to 0 at FC) is STRUCTURALLY IMPOSSIBLE from this loop,
    #: mirroring ``LateMaillardTrim.trim_heat_percent``'s ``ge=10`` floor
    #: guarantee for the pre-FC trim. D88's ``effective_ceiling`` (see
    #: :class:`~roastpilot_agent.post_fc_control.PostFcRorController`) can pull
    #: the EFFECTIVE box below this static value (never-add-heat-beyond-entry);
    #: this field remains the outer static bound either way.
    heat_floor_percent: int = Field(default=25, ge=1, le=100)
    #: The maximum post-FC heat the loop may command. Default 100. D88's
    #: never-add-heat-beyond-entry clamp (``effective_ceiling``) narrows the
    #: EFFECTIVE ceiling to the heat the roast held at FC engagement whenever
    #: that is lower than this static value.
    heat_ceiling_percent: int = Field(default=100, ge=1, le=100)
    #: **VESTIGIAL as of #498 (D89 Tier 1) — no longer read by the controller.**
    #: D83 call (6) originally had the loop pin fan to this single config value
    #: post-FC (the advisor's fan output IGNORED), because the roast-7
    #: over-brake was heat AND fan moving together. The 11 Jul validation A/B
    #: showed the pin wastes a second brake lever the advisor's judgment can
    #: use well (D89: "fan returns to the advisor as an ACTUATED lever in loop
    #: mode"), so fan is now the advisor's lever in loop mode, same as
    #: baseline, still through the same safety path — but the advisor's
    #: consult never writes fan directly (a safety-reviewer BLOCKER-1 fix,
    #: #498): it holds a safety-evaluated desired-fan target, and the
    #: controller's own single per-interval post-FC write applies
    #: ``(this tick's computed heat, that desired fan)`` together, so exactly
    #: one write (and one rate-limit slot) is consumed per tick. This field is
    #: never consulted anywhere in that path. Kept (not removed) so an
    #: existing config file/env var setting it does not fail validation; a
    #: follow-up may retire the field outright once the config-UI surface is
    #: updated to match.
    fan_percent: int = Field(default=40, ge=0, le=100)
    #: The control-loop cadence in seconds. Default 5.0 — matches
    #: ``ControllerConfig.post_fc_min_consult_interval_seconds`` and the D36
    #: post-FC advisory cadence, so the loop judges RoR trajectory across a
    #: deliberate dwell rather than chasing per-tick (1 s) thermocouple noise.
    control_interval_seconds: float = Field(default=5.0, gt=0)
    #: The EMA smoothing weight applied to the newest RoR sample (0 < α ≤ 1).
    #: Default 0.4 — a moderate smoothing factor; α=1.0 disables smoothing
    #: (each sample fully replaces the estimate), used as the no-smoothing
    #: comparison case in tests. Lower values smooth harder but lag more.
    ror_smoothing_alpha: float = Field(default=0.4, gt=0, le=1.0)
    #: The ceiling-guard drop's OWN master flag (D88 amendment A2, #405 Slice
    #: C2) — deliberately SEPARATE from ``enabled`` above (the RoR-taper
    #: loop's flag). ``False`` (default) keeps today's incumbent behaviour
    #: byte-for-byte unchanged: the 196 °C boundary is owned solely by the
    #: advisor's own judgment, as it is today. The guard is a SAFETY ANCHOR,
    #: not a taper feature (D88 amendment A1) — when this flag is ``True`` it
    #: fires in DEVELOPMENT regardless of the RoR-taper ``enabled`` flag or
    #: whether the current DEVELOPMENT dwell was reached via the true FC edge
    #: (i.e. it also fires after an operator resume out of recovery, where the
    #: taper loop stays inert) — a taper-gated guard would leave every
    #: taper-flag-OFF roast, and every post-recovery resume, with NO
    #: deterministic bitter-line protection. The operator's stated intent is
    #: to flip this ``True`` at the supervised validation roast — a CONSCIOUS,
    #: separately reviewed incumbent-behaviour change, never a silent rider
    #: bundled with the taper flag.
    ceiling_guard_drop_enabled: bool = Field(default=False)
    #: The bean-temperature ceiling (Celsius) the guard drops at, D88
    #: amendment A1. Default 196.0 — the operator's empirical bitter-ceiling
    #: value (mirrors ``SafetyLimits.bitter_ceiling_temp_c``'s default; kept
    #: as an independent field here, not a cross-reference, so this model
    #: stays self-contained and testable without importing ``SafetyLimits``).
    #: :class:`AppConfig`'s cross-section validator enforces
    #: ``ceiling_guard_temp_c < safety.emergency_drop_temp_c`` AND
    #: ``ceiling_guard_temp_c <= safety.bitter_ceiling_temp_c`` — a guard
    #: configured ABOVE the emergency-drop net, or above the bitter ceiling
    #: it is meant to anchor, is unconstructible.
    ceiling_guard_temp_c: float = Field(default=196.0, gt=0)

    @model_validator(mode="after")
    def _check_heat_range(self) -> "PostFirstCrackControl":
        """The heat floor must not exceed the heat ceiling.

        A floor above its ceiling would be an empty box — no heat value could
        ever satisfy both bounds, so the loop's output clamp
        (:meth:`~roastpilot_agent.post_fc_control.PostFcRorController.compute`)
        would have no valid range to clamp into.

        Returns:
            The validated control-parameters instance.

        Raises:
            ValueError: If ``heat_floor_percent`` exceeds ``heat_ceiling_percent``.
        """
        if self.heat_floor_percent > self.heat_ceiling_percent:
            raise ValueError(
                "heat_floor_percent must not exceed heat_ceiling_percent "
                f"({self.heat_floor_percent} > {self.heat_ceiling_percent})"
            )
        return self

    @model_validator(mode="after")
    def _check_taper_range(self) -> "PostFirstCrackControl":
        """The taper's end setpoint must not exceed its start-max cap (D88).

        The taper decays from ``r0 = clamp(ror_at_engagement,
        taper_end_ror_c_per_min, taper_start_max_ror_c_per_min)`` down to
        ``taper_end_ror_c_per_min``. If the end value exceeded the start-max
        cap the clamp would have no valid range to land ``r0`` in — the same
        empty-box failure :meth:`_check_heat_range` guards against for the
        heat bounds.

        Returns:
            The validated control-parameters instance.

        Raises:
            ValueError: If ``taper_end_ror_c_per_min`` exceeds
                ``taper_start_max_ror_c_per_min``.
        """
        if self.taper_end_ror_c_per_min > self.taper_start_max_ror_c_per_min:
            raise ValueError(
                "taper_end_ror_c_per_min must not exceed taper_start_max_ror_c_per_min "
                f"({self.taper_end_ror_c_per_min} > {self.taper_start_max_ror_c_per_min})"
            )
        return self


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
    #: The deterministic anticipatory heat-trim (late Maillard → FC, D35 §3, #327).
    #: When its window is open the policy lowers the pre-FC heat floor + target to
    #: the trim level; outside the window (or when FC-ETA is unknown) it falls
    #: closed to the flat ``heat_target_percent`` / ``fan_target_percent`` floor.
    #: The trim's ``trim_heat_percent`` must not exceed ``heat_target_percent``
    #: (the trim only ever lowers heat — a validator pins this).
    late_maillard_trim: LateMaillardTrim = Field(default_factory=LateMaillardTrim)

    @model_validator(mode="after")
    def _check_fan_ceiling(self) -> "PreFirstCrackLevers":
        """The fan ceiling must not sit below the fan target, and the trim must
        not raise heat above the flat-floor target.

        A fan ceiling below the target would make the deterministic fan write
        fall outside its own box (the gate would clamp the policy's own target),
        breaking told == enforced for the deterministic path. The
        ``late_maillard_trim`` heat must stay <= the flat-floor
        ``heat_target_percent`` so the trim is a strict REDUCTION (#327): it
        never raises heat above the floor and so can never delay FC by adding
        heat — the floor stays the always-on guarantee that FC arrives (§8.4).

        For the adaptive trim (#386), ``max_trim`` — the shallowest depth the
        adaptive formula can produce — must also stay <= ``heat_target_percent``
        so that adaptive depth is always a strict reduction at its SHALLOWEST
        end: even when both gain terms are zero the formula cannot produce a heat
        level above the pre-FC floor target.

        Returns:
            The validated levers instance.

        Raises:
            ValueError: If ``fan_ceiling_percent`` is below ``fan_target_percent``,
                the fixed trim heat exceeds ``heat_target_percent``, or the
                adaptive ``max_trim`` exceeds ``heat_target_percent``.
        """
        if self.fan_ceiling_percent < self.fan_target_percent:
            raise ValueError(
                "fan_ceiling_percent must not be below fan_target_percent "
                f"({self.fan_ceiling_percent} < {self.fan_target_percent})"
            )
        if self.late_maillard_trim.trim_heat_percent > self.heat_target_percent:
            raise ValueError(
                "late_maillard_trim.trim_heat_percent must not exceed "
                "heat_target_percent (the trim only lowers heat) "
                f"({self.late_maillard_trim.trim_heat_percent} > {self.heat_target_percent})"
            )
        # Scoped to adaptive-enabled configs (#386 Augment medium): when the
        # adaptive depth is OFF, max_trim is unused (depth_for returns the fixed
        # trim_heat_percent, whose own ≤ heat_target_percent check above is the
        # disabled-path guarantee), so a profile/learned plan that lowers
        # heat_target_percent below the default max_trim (75) must not be rejected
        # for a field it never reads.
        if (
            self.late_maillard_trim.adaptive_depth_enabled
            and self.late_maillard_trim.max_trim > self.heat_target_percent
        ):
            raise ValueError(
                "late_maillard_trim.max_trim must not exceed heat_target_percent "
                "when adaptive_depth_enabled (adaptive depth is always a strict "
                "reduction, even at its shallowest) "
                f"({self.late_maillard_trim.max_trim} > {self.heat_target_percent})"
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
    # Deterministic post-first-crack RoR-target PI control-loop parameters
    # (D82/D83, #405 Slice B). INERT today (#405 Slice B1): nothing in
    # controller.py / safety.py / control_policy.py reads this yet — Slice B2
    # wires it in behind the ``enabled`` flag (default False, byte-for-byte
    # today's advisor-driven post-FC behaviour unchanged). Parameterised
    # factory, not a bare model default, per the repo's pyright-strict
    # typed-default idiom (mirrors ``pre_first_crack_levers`` above).
    post_first_crack_control: PostFirstCrackControl = Field(default_factory=PostFirstCrackControl)
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
    # Drying-end landmark (#351): the bean temperature at the drying→browning
    # boundary, recorded once pre-FC as a DISPLAY/observability signal (the chart
    # dry-end marker + the persisted timeline). Default 150.0 °C is .alog-validated
    # against the operator's 47 annotated Artisan Hottop roasts — every one carries
    # a computed Dry-End bean temp (``computed.DRY_BT``) tightly clustered: median
    # 149.0, mean 150.5, min 144.7, max 169.0 (lone outlier), σ≈4.9. The agent's own
    # probe is on the SAME scale: the 7 Jun live roasts cross 150 °C at +352 s /
    # +366 s post-charge (cleanly pre-FC, ~170-190 s before FC), with FC at bean
    # 178-181 °C and drop at 197 °C — matching the operator's empirical profile and
    # the .alog FC/drop distribution. Observability only: it is NEVER fed to the
    # advisor or any safety/control path (it is emitted as an SSE event + persisted
    # to the timeline, NOT recorded as an advisor-facing ``RoastMilestone``).
    drying_end_bean_temp_c: float = Field(default=150.0, gt=0)
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
    # (roastpilot_agent.coherence.evaluate_lever_coherence).
    #
    # Default 10 — TUNED from the operator's own post-FC behaviour on the 17
    # known-good medium Artisan roasts (#277, scripts/deadband_tune.py;
    # docs/advisor/deadband-tuning-2026-06-21.md). The Hottop's levers are
    # quantised to 10 pp, so EVERY one of the operator's real post-FC reversals is
    # >= 10 pp (heat reversals span 10-50 pp, fan 10-50 pp; the smallest observed
    # is 10). With the gate's strict ``abs(delta) < threshold`` test, a 10 pp
    # reversal passes at threshold 10 (10 < 10 is false) but is damped at 11+; so
    # 10 is the LARGEST value that damps ZERO of the operator's real intentional
    # moves while still catching any sub-10-pp (sub-granularity) jitter. The prior
    # placeholder 15 would have damped 13 heat + 4 fan real operator reversals —
    # exactly the intentional decisive moves the gate must let through (D35 §1).
    # NB: because the operator's intentional reversals and the #218
    # 30<->40<->30 twiddle are BOTH 10 pp on this roaster, a pure-magnitude
    # deadband at 10 pp granularity cannot separate them; the gate's #276
    # direction-advancing oscillation damping (a repeated alternation keeps
    # re-reversing and stays damped) is what bounds the #218 staircase, not this
    # magnitude floor. Config-overridable; a literal-free named constant the gate
    # reads.
    post_fc_deadband_threshold_percent: int = Field(default=10, ge=1, le=100)
    # ``post_fc_min_confidence`` — the advisor confidence floor below which a
    # post-FC recommendation is treated as "I don't know" and fails closed to a
    # deterministic HOLD (no actuation), alongside the silent/slow/error/rejected
    # paths (#276). Default 0.2 — a near-zero-confidence move holds; legitimate
    # advice (the FakeAdvisor scripts at 0.9) passes. Tuned on the replay harness
    # (#277). Set 0.0 to disable the floor.
    post_fc_min_confidence: float = Field(default=0.2, ge=0.0, le=1.0)
    # ``drop_dev_margin_percent`` — the deterministic DROP COHERENCE GUARD (#312).
    # The advisor may FABRICATE a development number to justify an irreversible
    # drop (the first supervised roast: the model asserted "14 %" when the system's
    # true development was ~5.4 %, and dropped the beans early on that invented
    # figure). The drop is irreversible, so the controller cross-checks the model's
    # ``should_drop=true`` against the SYSTEM's real, computed development percent
    # (``_development_percent``, charge/FC-referenced) — never the model's claimed
    # number. A drop is HONOURED only when
    #   development_percent >= target_development_percent - drop_dev_margin_percent.
    # Below that the advisor's drop is REJECTED (recorded, surfaced as a note);
    # the same consult's heat/fan advice still applies. This gates the ADVISOR
    # drop only — the operator's manual DROP BEANS is an operator action through a
    # separate path and is never gated; e-stop and the safety box are unaffected.
    #
    # Default 3.0 percentage points — a small tolerance below the profile target
    # so a drop that is genuinely "within the window" (a percentage point or two
    # short of target, the normal operator judgement) still goes through, while a
    # drop materially short of target (the fabricated-"we're done" failure, which
    # was ~7-8 pp below the ~13 % target) is blocked. Config-overridable; a
    # literal-free named constant the controller's drop guard reads.
    drop_dev_margin_percent: float = Field(default=3.0, ge=0.0, le=100.0)

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
    outcome. The #277 post-FC control bake-off (21 Jun 2026 — gpt-4o vs the
    prior winner and fast control candidates, scored on the post-FC DEVELOPMENT
    scope across 17 known-good mediums, 2 seeds) pinned ``openai/gpt-4o`` via
    OpenRouter: it tracks the operator's real heat moves the closest (heat MAE
    ~7.5 pp vs ~22 for gemini-3.1-flash-lite and ~31 for gemini-3-flash-preview),
    has the best heat-direction agreement (~0.78), a reliable drop (F1 ~0.86),
    live-viable latency (~2.0 s), and is the proven n8n control model (D40.4).
    The prompt is ``c1`` (#274 / D39.1): the AS-BUILT control teaching SYSTEM
    frame, wired live for the post-FC loop (#277) — the per-tick #275 context is
    the user message. See ``docs/advisor/bakeoff-results-2026-06-21.md``. To run a
    model on its native provider (no OpenRouter hop/markup, per D18), set
    ``provider`` + the matching ``api_key_env``. ``OPENROUTER_API_KEY`` must be
    set in the environment at runtime; ``FakeAdvisor`` stays the test/CI default.

    Per-phase model selection (#173): ``model_slug`` is the base/default slug
    (the identity in the decision-trace descriptor and the reachability probe),
    and ``model_slug_by_phase`` is an optional per-phase override map resolved
    by :meth:`model_for`. By default every phase resolves to
    ``DEFAULT_ADVISOR_MODEL`` — gpt-4o everywhere (#277 PIN); under D35 only the
    post-FC DEVELOPMENT phase actually consults the advisor — so the map is
    retained additive plumbing with zero behavior change; a future re-run could
    flip a phase slot to a different model. A phase absent from the
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
    # c3 (#274 + roast-2 + roast-3 tuning): the control teaching SYSTEM frame for
    # the post-FC control loop, wired live here. It is the whole-machine teaching
    # frame (told == enforced: every numeric limit comes from the live
    # AdvisorContext / #273 policy, it names no thresholds, and it makes acting
    # pre-FC wrong, not merely named). c2 = c1 PLUS a post-FC development-stretch
    # section after roast 2 (run c3b84625) showed the advisor riding a mid heat
    # level so the bean raced from first crack to the ceiling (dev only 1:09, DTR
    # 11.6 %, dropped slightly dark at 196). c3 = c2 PLUS a post-FC fan-as-active-
    # brake section after roast 3 (Ethiopia Koke) showed the advisor holding fan at
    # 30-40 while it cut heat to 0, so the bean coasted 193->203 (8 C past the 195
    # ceiling) with no brake left. c1/c2 stay selectable for an A/B; the #277 bake-
    # off was scored under c1. The per-tick #275 context is the user message. The
    # literal "c3" (not an import) keeps config free of an advisor->config import
    # cycle; a test pins it equal to advisor.CONTROL_TEACHING_PROMPT_VERSION so the
    # two can never drift. The model default stays gpt-4o (DEFAULT_ADVISOR_MODEL) —
    # this is a prompt change only.
    prompt_version: str = Field(default="c3", min_length=1)
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
        default map (gpt-4o for every phase, #277 PIN) this always resolves to
        :attr:`model_slug`, so per-phase selection is a behavioral no-op until a
        future re-run populates the map with a different per-phase model.

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


class MCPDeviceConfig(BaseModel):
    """Device-level MCP child configuration managed by the Config UI (D78-4, #420).

    These fields are rendered into the ``coffee-roaster-mcp.yaml`` via a
    passthrough-merge on every (re)spawn (see
    :mod:`roastpilot_agent.mcp_yaml`).  Only non-``None`` fields are written
    — a ``None`` value means "keep whatever the operator's hand-authored yaml
    says" so that unmanaged tuning (pinned model revision, Mac ``onnx_threads``,
    Pi-vs-Mac profile) survives a render unchanged.

    Temperature values are Celsius everywhere; the MCP child's
    ``temperature_unit`` setting is not managed here (it is fixed
    per-installation in the hand-authored yaml).

    Attributes:
        serial_port: The serial port device path for the Hottop roaster
            (e.g. ``/dev/cu.usbserial-XXXXXXXX`` on macOS,
            ``/dev/ttyUSB0`` on Linux). Maps to ``roaster.port`` in the
            MCP yaml.
        roaster_driver: The coffee-roaster-mcp driver name
            (e.g. ``hottop_kn8828b_2k_plus`` or ``mock``). Maps to
            ``roaster.driver`` in the MCP yaml.
        audio_input_device: PortAudio input device name substring (matched
            case-insensitively; e.g. ``"USB PnP"``). Maps to
            ``audio.input_device`` in the MCP yaml.
        recording_enabled: Whether the MCP audio recorder is active.  Maps
            to ``recording.enabled``.
        recording_autocapture: Whether recording starts automatically with
            each roast session.  Maps to ``recording.autocapture``.
        recording_devices: Optional ordered list of capture device-name
            substrings.  The first entry is the FC detector's device (teed,
            no second open); additional entries are independent capture
            streams.  Maps to ``recording.devices`` in the MCP yaml.
        fc_mode: First-crack detection mode: ``"disabled"``, ``"audio"``,
            or ``"manual"``.  Maps to ``first_crack.mode``.
        fc_confidence_threshold: Detector confidence threshold in ``[0, 1]``.
            Maps to ``first_crack.confidence_threshold``.
        auto_t0_detection_enabled: Whether the MCP's automatic charge-drop
            (T0) detection is active.  Maps to
            ``session.auto_t0_detection_enabled``.
        auto_t0_drop_threshold_c: The bean-temperature drop (°C) that
            triggers automatic T0 detection.  Maps to
            ``session.auto_t0_drop_threshold_c``.
        mcp_yaml_source_path: Path to the operator's hand-authored
            ``coffee-roaster-mcp.yaml``.  When set, the passthrough-merge
            reads this file as the base before overlaying the managed fields.
            When ``None``, only the managed fields are written (the MCP child
            fills the rest from its own defaults).
        ambient_mode: Ambient environmental sensor mode: ``"disabled"`` or
            ``"yoctopuce"`` (D85, #342/#474). Maps to ``ambient.mode`` in the
            MCP yaml. ``None`` (default) means "not managed" — inherit
            whatever the hand-authored yaml says (or the MCP's own
            ``disabled`` default). Applies next-roast via the between-roasts
            MCP respawn, same as the serial/audio/FC device fields.
        ambient_device: Optional Yoctopuce module serial number or logical
            name to target a specific probe. Maps to ``ambient.device`` in
            the MCP yaml. ``None`` means "not managed / inherit" (tri-state,
            matching the #439 inherit-vs-override convention for the other
            optional string device fields); an empty string is treated the
            same as ``None`` at the PUT/merge layer (the blank-string guard).
        ambient_poll_interval_seconds: Minimum seconds between ambient USB
            reads. Maps to ``ambient.poll_interval_seconds`` in the MCP yaml.
            ``None`` means "not managed / inherit" — the MCP's own default
            (30.0 s) or the hand-authored yaml's value governs.
    """

    serial_port: str | None = None
    roaster_driver: str | None = None
    audio_input_device: str | None = None
    recording_enabled: bool | None = None
    recording_autocapture: bool | None = None
    recording_devices: tuple[str, ...] | None = None
    fc_mode: Literal["disabled", "audio", "manual"] | None = None
    fc_confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    auto_t0_detection_enabled: bool | None = None
    auto_t0_drop_threshold_c: float | None = Field(default=None, gt=0)
    mcp_yaml_source_path: Path | None = None
    # Ambient environmental sensor fields (D85, #342/#474). Device/hardware
    # config, not safety — editable via PUT /api/config (D78 excludes only
    # SafetyLimits). Rendered into the MCP yaml's ``ambient:`` section by
    # mcp_yaml.py::_device_config_to_overlay; applied next-roast via the
    # existing _respawn_mcp_for_device_config drift check (api.py), which
    # compares the whole MCPDeviceConfig by equality so these fields are
    # covered automatically.
    ambient_mode: Literal["disabled", "yoctopuce"] | None = None
    ambient_device: str | None = None
    ambient_poll_interval_seconds: float | None = Field(default=None, gt=0)


#: The bare console-script name of the coffee-roaster-mcp child (the default
#: ``MCPConfig.command``). It is a *constant*, not just a Field default, so the
#: spawn-hardening in ``mcp_client.build_server_parameters`` can recognise the
#: "operator left it at the default" case and resolve the script to the agent's
#: OWN environment (next to ``sys.executable``) rather than letting a bare-PATH
#: lookup pick up a foreign install. See that method for the why (a homebrew
#: coffee-roaster-mcp with stale, mismatched deps segfaulted a live roast).
DEFAULT_MCP_COMMAND = "coffee-roaster-mcp"


class MCPConfig(BaseModel):
    """coffee-roaster-mcp child-process settings (D6, E5-S2).

    - ``command`` + the fixed ``serve`` positional form the spawn argv
      (`coffee-roaster-mcp serve`, matching server.json packageArguments).
      When left at the ``DEFAULT_MCP_COMMAND`` default, the client resolves it
      to the in-venv console script before spawning (see
      ``mcp_client.build_server_parameters``); an explicit override is spawned
      verbatim.
    - ``call_timeout_seconds`` 5.0: every MCP call — including
      ``emergency_stop`` — must raise rather than stall the tick loop
      (safety-reviewer carry-forward, E4-S2). Five seconds ≈ five stalled
      ticks worst case before the typed failure surfaces and the
      consecutive-failure rules take over; far below any human reaction
      window, far above any healthy stdio round trip.
    - ``startup_timeout_seconds`` 15.0: the bootstrap-safe mock server
      starts in well under a second; 15 s tolerates first-run environment
      slowness without masking a wedged child.
    - ``stop_timeout_seconds`` 10.0: the ceiling on graceful child teardown
      (``MCPServerProcess.stop``). The MCP SDK's own stdin-close → wait →
      SIGTERM → SIGKILL sequence is bounded by a 2 s grace, but the agent
      must not depend on that internal escalation: a wedged native child
      (blocked PortAudio read) or a task group still awaiting an open pipe
      can stall ``aclose`` indefinitely (#212, bit roast 3). When this
      bound trips, the agent force-terminates the child process group and
      exits anyway — a hung shutdown otherwise drives the operator to
      ``kill -9``, the one uncatchable path that leaves the roaster
      commanded-hot. 10 s clears the SDK's 2 s with margin.
    """

    command: str = Field(default=DEFAULT_MCP_COMMAND, min_length=1)
    call_timeout_seconds: float = Field(default=5.0, gt=0)
    startup_timeout_seconds: float = Field(default=15.0, gt=0)
    stop_timeout_seconds: float = Field(default=10.0, gt=0)
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
    mcp_device: MCPDeviceConfig = Field(default_factory=MCPDeviceConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @model_validator(mode="after")
    def _check_ceiling_guard_within_safety_bounds(self) -> "AppConfig":
        """The post-FC ceiling guard must sit under BOTH safety-owned bounds (D88 A1).

        This is the first cross-SECTION validator on :class:`AppConfig` (the
        per-section validators like :meth:`PostFirstCrackControl._check_taper_range`
        or :meth:`SafetyLimits._check_drop_ceiling_order` only see their own
        section's fields; ``ceiling_guard_temp_c`` lives on
        ``controller.post_first_crack_control`` while the two bounds it must
        respect live on ``safety``, so the check can only run here, where both
        sections are visible on ``self``).

        A guard configured AT OR ABOVE ``safety.emergency_drop_temp_c`` would
        never fire before the hard emergency-drop bound already forced the
        issue — the guard would be a dead letter, defeating the point of an
        *earlier*, deterministic bitter-line anchor. A guard configured ABOVE
        ``safety.bitter_ceiling_temp_c`` would let the roast run hotter than
        the very bitter ceiling the guard exists to anchor before the guard
        even engages — silently weakening the incumbent bitter-ceiling
        protection the moment the flag is flipped on. Both are BLOCKER-class
        misconfigurations (safety-reviewer ratification) — unconstructible,
        not merely logged.

        Returns:
            The validated application config.

        Raises:
            ValueError: If ``controller.post_first_crack_control.ceiling_guard_temp_c``
                is not strictly below ``safety.emergency_drop_temp_c``, or
                exceeds ``safety.bitter_ceiling_temp_c``.
        """
        guard_temp_c = self.controller.post_first_crack_control.ceiling_guard_temp_c
        if guard_temp_c >= self.safety.emergency_drop_temp_c:
            raise ValueError(
                "controller.post_first_crack_control.ceiling_guard_temp_c must be below "
                f"safety.emergency_drop_temp_c ({guard_temp_c} >= "
                f"{self.safety.emergency_drop_temp_c})"
            )
        if guard_temp_c > self.safety.bitter_ceiling_temp_c:
            raise ValueError(
                "controller.post_first_crack_control.ceiling_guard_temp_c must not exceed "
                f"safety.bitter_ceiling_temp_c ({guard_temp_c} > "
                f"{self.safety.bitter_ceiling_temp_c})"
            )
        return self
