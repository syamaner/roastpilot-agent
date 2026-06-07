"""Deterministic controller (component plan §3–§4; orchestration plan
§ State Machine, § Controller Loop).

Owns the transition table, the monotonic 1.0 s tick() loop, the T0
debounce, and transition methods. The RoastPhase vocabulary lives in
models.py (D15) and is re-exported here for plan §4 compatibility. The
advisor cannot trigger state transitions; restart never auto-resumes heat
or fan (``operator_recovery_required``). Implementation lands in E4.
"""

from roastpilot_agent.models import RoastPhase

__all__ = ["RoastController", "RoastPhase"]


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
