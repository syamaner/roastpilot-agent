"""Replay harness tests (E10-S1).

All hardware-free: the replay harness drives the real RoastService /
RoastRunner / RoastController against recorded telemetry, so these assert the
same typed SSE event surface + REST snapshots the SPA hydrates from. The two
synthesized pieces — the CLAMP key frame and the pre-T0 overrun fault fixture —
are exercised here for their *real* downstream verdicts (a genuine CLAMP from
``SafetyPolicy.evaluate_command``; a genuine RECOVERY from the overrun rule).
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from roastpilot_agent.api import RoastService
from roastpilot_agent.models import SseEvent, SseEventType
from roastpilot_agent.replay import (
    MAX_SPEED,
    MIN_SPEED,
    ReplayMarker,
    ReplaySource,
    clamp_speed,
    create_replay_app,
    load_export,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "replay"
_SESSION_2 = _FIXTURES / "session-2"
_SESSION_1 = _FIXTURES / "session-1"
_FAULT = _FIXTURES / "fault-pre-t0"
_COOLING_COMPLETE = _FIXTURES / "cooling-complete"


def _drain(queue: "asyncio.Queue[SseEvent]") -> list[SseEvent]:
    """Non-blocking drain of a broadcaster subscriber queue."""
    events: list[SseEvent] = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return events


@pytest_asyncio.fixture
async def session2(tmp_path: Path) -> AsyncIterator[tuple[RoastService, ReplaySource]]:
    """A started, paused (--step) replay of the auto-T0 demo roast."""
    _app, service, source = await create_replay_app(
        _SESSION_2, tmp_path / "s2.sqlite3", step_mode=True, speed=60
    )
    try:
        yield service, source
    finally:
        await source.aclose()


# --- Fixture parsing -------------------------------------------------------


def test_load_export_parses_session2_frames_and_markers() -> None:
    """session-2 parses to its recorded telemetry frames with the milestone
    markers tagged (auto-T0, FC, drop) and the run-ending marker on the last."""
    script = load_export(_SESSION_2)
    assert len(script.frames) == 270
    # All temperatures are Celsius and present on every frame.
    assert all(f.telemetry.bean_temp_c > 0 for f in script.frames)
    markers = {m for f in script.frames for m in f.markers}
    assert ReplayMarker.PREHEATING in markers
    assert ReplayMarker.T0 in markers
    assert ReplayMarker.FIRST_CRACK in markers
    assert ReplayMarker.DROP in markers
    assert ReplayMarker.END in script.frames[-1].markers
    # Detection booleans latch on after their recorded offset.
    assert not script.frames[0].telemetry.t0_detected
    assert script.frames[-1].telemetry.t0_detected
    assert script.frames[-1].telemetry.first_crack_detected


def test_load_export_missing_jsonl_raises(tmp_path: Path) -> None:
    """A directory without roast.jsonl is a clear error, not a silent empty."""
    with pytest.raises(FileNotFoundError):
        load_export(tmp_path)


def test_session1_manual_t0_export_parses() -> None:
    """session-1 (manual-T0) also parses and reaches a recorded drop."""
    script = load_export(_SESSION_1)
    assert script.frames
    markers = {m for f in script.frames for m in f.markers}
    assert ReplayMarker.T0 in markers
    assert ReplayMarker.DROP in markers


# --- Speed clamp -----------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(0.1, MIN_SPEED), (1.0, 1.0), (30.0, 30.0), (60.0, 60.0), (999.0, MAX_SPEED)],
)
def test_speed_clamps_into_band(requested: float, expected: float) -> None:
    """Replay speed is clamped to the supported 1x-60x band."""
    assert clamp_speed(requested) == expected


# --- Real-pipeline event sequence + phase progression ----------------------


@pytest.mark.asyncio
async def test_replay_drives_real_phase_progression(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """Stepping the replay advances the real controller through the recorded
    roast's agent phases — server-derived, never inferred from the export."""
    service, source = session2
    assert source.run_id is not None

    # Boots into preheating (the controller's idle->preheating start).
    detail = await service.detail(source.run_id)
    assert detail.agent_phase.value == "preheating"

    result = await source.advance_to(ReplayMarker.FIRST_CRACK)
    assert result.agent_phase == "development"

    result = await source.advance_to(ReplayMarker.DROP)
    assert result.agent_phase == "cooling"

    result = await source.advance_to(ReplayMarker.END)
    # session-2 ends in cooling: its cooling-stop is recorded past the last
    # telemetry frame, so STOP_COOLING never injects (see step-past-end test).
    assert result.agent_phase == "cooling"


@pytest.mark.asyncio
async def test_advance_to_t0_marker_phase(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """The ``t0`` marker fires when T0 is *detected*, which is BEFORE the agent
    transitions out of preheating — the controller debounces T0 over several
    ticks (``t0_debounce_ticks``) before moving to roasting. So at the ``t0``
    marker the phase is still ``preheating``; ``roasting_pre_first_crack`` only
    appears a few ticks later. Asserted explicitly so the debounce semantics are
    pinned, not assumed."""
    _service, source = session2
    at_t0 = await source.advance_to(ReplayMarker.T0)
    assert at_t0.marker_reached is True
    assert at_t0.agent_phase == "preheating"
    # A few more ticks past detection, the debounced transition has fired.
    later = await source.advance_to(ReplayMarker.FIRST_CRACK)
    assert later.agent_phase == "development"


@pytest.mark.asyncio
async def test_replay_emits_typed_sse_frames(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """The browser sees standard typed SseEvent frames — a per-tick telemetry
    frame and the controller's phase/event frames — not a bespoke format."""
    service, source = session2
    subscriber = service.events.subscribe()
    await source.advance_to(ReplayMarker.FIRST_CRACK)
    frames = _drain(subscriber)
    types = {f.event for f in frames}
    assert SseEventType.TELEMETRY in types
    assert SseEventType.PHASE_CHANGED in types
    # Every frame is the typed envelope with a monotonic id for ordering/dedup.
    assert all(isinstance(f, SseEvent) and f.id is not None for f in frames)
    telemetry = [f for f in frames if f.event is SseEventType.TELEMETRY]
    assert telemetry[0].data["agent_phase"] in {"preheating", "roasting_pre_first_crack"}


@pytest.mark.asyncio
async def test_snapshots_populated_for_replayed_run(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """GET /{id} + /telemetry + /timeline are populated from the same store
    writes a live roast makes, so the SPA hydrate-then-apply path works."""
    service, source = session2
    assert source.run_id is not None
    await source.advance_to(ReplayMarker.FIRST_CRACK)

    detail = await service.detail(source.run_id)
    assert detail.agent_phase.value in {"development", "roasting_pre_first_crack"}

    series = await service.telemetry(source.run_id, downsample=1)
    assert series.point_count > 0
    assert all(p.bean_temp_c is not None for p in series.points)

    timeline = await service.timeline(source.run_id)
    assert timeline.events  # run_started + phase changes at minimum


# --- The CLAMP key frame ---------------------------------------------------


@pytest.mark.asyncio
async def test_clamp_key_frame_surfaces_in_timeline_and_sse(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """Exactly one CLAMP advisory surfaces — in the timeline (detail trace
    table) and on SSE (live advisory panel) — with a policy-accurate reason."""
    service, source = session2
    assert source.run_id is not None
    subscriber = service.events.subscribe()

    result = await source.advance_to(ReplayMarker.CLAMP)
    assert ReplayMarker.CLAMP.value  # marker exists
    assert result.agent_phase == "development"

    timeline = await service.timeline(source.run_id)
    clamps = [e for e in timeline.safety_evaluations if e.verdict == "clamp"]
    assert len(clamps) == 1
    assert "clamped to heat 100 %" in clamps[0].reason  # real policy wording

    advisories = [e for e in timeline.events if e.kind.value == "advisory"]
    assert len(advisories) == 1
    payload = advisories[0].payload or {}
    assert payload.get("synthesized") is True
    assert payload.get("source") == "replay_overlay"
    assert payload["evaluation"]["verdict"] == "clamp"
    assert payload["evaluation"]["input_heat"] == 105
    assert payload["evaluation"]["adjusted_heat"] == 100

    sse_advisories = [f for f in _drain(subscriber) if f.event is SseEventType.ADVISORY]
    assert len(sse_advisories) == 1
    assert sse_advisories[0].data["evaluation"]["verdict"] == "clamp"


@pytest.mark.asyncio
async def test_clamp_emitted_only_once(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """The CLAMP overlay fires exactly once across a full replay, even when
    stepped past its trigger repeatedly."""
    service, source = session2
    assert source.run_id is not None
    await source.advance_to(ReplayMarker.END)
    await source.advance_to(ReplayMarker.END)  # idempotent re-request
    timeline = await service.timeline(source.run_id)
    clamps = [e for e in timeline.safety_evaluations if e.verdict == "clamp"]
    assert len(clamps) == 1


# --- Deterministic stepping ------------------------------------------------


@pytest.mark.asyncio
async def test_step_advances_exact_ticks(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """step(n) advances exactly n recorded frames, deterministically."""
    _service, source = session2
    r1 = await source.step(5)
    assert r1.tick == 5
    r2 = await source.step(10)
    assert r2.tick == 15
    assert r2.settled is True
    # last_event_id is the broadcaster sequence — monotonic, non-decreasing.
    assert r2.last_event_id >= r1.last_event_id


@pytest.mark.asyncio
async def test_step_past_end_stops_at_finalize(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """Stepping more ticks than the export has stops cleanly at the last frame.

    The run ends in ``cooling`` (not ``complete``): session-2 *does* record a
    cooling-stop, but at 1394.45 s — past its last telemetry frame (~1357 s) —
    so the STOP_COOLING injection never lands and the controller stays in
    cooling. (The ``cooling-complete`` fixture is the one that reaches COMPLETE.)
    """
    _service, source = session2
    result = await source.step(10_000)
    assert result.tick == source.frame_count  # clamped at the last frame
    assert result.agent_phase == "cooling"
    # Stepping again past the end is a no-op (cursor already exhausted).
    again = await source.step(5)
    assert again.tick == source.frame_count


@pytest.mark.asyncio
async def test_advance_to_unreached_marker_reports_not_reached(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """advance_to a marker the export never produces exhausts the frames and
    flags ``marker_reached=False`` (the control route turns this into a 404).
    session-2 never faults, so ``fault`` is never reached."""
    _service, source = session2
    result = await source.advance_to(ReplayMarker.FAULT)
    assert result.tick == source.frame_count
    assert result.marker_reached is False
    assert result.requested_marker == "fault"
    # A reached marker reports marker_reached=True with the marker echoed.
    reached = await source.advance_to(ReplayMarker.COOLING)
    assert reached.marker_reached is True
    assert reached.requested_marker == "cooling"


@pytest.mark.asyncio
async def test_advance_to_is_idempotent_once_reached(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """Re-requesting a reached marker returns the settled state without
    stepping further (so a Playwright setup can poll safely)."""
    _service, source = session2
    first = await source.advance_to(ReplayMarker.FIRST_CRACK)
    again = await source.advance_to(ReplayMarker.FIRST_CRACK)
    assert again.tick == first.tick


@pytest.mark.asyncio
async def test_free_running_replay_completes_with_injected_sleep(tmp_path: Path) -> None:
    """The free-running run() path drives the whole export; an injected no-op
    sleep keeps it instant (it shares the stepping core, so the same events)."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    _app, service, source = await create_replay_app(
        _SESSION_2, tmp_path / "free.sqlite3", step_mode=False, speed=60
    )
    # Swap in the no-op sleep so the wall clock never advances.
    source._sleep = fake_sleep  # type: ignore[attr-defined]  # noqa: SLF001 — test injection
    await source.run()
    assert source.run_id is not None
    detail = await service.detail(source.run_id)
    assert detail.agent_phase.value == "cooling"
    # 60x over a 1 s tick → ~0.0167 s requested delay each inter-tick gap.
    assert sleeps and all(abs(d - 1.0 / 60.0) < 1e-9 for d in sleeps)
    await source.aclose()


# --- The synthetic pre-T0 overrun fault fixture ----------------------------


@pytest.mark.asyncio
async def test_fault_fixture_drives_real_recovery(tmp_path: Path) -> None:
    """The synthetic overrun track drives the REAL safety policy past the
    pre-T0 bound into operator_recovery_required (no fabricated verdict)."""
    _app, service, source = await create_replay_app(
        _FAULT, tmp_path / "fault.sqlite3", step_mode=True, speed=60
    )
    assert source.run_id is not None
    result = await source.advance_to(ReplayMarker.RECOVERY)
    assert result.agent_phase == "operator_recovery_required"

    timeline = await service.timeline(source.run_id)
    verdicts = {e.verdict for e in timeline.safety_evaluations}
    assert "recovery" in verdicts
    # A recovery_required event reaches the trace (drives the SPA RecoveryModal).
    assert any(e.kind.value == "recovery_required" for e in timeline.events)
    await source.aclose()


# --- The gated control-route safety boundary -------------------------------


@pytest.mark.asyncio
async def test_step_routes_mounted_only_in_step_mode(tmp_path: Path) -> None:
    """The /api/replay/{step,advance-to} control routes exist ONLY on the
    --step app — never on a non-step replay app, AND never on the LIVE app.

    The live-app assertion is the load-bearing safety boundary: these routes
    drive the controller tick loop, so they must never be reachable on a real
    roast. Pin it directly against ``api.create_app`` (the live factory)."""
    from roastpilot_agent.api import create_app

    step_app, _s1, step_src = await create_replay_app(
        _SESSION_2, tmp_path / "step.sqlite3", step_mode=True, speed=60
    )
    free_app, _s2, free_src = await create_replay_app(
        _SESSION_2, tmp_path / "free.sqlite3", step_mode=False, speed=60
    )
    step_routes = {r.path for r in step_app.routes}  # type: ignore[attr-defined]
    free_routes = {r.path for r in free_app.routes}  # type: ignore[attr-defined]
    assert "/api/replay/step" in step_routes
    assert "/api/replay/advance-to" in step_routes
    assert "/api/replay/step" not in free_routes
    assert "/api/replay/advance-to" not in free_routes
    # The LIVE app (real roast) never exposes the replay control routes.
    live_routes = {r.path for r in create_app(service=None).routes}  # type: ignore[attr-defined]
    assert "/api/replay/step" not in live_routes
    assert "/api/replay/advance-to" not in live_routes
    await step_src.aclose()
    await free_src.aclose()


@pytest.mark.asyncio
async def test_speed_clamp_bounds_are_exposed() -> None:
    """The 1x-60x band constants back the screen-recording (1x) + dev (60x)
    rates the kickoff brief calls for."""
    assert MIN_SPEED == 1.0
    assert MAX_SPEED == 60.0


# --- ReplayRoasterControl write surface ------------------------------------


@pytest.mark.asyncio
async def test_roaster_control_records_writes_without_actuating() -> None:
    """The replay roaster is a recording no-op: it satisfies CommandExecutor
    (so the controller owns/evaluates writes) but actuates nothing."""
    from roastpilot_agent.replay import ReplayRoasterControl

    control = ReplayRoasterControl()
    assert await control.read_telemetry() is None  # no frames loaded yet
    assert control.last_state is None
    await control.start_session()
    await control.set_targets(heat_percent=60, fan_percent=40)
    await control.mark_beans_added()
    await control.mark_first_crack()
    await control.drop_beans()
    await control.start_cooling()
    await control.stop_cooling()
    await control.emergency_stop(reason="test")
    names = [name for name, _ in control.commands]
    assert names == [
        "start_session",
        "set_targets",
        "mark_beans_added",
        "mark_first_crack",
        "drop_beans",
        "start_cooling",
        "stop_cooling",
        "emergency_stop",
    ]


# --- Real fault-on-replay + a genuine run-completing replay ----------------


@pytest.mark.asyncio
async def test_session1_replay_faults_on_env_ceiling(tmp_path: Path) -> None:
    """session-1 *faults* on replay — it is NOT a "completes" fixture.

    The real roast completed (the Hottop tolerated it), but session-1 carries
    real env-temp readings up to 242 °C, which exceed the agent's deliberately
    conservative ``max_env_temp_c`` = 240 °C software ceiling. The **real**
    safety policy correctly trips (EMERGENCY_STOP → FAULTED) — faithful replay
    of a real reading, not a replay bug. (Distinct from the synthetic
    ``fault-pre-t0`` fixture, which trips the *pre-T0 overrun* rule → RECOVERY;
    session-1's pre-T0 bean temp stays under 200 °C.)"""
    _app, service, source = await create_replay_app(
        _SESSION_1, tmp_path / "s1.sqlite3", step_mode=True, speed=60
    )
    assert source.run_id is not None
    result = await source.advance_to(ReplayMarker.FAULT)
    assert result.agent_phase == "faulted"
    detail = await service.detail(source.run_id)
    assert detail.outcome == "faulted"
    assert detail.fault_reason is not None
    assert "exceeds the hard ceiling" in detail.fault_reason
    await source.aclose()


@pytest.mark.asyncio
async def test_cooling_complete_fixture_stops_cooling_and_completes(tmp_path: Path) -> None:
    """The synthetic ``cooling-complete`` fixture records cooling_stopped BEFORE
    its last telemetry frame, so the replay's STOP_COOLING injection actually
    fires (real coverage of that branch) and the run reaches COMPLETE — the
    genuine successful-roast baseline session-1 cannot be."""
    _app, service, source = await create_replay_app(
        _COOLING_COMPLETE, tmp_path / "cc.sqlite3", step_mode=True, speed=60
    )
    assert source.run_id is not None
    result = await source.advance_to(ReplayMarker.END)
    assert result.finalized is True
    detail = await service.detail(source.run_id)
    assert detail.outcome == "completed"
    assert detail.completed_at_utc is not None
    # The STOP_COOLING operator action was issued through the real control path.
    assert "stop_cooling" in source.issued_commands
    await source.aclose()


# --- HTTP control surface (via the gated routes) ---------------------------


@pytest.mark.asyncio
async def test_http_step_routes_drive_the_replay(tmp_path: Path) -> None:
    """The gated POST /api/replay/{step,advance-to} routes advance the real
    controller and return the settled body shape the Playwright setup reads."""
    from fastapi.testclient import TestClient

    app, _service, source = await create_replay_app(
        _SESSION_2, tmp_path / "http.sqlite3", step_mode=True, speed=60
    )
    with TestClient(app) as client:
        body = client.post("/api/replay/step", json={"ticks": 3}).json()
        assert body["tick"] == 3
        assert body["settled"] is True
        assert set(body) >= {
            "agent_phase",
            "tick",
            "elapsed_seconds",
            "finalized",
            "settled",
            "last_event_id",
            "requested_marker",
            "marker_reached",
        }
        advanced = client.post("/api/replay/advance-to", json={"marker": "first_crack"}).json()
        assert advanced["agent_phase"] == "development"
        assert advanced["marker_reached"] is True
        assert advanced["requested_marker"] == "first_crack"
        assert advanced["last_event_id"] >= body["last_event_id"]
    assert source.run_id is not None


@pytest.mark.asyncio
async def test_http_advance_to_unreached_marker_is_404(tmp_path: Path) -> None:
    """advance-to a marker that never fires returns 404 with a descriptive body,
    so a Playwright global-setup fails loud on a wrong fixture/marker rather than
    screenshotting the wrong (terminal) state."""
    from fastapi.testclient import TestClient

    app, _service, _source = await create_replay_app(
        _SESSION_2, tmp_path / "http404.sqlite3", step_mode=True, speed=60
    )
    with TestClient(app) as client:
        # session-2 never faults → the 'fault' marker can never be reached.
        response = client.post("/api/replay/advance-to", json={"marker": "fault"})
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert "fault" in detail
        assert "never fired" in detail


@pytest.mark.asyncio
async def test_empty_export_raises(tmp_path: Path) -> None:
    """An export whose roast.jsonl has only blank lines is a clear error."""
    bad = tmp_path / "empty"
    bad.mkdir()
    (bad / "roast.jsonl").write_text("\n  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="no telemetry records"):
        load_export(bad)


@pytest.mark.asyncio
async def test_fault_severity_override_reaches_faulted(tmp_path: Path) -> None:
    """With the overrun severity set to ``fault``, the synthetic overrun fixture
    reaches FAULTED (the dashboard-fault baseline) through the real policy."""
    from roastpilot_agent.config import AppConfig, SafetyLimits

    config = AppConfig(safety=SafetyLimits(pre_t0_overrun_severity="fault"))
    _app, service, source = await create_replay_app(
        _FAULT, tmp_path / "faulted.sqlite3", step_mode=True, speed=60, config=config
    )
    assert source.run_id is not None
    result = await source.advance_to(ReplayMarker.FAULT)
    assert result.agent_phase == "faulted"
    timeline = await service.timeline(source.run_id)
    assert any(e.verdict == "fault" for e in timeline.safety_evaluations)
    await source.aclose()
