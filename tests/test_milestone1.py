"""E9 vertical slice — the 12-step mock milestone (component plan §8;
orchestration plan § First Milestone).

A full roast driven end to end through the live ``RoastService`` +
``RoastRunner`` wiring: service start → roast start → streamed state → mock
auto-T0 → first crack → one advisory decision through the fake adapter →
safety validation → heat/fan command → drop → stop cooling → log export →
restart proves the completed state recoverable. Hardware-free: a fake MCP
client supplies telemetry frames and records writes, a deterministic
``FakeAdvisor`` advises, and a temp SQLite store persists the decision trace.

E9-S1 runs this against the fake MCP client (here). E9-S2 runs the same flow
against the real ``coffee-roaster-mcp`` subprocess in mock-driver mode.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from roastpilot_agent.advisor import FakeAdvisor, RoastDecision
from roastpilot_agent.api import RoastService
from roastpilot_agent.config import AppConfig, ControllerConfig, PostFirstCrackControl
from roastpilot_agent.mcp_client import ExportRoastLogResult
from roastpilot_agent.models import (
    OperatorAction,
    OperatorActionRequest,
    RoastEventKind,
    RoastPhase,
    RoastProfile,
    RoastTelemetry,
    SseEvent,
    SseEventType,
)
from roastpilot_agent.store import RoastStore
from tests.conftest import FakeClock, FakeMCPClient

#: This module's tests are advisor-driven vertical slices (a FakeAdvisor
#: returns one decision, checked via the persisted advisor_decisions row) —
#: they predate the 12 Jul D88/D89 promotion (#495) and were never meant to
#: exercise the deterministic post-FC taper or the ceiling-guard auto-drop.
#: Both flags now default True; several of these fixtures read bean=196.0 (at
#: the default ceiling-guard temperature), which would auto-drop the roast
#: before the advisor consult these tests are actually about ever runs.
#: Pinned to the pre-promotion baseline explicitly.
_BASELINE_POST_FC_CONTROL = PostFirstCrackControl(enabled=False, ceiling_guard_drop_enabled=False)


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


def _decision(heat: int = 55, fan: int = 45, drop: bool = False) -> RoastDecision:
    return RoastDecision(
        target_heat=heat,
        target_fan=fan,
        should_drop=drop,
        confidence=0.9,
        rationale="hold targets",
    )


def _export_result(tmp_path: Path) -> ExportRoastLogResult:
    log_dir = tmp_path / "export"
    log_dir.mkdir()
    paths = {name: log_dir / f"roast.{name}" for name in ("jsonl", "csv", "summary")}
    for path in paths.values():
        path.write_text("{}\n", encoding="utf-8")
    return ExportRoastLogResult(
        session_id="mock-session",
        log_dir=str(log_dir),
        jsonl_path=str(paths["jsonl"]),
        csv_path=str(paths["csv"]),
        summary_path=str(paths["summary"]),
        ready=True,
        note="mock export",
    )


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[RoastStore]:
    instance = RoastStore(tmp_path / "milestone.sqlite3")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


def _drain(queue: "asyncio.Queue[SseEvent]") -> list[SseEvent]:
    """Non-blocking drain of a broadcaster subscriber queue."""
    events: list[SseEvent] = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return events


@pytest.mark.asyncio
async def test_twelve_step_vertical_slice_against_fake_mcp(
    store: RoastStore, tmp_path: Path
) -> None:
    """The 12-step milestone, end to end, against the fake MCP client."""
    clock = FakeClock()
    # A small telemetry-log interval so each advanced tick persists a row;
    # everything else default (the conservative safety + timing config).
    config = AppConfig(
        controller=ControllerConfig(
            telemetry_log_interval_seconds=1.0,
            post_first_crack_control=_BASELINE_POST_FC_CONTROL,
        )
    )
    preheat = _reading(bean=178.0, env=185.0)  # inside the 170–200 charge band
    mcp = FakeMCPClient([preheat], export_result=_export_result(tmp_path))
    advisor = FakeAdvisor([], default_decision=_decision())

    # Step 1: start the agent service (with the live roaster + advisor wired).
    service = RoastService(
        store,
        config=config,
        roaster=mcp,
        advisor=advisor,
        exporter=mcp,
        run_loop=False,  # the test drives ticks deterministically
        clock=clock,
    )
    subscriber = service.events.subscribe()

    async def tick() -> bool:
        assert service.runner is not None
        clock.advance(3.0)  # clear the 2 s command rate limit + telemetry throttle
        return await service.runner.tick_once()

    # Step 2: start a roast (mock). The run starts and reaches preheating.
    detail = await service.start_roast(_profile())
    run_id = detail.id
    assert detail.agent_phase is RoastPhase.PREHEATING
    assert (await store.active_run()) is not None

    # Step 3: stream state — a per-tick telemetry frame reaches SSE subscribers.
    await tick()
    frames = _drain(subscriber)
    assert any(frame.event is SseEventType.TELEMETRY for frame in frames)

    # Step 4: simulate T0 via mock automatic detection (debounced).
    mcp.frames = [_reading(bean=95.0, env=150.0, t0_detected=True)]
    for _ in range(config.controller.t0_debounce_ticks):
        await tick()
    assert (await service.detail(run_id)).agent_phase is RoastPhase.ROASTING_PRE_FIRST_CRACK

    # Step 5: simulate first crack via mock status.
    mcp.frames = [_reading(bean=196.0, env=205.0, t0_detected=True, first_crack_detected=True)]
    await tick()
    assert (await service.detail(run_id)).agent_phase is RoastPhase.DEVELOPMENT

    # Steps 6–8: an advisory decision runs through the fake adapter, is validated
    # by safety policy, and a heat/fan command is executed.
    mcp.frames = [_reading(bean=205.0, env=210.0, t0_detected=True, first_crack_detected=True)]
    await tick()
    timeline = await service.timeline(run_id)
    assert any(e.kind is RoastEventKind.ADVISORY for e in timeline.events)
    assert any(e.kind is RoastEventKind.COMMAND_EXECUTED for e in timeline.events)
    assert timeline.safety_evaluations, "safety verdicts persisted"
    assert "set_targets" in mcp.commands()

    # Step 9: drop the beans — an operator action through the queue and full
    # safety policy on drain.
    result = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.DROP_BEANS)
    )
    assert result.result == "accepted" and result.queued
    await tick()
    assert (await service.detail(run_id)).agent_phase is RoastPhase.COOLING
    assert "drop_beans" in mcp.commands()

    # Step 10: stop cooling (operator) — completes the run.
    result = await service.submit_operator_action(
        run_id, OperatorActionRequest(action=OperatorAction.STOP_COOLING)
    )
    assert result.result == "accepted"
    finalized = await tick()
    assert finalized

    # Step 11: logs are exported and the manifest persisted.
    completed = await service.detail(run_id)
    assert completed.agent_phase is RoastPhase.COMPLETE
    assert completed.outcome == "completed"
    assert completed.export_manifest is not None
    assert "export_roast_log" in mcp.commands()
    manifest = await service.log_manifest(run_id)
    assert manifest.ready

    # Decision trace (advisory → verdict → command) is persisted and readable
    # via the timeline route.
    timeline = await service.timeline(run_id)
    assert any(e.kind is RoastEventKind.ADVISORY for e in timeline.events), "advisory"
    assert timeline.safety_evaluations, "verdict"
    assert any(e.kind is RoastEventKind.COMMAND_EXECUTED for e in timeline.events), (
        "command in events"
    )
    assert timeline.commands, "operator commands in command_log"
    # The advisor decision trace is persisted (#167): the fake-advisor 'ok'
    # decision wrote an advisor_decisions row carrying the decision and linked
    # to the safety verdict it produced. An empty table here is the original
    # bug — record_advisor_decision had zero call sites.
    assert timeline.advisor_decisions, "advisor decision trace persisted"
    ok_rows = [a for a in timeline.advisor_decisions if a.status == "ok"]
    assert ok_rows, "an ok advisor decision was persisted"
    ok = ok_rows[0]
    assert ok.decision is not None, "the RoastDecision is recorded"
    assert ok.provider == "fake" and ok.model == "fake-model"
    assert ok.safety_evaluation_id is not None, "linked to its safety verdict"
    linked_ids = {s.tick for s in timeline.safety_evaluations}
    assert linked_ids, "safety verdicts exist to link to"
    event_kinds = {e.kind for e in timeline.events}
    assert {
        RoastEventKind.RUN_STARTED,
        RoastEventKind.RUN_COMPLETED,
        RoastEventKind.LOGS_EXPORTED,
    } <= event_kinds

    # Step 12: restart proves the completed state is recoverable.
    await service.shutdown()
    restarted = RoastService(
        store,
        config=config,
        roaster=FakeMCPClient(),
        advisor=FakeAdvisor(),
        run_loop=False,
        clock=FakeClock(),
    )
    latest = await store.read_latest_run()
    assert latest is not None
    assert latest.agent_phase is RoastPhase.COMPLETE
    recovered = await restarted.detail(run_id)
    assert recovered.agent_phase is RoastPhase.COMPLETE
    assert recovered.outcome == "completed"
    assert recovered.export_manifest is not None
    # A completed run is not active, so a fresh roast is unblocked after restart.
    assert (await store.active_run()) is None


@pytest.mark.asyncio
async def test_provider_error_persists_advisor_row_with_null_decision(
    store: RoastStore, tmp_path: Path
) -> None:
    """A forced advisor ``provider_error`` writes an advisor_decisions row
    (#167): ``status='provider_error'``, ``decision_json IS NULL``, and linked
    to the REJECT safety verdict the failure produced — the #134 trace that was
    thrown away. Backstops the failure path the success roast cannot exercise."""
    from roastpilot_agent.advisor import AdvisorFailureMode

    clock = FakeClock()
    config = AppConfig(
        controller=ControllerConfig(
            telemetry_log_interval_seconds=1.0,
            post_first_crack_control=_BASELINE_POST_FC_CONTROL,
        )
    )
    mcp = FakeMCPClient(
        [_reading(bean=205.0, env=210.0, t0_detected=True, first_crack_detected=True)],
        export_result=_export_result(tmp_path),
    )
    # Every advisory call fails as a provider error (the #134 failure mode).
    advisor = FakeAdvisor([AdvisorFailureMode.PROVIDER_ERROR] * 8)
    service = RoastService(
        store,
        config=config,
        roaster=mcp,
        advisor=advisor,
        exporter=mcp,
        run_loop=False,
        clock=clock,
    )

    async def tick() -> bool:
        assert service.runner is not None
        clock.advance(3.0)
        return await service.runner.tick_once()

    detail = await service.start_roast(_profile())
    run_id = detail.id
    # Drive to DEVELOPMENT (an advice phase) and request advice; the advisor
    # raises a provider error, which becomes a REJECT verdict + a failure row.
    mcp.frames = [_reading(bean=95.0, env=150.0, t0_detected=True)]
    for _ in range(config.controller.t0_debounce_ticks):
        await tick()
    mcp.frames = [_reading(bean=196.0, env=205.0, t0_detected=True, first_crack_detected=True)]
    await tick()
    mcp.frames = [_reading(bean=205.0, env=210.0, t0_detected=True, first_crack_detected=True)]
    await tick()

    timeline = await service.timeline(run_id)
    failures = [a for a in timeline.advisor_decisions if a.status == "provider_error"]
    assert failures, "the provider_error advisor outcome was persisted"
    failure = failures[0]
    assert failure.decision is None, "no decision on a provider error"
    assert failure.safety_evaluation_id is not None, "linked to the REJECT verdict"

    # The linked id resolves to a real safety_evaluations row, and it is a REJECT.
    async with store.connection.execute(
        "SELECT verdict FROM safety_evaluations WHERE id = ?",
        (failure.safety_evaluation_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "the linked safety_evaluation row exists"
    assert row[0] == "reject", "an advisor failure rejects and holds (E3-S3)"

    await service.shutdown()
