"""Hard safety policy (component plan §4; orchestration plan § Safety Policy).

Safety policy is deterministic code, not prompt text. Every roaster write
passes through this layer; verdicts are typed and never string-compared in
core logic. E3-S1 implements the temperature and pre-T0 overrun rules;
telemetry validity (E3-S2), command validation (E3-S3), e-stop plumbing
(E3-S4), and phase/source validity (E3-S5, D16) follow.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.models import ACTIVE_ROAST_PHASES, RoastPhase


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

    ``rule`` names the rule that fired (or ``all_clear``), matching the
    plan §5 ``safety_evaluations.rule`` column. ``adjusted_heat``/
    ``adjusted_fan`` are nullable per the schema: most non-ALLOW/CLAMP
    verdicts carry no adjusted command — the pre-T0 overrun rule is the
    documented exception (heat 0 %, safe fan, RECOVERY/FAULT verdict).
    """

    rule: str = Field(min_length=1)
    verdict: SafetyVerdict
    adjusted_heat: int | None = Field(default=None, ge=0, le=100)
    adjusted_fan: int | None = Field(default=None, ge=0, le=100)
    reason: str = Field(min_length=1)


class SafetyPolicy:
    """Evaluates telemetry and commands before anything reaches the roaster.

    Deterministic and side-effect free: rules read the configured
    :class:`SafetyLimits` and the inputs only. The controller owns acting
    on verdicts; no code path may deliver advisor output to the MCP client
    without a :class:`SafetyEvaluation`.
    """

    def __init__(self, limits: SafetyLimits) -> None:
        self._limits = limits

    def evaluate_telemetry(
        self,
        *,
        phase: RoastPhase,
        bean_temp_c: float,
        env_temp_c: float,
        t0_confirmed: bool,
    ) -> SafetyEvaluation:
        """Apply the temperature rule set to a telemetry reading (E3-S1).

        Severity order: hard ceilings first (bean, then environment —
        breaching either is an emergency stop), then the pre-T0 overrun
        rule, which applies only while ``preheating`` with no confirmed T0
        (orchestration plan § Safety Policy): heat is forced to 0 %, fan to
        the configured safe value, and the verdict is RECOVERY or FAULT per
        ``pre_t0_overrun_severity``. Limits are strict: a reading exactly
        at a ceiling does not trip it.
        """
        limits = self._limits
        if bean_temp_c > limits.max_bean_temp_c:
            return SafetyEvaluation(
                rule="max_bean_temp",
                verdict=SafetyVerdict.EMERGENCY_STOP,
                reason=(
                    f"bean temperature {bean_temp_c:.1f} °C exceeds the hard ceiling "
                    f"{limits.max_bean_temp_c:.1f} °C"
                ),
            )
        if env_temp_c > limits.max_env_temp_c:
            return SafetyEvaluation(
                rule="max_env_temp",
                verdict=SafetyVerdict.EMERGENCY_STOP,
                reason=(
                    f"environment temperature {env_temp_c:.1f} °C exceeds the hard ceiling "
                    f"{limits.max_env_temp_c:.1f} °C"
                ),
            )
        if (
            phase is RoastPhase.PREHEATING
            and not t0_confirmed
            and bean_temp_c > limits.pre_t0_max_bean_temp_c
        ):
            severe = limits.pre_t0_overrun_severity == "fault"
            return SafetyEvaluation(
                rule="pre_t0_overrun",
                verdict=SafetyVerdict.FAULT if severe else SafetyVerdict.RECOVERY,
                adjusted_heat=0,
                adjusted_fan=limits.overrun_safe_fan_percent,
                reason=(
                    f"bean temperature {bean_temp_c:.1f} °C exceeds the pre-T0 charge bound "
                    f"{limits.pre_t0_max_bean_temp_c:.1f} °C with no confirmed T0: heat 0 %, "
                    f"fan {limits.overrun_safe_fan_percent} %, "
                    f"{'fault' if severe else 'operator recovery'} required"
                ),
            )
        return SafetyEvaluation(
            rule="all_clear",
            verdict=SafetyVerdict.ALLOW,
            reason="telemetry within configured limits",
        )

    def evaluate_telemetry_validity(
        self,
        *,
        phase: RoastPhase,
        telemetry_age_seconds: float | None,
        max_stale_seconds: float,
    ) -> SafetyEvaluation:
        """Stale/missing telemetry rules (E3-S2).

        Applies only during :data:`~roastpilot_agent.models.ACTIVE_ROAST_PHASES`
        — a hot machine with beans in play must never run blind. Missing
        telemetry (``None``) or telemetry older than ``max_stale_seconds``
        (caller passes ``ControllerConfig.max_stale_telemetry_seconds``)
        fails closed: heat 0 %, safe fan, FAULT (AGENTS.md: unsafe or
        uncertain behavior fails closed). The bound is strict — exactly
        ``max_stale_seconds`` old is still fresh.
        """
        if phase not in ACTIVE_ROAST_PHASES:
            return SafetyEvaluation(
                rule="all_clear",
                verdict=SafetyVerdict.ALLOW,
                reason=f"telemetry validity not enforced in phase {phase.value}",
            )
        limits = self._limits
        if telemetry_age_seconds is None:
            return SafetyEvaluation(
                rule="missing_telemetry",
                verdict=SafetyVerdict.FAULT,
                adjusted_heat=0,
                adjusted_fan=limits.overrun_safe_fan_percent,
                reason=(
                    f"no telemetry during active roast (phase {phase.value}): failing closed "
                    f"— heat 0 %, fan {limits.overrun_safe_fan_percent} %"
                ),
            )
        if telemetry_age_seconds > max_stale_seconds:
            return SafetyEvaluation(
                rule="stale_telemetry",
                verdict=SafetyVerdict.FAULT,
                adjusted_heat=0,
                adjusted_fan=limits.overrun_safe_fan_percent,
                reason=(
                    f"telemetry is {telemetry_age_seconds:.1f} s old (limit "
                    f"{max_stale_seconds:.1f} s) during active roast (phase {phase.value}): "
                    f"failing closed — heat 0 %, fan {limits.overrun_safe_fan_percent} %"
                ),
            )
        return SafetyEvaluation(
            rule="all_clear",
            verdict=SafetyVerdict.ALLOW,
            reason="telemetry is fresh",
        )

    def evaluate_mcp_failure(
        self,
        *,
        operation: Literal["read", "write"],
        consecutive_failures: int,
    ) -> SafetyEvaluation:
        """MCP read/write failure rules (E3-S2).

        Transient failures are tolerated (ALLOW — the controller skips the
        tick or retries next tick); ``max_consecutive_mcp_failures`` (default
        3 ≈ a 3 s blind window at the 1.0 s tick) or more consecutive
        failures fail closed with heat 0 %, safe fan, FAULT — the roaster
        can no longer be observed (read) or controlled (write).
        """
        if consecutive_failures < 0:
            raise ValueError("consecutive_failures must be >= 0")
        limits = self._limits
        if consecutive_failures == 0:
            return SafetyEvaluation(
                rule="all_clear",
                verdict=SafetyVerdict.ALLOW,
                reason=f"no consecutive MCP {operation} failures",
            )
        if consecutive_failures < limits.max_consecutive_mcp_failures:
            return SafetyEvaluation(
                rule=f"mcp_{operation}_failure_tolerated",
                verdict=SafetyVerdict.ALLOW,
                reason=(
                    f"{consecutive_failures} consecutive MCP {operation} failure(s), below "
                    f"the fault threshold {limits.max_consecutive_mcp_failures}: tolerated, "
                    f"controller skips/retries next tick"
                ),
            )
        return SafetyEvaluation(
            rule=f"mcp_{operation}_failures_exhausted",
            verdict=SafetyVerdict.FAULT,
            adjusted_heat=0,
            adjusted_fan=limits.overrun_safe_fan_percent,
            reason=(
                f"{consecutive_failures} consecutive MCP {operation} failures (threshold "
                f"{limits.max_consecutive_mcp_failures}): the roaster can no longer be "
                f"{'observed' if operation == 'read' else 'controlled'} — failing closed, "
                f"heat 0 %, fan {limits.overrun_safe_fan_percent} %"
            ),
        )

    def evaluate_command(self, requested_heat: int, requested_fan: int) -> SafetyEvaluation:
        """Validate, clamp, or reject a heat/fan command (E3-S3)."""
        raise NotImplementedError("E3-S3: command validation rules")
