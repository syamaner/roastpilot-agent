"""Focused recovery and arbiter safety tests for fault-control races."""

from __future__ import annotations

import asyncio

import pytest

from roastpilot_agent.controller import CommandArbiter


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

    async def must_not_run() -> None:
        calls.append("estop")

    waiting_stop = asyncio.create_task(arbiter.run_emergency_stop(must_not_run))
    for _ in range(50):
        if arbiter._condition._waiters:  # pyright: ignore[reportPrivateUsage] - race point proof
            break
        await asyncio.sleep(0)
    assert arbiter._condition._waiters  # pyright: ignore[reportPrivateUsage] - race point proof
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
