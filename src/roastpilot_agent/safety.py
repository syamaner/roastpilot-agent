"""Hard safety policy (component plan §4; orchestration plan § Safety Policy).

Safety policy is deterministic code, not prompt text. Every roaster write
passes through this layer; verdicts are typed and never string-compared in
core logic. The full rule set (max temperatures, pre-T0 overrun, stale
telemetry, bounds, rate limits, drop eligibility, e-stop) lands in E3.
"""

from enum import Enum

from pydantic import BaseModel, Field


class SafetyVerdict(Enum):
    """Typed safety verdict for every command evaluation (six values per D15).

    A plain ``Enum``, deliberately not ``StrEnum``: comparing a verdict
    against a raw string must be a pyright strict error
    (``reportUnnecessaryComparison``). Use ``.value`` at serialization
    boundaries (matches the schema column in component plan §5).
    """

    ALLOW = "allow"
    CLAMP = "clamp"
    REJECT = "reject"
    RECOVERY = "recovery"
    FAULT = "fault"
    EMERGENCY_STOP = "emergency_stop"


class SafetyEvaluation(BaseModel):
    """Typed safety handshake attached to every roaster write.

    ``adjusted_heat``/``adjusted_fan`` are nullable per the plan §5 schema:
    REJECT, RECOVERY, FAULT, and EMERGENCY_STOP verdicts carry no adjusted
    command, and a fabricated 0 would be indistinguishable from a genuine
    clamp-to-zero in the persisted decision trace.
    """

    verdict: SafetyVerdict
    adjusted_heat: int | None = Field(default=None, ge=0, le=100)
    adjusted_fan: int | None = Field(default=None, ge=0, le=100)
    reason: str


class SafetyPolicy:
    """Evaluates every command before it reaches the roaster.

    The rule set is implemented in E3; no code path may deliver advisor
    output to the MCP client without a :class:`SafetyEvaluation`.
    """

    def evaluate_command(self, requested_heat: int, requested_fan: int) -> SafetyEvaluation:
        """Validate, clamp, or reject a heat/fan command (E3)."""
        raise NotImplementedError("E3: safety rule set")
