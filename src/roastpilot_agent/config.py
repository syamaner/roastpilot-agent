"""Typed application configuration (component plan §4; orchestration plan
§ Configuration Model).

Finalized at E2-S3. Controller timing defaults are the documented
hardware-aligned values from the orchestration plan; safety limits are
deliberately conservative software ceilings pending supervised hardware
validation at E12 (E12-S1).
"""

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    advisory_min_interval_seconds: float = Field(default=15.0, gt=0)
    advisory_timeout_seconds: float = Field(default=10.0, gt=0)
    t0_debounce_ticks: int = Field(default=3, ge=1)
    telemetry_log_interval_seconds: float = Field(default=5.0, gt=0)
    max_stale_telemetry_seconds: float = Field(default=3.0, gt=0)


class AdvisorConfig(BaseModel):
    """Advisor provider configuration (decision D5: OpenRouter via PydanticAI).

    The exact default model slug is an open item (component plan §11.1);
    it is confirmed and set at E8. ``api_key_env`` names the environment
    variable holding the provider key — the key itself never lives in
    config or the database.
    """

    provider_base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = Field(default="OPENROUTER_API_KEY", min_length=1)
    model_slug: str = ""
    timeout_seconds: float = Field(default=10.0, gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    prompt_version: str = Field(default="v0", min_length=1)


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
    """

    max_bean_temp_c: float = Field(default=230.0, gt=0)
    max_env_temp_c: float = Field(default=240.0, gt=0)
    pre_t0_max_bean_temp_c: float = Field(default=200.0, gt=0)
    overrun_safe_fan_percent: int = Field(default=100, ge=0, le=100)
    pre_t0_overrun_severity: Literal["recovery", "fault"] = "recovery"
    min_seconds_between_commands: float = Field(default=2.0, gt=0)


class AppConfig(BaseSettings):
    """Top-level application settings, loadable from environment variables.

    Nested fields override via ``ROASTPILOT_`` + section + ``__`` + field,
    e.g. ``ROASTPILOT_CONTROLLER__TICK_INTERVAL_SECONDS=0.5``.
    """

    model_config = SettingsConfigDict(env_prefix="ROASTPILOT_", env_nested_delimiter="__")

    controller: ControllerConfig = Field(default_factory=ControllerConfig)
    advisor: AdvisorConfig = Field(default_factory=AdvisorConfig)
    safety: SafetyLimits = Field(default_factory=SafetyLimits)
