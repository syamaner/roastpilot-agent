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
    AdvisorConfig,
    AppConfig,
    ControllerConfig,
    SafetyLimits,
)
from roastpilot_agent.models import RoastPhase


def test_controller_defaults_match_orchestration_plan() -> None:
    config = ControllerConfig()
    assert config.tick_interval_seconds == 1.0
    assert config.advisory_min_temp_delta_c == 1.0
    assert config.advisory_min_ror_delta_c_per_min == 2.0
    # #171: phase-keyed consult floors replace the single 15 s heartbeat.
    assert config.advisory_min_interval_seconds == {
        RoastPhase.PREHEATING: 30.0,
        RoastPhase.ROASTING_PRE_FIRST_CRACK: 10.0,
        RoastPhase.DEVELOPMENT: 0.0,
    }
    assert config.advisory_timeout_seconds == 10.0
    assert config.t0_debounce_ticks == 3
    assert config.telemetry_log_interval_seconds == 5.0
    assert config.max_stale_telemetry_seconds == 3.0


def test_advisory_interval_for_resolves_per_phase_and_defaults_unthrottled() -> None:
    config = ControllerConfig()
    assert config.advisory_interval_for(RoastPhase.PREHEATING) == 30.0
    assert config.advisory_interval_for(RoastPhase.ROASTING_PRE_FIRST_CRACK) == 10.0
    # Development is unthrottled (0); a phase absent from the map is too.
    assert config.advisory_interval_for(RoastPhase.DEVELOPMENT) == 0.0
    assert config.advisory_interval_for(RoastPhase.COOLING) == 0.0


def test_advisory_interval_is_config_tunable() -> None:
    config = ControllerConfig(advisory_min_interval_seconds={RoastPhase.PREHEATING: 45.0})
    assert config.advisory_interval_for(RoastPhase.PREHEATING) == 45.0
    # Unspecified phases fall back to unthrottled.
    assert config.advisory_interval_for(RoastPhase.ROASTING_PRE_FIRST_CRACK) == 0.0


def test_advisor_defaults_match_d5_d18_and_bakeoff() -> None:
    config = AdvisorConfig()
    assert config.provider == "openai_compatible"
    assert config.provider_base_url == "https://openrouter.ai/api/v1"
    assert config.api_key_env == "OPENROUTER_API_KEY"
    # Artisan-expanded bake-off winner (D33, 14 Jun): gemini-3.1-flash-lite via
    # OpenRouter — the only model that reliably calls the drop on 28 real roasts
    # (opus + the frontier/slow models over-hold). Electric-Hottop prompt v2.
    assert config.model_slug == "google/gemini-3.1-flash-lite"
    assert config.prompt_version == "v2"
    assert config.timeout_seconds == 10.0
    assert config.temperature == 0.0
    assert config.reasoning_effort is None  # provider default until measured


def test_advisor_per_phase_model_default_is_pinned_model_everywhere() -> None:
    """#173 MECHANISM: per-phase model selection defaults to the single pinned
    model (gemini-3.1-flash-lite, D33) for every phase — the map is retained so
    a future re-run could flip a phase slot to a different model."""
    config = AdvisorConfig()
    assert DEFAULT_ADVISOR_MODEL == "google/gemini-3.1-flash-lite"
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
