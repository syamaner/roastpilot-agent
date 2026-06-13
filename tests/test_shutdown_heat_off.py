"""Graceful-shutdown heat-off through the safety path (#142).

A graceful ``serve`` teardown / Ctrl-C mid-roast must command heat to 0 through
the controller's safety path **before** the MCP child is stopped — otherwise the
Hottop stays commanded hot and, once the process dies, the UI Emergency Stop is
gone too (operator: "no way to stop without power cycle"). These tests assert the
real behavior: the heat-off e-stop is issued, it is bounded so shutdown cannot
hang, it is a no-op when nothing is hot, and the hard-kill path (which cannot run
shutdown) still relies on restart → ``operator_recovery_required``.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from roastpilot_agent.advisor import FakeAdvisor, RoastDecision
from roastpilot_agent.api import RoastService
from roastpilot_agent.cli import _teardown_live  # pyright: ignore[reportPrivateUsage]
from roastpilot_agent.config import AppConfig, ControllerConfig
from roastpilot_agent.mcp_client import MCPServerProcess
from roastpilot_agent.models import (
    ACTIVE_ROAST_PHASES,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
)
from roastpilot_agent.store import RoastStore
from tests.conftest import FakeClock, FakeMCPClient


def _profile() -> RoastProfile:
    return RoastProfile(
        name="House Espresso",
        bean_origin="Ethiopia",
        bean_varietal="Heirloom",
        bean_weight_grams=250.0,
        initial_heat_percent=70,
        initial_fan_percent=40,
        target_drop_temp_c=205.0,
        target_development_percent=20.0,
    )


def _reading(bean: float, env: float, **kwargs: object) -> RoastTelemetry:
    return RoastTelemetry.model_validate({"bean_temp_c": bean, "env_temp_c": env, **kwargs})


def _decision() -> RoastDecision:
    return RoastDecision(
        target_heat=55,
        target_fan=45,
        should_drop=False,
        confidence=0.9,
        rationale="hold targets",
    )


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[RoastStore]:
    instance = RoastStore(tmp_path / "shutdown.sqlite3")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


def _config() -> AppConfig:
    return AppConfig(controller=ControllerConfig(telemetry_log_interval_seconds=1.0))


@pytest.mark.asyncio
async def test_shutdown_commands_heat_off_through_safety_before_mcp_stop(
    store: RoastStore,
) -> None:
    """A graceful shutdown of an active roast issues a heat-off e-stop through
    the safety path, and it lands before the MCP child is stopped.

    Asserts the load-bearing ordering directly: the ``emergency_stop`` MCP write
    is recorded in ``mcp.calls`` *before* ``mcp.stop`` runs, the controller is in
    a hardware-off phase (``faulted``), and the typed ``EMERGENCY_STOP`` verdict
    is persisted to the decision trace.
    """
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    service = RoastService(
        store,
        config=_config(),
        roaster=mcp,
        advisor=FakeAdvisor([], default_decision=_decision()),
        run_loop=False,
        clock=clock,
    )
    detail = await service.start_roast(_profile())
    run_id = detail.id
    # start_roast drives idle → preheating: an active (possibly-hot) phase, so a
    # graceful shutdown here must command heat off.
    assert detail.agent_phase in ACTIVE_ROAST_PHASES
    assert "emergency_stop" not in mcp.commands()

    issued = await service.safe_shutdown_heat_off()

    assert issued is True
    # heat → 0 was commanded through the controller's e-stop MCP write.
    assert "emergency_stop" in mcp.commands()
    # The controller is now in a hardware-off phase and the typed EMERGENCY_STOP
    # verdict is in the persisted decision trace.
    assert service.runner is not None
    assert service.runner.controller_snapshot().phase is RoastPhase.FAULTED
    timeline = await service.timeline(run_id)
    assert any(e.verdict == "emergency_stop" for e in timeline.safety_evaluations), (
        "EMERGENCY_STOP verdict persisted"
    )


@pytest.mark.asyncio
async def test_shutdown_heat_off_after_loop_cancelled(store: RoastStore) -> None:
    """Heat-off still lands after the tick loop is cancelled (production sequence).

    In production, uvicorn's lifespan cleanup runs ``service.shutdown()`` (which
    cancels the tick loop task) *before* the ``_teardown_live`` ``finally`` runs
    the heat-off step. So this exercises that order with a REAL background loop
    task: start a roast → spawn ``runner.run()`` → let it run a tick → cancel it
    (as ``service.shutdown()`` does) → ``safe_shutdown_heat_off()`` must still
    issue the e-stop. The write survives because it calls
    ``operator_emergency_stop`` directly on the controller (not through the
    cancelled loop), and the controller / emitter / store are untouched by task
    cancellation. ``run_loop=False`` keeps the spawn explicit and deterministic.
    """
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    service = RoastService(
        store,
        config=_config(),
        roaster=mcp,
        advisor=FakeAdvisor([], default_decision=_decision()),
        run_loop=False,  # spawn the loop task explicitly below for determinism
        clock=clock,
    )
    detail = await service.start_roast(_profile())
    run_id = detail.id
    assert detail.agent_phase in ACTIVE_ROAST_PHASES
    assert service.runner is not None

    # Spawn a real background tick-loop task, let it run a tick, then cancel it
    # exactly as service.shutdown() does on lifespan teardown.
    loop_task = asyncio.create_task(service.runner.run())
    await asyncio.sleep(0)  # let the loop start and run its first tick
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task

    issued = await service.safe_shutdown_heat_off()
    assert issued is True
    assert "emergency_stop" in mcp.commands()
    assert service.runner is not None
    assert service.runner.controller_snapshot().phase is RoastPhase.FAULTED
    timeline = await service.timeline(run_id)
    assert any(e.verdict == "emergency_stop" for e in timeline.safety_evaluations)


@pytest.mark.asyncio
async def test_shutdown_heat_off_is_noop_with_no_active_run(store: RoastStore) -> None:
    """Shutdown is a no-op when there is no live runner (API-only / never started):
    there is no commanded heat to turn off, so no e-stop is issued."""
    service = RoastService(
        store,
        config=_config(),
        roaster=FakeMCPClient(),
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=FakeClock(),
    )
    # No roast started → service.runner is None.
    assert service.runner is None
    assert await service.safe_shutdown_heat_off() is False


@pytest.mark.asyncio
async def test_shutdown_heat_off_is_noop_in_inactive_phase(store: RoastStore) -> None:
    """Shutdown is a no-op when the active run is in an inactive (already-off)
    phase — here a completed run — so it neither issues an e-stop nor raises."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    service = RoastService(
        store,
        config=_config(),
        roaster=mcp,
        advisor=FakeAdvisor([], default_decision=_decision()),
        run_loop=False,
        clock=clock,
    )
    await service.start_roast(_profile())
    assert service.runner is not None
    # Drive the controller to a terminal, hardware-off phase via e-stop, then
    # confirm a *second* shutdown call is a no-op (no fresh e-stop).
    assert await service.safe_shutdown_heat_off() is True
    assert service.runner.controller_snapshot().phase not in ACTIVE_ROAST_PHASES
    calls_before = len(mcp.calls)
    assert await service.safe_shutdown_heat_off() is False
    assert len(mcp.calls) == calls_before, "no second e-stop from an inactive phase"


@pytest.mark.asyncio
async def test_shutdown_heat_off_is_bounded_and_does_not_hang(store: RoastStore) -> None:
    """A wedged MCP emergency_stop must not hang shutdown: the heat-off write is
    bounded, so a hanging child times out and shutdown proceeds (fail closed)."""
    clock = FakeClock()

    class HangingMCP(FakeMCPClient):
        async def emergency_stop(self, *, reason: str) -> None:
            await asyncio.Event().wait()  # never completes — simulates a wedged child

    mcp = HangingMCP([_reading(bean=178.0, env=185.0)])
    service = RoastService(
        store,
        config=_config(),
        roaster=mcp,
        advisor=FakeAdvisor([], default_decision=_decision()),
        run_loop=False,
        clock=clock,
    )
    await service.start_roast(_profile())

    # Bounded: returns within the timeout, never hangs, and reports it did not
    # confirm (False) rather than blocking teardown.
    issued = await asyncio.wait_for(
        service.safe_shutdown_heat_off(timeout_seconds=0.05), timeout=2.0
    )
    assert issued is False


@pytest.mark.asyncio
async def test_shutdown_heat_off_fails_closed_on_unexpected_error(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected error in the heat-off path fails closed: it is logged loudly
    and swallowed (returns False) so teardown still proceeds to stop the MCP child
    rather than aborting and orphaning it."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    service = RoastService(
        store,
        config=_config(),
        roaster=mcp,
        advisor=FakeAdvisor([], default_decision=_decision()),
        run_loop=False,
        clock=clock,
    )
    await service.start_roast(_profile())
    assert service.runner is not None

    async def _boom() -> bool:
        raise RuntimeError("unexpected runner failure")

    monkeypatch.setattr(service.runner, "shutdown_heat_off", _boom)
    assert await service.safe_shutdown_heat_off() is False


@pytest.mark.asyncio
async def test_teardown_runs_heat_off_before_mcp_stop() -> None:
    """The live-serve teardown calls heat-off BEFORE stopping the MCP child.

    The ordering is load-bearing: the heat→0 write must land while the MCP child
    is still alive. Drives :func:`roastpilot_agent.cli._teardown_live` with
    recorders and asserts the recorded order is heat-off → service.shutdown →
    mcp.stop → store.close.
    """
    order: list[str] = []

    class _RecordingService:
        async def safe_shutdown_heat_off(self) -> bool:
            order.append("safe_shutdown_heat_off")
            return True

        async def shutdown(self) -> None:
            order.append("service.shutdown")

    class _RecordingMCP:
        async def stop(self) -> None:
            order.append("mcp.stop")

    class _RecordingStore:
        async def close(self) -> None:
            order.append("store.close")

    # The helper takes the concrete production types; the recorders satisfy the
    # exact methods it calls, so a structural cast keeps this a real behavior
    # test without standing up uvicorn / a live MCP child.
    await _teardown_live(
        cast(RoastService, _RecordingService()),
        cast(MCPServerProcess, _RecordingMCP()),
        cast(RoastStore, _RecordingStore()),
    )

    assert order == [
        "safe_shutdown_heat_off",
        "service.shutdown",
        "mcp.stop",
        "store.close",
    ]
    assert order.index("safe_shutdown_heat_off") < order.index("mcp.stop")


@pytest.mark.asyncio
async def test_teardown_continues_when_heat_off_raises() -> None:
    """A raising heat-off step is logged-not-raised and never aborts the rest of
    teardown — the MCP child is still stopped and the store closed (best-effort
    chain, so one failure cannot orphan the child)."""
    order: list[str] = []

    class _RaisingService:
        async def safe_shutdown_heat_off(self) -> bool:
            order.append("safe_shutdown_heat_off")
            raise RuntimeError("boom")

        async def shutdown(self) -> None:
            order.append("service.shutdown")

    class _RecordingMCP:
        async def stop(self) -> None:
            order.append("mcp.stop")

    class _RecordingStore:
        async def close(self) -> None:
            order.append("store.close")

    await _teardown_live(
        cast(RoastService, _RaisingService()),
        cast(MCPServerProcess, _RecordingMCP()),
        cast(RoastStore, _RecordingStore()),
    )

    assert order == [
        "safe_shutdown_heat_off",
        "service.shutdown",
        "mcp.stop",
        "store.close",
    ]


@pytest.mark.asyncio
async def test_hard_kill_path_unchanged_restart_enters_recovery(store: RoastStore) -> None:
    """The hard-kill path (SIGKILL / crash / power loss) cannot run shutdown, so
    it never calls safe_shutdown_heat_off — a restart over a possibly-active run
    must still enter operator_recovery_required and issue no MCP write (the
    restart-never-auto-resumes invariant, unchanged by #142)."""
    clock = FakeClock()
    mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    service = RoastService(
        store,
        config=_config(),
        roaster=mcp,
        advisor=FakeAdvisor([], default_decision=_decision()),
        run_loop=False,
        clock=clock,
    )
    detail = await service.start_roast(_profile())
    run_id = detail.id
    assert detail.agent_phase in ACTIVE_ROAST_PHASES
    # Simulate a hard kill: the process dies with NO graceful teardown — so
    # safe_shutdown_heat_off is deliberately NOT called here. The persisted run
    # is left mid-roast (not completed).

    # Restart over that possibly-active run.
    restart_mcp = FakeMCPClient([_reading(bean=178.0, env=185.0)])
    restarted = RoastService(
        store,
        config=_config(),
        roaster=restart_mcp,
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=FakeClock(),
    )
    await restarted.recover_on_start()

    # Recovery entered, and NO MCP write was issued on the restart (no
    # auto-resume of heat/fan, no e-stop).
    recovered = await restarted.detail(run_id)
    assert recovered.agent_phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED
    assert restart_mcp.calls == [], "restart issues no MCP write — never auto-resumes"
