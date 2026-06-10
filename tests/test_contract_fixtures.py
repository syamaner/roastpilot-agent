"""Contract-fixture drift guard — the Python frame-dump half (E10-S6 PR2, #98).

The SPA hand-mirrors the agent's SSE + REST contract in TypeScript (``web/src/
lib/types.ts`` and ``web/src/pages/dashboard/events.ts``). A hand mirror drifts
silently: #115 was a ``phase_changed`` ``{phase}`` → reducer-read ``agent_phase``
mismatch that *every S2 test enshrined* (green-but-wrong). This test, paired with
``web/src/lib/contract.test.ts``, closes that whole class.

It dumps the **real** SSE frame for every :class:`SseEventType` — and the real
REST snapshots (``RoastDetail`` incl. ``enabled_actions``, ``RoastSummary``) — to
committed JSON fixtures, then the vitest loads those exact frames and asserts the
SPA's real parsers accept every field. The frames are sourced from the real
``api.py`` emit path (the replay harness driving the real controller/broadcaster
in-process — no live MCP) and from real ``model_dump()`` payloads at the genuine
controller emit sites; never hand-authored (hand-authored payloads drift too —
the #115 trap).

The frames that the recorded fixtures naturally drive (``run_started``,
``phase_changed``, ``telemetry``, ``charge_guidance``, ``t0_detected``,
``first_crack``, ``advisory`` incl. the synthesized CLAMP, ``command_executed``,
``recovery_required``, ``fault``, ``run_completed``) come straight off the wire.
The five the fixtures don't exercise (``safety_alert``, ``recovery_acknowledged``,
``command_failed``, ``logs_exported``, ``heartbeat``) are gap-filled by emitting
real ``model_dump()`` payloads through the **same** real ``EventBroadcaster`` so
every ``SseEventType`` is pinned. A coverage assertion fails if a new event type
is ever added and left unmapped.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from roastpilot_agent.models import (
    RoastDetail,
    RoastEventKind,
    RoastSummary,
    SseEvent,
    SseEventType,
)
from roastpilot_agent.replay import (
    ReplayMarker,
    ReplaySource,
    build_replay_service,
)
from roastpilot_agent.safety import SafetyEvaluation, SafetyVerdict

if TYPE_CHECKING:
    from roastpilot_agent.api import RoastService

# The committed fixtures the vitest contract test loads. Under tests/fixtures/
# per plan §8 (small contract/validation fixtures are the one commit exception).
_CONTRACT_DIR = Path(__file__).parent / "fixtures" / "contract"
_SSE_FRAMES_PATH = _CONTRACT_DIR / "sse_frames.json"
_REST_SNAPSHOTS_PATH = _CONTRACT_DIR / "rest_snapshots.json"

_REPLAY_FIXTURES = Path(__file__).parent / "fixtures" / "replay"
_SESSION_2 = _REPLAY_FIXTURES / "session-2"  # auto-T0 demo roast → most frames
_SESSION_1 = _REPLAY_FIXTURES / "session-1"  # faults on env ceiling → `fault`
_COOLING_COMPLETE = _REPLAY_FIXTURES / "cooling-complete"  # → `run_completed`
_FAULT_PRE_T0 = _REPLAY_FIXTURES / "fault-pre-t0"  # → `recovery_required`

# The fixture-WRITE tests are regenerate-on-demand only — they rewrite the
# committed JSON (which carries a wall-clock ``started_at_utc`` + random run id),
# so an unconditional run would dirty the tree on a plain ``pytest``. Gate them
# behind an env flag: ``REGEN_CONTRACT_FIXTURES=1 pytest`` regenerates after a
# real contract change (or the must-fail rename proof); a normal run skips them,
# keeping the committed fixtures the stable artifact the ``test_committed_*``
# tests (and the vitest) read.
_regen_only = pytest.mark.skipif(
    not os.environ.get("REGEN_CONTRACT_FIXTURES"),
    reason="fixture-write test: set REGEN_CONTRACT_FIXTURES=1 to regenerate on demand",
)


def _drain(queue: "asyncio.Queue[SseEvent]") -> list[SseEvent]:
    """Non-blocking drain of a broadcaster subscriber queue (the SSE wire)."""
    frames: list[SseEvent] = []
    while True:
        try:
            frames.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return frames


async def _subscribed_replay(
    export_dir: Path,
    store_path: Path,
) -> tuple["RoastService", ReplaySource, "asyncio.Queue[SseEvent]"]:
    """Wire a real replay service, subscribe BEFORE start, drive idle→preheating.

    Subscribing before :meth:`ReplaySource.start` is what lets us capture the
    ``run_started`` + first ``phase_changed`` frames the start handshake emits
    (the broadcaster fans out only to queues subscribed at emit time). The run is
    real: the recorded telemetry drives the real controller + safety + emit path.
    """
    service, source, store = build_replay_service(export_dir, store_path)
    await store.initialize()
    queue = service.events.subscribe()
    await source.start()
    return service, source, queue


async def _collect_session2_frames(tmp_path: Path) -> list[SseEvent]:
    """Drive the auto-T0 demo roast to its drop, returning every wire frame.

    Yields ``run_started``, ``phase_changed`` (enriched with ``enabled_actions``),
    ``telemetry``, ``charge_guidance``, ``t0_detected``, ``first_crack``,
    ``advisory`` (the synthesized replay CLAMP key frame carries
    ``decision`` + ``evaluation`` + ``synthesized``), and ``command_executed``
    (the operator-queued drop)."""
    _service, source, queue = await _subscribed_replay(_SESSION_2, tmp_path / "s2.sqlite3")
    try:
        await source.advance_to(ReplayMarker.FIRST_CRACK)
        await source.advance_to(ReplayMarker.DROP)
        return _drain(queue)
    finally:
        await source.aclose()


async def _collect_first(
    export_dir: Path,
    store_path: Path,
    marker: ReplayMarker,
    event_type: SseEventType,
) -> SseEvent:
    """Advance a fixture to ``marker`` and return its first ``event_type`` frame."""
    _service, source, queue = await _subscribed_replay(export_dir, store_path)
    try:
        await source.advance_to(marker)
        frames = _drain(queue)
    finally:
        await source.aclose()
    matching = [f for f in frames if f.event is event_type]
    assert matching, f"replay of {export_dir.name} emitted no {event_type.value} frame"
    return matching[0]


def _gap_fill_frames() -> list[SseEvent]:
    """The five SseEventTypes the recorded fixtures don't naturally drive.

    Emitted through the real :class:`EventBroadcaster` so the frame shape (id +
    envelope) is real, with payloads taken from real ``model_dump()`` /the exact
    controller emit-site dicts — never hand-authored. Each payload mirrors a real
    controller emit site:

    * ``safety_alert`` — controller.py operator-timeout alert dict.
    * ``recovery_acknowledged`` — controller.py ``{"acknowledged": <phase>}``.
    * ``command_failed`` — controller.py ``{"command": ..., "reason": ...}``.
    * ``logs_exported`` — the export manifest ``model_dump`` (api.py emit site).
    * ``heartbeat`` — the API-originated keepalive (empty data).
    """
    from roastpilot_agent.api import EventBroadcaster
    from roastpilot_agent.models import LogManifest, RoastPhase

    broadcaster = EventBroadcaster()
    queue = broadcaster.subscribe()

    # A real safety handshake (the same model the wire carries for fault/recovery)
    # — a REJECT here stands for the operator-timeout safety alert trail.
    alert_eval = SafetyEvaluation(
        rule="operator_timeout",
        verdict=SafetyVerdict.REJECT,
        input_heat=None,
        input_fan=None,
        adjusted_heat=None,
        adjusted_fan=None,
        reason="operator did not confirm charge within the allowed window",
    )
    broadcaster.emit(RoastEventKind.SAFETY_ALERT, alert_eval.model_dump(mode="json"))

    # controller.py operator-acknowledge emit site shape.
    broadcaster.emit(
        RoastEventKind.RECOVERY_ACKNOWLEDGED,
        {"acknowledged": RoastPhase.OPERATOR_RECOVERY_REQUIRED.value},
    )

    # controller.py command-failure emit site shape (a rejected e-stop carries a reason).
    broadcaster.emit(
        RoastEventKind.COMMAND_FAILED,
        {"command": "emergency_stop", "reason": "already faulted"},
    )

    # api.py logs-exported emit site: a real LogManifest model_dump.
    manifest = LogManifest(
        log_dir="/tmp/roast-export",
        jsonl_path="/tmp/roast-export/roast.jsonl",
        csv_path="/tmp/roast-export/roast.csv",
        summary_path="/tmp/roast-export/summary.json",
        ready=True,
        note=None,
    )
    broadcaster.emit(RoastEventKind.LOGS_EXPORTED, manifest.model_dump(mode="json"))

    frames = _drain(queue)
    # The API-originated keepalive: api.py's SSE generator constructs it directly
    # as ``SseEvent(event=SseEventType.HEARTBEAT)`` (event-only, empty data) — not
    # via the broadcaster — so we build it the same way it reaches the wire.
    frames.append(SseEvent(event=SseEventType.HEARTBEAT))
    return frames


def _advisory_variants() -> list[SseEvent]:
    """The advisory shapes the SPA parses beyond the one the replay emits.

    The ``advisory`` event is multiplexed (events.ts ``AdvisoryEventData``): the
    replay CLAMP overlay carries ``decision`` + ``evaluation``; the SPA also folds
    the pause/resume toggle (``advisory_paused``) and a skipped record
    (``skipped``). Those two come from the real controller emit sites
    (controller.py operator_pause_advisory / the no-telemetry skip), emitted
    through the real broadcaster so the SPA's branch parsing is pinned too.
    """
    from roastpilot_agent.api import EventBroadcaster

    broadcaster = EventBroadcaster()
    queue = broadcaster.subscribe()
    # controller.py operator_pause_advisory emit site.
    broadcaster.emit(RoastEventKind.ADVISORY, {"advisory_paused": True})
    # controller.py no-telemetry skip emit site.
    broadcaster.emit(RoastEventKind.ADVISORY, {"trigger": "scheduled", "skipped": "no_telemetry"})
    return _drain(queue)


@pytest_asyncio.fixture
async def all_sse_frames(tmp_path: Path) -> list[SseEvent]:
    """Every SseEventType's real frame, gathered from the real emit path."""
    frames: list[SseEvent] = []
    frames.extend(await _collect_session2_frames(tmp_path))
    frames.append(
        await _collect_first(
            _SESSION_1, tmp_path / "s1.sqlite3", ReplayMarker.FAULT, SseEventType.FAULT
        )
    )
    frames.append(
        await _collect_first(
            _COOLING_COMPLETE,
            tmp_path / "cc.sqlite3",
            ReplayMarker.END,
            SseEventType.RUN_COMPLETED,
        )
    )
    frames.append(
        await _collect_first(
            _FAULT_PRE_T0,
            tmp_path / "fault.sqlite3",
            ReplayMarker.RECOVERY,
            SseEventType.RECOVERY_REQUIRED,
        )
    )
    frames.extend(_advisory_variants())
    frames.extend(_gap_fill_frames())
    return frames


def _select_one_per_type(frames: list[SseEvent]) -> dict[SseEventType, SseEvent]:
    """One representative frame per SseEventType (first seen), for the fixture."""
    by_type: dict[SseEventType, SseEvent] = {}
    for frame in frames:
        by_type.setdefault(frame.event, frame)
    return by_type


@pytest.mark.asyncio
async def test_every_sse_event_type_has_a_real_frame(all_sse_frames: list[SseEvent]) -> None:
    """The frames cover EVERY SseEventType — a new unmapped event type fails here.

    This is the coverage guard: if someone adds an SseEventType and this dump
    isn't extended, the committed fixture is missing it and the vitest can't pin
    its TS mirror. Fail loud at the source."""
    seen = {frame.event for frame in all_sse_frames}
    missing = set(SseEventType) - seen
    assert not missing, f"no real frame captured for: {sorted(m.value for m in missing)}"


@_regen_only
@pytest.mark.asyncio
async def test_write_sse_frame_fixture(all_sse_frames: list[SseEvent]) -> None:
    """Write the committed SSE-frame fixture the vitest contract test loads.

    One representative frame per type, plus the extra ``advisory`` variants
    (the SPA parses several shapes off that one event). Each entry is the real
    ``SseEvent.model_dump`` — ``{event, data, id}``. Writing in a test (not a
    script) keeps the fixture re-derivable and re-validated on every run.
    """
    by_type = _select_one_per_type(all_sse_frames)

    # Frames keyed one-per-type, in the stable SseEventType declaration order.
    primary = [by_type[t].model_dump(mode="json") for t in SseEventType if t in by_type]

    # The advisory-shape variants the SPA also parses (pause toggle, skipped),
    # beyond the representative (the CLAMP overlay) already in `primary`.
    advisory_variants = [
        f.model_dump(mode="json")
        for f in all_sse_frames
        if f.event is SseEventType.ADVISORY and ("advisory_paused" in f.data or "skipped" in f.data)
    ]

    # The `command_executed` shape the SPA's drop-marker branch parses
    # (events.ts / useDashboardEvents reads `data.command === "drop_beans"`),
    # distinct from the heat/fan representative already in `primary`.
    command_variants = [
        f.model_dump(mode="json")
        for f in all_sse_frames
        if f.event is SseEventType.COMMAND_EXECUTED and f.data.get("command") == "drop_beans"
    ]

    payload = {
        "frames": primary,
        "advisory_variants": advisory_variants,
        "command_variants": command_variants,
    }
    _CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
    _SSE_FRAMES_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    # Re-validate: every written frame round-trips through the real SseEvent model
    # (so the fixture can never go stale silently against the model).
    for raw in primary + advisory_variants + command_variants:
        SseEvent.model_validate(raw)

    assert command_variants, "no drop_beans command_executed frame captured"


@_regen_only
@pytest.mark.asyncio
async def test_write_rest_snapshot_fixture(tmp_path: Path) -> None:
    """Write the committed REST-snapshot fixture (RoastDetail + RoastSummary).

    Sourced from the real ``RoastService``: a completed cooling run gives a
    ``RoastDetail`` with a real ``enabled_actions`` projection and a
    ``RoastSummary`` history item. Both are real ``model_dump`` output and are
    re-validated against their models here.
    """
    service, source, _queue = await _subscribed_replay(_COOLING_COMPLETE, tmp_path / "rest.sqlite3")
    try:
        await source.advance_to(ReplayMarker.END)
        assert source.run_id is not None
        detail = await service.detail(source.run_id)
        history = await service.history()
    finally:
        await source.aclose()

    assert history.runs, "completed replay produced no history row"
    summary = next((r for r in history.runs if r.id == detail.id), history.runs[0])

    payload = {
        "roast_detail": detail.model_dump(mode="json"),
        "roast_summary": summary.model_dump(mode="json"),
    }
    _CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
    _REST_SNAPSHOTS_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    # Re-validate against the real models so the fixture can't drift from them.
    RoastDetail.model_validate(payload["roast_detail"])
    RoastSummary.model_validate(payload["roast_summary"])


@pytest.mark.asyncio
async def test_roast_detail_snapshot_carries_enabled_actions(tmp_path: Path) -> None:
    """The committed RoastDetail snapshot must carry enabled_actions.

    The SPA's hydrate path reads ``snapshot.enabled_actions``; a server that
    stopped sending them would silently empty the action bar. Pin it directly so
    the fixture the vitest hydrate test loads always exercises that field.
    """
    service, source, _queue = await _subscribed_replay(_SESSION_2, tmp_path / "ea.sqlite3")
    try:
        assert source.run_id is not None
        detail = await service.detail(source.run_id)
    finally:
        await source.aclose()
    # preheating: the operator can mark beans added / emergency stop, etc.
    assert detail.enabled_actions, "RoastDetail.enabled_actions is empty in preheating"


def test_committed_sse_fixture_matches_models() -> None:
    """The committed SSE fixture on disk still validates against the models.

    Guards against the fixture being edited by hand out of sync with the models
    (it must always be regenerated by the dump tests, never hand-patched).
    """
    raw = json.loads(_SSE_FRAMES_PATH.read_text())
    by_event = {frame["event"] for frame in raw["frames"]}
    assert by_event == {t.value for t in SseEventType}, (
        "committed sse_frames.json is missing/extra event types — regenerate it"
    )
    for frame in raw["frames"] + raw["advisory_variants"] + raw["command_variants"]:
        SseEvent.model_validate(frame)


def test_committed_rest_fixture_matches_models() -> None:
    """The committed REST fixture on disk still validates against the models."""
    raw = json.loads(_REST_SNAPSHOTS_PATH.read_text())
    RoastDetail.model_validate(raw["roast_detail"])
    RoastSummary.model_validate(raw["roast_summary"])
