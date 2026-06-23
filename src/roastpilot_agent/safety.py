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
from roastpilot_agent.control_policy import PhaseControlLimits
from roastpilot_agent.models import (
    ACTIVE_ROAST_PHASES,
    OperatorAction,
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
    # operator abort during roasting. Never while preheating (no beans yet). Also
    # valid in `faulted` (#210): an e-stop/fault leaves the drum hot (heat off but
    # still hot), and the operator must be able to dump the beans so they stop
    # scorching — a safe-ing action, not a resume. Like the #206 cooling
    # additions, DROP from faulted issues the command WITHOUT a phase transition
    # (the run stays faulted until acknowledged); `set_heat` is deliberately NOT
    # extended to faulted, so heat stays off throughout.
    RoastCommand.DROP_BEANS: frozenset(
        {RoastPhase.ROASTING_PRE_FIRST_CRACK, RoastPhase.DEVELOPMENT, RoastPhase.FAULTED}
    ),
    # start_cooling is recovery-only (plan §6) plus the controller's
    # post-drop fallback when cooling_on is not observed (plan §3). Also valid
    # in `faulted`: a fault/e-stop can leave the machine hot, and the operator
    # must be able to engage cooling on a faulted-but-physically-active roaster
    # (#206). Cooling is never the hazard — moving air only aids cooling.
    RoastCommand.START_COOLING: frozenset(
        {RoastPhase.COOLING, RoastPhase.OPERATOR_RECOVERY_REQUIRED, RoastPhase.FAULTED}
    ),
    # The D16 canonical invalid example: stop_cooling during development. Valid in
    # `faulted` (#206): an e-stop can engage cooling, and the operator must be able
    # to stop it without power-cycling. This is the loss-of-control gap #206 fixes;
    # `set_heat` is deliberately NOT extended to faulted (heat stays off).
    RoastCommand.STOP_COOLING: frozenset({RoastPhase.COOLING, RoastPhase.FAULTED}),
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


#: The operator actions that resolve to an MCP write command (and so are gated by
#: :data:`COMMAND_PHASE_MATRIX`). The canonical mapping — ``api.py`` imports this
#: rather than keeping its own copy, so the queue pre-check and the
#: ``enabled_actions`` derivation share one source of truth.
OPERATOR_ACTION_COMMAND: dict[OperatorAction, RoastCommand] = {
    OperatorAction.MARK_BEANS_ADDED: RoastCommand.MARK_BEANS_ADDED,
    OperatorAction.MARK_FIRST_CRACK: RoastCommand.MARK_FIRST_CRACK,
    OperatorAction.DROP_BEANS: RoastCommand.DROP_BEANS,
    OperatorAction.START_COOLING: RoastCommand.START_COOLING,
    OperatorAction.STOP_COOLING: RoastCommand.STOP_COOLING,
    OperatorAction.EMERGENCY_STOP: RoastCommand.EMERGENCY_STOP,
}


def enabled_operator_actions(phase: RoastPhase) -> list[OperatorAction]:
    """The operator actions the server would ACCEPT in ``phase`` (E10 option (a),
    D25). A pure PERMISSION MIRROR — "what the controller accepts" — not a render
    list (a page may still hide a permitted-but-meaningless action; that's its
    presentation call).

    Derived READ-ONLY from the controller's existing acceptance, with NO second
    source of truth (the drift this exists to kill):

    * **MCP-write actions** (the six in :data:`OPERATOR_ACTION_COMMAND`): included
      iff ``phase`` is in their :data:`COMMAND_PHASE_MATRIX` row — the same matrix
      the controller enforces on drain. (``emergency_stop``'s row is every phase.)
    * **``pause_advisory`` / ``resume_advisory``**: included in EVERY phase — the
      controller never phase-gates them (``RoastController.operator_pause_advisory``
      / ``operator_resume_advisory`` are unconditional toggles, no safety eval), so
      mirroring server truth means always-enabled.
    * **``acknowledge_recovery``**: included iff ``phase`` is
      ``operator_recovery_required`` — the only phase from which the controller's
      drain (``RoastRunner._dispatch_acknowledge``) acts on it; any other phase
      records a failed action.
    * **``acknowledge_fault``** (#206): included iff ``phase`` is ``faulted`` — the
      only phase from which the controller's drain acts on it (it finalises the
      operable-faulted run). Mirrors ``acknowledge_recovery``; any other phase
      records a failed action. No MCP write.

    No new safety rule: this is a projection of acceptance the controller already
    encodes, and every action is still re-validated by the controller before any
    MCP write — this is enablement, never enforcement. The biconditional test
    ``test_enabled_actions_mirror_controller_acceptance`` pins it: for all
    (action, phase), ``controller_accepts(action, phase)`` iff
    ``action in enabled_operator_actions(phase)``, driving the real controller.
    Returned in :class:`OperatorAction` declaration order for a stable result.
    """
    enabled: list[OperatorAction] = []
    for action in OperatorAction:
        command = OPERATOR_ACTION_COMMAND.get(action)
        if command is not None:
            if phase in COMMAND_PHASE_MATRIX[command]:
                enabled.append(action)
        elif action in (OperatorAction.PAUSE_ADVISORY, OperatorAction.RESUME_ADVISORY):
            # The controller never phase-gates the advisory toggles → every phase.
            enabled.append(action)
        elif (
            # Each acknowledge action mirrors exactly one phase: acknowledge_recovery
            # iff operator_recovery_required, acknowledge_fault iff faulted (#206) —
            # the only phase from which the controller's drain acts on it.
            action is OperatorAction.ACKNOWLEDGE_RECOVERY
            and phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
        ) or (action is OperatorAction.ACKNOWLEDGE_FAULT and phase is RoastPhase.FAULTED):
            enabled.append(action)
    return enabled


class SafetyPolicy:
    """Evaluates telemetry and commands before anything reaches the roaster.

    Deterministic and side-effect free: rules read the configured
    :class:`SafetyLimits` and the inputs only. The controller owns acting
    on verdicts; no code path may deliver advisor output to the MCP client
    without a :class:`SafetyEvaluation`.
    """

    def __init__(self, limits: SafetyLimits) -> None:
        self._limits = limits

    @property
    def limits(self) -> SafetyLimits:
        """The configured safety limits backing every rule (read-only).

        Exposed so the controller can build the single
        :class:`~roastpilot_agent.control_policy.RoastControlPolicy` from the
        *same* limits the gate enforces (D35 §8.3, #273) — the told==enforced
        single source — without a second copy of the configuration.
        """
        return self._limits

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
        bounds: PhaseControlLimits | None = None,
    ) -> SafetyEvaluation:
        """Heat/fan command validation: rate limit, then bounds (E3-S3).

        Rate limiting first — a command inside ``min_seconds_between_commands``
        of the previous one is REJECTed outright (``None`` means no prior
        command; exactly at the limit is allowed). Then bounds: a request
        outside the executable heat/fan box is CLAMPed, never rejected — the
        intent (more/less heat or air) is honored at the nearest safe value.
        ALLOW and CLAMP both carry the adjusted values to execute.

        The box comes from the single :class:`RoastControlPolicy` source (D35
        §8.3, #273): when ``bounds`` is supplied, the heat/fan floor + ceiling it
        carries are the clamp range — the *same* limits placed in the advisor
        context, so the value the model is told equals the value enforced here
        (told == enforced). When ``bounds`` is ``None`` the range is the full
        0–100 lever (the historical behaviour, preserved unchanged); the policy
        resolves that same 0–100 box for every phase today, so wiring the gate to
        the policy is a verdict no-op until #222 narrows a phase.

        Args:
            requested_heat: The requested heat level (deliberately unbounded —
                an out-of-range request is recorded exactly as made).
            requested_fan: The requested fan level (deliberately unbounded).
            seconds_since_last_command: Seconds since the previous accepted
                command, or ``None`` if there was none.
            bounds: The phase-resolved control box from
                :class:`RoastControlPolicy`, or ``None`` for the full 0–100
                lever range.

        Returns:
            A :class:`SafetyEvaluation`: REJECT when rate-limited, CLAMP when the
            request falls outside the box, ALLOW otherwise — the latter two
            carrying the bounded values to execute.
        """
        limits = self._limits
        heat_floor = bounds.heat_floor_percent if bounds is not None else 0
        heat_ceiling = bounds.heat_ceiling_percent if bounds is not None else 100
        fan_floor = bounds.fan_floor_percent if bounds is not None else 0
        fan_ceiling = bounds.fan_ceiling_percent if bounds is not None else 100
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
        clamped_heat = min(heat_ceiling, max(heat_floor, requested_heat))
        clamped_fan = min(fan_ceiling, max(fan_floor, requested_fan))
        if clamped_heat != requested_heat or clamped_fan != requested_fan:
            return SafetyEvaluation(
                rule="command_bounds",
                verdict=SafetyVerdict.CLAMP,
                input_heat=requested_heat,
                input_fan=requested_fan,
                adjusted_heat=clamped_heat,
                adjusted_fan=clamped_fan,
                reason=(
                    f"requested heat {requested_heat} % / fan {requested_fan} % outside the "
                    f"control box (heat {heat_floor}–{heat_ceiling} %, fan "
                    f"{fan_floor}–{fan_ceiling} %): clamped to heat {clamped_heat} % / fan "
                    f"{clamped_fan} %"
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

    def evaluate_advisor_drop_coherence(
        self,
        *,
        system_development_percent: float,
        target_development_percent: float,
        margin_percent: float,
        current_heat: int,
        current_fan: int,
    ) -> SafetyEvaluation:
        """Drop coherence guard ⇒ rejected drop when development is below target (#312).

        The deterministic cross-check the controller applies before honouring an
        advisor ``should_drop=true``: the drop is irreversible, so it is gated on
        the SYSTEM's real development percent (never the model's claimed number,
        which the first supervised roast showed can be fabricated). When

            ``system_development_percent < target_development_percent - margin_percent``

        the drop is REJECTed; the adjusted values hold the current targets (no
        lever write, no drop), mirroring the low-confidence-reject hold pattern so
        the blocked drop is observable in the safety_evaluations trace, not only
        the event stream. This method is consulted only on the below-window path,
        so it always returns REJECT; coherence (the at/above-window case) is the
        caller's signal to proceed to the drop-eligibility evaluation.

        No invariant is at risk: no roaster write happens on a REJECT, and the
        operator's manual drop is a separate, un-gated path. All percents are the
        charge/FC-referenced development ratio in percentage points (not Celsius).

        Args:
            system_development_percent: The controller's real development percent
                (``Controller._development_percent``), charge/FC-referenced. Its
                charge/FC clock origins honour MCP v0.1.7's backdated T0/FC instant
                (#337), so this dev% reads ~+1.8 pp higher than a receive-tick
                origin would — releasing this guard ~1-2 pp earlier, on a truer
                dev%. It still fails safe: a below-window read only HOLDS the drop.
            target_development_percent: The profile's development-ratio target.
            margin_percent: The tolerance below target within which a drop is still
                honoured (``ControllerConfig.drop_dev_margin_percent``).
            current_heat: The heat level currently in effect (held).
            current_fan: The fan level currently in effect (held).

        Returns:
            A REJECT :class:`SafetyEvaluation` whose adjusted values hold the
            current targets.
        """
        floor = target_development_percent - margin_percent
        return SafetyEvaluation(
            rule="advisor_drop_coherence",
            verdict=SafetyVerdict.REJECT,
            adjusted_heat=current_heat,
            adjusted_fan=current_fan,
            reason=(
                f"advisor drop blocked: system development {system_development_percent:.2f} % "
                f"below the drop window floor {floor:.2f} % "
                f"(target {target_development_percent:.2f} % - margin {margin_percent:.2f} pp); "
                f"drop withheld, current targets held (heat {current_heat} %, fan {current_fan} %)"
            ),
        )

    def evaluate_advisor_low_confidence(
        self,
        *,
        confidence: float,
        min_confidence: float,
        current_heat: int,
        current_fan: int,
    ) -> SafetyEvaluation:
        """Low-confidence advisor recommendation ⇒ rejected ⇒ deterministic hold (#276).

        A post-FC recommendation whose ``confidence`` is below the configured
        ``min_confidence`` floor is treated as "I don't know" and fails closed:
        the recommendation is REJECTed and the deterministic fallback holds the
        current targets (the adjusted values echo the heat/fan already in effect),
        exactly like the reachable-but-misbehaving failures
        (:meth:`evaluate_advisor_failure`). It never blocks the tick and it never
        actuates — a model that is unsure must not move the levers. The
        recommendation is still persisted in the decision trace; this verdict is
        the no-write outcome attached to it.

        A confidence at or above the floor is the caller's signal to proceed to
        the command-bounds evaluation; this method is only consulted on the
        below-floor path, so it always returns REJECT.

        Args:
            confidence: The advisor's self-reported confidence (0-1).
            min_confidence: The configured post-FC confidence floor (0-1).
            current_heat: The heat level currently in effect (held).
            current_fan: The fan level currently in effect (held).

        Returns:
            A REJECT :class:`SafetyEvaluation` whose adjusted values hold the
            current targets.
        """
        return SafetyEvaluation(
            rule="advisor_low_confidence",
            verdict=SafetyVerdict.REJECT,
            adjusted_heat=current_heat,
            adjusted_fan=current_fan,
            reason=(
                f"advisor confidence {confidence:.2f} below the post-FC floor "
                f"{min_confidence:.2f}: recommendation rejected, deterministic fallback holds "
                f"current targets (heat {current_heat} %, fan {current_fan} %)"
            ),
        )

    def evaluate_advisor_failure(
        self,
        *,
        status: Literal["malformed", "unsafe"],
        current_heat: int,
        current_fan: int,
    ) -> SafetyEvaluation:
        """Provider-reachable advisor failure ⇒ rejected recommendation ⇒ hold.

        The reachable-but-misbehaving failures — ``malformed`` (output the
        model could not produce) and ``unsafe`` (a well-shaped but
        out-of-bounds command) — never block the tick (orchestration plan
        § Advisory Call Frequency): the recommendation is REJECTed and the
        fallback holds the current targets (the adjusted values echo the
        heat/fan already in effect). Every outcome is persisted via the
        decision trace (plan §5).

        The *availability* failures (``timeout`` / ``provider_error``) are a
        different class — they route to :meth:`evaluate_advisor_availability`
        (D30, #166), which counts consecutive outages toward the fail-closed
        stop — and deliberately never reach this rule.
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

    def evaluate_advisor_availability(
        self,
        *,
        consecutive_failures: int,
        current_heat: int,
        current_fan: int,
    ) -> SafetyEvaluation:
        """Sustained advisor-availability outage ⇒ fail closed after N (D30, #166).

        Mirrors :meth:`evaluate_mcp_failure` for the advisor, but escalates to
        RECOVERY rather than FAULT: a sustained advisor outage is an
        operator-in-the-loop stop, not a machine fault. The caller counts only
        the *availability* failures (``provider_error`` / ``timeout``) toward
        ``consecutive_failures`` — ``malformed`` / ``unsafe`` are
        provider-reachable (a different class) and never reach this rule.

        Below ``max_consecutive_advisor_failures`` the recommendation is
        REJECTed and the deterministic fallback holds the current targets
        (the E3-S3 behavior, unchanged) — a single transient blip never stops
        the roast. At or above the threshold the verdict is RECOVERY carrying
        heat 0 % and the configured safe fan: the controller drives heat down
        through the safety path and enters ``operator_recovery_required``,
        where the operator must explicitly resume / drop / cool. The hard
        temperature ceilings and emergency stop stay active throughout
        regardless of this rule.

        Args:
            consecutive_failures: Consecutive advisor *availability* failures
                observed (``>= 1``; the caller increments only on
                ``provider_error`` / ``timeout`` and resets on a successful
                ``ok`` decision).
            current_heat: The heat level currently in effect (held on REJECT).
            current_fan: The fan level currently in effect (held on REJECT).

        Returns:
            A :class:`SafetyEvaluation`: REJECT holding current targets below
            the threshold, RECOVERY with heat 0 % / safe fan at or above it.

        Raises:
            ValueError: If ``consecutive_failures`` is less than 1.
        """
        if consecutive_failures < 1:
            raise ValueError("consecutive_failures must be >= 1")
        limits = self._limits
        if consecutive_failures < limits.max_consecutive_advisor_failures:
            return SafetyEvaluation(
                rule="advisor_unavailable_tolerated",
                verdict=SafetyVerdict.REJECT,
                adjusted_heat=current_heat,
                adjusted_fan=current_fan,
                reason=(
                    f"{consecutive_failures} consecutive advisor availability failure(s), below "
                    f"the fail-closed threshold {limits.max_consecutive_advisor_failures}: "
                    f"recommendation rejected, deterministic fallback holds current targets "
                    f"(heat {current_heat} %, fan {current_fan} %)"
                ),
            )
        return SafetyEvaluation(
            rule="advisor_unavailable_exhausted",
            verdict=SafetyVerdict.RECOVERY,
            adjusted_heat=0,
            adjusted_fan=limits.overrun_safe_fan_percent,
            reason=(
                f"{consecutive_failures} consecutive advisor availability failures (threshold "
                f"{limits.max_consecutive_advisor_failures}): the advisor is sustainedly "
                f"unavailable — failing closed, heat 0 %, fan "
                f"{limits.overrun_safe_fan_percent} %, operator recovery required (resume / "
                f"drop / cool)"
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
