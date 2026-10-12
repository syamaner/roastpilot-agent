"""Hardware-free audit and shutdown coverage for terminal fault controls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pytest

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.controller import FaultControlExecutor
from roastpilot_agent.models import FaultControlState, OperatorAction, RoastCommand
from roastpilot_agent.safety import SafetyEvaluation, SafetyPolicy


def _fault_state() -> FaultControlState:
    """Return a confirmed, inactive exact fault-session state."""
    return FaultControlState(
        session_id="fault-session",
        mcp_phase="fault",
        active=False,
        device_connected=True,
        heat_level_percent=0,
        fan_level_percent=0,
        cooling_on=True,
        beans_added=True,
        beans_dropped=False,
    )


@dataclass
class _Port:
    """Record guarded commands while returning one stable exact-session proof."""

    writes: list[OperatorAction] = field(default_factory=list[OperatorAction])

    async def read_fault_control_state(self, session_id: str) -> FaultControlState:
        """Return the exact state for the requested terminal session."""
        assert session_id == "fault-session"
        return _fault_state()

    async def read_latest_fault_control_state(self) -> FaultControlState:
        """Return the matching latest fault state."""
        return _fault_state()

    async def write_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> None:
        """Record a guarded write at the exact fault-session boundary."""
        assert expected_session_id == "fault-session"
        self.writes.append(action)


@dataclass
class _EvaluationFailingTrace:
    """Trace double whose e-stop admission persistence is unavailable."""

    commands: list[tuple[RoastCommand, Literal["ok", "failed"], int | None, object]] = field(
        default_factory=list[tuple[RoastCommand, Literal["ok", "failed"], int | None, object]]
    )

    async def record_fault_control_evaluation(
        self, *, run_id: str, evaluation: SafetyEvaluation
    ) -> int:
        """Model a durable audit failure after the e-stop's safe dispatch."""
        assert run_id == "fault-run"
        raise RuntimeError("synthetic post-dispatch audit failure")

    async def record_fault_control_command(
        self,
        *,
        run_id: str,
        tool: RoastCommand,
        status: Literal["ok", "failed"],
        args: object,
        result: object,
        safety_evaluation_id: int | None,
    ) -> None:
        """Retain the truthful actuator outcome even without an evaluation row."""
        assert run_id == "fault-run"
        assert args == {"fault_control": True}
        self.commands.append((tool, status, safety_evaluation_id, result))


@pytest.mark.asyncio
async def test_estop_dispatch_succeeds_when_post_dispatch_evaluation_audit_fails() -> None:
    """An e-stop remains confirmed when only its post dispatch audit row fails."""
    port = _Port()
    trace = _EvaluationFailingTrace()
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=trace)

    assert (
        await executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: True,
        )
    ) == _fault_state()
    assert port.writes == [OperatorAction.EMERGENCY_STOP]
    assert await executor.settle_audit_tasks()
    assert trace.commands == [
        (
            RoastCommand.EMERGENCY_STOP,
            "ok",
            None,
            {
                "admission": "allowed",
                "dispatch": "completed",
                "confirmation": "read_back",
            },
        )
    ]
