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

**Continuous server-drift auto-catch (#121).** PR #120 caught SPA-side drift
continuously (the vitest runs against the committed fixture) and a *deliberate*
server rename (the dev regenerates → vitest red), but it could NOT auto-catch an
*accidental* server-side field rename where the dev forgot to regenerate: the
committed fixture went stale and CI stayed green. #121 closes that gap. The
volatile fields (the broadcaster ``id`` sequence, ``elapsed_seconds``, the run
id, ``started_at_utc`` / ``completed_at_utc``) are normalized to fixed sentinels
so a regeneration is byte-deterministic, then a default-on test
(:func:`test_committed_sse_fixture_is_in_sync_with_server` /
:func:`test_committed_rest_fixture_is_in_sync_with_server`) rebuilds the fixture
in memory from the live server and asserts it equals the committed bytes. An
accidental server reshape now fails a plain ``pytest`` run with a "regenerate
with X" hint — no manual regen needed. ``scripts/check_contract_drift.py`` wires
the same regenerate-and-compare into CI as a ``git diff``-style gate.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from roastpilot_agent.models import (
    MicStatus,
    RoastDetail,
    RoastEventKind,
    RoastSummary,
    SseEvent,
    SseEventType,
    TelemetryEventData,
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

# Fixed sentinels for the volatile fields, so a regeneration is byte-deterministic
# (#121). Without these, every regen rewrites the broadcaster `id` sequence, the
# random run id, and the wall-clock timestamps — masking real server drift in the
# `git diff`. The contract these fields *carry* is shape, not value (the SPA looks
# frames up by `event`, never by `id`; it reads timestamps as opaque ISO strings),
# so pinning the value loses no contract coverage while making drift detectable.
_SENTINEL_SSE_ID = 0
_SENTINEL_RUN_ID = "00000000000000000000000000000000"
_SENTINEL_TIMESTAMP = "2026-01-01T00:00:00+00:00"
_SENTINEL_ELAPSED = 0.0

# The hint printed when the committed fixture is out of sync with the server.
_REGEN_HINT = (
    "committed contract fixture is out of sync with the server — a server model "
    "reshaped without the fixture being regenerated (the #115 drift class). "
    "Regenerate with: REGEN_CONTRACT_FIXTURES=1 python -m pytest "
    "tests/test_contract_fixtures.py"
)


# A dumped frame/snapshot is JSON-shaped (model_dump(mode="json")); the fixture
# payloads are JSON objects whose values are frames, frame-lists, or snapshots.
_JsonObj = dict[str, Any]
_FixturePayload = dict[str, Any]


def _normalize_sse_frame(raw: _JsonObj) -> _JsonObj:
    """Pin a dumped SSE frame's volatile fields to fixed sentinels (#121).

    ``id`` is the broadcaster's monotonic sequence (varies with tick count); the
    telemetry frame's ``elapsed_seconds`` is wall-clock-derived. Both are pinned
    so a regeneration is deterministic.

    Every value normalization here guards on **non-null**, never mere key
    existence: a ``null`` is a real contract state these fields can take
    (``id`` is ``int | None`` — the API-built heartbeat carries no id;
    ``elapsed_seconds`` is ``float | None``), so a server regression that turns a
    present value into ``null`` MUST survive normalization to fail the byte
    compare. Key-existence normalization would silently rewrite a present-but-null
    field to the sentinel and mask that drift — and the SPA treats null
    ``elapsed_seconds`` differently (it drops the point from the chart), so the
    distinction is behaviourally meaningful, not cosmetic.

    Args:
        raw: One ``SseEvent.model_dump(mode="json")`` dict.

    Returns:
        The same dict, mutated in place, with volatile fields normalized.
    """
    if raw.get("id") is not None:
        raw["id"] = _SENTINEL_SSE_ID
    data: _JsonObj | None = raw.get("data")
    if data is not None and data.get("elapsed_seconds") is not None:
        data["elapsed_seconds"] = _SENTINEL_ELAPSED
    # #308: the charge-referenced roast clock is wall-clock-derived (volatile)
    # exactly like elapsed_seconds, so pin it to the same sentinel when present.
    # Guarded on non-null so a server regression that nulls it survives to fail
    # the byte compare (it is None before charge — a real contract state).
    if data is not None and data.get("charge_elapsed_seconds") is not None:
        data["charge_elapsed_seconds"] = _SENTINEL_ELAPSED
    return raw


def _normalize_rest_snapshot(raw: _JsonObj) -> _JsonObj:
    """Pin a dumped REST snapshot's volatile fields to fixed sentinels (#121).

    ``id`` is a random run id; ``started_at_utc`` / ``completed_at_utc`` /
    ``first_crack_at_utc`` (#111) are wall-clock. All are pinned so the snapshot
    regenerates deterministically while still exercising the field shape (a
    present, non-null ISO string / id).

    As in :func:`_normalize_sse_frame`, every normalization guards on **non-null**
    so a value→``null`` server regression survives to fail the byte compare. ``id``
    is a non-null ``str`` in the models today (a run always has one), but a
    regression to ``null`` would break the SPA's run keying (detail/history route
    by id), so guarding on non-null is the consistent, defence-in-depth choice
    rather than masking it. ``completed_at_utc`` is legitimately ``null`` for an
    in-progress run, so the non-null guard preserves that real distinction.

    Args:
        raw: A ``RoastDetail`` or ``RoastSummary`` ``model_dump(mode="json")`` dict.

    Returns:
        The same dict, mutated in place, with volatile fields normalized.
    """
    if raw.get("id") is not None:
        raw["id"] = _SENTINEL_RUN_ID
    for key in ("started_at_utc", "completed_at_utc", "first_crack_at_utc"):
        if raw.get(key) is not None:
            raw[key] = _SENTINEL_TIMESTAMP
    return raw


def _serialize(payload: object) -> str:
    """The canonical on-disk form for a contract fixture (stable + diffable)."""
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


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


def _build_sse_payload(all_sse_frames: list[SseEvent]) -> _FixturePayload:
    """The canonical SSE-fixture payload, built from real wire frames (#121).

    One representative frame per ``SseEventType`` (in declaration order), plus the
    extra ``advisory`` shapes (pause toggle, skipped) and the ``drop_beans``
    ``command_executed`` variant the SPA's drop-marker branch parses. Each entry
    is the real ``SseEvent.model_dump`` with its volatile fields normalized
    (:func:`_normalize_sse_frame`), so two regenerations are byte-identical.

    This is the single source of truth for the committed fixture: the regenerate
    test writes its serialization, and the default-on in-sync test compares the
    committed bytes against its serialization. Building it once means the two can
    never disagree about the shape.

    Args:
        all_sse_frames: Every real wire frame from the ``all_sse_frames`` fixture.

    Returns:
        The fixture dict (``frames`` / ``advisory_variants`` / ``command_variants``).
    """
    by_type = _select_one_per_type(all_sse_frames)

    # Frames keyed one-per-type, in the stable SseEventType declaration order.
    primary: list[_JsonObj] = [
        _normalize_sse_frame(by_type[t].model_dump(mode="json"))
        for t in SseEventType
        if t in by_type
    ]

    # The advisory-shape variants the SPA also parses (pause toggle, skipped),
    # beyond the representative (the CLAMP overlay) already in `primary`.
    advisory_variants: list[_JsonObj] = [
        _normalize_sse_frame(f.model_dump(mode="json"))
        for f in all_sse_frames
        if f.event is SseEventType.ADVISORY and ("advisory_paused" in f.data or "skipped" in f.data)
    ]

    # The `command_executed` shape the SPA's drop-marker branch parses
    # (events.ts / useDashboardEvents reads `data.command === "drop_beans"`),
    # distinct from the heat/fan representative already in `primary`.
    command_variants: list[_JsonObj] = [
        _normalize_sse_frame(f.model_dump(mode="json"))
        for f in all_sse_frames
        if f.event is SseEventType.COMMAND_EXECUTED and f.data.get("command") == "drop_beans"
    ]

    assert command_variants, "no drop_beans command_executed frame captured"
    # Re-validate: every frame still round-trips through the real SseEvent model
    # after normalization (so the fixture can never go stale silently vs the model).
    for raw in primary + advisory_variants + command_variants:
        SseEvent.model_validate(raw)

    return {
        "frames": primary,
        "advisory_variants": advisory_variants,
        "command_variants": command_variants,
    }


@_regen_only
@pytest.mark.asyncio
async def test_write_sse_frame_fixture(all_sse_frames: list[SseEvent]) -> None:
    """Write the committed SSE-frame fixture the vitest contract test loads.

    Regenerate-on-demand only (``REGEN_CONTRACT_FIXTURES=1``). Writes the exact
    serialization of :func:`_build_sse_payload`; the default-on in-sync test
    re-derives the same payload and asserts it equals these committed bytes.
    """
    _CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
    _SSE_FRAMES_PATH.write_text(_serialize(_build_sse_payload(all_sse_frames)))


async def _collect_rest_snapshots(tmp_path: Path) -> tuple[RoastDetail, RoastSummary]:
    """Drive a completed cooling replay and return its (detail, summary) snapshots.

    Sourced from the real ``RoastService``: a completed cooling run gives a
    ``RoastDetail`` with a real ``enabled_actions`` projection and a
    ``RoastSummary`` history item. The detail is enriched with a real, non-null
    ``MicStatus`` projection (#197) — the same one ``detail()`` applies live to an
    active run — so the committed snapshot exercises that field shape, not the
    ``None`` branch the flat export would otherwise leave it on.
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
    detail = detail.model_copy(
        update={
            "mic_status": MicStatus.from_first_crack_status(
                status="detected",
                audio_running=True,
                queued_window_count=0,
                emitted_window_count=0,
                dropped_window_count=0,
                processed_window_count=0,
                reason=None,
            )
        }
    )
    return detail, summary


def _build_rest_payload(detail: RoastDetail, summary: RoastSummary) -> _FixturePayload:
    """The canonical REST-fixture payload, with volatile fields normalized (#121).

    Single source of truth shared by the regenerate test and the default-on
    in-sync test, mirroring :func:`_build_sse_payload`.

    Args:
        detail: The real ``RoastDetail`` snapshot (mic-status enriched).
        summary: The matching ``RoastSummary`` history row.

    Returns:
        The fixture dict (``roast_detail`` / ``roast_summary``), normalized.
    """
    payload = {
        "roast_detail": _normalize_rest_snapshot(detail.model_dump(mode="json")),
        "roast_summary": _normalize_rest_snapshot(summary.model_dump(mode="json")),
    }
    # Re-validate against the real models so the fixture can't drift from them.
    RoastDetail.model_validate(payload["roast_detail"])
    RoastSummary.model_validate(payload["roast_summary"])
    return payload


@_regen_only
@pytest.mark.asyncio
async def test_write_rest_snapshot_fixture(tmp_path: Path) -> None:
    """Write the committed REST-snapshot fixture (RoastDetail + RoastSummary).

    Regenerate-on-demand only. Writes the exact serialization of
    :func:`_build_rest_payload`; the default-on in-sync test re-derives the same
    payload and asserts it equals these committed bytes.
    """
    detail, summary = await _collect_rest_snapshots(tmp_path)
    _CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
    _REST_SNAPSHOTS_PATH.write_text(_serialize(_build_rest_payload(detail, summary)))


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


@pytest.mark.asyncio
async def test_telemetry_frame_carries_mic_status(tmp_path: Path) -> None:
    """The live ``telemetry`` SSE frame carries a real ``MicStatus`` (#197).

    The SPA renders the mic icon from ``telemetry.mic_status``; a server that
    stopped projecting it would silently grey the icon. Pin the field on the
    real wire frame (the replay synthesizes a capture-alive status) so the
    fixture the vitest contract test loads always exercises the ``MicStatus``
    shape, and re-validate it through the real model."""
    frames = await _collect_session2_frames(tmp_path)
    telemetry_frames = [f for f in frames if f.event is SseEventType.TELEMETRY]
    assert telemetry_frames, "replay emitted no telemetry frame"
    mic_payloads = [
        f.data["mic_status"] for f in telemetry_frames if f.data.get("mic_status") is not None
    ]
    assert mic_payloads, "no telemetry frame carried a non-null mic_status"
    # Round-trips through both the field's model and its container.
    for raw in mic_payloads:
        MicStatus.model_validate(raw)
    TelemetryEventData.model_validate(telemetry_frames[0].data)


def test_committed_rest_fixture_carries_mic_status() -> None:
    """The committed RoastDetail snapshot pins the ``MicStatus`` shape (#197).

    The SPA's hydrate path reads ``snapshot.mic_status``; pin a non-null, real
    ``MicStatus`` on the committed fixture so its TS mirror is exercised, not
    only the ``None`` branch."""
    raw = json.loads(_REST_SNAPSHOTS_PATH.read_text())
    mic = raw["roast_detail"]["mic_status"]
    assert mic is not None, "committed RoastDetail snapshot has null mic_status — regenerate it"
    MicStatus.model_validate(mic)


def test_committed_sse_fixture_carries_mic_status() -> None:
    """The committed SSE telemetry frame pins the ``MicStatus`` shape (#197)."""
    raw = json.loads(_SSE_FRAMES_PATH.read_text())
    telemetry = next(f for f in raw["frames"] if f["event"] == SseEventType.TELEMETRY.value)
    mic = telemetry["data"]["mic_status"]
    assert mic is not None, "committed telemetry frame has null mic_status — regenerate it"
    MicStatus.model_validate(mic)


def test_committed_sse_fixture_carries_development_time_and_dtr() -> None:
    """The committed SSE telemetry frame pins the development-time + DTR keys (#220).

    The dashboard renders the live ``Development`` timer + ``DTR`` readout from
    ``telemetry.development_elapsed_seconds`` and ``telemetry.development_percent``;
    a server that stopped projecting them would silently blank both. The
    representative frame is pre-first-crack, so the values are ``null`` (the
    readouts show '—'), but the KEYS must be present on the wire shape the SPA's TS
    mirror reads. (DTR is charge-referenced per #219 — see the controller tests for
    the post-FC value semantics.)"""
    raw = json.loads(_SSE_FRAMES_PATH.read_text())
    telemetry = next(f for f in raw["frames"] if f["event"] == SseEventType.TELEMETRY.value)
    data = telemetry["data"]
    assert "development_elapsed_seconds" in data, (
        "committed telemetry frame is missing development_elapsed_seconds — regenerate it"
    )
    assert "development_percent" in data, (
        "committed telemetry frame is missing development_percent — regenerate it"
    )


def test_committed_sse_fixture_carries_charge_elapsed_seconds() -> None:
    """The committed SSE telemetry frame pins the charge-referenced clock key (#308).

    The dashboard renders ROAST TIME (0:00 = charge) from
    ``telemetry.charge_elapsed_seconds`` and re-origins the chart x-axis to charge;
    a server that stopped projecting it would silently break the header re-origin.
    The representative frame is pre-charge, so the value is ``null`` (the header
    shows '—' until the bean is on the drum), but the KEY must be present on the
    wire shape the SPA's TS mirror reads. Distinct from ``elapsed_seconds`` (the
    serve-referenced raw x lead-in), which must also remain present."""
    raw = json.loads(_SSE_FRAMES_PATH.read_text())
    telemetry = next(f for f in raw["frames"] if f["event"] == SseEventType.TELEMETRY.value)
    data = telemetry["data"]
    assert "charge_elapsed_seconds" in data, (
        "committed telemetry frame is missing charge_elapsed_seconds — regenerate it"
    )
    assert "elapsed_seconds" in data, (
        "committed telemetry frame is missing the serve-referenced elapsed_seconds — "
        "regenerate it (it must coexist with charge_elapsed_seconds, #308)"
    )


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


# --- Continuous server-drift auto-catch (#121) -----------------------------
#
# These run on a plain `pytest` (NOT regen-gated). They rebuild the fixture in
# memory from the live server through the same builders the regenerate tests use,
# and assert it equals the committed bytes. An accidental server-side reshape that
# nobody regenerated for now fails CI here — closing the gap PR #120 left open
# (`test_committed_*_matches_models` only catches an edit that breaks model
# validation, not a faithful-but-renamed field; the byte compare catches both).


@pytest.mark.asyncio
async def test_committed_sse_fixture_is_in_sync_with_server(all_sse_frames: list[SseEvent]) -> None:
    """The committed SSE fixture equals a fresh regeneration from the server.

    The continuous half of the #121 guard. Builds the canonical payload from the
    real wire frames and compares it to the committed bytes; a server reshape that
    drifted the fixture (without a regen) fails here with the regenerate hint.
    """
    fresh = _serialize(_build_sse_payload(all_sse_frames))
    committed = _SSE_FRAMES_PATH.read_text()
    assert fresh == committed, _REGEN_HINT


@pytest.mark.asyncio
async def test_committed_rest_fixture_is_in_sync_with_server(tmp_path: Path) -> None:
    """The committed REST fixture equals a fresh regeneration from the server."""
    detail, summary = await _collect_rest_snapshots(tmp_path)
    fresh = _serialize(_build_rest_payload(detail, summary))
    committed = _REST_SNAPSHOTS_PATH.read_text()
    assert fresh == committed, _REGEN_HINT


@pytest.mark.asyncio
async def test_in_sync_guard_fails_on_injected_server_drift(
    all_sse_frames: list[SseEvent],
) -> None:
    """Proof the in-sync guard FAILS on a server-side field rename (#121).

    The #120 bar was "must fail on a deliberate rename, proven once." This proves
    the *continuous* form: simulate an accidental server reshape by renaming a
    field in the freshly-built payload (here ``phase`` → ``agent_phase`` on the
    ``phase_changed`` frame, the literal #115 drift), then assert the in-sync
    byte-compare against the committed fixture goes RED. If this test ever passes,
    the guard has stopped catching drift and is worthless — so it is itself a
    must-fail-on-rename sentinel.
    """
    payload = _build_sse_payload(all_sse_frames)
    frames: list[_JsonObj] = payload["frames"]
    phase_changed = next(f for f in frames if f["event"] == SseEventType.PHASE_CHANGED.value)
    data: _JsonObj = phase_changed["data"]
    assert "phase" in data, "expected a `phase` field to rename"
    # The #115 drift: server renames `phase` → `agent_phase`.
    data["agent_phase"] = data.pop("phase")

    drifted = _serialize(payload)
    committed = _SSE_FRAMES_PATH.read_text()
    assert drifted != committed, (
        "the in-sync guard did NOT detect an injected `phase`→`agent_phase` rename "
        "— it has stopped catching server drift"
    )


def test_normalize_preserves_present_but_null_elapsed_seconds() -> None:
    """A present-but-null ``elapsed_seconds`` is NOT rewritten to the sentinel.

    The guard's value normalizations key on non-null, never key existence. If
    ``elapsed_seconds`` regressed ``float`` → ``null`` server-side, normalizing it
    to ``0.0`` would byte-match the committed fixture and hide the drift — yet the
    SPA treats null elapsed differently (drops the point from the chart), so it is
    a real contract change. This pins that the null survives normalization (so the
    byte compare would catch it), and that a present float is still pinned.
    """
    null_frame = _normalize_sse_frame(
        {"event": "telemetry", "id": 42, "data": {"elapsed_seconds": None}}
    )
    assert null_frame["data"]["elapsed_seconds"] is None, (
        "present-but-null elapsed_seconds was normalized to the sentinel — a "
        "float→null server regression would be masked"
    )
    # The id still normalizes (it was a non-null int).
    assert null_frame["id"] == _SENTINEL_SSE_ID

    value_frame = _normalize_sse_frame(
        {"event": "telemetry", "id": 7, "data": {"elapsed_seconds": 123.4}}
    )
    assert value_frame["data"]["elapsed_seconds"] == _SENTINEL_ELAPSED


def test_normalize_preserves_null_id_fields() -> None:
    """Null ``id`` fields survive normalization (the value→null guard, both sides).

    The heartbeat frame's ``id`` is legitimately null; a REST snapshot ``id``
    regressing to null would break the SPA's run keying. Both must survive
    normalization so the byte compare catches a value→null regression rather than
    rewriting it to the sentinel.
    """
    heartbeat = _normalize_sse_frame({"event": "heartbeat", "id": None, "data": {}})
    assert heartbeat["id"] is None, "null SSE id (heartbeat) was rewritten to the sentinel"

    null_run_id = _normalize_rest_snapshot({"id": None, "started_at_utc": None})
    assert null_run_id["id"] is None, "present-but-null REST id was rewritten to the sentinel"
    # A present run id still normalizes.
    real_run_id = _normalize_rest_snapshot({"id": "deadbeef", "started_at_utc": None})
    assert real_run_id["id"] == _SENTINEL_RUN_ID
