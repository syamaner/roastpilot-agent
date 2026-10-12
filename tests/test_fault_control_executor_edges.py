"""Focused safety-edge coverage for terminal fault-control execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal, cast

import pytest

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.controller import CommandArbiter, FaultControlExecutor
from roastpilot_agent.models import FaultControlState, OperatorAction, RoastCommand, RoastPhase
from roastpilot_agent.safety import SafetyEvaluation, SafetyPolicy, SafetyVerdict


@dataclass
class RecordingFaultTrace:
    """In-memory durable-boundary double that records terminal control facts."""

    evaluations: list[SafetyEvaluation] = field(default_factory=list[SafetyEvaluation])
    commands: list[tuple[RoastCommand, Literal["ok", "failed"], object]] = field(
        default_factory=list[tuple[RoastCommand, Literal["ok", "failed"], object]]
    )
    fail_command_recording: bool = False

    async def record_fault_control_evaluation(
        self, *, run_id: str, evaluation: SafetyEvaluation
    ) -> int:
        """Record a required typed safety admission."""
        assert run_id == "fault-run"
        self.evaluations.append(evaluation)
        return len(self.evaluations)

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
        """Record an exact-session outcome linked to a prior admission."""
        assert run_id == "fault-run"
        assert args == {"fault_control": True}
        assert safety_evaluation_id is not None
        if self.fail_command_recording:
            raise RuntimeError("synthetic audit storage failure")
        self.commands.append((tool, status, result))


@dataclass
class FaultPort:
    """Narrow fake exact-session port with explicit state and write outcomes."""

    state: FaultControlState
    latest: FaultControlState | None = None
    fail_write: bool = False
    fail_readback: bool = False
    writes: list[OperatorAction] = field(default_factory=list[OperatorAction])

    async def read_fault_control_state(self, session_id: str) -> FaultControlState:
        """Return one typed exact-session state."""
        assert session_id == "fault-session"
        if self.fail_readback and self.writes:
            raise RuntimeError("synthetic exact readback failure")
        return self.state

    async def read_latest_fault_control_state(self) -> FaultControlState:
        """Return the latest terminal state used by D212."""
        return self.latest or self.state

    async def write_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> None:
        """Record a guarded write or surface the configured adapter failure."""
        assert expected_session_id == "fault-session"
        self.writes.append(action)
        if self.fail_write:
            raise RuntimeError("synthetic fault-control write failure")


def _state(**changes: object) -> FaultControlState:
    """Build a safe inactive fault snapshot and apply one test's differences."""
    fields: dict[str, object] = {
        "session_id": "fault-session",
        "mcp_phase": "fault",
        "active": False,
        "device_connected": True,
        "heat_level_percent": 0,
        "fan_level_percent": 0,
        "cooling_on": True,
        "beans_added": True,
        "beans_dropped": False,
    }
    fields.update(changes)
    return FaultControlState.model_validate(fields)


class RejectingRoutineSafety:
    """Policy double that proves a rejected admission is traced before any write."""

    def evaluate_command_phase(
        self, *, command: RoastCommand, phase: RoastPhase
    ) -> SafetyEvaluation:
        """Reject an otherwise fault-valid routine command."""
        assert command is RoastCommand.STOP_COOLING
        assert phase is RoastPhase.FAULTED
        return SafetyEvaluation(
            rule="test_rejection",
            verdict=SafetyVerdict.REJECT,
            reason="test routine rejection",
        )


@pytest.mark.asyncio
async def test_rejected_routine_admission_is_traced_without_reading_or_writing() -> None:
    """A non-ALLOW routine decision records its rejection and never touches the port."""
    trace = RecordingFaultTrace()
    port = FaultPort(_state())
    executor = FaultControlExecutor(
        cast(SafetyPolicy, RejectingRoutineSafety()), port, trace_sink=trace
    )

    assert (
        await executor.execute(
            OperatorAction.STOP_COOLING,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: True,
        )
        is None
    )
    assert port.writes == []
    assert await executor.settle_audit_tasks()
    assert len(trace.evaluations) == 1
    assert trace.commands == [
        (
            RoastCommand.STOP_COOLING,
            "failed",
            {
                "admission": "rejected",
                "dispatch": "not_attempted",
                "confirmation": "failed",
            },
        )
    ]


@pytest.mark.asyncio
async def test_d212_stale_close_reservation_traces_no_write() -> None:
    """A superseded D212 reservation refuses its proof before reading or writing."""
    arbiter = CommandArbiter()
    generation = arbiter.generation
    assert arbiter.reserve_close(generation)
    arbiter.admit_emergency_stop()
    trace = RecordingFaultTrace()
    port = FaultPort(_state(cooling_on=False))
    executor = FaultControlExecutor(
        SafetyPolicy(SafetyLimits()), port, arbiter=arbiter, trace_sink=trace
    )

    assert (
        await executor.execute(
            OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: True,
            close_reservation=generation,
        )
        is None
    )
    assert port.writes == []
    assert await executor.settle_audit_tasks()
    assert trace.commands == [
        (
            RoastCommand.STOP_COOLING,
            "failed",
            {
                "admission": "allowed",
                "dispatch": "not_completed",
                "confirmation": "unconfirmed",
            },
        )
    ]


@pytest.mark.asyncio
async def test_d212_safe_zero_is_audited_without_a_stop_write() -> None:
    """D212 records its no-dispatch result when exact and latest reads prove safety."""
    arbiter = CommandArbiter()
    generation = arbiter.generation
    assert arbiter.reserve_close(generation)
    trace = RecordingFaultTrace()
    port = FaultPort(_state(cooling_on=False))
    executor = FaultControlExecutor(
        SafetyPolicy(SafetyLimits()), port, arbiter=arbiter, trace_sink=trace
    )

    assert (
        await executor.execute(
            OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: True,
            close_reservation=generation,
        )
    ) == _state(cooling_on=False)
    assert port.writes == []
    assert await executor.settle_audit_tasks()
    assert trace.commands == [
        (
            RoastCommand.STOP_COOLING,
            "ok",
            {
                "admission": "allowed",
                "dispatch": "not_required",
                "confirmation": "read_back",
            },
        )
    ]


@pytest.mark.asyncio
async def test_fault_state_identity_mismatch_is_traced_without_write() -> None:
    """A stale exact-session read cannot dispatch a routine command or claim confirmation."""
    trace = RecordingFaultTrace()
    port = FaultPort(_state(session_id="another-session"))
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=trace)

    assert (
        await executor.execute(
            OperatorAction.DROP_BEANS,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: True,
        )
        is None
    )
    assert port.writes == []
    assert await executor.settle_audit_tasks()
    assert trace.commands[0][2] == {
        "admission": "allowed",
        "dispatch": "not_attempted",
        "confirmation": "unconfirmed",
    }


@pytest.mark.asyncio
async def test_failed_routine_write_records_unconfirmed_failure() -> None:
    """An adapter failure retains a durable outcome without falsely reporting readback."""
    trace = RecordingFaultTrace()
    port = FaultPort(_state(), fail_write=True)
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=trace)

    with pytest.raises(RuntimeError, match="synthetic fault-control write failure"):
        await executor.execute(
            OperatorAction.DROP_BEANS,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: True,
        )

    assert port.writes == [OperatorAction.DROP_BEANS]
    assert await executor.settle_audit_tasks()
    assert trace.commands[0][2] == {
        "admission": "allowed",
        "dispatch": "failed",
        "confirmation": "unconfirmed",
    }


@pytest.mark.asyncio
async def test_estop_readback_failure_is_audited_after_the_priority_write() -> None:
    """A post-e-stop read error is traceable while preserving the completed write fact."""
    trace = RecordingFaultTrace()
    port = FaultPort(_state(), fail_readback=True)
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=trace)

    with pytest.raises(RuntimeError, match="synthetic exact readback failure"):
        await executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: True,
        )

    assert port.writes == [OperatorAction.EMERGENCY_STOP]
    assert await executor.settle_audit_tasks()
    assert trace.commands == [
        (
            RoastCommand.EMERGENCY_STOP,
            "failed",
            {
                "admission": "allowed",
                "dispatch": "completed",
                "confirmation": "readback_failed",
            },
        )
    ]


@pytest.mark.asyncio
async def test_estop_succeeds_when_its_audit_command_recording_fails() -> None:
    """Audit storage failure cannot change a completed e-stop's actuator outcome."""
    trace = RecordingFaultTrace(fail_command_recording=True)
    port = FaultPort(_state(heat_level_percent=0))
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=trace)

    assert (
        await executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: True,
        )
    ) == _state(heat_level_percent=0)
    assert port.writes == [OperatorAction.EMERGENCY_STOP]
    assert await executor.settle_audit_tasks()
    assert len(trace.evaluations) == 1
    assert trace.commands == []


@pytest.mark.asyncio
async def test_cancelled_queued_estop_releases_its_priority_admission() -> None:
    """Cancelling an e-stop waiter before dispatch cannot wedge later routine work."""

    class BlockingRoutinePort(FaultPort):
        def __init__(self) -> None:
            super().__init__(_state())
            self.write_entered = asyncio.Event()
            self.release_write = asyncio.Event()

        async def write_fault_control(
            self, action: OperatorAction, *, expected_session_id: str
        ) -> None:
            assert expected_session_id == "fault-session"
            self.writes.append(action)
            if action is OperatorAction.DROP_BEANS:
                self.write_entered.set()
                await self.release_write.wait()

    arbiter = CommandArbiter()
    port = BlockingRoutinePort()
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, arbiter=arbiter)
    routine = asyncio.create_task(
        executor.execute(
            OperatorAction.DROP_BEANS,
            "fault-session",
            is_current_generation=lambda: True,
        )
    )
    await asyncio.wait_for(port.write_entered.wait(), timeout=1.0)
    stopped = asyncio.create_task(
        executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            is_current_generation=lambda: True,
        )
    )
    await asyncio.sleep(0)
    stopped.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopped

    port.release_write.set()
    assert await asyncio.wait_for(routine, timeout=1.0) is None

    async def later_routine() -> str:
        return "later"

    assert await asyncio.wait_for(
        arbiter.run_routine(arbiter.generation, later_routine), timeout=1.0
    ) == (True, "later")


@pytest.mark.asyncio
async def test_d212_generation_superseded_after_latest_read_is_traced_without_write() -> None:
    """D212 rechecks its generation after the latest-state proof before writing."""

    class SupersedingLatestPort(FaultPort):
        def __init__(self) -> None:
            super().__init__(_state(cooling_on=True))
            self.current = True

        async def read_latest_fault_control_state(self) -> FaultControlState:
            self.current = False
            return self.state

    arbiter = CommandArbiter()
    generation = arbiter.generation
    assert arbiter.reserve_close(generation)
    trace = RecordingFaultTrace()
    port = SupersedingLatestPort()
    executor = FaultControlExecutor(
        SafetyPolicy(SafetyLimits()), port, arbiter=arbiter, trace_sink=trace
    )

    assert (
        await executor.execute(
            OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: port.current,
            close_reservation=generation,
        )
        is None
    )
    assert port.writes == []
    assert await executor.settle_audit_tasks()
    assert trace.commands[0][2] == {
        "admission": "allowed",
        "dispatch": "not_completed",
        "confirmation": "unconfirmed",
    }


@pytest.mark.asyncio
async def test_invalid_fault_drop_is_traced_without_a_write() -> None:
    """A faulted run with no beans cannot send a drop command to the roaster."""
    trace = RecordingFaultTrace()
    port = FaultPort(_state(beans_added=False))
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=trace)

    assert (
        await executor.execute(
            OperatorAction.DROP_BEANS,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: True,
        )
        is None
    )
    assert port.writes == []
    assert await executor.settle_audit_tasks()
    assert trace.commands[0][2] == {
        "admission": "allowed",
        "dispatch": "not_attempted",
        "confirmation": "unconfirmed",
    }


@pytest.mark.asyncio
async def test_routine_readback_failure_is_audited_after_a_completed_write() -> None:
    """A routine write does not become a success when its exact readback fails."""
    trace = RecordingFaultTrace()
    port = FaultPort(_state(), fail_readback=True)
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=trace)

    with pytest.raises(RuntimeError, match="synthetic exact readback failure"):
        await executor.execute(
            OperatorAction.DROP_BEANS,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: True,
        )

    assert port.writes == [OperatorAction.DROP_BEANS]
    assert await executor.settle_audit_tasks()
    assert trace.commands[0][2] == {
        "admission": "allowed",
        "dispatch": "completed",
        "confirmation": "readback_failed",
    }


@pytest.mark.asyncio
async def test_estop_superseded_after_write_is_traced_without_readback() -> None:
    """An e-stop whose generation becomes stale after dispatch cannot claim confirmation."""

    class SupersedingEStopPort(FaultPort):
        def __init__(self) -> None:
            super().__init__(_state())
            self.current = True

        async def write_fault_control(
            self, action: OperatorAction, *, expected_session_id: str
        ) -> None:
            await super().write_fault_control(action, expected_session_id=expected_session_id)
            self.current = False

    trace = RecordingFaultTrace()
    port = SupersedingEStopPort()
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=trace)

    assert (
        await executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: port.current,
        )
        is None
    )
    assert port.writes == [OperatorAction.EMERGENCY_STOP]
    assert await executor.settle_audit_tasks()
    assert trace.commands[0][2] == {
        "admission": "allowed",
        "dispatch": "completed",
        "confirmation": "superseded",
    }


@pytest.mark.asyncio
async def test_routine_generation_stale_after_exact_read_is_traced_without_write() -> None:
    """A generation invalidated during the exact state read cannot issue a stale command."""

    class SupersedingReadPort(FaultPort):
        def __init__(self) -> None:
            super().__init__(_state())
            self.current = True

        async def read_fault_control_state(self, session_id: str) -> FaultControlState:
            state = await super().read_fault_control_state(session_id)
            self.current = False
            return state

    trace = RecordingFaultTrace()
    port = SupersedingReadPort()
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, trace_sink=trace)

    assert (
        await executor.execute(
            OperatorAction.DROP_BEANS,
            "fault-session",
            run_id="fault-run",
            is_current_generation=lambda: port.current,
        )
        is None
    )
    assert port.writes == []
    assert await executor.settle_audit_tasks()
    assert trace.commands[0][2] == {
        "admission": "allowed",
        "dispatch": "not_completed",
        "confirmation": "unconfirmed",
    }
