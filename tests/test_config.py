"""E2-S3: configuration surface tests (component plan §4; orchestration plan
§ Configuration Model; D5).

Documented defaults, env-var loading (``ROASTPILOT_`` prefix with ``__``
nesting), validation rejections, and the guidance-vs-safety-bound link.
"""

import os

import pydantic
import pytest

from roastpilot_agent.config import (
    DEFAULT_ADVISOR_MODEL,
    HOTTOP_FAN_LEVEL_PP,
    OPENROUTER_BASE_URL,
    AdvisorConfig,
    AmbientFanDoctrine,
    AppConfig,
    BeanSourcingConfig,
    ControllerConfig,
    PostFirstCrackControl,
    SafetyLimits,
)
from roastpilot_agent.models import RoastPhase


def _declared_le(model: type[pydantic.BaseModel], field: str) -> float:
    """Return a field's declared ``le`` bound from its own constraint metadata.

    Lets a test assert against the REAL bound rather than a copy of it, so
    loosening the field breaks the test instead of silently leaving it
    asserting a stale literal.

    Args:
        model: The pydantic model owning the field.
        field: The field name.

    Returns:
        The declared ``le`` value.

    Raises:
        AssertionError: If the field declares no ``le`` constraint.
    """
    for meta in model.model_fields[field].metadata:
        bound = getattr(meta, "le", None)
        if bound is not None:
            return float(bound)
    raise AssertionError(f"{model.__name__}.{field} declares no le constraint")


def test_controller_defaults_match_orchestration_plan() -> None:
    config = ControllerConfig()
    assert config.tick_interval_seconds == 1.0
    assert config.advisory_min_temp_delta_c == 1.0
    assert config.advisory_min_ror_delta_c_per_min == 2.0
    # D32 (#191) + D40.5 (#276): cadence by FC-proximity. Preheat is OFF (not in
    # the map + excluded from auto-advice phases); pre-first-crack has NO fixed
    # heartbeat (None — change-based only); development consults at the deliberate
    # ~5 s post-FC cadence (#276), not every tick. None (not inf) so the frozen
    # config JSON stays valid (PR #201 / Codex).
    assert config.advisory_min_interval_seconds == {
        RoastPhase.ROASTING_PRE_FIRST_CRACK: None,
        RoastPhase.DEVELOPMENT: 5.0,
    }
    # D35 §4-A / D40.5 (#276): the post-FC loop knobs.
    assert config.post_fc_min_consult_interval_seconds == 5.0
    # Tuned from the operator's recorded post-FC behaviour (#277): 10 is the
    # largest threshold that damps ZERO of the operator's real >=10 pp reversals
    # (every Hottop lever move is quantised to 10 pp). See
    # tests/test_coherence.py::test_operator_recorded_reversals_pass_at_default_threshold.
    assert config.post_fc_deadband_threshold_percent == 10
    assert config.post_fc_min_confidence == 0.2
    assert config.advisory_near_fc_bean_temp_c == 170.0
    assert config.advisory_near_fc_interval_seconds == 10.0
    # #209: post-charge settle window — fallback timeout 90 s, turning-point RoR
    # threshold 0.0 (RoR crosses zero).
    assert config.advisory_post_charge_settle_max_seconds == 90.0
    assert config.advisory_post_charge_turning_point_ror_c_per_min == 0.0
    assert config.advisory_timeout_seconds == 10.0
    assert config.t0_debounce_ticks == 3
    assert config.telemetry_log_interval_seconds == 5.0
    assert config.max_stale_telemetry_seconds == 3.0


def test_post_charge_turning_point_ror_threshold_is_unbounded() -> None:
    """#209: the turning-point RoR threshold carries no gt/ge bound — exactly
    0.0 (the default zero-crossing) must validate, and a negative threshold
    (release a touch before the bean fully turns) is also a legal tuning."""
    assert ControllerConfig(advisory_post_charge_turning_point_ror_c_per_min=0.0)
    assert (
        ControllerConfig(
            advisory_post_charge_turning_point_ror_c_per_min=-5.0
        ).advisory_post_charge_turning_point_ror_c_per_min
        == -5.0
    )


def test_advisory_interval_for_resolves_per_phase_and_defaults_unthrottled() -> None:
    config = ControllerConfig()
    # Pre-first-crack has NO fixed heartbeat (None — change-based + near-FC only).
    assert config.advisory_interval_for(RoastPhase.ROASTING_PRE_FIRST_CRACK) is None
    # Development has the ~5 s post-FC heartbeat (#276); phases absent from the
    # map are unthrottled (0) — preheat (not an auto-advice phase) and cooling.
    assert config.advisory_interval_for(RoastPhase.DEVELOPMENT) == 5.0
    assert config.advisory_interval_for(RoastPhase.PREHEATING) == 0.0
    assert config.advisory_interval_for(RoastPhase.COOLING) == 0.0


def test_advisory_interval_is_config_tunable() -> None:
    config = ControllerConfig(advisory_min_interval_seconds={RoastPhase.PREHEATING: 45.0})
    assert config.advisory_interval_for(RoastPhase.PREHEATING) == 45.0
    # Unspecified phases fall back to unthrottled.
    assert config.advisory_interval_for(RoastPhase.ROASTING_PRE_FIRST_CRACK) == 0.0


def test_frozen_controller_config_is_strict_json_valid() -> None:
    """The controller config is frozen into ``roast_runs.config_json`` via
    ``model_dump(mode="json")`` + ``json.dumps`` (``RoastStore.create_run``).
    A non-JSON float token (``Infinity``/``NaN``) there is invalid JSON — SQLite
    ``json_valid`` and the SPA's ``JSON.parse`` reject it (PR #201 / Codex). The
    "no fixed heartbeat" sentinel must serialize as ``null``, never ``Infinity``.

    Regression guard: ``json.loads`` with a ``parse_constant`` that raises makes
    any bare ``Infinity``/``-Infinity``/``NaN`` a hard failure, not a silent
    Python-only round-trip (``json`` accepts those by default; strict readers
    do not)."""
    import json

    dumped = ControllerConfig().model_dump(mode="json")

    def _reject_constant(token: str) -> float:
        raise AssertionError(f"frozen config emitted non-JSON constant {token!r}")

    round_tripped = json.loads(json.dumps(dumped), parse_constant=_reject_constant)
    assert round_tripped["advisory_min_interval_seconds"] == {
        RoastPhase.ROASTING_PRE_FIRST_CRACK.value: None,
        RoastPhase.DEVELOPMENT.value: 5.0,
    }


def test_advisor_defaults_match_d5_d18_and_bakeoff() -> None:
    config = AdvisorConfig()
    assert config.provider == "openai_compatible"
    assert config.provider_base_url == "https://openrouter.ai/api/v1"
    # Drift guard (#590 P2 fix): AdvisorConfig's own default must stay in
    # lockstep with the shared OPENROUTER_BASE_URL constant
    # bean_sourcing._resolve_extraction_model_slug compares against.
    assert config.provider_base_url == OPENROUTER_BASE_URL
    assert config.api_key_env == "OPENROUTER_API_KEY"
    # #277 post-FC control bake-off PIN (21 Jun): gpt-4o via OpenRouter — closest
    # to the operator's real heat moves (heat MAE ~7.5 pp) and the proven n8n
    # baseline (model UNCHANGED by the c2/c3 prompt tuning). Prompt c3 (#274 +
    # roast-2 development-stretch + roast-3 fan-as-active-brake tuning) — the
    # control teaching SYSTEM frame wired live for the post-FC loop; c1/c2 stay
    # selectable.
    assert config.model_slug == "openai/gpt-4o"
    assert config.prompt_version == "c3"
    assert config.timeout_seconds == 10.0
    assert config.temperature == 0.0
    assert config.reasoning_effort is None  # provider default until measured


def test_advisor_per_phase_model_default_is_pinned_model_everywhere() -> None:
    """#173 MECHANISM: per-phase model selection defaults to the single pinned
    model (gpt-4o, #277 PIN) for every phase — the map is retained so a future
    re-run could flip a phase slot to a different model."""
    config = AdvisorConfig()
    assert DEFAULT_ADVISOR_MODEL == "openai/gpt-4o"
    # The base slug and every phase override are the same single model today.
    assert config.model_slug == DEFAULT_ADVISOR_MODEL
    assert config.model_slug_by_phase == {
        RoastPhase.PREHEATING: DEFAULT_ADVISOR_MODEL,
        RoastPhase.ROASTING_PRE_FIRST_CRACK: DEFAULT_ADVISOR_MODEL,
        RoastPhase.DEVELOPMENT: DEFAULT_ADVISOR_MODEL,
    }


def test_model_for_resolves_pinned_model_for_every_phase_by_default() -> None:
    """The resolver returns the pinned model in every phase (including phases
    absent from the map, which fall back to ``model_slug``) — the default
    no-op."""
    config = AdvisorConfig()
    for phase in RoastPhase:
        assert config.model_for(phase) == DEFAULT_ADVISOR_MODEL


def test_model_for_resolves_per_phase_override() -> None:
    """A custom per-phase map resolves as set; the eventual bake-off shape —
    capable pre-FC, fast post-FC — wired through the mechanism."""
    config = AdvisorConfig(
        model_slug="anthropic/claude-opus-4.8",
        model_slug_by_phase={
            RoastPhase.PREHEATING: "anthropic/claude-opus-4.8",
            RoastPhase.ROASTING_PRE_FIRST_CRACK: "anthropic/claude-opus-4.8",
            RoastPhase.DEVELOPMENT: "anthropic/claude-haiku-4.5",
        },
    )
    assert config.model_for(RoastPhase.PREHEATING) == "anthropic/claude-opus-4.8"
    assert config.model_for(RoastPhase.ROASTING_PRE_FIRST_CRACK) == "anthropic/claude-opus-4.8"
    assert config.model_for(RoastPhase.DEVELOPMENT) == "anthropic/claude-haiku-4.5"


def test_model_for_falls_back_to_base_slug_for_unmapped_phase() -> None:
    """A phase absent from the override map resolves to the base ``model_slug``."""
    config = AdvisorConfig(
        model_slug="anthropic/claude-opus-4.8",
        model_slug_by_phase={RoastPhase.DEVELOPMENT: "anthropic/claude-haiku-4.5"},
    )
    # COOLING is not in the map → base slug.
    assert config.model_for(RoastPhase.COOLING) == "anthropic/claude-opus-4.8"
    assert config.model_for(RoastPhase.DEVELOPMENT) == "anthropic/claude-haiku-4.5"


def test_safety_limit_defaults_are_conservative() -> None:
    limits = SafetyLimits()
    assert limits.pre_t0_max_bean_temp_c == 200.0
    # Software ceilings stay below hardware territory and above any
    # legitimate roast target.
    assert limits.pre_t0_max_bean_temp_c < limits.max_bean_temp_c
    assert limits.max_bean_temp_c < limits.max_env_temp_c
    assert limits.overrun_safe_fan_percent == 100
    assert limits.pre_t0_overrun_severity == "recovery"
    assert limits.min_seconds_between_commands == 2.0
    # D35 §3 drop/bitter ceilings (#273): the emergency-drop bound sits above the
    # ≤196 °C bitter ceiling, and both stay below the hard bean-temp ceiling.
    assert limits.bitter_ceiling_temp_c == 196.0
    assert limits.emergency_drop_temp_c == 198.0
    assert limits.bitter_ceiling_temp_c < limits.emergency_drop_temp_c
    assert limits.emergency_drop_temp_c < limits.max_bean_temp_c


@pytest.mark.parametrize(
    ("model", "overrides"),
    [
        (ControllerConfig, {"tick_interval_seconds": 0}),
        (ControllerConfig, {"tick_interval_seconds": -1.0}),
        (ControllerConfig, {"t0_debounce_ticks": 0}),
        (ControllerConfig, {"advisory_timeout_seconds": 0}),
        (ControllerConfig, {"max_stale_telemetry_seconds": 0}),
        # #171: a negative phase consult floor is meaningless (0 = unthrottled).
        (ControllerConfig, {"advisory_min_interval_seconds": {RoastPhase.PREHEATING: -1.0}}),
        # D32 (#191): near-FC threshold and interval must be positive.
        (ControllerConfig, {"advisory_near_fc_bean_temp_c": 0}),
        (ControllerConfig, {"advisory_near_fc_interval_seconds": 0}),
        # #209: the post-charge settle fallback timeout must be positive
        # (gt=0); the turning-point RoR threshold has no bound (0 is valid).
        (ControllerConfig, {"advisory_post_charge_settle_max_seconds": 0}),
        (AdvisorConfig, {"timeout_seconds": 0}),
        (AdvisorConfig, {"temperature": -0.1}),
        (AdvisorConfig, {"temperature": 2.1}),
        (AdvisorConfig, {"api_key_env": ""}),
        (AdvisorConfig, {"prompt_version": ""}),
        (AdvisorConfig, {"model_slug": ""}),
        # #173: an empty per-phase model slug is meaningless.
        (AdvisorConfig, {"model_slug_by_phase": {RoastPhase.DEVELOPMENT: ""}}),
        (SafetyLimits, {"max_bean_temp_c": 0}),
        (SafetyLimits, {"overrun_safe_fan_percent": 101}),
        (SafetyLimits, {"pre_t0_overrun_severity": "explode"}),
        (SafetyLimits, {"min_seconds_between_commands": 0}),
        # #294 (D35 §3): inverted drop ceilings — emergency-drop must sit above
        # the bitter ceiling, so a 200/198 pair is rejected (mirrors the
        # rejection proven in test_control_policy).
        (
            SafetyLimits,
            {"bitter_ceiling_temp_c": 200.0, "emergency_drop_temp_c": 198.0},
        ),
        # #294 (D35 §3): a told ceiling at/above the hard enforced
        # ``max_bean_temp_c`` is a misconfiguration the gate can never honour.
        (
            SafetyLimits,
            {"max_bean_temp_c": 195.0, "bitter_ceiling_temp_c": 196.0},
        ),
        (
            SafetyLimits,
            {"max_bean_temp_c": 197.0, "emergency_drop_temp_c": 198.0},
        ),
    ],
)
def test_config_rejects_nonsense(
    model: type[pydantic.BaseModel], overrides: dict[str, object]
) -> None:
    with pytest.raises(pydantic.ValidationError):
        model.model_validate(overrides)


def test_app_config_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_"):
            monkeypatch.delenv(key)
    config = AppConfig()
    assert config.controller == ControllerConfig()
    assert config.advisor == AdvisorConfig()
    assert config.safety == SafetyLimits()
    assert config.bean_sourcing == BeanSourcingConfig()


def test_bean_sourcing_config_defaults() -> None:
    """#573 phase 1 + #590 slice A: sane, conservative add-bean-from-URL
    fetch limits, and a dedicated (longer) extraction timeout, decoupled
    from the roast-advice config. ``model_slug`` defaults to ``None`` — a
    sentinel meaning "resolve provider-aware"
    (``bean_sourcing._resolve_extraction_model_slug``, #590 P1 fix), not a
    fixed OpenRouter slug that would be invalid against a native advisor
    provider."""
    config = BeanSourcingConfig()
    assert config.fetch_timeout_seconds == 10.0
    assert config.max_response_bytes == 2_000_000
    assert config.user_agent
    assert "RoastPilotAgent" in config.user_agent
    assert config.extraction_timeout_seconds == 45.0
    assert config.model_slug is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"fetch_timeout_seconds": 0},
        {"fetch_timeout_seconds": -1},
        {"max_response_bytes": 0},
        {"user_agent": ""},
        {"extraction_timeout_seconds": 0},
        {"extraction_timeout_seconds": -1},
        {"model_slug": ""},
    ],
)
def test_bean_sourcing_config_rejects_nonsense(overrides: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        BeanSourcingConfig.model_validate(overrides)


@pytest.mark.parametrize("value", [260.0, 0.0, -500.0, float("nan"), float("inf")])
def test_ambient_fan_threshold_rejects_out_of_range_and_non_finite(value: float) -> None:
    """#709. This field exists to be RE-FIT BY HAND from RP-D scores, so a typo
    at exactly that moment is the plausible failure: unbounded, it accepts
    260.0 for 26.0 and silently puts every roast in the graduated regime.

    ``nan`` is quieter and worse — pydantic serialises it to ``null`` in
    ``model_dump_json``, so the advisor sees the field as ABSENT and takes the
    no-ambient branch rather than any doctrine at all."""
    with pytest.raises(pydantic.ValidationError):
        AmbientFanDoctrine(threshold_c=value)


def test_ambient_fan_threshold_accepts_a_real_refit() -> None:
    """The bounds must not obstruct a genuine re-fit across the corpus's real
    ambient span (23.1-31.6 °C), which is the whole point of holding the
    ratified ~26 °C value as config rather than prose."""
    assert AmbientFanDoctrine().threshold_c == 26.0
    assert AmbientFanDoctrine(threshold_c=23.5).threshold_c == 23.5
    assert AmbientFanDoctrine(threshold_c=31.6).threshold_c == 31.6


def test_ambient_fan_step_is_a_whole_number_of_real_hottop_levels() -> None:
    """#709 / D126, pinned against the REAL driver rather than a copy of its
    formula. The bound only means anything if a step of this size maps to a
    whole number of physical Hottop fan levels; that mapping lives in the
    pinned ``coffee-roaster-mcp`` dependency, so this imports and calls it. A
    hand-copied formula would stay green in CI while silently ceasing to hold
    on real hardware after a dependency bump — and `mcp-contract-checker`
    audits the ``mcp_client`` mirrors, not a formula re-typed in a test body.

    Pinned as the PROPERTY, not the number: the default step must move the
    hardware exactly one level from every legal starting fan value."""
    from coffee_roaster_mcp import drivers

    hottop_level = drivers._percent_to_hottop_fan_scale  # pyright: ignore[reportPrivateUsage]

    step = AmbientFanDoctrine().step_max_pp
    assert step == 10.0
    for start in range(0, int(100 - step) + 1):
        moved = hottop_level(int(start + step)) - hottop_level(start)
        assert moved == 1, f"fan {start} +{step:g} moved {moved} levels, not exactly one"


@pytest.mark.parametrize("value", [15.0, 5.0, 12.5, 0.0, -10.0, 30.0, 100.0, 101.0, float("nan")])
def test_ambient_fan_step_rejects_a_non_level_step(value: float) -> None:
    """#709 / D126: a re-fit to 15.0 would silently reopen the told-vs-enforced
    gap the field exists to close (from fan 30, a 15 pp step is a 20 pp
    physical move), so a step that is not a whole number of Hottop levels is
    rejected at construction rather than merely discouraged in a comment."""
    with pytest.raises(pydantic.ValidationError):
        AmbientFanDoctrine(step_max_pp=value)


def test_ambient_fan_step_accepts_a_whole_level_refit() -> None:
    """A re-fit stays possible — it just has to land on a representable step,
    and stay inside the two-level ceiling."""
    assert AmbientFanDoctrine(step_max_pp=20.0).step_max_pp == 20.0


def test_ambient_fan_step_cannot_be_widened_into_a_slam() -> None:
    """#709, independent-triage blocker. A whole-multiple rule ALONE accepts
    100.0 — a full floor-to-ceiling move as an ORDINARY, non-emergency,
    below-threshold step. That would falsify the doctrine's own "bounds the
    STEP, never the destination" claim and, with no deterministic slew clamp in
    this release, nothing downstream would catch it: the fan slam simply moves
    from the prose into the config.

    Derives the ceiling from the FIELD'S OWN constraint rather than restating
    today's value, so the test tracks the real bound. A literal would keep
    passing if ``le`` were loosened part-way (say to 30.0) — still true of the
    literal, no longer true of the field — which is precisely the regression
    this test exists to catch."""
    from coffee_roaster_mcp import drivers

    hottop_level = drivers._percent_to_hottop_fan_scale  # pyright: ignore[reportPrivateUsage]
    ceiling = _declared_le(AmbientFanDoctrine, "step_max_pp")

    # The widest step the field will EVER accept must still be graduation:
    # never more than two Hottop levels of travel.
    assert hottop_level(int(ceiling)) - hottop_level(0) <= 2
    # And it must genuinely BE the boundary — accepted at the ceiling, rejected
    # one whole level above it.
    assert AmbientFanDoctrine(step_max_pp=ceiling).step_max_pp == ceiling
    with pytest.raises(pydantic.ValidationError):
        AmbientFanDoctrine(step_max_pp=ceiling + HOTTOP_FAN_LEVEL_PP)


def test_ambient_fan_doctrine_is_inert_by_default() -> None:
    """#709: the doctrine mirrors ``reference_curve``'s inert-until-enabled
    posture, so merging it changes no roast. Enabling is deliberately two acts
    (select c11 AND set this flag), the same pairing c9 has with
    ``reference_curve.enabled``."""
    assert ControllerConfig().ambient_fan_doctrine.enabled is False


def test_app_config_loads_nested_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("ROASTPILOT_ADVISOR__MODEL_SLUG", "anthropic/claude-sonnet-4.6")
    monkeypatch.setenv("ROASTPILOT_SAFETY__PRE_T0_OVERRUN_SEVERITY", "fault")
    config = AppConfig()
    assert config.controller.tick_interval_seconds == 0.5
    assert config.advisor.model_slug == "anthropic/claude-sonnet-4.6"
    assert config.safety.pre_t0_overrun_severity == "fault"
    # Untouched sections keep their defaults.
    assert config.controller.t0_debounce_ticks == 3
    assert config.safety.max_bean_temp_c == 230.0


def test_app_config_rejects_invalid_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS", "0")
    with pytest.raises(pydantic.ValidationError):
        AppConfig()


# --- D88 amendment A1: the post-FC ceiling guard must sit within the ---
# --- safety-owned bounds it anchors to (a cross-SECTION check on AppConfig) ---


def test_ceiling_guard_defaults_are_valid() -> None:
    """The default 196.0 guard sits below the default 198.0 emergency-drop
    bound and at (not above) the default 196.0 bitter ceiling — constructs
    cleanly with no overrides. ``ceiling_guard_drop_enabled`` defaults
    ``True`` as of the 12 Jul D88/D89 promotion (operator-ratified on the
    11 Jul validation roast + 9/10 tasting) — was ``False`` before the flip;
    deliberately updated, not silently passed."""
    config = AppConfig()
    assert config.controller.post_first_crack_control.ceiling_guard_temp_c == 196.0
    assert config.controller.post_first_crack_control.ceiling_guard_drop_enabled is True


def test_ceiling_guard_at_or_above_emergency_drop_temp_is_rejected() -> None:
    """A guard AT the emergency-drop bound (never fires before the hard net
    already forces the issue) is rejected — the cross-field check is a
    strict ``<``, not ``<=``."""
    with pytest.raises(pydantic.ValidationError, match="ceiling_guard_temp_c must be below"):
        AppConfig(
            controller=ControllerConfig(
                post_first_crack_control=PostFirstCrackControl(ceiling_guard_temp_c=198.0)
            ),
            safety=SafetyLimits(bitter_ceiling_temp_c=196.0, emergency_drop_temp_c=198.0),
        )


def test_ceiling_guard_above_emergency_drop_temp_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError, match="ceiling_guard_temp_c must be below"):
        AppConfig(
            controller=ControllerConfig(
                post_first_crack_control=PostFirstCrackControl(ceiling_guard_temp_c=199.0)
            ),
            safety=SafetyLimits(bitter_ceiling_temp_c=196.0, emergency_drop_temp_c=198.0),
        )


def test_ceiling_guard_above_bitter_ceiling_temp_is_rejected() -> None:
    """A guard ABOVE the bitter ceiling would let the roast run hotter than
    the very bitter-line boundary it is meant to anchor before the guard
    even engages."""
    with pytest.raises(pydantic.ValidationError, match="ceiling_guard_temp_c must not exceed"):
        AppConfig(
            controller=ControllerConfig(
                post_first_crack_control=PostFirstCrackControl(ceiling_guard_temp_c=197.0)
            ),
            safety=SafetyLimits(bitter_ceiling_temp_c=196.0, emergency_drop_temp_c=198.0),
        )


def test_ceiling_guard_equal_to_bitter_ceiling_temp_is_valid() -> None:
    """Exactly AT the bitter ceiling is valid — the cross-field check on the
    bitter-ceiling leg is ``<=``, not a strict ``<`` (mirrors the D88 row's
    own wording: "``ceiling_guard_temp_c`` ... ``<= bitter_ceiling_temp_c``")."""
    config = AppConfig(
        controller=ControllerConfig(
            post_first_crack_control=PostFirstCrackControl(ceiling_guard_temp_c=196.0)
        ),
        safety=SafetyLimits(bitter_ceiling_temp_c=196.0, emergency_drop_temp_c=198.0),
    )
    assert config.controller.post_first_crack_control.ceiling_guard_temp_c == 196.0
