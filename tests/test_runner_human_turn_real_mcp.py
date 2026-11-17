"""HUMAN emergency routing through the installed producer's mock stdio child."""

import asyncio
import os
import sys
from collections.abc import Awaitable
from importlib.metadata import version
from pathlib import Path
from typing import Literal

import pytest

from roastpilot_agent.api import RoastRunner, RoastService
from roastpilot_agent.config import DEFAULT_MCP_COMMAND, AppConfig, ControllerConfig, MCPConfig
from roastpilot_agent.human_persistence import Settlement
from roastpilot_agent.human_protection import (
    ControlAuthority,
    CounterClass,
    CounterEvidence,
    ReadingFreshness,
)
from roastpilot_agent.mcp_client import (
    EventCommandResult,
    FaultControlCommandResult,
    MCPServerProcess,
    MCPToolError,
    RoasterControlAdapter,
    resolve_mcp_command,
)
from roastpilot_agent.models import (
    FaultAcknowledgementState,
    FaultControlsActionRequest,
    FaultControlsActionResult,
    OperatorAction,
    OperatorActionRequest,
    OperatorActionResult,
    RoastCommand,
    RoastEventKind,
    RoastPhase,
)
from roastpilot_agent.safety import SafetyEvaluation
from roastpilot_agent.store import RoastStore
from tests.conftest import FakeClock
from tests.test_controller import PROFILE

_MCP_COMMAND = Path(resolve_mcp_command(DEFAULT_MCP_COMMAND))
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.slow,
    pytest.mark.serial(reason="owns a real mock MCP stdio child"),
    pytest.mark.skipif(
        not (
            _MCP_COMMAND.is_file()
            and os.access(_MCP_COMMAND, os.X_OK)
            and _MCP_COMMAND.parent.resolve() == Path(sys.executable).parent.resolve()
        ),
        reason="requires the interpreter-local coffee-roaster-mcp executable",
    ),
]


@pytest.mark.parametrize(
    ("route", "interval", "negative", "fail_audit"),
    [
        pytest.param("global", "barrier", False, False, id="RS1"),
        pytest.param("run", "barrier", False, False, id="RS2"),
        pytest.param("global", "proof", False, False, id="RS3"),
        pytest.param("run", "proof", False, False, id="RS4"),
        pytest.param("global", "barrier", True, False, id="RS5"),
        pytest.param("global", "barrier", False, True, id="RS6"),
    ],
)
async def test_human_estop_during_finalisation_over_real_stdio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    route: str,
    interval: str,
    negative: bool,
    fail_audit: bool,
) -> None:
    """Bind the service result to producer command admission and fresh readback."""
    versions = {name: version(name) for name in ("coffee-roaster-mcp", "mcp", "pydantic")}
    print(versions)
    assert versions["coffee-roaster-mcp"] == "0.2.4"
    # The producer applies COFFEE_* environment overrides after loading YAML.
    for name in tuple(os.environ):
        if name.startswith("COFFEE_"):
            monkeypatch.delenv(name)
    producer_config = tmp_path / "mock.yaml"
    producer_config.write_text(
        "roaster:\n  driver: mock\n"
        "first_crack:\n  mode: disabled\n"
        "recording:\n  enabled: false\n"
        f"logging:\n  log_dir: '{tmp_path / 'logs'}'\n",
        encoding="utf-8",
    )
    child = MCPServerProcess(MCPConfig(env={"COFFEE_ROASTER_MCP_CONFIG": str(producer_config)}))
    store = RoastStore(tmp_path / "human-stdio.sqlite3")
    await store.initialize()
    try:
        await child.start()
        try:
            await _exercise_route(
                store,
                child,
                monkeypatch,
                caplog,
                route,
                interval,
                negative,
                fail_audit,
                producer_config,
            )
        finally:
            await child.stop()
    finally:
        await store.close()


async def _exercise_route(
    store: RoastStore,
    child: MCPServerProcess,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    route: str,
    interval: str,
    negative: bool,
    fail_audit: bool,
    producer_config: Path,
) -> None:
    clock = FakeClock()
    adapter = RoasterControlAdapter.from_process(child, clock=clock)
    client = adapter._client  # pyright: ignore[reportPrivateUsage]
    info = await client.get_server_info()
    assert info.bootstrap_safe is True and info.roaster_driver == "mock"
    assert info.first_crack_mode == "disabled"
    runtime = await client.get_runtime_config()
    assert runtime.roaster_driver == "mock" and runtime.first_crack_mode == "disabled"
    assert runtime.config_source is not None
    assert Path(runtime.config_source).resolve() == producer_config.resolve()
    assert Path(runtime.log_dir).resolve() == (producer_config.parent / "logs").resolve()
    service = RoastService(
        store,
        mcp=child,
        roaster=adapter,
        raw_state=adapter,
        authority=ControlAuthority.HUMAN,
        config=AppConfig(controller=ControllerConfig(t0_debounce_ticks=1)),
        run_loop=False,
        clock=clock,
    )
    run = await service.start_roast(PROFILE)
    runner = service.runner
    assert runner is not None
    controller = runner.controller_for_human_fault_drop()
    executor = service._fault_control_executor  # pyright: ignore[reportPrivateUsage]
    assert controller is not None and executor is not None

    # Charge and stop the actual mock producer before starting the service race.
    clock.advance(0.25)
    await runner.tick_once()
    await client.mark_beans_added()
    clock.advance(0.25)
    await runner.tick_once()
    await client.emergency_stop("establish mock stopped fault")
    session = adapter.latest_fault_session_id
    assert session is not None
    before = await client.get_roast_state(session)
    assert not before.active and before.phase == "fault" and before.cooling_on
    assert before.beans_added_at_utc is not None
    assert before.first_crack_status.mode == "disabled"
    assert not before.first_crack_status.audio_running
    controller.transition_to(RoastPhase.FAULTED)
    clock.advance(0.25)
    await runner.tick_once()
    tracker = controller._accepted_readings  # pyright: ignore[reportPrivateUsage]
    assert tracker.project_freshness() is ReadingFreshness.UNPROVEN
    assert not controller.human_recovery_drop_allowed(session_id=session)
    assert controller._known_human_identity() == (session, child.child_epoch)  # pyright: ignore[reportPrivateUsage]
    # Mock stdio emits real temperatures and session identity, but it has no
    # Hottop packet counters. Seed only the consumer's tracker so the real
    # policy can register the DROP whose evaluation is held for this race.
    for ordinal, expected in (
        (1, ReadingFreshness.UNPROVEN),
        (2, ReadingFreshness.FRESH),
    ):
        clock.advance(0.125)
        evidence = await adapter.read_human_evidence()
        assert evidence.counters.class_ is CounterClass.ABSENT
        assert evidence.bound_child_epoch is not None
        assert evidence.bound_child_epoch == child.child_epoch
        assert evidence.bean_temp_c is not None and evidence.env_temp_c is not None
        assert (
            tracker.observe(
                child_epoch=evidence.bound_child_epoch,
                counters=CounterEvidence(ordinal, 0, CounterClass.PRESENT),
                bean_temp_c=evidence.bean_temp_c,
                env_temp_c=evidence.env_temp_c,
                request_admitted_at=evidence.request_admitted_at,
            )
            is expected
        )
    assert controller.human_recovery_drop_allowed(session_id=session)
    lease = await store.open_fault_control_lease(run_id=run.id, mcp_session_id=session)

    evaluation_held, release_evaluation = asyncio.Event(), asyncio.Event()
    reserved, release_proof = asyncio.Event(), asyncio.Event()
    confirmed_before_release = False
    exhaust_proof_history_budget = interval == "proof" and route == "global"
    completion_left_before_release = False
    live_acknowledgements: list[object] = []
    emitter = runner._emitter  # pyright: ignore[reportPrivateUsage]
    original_emit = emitter.emit
    registered_evaluations = controller._human_drop_evaluation_tasks  # pyright: ignore[reportPrivateUsage]
    strict_results: list[FaultControlCommandResult] = []
    unbound_errors: list[MCPToolError] = []
    unbound_attempts: list[str] = []
    strict_tools: list[str] = []
    completion_writes: list[str] = []
    audit_failures = 0
    original_evaluation = store.record_safety_evaluation
    original_audit = store.record_command
    original_read_ack = adapter.read_fault_acknowledgement_state
    original_settle = controller.settle_human_drop_evaluations
    original_strict = client._execute_fault_control  # pyright: ignore[reportPrivateUsage]
    original_emergency = client.emergency_stop

    def record_live(kind: RoastEventKind, payload: object) -> None:
        if kind is RoastEventKind.FAULT_ACKNOWLEDGEMENT_EXECUTED:
            live_acknowledgements.append(payload)
        original_emit(kind, payload)

    async def record(*, run_id: str, tick: int, evaluation: SafetyEvaluation) -> int:
        if evaluation.rule == "human_recovery_drop":
            evaluation_held.set()
            await release_evaluation.wait()
        return await original_evaluation(run_id=run_id, tick=tick, evaluation=evaluation)

    async def record_command(
        *,
        run_id: str,
        tick: int,
        tool: RoastCommand,
        source: Literal["policy", "advisor", "operator", "safety", "recovery"],
        status: Literal["ok", "failed"],
        args: object = None,
        result: object = None,
        safety_evaluation_id: int | None = None,
    ) -> None:
        nonlocal audit_failures
        if fail_audit and tool is RoastCommand.EMERGENCY_STOP:
            audit_failures += 1
            raise RuntimeError("synthetic real-store emergency command audit failure")
        await original_audit(
            run_id=run_id,
            tick=tick,
            tool=tool,
            source=source,
            status=status,
            args=args,
            result=result,
            safety_evaluation_id=safety_evaluation_id,
        )

    async def read_ack(session_id: str) -> FaultAcknowledgementState:
        if interval == "proof" and not reserved.is_set():
            reserved.set()
            await release_proof.wait()
        return await original_read_ack(session_id)

    async def settle() -> bool:
        if interval == "barrier":
            reserved.set()
        return await original_settle()

    async def strict(
        name: Literal["emergency_stop", "start_cooling", "stop_cooling", "drop_beans"],
        arguments: dict[str, object],
    ) -> FaultControlCommandResult:
        strict_tools.append(name)
        result = await original_strict(name, arguments)
        if name == "emergency_stop":
            assert any(not evaluation.done() for evaluation in registered_evaluations)
            assert arguments["expected_session_id"] == session
            strict_results.append(result)
        return result

    async def unbound(
        reason: str = "manual emergency stop", *, expected_session_id: str | None = None
    ) -> EventCommandResult:
        assert expected_session_id is None
        unbound_attempts.append(reason)
        try:
            return await original_emergency(reason, expected_session_id=expected_session_id)
        except MCPToolError as error:
            unbound_errors.append(error)
            raise

    async def forbid_completion(*_args: object, **_kwargs: object) -> None:
        completion_writes.append("completion")

    monkeypatch.setattr(emitter, "emit", record_live)
    monkeypatch.setattr(store, "record_safety_evaluation", record)
    monkeypatch.setattr(store, "record_command", record_command)
    monkeypatch.setattr(adapter, "read_fault_acknowledgement_state", read_ack)
    monkeypatch.setattr(controller, "settle_human_drop_evaluations", settle)
    monkeypatch.setattr(client, "_execute_fault_control", strict)
    monkeypatch.setattr(client, "emergency_stop", unbound)
    monkeypatch.setattr(runner, "_fault_lease_close", forbid_completion)
    monkeypatch.setattr(runner, "_export_logs", forbid_completion)
    monkeypatch.setattr(store, "complete_run", forbid_completion)

    async def stop_global() -> FaultControlsActionResult:
        request = FaultControlsActionRequest(action=OperatorAction.EMERGENCY_STOP)
        if not negative:
            return await service.submit_fault_controls_action(request)

        # Select the original AM-5 direct route for this qualifying request.
        # No other request uses these routing substitutes.
        def direct_route_only(_selected: RoastRunner) -> bool:
            return False

        with monkeypatch.context() as direct_route:
            assert runner.emergency_stop_must_bypass_terminal_writer() is True
            direct_route.setattr(service, "_live_estop_bypasses_terminal_writer", direct_route_only)
            direct_route.setattr(service, "_fault_control_executor", None)
            return await service.submit_fault_controls_action(request)

    drop = asyncio.create_task(
        service.submit_operator_action(
            run.id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
        )
    )
    completion: asyncio.Task[bool] | None = None
    try:
        await asyncio.wait_for(evaluation_held.wait(), 4)
        if interval == "barrier":
            # Through the real producer, establish the dropped-beans state
            # D212 requires to stop cooling. The service DROP is held before
            # dispatch and will fail after emergency supersession.
            staged_drop = await client.drop_beans(expected_session_id=session)
            assert staged_drop.event.kind == "beans_dropped"
            assert staged_drop.event.payload.get("recovery_after_fault") is True
            ready = await client.get_roast_state(session)
            assert not ready.active and ready.phase == "fault" and ready.cooling_on
        acknowledgement = await service.submit_operator_action(
            run.id,
            OperatorActionRequest(
                action=OperatorAction.STOP_COOLING_AND_ACKNOWLEDGE,
                payload={"confirmation": True, "expected_generation": lease.generation},
            ),
        )
        assert acknowledgement.result == "accepted" and acknowledgement.queued
        completion = asyncio.create_task(runner.tick_once())
        await asyncio.wait_for(reserved.wait(), 4)
        ready = await client.get_roast_state(session)
        assert ready.cooling_on is (interval == "proof")
        if route == "global":
            stopped = await asyncio.wait_for(stop_global(), 4)
        else:
            stopped = await asyncio.wait_for(
                service.submit_operator_action(
                    run.id, OperatorActionRequest(action=OperatorAction.EMERGENCY_STOP)
                ),
                4,
            )
        if fail_audit:
            # Let the bounded completion waiter expire while its precise
            # evaluation owner remains held. The real e-stop has already run.
            assert not await asyncio.wait_for(asyncio.shield(completion), 3)
        completion_left_before_release = completion.done()
        if completion_left_before_release:
            assert completion.result() is False
        confirmed_before_release = not drop.done() and any(
            not evaluation.done() for evaluation in registered_evaluations
        )
        if exhaust_proof_history_budget:
            clock.advance(1.0)
    finally:
        release_evaluation.set()
        release_proof.set()
        try:
            pending: list[Awaitable[object]] = [drop] if completion is None else [drop, completion]
            outcomes = await asyncio.wait_for(asyncio.gather(*pending), 4)
        finally:
            try:
                assert await service.settle_fault_lease_persistence_for_teardown()
            finally:
                assert await executor.settle_for_shutdown()

    assert confirmed_before_release
    dropped, completed = outcomes
    assert isinstance(dropped, OperatorActionResult) and dropped.result == "failed"
    assert completed is False and completion_writes == []
    receipts = store.drain_human_storage_receipts()
    if completion_left_before_release:
        assert any(
            r.kind == "terminal_evaluation_settlement"
            and r.run_id == run.id
            and r.turn == runner.current_tick
            and r.settlement is Settlement.VERIFIED_NO_WRITE
            for r in receipts
        )
    assert "drop_beans" not in strict_tools
    after = await client.get_roast_state(session)
    before_fault_count = sum(event.kind == "fault" for event in before.events)
    after_fault_count = sum(event.kind == "fault" for event in after.events)
    if negative:
        assert strict_results == []
        assert len(unbound_attempts) == 1
        assert len(unbound_errors) == 1 and "No active roast session exists." in str(
            unbound_errors[0]
        )
        assert after_fault_count == before_fault_count
        assert stopped.result == "accepted"
        assert stopped.reason == "emergency_stop accepted for controller priority dispatch"
    else:
        assert stopped.result == ("confirmed" if route == "global" else "accepted")
        assert stopped.reason == "terminal emergency stop has fresh exact-session proof"
        assert len(strict_results) == 1 and unbound_attempts == []
        (result,) = strict_results
        assert result.session_id == session and result.phase == "fault"
        assert result.event.kind == "fault"
        assert result.event.payload["driver_safety_method_called"] is True
        assert result.event.payload.get("driver_error") is None
        assert after_fault_count == before_fault_count + 1
        assert after.heat_level_percent == 0 and after.cooling_on
        assert not controller._pending_fail_safe_is_emergency_stop_retry  # pyright: ignore[reportPrivateUsage]
    if interval == "proof":
        assert "stop_cooling" not in strict_tools
        timeline = await store.read_timeline(run.id)
        assert {
            "outcome": "failed",
            "reason": "superseded_by_emergency_stop",
        } in live_acknowledgements
        durable_supersession = any(
            event.kind is RoastEventKind.FAULT_ACKNOWLEDGEMENT_EXECUTED
            and event.payload is not None
            and event.payload.get("reason") == "superseded_by_emergency_stop"
            for event in timeline.events
        )
        if exhaust_proof_history_budget:
            assert not durable_supersession
        if not durable_supersession:
            assert any(
                r.kind == "record_event"
                and r.detail == RoastEventKind.FAULT_ACKNOWLEDGEMENT_EXECUTED.value
                and r.run_id == run.id
                and r.turn == runner.current_tick
                and r.settlement is Settlement.VERIFIED_NO_WRITE
                and r.writer_entered is False
                for r in receipts
            )
    if fail_audit:
        assert audit_failures == 1
        assert "could not persist fault-control command outcome" in caplog.messages
        (missing,) = [
            r
            for r in receipts
            if r.kind == "record_command" and r.detail == RoastCommand.EMERGENCY_STOP.value
        ]
        assert missing.run_id == run.id and missing.turn is None
        assert missing.settlement is Settlement.UNRESOLVED and missing.writer_entered is None
        assert missing.command is not None
        assert missing.command.session_id == session
        assert missing.command.child_generation == child.child_epoch
        assert missing.command.command_generation == controller.command_generation
