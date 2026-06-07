"""Typed application configuration (component plan §4; orchestration plan
§ Configuration Model).

Scaffold stubs only — the full configuration surface lands in E2. Controller
timing defaults below are the documented hardware-aligned values from the
orchestration plan; safety limit values beyond the documented pre-T0 charge
bound are deliberately not invented here (E3 defines them with tests).
"""

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ControllerConfig(BaseModel):
    """Controller timing and advisory-call thresholds.

    Defaults per orchestration plan § Configuration Model: the 1.0 s tick is
    set by the Hottop's K-type thermocouple response characteristics.
    """

    tick_interval_seconds: float = 1.0
    advisory_min_temp_delta_c: float = 1.0
    advisory_min_ror_delta_c_per_min: float = 2.0
    advisory_min_interval_seconds: float = 15.0
    advisory_timeout_seconds: float = 10.0
    t0_debounce_ticks: int = 3
    telemetry_log_interval_seconds: float = 5.0
    max_stale_telemetry_seconds: float = 3.0


class AdvisorConfig(BaseModel):
    """Advisor provider configuration (decision D5: OpenRouter via PydanticAI).

    The exact default model slug is an open item (component plan §11.1);
    it is confirmed and set at E8.
    """

    provider_base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    model_slug: str = ""
    timeout_seconds: float = 10.0
    temperature: float = 0.0
    prompt_version: str = "v0"


class SafetyLimits(BaseModel):
    """Hard safety limits enforced by deterministic code (E3).

    Only the documented pre-T0 upper charge safety bound (orchestration plan
    § Safety Policy, default 200 °C) is set here. Max bean/env temperatures
    and command rate limits are defined in E2/E3 with tests — the scaffold
    must not invent safety numbers.
    """

    pre_t0_max_bean_temp_c: float = 200.0


class AppConfig(BaseSettings):
    """Top-level application settings, loadable from environment variables."""

    model_config = SettingsConfigDict(env_prefix="ROASTPILOT_", env_nested_delimiter="__")

    controller: ControllerConfig = Field(default_factory=ControllerConfig)
    advisor: AdvisorConfig = Field(default_factory=AdvisorConfig)
    safety: SafetyLimits = Field(default_factory=SafetyLimits)
