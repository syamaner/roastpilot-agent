"""Deterministic controller (component plan §3–§4; orchestration plan
§ State Machine, § Controller Loop).

Owns the RoastPhase state machine, the monotonic 1.0 s tick() loop, the T0
debounce, and transition methods. The advisor cannot trigger state
transitions; restart never auto-resumes heat or fan
(``operator_recovery_required``). Implementation lands in E4.
"""

from enum import StrEnum


class RoastPhase(StrEnum):
    """Agent phases — the operator-facing truth (component plan §3)."""

    IDLE = "idle"
    STARTING = "starting"
    PREHEATING = "preheating"
    ROASTING_PRE_FIRST_CRACK = "roasting_pre_first_crack"
    DEVELOPMENT = "development"
    COOLING = "cooling"
    COMPLETE = "complete"
    FAULTED = "faulted"
    OPERATOR_RECOVERY_REQUIRED = "operator_recovery_required"


class RoastController:
    """Code-owned deterministic state machine and tick loop (E4).

    Each tick: read MCP state → persist snapshot → evaluate safety →
    apply transitions → (maybe) consult advisor → validate → execute
    approved commands → persist results → emit UI events.
    """

    def __init__(self) -> None:
        self._phase: RoastPhase = RoastPhase.IDLE

    @property
    def phase(self) -> RoastPhase:
        """Current agent phase."""
        return self._phase

    async def tick(self) -> None:
        """Run one controller tick on the monotonic fixed-rate schedule (E4)."""
        raise NotImplementedError("E4: controller tick loop")
