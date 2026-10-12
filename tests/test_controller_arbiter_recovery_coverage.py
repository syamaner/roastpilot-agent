"""Focused recovery and arbiter safety tests for fault-control races."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from roastpilot_agent.config import SafetyLimits
from roastpilot_agent.controller import CommandArbiter, FaultControlExecutor
from roastpilot_agent.models import FaultControlState, OperatorAction
from roastpilot_agent.safety import SafetyPolicy


@dataclass
class SupersedingReadPort:
    """Exact-session port that invalidates its caller after its first proof read."""

    state: FaultControlState
    generation_current: list[bool]
    reads: list[str] = field(default_factory=list[str])
    writes: list[OperatorAction] = field(default_factory=list[OperatorAction])

    async def read_fault_control_state(self, session_id: str) -> FaultControlState:
        """Return an exact fault state, then make the captured generation stale."""
        self.reads.append(session_id)
        self.generation_current[0] = False
        return self.state

    async def read_latest_fault_control_state(self) -> FaultControlState:
        """Return the same state for the unused D212 latest-state capability."""
        return self.state

    async def write_fault_control(
        self, action: OperatorAction, *, expected_session_id: str
    ) -> None:
        """Record any write so the stale-proof assertion can reject it."""
        assert expected_session_id == "fault-session"
        self.writes.append(action)


def _fault_state() -> FaultControlState:
    """Build one connected, cooling-active exact fault state."""
    return FaultControlState.model_validate(
        {
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
    )


@pytest.mark.asyncio
async def test_close_proof_waiter_fails_when_estop_revokes_its_reservation() -> None:
    """A D212 safe-zero proof cannot survive a priority e-stop admission."""
    arbiter = CommandArbiter()
    routine_entered = asyncio.Event()
    release_routine = asyncio.Event()

    async def routine() -> None:
        routine_entered.set()
        await release_routine.wait()

    generation = arbiter.generation
    running = asyncio.create_task(arbiter.run_routine(generation, routine))
    await asyncio.wait_for(routine_entered.wait(), timeout=1.0)
    assert arbiter.reserve_close(generation)
    waiting_for_proof = asyncio.create_task(arbiter.wait_for_close_proof(generation))
    for _ in range(50):
        if arbiter._condition._waiters:  # pyright: ignore[reportPrivateUsage] - race point proof
            break
        await asyncio.sleep(0)
    assert arbiter._condition._waiters  # pyright: ignore[reportPrivateUsage] - race point proof

    arbiter.admit_emergency_stop()
    release_routine.set()

    assert not await asyncio.wait_for(waiting_for_proof, timeout=1.0)
    assert await asyncio.wait_for(running, timeout=1.0) == (True, None)


@pytest.mark.asyncio
async def test_cancelled_estop_waiter_releases_priority_reservation() -> None:
    """Cancelling an e-stop before dispatch leaves the next routine write available."""
    arbiter = CommandArbiter()
    routine_entered = asyncio.Event()
    release_routine = asyncio.Event()
    calls: list[str] = []

    async def blocking_routine() -> None:
        routine_entered.set()
        await release_routine.wait()
        calls.append("initial")

    running = asyncio.create_task(arbiter.run_routine(arbiter.generation, blocking_routine))
    await asyncio.wait_for(routine_entered.wait(), timeout=1.0)
    generation = arbiter.admit_emergency_stop()
    waiting_stop = asyncio.create_task(arbiter.run_emergency_stop(lambda: blocking_routine()))
    await asyncio.sleep(0)
    waiting_stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_stop
    assert not arbiter.emergency_stop_active

    release_routine.set()
    assert await asyncio.wait_for(running, timeout=1.0) == (True, None)

    async def later_routine() -> str:
        calls.append("later")
        return "safe"

    assert await arbiter.run_routine(generation, later_routine) == (True, "safe")
    assert calls == ["initial", "later"]


@pytest.mark.asyncio
async def test_stale_estop_admission_releases_slot_for_current_routine() -> None:
    """A stale exact-session e-stop never writes and cannot permanently fence recovery."""
    arbiter = CommandArbiter()
    generation = arbiter.admit_emergency_stop()
    calls: list[str] = []

    async def must_not_run() -> None:
        calls.append("estop")

    with pytest.raises(RuntimeError, match="admission is no longer current"):
        await arbiter.run_emergency_stop(must_not_run, admitted=lambda: False)
    assert not arbiter.emergency_stop_active

    async def routine() -> str:
        calls.append("routine")
        return "completed"

    assert await arbiter.run_routine(generation, routine) == (True, "completed")
    assert calls == ["routine"]


@pytest.mark.asyncio
async def test_completed_estop_releases_its_admission_before_later_routine_work() -> None:
    """A delivered e-stop consumes its reservation and does not block later safe work."""
    arbiter = CommandArbiter()
    generation = arbiter.admit_emergency_stop()
    calls: list[str] = []

    async def estop() -> str:
        calls.append("estop")
        return "stopped"

    assert await arbiter.run_emergency_stop(estop) == "stopped"
    assert not arbiter.emergency_stop_active

    async def routine() -> str:
        calls.append("routine")
        return "completed"

    assert await arbiter.run_routine(generation, routine) == (True, "completed")
    assert calls == ["estop", "routine"]


@pytest.mark.asyncio
async def test_idempotent_estop_cancellation_does_not_fence_routine_writes() -> None:
    """Late duplicate cleanup has no reservation to consume and still leaves the arbiter live."""
    arbiter = CommandArbiter()
    await arbiter.cancel_emergency_stop_admission()
    calls: list[str] = []

    async def routine() -> str:
        calls.append("routine")
        return "completed"

    assert await arbiter.run_routine(arbiter.generation, routine) == (True, "completed")
    assert calls == ["routine"]


@pytest.mark.asyncio
async def test_routine_generation_stale_after_exact_read_never_reaches_write() -> None:
    """A fault action cannot write after its exact-state proof becomes stale."""
    generation_current = [True]
    port = SupersedingReadPort(_fault_state(), generation_current)
    executor = FaultControlExecutor(SafetyPolicy(SafetyLimits()), port)

    assert (
        await executor.execute(
            OperatorAction.STOP_COOLING,
            "fault-session",
            is_current_generation=lambda: generation_current[0],
        )
        is None
    )
    assert port.reads == ["fault-session"]
    assert port.writes == []
