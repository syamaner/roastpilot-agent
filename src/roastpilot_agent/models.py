"""Shared Pydantic models and enums (component plan §4).

Scaffold stubs only — the full model set (including MCP state mirrors and
SSE event payloads) lands in E2. All temperatures are Celsius everywhere.
"""

from pydantic import BaseModel, Field


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
    charge_guidance_max_c: float = 200.0
    initial_heat_percent: int = Field(ge=0, le=100)
    initial_fan_percent: int = Field(ge=0, le=100)
    target_drop_temp_c: float
    target_development_percent: float = Field(gt=0, lt=100)
