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
    AppliedRoasterState,
    RoastEventKind,
    RoastEventSource,
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
async def test_shutdown_heat_off_is_bounded_and_persists_unconfirmed_marker(
    store: RoastStore,
) -> None:
    """A persistently-wedged MCP emergency_stop must not hang shutdown: the
    heat-off write is bounded, retried once (#177), and on a second timeout it
    returns False AND persists a 'shutdown unconfirmed' marker to the trace so
    post-roast it is unambiguous the commanded stop went unacknowledged."""
    clock = FakeClock()

    class HangingMCP(FakeMCPClient):
        async def emergency_stop(self, *, reason: str) -> AppliedRoasterState | None:
            await asyncio.Event().wait()  # never completes — simulates a wedged child
            raise AssertionError("unreachable — the wait above never returns")

    mcp = HangingMCP([_reading(bean=178.0, env=185.0)])
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

    # Bounded: returns within the (doubled, retried) timeout, never hangs, and
    # reports it did not confirm (False) rather than blocking teardown.
    issued = await asyncio.wait_for(
        service.safe_shutdown_heat_off(timeout_seconds=0.05), timeout=2.0
    )
    assert issued is False

    # The give-up persisted an unambiguous trace marker (reused COMMAND_FAILED
    # kind, so no new SSE event kind / FE-contract change).
    timeline = await service.timeline(run_id)
    markers = [
        e
        for e in timeline.events
        if e.kind is RoastEventKind.COMMAND_FAILED
        and e.payload is not None
        and e.payload.get("command") == "shutdown_heat_off"
    ]
    assert len(markers) == 1, "exactly one shutdown-unconfirmed marker after give-up"
    marker = markers[0]
    assert marker.source is RoastEventSource.SAFETY
    assert marker.payload is not None
    assert marker.payload["unconfirmed"] is True
    assert marker.payload["context"] == "shutdown"
    assert marker.payload["reason"] == "timeout"


@pytest.mark.asyncio
async def test_shutdown_heat_off_retry_recovers_without_marker(store: RoastStore) -> None:
    """A TRANSIENT wedge — the first emergency_stop hangs, the retry succeeds —
    confirms heat-off on the retry and persists NO unconfirmed marker (#177):
    the marker is only for a genuine give-up, not a recovered retry."""
    clock = FakeClock()

    class TransientHangMCP(FakeMCPClient):
        def __init__(self, frames: list[RoastTelemetry | None | Exception]) -> None:
            super().__init__(frames)
            self._estop_attempts = 0

        async def emergency_stop(self, *, reason: str) -> AppliedRoasterState | None:
            self._estop_attempts += 1
            if self._estop_attempts == 1:
                await asyncio.Event().wait()  # first attempt wedges → times out
            return await super().emergency_stop(reason=reason)  # retry confirms

    mcp = TransientHangMCP([_reading(bean=178.0, env=185.0)])
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

    # timeout_seconds=1.0: large enough that the genuine-hang first attempt
    # still times out (asyncio.Event().wait() never returns), while the retry
    # (pure in-process FakeMCPClient call) comfortably completes within the
    # same 1.0 s window even under heavy CI scheduler load.  The previous
    # 0.05 s was too tight for the retry's SQLite store writes on a loaded
    # runner, causing intermittent failures (#399).
    issued = await asyncio.wait_for(
        service.safe_shutdown_heat_off(timeout_seconds=1.0), timeout=10.0
    )
    assert issued is True, "retry confirmed the heat-off"
    assert "emergency_stop" in mcp.commands()
    assert service.runner is not None
    assert service.runner.controller_snapshot().phase is RoastPhase.FAULTED

    # No give-up marker — the retry recovered.
    timeline = await service.timeline(run_id)
    assert not [
        e
        for e in timeline.events
        if e.kind is RoastEventKind.COMMAND_FAILED
        and e.payload is not None
        and e.payload.get("command") == "shutdown_heat_off"
    ], "a recovered retry must NOT leave an unconfirmed marker"


@pytest.mark.asyncio
async def test_shutdown_heat_off_fails_closed_on_unexpected_error(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistent unexpected error in the heat-off path fails closed: it is
    retried once (#177), logged loudly, swallowed (returns False) so teardown
    still stops the MCP child, and the give-up persists an unconfirmed marker
    stamped ``reason="error"``."""
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
    assert service.runner is not None

    attempts = 0

    async def _boom() -> bool:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("unexpected runner failure")

    monkeypatch.setattr(service.runner, "shutdown_heat_off", _boom)
    assert await service.safe_shutdown_heat_off() is False
    assert attempts == 2, "the heat-off write is retried once before giving up"

    timeline = await service.timeline(run_id)
    markers = [
        e
        for e in timeline.events
        if e.kind is RoastEventKind.COMMAND_FAILED
        and e.payload is not None
        and e.payload.get("command") == "shutdown_heat_off"
    ]
    assert len(markers) == 1
    assert markers[0].payload is not None
    assert markers[0].payload["reason"] == "error"


@pytest.mark.asyncio
async def test_record_shutdown_unconfirmed_swallows_store_error(
    store: RoastStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker write is fail-safe: a store error while persisting it is logged
    and swallowed, never raised, so it cannot block the rest of teardown (#177)."""
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

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("store write failed")

    monkeypatch.setattr(store, "record_event", _boom)
    # Must not raise even though the underlying store write blows up.
    await service.runner.record_shutdown_unconfirmed(command="shutdown_heat_off", reason="timeout")


@pytest.mark.asyncio
async def test_record_child_stop_unconfirmed_persists_marker_when_force_killed(
    store: RoastStore,
) -> None:
    """When the MCP child stop went unconfirmed (force-killed), the teardown
    step persists an ``mcp_stop`` unconfirmed marker a recovery read can see (#177)."""
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

    await service.record_child_stop_unconfirmed(stop_unconfirmed=True)

    timeline = await service.timeline(run_id)
    markers = [
        e
        for e in timeline.events
        if e.kind is RoastEventKind.COMMAND_FAILED
        and e.payload is not None
        and e.payload.get("command") == "mcp_stop"
    ]
    assert len(markers) == 1
    marker = markers[0]
    assert marker.source is RoastEventSource.SAFETY
    assert marker.payload is not None
    assert marker.payload["unconfirmed"] is True
    assert marker.payload["context"] == "shutdown"
    assert marker.payload["reason"] == "child_stop_unconfirmed"


@pytest.mark.asyncio
async def test_record_child_stop_unconfirmed_is_noop_on_clean_stop(store: RoastStore) -> None:
    """A clean child stop (stop_unconfirmed False) records NO marker (#177)."""
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

    await service.record_child_stop_unconfirmed(stop_unconfirmed=False)

    timeline = await service.timeline(run_id)
    assert not [
        e
        for e in timeline.events
        if e.kind is RoastEventKind.COMMAND_FAILED
        and e.payload is not None
        and e.payload.get("command") == "mcp_stop"
    ]


@pytest.mark.asyncio
async def test_record_child_stop_unconfirmed_is_noop_without_runner(store: RoastStore) -> None:
    """No live runner (API-only / never started) → the marker step is a safe
    no-op even when the child stop was unconfirmed (nothing to key it to)."""
    service = RoastService(
        store,
        config=_config(),
        roaster=FakeMCPClient(),
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=FakeClock(),
    )
    assert service.runner is None
    # Must not raise.
    await service.record_child_stop_unconfirmed(stop_unconfirmed=True)


@pytest.mark.asyncio
async def test_teardown_runs_heat_off_before_mcp_stop() -> None:
    """The live-serve teardown calls heat-off BEFORE stopping the MCP child.

    The ordering is load-bearing: the heat→0 write must land while the MCP child
    is still alive, and the child-stop-unconfirmed marker (#177) must be recorded
    after ``mcp.stop`` (so the verdict is known) but before ``store.close`` (so
    it can be written). Drives :func:`roastpilot_agent.cli._teardown_live` with
    recorders and asserts the recorded order is heat-off → service.shutdown →
    mcp.stop → record_child_stop_unconfirmed → store.close.
    """
    order: list[str] = []

    class _RecordingService:
        async def safe_shutdown_heat_off(self) -> bool:
            order.append("safe_shutdown_heat_off")
            return True

        async def shutdown(self) -> None:
            order.append("service.shutdown")

        async def record_child_stop_unconfirmed(self, *, stop_unconfirmed: bool) -> None:
            # The marker step reads mcp.stop_unconfirmed, set True by the recorder
            # below; assert the verdict reached it.
            order.append(f"record_child_stop_unconfirmed:{stop_unconfirmed}")

    class _RecordingMCP:
        stop_unconfirmed = True  # the child stop went unconfirmed this teardown

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
        "record_child_stop_unconfirmed:True",
        "store.close",
    ]
    assert order.index("safe_shutdown_heat_off") < order.index("mcp.stop")
    # The marker step sees the verdict AFTER mcp.stop and writes BEFORE store.close.
    assert order.index("mcp.stop") < order.index("record_child_stop_unconfirmed:True")
    assert order.index("record_child_stop_unconfirmed:True") < order.index("store.close")


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

        async def record_child_stop_unconfirmed(self, *, stop_unconfirmed: bool) -> None:
            order.append("record_child_stop_unconfirmed")

    class _RecordingMCP:
        stop_unconfirmed = False  # a clean child stop this teardown

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
        "record_child_stop_unconfirmed",
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
