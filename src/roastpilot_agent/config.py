"""Typed application configuration (component plan §4; orchestration plan
§ Configuration Model).

Finalized at E2-S3. Controller timing defaults are the documented
hardware-aligned values from the orchestration plan; safety limits are
deliberately conservative software ceilings pending supervised hardware
validation at E12 (E12-S1).
"""

import math
from pathlib import Path
from typing import Annotated, ClassVar, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import RoastPhase

# Phase-keyed advisor consult floors — advisor cadence scales with first-crack
# proximity (D32 / #191, refining #171). The advisor is consulted where it adds
# optimization judgment, which ramps toward FC:
#   - preheating: OFF — preheat is NOT an automatic-advice phase (see
#     ``AUTO_ADVICE_PHASES`` in controller.py). Reaching the charge band is a
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
# the base slug, and — with ``model_slug_by_phase`` empty by default (D151) — the
# model every advice call resolves to until the operator changes it. The pin is a
# DEFAULT, not a lock: D43/D73 and the #396 A/B both schedule hardware arms on
# other models, which a lock would forbid.
DEFAULT_ADVISOR_MODEL = "openai/gpt-4o"

# One Hottop fan level, in normalized percentage points (#709 / D126). The
# roaster's fan is a 0-10 INTEGER scale and the driver quantises every command
# with ``(value + 5) // 10``
# (``coffee_roaster_mcp.drivers._percent_to_hottop_fan_scale``), so 10 pp is the
# granularity at which a told fan number and the physical move can agree. The
# same fact ``ControllerConfig.post_fc_deadband_threshold_percent`` (also 10)
# already encodes; named here so the ambient doctrine's step bound can be
# validated against it rather than re-deriving the constant.
HOTTOP_FAN_LEVEL_PP = 10.0

# The MCP's OWN default ambient poll cadence, mirrored (#732). The agent never
# sets ``mcp_device.ambient_poll_interval_seconds`` by default — it leaves the
# field ``None`` and the MCP applies
# ``coffee_roaster_mcp.config.AmbientConfig.poll_interval_seconds``. That number
# is what decides how old a HEALTHY reading routinely gets, so the ambient
# doctrine's freshness bound has to be validated against it even when the
# operator has set nothing. Mirrored rather than imported to keep config
# construction free of an MCP import; a contract test asserts the two agree, so
# a bump that changes the cadence fails loudly here instead of silently
# narrowing the freshness margin.
DEFAULT_MCP_AMBIENT_POLL_INTERVAL_SECONDS = 30.0

# Advisor slugs whose post-FC advice latency has been MEASURED against the ~5 s
# first-crack-slot gate, split by what the measurement said (D151, #747). Both
# sets are ADVISORY DISPLAY DATA for the pre-charge banner and carry no runtime
# authority whatsoever: nothing rejects, clamps, or substitutes a model on the
# strength of them. A slug in neither set has no screen on record — which the
# banner says, rather than implying it is either safe or bad.
#
# Why a warning and not a gate (the D151 sub-decision): until #747 the fully
# populated per-phase map made a tick-busting model unreachable by accident, and
# making ``model_slug`` effective removes that. A runtime allow-list was
# rejected — it would go stale the day a model ships (gpt-5.6-luna would have
# needed a code change before it could be tried), rejects at config-save time
# far from the roast, and implies a June latency screen on one OpenRouter
# account still holds.
#
# What a slow model actually costs, stated precisely (safety-reviewer finding,
# folded pre-open — an earlier draft of this note claimed a slow model "cannot
# stall the loop", which is FALSE and was the sentence the reader would rely
# on). ``controller.tick`` awaits ``_maybe_run_advisory`` INLINE as its last
# statement, and the serve loop is drain-operator-queue -> tick, so a call that
# takes N seconds delays the next telemetry read, the next ``_evaluate_safety``
# (including the hard bean-temp ceiling), and the next drain of the operator
# queue — which is where the in-UI EMERGENCY STOP is consumed. N is bounded by
# ``ControllerConfig.advisory_timeout_seconds``, not unbounded, and any failure
# still falls back fail-closed to hold-current-targets with D30's
# consecutive-failure stop behind it. So the true statement is: a slow model
# DELAYS the control loop by up to that bound and degrades advice at the drop;
# it cannot hang it forever and it cannot actuate anything. Ctrl-C at the
# launcher is unaffected (it calls the controller directly). This exposure is
# pre-existing — it was ~2 s while the accidental pin held gpt-4o — and making
# ``model_slug`` effective is what puts a 6-10 s model within operator reach,
# which is precisely why the banner names the bound.
#
# An ARM, not a slug, is the unit of measurement — ``(model_slug,
# reasoning_effort)`` (local Codex P2 + safety-reviewer, both folded pre-open).
# Keying on the slug alone INVERTS recorded evidence in at least one real case:
# ``openai/gpt-5.5`` busts the gate at the provider default (10.8 s on 8 Jun,
# 7.17/8.58 s in D40/D41) but was measured at **2.9 s, passing**, with
# ``reasoning_effort="off"`` — a configuration
# ``docs/advisor-bakeoff-2026-06-08.md`` explicitly documents as a speed/cost
# alternative. A slug-keyed set would print "BUSTED" over the operator's own
# measured-passing arm, which is worse than silence: it teaches them the warning
# is noise. The effort key is the raw ``AdvisorConfig.reasoning_effort`` value,
# ``None`` meaning the provider default (itself a measured condition, not an
# absence).
#
# The ENDPOINT is the third dimension and is handled in ``launch_banner``
# instead of being repeated on every row: every screen below ran on OpenRouter
# via ``provider="openai_compatible"``, so any other provider or base URL voids
# the whole table rather than matching a row.
#
# PROMPT VERSION is deliberately NOT a key dimension, and this is the scope
# boundary — the dimensions stop here (local Codex P1, considered and declined
# with reasons, so a later round does not relitigate it). Prompt size IS a
# latency input: 8 Jun measured v1 -> v2 (~1,100 -> ~1,800 chars) adding ~1-2 s
# and pushing borderline frontier models over the gate. But that finding is
# about moving from a TOY prompt to a production-representative one, and it
# predates this table: D40/D41 ran at ``v4`` and #396 at ``c3``, both
# production-weight, as is every live ``c``-series prompt the operator selects.
# Keying on the prompt anyway would leave the table matching nothing, so the
# banner would warn on the proven baseline arm of every ordinary roast — the
# cry-wolf failure that costs exactly the case this warning exists for, and the
# reason a hard guard was rejected in the first place. What IS true and worth
# knowing: these are MODEL-level indicators measured under a production-weight
# prompt, not a certification of the operator's exact configuration, and a
# materially heavier future prompt would move them — most of all for
# ``claude-haiku-4.5``, the one entry with no margin (~4.1 s against ~5 s, and
# 4.3-5.5 s straddling the gate on 8 Jun's heavier prompt).
#
# Sources — numbers live in the reports, not here, so this table cannot drift
# into a stale citation: ``docs/advisor/bakeoff-summary-2026-06-16.md`` (D40/D41,
# 8 models x 28 roasts, median/max against the ~5 s gate — the authoritative
# screen for this gate), #396's 16 Jul screens (gpt-5.6-luna, gpt-4.1-mini), and
# ``docs/advisor-bakeoff-2026-06-08.md`` for the reasoning-off arms. Note the
# 8 Jun run scored against a LOOSER gate (it passes sonnet at 9.1 s and opus at
# 6.2 s), so only its reasoning-off arms are carried here; the ~5 s
# classification is D40/D41's.
#
# Slugs are lower-cased at definition and the two tables are asserted disjoint
# by test: ``launch_banner`` matches them lower-case, so a mixed-case entry
# added later would silently fall through to "no screen on record" — a false
# NEGATIVE, the one direction that matters here.
FC_LATENCY_SCREENED_ADVISOR_ARMS: dict[tuple[str, str | None], float] = {
    # D40/D41 (16 Jun), provider-default reasoning: cleared comfortably.
    # Values are the recorded WORST call (max), not the median — the median is
    # what the roster screen selected on, the max is what a hard per-call
    # timeout actually meets.
    ("google/gemini-3.1-flash-lite", None): 1.44,
    ("openai/gpt-4o-mini", None): 2.39,
    ("openai/gpt-4o", None): 3.73,
    # D40/D41: "✅ (tight)" in the report — 4.10 s median / 4.59 s max.
    ("anthropic/claude-haiku-4.5", None): 4.59,
    # #396 (16 Jul): 1.89/2.46 s on the dedicated screen, 3.76/4.52 s over the
    # 28-roast run.
    ("openai/gpt-5.6-luna", None): 4.52,
    # #396 (16 Jul): 2.62/4.39 s on the dedicated screen, 2.94/5.05 s over 28
    # roasts. The median sits well inside a 5 s bound; the max does not.
    ("openai/gpt-4.1-mini", None): 5.05,
    # 8 Jun: reasoning OFF turns the same model from 10.8 s into 2.9 s.
    # The one arm on this table whose base slug busts at the default.
    ("openai/gpt-5.5", "off"): 2.9,
}

#: The headroom an arm needs under ``ControllerConfig.advisory_timeout_seconds``
#: before the banner stays silent about it. An arm whose recorded worst call
#: leaves less than this fraction of the bound spare is reported as tight rather
#: than cleared.
#:
#: Derived rather than hardcoded (local Codex P2, folded pre-open): tightness is
#: a relation between a measurement and the CONFIGURED bound, not a property of
#: the model. A static partition was silently wrong the moment an operator moved
#: ``ROASTPILOT_CONTROLLER__ADVISORY_TIMEOUT_SECONDS`` — it stayed quiet about
#: gpt-4o under a 1 s bound, and still cried timeout for gpt-4.1-mini under a
#: 10 s one, which its 5.05 s worst call comfortably fits.
FC_LATENCY_TIGHT_HEADROOM_FRACTION = 0.2

#: Arms whose recorded screen BUSTED the gate — the reasoning / frontier models
#: that think past the wall (D40/D41). Named explicitly so the banner can say
#: "measured, and it busts" instead of the much weaker "no screen on record".
FC_LATENCY_BUSTED_ADVISOR_ARMS: frozenset[tuple[str, str | None]] = frozenset(
    (slug.lower(), effort)
    for slug, effort in {
        # The #277 brief pins this one to ``low``; that is the arm measured, and
        # 8 Jun found reasoning cannot be disabled on it at all (a 400 from the
        # endpoint), so no reasoning-off escape exists for it.
        ("openai/gpt-5-mini", "low"),
        ("anthropic/claude-opus-4.8", None),
        ("openai/gpt-5.5", None),
        # Structural generation latency, not reasoning: 8 Jun found Anthropic
        # models emit zero reasoning tokens, so reasoning-off is a no-op here
        # and there is no faster arm of this model to record.
        ("anthropic/claude-sonnet-4.6", None),
    }
)

#: The canonical OpenRouter API base URL — :attr:`AdvisorConfig.provider_base_url`'s
#: own default, and the single source of truth
#: ``bean_sourcing._resolve_extraction_model_slug`` compares against to tell
#: an actual OpenRouter endpoint apart from an arbitrary OTHER
#: OpenAI-compatible endpoint (a local server, LiteLLM, etc. — ``provider ==
#: "openai_compatible"`` covers ALL of those, not just OpenRouter, #590 P2
#: fix). Kept here (not duplicated) so both call sites — and any future
#: one — stay in lockstep with this one literal.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


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
    #:
    #: **Post-FC authority coupling (D156).** When the post-FC loop is enabled
    #: and the trim window is open at first crack, the resolved fixed or
    #: adaptive trim depth is the pre-FC heat carried into
    #: ``PostFcRorController.reset`` and becomes ``heat_engage_percent``. A
    #: lower per-bean ``pre_fc_heat`` can bind below that resolved depth, so the
    #: actual pre-FC heat at FC is the only engagement basis. If the window never
    #: opens or trim is disabled, ``pre_fc_heat`` instead replaces the flat
    #: configured floor and may be higher or lower. The D88 base ceiling is
    #: exactly ``max(1, min(heat_ceiling_percent, heat_engage_percent))``. The
    #: configured D96/D162 recovery term is
    #: ``min(heat_ceiling_percent, heat_engage_percent +
    #: recovery_headroom_percentage_points)``; the active effective ceiling is
    #: ``max(base_ceiling, recovery_term)``, never below the D88 base. With
    #: schema defaults, engagement 65 and headroom 15 give base/recovery ceilings
    #: 65/80; a resolved trim of 60 gives 60/75. With the post-FC loop disabled, post-FC
    #: heat remains advisor-driven and these D88/D96 caps do not apply. D156
    #: documents this inherited authority and defers any independent post-FC
    #: engagement baseline until after the timing A/B; this field creates no
    #: separate authority.
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

    #: The fields consumed ONLY while :attr:`adaptive_depth_enabled` is ``True``
    #: — the #386 formula's inputs plus the #412 damping coefficients. With
    #: adaptive mode off, :meth:`depth_for` returns ``trim_heat_percent``
    #: immediately and the controller's damping is never called, so a
    #: non-default value in this group changes nothing about the roast.
    #: Declared HERE, beside the fields, so a new coefficient joins the group in
    #: the same edit that adds it. Consumer: the roast-live banner (#746)
    #: subtracts this group before calling a fixed-mode trim non-default —
    #: tagging an inert value ⚠ EXPERIMENT would fire the warning on the proven
    #: baseline arm and train the operator to ignore it.
    ADAPTIVE_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "base_trim",
            "k_ror",
            "k_eta",
            "ror_ref",
            "eta_ref",
            "min_trim",
            "max_trim",
            "trim_depth_deadband_pp",
            "trim_depth_slew_pp_per_tick",
        }
    )

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
    existing safety gate. Nothing here talks to ``mcp_client`` directly.

    **Correction (D96 safety review, 15 Jul): the sentence this replaces
    ("the 196 °C bitter ceiling keeps clamping the loop's output") was FALSE
    for the command path and is corrected here rather than left to mislead a
    future reader.** ``safety.SafetyPolicy.evaluate_command`` (the SET_HEAT/
    SET_FAN gate every write in this module goes through) is temperature-BLIND
    — it only checks the command rate limit and the heat/fan box; it never
    reads ``bitter_ceiling_temp_c`` or ``emergency_drop_temp_c``. Those two
    values are *told* context only (surfaced in
    :class:`~roastpilot_agent.control_policy.PhaseControlLimits` /
    :class:`~roastpilot_agent.advisor.AdvisorContext`) until something
    ENFORCES them. The only two things that actually stop a bean temperature
    excursion are: (1) ``SafetyPolicy.evaluate_telemetry``'s hard ceiling,
    ``SafetyLimits.max_bean_temp_c`` (default 230 °C — well above 196/198,
    an emergency-stop backstop, not the bitter-line anchor), and (2) THIS
    module's own ``ceiling_guard_drop_enabled`` path (D88 amendment A1,
    :meth:`~roastpilot_agent.controller.RoastController._maybe_ceiling_guard_drop`),
    which is the ONLY code that enforces the 196 °C line as a deterministic
    drop. **D96's bounded-bidirectional heat law (below) therefore REQUIRES
    ``ceiling_guard_drop_enabled=True`` whenever ``recovery_enabled=True``** —
    a cross-field validator on this model rejects the alternative — because a
    law that can raise heat above entry with no deterministic 196 °C anchor
    would leave the bitter line owned solely by the advisor's own judgment,
    exactly the gap D88 already closed once for the taper's steady-state
    case. This requirement does not apply to the RoR-taper/never-add-heat
    path (``enabled`` alone): that law can only ever LOWER the ceiling
    relative to entry, so it never needs the guard to stay safe (though the
    guard defaults on regardless, per D88/D89's own promotion).

    ``enabled`` (the ``post_fc_ror_loop`` master flag) **defaults ``True`` as
    of the 12 Jul promotion (D88/D89, operator-ratified)**: the 11 Jul
    supervised validation roast (runs `d55b0fce`/`edbe9a76`,
    `docs/analysis/2026-07-09-roast9-10-postfc-ab.md`) passed structurally —
    the taper tracked the measured engagement RoR down with no heat rise
    above heat-at-engagement — and the cup scored 9/10 ("like sugar") on
    tasting, the operator's explicit sign-off condition. Every new roast now
    runs the taper by default; a baseline (advisor-driven post-FC) arm still
    exists one launch-line away (``...__ENABLED=false``,
    `scripts/roast-live.sh`) for any future A/B. **A closed loop changes the
    trajectory it is steering, so replay could not validate it — every
    parameter below was hardware-tuned at the validation roast, not
    offline-validated (n=2 going into D88), and stays exactly as tuned there
    through this promotion.** All temperatures are Celsius; RoR is °C/min;
    heat/fan are percentages.
    """

    #: The master flag (``post_fc_ror_loop``). **``True`` as of the 12 Jul
    #: promotion** (D88/D89, operator-ratified on the 11 Jul validation
    #: roast + 9/10 tasting) — every new roast runs the deterministic taper
    #: by default; the advisor-driven post-FC regime from before this flip
    #: is reachable via ``...__ENABLED=false`` for a baseline arm. Slice B2
    #: reads this flag before routing DEVELOPMENT heat through the PI loop
    #: instead of the advisor.
    enabled: bool = Field(default=True)
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
    #: EFFECTIVE ceiling to the actual heat the roast held at FC engagement,
    #: whenever that is lower than this static value, when the post-FC loop is
    #: enabled. With an open trim window, a lower per-bean ``pre_fc_heat`` can
    #: bind that engagement heat; otherwise it replaces the flat floor higher or
    #: lower.
    #: See
    #: ``LateMaillardTrim.trim_heat_percent`` for the pre-FC trim provenance of
    #: that engagement heat when the trim window is open at first crack.
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
    #: loop's flag). **``True`` as of the 12 Jul promotion** (D88/D89,
    #: operator-ratified alongside ``enabled`` above on the same 11 Jul
    #: validation roast + 9/10 tasting sign-off): the 196 °C boundary is now
    #: a deterministic SAFETY ANCHOR by default, not owned solely by the
    #: advisor's own judgment. The guard is a SAFETY ANCHOR, not a taper
    #: feature (D88 amendment A1) — this flag fires in DEVELOPMENT
    #: regardless of the RoR-taper ``enabled`` flag or whether the current
    #: DEVELOPMENT dwell was reached via the true FC edge (i.e. it also
    #: fires after an operator resume out of recovery, where the taper loop
    #: stays inert) — a taper-gated guard would leave every taper-flag-OFF
    #: roast, and every post-recovery resume, with NO deterministic
    #: bitter-line protection. Reachable via ``...__CEILING_GUARD_DROP_
    #: ENABLED=false`` for a baseline arm alongside ``enabled=false`` above —
    #: this was a CONSCIOUS, separately reviewed incumbent-behaviour change
    #: at the flip, never a silent rider bundled with the taper flag.
    ceiling_guard_drop_enabled: bool = Field(default=True)
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
    #: The bounded-bidirectional heat RECOVERY law's master flag (D96, #559).
    #: Default ``False`` — hardware-gated promotion, the identical posture
    #: D88's ``enabled``/``ceiling_guard_drop_enabled`` had before their own
    #: validation roast. When ``True``, the loop may raise heat ABOVE
    #: ``heat_engage_percent`` (relaxing D88's never-add-heat-beyond-entry
    #: clamp) whenever measured RoR runs persistently below the taper
    #: setpoint — roast 15's failure (fan 30→90 crashed RoR while heat sat
    #: ceiling-locked at entry with zero raise authority, dropping 7 °C
    #: short). See :class:`~roastpilot_agent.post_fc_control.PostFcRorController`
    #: for the full entry/exit state machine and the structural
    #: runaway-impossibility argument (the setpoint stays measured-anchored
    #: and monotonically non-increasing; the raise authority is a hard,
    #: error-independent cap — see ``recovery_headroom_percentage_points``).
    #: **REQUIRES ``ceiling_guard_drop_enabled=True``** (a validator below
    #: enforces this): a law that can raise heat above entry with no
    #: deterministic 196 °C anchor would leave the bitter line owned solely
    #: by the advisor's own judgment (see the corrected module docstring
    #: above) — unlike the RoR-taper's own never-add-heat law, which can
    #: only ever lower the ceiling and so carries no such requirement.
    recovery_enabled: bool = Field(default=False)
    #: Enables the experimental post-FC temperature projection recovery path
    #: and its bounded fast-entry floor for both projection and ordinary RoR
    #: recovery triggers. It is opt-in until a supervised roast promotes it.
    recovery_projection_enabled: bool = Field(default=False)
    #: Percentage points beyond the target DTR at which a projected shortfall
    #: may enter recovery.
    recovery_projection_entry_horizon_pp: float = Field(
        default=2.0, gt=0, le=20, allow_inf_nan=False
    )
    #: Percentage points beyond the target DTR that end recovery authority for
    #: this post-FC engagement.
    recovery_projection_cutoff_horizon_pp: float = Field(
        default=5.0, gt=0, le=20, allow_inf_nan=False
    )
    #: Minimum projected temperature shortfall (Celsius) required for entry.
    recovery_projection_margin_c: float = Field(default=3.0, gt=0, allow_inf_nan=False)
    #: One-time heat floor, in percentage points above engagement heat, applied
    #: whenever recovery enters while projection mode is enabled, including an
    #: entry confirmed by the ordinary RoR-error trigger.
    recovery_entry_step_pp: int = Field(default=10, ge=0, le=50)
    #: The RoR shortfall (°C/min) below the taper setpoint that ENTERS
    #: recovery, once sustained for ``recovery_confirm_ticks`` consecutive
    #: computed ticks. Default 1.0 — deliberately the SAME value as
    #: ``ror_deadband_c_per_min``: recovery engages exactly where the
    #: existing deadband already stops treating a shortfall as noise, not at
    #: an independently-chosen threshold that could silently drift out of
    #: sync with it. **Cross-referenced, not shared as one literal**,
    #: because the two fields answer different questions (the deadband gates
    #: the PI's OWN integration; this gates a DIFFERENT law's activation) —
    #: but a future change to one without considering the other would move
    #: where recovery starts relative to "this is real signal." Verified
    #: against roast 12 (run ``edbe9a76``, the validated 9/10 cup): measured
    #: RoR never falls more than ~0.3 °C/min below its own decaying setpoint
    #: at any tick, so this trigger has ~0.7 °C/min of margin on that trace —
    #: not the knife-edge (<0.1 °C/min) margin that falsified D94.
    recovery_trigger_margin_c_per_min: float = Field(default=1.0, ge=0)
    #: The RoR shortfall (°C/min) below the taper setpoint at or under which
    #: recovery EXITS, once sustained for ``recovery_confirm_ticks``
    #: consecutive computed ticks. Default 0.5 — deliberately SMALLER than
    #: ``recovery_trigger_margin_c_per_min`` (a validator enforces
    #: ``exit_margin < trigger_margin``): an EQUAL entry/exit margin
    #: (symmetric hysteresis at exactly the deadband's own edge) is
    #: limit-cycle-prone whenever measured RoR sits near the shared
    #: threshold — a single noisy tick could cross back and forth,
    #: re-triggering entry immediately after an exit. The asymmetric gap
    #: (entry 1.0, exit 0.5) means RoR must recover MORE before recovery
    #: releases than it needed to fall to trigger it, so a trace oscillating
    #: near either threshold in isolation cannot thrash the state — it must
    #: cross the WIDER combined gap, which ordinary tick-to-tick RoR noise
    #: does not span (see the mandatory limit-cycle convergence test in
    #: ``tests/test_post_fc_control.py``).
    recovery_exit_margin_c_per_min: float = Field(default=0.5, ge=0)
    #: Consecutive computed :meth:`~roastpilot_agent.post_fc_control.PostFcRorController.compute`
    #: ticks the entry or exit condition must hold before the state actually
    #: changes. Default 3 (≥15 s at the default 5 s cadence) — long enough to
    #: reject a single noisy RoR sample, short enough that the bean has not
    #: moved far before the loop reacts. **This is a TICK COUNT, not a
    #: wall-clock or monotonic-clock duration** (D95 clock-origin discipline,
    #: #559): it counts consecutive accepted-and-executed
    #: :meth:`~roastpilot_agent.post_fc_control.PostFcRorController.compute`
    #: calls, which only ever happen on the caller's actuation-gated cadence
    #: — never a receive-tick or FC-backdate-derived clock, sidestepping the
    #: whole D94/D95 falsification class by construction. "3 ticks" and
    #: "~15 seconds" are the same thing only while cadence holds at
    #: ``control_interval_seconds``; this field does not itself convert to
    #: seconds, deliberately, so it cannot silently disagree with a
    #: seconds-based sibling under a cadence drift.
    recovery_confirm_ticks: int = Field(default=3, ge=1)
    #: The maximum percentage points the recovery ceiling may sit ABOVE
    #: ``heat_engage_percent`` once recovery is active. Default 15 — an
    #: unvalidated hardware prior (no clean step-response exists in the
    #: store to measure °C/min-per-%heat plant gain directly; this default
    #: is a first guess, exactly D88's original kp/ki posture before their
    #: validation roast, NOT a measured value). The cap is a HARD,
    #: error-independent bound — never scaled by how far below setpoint RoR
    #: has fallen — so the worst case is bounded by construction regardless
    #: of tuning (see the module docstring's structural
    #: runaway-impossibility argument). Always further bounded by
    #: ``heat_ceiling_percent`` (the static outer box): the effective
    #: recovery term is, when the post-FC loop and recovery are enabled,
    #: ``min(heat_ceiling_percent, heat_engage_percent + recovery_headroom_percentage_points)``;
    #: the active effective ceiling is ``max(base_ceiling, recovery_term)``, so
    #: it never falls below D88's ``max(1, min(heat_ceiling_percent,
    #: heat_engage_percent))`` base.
    #: See ``LateMaillardTrim.trim_heat_percent`` for how the actual pre-FC heat
    #: at first crack supplies ``heat_engage_percent`` to this bound: an open
    #: trim admits only a lower per-bean ``pre_fc_heat``; otherwise it replaces
    #: the flat floor higher or lower.
    recovery_headroom_percentage_points: int = Field(default=15, ge=0, le=50)
    #: The maximum percentage points the EFFECTIVE CEILING may fall per tick
    #: while gliding back down from the recovery ceiling toward
    #: ``heat_engage_percent`` after recovery exits. Default 5 — deliberately
    #: SLOWER than entry (which jumps to the recovery ceiling immediately,
    #: the moment the entry condition is confirmed): entry is time-critical
    #: (roast 15's failure was the delay itself), a retreat is not, and a
    #: slow, bounded retreat is the direct structural guard against a
    #: raise→recover→snap-cut→crash→re-trigger limit cycle. This glides the
    #: CEILING only — the PI still computes whatever heat it wants each
    #: tick, clamped into whatever box the (possibly still-gliding) ceiling
    #: currently allows; there is exactly one box per tick, never two
    #: independent clamps fighting over the same value.
    recovery_exit_glide_pp_per_tick: int = Field(default=5, ge=1, le=50)

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

    @model_validator(mode="after")
    def _check_recovery_hysteresis(self) -> "PostFirstCrackControl":
        """The recovery exit margin must be strictly smaller than the entry margin (D96).

        Symmetric (equal) entry/exit margins are limit-cycle-prone: a trace
        sitting near the shared threshold could cross back and forth on
        ordinary tick-to-tick RoR noise, re-entering recovery immediately
        after exiting it. Requiring
        ``recovery_exit_margin_c_per_min < recovery_trigger_margin_c_per_min``
        guarantees a real gap between the two thresholds — RoR must recover
        further to release recovery than it needed to fall to trigger it —
        which is what the mandatory limit-cycle convergence test
        (``tests/test_post_fc_control.py``) exercises.

        Returns:
            The validated control-parameters instance.

        Raises:
            ValueError: If ``recovery_exit_margin_c_per_min`` is not strictly
                less than ``recovery_trigger_margin_c_per_min``.
        """
        if self.recovery_exit_margin_c_per_min >= self.recovery_trigger_margin_c_per_min:
            raise ValueError(
                "recovery_exit_margin_c_per_min must be strictly less than "
                "recovery_trigger_margin_c_per_min (an equal or wider exit margin is "
                "limit-cycle-prone) "
                f"({self.recovery_exit_margin_c_per_min} >= "
                f"{self.recovery_trigger_margin_c_per_min})"
            )
        return self

    @model_validator(mode="after")
    def _check_recovery_requires_ceiling_guard_and_master_flag(self) -> "PostFirstCrackControl":
        """The bounded-bidirectional heat law requires BOTH the RoR-taper
        master flag AND the 196 °C ceiling guard (D96; the master-flag half
        added PR #560 round 4, a Codex finding on the launch banner).

        **The ceiling-guard requirement (round 1):**
        ``safety.SafetyPolicy.evaluate_command`` — the gate every heat/fan
        write in this module passes through — is temperature-blind (rate
        limit + box only); the only two things that ever stop a bean
        temperature excursion are ``SafetyLimits.max_bean_temp_c`` (an
        emergency-stop backstop, default 230 °C, well above the 196/198
        bitter-line pair) and THIS module's own
        ``ceiling_guard_drop_enabled`` path. A recovery law that can raise
        heat above entry with the ceiling guard OFF would leave the 196 °C
        line owned solely by the advisor's own judgment — exactly the gap
        D88 already closed once for the taper's steady-state case.

        **The master-flag requirement (round 4):** ``recovery_enabled`` is
        meaningless with the RoR-taper loop itself (``enabled``) OFF —
        :meth:`~roastpilot_agent.controller.RoastController.
        _apply_deterministic_post_fc_levers` gates on ``config.enabled``
        FIRST, before anything recovery-specific ever runs, so a
        ``recovery_enabled=True`` / ``enabled=False`` combination is
        completely inert. Without this half of the validator, the CLI's
        launch-banner readout (:func:`~roastpilot_agent.cli.
        _format_post_fc_loop_readout`) could print "BOUNDED-BIDIRECTIONAL
        HEAT RECOVERY: ENABLED" for a config where the loop that would ever
        actually raise heat never even runs — mislabeling a validation/
        treatment arm to the operator. Requiring ``enabled=True`` makes the
        banner never lie by construction: any config that survives this
        validator and has ``recovery_enabled=True`` genuinely has the
        recovery mechanism reachable.

        This requirement (both halves) does not apply to the RoR-taper/
        never-add-heat law alone (``enabled=True``, ``recovery_enabled``
        left at its default ``False``), which can only ever lower the
        ceiling relative to entry and so never needs the guard, and is
        obviously unaffected by its OWN flag being on.

        Returns:
            The validated control-parameters instance.

        Raises:
            ValueError: If ``recovery_enabled`` is ``True`` while either
                ``enabled`` or ``ceiling_guard_drop_enabled`` is ``False``.
        """
        if self.recovery_enabled and not self.ceiling_guard_drop_enabled:
            raise ValueError(
                "recovery_enabled requires ceiling_guard_drop_enabled=True — a law that "
                "can raise heat above entry with no deterministic 196 °C ceiling-guard "
                "anchor would leave the bitter line owned solely by the advisor's own "
                "judgment"
            )
        if self.recovery_enabled and not self.enabled:
            raise ValueError(
                "recovery_enabled requires enabled=True (the RoR-taper master flag) — "
                "recovery is a relaxation of the taper's own never-add-heat-beyond-entry "
                "ceiling, and is completely inert (and would mislabel the launch banner) "
                "when the taper loop itself never runs"
            )
        if self.recovery_projection_enabled and not self.recovery_enabled:
            raise ValueError(
                "recovery_projection_enabled requires recovery_enabled=True — projection "
                "is an alternate recovery-entry signal, not an inert standalone flag"
            )
        if self.recovery_projection_cutoff_horizon_pp <= self.recovery_projection_entry_horizon_pp:
            raise ValueError(
                "recovery_projection_cutoff_horizon_pp must be strictly greater than "
                "recovery_projection_entry_horizon_pp"
            )
        return self


class ReferenceCurve(BaseModel):
    """Same-bean reference-curve retrieval master flag (#567 Slice B).

    Gates whether ``RoastService`` retrieves a completed, well-rated past
    roast of THIS SAME bean (by :func:`~roastpilot_agent.models.
    recording_origin_slug`) once at roast start and hands it to the
    controller as read-only :class:`~roastpilot_agent.advisor.AdvisorContext`
    (``reference_curve`` / ``reference_landmarks``) — a different completed
    roast's own trajectory and landmarks, for the advisor to compare
    against, never a target or a control input. No control path, safety
    rule, or controller transition reads these fields back; retrieval is a
    single store read at roast start (and again at post-restart recovery),
    never per tick.

    **This config model is inert on its own, mirroring
    :class:`PostFirstCrackControl`'s own inert-until-wired posture**: with
    ``enabled=False`` (the default), no store reference read ever happens
    and ``AdvisorContext.reference_curve`` / ``reference_landmarks`` stay
    empty / ``None`` — no store reads and empty context values, identical
    CONTROL behaviour to the pre-#567 code. (Precisely: the advisor's
    prompt JSON does gain two always-empty keys, ``reference_curve: []`` /
    ``reference_landmarks: null`` — inherent to any new ``AdvisorContext``
    field, the same as ``roast_style`` / ``post_fc_setpoint_c_per_min``
    before it; this is a context-shape addition, not a behavioural one.)

    ``enabled`` **defaults ``False`` and is hardware-gated for promotion**
    (design note §6.6, mirroring D88's own ship-disabled-then-promote
    posture): the design note's own replay bake-off is a gate to BUILD the
    retrieval/representation machinery, not evidence sufficient to flip the
    default — that requires at least one hardware roast with a live
    reference present, ratified by the operator, same as D88/D90's own
    validation-before-promotion precedent. Do not flip this default without
    that hardware validation.
    """

    #: The master flag. ``False`` by default — no retrieval happens, and the
    #: advisor context's reference fields stay empty/None, exactly today's
    #: behaviour. Flip only after the hardware-validation gate above clears.
    enabled: bool = Field(default=False)


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
    #: (the trim only ever lowers heat — a validator pins this). See
    #: ``LateMaillardTrim.trim_heat_percent`` for the single canonical account
    #: of how an open trim window supplies the post-FC engagement heat only
    #: when the post-FC loop is enabled; outside that window, a bean
    #: ``pre_fc_heat`` replaces the flat floor higher or lower.
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


class AmbientFanDoctrine(BaseModel):
    """Ambient-aware fan doctrine inputs for the ``c11`` teaching (#709, RP-B).

    The #707/D122 joint-drop-objective tree traced both Conebosque A/B arms'
    temperature-short drops to advisor fan aggression crashing the rate of
    rise, and the corpus put both crash roasts in the two coolest rooms
    (23.1 / 23.5 °C) while fan 100 at 25-31 °C repeatedly cupped fine. So the
    doctrine is CONDITIONAL on ambient, never a blanket softening. D156 may
    ceiling-bind a cool-room destination while heat retains downward authority;
    D157 preserves #498's full fan capability once fan may be the only brake.

    Both numbers live here, as DATA, rather than as constants written into the
    ``c11`` prose, which stays digit-free under test. Two reasons: the #218
    two-copies discipline (a constant baked into a prose rule is the pattern
    that misled the model in the #567/c9 and #563 told-ceiling arcs), and
    because both values are HYPOTHESES the operator re-fits from RP-D (#711)
    joint scores — a re-fit that must not require a prompt edit and a fresh
    bake-off.

    **Inert on its own, mirroring :class:`ReferenceCurve`'s posture.** With
    ``enabled=False`` (the default) the controller populates none of the
    doctrine's context fields, so ``AdvisorContext.ambient_temp_c`` /
    ``ambient_humidity_pct`` / ``ambient_fan_threshold_c`` /
    ``ambient_fan_step_max_pp`` stay ``None`` and the advisor's prompt JSON
    gains only always-null keys — a context-SHAPE addition, not a behavioural
    one, exactly as #567 reasoned for its own two fields.

    That distinction is the whole point of the flag. An always-null key is
    inert; a populated, meaningfully-named number is not. Without the gate,
    every roast on the live default ``c3`` would carry real ambient values and
    a named fan-step bound into a prompt that never teaches them — changing
    the live advisor's input, and contaminating any c3 baseline the RP-B
    comparison is measured against.

    **Enabling is deliberately two acts:** select ``c11`` AND set
    ``enabled=True``. Selecting ``c11`` alone leaves the doctrine inert (the
    teaching's own absent-ambient branch applies and it falls back to the
    unqualified fan-brake rule), which is the same pairing ``c9`` has with
    ``reference_curve.enabled``. A bake-off arm that forgets the flag measures
    the fallback, not the doctrine.

    ``threshold_c`` and ``step_max_pp`` remain advisory. The destination ceiling
    is the one deterministically enforced number, gated on both ``enabled`` and
    ``post_fc_fan_ceiling_enabled`` (D156): it narrows the DEVELOPMENT box
    through the existing ``command_bounds`` clamp, never through a new safety
    rule. The optional fan slew clamp stays out because it bounds the step, not
    the destination.

    Prompt selection is deliberately decoupled from both doctrine flags. They
    may be true while ``c3`` (the live default) supplies no ambient teaching; in
    that case the model simply sees the resolved number in
    ``fan_ceiling_percent``. Told == enforced still holds structurally because
    the same resolved box feeds the context and safety evaluation.
    """

    enabled: bool = False
    """Master flag for ambient context and destination-ceiling enforcement.

    When false, the controller feeds no doctrine ambient to the advisor and
    D156's policy conjunct cannot engage. Default ``False`` — promotion is
    gated on the offline decision-level bake-off plus a single-variable
    hardware roast scored by RP-D (#711), both operator-gated.
    """

    threshold_c: float = Field(default=26.0, gt=0.0, le=60.0)
    """The boundary ``c11`` compares ``ambient_temp_c`` against, in Celsius.

    Default 26.0 — the operator's ratified first cut (6 Aug), explicitly a
    HYPOTHESIS to re-fit once several roasts span the ambient range with the
    RP-D joint score attached. Ten corpus roasts (23.1-31.6 °C) show no clean
    ambient-to-fan correlation, so this is a starting point, not a pin.

    Documentary boundary reconciliation (D156): c11's prose places exactly
    ``threshold_c`` in its inclusive warm/graduated branch, while deterministic
    enforcement uses the strict cool test and therefore does NOT clamp at exact
    equality. This one-point divergence fails toward free fan authority and is
    #498-safe; the policy comparison deliberately remains ``ambient >= threshold``.

    Bounded because it is re-fit BY HAND, which is where a typo lands: an
    unbounded field accepts 260.0 for 26.0 and silently puts every roast in
    the graduated regime. ``nan`` is quieter and worse, since it serialises to
    ``null`` and the model would read the boundary as absent."""

    step_max_pp: float = Field(default=10.0, gt=0.0, le=HOTTOP_FAN_LEVEL_PP * 2)
    """The size of an ORDINARY below-threshold fan step, in percentage points.

    Default 10.0 per D126, refining D124's ratified "about 15 pp" on hardware
    grounds. The Hottop fan is a 0-10 INTEGER scale: the driver quantises every
    command with ``(value + 5) // 10``
    (``coffee_roaster_mcp.drivers._percent_to_hottop_fan_scale``). A 15 pp step
    is therefore one physical level from some starting values and two from
    others — from fan 30 it lands on level 5, a 20 pp move. Telling the model
    15 while the machine moves 10 or 20 is a told-vs-enforced gap in exactly
    the path this doctrine exists to protect. This is the same quantisation
    fact ``post_fc_deadband_threshold_percent`` (also 10) already encodes.

    Constrained twice, because each bound catches a different failure:

    * a whole multiple of one level, so ANY accepted value maps to an exact
      whole number of Hottop levels and told == physically-moved across a
      re-fit (10 pp = one level, 20 pp = two). A re-fit to 15.0 would silently
      reopen the gap D126 closed, so it is rejected at construction.
    * at most TWO levels. Without this, a "whole multiple" alone accepts 100.0
      — a full floor-to-ceiling move as an ORDINARY, non-emergency,
      below-threshold step. That would make the docstring line above ("bounds
      the STEP, never the destination") false. No deterministic SLEW clamp
      catches it: D156/D157's distinct destination ceiling may narrow the box
      while engaged, but does not turn a full-box ordinary move into graduation.
      Beyond two levels a step is not graduation in any meaningful sense.

    It bounds the STEP, never the destination: every fan value through the
    currently told ceiling stays reachable. D156/D157 may separately narrow
    that destination while engaged. The pace teaching remains subordinate to
    the fan-brake rule — when heat is at its floor and the bean is still
    climbing, fan is the only brake left and graduation does not apply."""

    max_reading_age_seconds: float = Field(default=90.0, gt=0.0, le=600.0)
    """How old ambient may be and still reach doctrine context or policy (#732).

    Unlike the two fields above, this one is **controller-only**: it gates the
    ambient value the controller supplies to both ``AdvisorContext`` and D156's
    predicate, but the age limit itself is never surfaced into
    ``AdvisorContext``, so no prompt sees it. The group holds knobs for two
    audiences — ``threshold_c`` and ``step_max_pp`` are told to the model, this
    is not — and a future field should say which it is.

    ``c11`` selects a fan regime by comparing ``ambient_temp_c`` against
    :attr:`threshold_c`, so a stale reading does not degrade gracefully — it
    puts the model confidently in the wrong regime, and a stale value is
    indistinguishable from a fresh one at the prompt. The asymmetry that
    matters is a stale LOW reading in a room that has since warmed: it holds
    the graduated regime when aggressive airflow is right, which is the
    direction #498 warns about.

    Past this bound the controller declines to populate ``ambient_temp_c`` /
    ``ambient_humidity_pct`` at all, so the doctrine degrades to the SAME
    absent-ambient path an unplugged probe already takes — a branch ``c11``
    handles deliberately (fall back to the unqualified fan-brake rule; do not
    read a missing reading as licence to be gentler). No new teaching, no new
    branch, and the failure direction is toward #498's full fan capability
    rather than away from it.

    Default 90.0 = three of the MCP's 30 s default ambient poll cycles
    (``coffee_roaster_mcp.config.AmbientConfig.poll_interval_seconds``), so
    ordinary poll jitter and a couple of missed cycles never flap the doctrine
    while a genuinely wedged reading is out within about a minute and a half —
    well inside a roast's post-first-crack window, where the doctrine acts.
    It is a BOUND, not a measurement: no corpus records reading age, so this is
    picked off the MCP's own cadence. Being too tight merely falls back to the
    absent-ambient branch; too loose is the failure being fixed.

    Ceilinged at 600.0 for the reason ``step_max_pp``'s own ceiling exists — a
    hand-refit knob whose ceiling sits far above its intent lets a plausible
    typo validate. ``900.0`` for ``90.0`` would pass ``gt=0.0`` alone and
    silently disable the guard for most of a 12-20 minute roast, and a bare
    ``inf`` would disable it outright while freezing a non-standard
    ``Infinity`` token into ``config_json``. Ten minutes exceeds any real
    freshness bound: past a whole roast's length this is not a staleness gate."""

    post_fc_fan_ceiling_enabled: bool = False
    """Whether the destination ceiling is deterministically ENFORCED on the
    DEVELOPMENT fan box (11 Aug ratification / D156, superseding the 6 Aug
    prompt-only posture). Default ``False``; enforcement also requires the
    master :attr:`enabled` flag and a known effective heat floor. In practice,
    that means the post-FC control loop is engaged and has produced an output.
    With the loop inert, the floor stays unknown and the ceiling cannot bind.
    Likewise, if bean rate-of-rise is missing on the first-crack engagement
    tick, the loop produces no output, the release latch arms for the whole run,
    and the ceiling never binds. This is #498-safe, but an A/B run with that
    single-tick RoR gap silently holds no airflow constant, just as surely as
    enabling the doctrine flags without the loop."""

    post_fc_fan_ceiling_percent: int = Field(default=70, ge=10, le=100)
    """The DEVELOPMENT fan destination ceiling in a cool room, in percent.

    Default 70 is physical Hottop level 7 under the driver's
    ``(value + 5) // 10`` quantisation (D126), matching the level c10's fan 65
    actually actuated so #708's later timing A/B stays single-variable. It is a
    first-cut hypothesis to re-fit from RP-D joint scores, like
    :attr:`threshold_c`.

    Typed as ``int`` so non-finite values are rejected by construction. Bounded
    because it is re-fit by hand: 10 preserves at least one physical fan level
    (a typo'd 0 would abolish DEVELOPMENT airflow), while 100 prevents the
    narrowed box from inverting."""

    @model_validator(mode="after")
    def _step_must_be_a_whole_number_of_hottop_levels(self) -> "AmbientFanDoctrine":
        """Reject a step that is not a whole number of Hottop fan levels.

        Returns:
            The validated model.

        Raises:
            ValueError: If ``step_max_pp`` is not a multiple of 10.
        """
        if self.step_max_pp % HOTTOP_FAN_LEVEL_PP != 0:
            raise ValueError(
                "ambient_fan_doctrine.step_max_pp must be a whole multiple of "
                f"{HOTTOP_FAN_LEVEL_PP:g} pp (one Hottop fan level), so the bound the "
                "model is told maps to a whole number of physical fan levels under "
                f"the driver's (value + 5) // 10 quantisation (D126). Got "
                f"{self.step_max_pp:g}"
            )
        return self

    @model_validator(mode="after")
    def _ceiling_must_be_a_whole_number_of_hottop_levels(self) -> "AmbientFanDoctrine":
        """Reject a ceiling that is not a whole number of Hottop fan levels.

        Returns:
            The validated model.

        Raises:
            ValueError: If ``post_fc_fan_ceiling_percent`` is not a multiple of 10.
        """
        if self.post_fc_fan_ceiling_percent % HOTTOP_FAN_LEVEL_PP != 0:
            raise ValueError(
                "ambient_fan_doctrine.post_fc_fan_ceiling_percent must be a whole "
                f"multiple of {HOTTOP_FAN_LEVEL_PP:g} pp (one Hottop fan level): the "
                "driver quantises with (value + 5) // 10 (D126), so a non-multiple "
                "ceiling (e.g. 75) would actuate the NEXT level up (80) and the "
                "physically moved fan would exceed the enforced ceiling. Got "
                f"{self.post_fc_fan_ceiling_percent:d}"
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
    # The bound on the advisory await, and therefore on how long a slow model
    # DELAYS the control loop (operator decision, 9 Aug 2026, #747 / D151).
    # Lowered 10.0 -> 5.0 to match the ~5 s FC-slot latency screen the advisor
    # roster is chosen against: the call is awaited inline at the end of
    # ``tick()`` and the serve loop is drain-operator-queue -> tick, so this
    # number is exactly how long a stalled provider can hold off the next
    # telemetry read, the next ``_evaluate_safety``, and the next drain of the
    # operator queue (where the in-UI EMERGENCY STOP is consumed). Holding it at
    # 2x the screen meant an unscreened model could hold the loop for twice as
    # long as any model we would knowingly run.
    #
    # Margin against a FALSE timeout on the pinned model: gpt-4o measured
    # 2.41/3.73 s median/max over 28 roasts (D40/D41) and 2.54/3.59 s (#396,
    # 16 Jul), so 5.0 leaves >1 s over the worst observed call. An isolated
    # overrun is already tolerated — it becomes one REJECT with the
    # hold-current-targets fallback, and D30's stop needs THREE CONSECUTIVE
    # failures (``_consecutive_advisor_failures`` resets on any good call), so a
    # single slow tick cannot push a roast into recovery. A model that times out
    # three times running is one that should not be driving the roast anyway.
    advisory_timeout_seconds: float = Field(default=5.0, gt=0)
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
    # #567 Slice B: same-bean reference-curve retrieval master flag. INERT with
    # the default False (mirrors post_first_crack_control's own inert-until-
    # wired posture): no store reference read happens and the advisor
    # context's reference fields stay empty/None — no store reads, identical
    # control behaviour to today's (see ReferenceCurve's docstring for the
    # precise claim: the advisor prompt JSON does gain two always-empty
    # keys, inherent to any new AdvisorContext field). Hardware-gated for
    # promotion; parameterised factory per the repo's pyright-strict
    # typed-default idiom (mirrors pre_first_crack_levers/
    # post_first_crack_control above).
    reference_curve: ReferenceCurve = Field(default_factory=ReferenceCurve)
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

    # #709 (RP-B): the ambient-aware fan doctrine's own config group. Mirrors
    # ``reference_curve`` above — a selectable advisor teaching whose context
    # inputs stay INERT until explicitly enabled, so selecting the prompt and
    # feeding it data are one deliberate act rather than a default.
    ambient_fan_doctrine: AmbientFanDoctrine = Field(default_factory=AmbientFanDoctrine)

    @field_validator("max_stale_telemetry_seconds")
    @classmethod
    def _check_finite_staleness_bound(cls, value: float) -> float:
        """Reject a non-finite bound that would disable stale-read detection."""
        if not math.isfinite(value):
            raise ValueError("max_stale_telemetry_seconds must be finite")
        return value

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
    The prompt is the AS-BUILT control teaching SYSTEM frame (#274 / D39.1),
    wired live for the post-FC loop (#277) — the per-tick #275 context is
    the user message; see :attr:`prompt_version`'s field comment for the
    current default and its A/B history (the #277 bake-off itself was scored
    under ``c1``, superseded since).
    See ``docs/advisor/bakeoff-results-2026-06-21.md``. To run a
    model on its native provider (no OpenRouter hop/markup, per D18), set
    ``provider`` + the matching ``api_key_env``. ``OPENROUTER_API_KEY`` must be
    set in the environment at runtime; ``FakeAdvisor`` stays the test/CI default.

    Per-phase model selection (#173): ``model_slug`` is the model, and
    ``model_slug_by_phase`` is an EMPTY-by-default per-phase override map
    resolved by :meth:`model_for`. With no override, every phase resolves to
    ``model_slug`` — so a slug set in ``/config``, in the saved config file, or
    via ``ROASTPILOT_ADVISOR__MODEL_SLUG`` is the model that answers.

    That empty default is D151 (#747), and it is a bug fix, not a re-pin. The
    map used to ship populated with ``DEFAULT_ADVISOR_MODEL`` for every phase
    and is absent from ``AdvisorConfigEdit``, so it silently SHADOWED every
    operator-set ``model_slug``: roast 8 (28 Jun 2026) was launched as a
    ``gpt-4.1-mini`` arm, ran gpt-4o for all 19 decisions, and was written up
    as a mini hardware result in D73/D74 and #396. The pin itself (#277/D43)
    is unchanged — it is the FIELD DEFAULT, which the operator may now change.

    The mechanism is retained (an override still wins) so a future re-run can
    pin one phase to a different model; under D35 only post-FC DEVELOPMENT
    consults the advisor, so that map has exactly one live slot today.
    """

    provider: Literal["openai", "anthropic", "google", "ollama", "openai_compatible"] = (
        "openai_compatible"
    )
    provider_base_url: str = OPENROUTER_BASE_URL
    api_key_env: str = Field(default="OPENROUTER_API_KEY", min_length=1)
    model_slug: str = Field(default=DEFAULT_ADVISOR_MODEL, min_length=1)
    # Phase-keyed model override map (#173) — EMPTY by default (D151, #747), so
    # :meth:`model_for` falls back to ``model_slug`` in every phase and the
    # configured model is the model that answers. Populating a slot overrides
    # that phase only; a phase absent from the map falls back to ``model_slug``.
    # Deliberately NOT in ``AdvisorConfigEdit``: a per-phase UI would expose
    # three inputs for one live slot (D35 — only DEVELOPMENT consults), and it
    # was that unreachable-from-the-UI map, shipped populated, that shadowed
    # ``model_slug`` for six weeks. Each slug is ``min_length=1`` (an empty
    # model slug is meaningless). Parameterized factory, not a bare ``dict``
    # default, per the repo's pyright-strict typed-default idiom.
    model_slug_by_phase: dict[RoastPhase, Annotated[str, Field(min_length=1)]] = Field(
        default_factory=dict[RoastPhase, str]
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
    # fast structured advice inside the controller's advisory budget
    # (``ControllerConfig.advisory_timeout_seconds``, 5.0 s since D151), so the
    # bake-off
    # measures reasoning on-vs-off (E8-S4 cost/reasoning eval).
    reasoning_effort: Literal["off", "minimal", "low", "medium", "high"] | None = None

    @field_validator("model_slug")
    @classmethod
    def _strip_model_slug(cls, value: str) -> str:
        """Normalise the base slug; see :func:`normalize_model_slug`.

        ``min_length=1`` admits ``"  "``, which before D151 was inert — the
        always-populated phase map shadowed it. This change is what makes it
        live, so a blank-looking slug would now ship a garbage identifier to the
        provider on every DEVELOPMENT call.

        Args:
            value: The configured slug.

        Returns:
            The stripped slug.
        """
        return normalize_model_slug(value, "model_slug")

    @field_validator("model_slug_by_phase")
    @classmethod
    def _strip_phase_slugs(cls, value: dict[RoastPhase, str]) -> dict[RoastPhase, str]:
        """Apply the same normalisation to every per-phase override.

        A phase override is dispatched by the same code path, so it carries the
        same blank/padded hazard as the base slug.

        Args:
            value: The per-phase override map.

        Returns:
            The map with every slug stripped.
        """
        return {
            phase: normalize_model_slug(slug, f"model_slug_by_phase[{phase.value}]")
            for phase, slug in value.items()
        }

    def dispatch_identity(self) -> tuple[str, str, str, str, str | None]:
        """What a probe or an advice call actually CONTACTS (#747 review fold).

        Named and returned as a unit because "does this probe still describe the
        current config" has already taken three rounds of review-discovered
        subtlety, all of it previously living as comments at one call site — and
        the dimension that kept getting missed (``reasoning_effort``) is exactly
        what an unnamed inline tuple invites. ``build_model`` bakes the reasoning
        effort into the model settings every cached agent uses, including the one
        ``healthcheck`` probes with, so it is part of the identity.

        The base URL is compared NORMALISED, using the same helper the endpoint
        screen uses: a trailing slash or host-case edit is cosmetic, and treating
        it as a different endpoint would needlessly discard a valid probe.

        The per-phase override map is deliberately NOT here — it changes which
        model gives ADVICE, not what the base-slug probe contacted, and callers
        that care compare :func:`advisor_screen.advice_models` alongside this.

        Returns:
            ``(provider, normalised base URL, api_key_env, model_slug,
            reasoning_effort)``.
        """
        return (
            self.provider,
            normalize_base_url(self.provider_base_url),
            self.api_key_env,
            self.model_slug,
            self.reasoning_effort,
        )

    def model_for(self, phase: RoastPhase) -> str:
        """Return the advisor model slug to use for ``phase`` (#173).

        Looks ``phase`` up in :attr:`model_slug_by_phase`, falling back to
        :attr:`model_slug` when the phase carries no override. The map is empty
        by default (D151, #747), so this resolves to the configured
        :attr:`model_slug` in every phase — including the one phase that
        consults the advisor under D35 — until a future re-run populates a slot.

        This is the ONLY correct way to ask which model will answer. Reading
        :attr:`model_slug` directly is right only when the question really is
        about the base slug (the reachability probe, the off-OpenRouter
        bean-sourcing extraction fallback); anything reporting or dispatching
        roast advice must come through here, or it will name the wrong model
        the moment an override exists.

        Args:
            phase: The agent phase the controller is currently in.

        Returns:
            The model slug for ``phase`` — its override if present, else the
            base ``model_slug``.
        """
        return self.model_slug_by_phase.get(phase, self.model_slug)


# Tolerant provider-endpoint matching, shared by ``bean_sourcing`` (which
# routes extraction off OpenRouter) and ``advisor_screen`` (which applies the
# FC-latency screen only to the endpoint it was measured on). Lives here
# because this module already owns ``OPENROUTER_BASE_URL``, and because a
# second, less tolerant copy is exactly the drift Claude's review caught.
_DEFAULT_PORT_BY_SCHEME: dict[str, int] = {"http": 80, "https": 443}


def normalize_model_slug(value: str, field_name: str) -> str:
    """Strip a model slug and reject it if blank (#747 review fold).

    One implementation for all three boundaries — ``AdvisorConfig.model_slug``,
    its per-phase overrides, and ``AdvisorConfigEdit.model_slug`` — because
    three hand-rolled copies of "strip, raise if empty" is the same drift this
    PR is elsewhere fixing.

    Stripping matters beyond tidiness: ``build_model`` dispatches the slug
    verbatim, so a padded value would otherwise be dispatched padded while the
    FC-latency screen matched on the raw string.

    Args:
        value: The configured slug.
        field_name: Field name, for the error message.

    Returns:
        The stripped slug.

    Raises:
        ValueError: If the slug is blank once stripped.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank")
    return stripped


def normalize_base_url(url: str) -> str:
    """Normalise a provider base URL for tolerant comparison (#590 P2 fix).

    Strips a trailing ``/``, lower-cases the host (scheme/path stay
    case-sensitive, matching URL semantics — only the host is defined to be
    case-insensitive), AND drops an explicit port that merely restates the
    scheme's implicit default (:data:`_DEFAULT_PORT_BY_SCHEME`) — so
    ``"https://openrouter.ai/api/v1"``, ``"https://openrouter.ai/api/v1/"``,
    ``"https://OpenRouter.ai/api/v1"``, and
    ``"https://openrouter.ai:443/api/v1"`` all normalise identically. A
    NON-default explicit port (e.g. a LAN reverse-proxy on ``:8443``) is
    preserved — dropping it would be the exact false-positive this
    tolerant match must NOT introduce. Never raises: most non-URL strings
    degrade to a mostly-empty ``SplitResult``; eager malformed-bracket errors
    and malformed/non-numeric ports are caught explicitly. Either way, a
    malformed ``provider_base_url`` here just
    fails the equality check harmlessly (falls through to the
    native-provider branch) rather than crashing model resolution.

    Args:
        url: The base URL to normalise.

    Returns:
        The normalised URL for ``==`` comparison.
    """
    stripped = url.strip().rstrip("/")
    try:
        parsed = urlsplit(stripped)
    except ValueError:
        # Malformed bracketed hosts raise eagerly. Treat them like every other
        # non-matching provider URL so attempt admission can still be recorded.
        return stripped
    netloc = parsed.netloc.lower()
    try:
        port = parsed.port
    except ValueError:
        # A non-numeric/out-of-range port -- can't be a default-port match
        # either way, so leave netloc as-is and let the equality check
        # fail harmlessly (this function must never raise).
        port = None
    default_port = _DEFAULT_PORT_BY_SCHEME.get(parsed.scheme.lower())
    if port is not None and port == default_port:
        # ``SplitResult.port`` is parsed directly off netloc's trailing
        # ``:<port>`` segment, so whenever it returns a value, ``netloc``
        # (already lower-cased above, and port digits are case-invariant)
        # is GUARANTEED to end with exactly that suffix -- no ``.endswith``
        # guard needed (would be an unreachable branch under coverage).
        netloc = netloc[: -len(f":{port}")]
    return urlunsplit(parsed._replace(netloc=netloc))


def is_openrouter_endpoint(advisor_config: AdvisorConfig) -> bool:
    """Whether ``advisor_config`` is ACTUALLY pointed at OpenRouter (#590 P2 fix).

    ``advisor_config.provider == "openai_compatible"`` alone is NOT
    sufficient: that provider setting is the generic OpenAI-compatible-API
    path, which also covers a local server, LiteLLM, or any other
    OpenAI-compatible endpoint reachable via a custom ``provider_base_url``
    — none of which necessarily serve the OpenRouter-specific
    :data:`_DEFAULT_EXTRACTION_MODEL_SLUG`. This additionally requires
    ``provider_base_url`` to match :data:`~roastpilot_agent.config.OPENROUTER_BASE_URL`
    (tolerant of a trailing-slash / host-case / explicit-default-port
    variant — see :func:`normalize_base_url`).

    Args:
        advisor_config: The operator's advisor provider/key/model config.

    Returns:
        ``True`` only when the provider is ``"openai_compatible"`` AND its
        base URL resolves to OpenRouter's.
    """
    return advisor_config.provider == "openai_compatible" and normalize_base_url(
        advisor_config.provider_base_url
    ) == normalize_base_url(OPENROUTER_BASE_URL)


class BeanSourcingConfig(BaseModel):
    """Add-bean-from-URL fetch + extraction limits (#573 phase 1, #590 slice A).

    Governs the respectful, fail-soft vendor-page fetch in
    ``roastpilot_agent.bean_sourcing.draft_bean_profile_from_url``: a bounded
    timeout, a hard cap on the response body (enforced while streaming, so an
    oversized or slow-drip response is never read fully into memory), and an
    identifying ``User-Agent`` (a vendor's own logs should be able to tell
    this traffic apart from a browser). The structured LLM extraction step
    reuses the operator's already-configured :class:`AdvisorConfig` for
    provider/key (BYOK) — see
    ``bean_sourcing.draft_bean_profile_from_url``'s ``advisor_config``
    parameter — but owns its OWN extraction timeout
    (:attr:`extraction_timeout_seconds`), independent of the roast-advice
    timeout :class:`AdvisorConfig` configures (#590 slice A: a one-shot
    bean draft is not a per-tick advice call). The extraction MODEL
    (:attr:`model_slug`) defaults to a PROVIDER-AWARE resolution rather
    than a fixed slug — see :attr:`model_slug`'s own docstring — so a bean
    draft neither rides whatever roast-advice model happens to be
    configured nor sends an OpenRouter-prefixed slug to a native provider.
    """

    fetch_timeout_seconds: float = Field(default=10.0, gt=0)
    """Bound on the vendor-page GET (connect + read). A product page is a
    normal web response; 10 s comfortably covers a slow vendor host without
    leaving a drafting request hanging indefinitely."""

    max_response_bytes: int = Field(default=2_000_000, gt=0)
    """Hard cap on the fetched response body. A green-coffee product page is a
    few hundred KB at most (mostly markup/CSS); 2 MB leaves generous headroom
    while still bounding memory use if a URL redirects into an unexpectedly
    large asset."""

    user_agent: str = Field(
        default="RoastPilotAgent-BeanSourcing/1.0 (+https://github.com/syamaner/roastpilot-agent)",
        min_length=1,
    )
    """Identifying ``User-Agent`` sent with the fetch — a courteous default a
    vendor can distinguish from a browser or an unlabelled scraper."""

    extraction_timeout_seconds: float = Field(default=45.0, gt=0)
    """Bound on the bean-identity extraction LLM call
    (``bean_sourcing._extract_bean_identity``'s ``agent.run()``) —
    deliberately a SEPARATE, LONGER budget than the *per-tick roast-advice*
    call the live control loop makes once a second, which is bounded by
    :attr:`ControllerConfig.advisory_timeout_seconds` (5.0 s since D151 —
    :attr:`AdvisorConfig.timeout_seconds` has no runtime consumer in the agent).
    A bean draft is a one-shot request the operator explicitly triggers by
    pasting a vendor URL and can wait ~30 s for; the far tighter advice budget
    starved every call for the reasoning models tested in the bean-sourcing
    bake-off (``gpt-5-nano``/``gpt-5-mini`` both scored 0/81 on the first
    pass — see ``docs/advisor/bean-sourcing-bakeoff-2026-07-19.md``,
    "Operational findings"). 45 s matches the budget the bake-off itself
    used to get a fair read on those models."""

    model_slug: str | None = Field(default=None, min_length=1)
    """An explicit extraction model slug, independent of whatever model
    :class:`AdvisorConfig` has configured for roast advice — bean drafting
    must not silently ride a roast-advice model swap. ``None`` (the
    default) means "resolve provider-aware" rather than "use a fixed slug"
    — see ``bean_sourcing._resolve_extraction_model_slug``:

    - When the advisor is actually pointed at **OpenRouter** — i.e.
      :attr:`AdvisorConfig.provider` is ``"openai_compatible"`` AND
      :attr:`AdvisorConfig.provider_base_url` matches :data:`OPENROUTER_BASE_URL`
      (the BYOK-OpenRouter path the bake-off used) — it resolves to
      ``"openai/gpt-5-mini"``, the bean-sourcing extraction bake-off's
      screening pick (``docs/advisor/bean-sourcing-bakeoff-2026-07-19.md``:
      best macro-F1/recall of any model with zero page errors, ~1/8 the
      cost of the ``gpt-4o`` ceiling). That bake-off ran on the pre-#590
      extraction pipeline and is explicitly flagged there as a SCREENING
      pick to re-confirm once the #590 preprocessing (extruct/trafilatura)
      lands, not a locked decision. ``provider == "openai_compatible"``
      ALONE is not enough to key this on (a P2 caught in review, after the
      P1 below): that provider setting also covers ANY OTHER
      OpenAI-compatible endpoint (a local server, LiteLLM, etc. via a
      custom ``provider_base_url``), which does not necessarily serve
      ``"openai/gpt-5-mini"`` either.
    - For anything else — a NATIVE provider (``openai``/``anthropic``/
      ``google``/``ollama``), OR an ``openai_compatible`` provider pointed
      at a non-OpenRouter endpoint — it resolves to
      ``advisor_config.model_slug`` instead. The OpenRouter-prefixed
      ``"openai/gpt-5-mini"`` slug is meaningless (or silently wrong-vendor)
      against those, which made every extraction fail whenever the
      operator's advisor was configured that way (a P1 caught in review on
      the PR that introduced this field, before merge).

    Set this explicitly to opt OUT of the provider-aware default and pin a
    specific slug regardless of provider (e.g. the bake-off harness does
    this per roster model under test)."""


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

    @field_validator("max_bean_temp_c", "max_env_temp_c", "pre_t0_max_bean_temp_c")
    @classmethod
    def _check_finite_temperature_ceiling(cls, value: float) -> float:
        """Reject a non-finite bound that would disable a temperature guard."""
        if not math.isfinite(value):
            raise ValueError("temperature safety ceilings must be finite")
        return value

    @field_validator("min_seconds_between_commands")
    @classmethod
    def _check_finite_command_interval(cls, value: float) -> float:
        """Reject a non-finite interval that would suppress every command."""
        if not math.isfinite(value):
            raise ValueError("min_seconds_between_commands must be finite")
        return value

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
    bean_sourcing: BeanSourcingConfig = Field(default_factory=BeanSourcingConfig)

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

    @model_validator(mode="after")
    def _check_ambient_freshness_bound_outlives_the_poll_interval(self) -> "AppConfig":
        """The doctrine's freshness bound must exceed the ambient POLL interval (#732).

        The second cross-SECTION validator, and for the same structural reason
        as the first: ``max_reading_age_seconds`` lives on
        ``controller.ambient_fan_doctrine`` while the cadence that decides how
        old a *healthy* reading routinely gets lives on
        ``mcp_device.ambient_poll_interval_seconds``. Neither section can see
        the other, so the check can only run here.

        **The failure it prevents is silent and total.** The poll interval is
        operator-editable from ``/config`` with no maximum, while the freshness
        bound is file-only. Set the interval above the bound and EVERY reading
        is stale on EVERY tick, forever — the controller declines ambient for
        the whole roast, the doctrine runs its absent-ambient fallback, and
        nothing surfaces that: the dashboard's Room tile reads the ungated
        telemetry, so it still shows a room temperature the advisor was never
        given. The direction is fail-safe, but an RP-B hardware arm would be
        recorded as "c11 with ambient" while the model saw the absent branch on
        every tick — a green, meaningless result, which is precisely the class
        of outcome #709's two-act enablement already warns about.

        **Enforces the property actually required, not a preferred margin.** A
        healthy reading is at its oldest just before the next poll lands, so the
        bound must be at least the cadence for the doctrine to function at all;
        that is the correctness line and the only one worth making
        unconstructible. An earlier revision demanded 2x for flap margin and was
        wrong to: at a 90 s bound against a 60 s cadence the doctrine works
        perfectly well, and rejecting that pair caused a *working* configuration
        to be treated as broken — including, through the recovery path, retiring
        a doctrine that was serving fresh readings. Two or more cadences remains
        the sensible operating margin; it is advice, not a validation rule.

        **An UNSET cadence is unknown, not incompatible, and this validator
        does not judge it.** ``mcp_yaml`` renders
        ``ambient_poll_interval_seconds`` only when it is set, so ``None`` means
        "inherit" from a file this validator will not read — config
        construction must not depend on the filesystem, since ``AppConfig`` is
        built in tests, replay and recovery. Requiring the value is therefore a
        START-A-ROAST precondition
        (``RoastService._require_explicit_ambient_cadence``, which also carries
        the rationale) rather than a construction one; see the inline note on
        the early return below for why that placement is load-bearing.

        Only enforced while the doctrine is ENABLED, so the inert default can
        never make an otherwise-valid config unconstructible.

        **This compares a controller field against a device field, so it is
        only meaningful when both come from the same generation.** Recovery
        deliberately recombines a run's FROZEN controller with the CURRENT
        device config, where they can legitimately disagree and the pair is
        unrepairable because the run already happened; that ONE site handles
        the clash itself (see ``RoastApp.recover_on_start``) rather than being
        allowed to raise, because a guard against a silently-void advisory
        input must never be able to block a recovery.

        Returns:
            The validated application config.

        Raises:
            ValueError: If the doctrine is enabled, the ambient poll interval is
                KNOWN (explicitly set), and the freshness bound is below it. An
                unset poll interval is not a rejection here — see above.
        """
        doctrine = self.controller.ambient_fan_doctrine
        if not doctrine.enabled:
            return self
        poll_seconds = self.mcp_device.ambient_poll_interval_seconds
        if poll_seconds is None:
            # Unknown, not incompatible. Requiring the value is a START-A-ROAST
            # precondition (``RoastService._require_explicit_ambient_cadence``),
            # deliberately NOT a construction one: making it a second way for
            # this validator to raise meant recovery — which rebuilds an
            # ``AppConfig`` for a run already in progress — retired the doctrine
            # merely because the live cadence was unset, silently changing the
            # fan advice an operator resumed into. An in-flight run's own
            # configuration is not the place to enforce an authoring rule.
            return self
        if doctrine.max_reading_age_seconds < poll_seconds:
            raise ValueError(
                "controller.ambient_fan_doctrine.max_reading_age_seconds must be at least "
                "mcp_device.ambient_poll_interval_seconds while the doctrine is enabled, or "
                "every healthy reading ages past the bound before the next poll lands, is "
                "declined as stale, and c11 silently runs its absent-ambient fallback for "
                f"the whole roast. Got {doctrine.max_reading_age_seconds:g} against a poll "
                f"interval of {poll_seconds:g}. Two or more poll intervals is the "
                "recommended operating margin."
            )
        return self
