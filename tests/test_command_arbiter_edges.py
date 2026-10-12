"""Concurrency-edge tests for the controller's one-writer command arbiter."""

from __future__ import annotations

import asyncio

import pytest

from roastpilot_agent.controller import CommandArbiter


@pytest.mark.asyncio
async def test_revoked_close_reservation_fails_while_waiting_for_an_inflight_write() -> None:
    """A D212 proof waiter returns false when an admitted e-stop revokes its lease."""
    arbiter = CommandArbiter()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_routine() -> str:
        entered.set()
        await release.wait()
        return "routine-complete"

    generation = arbiter.generation
    routine = asyncio.create_task(arbiter.run_routine(generation, blocking_routine))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    assert arbiter.reserve_close(generation)
    proof = asyncio.create_task(arbiter.wait_for_close_proof(generation))
    await asyncio.sleep(0)

    arbiter.admit_emergency_stop()
    release.set()

    assert not await asyncio.wait_for(proof, timeout=1.0)
    assert await asyncio.wait_for(routine, timeout=1.0) == (True, "routine-complete")


@pytest.mark.asyncio
async def test_stale_admitted_estop_releases_priority_without_running_its_operation() -> None:
    """A stale exact-session e-stop releases its admission before a later routine action."""
    arbiter = CommandArbiter()
    generation = arbiter.admit_emergency_stop()
    calls: list[str] = []

    async def should_not_run() -> None:
        calls.append("estop")

    with pytest.raises(RuntimeError, match="admission is no longer current"):
        await arbiter.run_emergency_stop(should_not_run, admitted=lambda: False)

    assert calls == []
    assert not arbiter.emergency_stop_active

    async def routine() -> str:
        calls.append("routine")
        return "routine-complete"

    assert await arbiter.run_routine(generation, routine) == (True, "routine-complete")
    assert calls == ["routine"]


@pytest.mark.asyncio
async def test_pending_estop_discards_routine_then_runs_before_any_new_write() -> None:
    """A queued e-stop reserves the next writer boundary ahead of routine control."""
    arbiter = CommandArbiter()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def blocking_routine() -> str:
        calls.append("routine-start")
        entered.set()
        await release.wait()
        calls.append("routine-end")
        return "first"

    first = asyncio.create_task(arbiter.run_routine(arbiter.generation, blocking_routine))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    generation = arbiter.admit_emergency_stop()

    async def estop() -> str:
        calls.append("estop")
        return "stopped"

    stopping = asyncio.create_task(arbiter.run_emergency_stop(estop))
    await asyncio.sleep(0)

    async def discarded_routine() -> str:
        calls.append("discarded")
        return "unsafe"

    assert await arbiter.run_routine(generation, discarded_routine) == (False, None)
    release.set()

    assert await asyncio.wait_for(first, timeout=1.0) == (True, "first")
    assert await asyncio.wait_for(stopping, timeout=1.0) == "stopped"
    assert calls == ["routine-start", "routine-end", "estop"]


@pytest.mark.asyncio
async def test_cancelled_waiting_estop_releases_its_reservation_for_later_work() -> None:
    """Cancelling an e-stop that waits behind a write cannot permanently block the arbiter."""
    arbiter = CommandArbiter()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def blocking_routine() -> None:
        entered.set()
        await release.wait()
        calls.append("first-routine")

    first = asyncio.create_task(arbiter.run_routine(arbiter.generation, blocking_routine))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    generation = arbiter.admit_emergency_stop()

    estop_entered = asyncio.Event()
    keep_estop_open = asyncio.Event()

    async def blocking_estop() -> None:
        calls.append("estop")
        estop_entered.set()
        await keep_estop_open.wait()

    stopping = asyncio.create_task(arbiter.run_emergency_stop(blocking_estop))
    release.set()
    await asyncio.wait_for(estop_entered.wait(), timeout=1.0)
    assert arbiter.emergency_stop_active
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    for _ in range(50):
        if not arbiter.emergency_stop_active:
            break
        await asyncio.sleep(0)
    assert not arbiter.emergency_stop_active

    assert await asyncio.wait_for(first, timeout=1.0) == (True, None)

    async def later_routine() -> str:
        calls.append("later-routine")
        return "later"

    assert await asyncio.wait_for(arbiter.run_routine(generation, later_routine), timeout=1.0) == (
        True,
        "later",
    )
    assert calls == ["first-routine", "estop", "later-routine"]
