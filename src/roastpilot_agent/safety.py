"""Hard safety policy (component plan §4; orchestration plan § Safety Policy).

Safety policy is deterministic code, not prompt text. Every roaster write
passes through this layer; verdicts are typed and never string-compared in
core logic. The full rule set (max temperatures, pre-T0 overrun, stale
telemetry, bounds, rate limits, drop eligibility, e-stop) lands in E3.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class SafetyVerdict(StrEnum):
    """Typed safety verdict for every command evaluation."""

    ALLOW = "allow"
    CLAMP = "clamp"
    REJECT = "reject"
    FAULT = "fault"
    EMERGENCY_STOP = "emergency_stop"


class SafetyEvaluation(BaseModel):
    """Typed safety handshake attached to every roaster write."""

    verdict: SafetyVerdict
    adjusted_heat: int = Field(ge=0, le=100)
    adjusted_fan: int = Field(ge=0, le=100)
    reason: str


class SafetyPolicy:
    """Evaluates every command before it reaches the roaster.

    The rule set is implemented in E3; no code path may deliver advisor
    output to the MCP client without a :class:`SafetyEvaluation`.
    """

    def evaluate_command(self, requested_heat: int, requested_fan: int) -> SafetyEvaluation:
        """Validate, clamp, or reject a heat/fan command (E3)."""
        raise NotImplementedError("E3: safety rule set")
