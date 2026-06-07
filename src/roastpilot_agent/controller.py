"""Deterministic controller (component plan §3–§4; orchestration plan
§ State Machine, § Controller Loop).

Owns the transition table, the monotonic 1.0 s tick() loop (E4-S2), the T0
debounce (E4-S3), and transition methods. The RoastPhase vocabulary lives
in models.py (D15) and is re-exported here for plan §4 compatibility.

The advisor cannot trigger state transitions — structurally: no transition
API accepts advisor output, and T0/FC sources are additionally validated by
safety.evaluate_event_source (E3-S5). Restart never auto-resumes heat or
fan (``operator_recovery_required``).
"""

from roastpilot_agent.models import RoastPhase

__all__ = [
    "TRANSITION_TABLE",
    "InvalidTransitionError",
    "RoastController",
    "RoastPhase",
]


class InvalidTransitionError(Exception):
    """Raised when a phase transition is not in the transition table."""

    def __init__(self, current: RoastPhase, target: RoastPhase) -> None:
        self.current = current
        self.target = target
        super().__init__(f"invalid transition: {current.value} -> {target.value}")


# Normal-path and operator-driven edges (plan §3 / orchestration plan
# § State Machine "Recommended transition ownership"). The universal edges
# `* -> faulted` and `* -> operator_recovery_required` are handled in
# RoastController.can_transition, not listed per row.
TRANSITION_TABLE: dict[RoastPhase, frozenset[RoastPhase]] = {
    # Operator starts a roast.
    RoastPhase.IDLE: frozenset({RoastPhase.STARTING}),
    # MCP session started successfully.
    RoastPhase.STARTING: frozenset({RoastPhase.PREHEATING}),
    # MCP reports confirmed T0 (debounced, E4-S3).
    RoastPhase.PREHEATING: frozenset({RoastPhase.ROASTING_PRE_FIRST_CRACK}),
    # First crack detected or operator override.
    RoastPhase.ROASTING_PRE_FIRST_CRACK: frozenset({RoastPhase.DEVELOPMENT}),
    # Validated drop decision or operator drop.
    RoastPhase.DEVELOPMENT: frozenset({RoastPhase.COOLING}),
    # Cooling stopped and logs exported.
    RoastPhase.COOLING: frozenset({RoastPhase.COMPLETE}),
    # Run finalized: the controller returns to idle for the next run.
    # (Refinement over plan §3, which leaves `complete` with no exit
    # trigger — a long-running service needs the reset edge.)
    RoastPhase.COMPLETE: frozenset({RoastPhase.IDLE}),
    # Operator acknowledgement ends a faulted run.
    RoastPhase.FAULTED: frozenset({RoastPhase.IDLE}),
    # Explicit operator action only (orchestration plan § Persistence:
    # resume, drop, cool, or end the run — never automatic). Resume targets
    # cover the persisted active phases; ending goes to complete (logs
    # exported) or idle (abandoned).
    RoastPhase.OPERATOR_RECOVERY_REQUIRED: frozenset(
        {
            RoastPhase.PREHEATING,
            RoastPhase.ROASTING_PRE_FIRST_CRACK,
            RoastPhase.DEVELOPMENT,
            RoastPhase.COOLING,
            RoastPhase.COMPLETE,
            RoastPhase.IDLE,
        }
    ),
}
"""Explicit transition table: maps each phase to its legal targets
(excluding the universal faulted/recovery edges). Every phase has a row;
a test pins completeness."""

#: Phases any state may fall into (plan §3: `* -> faulted`,
#: `* -> operator_recovery_required`). Self-transitions are not transitions.
UNIVERSAL_TARGETS: frozenset[RoastPhase] = frozenset(
    {RoastPhase.FAULTED, RoastPhase.OPERATOR_RECOVERY_REQUIRED}
)


class RoastController:
    """Code-owned deterministic state machine and tick loop.

    E4-S1 owns the transition mechanics; the tick pipeline (read state →
    persist → safety → transitions → advisory? → validate → execute →
    persist → emit) lands in E4-S2. Transition methods take only the
    target phase — there is deliberately no API through which advisor
    output can reach a transition.
    """

    def __init__(self) -> None:
        self._phase: RoastPhase = RoastPhase.IDLE

    @property
    def phase(self) -> RoastPhase:
        """Current agent phase."""
        return self._phase

    def can_transition(self, target: RoastPhase) -> bool:
        """Whether ``target`` is a legal next phase from the current one."""
        if target is self._phase:
            return False
        if target in UNIVERSAL_TARGETS:
            return True
        return target in TRANSITION_TABLE[self._phase]

    def transition_to(self, target: RoastPhase) -> None:
        """Commit a phase transition or raise :class:`InvalidTransitionError`.

        Persistence and event emission around transitions are wired in
        E4-S2; this method owns legality only.
        """
        if not self.can_transition(target):
            raise InvalidTransitionError(self._phase, target)
        self._phase = target

    async def tick(self) -> None:
        """Run one controller tick on the monotonic fixed-rate schedule (E4-S2)."""
        raise NotImplementedError("E4-S2: controller tick loop")
