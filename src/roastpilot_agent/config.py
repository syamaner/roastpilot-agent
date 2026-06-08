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
    # D16: operator timeout applies ONLY in true operator-required states
    # (manual confirmation, hold, recovery) — never in normal phases. The
    # machine is already hardware-off in those states, so the timeout
    # raises a safety alert (a nag, not an actuation); 600 s gives an
    # operator a realistic window to return before the system complains.
    operator_timeout_seconds: float = Field(default=600.0, gt=0)


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
    outcome (E8-S4, plan §11.1 → D20, refined → D21): ``anthropic/
    claude-opus-4.8`` via OpenRouter won on advice quality with comfortable
    latency headroom under the 10 s budget, and ``v2`` is the electric-Hottop
    prompt (fan as a coupled heat-transfer-mode lever + development-duration
    objective). Under v2's richer prompt opus is the only frontier model that
    still passes the latency gate. To run opus natively (no OpenRouter
    hop/markup, per D18), set ``provider=anthropic`` +
    ``api_key_env=ANTHROPIC_API_KEY``. ``OPENROUTER_API_KEY`` must be set in
    the environment at runtime; ``FakeAdvisor`` stays the test/CI default.
    """

    provider: Literal["openai", "anthropic", "google", "ollama", "openai_compatible"] = (
        "openai_compatible"
    )
    provider_base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = Field(default="OPENROUTER_API_KEY", min_length=1)
    model_slug: str = "anthropic/claude-opus-4.8"
    timeout_seconds: float = Field(default=10.0, gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    prompt_version: str = Field(default="v2", min_length=1)


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
    """

    max_bean_temp_c: float = Field(default=230.0, gt=0)
    max_env_temp_c: float = Field(default=240.0, gt=0)
    pre_t0_max_bean_temp_c: float = Field(default=200.0, gt=0)
    overrun_safe_fan_percent: int = Field(default=100, ge=0, le=100)
    pre_t0_overrun_severity: Literal["recovery", "fault"] = "recovery"
    min_seconds_between_commands: float = Field(default=2.0, gt=0)
    max_consecutive_mcp_failures: int = Field(default=3, ge=1)


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
