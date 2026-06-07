"""E2-S3: configuration surface tests (component plan §4; orchestration plan
§ Configuration Model; D5).

Documented defaults, env-var loading (``ROASTPILOT_`` prefix with ``__``
nesting), validation rejections, and the guidance-vs-safety-bound link.
"""

import os

import pydantic
import pytest

from roastpilot_agent.config import AdvisorConfig, AppConfig, ControllerConfig, SafetyLimits


def test_controller_defaults_match_orchestration_plan() -> None:
    config = ControllerConfig()
    assert config.tick_interval_seconds == 1.0
    assert config.advisory_min_temp_delta_c == 1.0
    assert config.advisory_min_ror_delta_c_per_min == 2.0
    assert config.advisory_min_interval_seconds == 15.0
    assert config.advisory_timeout_seconds == 10.0
    assert config.t0_debounce_ticks == 3
    assert config.telemetry_log_interval_seconds == 5.0
    assert config.max_stale_telemetry_seconds == 3.0


def test_advisor_defaults_match_d5() -> None:
    config = AdvisorConfig()
    assert config.provider_base_url == "https://openrouter.ai/api/v1"
    assert config.api_key_env == "OPENROUTER_API_KEY"
    assert config.model_slug == ""  # confirmed at E8 (plan §11.1)
    assert config.timeout_seconds == 10.0
    assert config.temperature == 0.0
    assert config.prompt_version == "v0"


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
        (AdvisorConfig, {"timeout_seconds": 0}),
        (AdvisorConfig, {"temperature": -0.1}),
        (AdvisorConfig, {"temperature": 2.1}),
        (AdvisorConfig, {"api_key_env": ""}),
        (AdvisorConfig, {"prompt_version": ""}),
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
