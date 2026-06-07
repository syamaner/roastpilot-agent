"""Advisory layer (component plan §4; orchestration plan § PydanticAI
Advisory Layer).

The advisor never receives MCP write tools. It receives structured context
and returns typed data only; safety policy validates, clamps, or rejects
every recommendation before any hardware write. Implementations land in E8.
"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from roastpilot_agent.models import RoastPhase


class AdvisorContext(BaseModel):
    """Structured context provided to the advisory layer.

    Built from the MCP roast state, the frozen profile, and recent
    decisions; ``reference_roasts`` stays empty until M2 (component plan §4).
    """

    phase: RoastPhase
    roast_elapsed_seconds: float
    development_elapsed_seconds: float | None
    current_bean_temp_c: float
    current_env_temp_c: float
    bean_ror_c_per_min: float | None
    env_ror_c_per_min: float | None
    target_drop_temp_c: float
    profile_name: str
    recent_telemetry_samples: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    first_crack_detected: bool = False
    first_crack_timestamp_seconds: float | None = None


class RoastDecision(BaseModel):
    """Typed advisory recommendation returned by the advisor."""

    target_heat: int = Field(ge=0, le=100)
    target_fan: int = Field(ge=0, le=100)
    should_drop: bool
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class RoastAdvisor(ABC):
    """Advisor interface — the controller never depends on provider concepts."""

    @abstractmethod
    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Return a typed advisory recommendation."""
        raise NotImplementedError


class FakeAdvisor(RoastAdvisor):
    """Deterministic advisor for tests and demos (E8)."""

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Return a deterministic scripted recommendation (E8)."""
        raise NotImplementedError("E8: deterministic fake advisor")


class PydanticAIAdvisor(RoastAdvisor):
    """OpenRouter-backed PydanticAI advisor (decision D5).

    Strict Pydantic output models, versioned prompts, context hashes (not
    raw payloads) in logs; timeout/malformed/unsafe output is treated as a
    rejected recommendation. Implementation lands in E8.
    """

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Call the configured OpenRouter model via PydanticAI (E8)."""
        raise NotImplementedError("E8: PydanticAI advisor")
