"""E2-S3: configuration surface tests (component plan §4; orchestration plan
§ Configuration Model; D5).

Documented defaults, env-var loading (``ROASTPILOT_`` prefix with ``__``
nesting), validation rejections, and the guidance-vs-safety-bound link.
"""

# Disposable CI-classifier proof fixture: this comment intentionally changes no behaviour.

import inspect
import json
import os
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Union, get_args, get_origin

import pydantic
import pytest

from roastpilot_agent.config import (
    DEFAULT_ADVISOR_MODEL,
    DEFAULT_MCP_AMBIENT_POLL_INTERVAL_SECONDS,
    FINITE_NUMERIC_MODEL_CONFIG,
    HOTTOP_FAN_LEVEL_PP,
    OPENROUTER_BASE_URL,
    AdvisorConfig,
    AmbientFanDoctrine,
    AppConfig,
    BeanSourcingConfig,
    ControllerConfig,
    JointWindowPlanner,
    LateMaillardTrim,
    LoggingConfig,
    MCPConfig,
    MCPDeviceConfig,
    PostFirstCrackControl,
    PreFirstCrackLevers,
    ReferenceCurve,
    SafetyLimits,
)
from roastpilot_agent.models import RoastPhase
from roastpilot_agent.store import FrozenRunConfig


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


def test_trim_authority_comment_distinguishes_override_and_ceiling_terms() -> None:
    """The canonical trim commentary keeps the D88/D96 truth classes distinct."""
    source = " ".join(inspect.getsource(LateMaillardTrim).replace("#: ", "").split())

    assert "lower per-bean ``pre_fc_heat`` can bind below that resolved depth" in source
    assert (
        "``pre_fc_heat`` instead replaces the flat configured floor and may be higher or lower"
        in source
    )
    assert "``max(1, min(heat_ceiling_percent, heat_engage_percent))``" in source
    assert "``min(heat_ceiling_percent, heat_engage_percent +" in source
    assert "``max(base_ceiling, recovery_term)``, never below the D88 base" in source
    assert "actual actuated FC heat 65" in source
    assert "actual actuated FC heat 60" in source
    assert "65/80" in source
    assert "60/75" in source


@pytest.mark.docs
def test_runbook_recovery_off_describes_the_d88_ceiling_not_heat_direction() -> None:
    """Recovery-off runbook text keeps PI heat motion bounded, not one-way."""
    runbook = (Path(__file__).parents[1] / "docs" / "roast-session-runbook.md").read_text()

    assert "heat may move only up to, never above, the D88 base cap" in runbook
    assert "exceeding it is a stop-and-record event" in runbook
    assert "heat only moves DOWN from the D88 base cap" not in runbook


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
    # Lowered 10.0 -> 5.0 (operator, 9 Aug 2026, #747 / D151) to match the ~5 s
    # FC-slot screen the advisor roster is chosen against. This is the bound on
    # how long a slow model can hold the control loop off its next safety
    # evaluation and its next drain of the operator queue (where the in-UI
    # emergency stop is consumed), so holding it at 2x the screen let an
    # unscreened model delay the loop for twice as long as any model we would
    # knowingly run.
    assert config.advisory_timeout_seconds == 5.0
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


def test_advisor_per_phase_model_map_ships_empty() -> None:
    """D151 (#747): the per-phase override map is EMPTY by default.

    It used to ship populated with the pinned model for every phase, which —
    since the map is absent from ``AdvisorConfigEdit`` and so unreachable from
    ``/config`` — silently shadowed every operator-set ``model_slug``. The pin
    itself is unchanged; it is now the FIELD DEFAULT rather than an override
    the operator cannot see or reach.
    """
    config = AdvisorConfig()
    assert DEFAULT_ADVISOR_MODEL == "openai/gpt-4o"
    assert config.model_slug == DEFAULT_ADVISOR_MODEL
    assert config.model_slug_by_phase == {}


def test_model_for_resolves_pinned_model_for_every_phase_by_default() -> None:
    """The resolver returns the pinned model in every phase — now by FALLING
    BACK to ``model_slug`` for every phase rather than by reading an override,
    so the default behaviour is unchanged while the field becomes effective."""
    config = AdvisorConfig()
    for phase in RoastPhase:
        assert config.model_for(phase) == DEFAULT_ADVISOR_MODEL


def test_configured_model_slug_governs_the_phase_that_consults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #747 regression: a set ``model_slug`` IS the model that answers.

    Guards both routes the operator actually uses — a constructed/saved value
    and the documented ``ROASTPILOT_ADVISOR__MODEL_SLUG`` env var — against the
    exact failure that ran roast 8 on gpt-4o while its launch line, D73/D74 and
    #396 all recorded a gpt-4.1-mini arm. DEVELOPMENT is the phase under D35
    that consults the advisor at all, so it is the one that has to be right.
    """
    assert AdvisorConfig(model_slug="openai/gpt-4.1-mini").model_for(RoastPhase.DEVELOPMENT) == (
        "openai/gpt-4.1-mini"
    )

    monkeypatch.setenv("ROASTPILOT_ADVISOR__MODEL_SLUG", "openai/gpt-4.1-mini")
    assert AppConfig().advisor.model_for(RoastPhase.DEVELOPMENT) == "openai/gpt-4.1-mini"


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
    assert limits.max_env_temp_c == 240.0
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


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "field",
    ["max_bean_temp_c", "max_env_temp_c", "pre_t0_max_bean_temp_c"],
)
def test_temperature_safety_ceilings_reject_non_finite_values(
    field: str, non_finite: float
) -> None:
    """A non-finite configured bound must not disable a hard safety guard."""
    with pytest.raises(pydantic.ValidationError):
        SafetyLimits.model_validate({field: non_finite})


@pytest.mark.parametrize(
    ("value", "is_valid"),
    [
        (float("nan"), False),
        (float("inf"), False),
        (float("-inf"), False),
        (30.0, True),
    ],
)
@pytest.mark.parametrize(
    ("model", "field"),
    [
        (ControllerConfig, "max_stale_telemetry_seconds"),
        (SafetyLimits, "min_seconds_between_commands"),
    ],
)
def test_control_safety_intervals_require_finite_values(
    model: type[ControllerConfig] | type[SafetyLimits], field: str, value: float, is_valid: bool
) -> None:
    """Timing bounds reject non-finite values without rejecting valid ones."""
    if is_valid:
        validated = model.model_validate({field: value})
        assert getattr(validated, field) == value
    else:
        with pytest.raises(pydantic.ValidationError):
            model.model_validate({field: value})


@pytest.mark.parametrize(
    ("model", "overrides"),
    [
        (ControllerConfig, {"tick_interval_seconds": 0}),
        (ControllerConfig, {"tick_interval_seconds": -1.0}),
        (ControllerConfig, {"t0_debounce_ticks": 0}),
        (ControllerConfig, {"advisory_timeout_seconds": 0}),
        (ControllerConfig, {"advisory_timeout_seconds": float("-inf")}),
        (ControllerConfig, {"operator_timeout_seconds": float("-inf")}),
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
        (PostFirstCrackControl, {"taper_duration_seconds": float("-inf")}),
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


def _reachable_config_models(root: type[pydantic.BaseModel]) -> set[type[pydantic.BaseModel]]:
    """Walk the AppConfig field tree to the nested configuration models."""
    seen: set[type[pydantic.BaseModel]] = set()

    def walk_annotation(annotation: object) -> None:
        if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
            walk_model(annotation)
            return
        origin = get_origin(annotation)
        if origin is Annotated:
            walk_annotation(get_args(annotation)[0])
        elif origin in {Union, types.UnionType, list, tuple, dict}:
            for argument in get_args(annotation):
                walk_annotation(argument)

    def walk_model(model: type[pydantic.BaseModel]) -> None:
        if model in seen:
            return
        seen.add(model)
        for field in model.model_fields.values():
            walk_annotation(field.rebuild_annotation())

    walk_model(root)
    return seen


def _float_payload_kind(annotation: object) -> str | None:
    """Classify supported float-bearing config field shapes for T1."""
    if annotation is float:
        return "scalar"
    origin = get_origin(annotation)
    if origin is Annotated:
        return _float_payload_kind(get_args(annotation)[0])
    if origin in {Union, types.UnionType}:
        kinds = {
            kind
            for argument in get_args(annotation)
            if argument is not type(None)
            if (kind := _float_payload_kind(argument)) is not None
        }
        if len(kinds) == 1:
            return kinds.pop()
    if origin in {dict, Mapping}:
        value_kind = _float_payload_kind(get_args(annotation)[1])
        if value_kind == "scalar":
            return "mapping"
    if "float" in repr(annotation):
        pytest.fail(f"T1 does not understand float-bearing annotation {annotation!r}")
    return None


def test_app_config_tree_rejects_non_finite_floats_reflectively() -> None:
    """T1: every reachable config model rejects non-finite numeric values."""
    models = _reachable_config_models(AppConfig)
    expected_floor = {
        AppConfig,
        ControllerConfig,
        PreFirstCrackLevers,
        LateMaillardTrim,
        PostFirstCrackControl,
        ReferenceCurve,
        AmbientFanDoctrine,
        AdvisorConfig,
        SafetyLimits,
        MCPConfig,
        MCPDeviceConfig,
        LoggingConfig,
        BeanSourcingConfig,
    }
    assert expected_floor <= models
    assert FINITE_NUMERIC_MODEL_CONFIG == {"allow_inf_nan": False}

    float_fields: set[str] = set()
    for model in models:
        assert model.model_config.get("allow_inf_nan") is False, model.__name__
        for name, field in model.model_fields.items():
            kind = _float_payload_kind(field.rebuild_annotation())
            if kind is None:
                continue
            float_fields.add(name)
            for non_finite in (float("inf"), float("-inf"), float("nan")):
                value: Any = (
                    {RoastPhase.DEVELOPMENT: non_finite} if kind == "mapping" else non_finite
                )
                with pytest.raises(pydantic.ValidationError) as raised:
                    model.model_validate({name: value})
                assert any(
                    error["type"] == "finite_number" and error["loc"][0] == name
                    for error in raised.value.errors()
                )
    assert {
        "tick_interval_seconds",
        "kp_percent_per_ror",
        "ki_percent_per_ror_second",
        "k_ror",
        "threshold_c",
        "timeout_seconds",
        "fetch_timeout_seconds",
        "call_timeout_seconds",
        "stop_timeout_seconds",
        "ambient_poll_interval_seconds",
        "advisory_min_interval_seconds",
    } <= float_fields


def test_finite_config_values_and_none_sentinels_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2: finite values and documented None sentinels retain their behaviour."""
    for key in list(os.environ):
        if key.startswith("ROASTPILOT_"):
            monkeypatch.delenv(key)
    assert AppConfig() == AppConfig()
    assert ControllerConfig(tick_interval_seconds=1e300).tick_interval_seconds == 1e300
    assert (
        ControllerConfig(
            advisory_post_charge_turning_point_ror_c_per_min=-5.0
        ).advisory_post_charge_turning_point_ror_c_per_min
        == -5.0
    )
    assert (
        ControllerConfig(
            advisory_min_interval_seconds={RoastPhase.DEVELOPMENT: None}
        ).advisory_min_interval_seconds[RoastPhase.DEVELOPMENT]
        is None
    )
    assert MCPDeviceConfig(ambient_poll_interval_seconds=None).ambient_poll_interval_seconds is None
    assert PostFirstCrackControl().recovery_projection_entry_horizon_pp == 2.0
    assert (
        PostFirstCrackControl(
            recovery_projection_cutoff_horizon_pp=20
        ).recovery_projection_cutoff_horizon_pp
        == 20
    )
    with pytest.raises(pydantic.ValidationError):
        PostFirstCrackControl(recovery_projection_entry_horizon_pp=21)
    with pytest.raises(pydantic.ValidationError):
        PostFirstCrackControl(recovery_projection_cutoff_horizon_pp=21)


@pytest.mark.parametrize(
    "payload",
    [
        {"controller": {"tick_interval_seconds": float("inf")}},
        {"controller": {"post_first_crack_control": {"kp_percent_per_ror": float("inf")}}},
        {"controller": {"pre_first_crack_levers": {"late_maillard_trim": {"k_ror": float("nan")}}}},
        {"mcp": {"stop_timeout_seconds": float("inf")}},
        {"safety": {"bitter_ceiling_temp_c": float("-inf")}},
        {"mcp_device": {"ambient_poll_interval_seconds": float("inf")}},
    ],
)
def test_app_config_nested_payload_rejects_non_finite_numbers(payload: dict[str, object]) -> None:
    """T3: ordinary nested Python payloads fail closed at the config boundary."""
    with pytest.raises(pydantic.ValidationError) as raised:
        AppConfig.model_validate(payload)
    assert any(error["type"] == "finite_number" for error in raised.value.errors())


@pytest.mark.parametrize("value", ["inf", "Infinity", "-inf", "nan", "1e999"])
def test_app_config_environment_rejects_non_finite_primary_tick(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """T4: environment strings cannot bypass finite model validation."""
    monkeypatch.setenv("ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS", value)
    with pytest.raises(pydantic.ValidationError):
        AppConfig()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ROASTPILOT_MCP__CALL_TIMEOUT_SECONDS", "inf"),
        ("ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__KI_PERCENT_PER_ROR_SECOND", "inf"),
        ("ROASTPILOT_SAFETY__MAX_BEAN_TEMP_C", "inf"),
        ("ROASTPILOT_CONTROLLER", '{"tick_interval_seconds": Infinity}'),
    ],
)
def test_app_config_environment_rejects_non_finite_nested_values(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    """T4: nested and JSON environment config obeys the same finite boundary."""
    monkeypatch.setenv(key, value)
    with pytest.raises(pydantic.ValidationError):
        AppConfig()


def test_app_config_environment_accepts_finite_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """T4: a valid finite environment override remains configurable."""
    monkeypatch.setenv("ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS", "0.5")
    assert AppConfig().controller.tick_interval_seconds == 0.5


def test_frozen_run_config_rejects_infinite_json() -> None:
    """T8: recovery cannot deserialize a persisted non-finite frozen config."""
    payload = {
        "controller": {
            **ControllerConfig().model_dump(mode="json"),
            "tick_interval_seconds": float("inf"),
        },
        "safety": SafetyLimits().model_dump(mode="json"),
    }
    with pytest.raises(pydantic.ValidationError) as raised:
        FrozenRunConfig.model_validate_json(json.dumps(payload))
    assert any(error["type"] == "finite_number" for error in raised.value.errors())


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


def test_ambient_fan_doctrine_ceiling_defaults_off_at_70() -> None:
    """The deterministic destination ceiling ships inert at Hottop level 7."""
    doctrine = AmbientFanDoctrine()
    assert doctrine.post_fc_fan_ceiling_enabled is False
    assert doctrine.post_fc_fan_ceiling_percent == 70


@pytest.mark.parametrize("value", [75, 15])
def test_ceiling_rejects_a_non_multiple_of_10(value: int) -> None:
    """D126: only exactly actuatable Hottop fan levels may be ceilings."""
    with pytest.raises(pydantic.ValidationError, match=r"whole multiple.*D126"):
        AmbientFanDoctrine(post_fc_fan_ceiling_percent=value)


def test_ceiling_bounds() -> None:
    """The ceiling preserves airflow and can never invert the fan box."""
    for value in (10, 50, 100):
        assert (
            AmbientFanDoctrine(post_fc_fan_ceiling_percent=value).post_fc_fan_ceiling_percent
            == value
        )
    for value in (0, 105):
        with pytest.raises(pydantic.ValidationError):
            AmbientFanDoctrine(post_fc_fan_ceiling_percent=value)


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_ceiling_is_finite_by_construction(value: float) -> None:
    """The integer config type rejects non-finite destination ceilings."""
    with pytest.raises(pydantic.ValidationError):
        AmbientFanDoctrine(post_fc_fan_ceiling_percent=value)  # type: ignore[arg-type]


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


@pytest.mark.parametrize("value", [900.0, 601.0, 0.0, -30.0, float("inf"), float("nan")])
def test_ambient_freshness_bound_cannot_be_widened_into_no_bound(value: float) -> None:
    """#732, and the SECOND occurrence of the shape independent triage blocked
    on #709's ``step_max_pp``: a hand-refit knob whose ceiling sits far above
    its intent lets a plausible typo validate.

    ``900.0`` for ``90.0`` passes a ``gt=0.0``-only rule and silently disables
    the staleness guard for most of a 12-20 minute roast — the guard is still
    there, still tested, and no longer guarding anything. ``inf`` disables it
    outright AND freezes a bare ``Infinity`` token into the per-run
    ``config_json``, which is not valid strict JSON. ``nan`` is quieter still:
    every comparison against it is ``False``, so the bound would never fire.

    The whole point is a config-only re-fit, so nothing downstream would catch
    any of these — validation is the only place they can be caught."""
    with pytest.raises(pydantic.ValidationError):
        AmbientFanDoctrine(max_reading_age_seconds=value)


def test_ambient_freshness_bound_accepts_a_real_refit() -> None:
    """#732: the operator can genuinely re-fit it — a slower ambient poll wants
    a wider bound, and the tighter 60 s (two poll cycles) option must hold too.
    Asserted against the field's own declared ceiling so the test tracks the
    real bound rather than restating today's literal."""
    ceiling = _declared_le(AmbientFanDoctrine, "max_reading_age_seconds")

    assert AmbientFanDoctrine(max_reading_age_seconds=60.0).max_reading_age_seconds == 60.0
    assert AmbientFanDoctrine(max_reading_age_seconds=ceiling).max_reading_age_seconds == ceiling
    # A whole roast's length is the reasoning behind the ceiling: past that it
    # is not a freshness bound at all.
    assert ceiling <= 600.0


def test_mcp_ambient_poll_default_matches_the_installed_server() -> None:
    """#732 drift guard: the mirrored MCP poll cadence must equal the real one.

    ``DEFAULT_MCP_AMBIENT_POLL_INTERVAL_SECONDS`` is a COPY of the server's own
    default, used to validate the doctrine's freshness bound when the operator
    has set no interval. A server bump that changed the cadence would silently
    narrow (or invert) that margin — the bound would still validate while every
    healthy reading aged past it. Assert against the installed package so the
    bump fails here instead of on the roast."""
    from coffee_roaster_mcp.config import AmbientConfig

    assert AmbientConfig().poll_interval_seconds == DEFAULT_MCP_AMBIENT_POLL_INTERVAL_SECONDS


def test_ambient_freshness_bound_must_outlive_the_poll_interval() -> None:
    """#732, the pre-open safety review's finding 1. The poll interval is
    operator-editable from ``/config`` with no maximum; the freshness bound is
    file-only. Set the interval above the bound and EVERY healthy reading is
    declined on EVERY tick — for the whole roast, with no log, no event, and a
    Room tile still showing a temperature the advisor never received.

    The direction is fail-safe, but an RP-B hardware arm would then be recorded
    as "c11 with ambient" while the model saw the absent branch throughout: a
    green, meaningless result. Cross-section validation is the only place this
    is catchable — neither config group can see the other.

    The threshold is the CORRECTNESS line (bound >= cadence), not a preferred
    margin. Post-open review showed a 2x rule failing a 90 s bound against a
    60 s cadence, where the doctrine in fact works perfectly well — and, via
    recovery, retiring a doctrine that was serving fresh readings."""
    doctrine = AmbientFanDoctrine(enabled=True, max_reading_age_seconds=90.0)

    # 90 s against a 30 s cadence is three polls — comfortably valid.
    assert AppConfig(
        controller=ControllerConfig(ambient_fan_doctrine=doctrine),
        mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=30.0),
    )

    # 90 s against a 60 s cadence is NOT rejected: a healthy reading is at its
    # oldest just before the next poll, so 60 < 90 means it always arrives
    # fresh. Rejecting this pair is the bug post-open review caught.
    assert AppConfig(
        controller=ControllerConfig(ambient_fan_doctrine=doctrine),
        mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=60.0),
    )

    # A cadence that genuinely outruns the bound IS rejected at construction,
    # rather than silently voiding the doctrine at roast time.
    with pytest.raises(pydantic.ValidationError, match="max_reading_age_seconds"):
        AppConfig(
            controller=ControllerConfig(ambient_fan_doctrine=doctrine),
            mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=120.0),
        )


def test_an_unset_cadence_does_not_block_config_construction() -> None:
    """#732, post-open Codex round 4: an unset cadence is UNKNOWN, not incompatible.

    Requiring the value is a start-a-roast precondition
    (``RoastService._require_explicit_ambient_cadence``), deliberately not a
    construction one. As a construction rule it also fired during recovery,
    which rebuilds a config for a run already in progress — so an operator
    resume retired the doctrine merely because the CURRENT cadence was unset,
    silently changing the fan advice mid-run. An in-flight run's own
    configuration is not the place to enforce an authoring rule."""
    assert MCPDeviceConfig().ambient_poll_interval_seconds is None
    assert AppConfig(
        controller=ControllerConfig(
            ambient_fan_doctrine=AmbientFanDoctrine(enabled=True, max_reading_age_seconds=90.0)
        ),
        mcp_device=MCPDeviceConfig(),
    )


def test_ambient_freshness_bound_is_unconstrained_while_the_doctrine_is_inert() -> None:
    """#732: the cross-check binds only when the doctrine is ENABLED.

    The doctrine ships inert, so a config that would be rejected with the flag
    on must stay constructible with it off — otherwise #732 could make an
    existing, working, doctrine-free deployment unbootable, which is exactly
    the kind of blast radius an inert feature is supposed to preclude."""
    assert AppConfig(
        controller=ControllerConfig(
            ambient_fan_doctrine=AmbientFanDoctrine(max_reading_age_seconds=10.0)
        ),
        mcp_device=MCPDeviceConfig(ambient_poll_interval_seconds=600.0),
    )


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


# --- #710 (RP-C) slice 1, D177: the joint-window planner requires the ---
# --- deterministic ceiling guard (a cross-field check on ControllerConfig) ---


def test_joint_window_planner_defaults() -> None:
    """T22: the planner group is inert by default with the D176-ratified
    numbers."""
    planner = JointWindowPlanner()
    assert planner.enabled is False
    assert planner.temp_margin_c == 3.0
    assert planner.closing_horizon_seconds == 30.0


@pytest.mark.parametrize("value", [0.0, -0.1])
def test_joint_window_planner_temp_margin_must_be_positive(value: float) -> None:
    """The temperature margin rejects zero and negative values at the boundary."""
    with pytest.raises(pydantic.ValidationError):
        JointWindowPlanner(temp_margin_c=value)


@pytest.mark.parametrize("value", [0.0, -0.1])
def test_joint_window_planner_closing_horizon_must_be_positive(value: float) -> None:
    """The closing horizon rejects zero and negative values at the boundary."""
    with pytest.raises(pydantic.ValidationError):
        JointWindowPlanner(closing_horizon_seconds=value)


def test_joint_window_planner_enabled_without_ceiling_guard_is_rejected() -> None:
    """T23: ``joint_window_planner.enabled=True`` with the ceiling guard OFF
    is unconstructible, and the message names both fields."""
    with pytest.raises(
        pydantic.ValidationError,
        match=r"joint_window_planner\.enabled.*ceiling_guard_drop_enabled",
    ):
        ControllerConfig(
            joint_window_planner=JointWindowPlanner(enabled=True),
            post_first_crack_control=PostFirstCrackControl(ceiling_guard_drop_enabled=False),
        )


def test_joint_window_planner_enabled_without_ceiling_guard_rejected_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: the same invalid combination is rejected through the
    ``ROASTPILOT_CONTROLLER__…`` environment-variable path, not only direct
    construction."""
    monkeypatch.setenv("ROASTPILOT_CONTROLLER__JOINT_WINDOW_PLANNER__ENABLED", "true")
    monkeypatch.setenv(
        "ROASTPILOT_CONTROLLER__POST_FIRST_CRACK_CONTROL__CEILING_GUARD_DROP_ENABLED", "false"
    )
    with pytest.raises(pydantic.ValidationError):
        AppConfig()


def test_joint_window_planner_enabled_with_ceiling_guard_default_constructs() -> None:
    """T25: ``enabled=True`` with the guard at its default ``True`` constructs
    cleanly."""
    config = ControllerConfig(joint_window_planner=JointWindowPlanner(enabled=True))
    assert config.joint_window_planner.enabled is True
    assert config.post_first_crack_control.ceiling_guard_drop_enabled is True


def test_joint_window_planner_disabled_with_ceiling_guard_off_constructs() -> None:
    """T26: ``enabled=False`` with the guard also ``False`` constructs
    cleanly — the incumbent baseline arm (planner never enabled) stays
    available regardless of the guard's own setting."""
    config = ControllerConfig(
        post_first_crack_control=PostFirstCrackControl(ceiling_guard_drop_enabled=False)
    )
    assert config.joint_window_planner.enabled is False
    assert config.post_first_crack_control.ceiling_guard_drop_enabled is False


def test_bare_app_config_joint_window_planner_group_is_inert() -> None:
    """T27: a bare ``AppConfig()`` remains valid and the whole group is
    inert."""
    config = AppConfig()
    assert config.controller.joint_window_planner.enabled is False
