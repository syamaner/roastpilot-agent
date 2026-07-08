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
from typing import NoReturn

import pytest
import pytest_asyncio

from roastpilot_agent.api import RoastService
from roastpilot_agent.models import RoastEventKind, RoastPhase, SseEvent, SseEventType
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
    # #467 (revises #464/D86): the pre-#342 export carries no ambient reading,
    # so replay synthesizes a fixed, representative triad on every frame —
    # mirroring how `mic_status` is synthesized rather than read off the flat
    # export — so the "Room" readout renders real values in replay/demo mode.
    assert all(f.telemetry.ambient_temp_c == 21.0 for f in script.frames)
    assert all(f.telemetry.ambient_humidity_pct == 45.0 for f in script.frames)
    assert all(f.telemetry.ambient_pressure_hpa == 1013.0 for f in script.frames)


def test_synthesized_ambient_returns_fixed_representative_triad() -> None:
    """``_synthesized_ambient`` returns a fixed, plausible indoor roastery triad.

    Recorded exports predate #342 and the live agent persists no per-tick
    ambient history, so replay has no real ambient value to read back (#467) —
    it synthesizes one instead, mirroring `_synthesized_mic_status`."""
    from roastpilot_agent.replay import (
        _synthesized_ambient,  # pyright: ignore[reportPrivateUsage, reportPrivateImportUsage]
    )

    temp_c, humidity_pct, pressure_hpa = _synthesized_ambient()
    assert temp_c == 21.0
    assert humidity_pct == 45.0
    assert pressure_hpa == 1013.0
    # Deterministic: repeated calls return the identical representative triad.
    assert _synthesized_ambient() == (temp_c, humidity_pct, pressure_hpa)


def test_telemetry_from_record_carries_synthesized_ambient() -> None:
    """``_telemetry_from_record`` sets the synthesized ambient triad (#467).

    Directly exercises the projection function (not just the parsed export),
    so a regression here fails at the unit boundary, not only end-to-end."""
    from roastpilot_agent.replay import (
        _telemetry_from_record,  # pyright: ignore[reportPrivateUsage, reportPrivateImportUsage]
    )

    record = {
        "bean_temp_c": 150.0,
        "env_temp_c": 160.0,
        "bean_ror_c_per_min": None,
        "env_ror_c_per_min": None,
        "cooling_on": False,
    }
    telemetry = _telemetry_from_record(record, t0=True, first_crack=False)
    assert telemetry.ambient_temp_c == 21.0
    assert telemetry.ambient_humidity_pct == 45.0
    assert telemetry.ambient_pressure_hpa == 1013.0


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


# --- #128: stepped elapsed_seconds tracks sim-time, not wall-clock ---------


@pytest.mark.asyncio
async def test_stepped_elapsed_seconds_spreads_over_recorded_duration(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """Regression guard for #128.

    In ``--step`` mode the whole burst drains in a few ms of wall time, so if
    ``elapsed_seconds`` were wall-clock every telemetry frame would carry the
    same ~instant value and the dashboard curve (plotted at ``t =
    elapsed_seconds``) would collapse onto one x. With the sim clock, elapsed
    tracks each frame's recorded ``monotonic_seconds``: it rises monotonically
    and spans the recorded run up to first crack (~1000 s for session-2), on
    both the live SSE frames and the persisted REST series.
    """
    service, source = session2
    assert source.run_id is not None
    subscriber = service.events.subscribe()

    await source.advance_to(ReplayMarker.FIRST_CRACK)

    # SSE telemetry frames: elapsed is non-decreasing and spans a real range.
    frames = _drain(subscriber)
    elapsed = [
        f.data["elapsed_seconds"]
        for f in frames
        if f.event is SseEventType.TELEMETRY and f.data.get("elapsed_seconds") is not None
    ]
    assert len(elapsed) > 1
    assert elapsed == sorted(elapsed), "elapsed must be monotonically non-decreasing"
    # Recorded first crack lands ~1000 s into the run — far from a wall-clock
    # collapse (which would pin every frame to a sub-second span).
    assert elapsed[-1] - elapsed[0] > 100.0

    # The persisted REST series the SPA hydrates from spreads the same way. The
    # store throttles rows by ``telemetry_log_interval_seconds`` keyed on
    # ``elapsed_seconds`` — under the wall-clock bug elapsed barely moved, so the
    # throttle kept a single row (the issue's "point_count 1"). With sim-time it
    # advances, so the throttle keeps a real spread of rows.
    series = await service.telemetry(source.run_id, downsample=1)
    assert series.point_count > 1
    persisted = [p.elapsed_seconds for p in series.points if p.elapsed_seconds is not None]
    assert persisted == sorted(persisted)
    assert persisted[-1] - persisted[0] > 100.0

    # Elapsed is derived from the recording's own sim-time: the final frame's
    # elapsed equals its recorded monotonic offset from frame 0. This is
    # mode-independent — the stepped and free-running (1×) paths share
    # ``_advance_one``, so the 1× rig produces the identical spread, never a
    # wall-clock collapse.
    script = load_export(_SESSION_2)
    base = script.frames[0].monotonic_seconds
    fc_index = next(
        i for i, frame in enumerate(script.frames) if ReplayMarker.FIRST_CRACK in frame.markers
    )
    expected_fc_elapsed = script.frames[fc_index].monotonic_seconds - base
    assert abs(elapsed[-1] - expected_fc_elapsed) < 1e-6


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
async def test_step_to_is_idempotent_absolute_cursor(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """#338: step_to(n) advances to an ABSOLUTE cursor and is idempotent — the
    retry-safe sibling of the additive step(). Re-issuing the same (or a lower)
    target after reaching it is a no-op, so a Playwright retry can't over-step."""
    _service, source = session2
    r1 = await source.step_to(8)
    assert r1.tick == 8
    # Re-targeting the same absolute cursor steps NOTHING (idempotent under retry).
    r2 = await source.step_to(8)
    assert r2.tick == 8
    # A target BELOW the current cursor is forward-only — also a no-op (never rewinds).
    r3 = await source.step_to(3)
    assert r3.tick == 8
    # A higher target advances only the delta.
    r4 = await source.step_to(12)
    assert r4.tick == 12
    # A target past the end stops cleanly at the last frame (clamped, never hangs).
    end = await source.step_to(10_000)
    assert end.tick == source.frame_count


@pytest.mark.asyncio
async def test_step_result_carries_run_id_and_charged_point_count(
    session2: tuple[RoastService, ReplaySource],
) -> None:
    """#338 lossless settle fields: the step result carries the run id and the
    store-backed CHARGED telemetry point count (== the rendered curve length).
    Pre-charge (preheating) the charged count is 0 — the curve is empty until T0;
    post-FC it is positive and matches the persisted charged rows."""
    _service, source = session2
    pre = await source.step_to(8)  # still preheating, pre-charge
    assert pre.run_id == source.run_id
    assert pre.agent_phase == "preheating"
    assert pre.persisted_point_count == 0  # no charged points before T0
    post = await source.advance_to(ReplayMarker.FIRST_CRACK)
    assert post.persisted_point_count > 0  # the developed curve carries points
    # The reported count equals the charged rows in the REST snapshot the SPA hydrates.
    assert source.run_id is not None
    series = await _service.telemetry(source.run_id, downsample=1)
    charged = sum(1 for p in series.points if p.charge_elapsed_seconds is not None)
    assert post.persisted_point_count == charged


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
    sleep keeps it instant (it shares the stepping core, so the same events).

    The no-op sleep is plumbed through the public ``create_replay_app(sleep=...)``
    factory parameter (#103) — not by reaching into the source's private
    ``_sleep`` — so the wiring a real caller would use is the wiring under test.
    """
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    _app, service, source = await create_replay_app(
        _SESSION_2, tmp_path / "free.sqlite3", step_mode=False, speed=60, sleep=fake_sleep
    )
    await source.run()
    assert source.run_id is not None
    detail = await service.detail(source.run_id)
    assert detail.agent_phase.value == "cooling"
    # 60x over a 1 s tick → ~0.0167 s requested delay each inter-tick gap. That
    # the injected sleep was actually awaited proves the factory threaded it in.
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
    # try/finally so a failure building the SECOND app (or any assertion below)
    # still tears down the FIRST source's store + aiosqlite worker (#103).
    try:
        free_app, _s2, free_src = await create_replay_app(
            _SESSION_2, tmp_path / "free.sqlite3", step_mode=False, speed=60
        )
        try:
            step_routes = {r.path for r in step_app.routes}  # type: ignore[attr-defined]
            free_routes = {r.path for r in free_app.routes}  # type: ignore[attr-defined]
            assert "/api/replay/step" in step_routes
            assert "/api/replay/step-to" in step_routes  # #338 idempotent variant
            assert "/api/replay/advance-to" in step_routes
            assert "/api/replay/step" not in free_routes
            assert "/api/replay/step-to" not in free_routes
            assert "/api/replay/advance-to" not in free_routes
            # The LIVE app (real roast) never exposes the replay control routes.
            live_routes = {r.path for r in create_app(service=None).routes}  # type: ignore[attr-defined]
            assert "/api/replay/step" not in live_routes
            assert "/api/replay/step-to" not in live_routes
            assert "/api/replay/advance-to" not in live_routes
        finally:
            await free_src.aclose()
    finally:
        await step_src.aclose()


@pytest.mark.asyncio
async def test_replay_serves_spa_when_spa_dir_given(tmp_path: Path) -> None:
    """``create_replay_app(..., spa_dir=...)`` mounts the built SPA at / so the
    recorded roast renders in the real dashboard, without shadowing the API:
    GET / returns index.html and /api/health still works."""
    from fastapi.testclient import TestClient

    spa = tmp_path / "dist"
    spa.mkdir()
    (spa / "index.html").write_text("<title>RoastPilot Replay</title>", encoding="utf-8")

    app, _service, source = await create_replay_app(
        _SESSION_2, tmp_path / "replay-spa.sqlite3", step_mode=True, speed=60, spa_dir=spa
    )
    try:
        with TestClient(app) as client:
            root = client.get("/")
            assert root.status_code == 200
            assert "RoastPilot Replay" in root.text
            assert client.get("/api/health").status_code == 200
    finally:
        await source.aclose()


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
    session-1's pre-T0 bean temp stays under 200 °C.)

    Post-#206 a fault no longer auto-finalises the run: the run reaches FAULTED
    and stays operable (loop alive, ``completed_at`` null, outcome not yet set)
    until the operator acknowledges it, so a fault never strands a physically-
    running machine. The fault reason is therefore asserted on the live FAULT
    event in the decision trace, not on the (not-yet-persisted) outcome."""
    _app, service, source = await create_replay_app(
        _SESSION_1, tmp_path / "s1.sqlite3", step_mode=True, speed=60
    )
    assert source.run_id is not None
    result = await source.advance_to(ReplayMarker.FAULT)
    assert result.agent_phase == "faulted"
    # Operable-faulted (#206): faulted, but NOT finalised — outcome/completed_at
    # stay unset until the operator acknowledges the fault.
    detail = await service.detail(source.run_id)
    assert detail.agent_phase is RoastPhase.FAULTED
    assert detail.outcome is None
    assert detail.completed_at_utc is None
    # The real safety reason is on the FAULT event in the decision trace.
    timeline = await service.timeline(source.run_id)
    fault_events = [e for e in timeline.events if e.kind is RoastEventKind.FAULT]
    assert fault_events, "expected a FAULT event in the trace"
    reasons = [(e.payload or {}).get("reason") for e in fault_events if isinstance(e.payload, dict)]
    assert any(isinstance(r, str) and "exceeds the hard ceiling" in r for r in reasons)
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
    """The gated POST /api/replay/{step,step-to,advance-to} routes advance the real
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
            "run_id",  # #338 lossless settle fields
            "persisted_point_count",
            "requested_marker",
            "marker_reached",
        }
        # #338: the run id + charged point count are in the body for the REST settle.
        assert body["run_id"] == source.run_id
        assert body["persisted_point_count"] == 0  # pre-charge: empty curve
        # #338: the idempotent absolute-cursor route lands the target and re-targets
        # are no-ops (retry-safe).
        stepped_to = client.post("/api/replay/step-to", json={"tick": 6}).json()
        assert stepped_to["tick"] == 6
        again = client.post("/api/replay/step-to", json={"tick": 6}).json()
        assert again["tick"] == 6  # idempotent
        advanced = client.post("/api/replay/advance-to", json={"marker": "first_crack"}).json()
        assert advanced["agent_phase"] == "development"
        assert advanced["marker_reached"] is True
        assert advanced["requested_marker"] == "first_crack"
        assert advanced["last_event_id"] >= body["last_event_id"]
        assert advanced["persisted_point_count"] > 0  # developed curve has points
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


def test_load_export_skips_unrecognised_record_type(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-``event`` record with an unexpected ``type`` is skipped + warned, not
    silently coerced into a telemetry frame (#103).

    The old open ``else`` treated EVERY non-event record as telemetry, so a
    future ``type="summary"`` body would become a bogus telemetry frame (and
    crash on the missing temperature keys). This pins the guard: a ``summary``
    record is dropped with a warning, and parsing still yields exactly the real
    telemetry frames — proving the unexpected record never entered the series.
    """
    export = tmp_path / "with-summary"
    export.mkdir()
    lines = [
        '{"type": "telemetry", "bean_temp_c": 38.0, "env_temp_c": 43.0, "monotonic_seconds": 1.0}',
        '{"type": "summary", "drop_temp_c": 195.0, "dtr": 0.15}',
        '{"type": "telemetry", "bean_temp_c": 40.0, "env_temp_c": 45.0, "monotonic_seconds": 2.0}',
        '{"type": "event", "kind": "beans_added", "monotonic_seconds": 1.5}',
    ]
    (export / "roast.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        script = load_export(export)

    # Exactly the two real telemetry frames — the summary record is NOT among them.
    assert len(script.frames) == 2
    assert [f.telemetry.bean_temp_c for f in script.frames] == [38.0, 40.0]
    # And it warned loudly about the unrecognised type (mentioning 'summary').
    assert any("summary" in rec.message and "unrecognised" in rec.message for rec in caplog.records)


def test_load_export_treats_missing_type_as_telemetry(tmp_path: Path) -> None:
    """A record with no ``type`` field stays telemetry (back-compat with any
    pre-typed export), distinct from an *unexpected* explicit type (#103)."""
    export = tmp_path / "legacy"
    export.mkdir()
    (export / "roast.jsonl").write_text(
        '{"bean_temp_c": 38.0, "env_temp_c": 43.0, "monotonic_seconds": 1.0}\n',
        encoding="utf-8",
    )
    script = load_export(export)
    assert len(script.frames) == 1
    assert script.frames[0].telemetry.bean_temp_c == 38.0


@pytest.mark.asyncio
async def test_create_replay_app_closes_store_when_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``source.start()`` failure during ``create_replay_app`` tears the store
    (and its aiosqlite worker thread) down rather than leaking it (#103).

    Before the guard, a raise from ``start()`` after ``store.initialize()`` left
    the opened store — and its background worker thread — orphaned: no app is
    returned, so no lifespan ever runs to close it. This injects that failure and
    asserts (a) the error propagates and (b) the captured store was closed (its
    public ``connection`` property now raises "not initialized"), which is what
    joins the worker thread. A net thread count back at baseline confirms no
    worker outlived the failed bring-up.
    """
    import threading

    import roastpilot_agent.replay as replay_module
    from roastpilot_agent.replay import ReplaySource
    from roastpilot_agent.store import RoastStore

    captured: dict[str, RoastStore] = {}
    real_build = replay_module.build_replay_service

    def _capturing_build(
        export_dir: Path, store_path: Path, **kwargs: object
    ) -> tuple[object, ReplaySource, RoastStore]:
        service, source, store = real_build(export_dir, store_path, **kwargs)  # type: ignore[arg-type]
        captured["store"] = store
        return service, source, store

    async def _boom(self: ReplaySource) -> NoReturn:
        raise RuntimeError("synthetic start failure")

    monkeypatch.setattr(replay_module, "build_replay_service", _capturing_build)
    monkeypatch.setattr(ReplaySource, "start", _boom)

    baseline_threads = threading.active_count()
    with pytest.raises(RuntimeError, match="synthetic start failure"):
        await create_replay_app(_SESSION_2, tmp_path / "leak.sqlite3", step_mode=True, speed=60)

    store = captured["store"]
    # The store the factory opened was closed by the guard — its public
    # ``connection`` accessor raises once closed, and the close is what stops the
    # aiosqlite worker thread.
    with pytest.raises(RuntimeError, match="not initialized"):
        _ = store.connection
    # And no worker thread outlived the failed bring-up (give it a beat to join).
    for _ in range(50):
        if threading.active_count() <= baseline_threads:
            break
        await asyncio.sleep(0.01)
    assert threading.active_count() <= baseline_threads


@pytest.mark.asyncio
async def test_clamp_persists_before_sse_emit_no_double_emit_on_store_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLAMP overlay persists BEFORE the SSE flush, so a store-write failure
    emits nothing and the once-only guard stays unset for a clean retry (#103).

    Mirrors the live ``tick_once`` persist-then-flush ordering. The old order
    (emit-then-persist) could double-emit the advisory to SSE if a store write
    failed after the emit but before ``_clamp_emitted`` latched. This makes the
    CLAMP overlay's FIRST ``record_safety_evaluation`` write fail (other
    per-tick evaluations pass through untouched) — so nothing is persisted AND,
    because the emit now runs last, nothing is broadcast — then lets the retry
    succeed and asserts exactly ONE advisory frame and ONE CLAMP verdict, never
    two.
    """
    from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict
    from roastpilot_agent.store import RoastStore

    real_record_eval = RoastStore.record_safety_evaluation
    clamp_calls = {"n": 0}

    async def flaky_record_eval(
        self: RoastStore, *, run_id: str, tick: int, evaluation: SafetyEvaluation
    ) -> int:
        # Only the synthesized CLAMP overlay's write is made to fail (once); the
        # ordinary per-tick evaluations the controller persists pass through.
        if evaluation.verdict is SafetyVerdict.CLAMP:
            clamp_calls["n"] += 1
            if clamp_calls["n"] == 1:
                raise RuntimeError("synthetic store failure")
        return await real_record_eval(self, run_id=run_id, tick=tick, evaluation=evaluation)

    monkeypatch.setattr(RoastStore, "record_safety_evaluation", flaky_record_eval)

    _app, service, source = await create_replay_app(
        _SESSION_2, tmp_path / "clamp-fail.sqlite3", step_mode=True, speed=60
    )
    try:
        subscriber = service.events.subscribe()
        # First attempt: stepping to the CLAMP trigger raises out of the FIRST
        # store write — and because the SSE emit now runs AFTER persistence, no
        # advisory frame was broadcast.
        with pytest.raises(RuntimeError, match="synthetic store failure"):
            await source.advance_to(ReplayMarker.CLAMP)
        first = [f for f in _drain(subscriber) if f.event is SseEventType.ADVISORY]
        assert first == [], "no advisory must reach SSE when the store write failed"

        # Retry succeeds (the next call to the patched method passes through):
        # exactly one advisory now reaches SSE (no double-emit), and exactly one
        # CLAMP verdict lands in the timeline.
        await source.advance_to(ReplayMarker.CLAMP)
        second = [f for f in _drain(subscriber) if f.event is SseEventType.ADVISORY]
        assert len(second) == 1
        assert source.run_id is not None
        timeline = await service.timeline(source.run_id)
        clamps = [e for e in timeline.safety_evaluations if e.verdict == "clamp"]
        assert len(clamps) == 1
    finally:
        await source.aclose()


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


@pytest.mark.asyncio
async def test_trim_engages_once_no_flip_flop_on_session2_replay(tmp_path: Path) -> None:
    """#327 hysteresis: the anticipatory heat trim engages EXACTLY ONCE on the real
    session-2 roast and holds through to the FC hand-off — no 100↔65 oscillation.

    Pre-latch, the naive FC-ETA wobbled across the window boundary so the
    deterministic heat thrashed 100→65→100→65→100 (an extra ``set_targets`` per
    flip — the #218 lever-thrash and the source of the ``dashboard-developed``
    replay event-stream churn). With the per-run latch the lever sequence to first
    crack is the flat-floor write then a single trim step, after which the trim is
    held; FC then hands control to the post-FC loop (phase development)."""
    _app, _service, source = await create_replay_app(
        _SESSION_2, tmp_path / "trim_latch.sqlite3", step_mode=True, speed=60
    )
    try:
        result = await source.advance_to(ReplayMarker.FIRST_CRACK)
        assert result.agent_phase == "development"  # FC handed off to the post-FC loop
        # The source owns the recording control; read the executed lever writes.
        set_targets = [args for name, args in source._control.commands if name == "set_targets"]  # pyright: ignore[reportPrivateUsage]
        # The run-start command (the replay profile's heat 100 / fan 10), then the
        # deterministic pre-FC floor (fan opens to 30), then a SINGLE trim step to
        # 65 — held to FC. The key assertion is no 100↔65 oscillation: heat 65
        # appears exactly once and never reverts to 100 before the FC hand-off.
        assert set_targets == [
            {"heat": 100, "fan": 10},
            {"heat": 100, "fan": 30},
            {"heat": 65, "fan": 30},
        ], set_targets
        heats = [t["heat"] for t in set_targets]
        assert heats.count(65) == 1  # one engage
        assert heats[-1] == 65  # held trimmed into the FC hand-off (no snap-back)
    finally:
        await source.aclose()
