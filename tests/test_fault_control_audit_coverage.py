"""Hardware-free audit and shutdown coverage for terminal fault controls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

import pytest

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.controller import CommandArbiter, FaultControlExecutor
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


class _BlockingPort(_Port):
    """Port that holds one guarded write until the test releases it."""

    def __init__(self) -> None:
        """Initialise explicit event ordering for a single in-flight write."""
        super().__init__()
        self.write_entered = asyncio.Event()
        self.release_write = asyncio.Event()

    async def write_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> None:
        """Block the exact command boundary after recording the attempted action."""
        await super().write_fault_control(action, expected_session_id=expected_session_id)
        self.write_entered.set()
        await self.release_write.wait()


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


@pytest.mark.asyncio
async def test_unreserved_estop_paths_do_not_strand_later_routine_work() -> None:
    """Direct arbiter callers cannot create a negative or stranded e-stop reservation."""
    arbiter = CommandArbiter()
    operations: list[str] = []

    async def routine() -> str:
        operations.append("routine")
        return "routine"

    async def stop() -> str:
        operations.append("stop")
        return "stopped"

    with pytest.raises(RuntimeError, match="admission is no longer current"):
        await arbiter.run_emergency_stop(stop, admitted=lambda: False)
    assert not arbiter.emergency_stop_active
    assert await arbiter.run_routine(arbiter.generation, routine) == (True, "routine")

    assert await arbiter.run_emergency_stop(stop) == "stopped"
    assert not arbiter.emergency_stop_active
    assert await arbiter.run_routine(arbiter.generation, routine) == (True, "routine")
    assert operations == ["routine", "stop", "routine"]


@pytest.mark.asyncio
async def test_cancelled_queued_estop_releases_priority_after_the_inflight_routine() -> None:
    """A cancelled queued e-stop cannot strand its reservation or dispatch after cancellation."""
    arbiter = CommandArbiter()
    port = _BlockingPort()
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, arbiter=arbiter)
    routine = asyncio.create_task(
        executor.execute(
            OperatorAction.STOP_COOLING,
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
    for _ in range(50):
        if arbiter.emergency_stop_active and executor._queue.qsize() == 1:  # pyright: ignore[reportPrivateUsage] - queued cancellation proof
            break
        await asyncio.sleep(0)
    assert arbiter.emergency_stop_active
    assert executor._queue.qsize() == 1  # pyright: ignore[reportPrivateUsage] - queued cancellation proof

    stopped.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopped
    port.release_write.set()
    assert await asyncio.wait_for(routine, timeout=1.0) is None
    assert await asyncio.wait_for(executor.settle_for_shutdown(), timeout=1.0)
    assert port.writes == [OperatorAction.STOP_COOLING]
    assert not arbiter.emergency_stop_active

    async def later_routine() -> str:
        return "later"

    assert await arbiter.run_routine(arbiter.generation, later_routine) == (True, "later")


@pytest.mark.asyncio
async def test_cancelled_queued_routine_never_dispatches_after_an_inflight_estop() -> None:
    """A cancelled routine waiter cannot write after the admitted e-stop completes."""
    arbiter = CommandArbiter()
    port = _BlockingPort()
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port, arbiter=arbiter)
    stopping = asyncio.create_task(
        executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            is_current_generation=lambda: True,
        )
    )
    await asyncio.wait_for(port.write_entered.wait(), timeout=1.0)
    routine = asyncio.create_task(
        executor.execute(
            OperatorAction.STOP_COOLING,
            "fault-session",
            is_current_generation=lambda: True,
        )
    )
    for _ in range(50):
        if executor._queue.qsize() == 1:  # pyright: ignore[reportPrivateUsage] - queued cancellation proof
            break
        await asyncio.sleep(0)
    assert executor._queue.qsize() == 1  # pyright: ignore[reportPrivateUsage] - queued cancellation proof

    routine.cancel()
    with pytest.raises(asyncio.CancelledError):
        await routine
    port.release_write.set()
    assert await asyncio.wait_for(stopping, timeout=1.0) == _fault_state()
    assert await asyncio.wait_for(executor.settle_for_shutdown(), timeout=1.0)
    assert port.writes == [OperatorAction.EMERGENCY_STOP]
    assert not arbiter.emergency_stop_active


class _FatalDispatchFailure(BaseException):
    """Model a non-``Exception`` adapter failure that escapes the drain worker."""


class _FatalPort(_Port):
    """Port that records the attempted e-stop before a fatal adapter failure."""

    async def write_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> None:
        """Raise a fatal adapter failure after the exact write boundary is entered."""
        await super().write_fault_control(action, expected_session_id=expected_session_id)
        raise _FatalDispatchFailure("synthetic fatal adapter failure")


@pytest.mark.asyncio
async def test_shutdown_fails_closed_when_a_fatal_adapter_error_ends_the_worker() -> None:
    """A nonordinary adapter failure leaves a queued e-stop uncertain at shutdown."""
    port = _FatalPort()
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port)
    waiting = asyncio.create_task(
        executor.execute(
            OperatorAction.EMERGENCY_STOP,
            "fault-session",
            is_current_generation=lambda: True,
        )
    )
    for _ in range(50):
        worker = executor._worker  # pyright: ignore[reportPrivateUsage] - worker termination proof
        if worker is not None and worker.done():
            break
        await asyncio.sleep(0)
    worker = executor._worker  # pyright: ignore[reportPrivateUsage] - worker termination proof
    assert worker is not None and worker.done()
    assert isinstance(worker.exception(), _FatalDispatchFailure)
    assert not waiting.done()
    assert not await executor.settle_for_shutdown()
    assert port.writes == [OperatorAction.EMERGENCY_STOP]

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
