"""Advisory layer (component plan §4; orchestration plan § PydanticAI
Advisory Layer).

The advisor never receives MCP write tools. It receives structured context
and returns typed data only; safety policy validates, clamps, or rejects
every recommendation before any hardware write.

Failure vocabulary (plan §4 failure handling): an advisor call ends in one
of five outcomes — valid, malformed, unsafe, timeout, or provider error.
At this boundary *malformed* means the provider output could not be parsed
into the ``RoastDecision`` shape, and *unsafe* means it parsed but violated
the field constraints (e.g. heat 150 %). Advice that is well-typed but
rejected by safety policy (rate-limited, drop in the wrong phase) is not an
advisor failure — that is the normal policy path. Every failure becomes a
rejected recommendation with the deterministic hold-current-targets
fallback (``SafetyPolicy.evaluate_advisor_failure``).
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from roastpilot_agent.models import RoastPhase


class AdvisorError(Exception):
    """Base class for advisor-layer failures (component plan §4)."""


class AdvisorMalformedOutputError(AdvisorError):
    """Provider output could not be parsed into the ``RoastDecision`` shape."""


class AdvisorUnsafeOutputError(AdvisorError):
    """Provider output parsed but violated ``RoastDecision`` constraints."""


class AdvisorProviderError(AdvisorError):
    """Transport or API failure reaching the advisory provider."""


class AdvisorFailureMode(Enum):
    """Scriptable advisor failure modes — one per failure status.

    Plain ``Enum``, never ``StrEnum`` (D15): values are wire forms, and a
    string comparison against one is a pyright strict error in core logic.
    """

    MALFORMED = "malformed"
    UNSAFE = "unsafe"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"


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


FakeAdvisorStep = RoastDecision | AdvisorFailureMode
"""One scripted FakeAdvisor outcome: a decision to return, or a failure to raise."""


class FakeAdvisor(RoastAdvisor):
    """Deterministic, scriptable advisor for tests and demos (E8-S1).

    Script steps are consumed in order. A ``RoastDecision`` step is returned
    as-is; an ``AdvisorFailureMode`` step raises the matching typed error so
    the controller exercises the rejected-recommendation fallback.
    ``AdvisorFailureMode.TIMEOUT`` raises ``TimeoutError`` directly — the
    deterministic equivalent of the controller's ``asyncio.wait_for``
    expiring, without consuming the configured timeout; the real
    elapsed-time path is covered separately by a never-resolving advisor.

    When the script is exhausted, ``default_decision`` is returned if
    configured (demo-friendly: constant deterministic advice), otherwise
    ``AdvisorProviderError`` is raised so a test with an underscripted
    advisor fails loudly. Received contexts are recorded on ``contexts``;
    an optional shared ``log`` list records call order across
    collaborators (the conftest fake convention).
    """

    def __init__(
        self,
        script: Sequence[FakeAdvisorStep] | None = None,
        *,
        default_decision: RoastDecision | None = None,
        log: list[str] | None = None,
    ) -> None:
        self._script: list[FakeAdvisorStep] = list(script or [])
        self._default_decision = default_decision
        self._log = log if log is not None else []
        self.contexts: list[AdvisorContext] = []

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Return the next scripted decision or raise the next scripted failure."""
        self._log.append("advisor")
        self.contexts.append(context)
        if not self._script:
            if self._default_decision is not None:
                return self._default_decision
            raise AdvisorProviderError(
                "FakeAdvisor script exhausted and no default_decision configured"
            )
        step = self._script.pop(0)
        if isinstance(step, AdvisorFailureMode):
            if step is AdvisorFailureMode.MALFORMED:
                raise AdvisorMalformedOutputError("scripted malformed-output failure")
            if step is AdvisorFailureMode.UNSAFE:
                raise AdvisorUnsafeOutputError("scripted unsafe-output failure")
            if step is AdvisorFailureMode.TIMEOUT:
                raise TimeoutError("scripted advisory timeout")
            if step is AdvisorFailureMode.PROVIDER_ERROR:
                raise AdvisorProviderError("scripted provider failure")
            # Exhaustive by intent: a mode added in a later story must be
            # handled here, not silently funnelled into provider error.
            raise AssertionError(f"unhandled AdvisorFailureMode: {step}")
        return step


class PydanticAIAdvisor(RoastAdvisor):
    """OpenRouter-backed PydanticAI advisor (decision D5).

    Strict Pydantic output models, versioned prompts, context hashes (not
    raw payloads) in logs; timeout/malformed/unsafe output is treated as a
    rejected recommendation. Implementation lands in E8-S2.
    """

    async def get_recommendation(self, context: AdvisorContext) -> RoastDecision:
        """Call the configured OpenRouter model via PydanticAI (E8-S2)."""
        raise NotImplementedError("E8-S2: PydanticAI advisor")
