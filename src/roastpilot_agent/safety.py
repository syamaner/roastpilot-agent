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
from roastpilot_agent.models import (
    ACTIVE_ROAST_PHASES,
    RoastCommand,
    RoastEventSource,
    RoastPhase,
)


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

    Persisted-ready per the plan §5 ``safety_evaluations`` row: ``rule``
    names the rule that fired (or ``all_clear``), ``input_heat``/
    ``input_fan`` record what was *requested* (deliberately unbounded — an
    out-of-range request must be recordable exactly as made), and
    ``adjusted_heat``/``adjusted_fan`` what may be *executed* (bounded,
    nullable: most non-ALLOW/CLAMP verdicts carry no adjusted command —
    the fail-closed rules are the documented exception).
    """

    rule: str = Field(min_length=1)
    verdict: SafetyVerdict
    input_heat: int | None = None
    input_fan: int | None = None
    adjusted_heat: int | None = Field(default=None, ge=0, le=100)
    adjusted_fan: int | None = Field(default=None, ge=0, le=100)
    reason: str = Field(min_length=1)


COMMAND_PHASE_MATRIX: dict[RoastCommand, frozenset[RoastPhase]] = {
    # A session may only be started while idle (the API returns 409 on an
    # active run) or during the starting handshake itself.
    RoastCommand.START_ROAST_SESSION: frozenset({RoastPhase.IDLE, RoastPhase.STARTING}),
    # Heat only while actively roasting — never during cooling (the D16
    # canonical invalid example), never without a session.
    RoastCommand.SET_HEAT: frozenset(
        {RoastPhase.PREHEATING, RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.DEVELOPMENT}
    ),
    # Fan additionally allowed during cooling: moving air is never dangerous
    # and aids cooling.
    RoastCommand.SET_FAN: frozenset(
        {
            RoastPhase.PREHEATING,
            RoastPhase.ROASTING_PRE_FIRST_CRACK,
            RoastPhase.DEVELOPMENT,
            RoastPhase.COOLING,
        }
    ),
    # Manual-T0 fallback during preheating (plan §3) — "recovery-only" in
    # the plan means a fallback for failed auto-detection, not the
    # operator_recovery_required phase.
    RoastCommand.MARK_BEANS_ADDED: frozenset({RoastPhase.PREHEATING}),
    # Operator FC override only makes sense before development begins.
    RoastCommand.MARK_FIRST_CRACK: frozenset({RoastPhase.ROASTING_PRE_FIRST_CRACK}),
    # Drop with beans in the drum: the normal development drop, or an early
    # operator abort during roasting. Never while preheating (no beans yet).
    RoastCommand.DROP_BEANS: frozenset(
        {RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.DEVELOPMENT}
    ),
    # start_cooling is recovery-only (plan §6) plus the controller's
    # post-drop fallback when cooling_on is not observed (plan §3).
    RoastCommand.START_COOLING: frozenset(
        {RoastPhase.COOLING, RoastPhase.OPERATOR_RECOVERY_REQUIRED}
    ),
    # The D16 canonical invalid example: stop_cooling during development.
    RoastCommand.STOP_COOLING: frozenset({RoastPhase.COOLING}),
    # Export is a file write, not roaster control: valid whenever a session
    # exists (faulted runs export for diagnosis).
    RoastCommand.EXPORT_ROAST_LOG: frozenset(
        {
            RoastPhase.PREHEATING,
            RoastPhase.ROASTING_PRE_FIRST_CRACK,
            RoastPhase.DEVELOPMENT,
            RoastPhase.COOLING,
            RoastPhase.COMPLETE,
            RoastPhase.FAULTED,
            RoastPhase.OPERATOR_RECOVERY_REQUIRED,
        }
    ),
    # E-stop from every phase — mirrors evaluate_emergency_stop (E3-S4);
    # a test pins this row to the full phase set.
    RoastCommand.EMERGENCY_STOP: frozenset(RoastPhase),
}
"""Command×phase validity matrix (E3-S5, D16): which agent phases each MCP
write command may execute in. Exhaustively tested per cell."""


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

    def evaluate_command(
        self,
        *,
        requested_heat: int,
        requested_fan: int,
        seconds_since_last_command: float | None,
    ) -> SafetyEvaluation:
        """Heat/fan command validation: rate limit, then bounds (E3-S3).

        Rate limiting first — a command inside ``min_seconds_between_commands``
        of the previous one is REJECTed outright (``None`` means no prior
        command; exactly at the limit is allowed). Then bounds: requests
        outside 0–100 % are CLAMPed, never rejected — the intent (more/less
        heat or air) is honored at the nearest safe value. ALLOW and CLAMP
        both carry the adjusted values to execute.
        """
        limits = self._limits
        if (
            seconds_since_last_command is not None
            and seconds_since_last_command < limits.min_seconds_between_commands
        ):
            return SafetyEvaluation(
                rule="command_rate_limited",
                verdict=SafetyVerdict.REJECT,
                input_heat=requested_heat,
                input_fan=requested_fan,
                reason=(
                    f"command issued {seconds_since_last_command:.1f} s after the previous one "
                    f"(minimum {limits.min_seconds_between_commands:.1f} s): rejected — the "
                    f"Hottop serial loop runs at ~1 Hz, faster writes have no effect"
                ),
            )
        clamped_heat = min(100, max(0, requested_heat))
        clamped_fan = min(100, max(0, requested_fan))
        if clamped_heat != requested_heat or clamped_fan != requested_fan:
            return SafetyEvaluation(
                rule="command_bounds",
                verdict=SafetyVerdict.CLAMP,
                input_heat=requested_heat,
                input_fan=requested_fan,
                adjusted_heat=clamped_heat,
                adjusted_fan=clamped_fan,
                reason=(
                    f"requested heat {requested_heat} % / fan {requested_fan} % outside 0–100: "
                    f"clamped to heat {clamped_heat} % / fan {clamped_fan} %"
                ),
            )
        return SafetyEvaluation(
            rule="all_clear",
            verdict=SafetyVerdict.ALLOW,
            input_heat=requested_heat,
            input_fan=requested_fan,
            adjusted_heat=requested_heat,
            adjusted_fan=requested_fan,
            reason="command within bounds and rate limit",
        )

    def evaluate_drop_recommendation(self, *, phase: RoastPhase) -> SafetyEvaluation:
        """Advisor drop-eligibility rule (E3-S3).

        An advisor ``should_drop`` recommendation is honored only during
        ``development`` (component plan §3: development → cooling via
        validated drop decision). Anywhere else it is REJECTed — dropping
        unroasted or already-dropped beans on a model's say-so is never
        acceptable. Operator drops are governed separately by the
        command×phase matrix (E3-S5, D16).
        """
        if phase is RoastPhase.DEVELOPMENT:
            return SafetyEvaluation(
                rule="drop_eligibility",
                verdict=SafetyVerdict.ALLOW,
                reason="drop recommendation during development: eligible",
            )
        return SafetyEvaluation(
            rule="drop_eligibility",
            verdict=SafetyVerdict.REJECT,
            reason=(
                f"advisor drop recommendation in phase {phase.value}: rejected — advisor "
                f"drops are honored only during development"
            ),
        )

    def evaluate_advisor_failure(
        self,
        *,
        status: Literal["timeout", "malformed", "unsafe", "provider_error"],
        current_heat: int,
        current_fan: int,
    ) -> SafetyEvaluation:
        """Advisor failure ⇒ rejected recommendation ⇒ deterministic fallback.

        Timeout, malformed output, unsafe output, or a provider error never
        blocks the tick (orchestration plan § Advisory Call Frequency): the
        recommendation is REJECTed and the fallback is to hold the current
        targets — the adjusted values echo the heat/fan already in effect.
        Every outcome is persisted via the decision trace (plan §5).
        """
        return SafetyEvaluation(
            rule=f"advisor_{status}",
            verdict=SafetyVerdict.REJECT,
            adjusted_heat=current_heat,
            adjusted_fan=current_fan,
            reason=(
                f"advisor outcome '{status}': recommendation rejected, deterministic fallback "
                f"holds current targets (heat {current_heat} %, fan {current_fan} %)"
            ),
        )

    def evaluate_emergency_stop(
        self,
        *,
        phase: RoastPhase,
        operator_reason: str | None = None,
    ) -> SafetyEvaluation:
        """Emergency stop is always permitted, from every phase (E3-S4).

        Deliberately the only rule with no condition: the signature takes
        the phase (for the trace) and an optional operator reason — never
        advisor, UI, or cloud state — and the verdict is unconditionally
        EMERGENCY_STOP. No adjusted heat/fan values: the single MCP
        ``emergency_stop`` command owns the hardware shutdown, not a
        heat/fan write.
        """
        detail = f": {operator_reason}" if operator_reason else ""
        return SafetyEvaluation(
            rule="emergency_stop",
            verdict=SafetyVerdict.EMERGENCY_STOP,
            reason=f"emergency stop requested in phase {phase.value}{detail}",
        )

    def evaluate_command_phase(
        self,
        *,
        command: RoastCommand,
        phase: RoastPhase,
    ) -> SafetyEvaluation:
        """Command×phase validity matrix (E3-S5, D16).

        Every MCP write command is checked against the current agent phase;
        an invalid combination (e.g. ``set_heat`` during ``cooling``,
        ``stop_cooling`` during ``development``) is REJECTed. The matrix is
        :data:`COMMAND_PHASE_MATRIX`; rationale per row lives on the
        constant. This rule gates *where* a command may run — bounds, rate
        limits, and drop eligibility still apply on top.
        """
        allowed = COMMAND_PHASE_MATRIX[command]
        if phase in allowed:
            return SafetyEvaluation(
                rule="command_phase_validity",
                verdict=SafetyVerdict.ALLOW,
                reason=f"{command.value} is valid in phase {phase.value}",
            )
        return SafetyEvaluation(
            rule="command_phase_validity",
            verdict=SafetyVerdict.REJECT,
            reason=(
                f"{command.value} is not valid in phase {phase.value} (allowed: "
                f"{', '.join(sorted(p.value for p in allowed))})"
            ),
        )

    def evaluate_event_source(
        self,
        *,
        transition: Literal["t0", "first_crack"],
        source: RoastEventSource,
    ) -> SafetyEvaluation:
        """FC/T0 source validity (E3-S5, D16).

        First-crack and T0 state transitions are accepted only from MCP
        detection status or explicit operator action (component plan §3
        entry triggers). Anything else — advisor, controller, safety —
        is REJECTed and the attempt is recorded in the trace: the advisor
        must never be able to start the roast clock or the development
        timer, directly or indirectly.
        """
        if source in (RoastEventSource.MCP, RoastEventSource.OPERATOR):
            return SafetyEvaluation(
                rule="event_source_validity",
                verdict=SafetyVerdict.ALLOW,
                reason=f"{transition} transition from source '{source.value}': accepted",
            )
        return SafetyEvaluation(
            rule="event_source_validity",
            verdict=SafetyVerdict.REJECT,
            reason=(
                f"{transition} transition from source '{source.value}': rejected — only MCP "
                f"detection or explicit operator action may drive T0/first-crack transitions"
            ),
        )
