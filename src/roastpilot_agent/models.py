"""Shared Pydantic models and enums (component plan §4).

Scaffold stubs only — the full model set (including MCP state mirrors and
SSE event payloads) lands in E2. All temperatures are Celsius everywhere.

The shared enums here are plain ``Enum``, deliberately not ``StrEnum``:
comparing a member against a raw string must be a pyright strict error
(``reportUnnecessaryComparison``), per the AGENTS.md typed-vocabulary
invariant. Use ``.value`` at serialization boundaries.
"""

from enum import Enum

from pydantic import BaseModel, Field


class RoastPhase(Enum):
    """Agent phases — the operator-facing truth (component plan §3).

    Lives here (not in controller.py) per D15: store, api, and advisor all
    consume the phase vocabulary, and importing it from controller.py would
    create import cycles once the tick loop wires those modules together.
    """

    IDLE = "idle"
    STARTING = "starting"
    PREHEATING = "preheating"
    ROASTING_PRE_FIRST_CRACK = "roasting_pre_first_crack"
    DEVELOPMENT = "development"
    COOLING = "cooling"
    COMPLETE = "complete"
    FAULTED = "faulted"
    OPERATOR_RECOVERY_REQUIRED = "operator_recovery_required"


class RoastProfile(BaseModel):
    """Minimal static roast profile (decision D7). Finalized in E2.

    No curve targets in M1: name, bean details, charge guidance range,
    initial heat/fan, target drop temperature, target development percent.
    """

    name: str
    bean_origin: str
    bean_varietal: str | None = None
    bean_weight_grams: float = Field(gt=0)
    charge_guidance_min_c: float = 170.0
    # The guidance ceiling deliberately equals the pre-T0 safety bound
    # (config.SafetyLimits.pre_t0_max_bean_temp_c, default 200.0): operators
    # are guided to charge before the hard bound trips. A scaffold test pins
    # charge_guidance_max_c <= pre_t0_max_bean_temp_c; keep them in sync.
    charge_guidance_max_c: float = 200.0
    initial_heat_percent: int = Field(ge=0, le=100)
    initial_fan_percent: int = Field(ge=0, le=100)
    target_drop_temp_c: float
    target_development_percent: float = Field(gt=0, lt=100)
