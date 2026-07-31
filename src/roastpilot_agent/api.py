"""FastAPI application: REST + SSE + static SPA mount (component plan §6).

E7 builds the full REST + SSE surface the SPA renders from — one backend
authority: the SPA never calls MCP, it renders from these routes, the typed
SSE event stream, and snapshots. The deterministic controller and the MCP
child that *drive* a live roast are wired into this surface by the E9 vertical
slice; E7 establishes the API contract, the operator action queue, and the
SSE event vocabulary those depend on.

E7-S1 (this module's first slice) covers the REST routes and their typed
response models: health, roast lifecycle start, history/detail reads, the
downsampled telemetry series, the decision-trace timeline, the export-log
manifest + downloads, and operator rating. The operator action queue (S2)
and the SSE stream (S3) extend :class:`RoastService` in place.
"""

import asyncio
import contextlib
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from roastpilot_agent import __version__
from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorDescriptor,
    RoastAdvisor,
    RoastDecision,
)
from roastpilot_agent.bean_sourcing import (
    BEAN_EXTRACTION_PROMPT_VERSION,
    BeanExtractionError,
    BeanExtractionUnavailableError,
    BeanFetchError,
    BeanSourcingDiagnostics,
    draft_bean_profile_from_url,
    redact_url_for_error,
    resolve_extraction_model_slug,
)
from roastpilot_agent.config import AppConfig, MCPDeviceConfig
from roastpilot_agent.config_store import (
    AppConfigEdit,
    AppConfigSnapshot,
    ConfigFileError,
    build_config_snapshot,
    load_app_config,
    load_saved_raw,
    persist_config_edit,
)
from roastpilot_agent.controller import (
    TRANSITION_TABLE,
    CommandExecutor,
    ControllerSnapshot,
    RoastController,
    StateReader,
    TickScheduler,
)
from roastpilot_agent.mcp_client import (
    ExportRoastLogResult,
    MCPConnectionError,
    MCPServerProcess,
    RoastSessionState,
    project_mic_status,
)
from roastpilot_agent.models import (
    ACTIVE_ROAST_PHASES,
    AdvisorHealth,
    AdvisorTraceStatus,
    BeanProfile,
    BeanProfileDraft,
    BeanProfileInput,
    BeanProfileList,
    ChargeWeightRequest,
    ClearStaleSessionRequest,
    ClearStaleSessionResult,
    HealthResponse,
    LogManifest,
    MCPChildStatus,
    MicStatus,
    OperatorAction,
    OperatorActionRequest,
    OperatorActionResult,
    OperatorRatingRequest,
    ReferenceRoast,
    RoastCommand,
    RoastDetail,
    RoastedWeightRequest,
    RoastEventKind,
    RoastEventSource,
    RoastHistory,
    RoastPhase,
    RoastProfile,
    RoastTimeline,
    SseEvent,
    SseEventType,
    TastingEntryRequest,
    TastingList,
    TelemetryEventData,
    TelemetrySeries,
    recording_origin_slug,
)
from roastpilot_agent.safety import (
    OPERATOR_ACTION_COMMAND,
    SafetyEvaluation,
    SafetyPolicy,
    SafetyVerdict,
    enabled_operator_actions,
)
from roastpilot_agent.seed import SEED_BEAN_PROFILES
from roastpilot_agent.store import (
    BeanDraftAttemptAlreadyClaimedError,
    BeanDraftAttemptClaimError,
    BeanProfileNotFoundError,
    PhysicallyImpossibleWeightError,
    RoastStore,
    RunActivelyDrivenError,
)

_log = logging.getLogger(__name__)

#: The route paths the access-log quiet filter targets, as the single source of
#: truth (issue #267). Both the FastAPI route registration in :func:`create_app`
#: and the CLI's ``uvicorn.access`` quiet filter read these, so a future route
#: rename can never silently un-quiet the chatty paths (a drift test pins it).
#:
#: ``TELEMETRY_PATH`` and ``EVENTS_PATH`` carry the ``{run_id}`` template
#: segment. They are matched by the filter as **prefix + suffix** patterns (see
#: ``cli._access_path_matches``) so ``/api/roasts/<any-run-id>/telemetry`` and
#: the per-run SSE stream are caught regardless of the run id — the chatty paths
#: are per-run, so an exact literal would never match a real request line.
HEALTH_PATH = "/api/health"
TELEMETRY_PATH = "/api/roasts/{run_id}/telemetry"
EVENTS_PATH = "/api/roasts/{run_id}/events"

#: The scaffold-fallback ``instance_id`` (#516) — minted once at MODULE IMPORT
#: (which happens once per process, the same "once per process" guarantee
#: ``RoastService.instance_id`` gets from its ``__init__``), for the no-service
#: scaffold app path in :func:`health` below. A per-request mint here would
#: defeat the whole point: every scaffold-mode health poll would report a
#: DIFFERENT instance id, making every health read look like a process change.
_SCAFFOLD_INSTANCE_ID = uuid.uuid4().hex

#: The default access-log quiet-path set (issue #267): the SSE stream, the
#: per-tick telemetry series, and the health poll — the three paths that flood
#: the console during a live roast. Sourced from the route constants above so it
#: cannot drift from the real routes.
DEFAULT_HTTP_ACCESS_LOG_QUIET_PATHS: tuple[str, ...] = (
    EVENTS_PATH,
    TELEMETRY_PATH,
    HEALTH_PATH,
)


#: A roaster control surface satisfies both controller protocols (read + write).
#: The E9 live stack wires either the real ``RoasterControlAdapter`` (over the
#: MCP child) or a test fake here — the controller never sees the MCP client.
class RoasterControl(StateReader, CommandExecutor, Protocol): ...


class LogExporter(Protocol):
    """Exports the roast log at completion (the runner's step 11)."""

    async def export_roast_log(self) -> ExportRoastLogResult:
        """Export logs and return the manifest result."""
        ...


class RawStateSource(Protocol):
    """Supplies the last raw MCP session state for telemetry-row enrichment."""

    @property
    def last_state(self) -> RoastSessionState | None:
        """The most recent raw ``RoastSessionState``, or ``None``."""
        ...


#: Operator actions that resolve to an MCP write command, used for the queue's
#: phase-validity pre-check (E7-S2). Aliased to the canonical map in ``safety.py``
#: (next to the matrix) so the queue pre-check and the ``enabled_actions``
#: derivation share one source of truth. The control actions — ``pause_advisory``
#: / ``resume_advisory`` / ``acknowledge_recovery`` / ``acknowledge_fault`` (#206)
#: — issue no MCP write and so have no matrix entry; they are accepted at the
#: queue and validated by the controller on drain.
_ACTION_COMMAND = OPERATOR_ACTION_COMMAND


class QueuedOperatorAction(BaseModel):
    """One operator action placed on the controller queue (E7-S2).

    The transport item the controller drains each tick (E9): it carries the
    target run, the typed action, and the operator-supplied payload. The
    queue never bypasses safety — the controller re-runs the full policy
    (rate limits, bounds, phase, drop eligibility) before any MCP write; the
    queue's phase pre-check only gives the operator immediate feedback."""

    run_id: str
    action: OperatorAction
    payload: dict[str, Any] | None = None


def _as_event_data(payload: object) -> dict[str, Any]:
    """Coerce a controller event payload to the SSE ``data`` dict.

    Controller emit sites always pass JSON-safe dicts; a non-dict is wrapped
    so the typed envelope always carries a mapping."""
    if isinstance(payload, dict):
        return cast("dict[str, Any]", payload)
    return {"value": payload}


def _phase_changed_with_actions(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a ``phase_changed`` payload with ``enabled_actions`` added.

    The controller emits ``{"phase": <value>}``; this adds the operator actions
    the server would accept in that phase (E10 option (a), D25) — a read-only
    projection over :data:`~roastpilot_agent.safety.COMMAND_PHASE_MATRIX`. A NEW
    dict is returned so the original (buffered for the persisted decision-trace
    timeline) stays lean. A missing/unknown phase — never expected from the
    controller's emit site — passes through unchanged rather than raising on the
    SSE hot path."""
    phase_value = data.get("phase")
    if not isinstance(phase_value, str):
        return data  # pragma: no cover — controller always emits a phase string
    try:
        phase = RoastPhase(phase_value)
    except ValueError:  # pragma: no cover — controller emits valid phase values
        return data
    return {**data, "enabled_actions": [a.value for a in enabled_operator_actions(phase)]}


class EventBroadcaster:
    """Typed SSE event fan-out to connected clients (E7-S3).

    Implements the controller's ``EventEmitter`` protocol — the controller
    (E9) emits agent-level events here — and also accepts the per-tick
    telemetry the API originates. Each SSE connection subscribes a bounded
    queue; :meth:`emit` is synchronous and non-blocking so it never stalls the
    controller tick. A subscriber too slow to keep up overflows its queue and
    the event is dropped for that client only (it resyncs from a snapshot on
    reconnect) — one stalled browser tab can never back-pressure the roast.

    A UI disconnect only removes that subscriber (the SSE generator's cleanup
    calls :meth:`unsubscribe`); it triggers no cooling and no state change,
    structurally — the broadcaster holds no controller, executor, or MCP
    reference, so backend safety continues with no client attached.

    A bounded ring buffer of recent frames (each carrying its monotonic id) backs
    ``Last-Event-ID`` resume (#339): a reconnecting client passes the id of its last applied frame
    to :meth:`subscribe`, and every buffered frame newer than that is pre-loaded
    into its queue before live frames resume, so a backgrounding/network hiccup
    self-heals without losing the discrete fault/recovery/CLAMP/advisory frames in
    the gap. Beyond the buffer's depth the gap is too old to replay losslessly —
    the client still re-bases from the REST snapshot, but events older than the
    oldest buffered id are gone (documented in :meth:`subscribe`).
    """

    #: Ring-buffer depth: recent frames retained for Last-Event-ID
    #: resume. Sized to the same order as ``max_queue`` (1000) — at the 1 s
    #: controller tick that is ~16 min of telemetry plus interleaved events, far
    #: longer than any realistic backgrounding/network hiccup, while staying a
    #: small, fixed-size buffer (one shared frame object per id, O(1) append).
    DEFAULT_REPLAY_BUFFER: int = 1000

    def __init__(self, *, max_queue: int = 1000, replay_buffer: int | None = None) -> None:
        self._subscribers: set[asyncio.Queue[SseEvent]] = set()
        self._max_queue = max_queue
        self._sequence = 0
        # Enforce replay_buffer <= max_queue: subscribe() pre-seeds oldest-first and
        # suppresses QueueFull, so a resumable slice larger than the queue would
        # silently drop the NEWEST gap frames (delivering stale ones and missing the
        # most recent). Keeping the buffer no larger than the queue makes that
        # overflow impossible, so the suppress is only a defensive no-op. An
        # EXPLICIT oversized replay_buffer is a caller error → fail loudly; the
        # default just clamps to max_queue (so a small max_queue still constructs).
        if replay_buffer is not None and replay_buffer > max_queue:
            raise ValueError(
                f"replay_buffer ({replay_buffer}) must not exceed max_queue "
                f"({max_queue}): pre-seeding iterates oldest-first and would drop the "
                "newest gap frames if the resumable slice overflowed the queue."
            )
        effective_replay = (
            replay_buffer
            if replay_buffer is not None
            else min(self.DEFAULT_REPLAY_BUFFER, max_queue)
        )
        # Bounded recent-frame history for Last-Event-ID resume. ``deque`` with a
        # ``maxlen`` evicts the oldest in O(1) on append, so the buffer never grows
        # and the append never stalls the controller tick.
        self._replay_buffer: deque[SseEvent] = deque(maxlen=effective_replay)

    @property
    def subscriber_count(self) -> int:
        """Number of attached SSE connections."""
        return len(self._subscribers)

    @property
    def last_event_id(self) -> int:
        """The id stamped on the most recently published frame (0 before any).

        The same monotonic sequence every ``SseEvent`` carries, so a caller that
        drove the stream synchronously (the replay step surface, E10-S1) can wait
        until a client's ``lastEventId`` has caught up to this value — a
        deterministic settle signal with no arbitrary sleep."""
        return self._sequence

    def subscribe(self, last_event_id: int | None = None) -> asyncio.Queue[SseEvent]:
        """Register a new subscriber queue for one SSE connection.

        Args:
            last_event_id: The id of the last frame the client already applied
                (its ``Last-Event-ID``), or ``None`` for a fresh connection. When
                given, every buffered frame with a strictly greater id is
                pre-loaded into the new queue in order, so a reconnecting client
                replays exactly the gap before live frames resume (#339).

        Returns:
            The subscriber's queue, pre-seeded with the resumable gap when a
            ``last_event_id`` is supplied.

        Note:
            Resume is lossless only within the ring buffer's depth
            (:data:`DEFAULT_REPLAY_BUFFER`). If ``last_event_id`` is older than the
            oldest buffered frame the buffered frames are still replayed, but the
            client is missing the frames between its id and the oldest buffered one
            — it relies on the REST snapshot re-hydration for current state, and
            those discrete in-gap events are unrecoverable. A subscriber too slow
            to drain (queue full) still drops live frames per :meth:`_publish`.
        """
        queue: asyncio.Queue[SseEvent] = asyncio.Queue(maxsize=self._max_queue)
        if last_event_id is not None:
            for event in self._replay_buffer:
                if event.id is not None and event.id > last_event_id:
                    # Pre-seed in order (deque iterates oldest→newest). The
                    # resumable slice is a suffix of the buffer, and the constructor
                    # guarantees replay_buffer <= max_queue, so the slice always fits
                    # the queue — the suppress is a defensive no-op, never the path
                    # that decides which frames survive.
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait(event)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[SseEvent]) -> None:
        """Remove a subscriber (idempotent — safe in a generator's finally)."""
        self._subscribers.discard(queue)

    def _publish(self, event_type: SseEventType, data: dict[str, Any]) -> None:
        # Stamp the sequence at construction (never mutate post-build): one
        # frame object is fanned out to every subscriber, read-only via
        # render(), so no client can observe another's state.
        self._sequence += 1
        event = SseEvent(event=event_type, data=data, id=self._sequence)
        # Retain in the ring buffer for Last-Event-ID resume (O(1), bounded by
        # maxlen — oldest evicted automatically). Done before fan-out so a
        # subscriber that reconnects mid-publish never sees a frame missing from
        # the buffer it would resume against.
        self._replay_buffer.append(event)
        for queue in self._subscribers:
            # The client fell behind; drop rather than block the roast. It
            # resyncs from REST snapshots on reconnect.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    def emit(self, kind: RoastEventKind, payload: object) -> None:
        """``EventEmitter`` protocol: publish a controller event (E9 sink).

        The ``phase_changed`` frame is enriched with ``enabled_actions`` — the
        operator actions the server would accept in the new phase, a read-only
        projection over the command×phase matrix (E10 option (a), D25) — so the
        SPA's action bar updates on every transition without hardcoding a
        client-side matrix. The enrichment builds a NEW payload; the controller's
        original dict (also buffered for the lean decision-trace timeline) is
        untouched."""
        data = _as_event_data(payload)
        if kind is RoastEventKind.PHASE_CHANGED:
            data = _phase_changed_with_actions(data)
        self._publish(SseEventType(kind.value), data)

    def emit_telemetry(self, data: TelemetryEventData) -> None:
        """Publish the per-tick ``telemetry`` event (plan §6)."""
        self._publish(SseEventType.TELEMETRY, data.model_dump(mode="json"))


#: A downloadable export artifact name (plan §6 log manifest), validated
#: against the known artifact set in :meth:`RoastService.log_artifact_path`.
LogArtifactName = str


_BEAN_DRAFT_CANCELLATION_GRACE_SECONDS = 0.5
_BEAN_DRAFT_FINALIZE_TIMEOUT_SECONDS = 1.0
_BEAN_DRAFT_EXPIRY_RETRY_SECONDS = 1.0
_BEAN_DRAFT_EXPIRY_MAX_SLEEP_SECONDS = 60.0
_BEAN_ATTEMPT_LEASE_HEARTBEAT_SECONDS = 30.0


class RoastRunConflictError(Exception):
    """A request conflicts with the current run state (maps to HTTP 409):
    starting a roast while one is active, or rating an in-progress run."""


class BeanDraftAlreadyClaimedConflictError(RoastRunConflictError):
    """A draft id was already claimed using different profile values."""


class RoastRunNotFoundError(Exception):
    """No run (or no requested artifact) matches the id (maps to HTTP 404)."""


class RoastRunGoneError(Exception):
    """An operator action targets a terminal (COMPLETE/FAULTED) run (HTTP 410).

    A completed or faulted run has no live controller loop draining its queue and
    no hot hardware to act on; the action is gone, not merely conflicting."""


@dataclass
class _BeanDraftOperation:
    """One registered bean-draft pipeline and its cancellation reason."""

    task: asyncio.Task[BeanProfileDraft]
    preempted_by_start: bool = False


def _before_the_minute(tasted_at_utc: str, completed_at_utc: str) -> bool:
    """Whether ``tasted_at_utc`` is strictly earlier than ``completed_at_utc``
    at MINUTE precision (#522 round 4).

    The FE's ``datetime-local`` picker cannot express seconds, so comparing
    the two full-precision ISO-8601 strings would 409 an honest "tasted at
    the completion minute" entry whenever ``completed_at_utc`` itself has a
    non-zero seconds/microseconds component (it always does — it is
    ``datetime.now(UTC).isoformat()``). Truncating both to the minute makes
    same-minute read as simultaneous, matching what the operator's input can
    actually express, while still catching any minute-scale (or coarser)
    negative offset.

    Args:
        tasted_at_utc: The (already UTC-normalized) operator-supplied tasting
            instant.
        completed_at_utc: The run's completion instant.

    Returns:
        ``True`` iff ``tasted_at_utc`` falls in a minute strictly before
        ``completed_at_utc``'s minute.
    """
    tasted_minute = datetime.fromisoformat(tasted_at_utc).replace(second=0, microsecond=0)
    completed_minute = datetime.fromisoformat(completed_at_utc).replace(second=0, microsecond=0)
    return tasted_minute < completed_minute


#: Fallback agent-event → source mapping for persisted ``roast_events`` rows.
#: A ``source`` carried in the event payload (e.g. an operator/MCP first-crack,
#: an advisor/operator command) wins over this map; it is the default for events
#: that do not name their origin. Most controller events are the controller's.
_EVENT_SOURCE: dict[RoastEventKind, RoastEventSource] = {
    RoastEventKind.ADVISORY: RoastEventSource.ADVISOR,
    RoastEventKind.FAULT: RoastEventSource.SAFETY,
    RoastEventKind.RECOVERY_REQUIRED: RoastEventSource.SAFETY,
    RoastEventKind.SAFETY_ALERT: RoastEventSource.SAFETY,
    RoastEventKind.T0_DETECTED: RoastEventSource.MCP,
}


@dataclass(frozen=True)
class _BufferedEvent:
    """One controller event captured for deferred store persistence, stamped
    with the controller-clock time it was emitted (not flush time)."""

    kind: RoastEventKind
    payload: object
    monotonic_seconds: float


def _event_source(event: _BufferedEvent) -> RoastEventSource:
    """Resolve the persisted ``source`` for an event: the payload's own
    ``source`` field when present and valid, else the fallback map, else the
    controller."""
    payload = event.payload
    if isinstance(payload, dict):
        named = cast("dict[str, Any]", payload).get("source")
        if isinstance(named, str):
            try:
                return RoastEventSource(named)
            except ValueError:  # pragma: no cover — defensive: emit sites use valid sources
                pass
    return _EVENT_SOURCE.get(event.kind, RoastEventSource.CONTROLLER)


class BufferingEventEmitter:
    """The controller's ``EventEmitter`` for a live run (E9): fan out to SSE
    immediately, buffer for deferred store persistence.

    ``emit`` is synchronous (the controller contract) and cannot ``await`` the
    async ``record_event``; so it forwards to the broadcaster for real-time SSE
    and appends to an in-memory buffer the runner drains and persists at the end
    of each tick. The ``monotonic_seconds`` is captured **at emit time** so the
    decision trace preserves intra-tick ordering (e.g. an operator drop before a
    safety fault in the same tick), not a single flush-time stamp."""

    def __init__(self, broadcaster: "EventBroadcaster", *, clock: Callable[[], float]) -> None:
        self._broadcaster = broadcaster
        self._clock = clock
        self._buffer: list[_BufferedEvent] = []

    def emit(self, kind: RoastEventKind, payload: object) -> None:
        """``EventEmitter`` protocol: publish to SSE now, buffer for persistence."""
        self._broadcaster.emit(kind, payload)
        self._buffer.append(_BufferedEvent(kind, payload, self._clock()))

    def emit_telemetry(self, data: TelemetryEventData) -> None:
        """Publish the per-tick ``telemetry`` SSE frame (not a persisted event)."""
        self._broadcaster.emit_telemetry(data)

    def peek(self) -> list[_BufferedEvent]:
        """A copy of the un-flushed buffer (e.g. to read a fault reason before
        the flush drains it)."""
        return list(self._buffer)

    def drain(self) -> list[_BufferedEvent]:
        """Return and clear the buffered events for persistence."""
        items = self._buffer
        self._buffer = []
        return items


class StoreSnapshotSink:
    """The controller's ``SnapshotSink`` for a live run (E9).

    ``persist_evaluation`` is the decision trace's verdict stream — every safety
    evaluation lands in ``safety_evaluations`` keyed by the current tick.
    ``persist_snapshot`` is a deliberate no-op: the runner persists an enriched
    telemetry row post-tick (with phase, commanded heat/fan, MCP phase, dev %,
    and the raw session dump) rather than the bare projected reading."""

    def __init__(self, store: RoastStore, run_id: str, tick: Callable[[], int]) -> None:
        self._store = store
        self._run_id = run_id
        self._tick = tick

    async def persist_snapshot(self, telemetry: object) -> None:
        return  # the runner owns the enriched telemetry row (see RoastRunner)

    async def persist_evaluation(self, evaluation: SafetyEvaluation) -> int | None:
        return await self._store.record_safety_evaluation(
            run_id=self._run_id, tick=self._tick(), evaluation=evaluation
        )

    async def persist_advisor_decision(
        self,
        *,
        descriptor: AdvisorDescriptor,
        context: AdvisorContext,
        latency_ms: int | None,
        decision: RoastDecision | None,
        status: AdvisorTraceStatus,
        safety_evaluation_id: int | None,
    ) -> None:
        await self._store.record_advisor_decision(
            run_id=self._run_id,
            tick=self._tick(),
            provider=descriptor.provider,
            model=descriptor.model,
            prompt_version=descriptor.prompt_version,
            context=context,
            latency_ms=latency_ms,
            decision=decision,
            status=status,
            safety_evaluation_id=safety_evaluation_id,
        )


class _TickCounter:
    """A shared mutable tick index — the runner advances it; the snapshot sink
    reads it so persisted evaluations carry the same tick as the row."""

    def __init__(self) -> None:
        self.value = 0


class RoastRunner:
    """Drives one live roast: the controller tick loop wired to MCP, the store,
    the operator queue, and the SSE broadcaster (E9 vertical slice).

    Per tick (the documented order): drain the operator queue through the full
    safety policy → run the controller tick → finalize on a terminal phase
    (export logs, complete the run) → flush buffered events to the store →
    publish + persist the telemetry row → persist a phase change. The advisor
    only advises; every roaster write still passes through the controller's
    safety policy. A restart never resumes heat/fan — the runner issues no
    hardware write on construction or recovery."""

    def __init__(
        self,
        *,
        controller: RoastController,
        store: RoastStore,
        emitter: BufferingEventEmitter,
        operator_queue: "asyncio.Queue[QueuedOperatorAction]",
        counter: _TickCounter,
        config: AppConfig,
        run_id: str,
        clock: Callable[[], float],
        exporter: LogExporter | None = None,
        raw_state: RawStateSource | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._controller = controller
        self._store = store
        self._emitter = emitter
        self._queue = operator_queue
        self._counter = counter
        self._config = config
        self._run_id = run_id
        self._clock = clock
        self._exporter = exporter
        self._raw_state = raw_state
        self._sleep = sleep
        self._finalized = False
        # Whether the operator has acknowledged a fault (#206). A fault no longer
        # auto-finalises: the run stays live (loop ticking, heat at 0) until
        # acknowledge_fault flips this and _handle_completion finalises it.
        self._fault_acknowledged = False
        # The fault reason latched on FAULTED entry (#206). A fault finalises on a
        # later tick (after ack), by which point the FAULT event was already flushed
        # from the emitter buffer — so the reason must be captured before the flush.
        self._captured_fault_reason: str | None = None
        self._last_persisted_phase: RoastPhase | None = None
        # Whether the absolute charge/T0 instant has been persisted for this run
        # (#235). Written once, the first tick the controller reports its charge
        # clock stamped, so a later restart→resume can restore the advisory DTR
        # clock. A restore on recovery seeds this True so it is never re-stamped.
        self._t0_persisted = False
        # Whether the ambient (temperature/humidity/pressure) reading has been
        # persisted for this run (#342, D85). Written once, the same tick as
        # ``_t0_persisted`` first goes True (the debounced charge transition) —
        # mirrors that latch's lifecycle exactly, but is its own flag because a
        # `None` ambient reading (probe disabled/unavailable) is itself a valid
        # persisted value and so cannot double as a "not yet written" sentinel
        # the way ``t0_detected_at_utc IS NULL`` does for the T0 write-once guard.
        self._ambient_persisted = False
        self._scheduler: TickScheduler | None = None

    async def start(self, profile: RoastProfile, *, recording_roast_num: int | None = None) -> None:
        """Begin the run: drive the controller's idle→preheating start, then
        flush its startup events and persist the resulting phase. Issues the
        profile's initial heat/fan through the controller's safety policy (never
        raw) — the controller owns that, not the runner.

        Args:
            profile: The roast profile to start.
            recording_roast_num: The store-derived per-origin recording roast
                number (#385), forwarded to the controller for the MCP recording
                filename. ``None`` lets the controller fall back to its per-process
                counter.
        """
        await self._controller.start_run(profile, recording_roast_num=recording_roast_num)
        await self._flush_events()
        await self._persist_phase_if_changed()

    async def recover(
        self,
        profile: RoastProfile,
        persisted_phase: RoastPhase,
        *,
        t0_detected_at_utc: str | None = None,
        ambient_captured: bool = False,
    ) -> None:
        """Restart into recovery: classify the persisted run into
        ``operator_recovery_required`` without resuming heat or fan, persist the
        resulting phase, and flush the recovery events. Issues no MCP write — the
        restart-never-auto-resumes invariant (controller ``recover_from_restart``
        owns the rule; the runner only persists and surfaces it).

        When the persisted run had already charged (``t0_detected_at_utc`` set,
        #235) the advisory DTR clock is restored from that absolute instant so a
        later operator-resume into pre-FC/development keeps a non-zero
        seconds-since-charge denominator. The restore seeds ``_t0_persisted`` so
        the live tick never re-stamps it, and is advisory/display-only — it
        touches no transition, verdict, or hardware write.

        ``ambient_captured`` (#342, D85) mirrors that seeding for the ambient
        triad: restoring the charge clock makes ``snapshot.charge_detected``
        true again on the very next tick, which would otherwise re-fire the
        once-only ambient capture and potentially overwrite a good pre-restart
        corpus reading with a transient post-restart probe hiccup. Seeding
        ``_ambient_persisted`` from the persisted run's already-captured state
        keeps the capture genuinely once-per-run across a restart."""
        self._controller.load_profile(profile)
        if t0_detected_at_utc is not None:
            self._controller.restore_charge_clock(t0_detected_at_utc)
            self._t0_persisted = True
        if ambient_captured:
            self._ambient_persisted = True
        await self._controller.recover_from_restart(persisted_phase)
        await self._flush_events()
        await self._persist_phase_if_changed()

    async def recover_faulted(self, profile: RoastProfile) -> None:
        """Restart into the operable-faulted state for a persisted fault (#206).

        A hard fault (or e-stop) before the restart must NOT offer
        resume-into-roasting (the ``operator_recovery_required`` row permits it).
        This re-enters ``faulted`` via
        :meth:`RoastController.recover_into_faulted`: the loop stays alive, heat
        and fan are NOT auto-resumed (``faulted`` is heat-off), emergency stop and
        engage/stop-cooling remain available, and the run finalises only when the
        operator acknowledges the fault. Issues no MCP write — same
        restart-never-auto-resumes invariant as :meth:`recover`.

        NB (#331): ``recover_on_start`` no longer calls this — it now AUTO-FINALISES
        a stale faulted run instead (see :meth:`recover_on_start`). This method is
        retained DELIBERATELY: it carries the #206 ``_captured_fault_reason`` latch
        (latch BEFORE ``_flush_events`` drains the FAULT event), pinned by
        ``test_recover_faulted_then_acknowledge_preserves_fault_reason`` which now
        exercises it directly. Do NOT delete it as "dead code" — that would drop the
        latch regression coverage. It is also the obvious hook if the operator ever
        decides a restored fault should stay operable-for-cooling rather than
        auto-finalise (the #331 trade)."""
        self._controller.load_profile(profile)
        await self._controller.recover_into_faulted(RoastPhase.FAULTED)
        # Latch before flush — the FAULT event is about to be drained from the
        # emitter buffer, and _captured_fault_reason must survive to finalisation
        # (#206: the normal multi-tick path latches in _handle_completion, but
        # that fires after the flush, so the recover path needs its own latch).
        if self._captured_fault_reason is None:
            self._captured_fault_reason = self._last_fault_reason()
        await self._flush_events()
        await self._persist_phase_if_changed()

    async def shutdown_heat_off(self) -> bool:
        """Command heat off on graceful shutdown, through the safety path (#142).

        A graceful ``serve`` teardown (Ctrl-C / SIGTERM) must not leave the
        Hottop commanded hot with no software control surface left: once the
        process dies the UI Emergency Stop is gone too. So before the MCP child
        is stopped, while it can still receive a write, drive heat to 0 through
        the controller's :meth:`RoastController.operator_emergency_stop` — the
        existing, tested heat-off path that produces a typed
        ``EMERGENCY_STOP`` :class:`~roastpilot_agent.safety.SafetyEvaluation`,
        commands the MCP ``emergency_stop`` write, and faults the run. This is
        an operator-initiated stop, not an auto-resume, so it does not weaken
        the restart→``operator_recovery_required`` invariant (a hard kill that
        skips this still relies on that path).

        Only acts when the controller is in an
        :data:`~roastpilot_agent.models.ACTIVE_ROAST_PHASES` phase — a phase in
        which the machine may be hot with control active. In ``idle`` /
        ``starting`` / ``complete`` / ``faulted`` /
        ``operator_recovery_required`` there is no live heat to command off, so
        this is a no-op (heat is already off or never engaged). The emitted
        events + the ``EMERGENCY_STOP`` evaluation are flushed and the phase
        change persisted so the decision trace records the shutdown stop.

        Returns:
            ``True`` if a heat-off e-stop was issued this call; ``False`` if it
            was a no-op (no active hot phase).
        """
        if self._controller.phase not in ACTIVE_ROAST_PHASES:
            return False
        await self._controller.operator_emergency_stop(reason="graceful shutdown")
        await self._flush_events()
        await self._persist_phase_if_changed()
        return True

    async def record_shutdown_unconfirmed(self, *, command: str, reason: str) -> None:
        """Persist a 'shutdown step did NOT confirm' marker to the trace (#177).

        Records that a fail-closed shutdown step went unacknowledged, so the
        post-roast decision trace makes it unambiguous and a recovery read / a
        human can see it. Two callers, two signals:

        - ``command="shutdown_heat_off"`` — :meth:`RoastService.safe_shutdown_heat_off`
          when the bounded heat-off (including its single retry) timed out or
          errored against a wedged child. The controller already persisted the
          ``EMERGENCY_STOP`` *intent* before the hang, but no confirmation
          landed — the heater may have stayed commanded and the power switch
          was the real stop.
        - ``command="mcp_stop"`` — :meth:`RoastService.record_child_stop_unconfirmed`
          when ``MCPServerProcess.stop_unconfirmed`` is True after teardown: the
          owner exited unexpectedly or teardown did not confirm cleanly.
          Force-termination is best-effort, so the marker records uncertainty,
          not a claim that a kill signal succeeded.

        Written **directly to the store** (not via the emitter buffer): the
        heat-off coroutine that owns the buffer may have just been cancelled by
        a timeout, so a drain is unreliable — a direct ``record_event`` is the
        robust channel. Reuses the existing ``COMMAND_FAILED`` event kind (no
        new SSE event kind, so no cross-boundary FE-contract change) with a
        distinguishing ``command`` / ``unconfirmed`` payload.

        This is **observability for diagnosis / recovery only** — never a
        trigger to auto-act. A restart still enters
        ``operator_recovery_required`` and never auto-resumes heat/fan.

        Fail-safe: a store error here is logged and swallowed — a missing trace
        breadcrumb must never block the rest of teardown.

        Args:
            command: The shutdown step that went unconfirmed
                (``"shutdown_heat_off"`` or ``"mcp_stop"``), stamped on the
                marker payload.
            reason: A short human-readable cause (e.g. ``"timeout"``) stamped on
                the marker payload for the trace.
        """
        try:
            # Deliberate kind choice: COMMAND_FAILED, not a new event kind.
            # (a) An unacknowledged safety command IS a failed command from the
            # trace's point of view; (b) a new kind would force a cross-boundary
            # FE event-kind contract change (the BE/FE parity rule in
            # web/src/lib/contract.test.ts). The payload — context="shutdown",
            # unconfirmed=True, command, reason — disambiguates it from an
            # ordinary tick-time command failure, so the semantics are
            # deliberate, not incidental.
            await self._store.record_event(
                run_id=self._run_id,
                kind=RoastEventKind.COMMAND_FAILED,
                source=RoastEventSource.SAFETY,
                payload={
                    "command": command,
                    "context": "shutdown",
                    "unconfirmed": True,
                    "reason": reason,
                },
            )
        except Exception:  # noqa: BLE001 — fail-safe: a marker write must never block teardown
            _log.error(
                "could not persist the shutdown-unconfirmed marker (command=%s, reason=%s) "
                "— the trace will not record that %s went unacknowledged",
                command,
                reason,
                command,
                exc_info=True,
            )

    @property
    def finalized(self) -> bool:
        """Whether the run reached a terminal phase and was completed."""
        return self._finalized

    def controller_snapshot(self) -> "ControllerSnapshot":
        """The controller's current post-tick snapshot (phase + commanded levels).

        A read-only projection the replay step surface (E10-S1) reads to report
        the settled agent phase / elapsed without touching the store; the
        controller owns the phase, never the caller."""
        return self._controller.snapshot()

    @property
    def current_tick(self) -> int:
        """The current tick index (the runner's shared counter).

        The replay harness (E10-S1) reads it to key its synthesized advisory
        trace to the same tick as the surrounding telemetry rows."""
        return self._counter.value

    async def run(self) -> None:
        """Production loop: tick at the configured interval until the run ends."""
        scheduler = TickScheduler(
            interval_seconds=self._config.controller.tick_interval_seconds,
            tick=self._tick_step,
            clock=self._clock,
            sleep=self._sleep,
        )
        self._scheduler = scheduler
        await scheduler.run()

    async def _tick_step(self) -> None:
        if await self.tick_once() and self._scheduler is not None:
            self._scheduler.stop()

    async def tick_once(self) -> bool:
        """Run one live tick; return whether the run is now finalized.

        Order is load-bearing: drain (operator writes, safety re-checked) →
        controller tick → completion side effects (which emit events) → single
        event flush (last, so terminal events like ``run_completed`` /
        ``logs_exported`` are never emitted-but-unpersisted) → telemetry."""
        self._counter.value += 1
        await self._drain_queue()
        await self._controller.tick()
        finalized = await self._handle_completion()
        await self._flush_events()
        await self._publish_and_persist_telemetry()
        if not finalized:
            await self._persist_phase_if_changed()
        return finalized

    async def _drain_queue(self) -> None:
        """Execute every queued operator action through the controller (which
        re-runs the full safety policy before any MCP write). Emergency stops are
        dispatched first so a queue backlog can never delay an e-stop."""
        batch: list[QueuedOperatorAction] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            # A single service-level queue is shared across runs; drop any item
            # left over from a prior run rather than apply it to this one.
            if item.run_id == self._run_id:
                batch.append(item)
        batch.sort(key=lambda item: item.action is not OperatorAction.EMERGENCY_STOP)
        for item in batch:
            await self._dispatch(item)

    async def _dispatch(self, item: QueuedOperatorAction) -> None:
        """Route one operator action to its controller handler and record the
        executed MCP command (when one was issued) in the decision trace."""
        controller = self._controller
        payload = item.payload or {}
        tool: RoastCommand | None = None
        before = len(self._emitter.peek())
        if item.action is OperatorAction.EMERGENCY_STOP:
            reason = payload.get("reason")
            await controller.operator_emergency_stop(reason if isinstance(reason, str) else None)
            tool = RoastCommand.EMERGENCY_STOP
        elif item.action is OperatorAction.MARK_BEANS_ADDED:
            await controller.operator_mark_beans_added()
            tool = RoastCommand.MARK_BEANS_ADDED
        elif item.action is OperatorAction.MARK_FIRST_CRACK:
            await controller.operator_mark_first_crack()
            tool = RoastCommand.MARK_FIRST_CRACK
        elif item.action is OperatorAction.DROP_BEANS:
            await controller.operator_drop_beans()
            tool = RoastCommand.DROP_BEANS
        elif item.action is OperatorAction.START_COOLING:
            await controller.operator_start_cooling()
            tool = RoastCommand.START_COOLING
        elif item.action is OperatorAction.STOP_COOLING:
            await controller.operator_stop_cooling()
            tool = RoastCommand.STOP_COOLING
        elif item.action is OperatorAction.PAUSE_ADVISORY:
            controller.operator_pause_advisory()
        elif item.action is OperatorAction.RESUME_ADVISORY:
            controller.operator_resume_advisory()
        elif item.action is OperatorAction.ACKNOWLEDGE_RECOVERY:
            await self._dispatch_acknowledge(payload)
        elif item.action is OperatorAction.ACKNOWLEDGE_FAULT:  # pragma: no cover — exhaustive chain
            await self._dispatch_acknowledge_fault(payload)
        if tool is not None:
            await self._record_dispatch_command(tool, item, since=before)

    async def _dispatch_acknowledge(self, payload: dict[str, Any]) -> None:
        """Phase-based ``acknowledge_recovery``: from ``operator_recovery_required``
        it resumes to the ``resume_to`` target in the payload (validated against
        the recovery transition row — ``starting`` is never legal). Any other
        phase records a failed operator action — never a guessed resume, and never
        a reset of a live run.

        A terminal phase is *not* acknowledged here. Post-#206 a fault no longer
        auto-finalises: the faulted run stays operable (loop alive, heat off) until
        the operator acknowledges it via ``acknowledge_fault``
        (:meth:`_dispatch_acknowledge_fault`). Resetting a live faulted run to
        ``idle`` from the drain would leave an ``idle`` run with no
        ``completed_at`` — which ``active_run`` would treat as still active. So an
        ``acknowledge_recovery`` racing an e-stop in the same tick is recorded
        failed; the run becomes operable-faulted, not idle. (Records two
        ``operator_actions`` rows for a control-only action: the queue-acceptance
        row at submit, then this execution-outcome row — the only meaningful
        outcome signal for an action with no matrix pre-check.)"""
        phase = self._controller.phase
        if phase is RoastPhase.OPERATOR_RECOVERY_REQUIRED:
            target = _parse_resume_target(payload.get("resume_to"))
            if target is not None:
                self._controller.operator_resume(target)
                return
        await self._store.record_operator_action(
            action=OperatorAction.ACKNOWLEDGE_RECOVERY.value,
            result="failed",
            run_id=self._run_id,
            payload=payload or None,
        )

    async def _dispatch_acknowledge_fault(self, payload: dict[str, Any]) -> None:
        """Phase-based ``acknowledge_fault`` (#206): from ``faulted`` it flips the
        ``_fault_acknowledged`` flag so :meth:`_handle_completion` finalises the
        run this tick (outcome ``faulted``) and the loop stops; it issues NO MCP
        write (heat is already off in ``faulted`` and stays off). Any other phase
        records a failed operator action — acknowledging a fault is meaningless
        outside ``faulted``.

        Records the execution-outcome ``operator_actions`` row (the queue-
        acceptance row was already written at submit) so the trace carries the
        only meaningful outcome signal for a control-only action with no matrix
        pre-check, mirroring :meth:`_dispatch_acknowledge`."""
        if self._controller.phase is RoastPhase.FAULTED:
            self._fault_acknowledged = True
            # #332: tell the controller too, so THIS tick's latched tick() skips the
            # upward-escalation re-read (a wedged-child read there would otherwise
            # sit between this drain and the same-tick finalise, delaying the
            # acknowledge from clearing — the roast-3 "slow to clear" latency).
            self._controller.note_fault_acknowledged()
            await self._store.record_operator_action(
                action=OperatorAction.ACKNOWLEDGE_FAULT.value,
                result="accepted",
                run_id=self._run_id,
                payload=payload or None,
            )
            return
        await self._store.record_operator_action(
            action=OperatorAction.ACKNOWLEDGE_FAULT.value,
            result="failed",
            run_id=self._run_id,
            payload=payload or None,
        )

    async def _record_dispatch_command(
        self, tool: RoastCommand, item: QueuedOperatorAction, *, since: int
    ) -> None:
        """Record the operator-issued MCP command in ``command_log`` with the
        outcome the controller's events report. A phase-rejected action issues no
        command (its REJECT verdict is already in ``safety_evaluations``) and so
        is not logged here."""
        new_events = self._emitter.peek()[since:]
        executed = any(e.kind is RoastEventKind.COMMAND_EXECUTED for e in new_events)
        failed = any(e.kind is RoastEventKind.COMMAND_FAILED for e in new_events)
        if not executed and not failed:
            return
        await self._store.record_command(
            run_id=self._run_id,
            tick=self._counter.value,
            tool=tool,
            source="operator",
            status="ok" if executed else "failed",
            args=item.payload,
        )

    async def _handle_completion(self) -> bool:
        """Finalise the run on a terminal phase — but a fault no longer
        auto-finalises (#206).

        ``complete`` finalises immediately. ``faulted`` finalises ONLY after the
        operator has acknowledged it (``self._fault_acknowledged``): until then
        the faulted run stays live (the loop keeps ticking and draining the
        queue, heat already forced to 0) so the operator can still engage or stop
        cooling on a physically-running machine — a fault must never strand a hot
        roaster with no software control surface (the #206 loss-of-control gap).

        Returns:
            ``True`` once the run is finalized (the loop should stop);
            ``False`` while it must keep running (no terminal phase, or a faulted
            run awaiting acknowledgement).
        """
        phase = self._controller.phase
        if phase is RoastPhase.FAULTED and self._captured_fault_reason is None:
            # Latch the reason while the FAULT event is still in the emitter
            # buffer — finalisation happens on a later (acknowledge) tick, after
            # the flush has drained it (#206).
            self._captured_fault_reason = self._last_fault_reason()
        finalise = phase is RoastPhase.COMPLETE or (
            phase is RoastPhase.FAULTED and self._fault_acknowledged
        )
        if not finalise:
            return False
        if self._finalized:
            return True
        self._finalized = True
        if phase is RoastPhase.COMPLETE:
            manifest = await self._export_logs()
            await self._store.complete_run(
                run_id=self._run_id,
                outcome="completed",
                agent_phase=phase,
                log_dir=None if manifest is None else manifest.log_dir,
                export_manifest=None if manifest is None else manifest.model_dump(mode="json"),
            )
        else:
            # The reason was latched when the run entered FAULTED (the FAULT
            # event is no longer in the buffer at this later finalise tick, #206);
            # fall back to a live read for a same-tick fault+ack edge case.
            await self._store.complete_run(
                run_id=self._run_id,
                outcome="faulted",
                agent_phase=phase,
                fault_reason=self._captured_fault_reason or self._last_fault_reason(),
            )
        self._last_persisted_phase = phase
        return True

    async def _export_logs(self) -> LogManifest | None:
        """Export the roast log at completion (step 11). Export is a diagnostic
        file write, not a roaster control command, so it does not route through
        the command×phase matrix. A failed export surfaces as ``command_failed``
        and leaves the run completed without a manifest."""
        if self._exporter is None:  # pragma: no cover — production wires the adapter exporter
            return None
        try:
            result = await self._exporter.export_roast_log()
        except Exception:
            self._emitter.emit(RoastEventKind.COMMAND_FAILED, {"command": "export_roast_log"})
            return None
        manifest = LogManifest(
            log_dir=result.log_dir,
            jsonl_path=result.jsonl_path,
            csv_path=result.csv_path,
            summary_path=result.summary_path,
            ready=result.ready,
            note=result.note,
        )
        self._emitter.emit(RoastEventKind.LOGS_EXPORTED, manifest.model_dump(mode="json"))
        return manifest

    def _last_fault_reason(self) -> str | None:
        for event in reversed(self._emitter.peek()):
            payload = event.payload
            if event.kind is RoastEventKind.FAULT and isinstance(payload, dict):
                reason = cast("dict[str, Any]", payload).get("reason")
                if isinstance(reason, str):
                    return reason
        return None  # pragma: no cover — every fault evaluation carries a reason

    async def _flush_events(self) -> None:
        for event in self._emitter.drain():
            try:
                await self._store.record_event(
                    run_id=self._run_id,
                    kind=event.kind,
                    source=_event_source(event),
                    monotonic_seconds=event.monotonic_seconds,
                    payload=event.payload,
                )
            except Exception:
                # One bad row never crashes the safety tick loop or drops a
                # sibling event already delivered to SSE.
                continue

    async def _persist_t0_if_charged(self, snapshot: ControllerSnapshot) -> None:
        """Persist the absolute charge/T0 instant once, when the controller first
        reports its charge clock stamped (#235).

        Restart recovery restores the advisory DTR clock from this timestamp, so
        the charge-referenced roast clock survives a resume instead of resetting
        to ``0.0``. Advisory/display-only — nothing safety-gating reads it. A
        store failure is swallowed (like the event flush): a missing recovery
        breadcrumb only degrades a later resumed run's DTR to the pre-#235
        behaviour, it never affects the live safety tick.

        #337: the controller now backdates its charge clock to the MCP-reported
        turning point, so the persisted instant is derived as
        ``now - charge_elapsed_seconds`` (the snapshot's charge-referenced clock,
        already backdated) rather than the bare persist-tick wall-clock. This
        keeps a resumed run's restored DTR clock consistent with the live one
        instead of re-introducing the ~17 s T0 lag on recovery. A missing /
        non-finite elapsed falls back to "now" (the pre-#337 behaviour)."""
        if self._t0_persisted or not snapshot.charge_detected:
            return
        charged_at_utc = self._backdated_charge_utc(snapshot.charge_elapsed_seconds)
        try:
            await self._store.record_t0_detected_at(self._run_id, t0_detected_at_utc=charged_at_utc)
        except Exception:  # pragma: no cover — fail-safe: a store error on this
            # advisory-only breadcrumb must never crash the safety tick; the only
            # cost is a resumed run's DTR degrading to the pre-#235 behaviour.
            return
        self._t0_persisted = True

    async def _persist_ambient_if_charged(self, snapshot: ControllerSnapshot) -> None:
        """Persist the MCP-owned ambient reading once, at charge (#342, D85).

        Mirrors :meth:`_persist_t0_if_charged` exactly: fires the same tick the
        controller first reports its charge clock stamped, reads the ambient
        triad off the *already-available* raw MCP state (``self._raw_state``,
        the same ``RoastSessionState`` :meth:`_live_mic_status` projects
        ``mic_status`` from — no redundant extra MCP round-trip), and swallows
        any store error so a bad write never crashes the safety tick. Read-only
        corpus metadata: no safety gate, transition, or advisor context ever
        reads the persisted columns, so the only cost of a failure here is a
        run's ambient triad reading back ``None``.

        Only a ``status == "ok"`` reading persists real values; a
        ``"disabled"``/``"unavailable"`` MCP ambient config persists nulls (the
        MCP's own fail-soft contract — never a fault or a recovery)."""
        if self._ambient_persisted or not snapshot.charge_detected:
            return
        state = None if self._raw_state is None else self._raw_state.last_state
        if state is None:
            return
        ambient = state.ambient_status
        temperature_c = ambient.temperature_c if ambient.status == "ok" else None
        humidity_percent = ambient.humidity_percent if ambient.status == "ok" else None
        pressure_hpa = ambient.pressure_hpa if ambient.status == "ok" else None
        try:
            await self._store.set_ambient(
                self._run_id,
                temperature_c=temperature_c,
                humidity_percent=humidity_percent,
                pressure_hpa=pressure_hpa,
            )
        except Exception:
            # Fail-safe: a store error on this corpus-only breadcrumb must never
            # crash the safety tick; the only cost is this run's ambient triad
            # reading back None (see test_ambient_capture_is_fail_soft_on_store_error).
            return
        self._ambient_persisted = True

    @staticmethod
    def _backdated_charge_utc(charge_elapsed_seconds: float | None) -> str | None:
        """Reconstruct the backdated charge UTC from the charge-referenced clock.

        Returns ``now - charge_elapsed_seconds`` as an ISO-8601 UTC string so the
        persisted recovery breadcrumb matches the controller's backdated charge
        clock (#337). ``None`` (defer to the store's own ``now``) when the elapsed
        is absent or non-finite — never fabricate a future or garbage instant.

        Args:
            charge_elapsed_seconds: Seconds since the (backdated) charge from the
                controller snapshot, or ``None`` before charge.

        Timing assumption (advisory-only, accepted): ``charge_elapsed_seconds``
        is read from the *current* snapshot while the reference here is live
        ``datetime.now(UTC)``. On the normal first-charged tick the two are
        same-tick, so the persisted instant is exact. The one drift case — the
        store write fails on the first charged tick and only succeeds on a retry
        AFTER drop (``charge_elapsed_seconds`` freezes at drop via
        ``_effective_now`` while ``now`` keeps advancing) — would persist a charge
        instant later than the true one. That path is itself ``# pragma: no
        cover``-rare (a store failure spanning the drop) and the persisted value
        is ADVISORY-ONLY: no safety or control gate reads it; the only effect is a
        resumed run's DTR *readout* reading slightly short. Accepted rather than
        threading a per-tick wall-clock through the snapshot for a degraded
        readout on a doubly-rare path (claude-review / Augment adjudication, #337).

        Returns:
            The backdated charge instant as an ISO-8601 UTC string, or ``None``.
        """
        if charge_elapsed_seconds is None or not math.isfinite(charge_elapsed_seconds):
            return None
        # `max(0.0, …)` is defensive narrowing: the snapshot's charge-elapsed is
        # `_effective_now() - _charge_monotonic`, and `_effective_now() >=
        # _charge_monotonic` always (charge precedes the current/drop instant, and
        # `_backdated_now` keeps the anchor <= now), so this never clamps.
        elapsed = max(0.0, charge_elapsed_seconds)  # pragma: no cover - unreachable narrowing
        return (datetime.now(UTC) - timedelta(seconds=elapsed)).isoformat()

    async def _publish_and_persist_telemetry(self) -> None:
        snapshot = self._controller.snapshot()
        await self._persist_t0_if_charged(snapshot)
        await self._persist_ambient_if_charged(snapshot)
        telemetry = snapshot.telemetry
        raw = None if self._raw_state is None else self._raw_state.last_state
        if telemetry is not None:
            self._emitter.emit_telemetry(
                TelemetryEventData(
                    agent_phase=snapshot.phase,
                    bean_temp_c=telemetry.bean_temp_c,
                    env_temp_c=telemetry.env_temp_c,
                    bean_ror_c_per_min=telemetry.bean_ror_c_per_min,
                    env_ror_c_per_min=telemetry.env_ror_c_per_min,
                    heat_percent=snapshot.current_heat,
                    fan_percent=snapshot.current_fan,
                    cooling_on=telemetry.cooling_on,
                    elapsed_seconds=snapshot.roast_elapsed_seconds,
                    charge_elapsed_seconds=snapshot.charge_elapsed_seconds,
                    development_elapsed_seconds=snapshot.development_elapsed_seconds,
                    development_percent=snapshot.development_percent,
                    t0_detected=telemetry.t0_detected,
                    first_crack_detected=telemetry.first_crack_detected,
                    mic_status=telemetry.mic_status,
                    ambient_temp_c=telemetry.ambient_temp_c,
                    ambient_humidity_pct=telemetry.ambient_humidity_pct,
                    ambient_pressure_hpa=telemetry.ambient_pressure_hpa,
                )
            )
        await self._store.record_telemetry(
            run_id=self._run_id,
            tick=self._counter.value,
            agent_phase=snapshot.phase,
            elapsed_seconds=snapshot.roast_elapsed_seconds,
            interval_seconds=self._config.controller.telemetry_log_interval_seconds,
            telemetry=telemetry,
            mcp_phase=None if raw is None else raw.phase,
            heat_level_percent=snapshot.current_heat,
            fan_level_percent=snapshot.current_fan,
            # Persist the CONTROLLER's development percent (#308), the single
            # charge/FC-referenced source the advisor also reasons on
            # (snapshot.development_percent ⇐ Controller._development_percent),
            # NOT the MCP raw value. The raw value used the MCP's own FC instant
            # and so disagreed with the advisor's number — the first supervised
            # roast persisted the MCP's ~2 %/5.4 % while the operator-facing dev%
            # must equal what the model sees. The MCP raw figure stays in
            # raw_state_json for diagnosis.
            development_percent=snapshot.development_percent,
            # #308: persist the charge-referenced roast clock alongside the
            # serve-referenced elapsed_seconds so the REST telemetry series can
            # re-origin the chart x-axis at charge on a history/reload read. None
            # before charge; frozen at the drop value in cooling. Display-only.
            charge_elapsed_seconds=snapshot.charge_elapsed_seconds,
            raw_state_json=None if raw is None else raw.model_dump_json(),
        )

    async def _persist_phase_if_changed(self) -> None:
        phase = self._controller.phase
        if phase is self._last_persisted_phase:
            return
        await self._store.update_run_phase(self._run_id, phase)
        self._last_persisted_phase = phase


def _parse_resume_target(value: object) -> RoastPhase | None:
    """Validate a recovery ``resume_to`` payload value: a legal target of the
    ``operator_recovery_required`` transition row (``starting`` excluded)."""
    if not isinstance(value, str):
        return None
    try:
        target = RoastPhase(value)
    except ValueError:
        return None
    if target not in TRANSITION_TABLE[RoastPhase.OPERATOR_RECOVERY_REQUIRED]:
        return None
    return target


class RoastService:
    """Backend authority behind the REST + SSE surface (component plan §6).

    Owns the persistence store, the active-run pointer, and — once wired —
    the MCP child handle used for the health route's liveness field. The
    controller tick loop and live MCP session that advance a roast are
    attached by the E9 vertical slice; the methods here are the seams it
    drives and the read projections the SPA renders from.
    """

    #: Backstop bound on the operator queue. The runner drains the whole queue
    #: every tick, so under normal one-operator use it never approaches this;
    #: the bound only trips on pathological spam, where the offending action is
    #: reported ``failed`` rather than growing memory without limit.
    OPERATOR_QUEUE_MAX = 256

    def __init__(
        self,
        store: RoastStore,
        *,
        config: AppConfig | None = None,
        mcp: MCPServerProcess | None = None,
        sse_heartbeat_seconds: float = 15.0,
        roaster: RoasterControl | None = None,
        advisor: RoastAdvisor | None = None,
        live_serve_mode: bool = False,
        exporter: LogExporter | None = None,
        raw_state: RawStateSource | None = None,
        run_loop: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._config = config or AppConfig()
        self._mcp = mcp
        #: The live roaster control surface (adapter over the MCP child, or a test
        #: fake). When ``None`` the service is API-only (E7 mode): roasts persist a
        #: ``starting`` row but no controller loop drives them.
        self._roaster = roaster
        self._advisor = advisor
        # True only when set by ``build_live_service`` in ``live.py``, meaning both
        # the config and the advisor were produced by the live-serve path and should
        # be refreshed from the reloaded config at each ``start_roast`` (D78
        # apply-next-roast guarantee).  False (default) when the caller injected
        # explicit values — test doubles, replay's custom config + no-advisor None —
        # which must never be replaced on reload.  A single boolean avoids the
        # partial-set footgun of two separate flags (setting one without the other).
        self._live_serve_mode = live_serve_mode
        #: The most recent advisor reachability probe (issue #168), set at
        #: ``serve`` startup via :meth:`set_advisor_health` and surfaced on
        #: ``GET /api/health`` so the dashboard can render an ADVISOR-OFFLINE
        #: state. ``None`` until a probe runs (e.g. the E7 API-only path).
        self._advisor_health: AdvisorHealth | None = None
        self._exporter = exporter
        self._raw_state = raw_state
        self._run_loop = run_loop
        self._clock = clock
        #: Minted ONCE per process, at construction (#516) — never persisted,
        #: never re-minted. See ``HealthResponse.instance_id``'s docstring for
        #: the full port-impostor-defence rationale (#513 follow-up).
        self.instance_id = uuid.uuid4().hex
        #: The active run's live loop, attached by :meth:`start_roast` once a
        #: roaster is wired; ``None`` between runs and in API-only mode.
        self.runner: RoastRunner | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self.active_run_id: str | None = None
        #: SSE keepalive interval (plan §6: 15 s). Injectable so tests observe
        #: a heartbeat without waiting the full interval.
        self.sse_heartbeat_seconds = sse_heartbeat_seconds
        #: The typed SSE event fan-out. In E9 the controller is constructed
        #: with this as its ``event_emitter`` so every agent event reaches the
        #: stream; the controller also calls ``emit_telemetry`` each tick.
        self.events = EventBroadcaster()
        # Serializes the active_run check + create_run insert: without a UNIQUE
        # "at most one open run" schema constraint, two concurrent POST /roasts
        # could both read no-active-run and both insert. One operator + a 1 s
        # tick makes this rare, but the at-most-one-active invariant is held
        # here, not left to chance.
        self._start_lock = asyncio.Lock()
        # Whole fetch+extraction tasks admitted while idle. Registration and
        # roast-start preemption both happen under _start_lock, closing the
        # check/register/start race without holding the lock across remote work.
        self._bean_draft_operations: dict[asyncio.Task[BeanProfileDraft], _BeanDraftOperation] = {}
        self._bean_draft_expiry_wakeup = asyncio.Event()
        self._bean_draft_expiry_task: asyncio.Task[None] | None = None
        # The phase-validity pre-check shares the run's configured safety
        # limits, so the queue's verdict matches the controller's on drain.
        self._safety = SafetyPolicy(self._config.safety)
        #: The controller action queue (plan §6: action → operator_actions row
        #: → controller queue → safety → MCP). The API enqueues phase-valid
        #: actions; the controller tick loop drains and executes them (E9).
        #: Bounded (``OPERATOR_QUEUE_MAX``) so a spammy operator cannot grow it
        #: without limit now that the runner drains it.
        self.operator_queue: asyncio.Queue[QueuedOperatorAction] = asyncio.Queue(
            maxsize=self.OPERATOR_QUEUE_MAX
        )
        # The mcp_device config that the MCP child was most recently spawned
        # with.  Compared against the freshly-reloaded config at each
        # start_roast: when they differ the child is respawned with the new
        # device config so a PUT /api/config mcp_device change applies
        # next-roast without an agent restart (#431).  Initialised to None
        # (service started with no MCP yet); set by set_spawned_mcp_device()
        # once the live-serve path has successfully spawned the child.
        self._spawned_mcp_device: MCPDeviceConfig | None = None

    def mcp_child_status(self) -> MCPChildStatus:
        """Liveness of the coffee-roaster-mcp child for the health route.

        ``not_configured`` is the E7 API-only mode (no child wired yet);
        E9 attaches a real :class:`MCPServerProcess` and this reflects its
        ``running`` flag.
        """
        if self._mcp is None:
            return MCPChildStatus.NOT_CONFIGURED
        return MCPChildStatus.RUNNING if self._mcp.running else MCPChildStatus.STOPPED

    @property
    def advisor(self) -> RoastAdvisor | None:
        """The wired advisor, or ``None`` when none is configured (issue #168).

        Read-only accessor so the ``serve`` entrypoint can run the startup
        reachability probe without reaching into private state; the advisor
        stays advisory-only (the controller still owns the loop).
        """
        return self._advisor

    def set_advisor_health(self, health: AdvisorHealth) -> None:
        """Record the startup advisor reachability probe result (issue #168).

        Set once by the ``serve`` entrypoint after :func:`live.probe_advisor_health`
        runs, so ``GET /api/health`` can surface whether the advisor answered
        before charge. Pure observability — the advisor is advisory-only.

        Args:
            health: The reachability probe result to surface on ``/api/health``.
        """
        self._advisor_health = health

    def set_spawned_mcp_device(self, device_config: MCPDeviceConfig) -> None:
        """Record the device config the MCP child was most recently spawned with.

        Called by :func:`~roastpilot_agent.live.build_live_service` after a
        successful initial spawn, and by the between-roast respawn path in
        :meth:`start_roast` after each respawn.  Provides the baseline that
        :meth:`start_roast` compares the reloaded config against to detect
        device-config drift (#431).

        Args:
            device_config: The :class:`~roastpilot_agent.config.MCPDeviceConfig`
                that was rendered into the MCP yaml for the most recent spawn.
        """
        self._spawned_mcp_device = device_config

    async def _respawn_mcp_for_device_config(self, new_device_config: MCPDeviceConfig) -> None:
        """Stop the running MCP child and restart it with *new_device_config* (#431).

        Called from :meth:`start_roast` when a reloaded ``mcp_device`` section
        differs from :attr:`_spawned_mcp_device`, so a ``PUT /api/config``
        device change applies next-roast without an agent restart.

        **Safety invariant (AGENTS.md):** this method is called only when no
        roast is active — the ``active_run()`` guard in :meth:`start_roast`
        (under ``_start_lock``) runs before this.  A between-roast respawn of an
        idle child never auto-resumes heat or fan: the child simply restarts in a
        clean, heat-off state (the MCP's own start-up posture).  The
        ``operator_recovery_required`` invariant applies to an agent RESTART over
        a POSSIBLY-ACTIVE run; this is a deliberate between-roast respawn where
        the agent owns the full lifecycle and knows the state is idle.

        Fail-closed: if stop or re-start raises, the exception propagates out
        of :meth:`start_roast`. The baseline is invalidated before the sequence
        so a failed restart is retried on the next request. An unconfirmed stop
        instead requires hardware verification and an agent restart (#668).
        Only a successful start records the new baseline.

        Args:
            new_device_config: The new device config to render into the MCP yaml
                on the next spawn.
        """
        if self._mcp is None:
            return  # pragma: no cover - only reached with a live _mcp wired
        _log.info(
            "mcp_device config changed since last spawn — respawning MCP child"
            " with new device config"
        )
        # Invalidate before touching the child so a confirmed-stop restart
        # failure is retried rather than hidden by a stale baseline.
        self._spawned_mcp_device = None
        # stop() bypasses record_child_stop_unconfirmed intentionally: there
        # is no active run to key a marker to, and start() resets the flag.
        await self._mcp.stop()
        # If stop() could not confirm clean teardown, the old process may still
        # be holding the serial port or audio device. Starting a new child into
        # that state risks a resource conflict or a hidden live process. Abort
        # the respawn. After the operator verifies the hardware is inactive, a
        # controlled agent restart clears process-local teardown uncertainty.
        if self._mcp.stop_unconfirmed:
            raise MCPConnectionError(
                "old MCP child teardown was unconfirmed; "
                "aborting respawn - verify the roaster and old MCP child resources are inactive, "
                "restart the agent, then retry"
            )
        self._mcp.set_device_config(new_device_config)
        await self._mcp.start()
        self._spawned_mcp_device = new_device_config  # success → new baseline
        _log.info("MCP child respawned successfully with updated device config")

    async def health(self) -> HealthResponse:
        """Liveness + MCP child status + active run id + advisor health (plan §6).

        Reports the active run from persisted state without mutating the
        in-memory ``active_run_id`` pointer — a GET must not have a write
        side-effect, and once E9 wires the controller loop that pointer is the
        loop's to own, not a health poll's. ``advisor`` carries the startup
        reachability probe (issue #168) when one has run. ``instance_id`` is
        this process's ``self.instance_id`` (#516), minted once at
        construction — see :class:`~roastpilot_agent.models.HealthResponse`'s
        docstring for the port-impostor-defence rationale.
        """
        active = await self._store.active_run()
        return HealthResponse(
            version=__version__,
            instance_id=self.instance_id,
            mcp_child=self.mcp_child_status(),
            active_run_id=None if active is None else active.run_id,
            advisor=self._advisor_health,
        )

    async def start_roast(self, profile: RoastProfile) -> RoastDetail:
        """Start a roast: reload saved config, respawn MCP if needed, persist the run.

        **Config reload (D76/D78 apply-next-roast guarantee):** the saved config
        is re-read from disk at the start of every roast so that a ``PUT
        /api/config`` made between roasts (to a pre-FC heat/fan target, the
        advisor model, or a device setting) drives the *next* roast without
        requiring an agent restart.  The reload happens inside ``_start_lock``
        and only when no run is active, so a running roast's config is never
        mutated mid-loop.

        **MCP device respawn (#431):** when the reloaded ``mcp_device`` section
        differs from the config used at the most recent spawn, the MCP child is
        stopped and restarted with a fresh YAML rendered from the new device
        config.  This makes serial-port, driver, audio-input, and FC-mode
        changes apply next-roast.  The respawn is between-roast only (the
        active-run guard runs first) and never auto-resumes heat or fan.

        **Bean-draft preemption (#657):** drafts register their whole async
        fetch/extraction task under ``_start_lock``. Before any config reload,
        MCP work, or run persistence, start marks and cancels all unfinished
        drafts and waits briefly for cooperative cleanup. That drain is bounded
        so an uncooperative request cannot recreate the long start delay; a
        timeout is logged as explicit best-effort remote cancellation.

        Safety limits are **env-resolved** by :func:`load_app_config` — the
        :func:`~roastpilot_agent.config_store._inject_saved_as_env` injector
        skips the ``ROASTPILOT_SAFETY__`` prefix unconditionally, so no saved
        config file can change a safety limit.  The :class:`SafetyPolicy` is
        always rebuilt from the reloaded (env-wins) :class:`AppConfig`.

        Returns 409 (``RoastRunConflictError``) when a run is already active —
        the API-level guard the controller's idle-only ``start_run`` transition
        mirrors as the inner guard. The persisted run begins in ``starting``;
        the E9 vertical slice drives the MCP session start and the tick loop
        that advances it. The active-run check reads persisted state, so the
        guard holds across an agent restart.
        """
        async with self._start_lock:
            active = await self._store.active_run()
            if active is not None:
                raise RoastRunConflictError(
                    f"a roast is already active (run {active.run_id}, phase "
                    f"{active.agent_phase.value}); end it before starting another"
                )
            await self._preempt_bean_drafts_for_roast_start()
            # Reload effective config from the saved file + env (D76/D78).
            # Only in live-serve mode (``_live_serve_mode=True``, set by
            # ``build_live_service`` in live.py): both the config and the advisor
            # were produced from ``load_app_config`` / ``build_advisor`` at startup
            # and should be refreshed from the saved file + env at each roast start
            # so a PUT /api/config made between roasts takes effect without a
            # restart.
            #
            # Skipped for test doubles, API-only mode, and the replay harness,
            # where the caller passed explicit values that must not be replaced.
            if self._live_serve_mode:
                # Run in a thread: load_app_config does sync file I/O + env
                # snapshot/restore under _ENV_INJECTION_LOCK.  Drop the
                # injected_keys return value (only needed by build_config_snapshot
                # for the env-badge UI).
                fresh_config, _ = await asyncio.to_thread(load_app_config)
                _log.debug(
                    "start_roast: reloaded config (advisor_model_slug=%r)",
                    fresh_config.advisor.model_slug,
                )
                # Safety is always env-resolved; rebuild so self._safety matches
                # self._config (the file injector skips ROASTPILOT_SAFETY__ so the
                # SafetyLimits field values are identical to the startup values,
                # but rebuilding keeps the pairing explicit and invariant-safe).
                fresh_safety = SafetyPolicy(fresh_config.safety)
                # Rebuild the advisor from the fresh config so model/prompt changes
                # apply next-roast (D78).  build_advisor handles a missing API key
                # gracefully (logs a warning, returns None → advisory-paused).
                # Imported lazily to break the circular dependency: live.py imports
                # RoastService from api.py at module level, so a top-level import of
                # build_advisor here would form a cycle (api → live → api).
                from roastpilot_agent.live import (
                    build_advisor,  # noqa: PLC0415 (deliberate lazy import — circular dependency)
                )

                fresh_advisor = build_advisor(fresh_config)
                # Commit all three atomically so the trio is always consistent
                # (guards against a future raising build_advisor leaving _config
                # ahead of _advisor).
                self._config = fresh_config
                self._safety = fresh_safety
                self._advisor = fresh_advisor
                # MCP device respawn (#431): when the reloaded mcp_device differs
                # from what the child was spawned with, stop and restart the child
                # so hardware changes (serial port, driver, audio input, FC mode,
                # ambient sensor mode/device/poll interval (D85, #474), etc.)
                # take effect next-roast without an agent restart.
                #
                # MCP is only wired (non-None) in live-serve mode
                # (build_live_service), so _mcp is not None is already the
                # live-mode guard.  The baseline (_spawned_mcp_device) is
                # None in two cases:
                #   1. Before the first roast: build_live_service calls
                #      set_spawned_mcp_device() right after spawn, so this
                #      is only reachable if the caller skips that step — which
                #      production never does.  A None baseline with a live _mcp
                #      is treated conservatively as "respawn needed".
                #   2. After a failed respawn: the None baseline re-detects drift.
                #      Unconfirmed teardown remains restart-only (#668).
                # Between-roasts guarantee: the active_run() check above (under
                # _start_lock) confirms no roast is active before this block.
                if self._mcp is not None and (
                    self._spawned_mcp_device is None
                    or fresh_config.mcp_device != self._spawned_mcp_device
                ):
                    await self._respawn_mcp_for_device_config(fresh_config.mcp_device)
            run_id = uuid.uuid4().hex
            await self._store.create_run(
                run_id=run_id,
                profile=profile,
                config=self._config,
                agent_phase=RoastPhase.STARTING,
            )
            self.active_run_id = run_id
        if self._roaster is not None:
            await self._begin_live_run(profile, run_id)
        detail = await self._store.read_run(run_id)
        if detail is None:  # pragma: no cover — read immediately after create
            raise RuntimeError(f"read_run returned None for just-created run {run_id}")
        # #516: stamp THIS process's instance_id on the 201 response — the
        # start-roast confirm loop's capture point (see HealthResponse's
        # docstring). A store read carries no process identity; the store
        # layer must stay unaware of this process-scoped value.
        return detail.model_copy(update={"instance_id": self.instance_id})

    async def _begin_live_run(self, profile: RoastProfile, run_id: str) -> None:
        """Construct and start the live controller loop for a run (E9).

        Builds a fresh controller bound to this run id, wired to the roaster
        control surface, the store (via the snapshot sink + the runner's
        telemetry/event persistence), the advisor, and the SSE broadcaster. The
        controller's idle→preheating start runs before returning; the per-tick
        loop runs as a background task when ``run_loop`` is set (tests drive
        ``service.runner.tick_once()`` directly with ``run_loop=False``)."""
        runner = await self._build_runner(run_id, profile)
        if runner is None:  # pragma: no cover — guarded by the caller
            return
        recording_roast_num = await self._recording_roast_num(profile)
        await runner.start(profile, recording_roast_num=recording_roast_num)
        if self._run_loop:
            self._loop_task = asyncio.create_task(runner.run())

    async def _recording_roast_num(self, profile: RoastProfile) -> int | None:
        """The store-derived per-origin recording roast number (#385).

        Prior completed roasts of this profile's origin + 1, so the MCP recording
        filename counter is stable and meaningful across agent restarts (the
        per-process counter reset to 0 each restart, colliding same-bean roasts).

        Best-effort: returns ``None`` when the profile yields no origin slug (the
        controller then skips the metadata call) or when the count query fails for
        any reason — the controller falls back to its per-process counter rather
        than blocking the roast on a recording-naming detail.

        Args:
            profile: The roast profile being started.

        Returns:
            The 1-based per-origin roast number, or ``None`` to defer to the
            controller's per-process fallback.
        """
        origin_slug = recording_origin_slug(profile)
        if origin_slug is None:  # pragma: no cover - slug=None tested at the unit level
            return None
        try:
            prior = await self._store.count_completed_runs_for_origin(origin_slug)
        except Exception:  # pragma: no cover - defensive; never block the roast
            _log.warning(
                "count_completed_runs_for_origin failed (origin=%r); "
                "falling back to the per-process recording roast number",
                origin_slug,
                exc_info=True,
            )
            return None
        return prior + 1

    async def _retrieve_reference_for(self, profile: RoastProfile) -> ReferenceRoast | None:
        """Fail-soft, flag-gated same-bean reference retrieval (#567 Slice B).

        Returns ``None`` immediately when ``reference_curve.enabled`` is
        ``False`` (the default) — no store read happens at all, matching
        today's exact behaviour. When enabled, looks up a completed,
        well-rated past roast of THIS SAME bean (by
        :func:`~roastpilot_agent.models.recording_origin_slug`) via
        :meth:`~roastpilot_agent.store.RoastStore.load_reference_roast`.

        Mirrors :meth:`RoastRunner._persist_t0_if_charged`'s established
        fail-soft shape exactly (design note §6.2, a must-fix, not an
        optional nicety): any store error degrades to ``None`` — the
        pre-#567 behaviour — rather than turning a working ``start_roast``
        (or restart recovery) into a 500. A profile with no derivable
        origin slug (:func:`recording_origin_slug` returns ``None`` — an
        ad-hoc, unsaved profile) is treated the same way, with no store
        call attempted.

        Args:
            profile: The roast profile being started (fresh) or restored
                (recovery) — its identity fields drive the same-bean match
                and its charge weight drives the weight-tolerance filter.

        Returns:
            The best usable :class:`~roastpilot_agent.models.ReferenceRoast`,
            or ``None`` when the flag is off, no qualifying reference
            exists, or the lookup failed.
        """
        if not self._config.controller.reference_curve.enabled:
            return None
        origin_slug = recording_origin_slug(profile)
        if origin_slug is None:
            return None
        try:
            return await self._store.load_reference_roast(origin_slug, profile.bean_weight_grams)
        except Exception:
            # Fail-safe: a store error on this advisory-only context lookup
            # must never block a roast start or recovery; the only cost is
            # degrading to no-reference (today's behaviour). Exercised
            # directly by
            # ``test_reference_curve_retrieval_fail_soft_never_blocks_start``
            # (a real, non-defensive path — this is the design note §6.2
            # must-fix, not unreachable defensive code).
            _log.warning(
                "load_reference_roast failed (origin=%r); starting/recovering with no reference",
                origin_slug,
                exc_info=True,
            )
            return None

    async def _build_runner(self, run_id: str, profile: RoastProfile) -> "RoastRunner | None":
        """Construct a controller + runner bound to ``run_id`` (shared by the
        fresh-start and restart-recovery paths). ``None`` in API-only mode.

        **#567 Slice B:** this is the SINGLE common construction point for
        both a fresh start (``_begin_live_run``) and an agent-restart
        recovery (``recover_on_start``), so it is also the single place the
        same-bean reference is retrieved — once, fresh, fail-soft, flag-
        gated (:meth:`_retrieve_reference_for`) — and cached on the new
        controller instance for the run's lifetime. This satisfies the
        design note's resume rule (§6.5): a resumed run re-retrieves fresh
        rather than trying to persist/restore an in-memory cache across the
        restart boundary, because a fresh controller (and so a fresh
        ``self._reference_roast``) is unconditionally built here on every
        restart-recovery path, exactly as it is on every fresh start.
        """
        roaster = self._roaster
        if roaster is None:  # pragma: no cover — guarded by the caller
            return None
        reference_roast = await self._retrieve_reference_for(profile)
        counter = _TickCounter()
        sink = StoreSnapshotSink(self._store, run_id, lambda: counter.value)
        emitter = BufferingEventEmitter(self.events, clock=self._clock)
        controller = RoastController(
            config=self._config.controller,
            safety=self._safety,
            state_reader=roaster,
            command_executor=roaster,
            snapshot_sink=sink,
            event_emitter=emitter,
            advisor=self._advisor,
            clock=self._clock,
            reference_roast=reference_roast,
        )
        runner = RoastRunner(
            controller=controller,
            store=self._store,
            emitter=emitter,
            operator_queue=self.operator_queue,
            counter=counter,
            config=self._config,
            run_id=run_id,
            clock=self._clock,
            exporter=self._exporter,
            raw_state=self._raw_state,
        )
        self.runner = runner
        return runner

    async def recover_on_start(self) -> None:
        """Restart recovery (orchestration plan § Persistence; architecture
        invariant): a possibly-active persisted run is brought back without ever
        auto-resuming heat or fan and without issuing any MCP write. A terminal
        or idle run needs no recovery. Call once at agent startup before serving;
        a no-op in API-only mode.

        Two non-terminal restart cases, fail-closed both ways:

        * **A persisted ``faulted`` run (#331)** is AUTO-FINALISED to a terminal
          ``faulted`` outcome (``finalize_stale_faulted_run``) rather than restored
          as the active run. A restart is a new session: the fault was already
          handled-or-abandoned last session, the machine was re-initialised, and the
          only in-app action on a restored fault is acknowledge (which just
          finalises it) — so restoring it as active served no operator action and
          stranded the operator on a stale run, blocking a fresh roast (the roast-3
          boot-onto-"test 6" bug). Finalising it lands it in HISTORY (fault_reason
          preserved for diagnosis) and the boot is clean/idle. This resumes nothing
          and issues no MCP write — the restart-never-auto-resumes invariant (about
          actuation) is untouched. (The in-SESSION operable-faulted path, #206 —
          ``recover_faulted`` / ``recover_into_faulted`` — is unchanged; it keeps a
          LIVE fault operable for cooling within a session, distinct from this
          cross-restart stale-fault case.)
        * **An active-roast phase** (preheating / pre-FC / development / cooling)
          enters ``operator_recovery_required`` via :meth:`RoastRunner.recover`,
          where explicit operator action (resume/drop/cool/end) is required and
          emergency stop stays available. UNCHANGED by #331.
        """
        if self._roaster is None:
            return
        persisted = await self._store.read_latest_run()
        if persisted is None or persisted.completed_at_utc is not None:
            return  # fresh database, or a terminal run — nothing possibly active
        if persisted.agent_phase in (RoastPhase.IDLE, RoastPhase.COMPLETE):
            return
        if persisted.agent_phase is RoastPhase.FAULTED:
            # #331: a prior session's UNFINALISED faulted run must NOT be restored
            # as the active run — that strands the operator on a stale fault (e.g.
            # roast 3 booting onto the 14-Jun "test 6" fault) and blocks a fresh
            # roast. A restart is a NEW session: the machine was re-initialised, the
            # fault was already handled-or-abandoned last session, and the only
            # in-app action on a restored fault is acknowledge (which just finalises
            # it) — so re-entering it as active serves no operator action. Finalise
            # it terminally (outcome ``faulted``, fault_reason PRESERVED for
            # diagnosis) so it lands in HISTORY and the boot is clean/idle, ready for
            # a new roast. This is a store write only — it resumes nothing, issues no
            # MCP write, and never touches heat/fan, so the restart-never-auto-resumes
            # invariant (about actuation) is untouched. NB: the in-SESSION
            # operable-faulted path (#206) is unchanged — that keeps a LIVE fault
            # operable for cooling within the session; this only handles the
            # cross-restart STALE fault.
            await self._store.finalize_stale_faulted_run(persisted.run_id)
            return
        runner = await self._build_runner(persisted.run_id, persisted.profile)
        if runner is None:  # pragma: no cover — guarded above
            return
        self.active_run_id = persisted.run_id
        await runner.recover(
            persisted.profile,
            persisted.agent_phase,
            t0_detected_at_utc=persisted.t0_detected_at_utc,
            ambient_captured=persisted.ambient_captured,
        )
        if self._run_loop:
            self._loop_task = asyncio.create_task(runner.run())

    async def safe_shutdown_heat_off(self, *, timeout_seconds: float = 5.0) -> bool:
        """Drive heat off through the safety path on graceful shutdown (#142).

        Called by the live-serve teardown **before** the MCP child is stopped —
        the write must land while the child is still alive. Delegates to
        :meth:`RoastRunner.shutdown_heat_off`, which routes through the
        controller's ``operator_emergency_stop`` (heat→0 + a typed
        ``EMERGENCY_STOP`` evaluation). A no-op in API-only mode or when no live
        run is active.

        Bounded and fail-closed: the heat-off write is wrapped in a short
        :func:`asyncio.wait_for` so a wedged MCP child can never hang shutdown.
        On a timeout or error the write is **retried once** before giving up
        (#177): the first attempt's cancellation propagates out of the
        controller before it transitions to ``FAULTED``, so the run is still in
        an active hot phase and the retry meaningfully re-attempts the write. If
        the retry also fails, a 'shutdown heat-off did NOT confirm' marker is
        persisted to the decision trace (:meth:`RoastRunner.record_shutdown_unconfirmed`)
        so post-roast it is unambiguous the commanded stop went unacknowledged.
        Every failure is logged loudly (the operator must know the stop did not
        confirm and may need the power switch) and swallowed so the rest of
        teardown — including ``mcp.stop`` — still runs.

        Args:
            timeout_seconds: Upper bound on EACH heat-off write attempt before
                shutdown proceeds (default 5 s — generous for one MCP call,
                short enough never to wedge a Ctrl-C). Applied to both the first
                attempt and the single retry, so worst-case bound is ~2×.

        Returns:
            ``True`` if the heat-off safety path ran and the controller faulted
            (the e-stop was dispatched through ``operator_emergency_stop``).
            Note this is *not* a hardware acknowledgement: if the MCP
            ``emergency_stop`` write itself fails, ``operator_emergency_stop``
            still emits ``COMMAND_FAILED``, faults the run, and reports success
            here — fail-safe is the controller's job, not the caller's.
            ``False`` if it was a no-op (no active run / already hardware-off) or
            the safety path did not run to completion after the retry (the
            ``wait_for`` timeout or an unexpected error, logged + a trace marker
            persisted + swallowed).
        """
        runner = self.runner
        if runner is None:
            return False
        # One retry on a wedged-child timeout before giving up (#177): the
        # first attempt's cancel propagates out of the controller BEFORE it
        # transitions to FAULTED, so the run is still in an active hot phase
        # and the retry meaningfully re-attempts the heat-off write rather than
        # short-circuiting as a no-op. A second timeout is the give-up point.
        #
        # Narrow benign branch: if a NON-timeout first-attempt error originates
        # AFTER the controller already transitioned to FAULTED, the retry's
        # phase guard makes it a no-op returning False with NO marker. That is
        # fine — the run is already FAULTED and the e-stop was dispatched, so
        # there is no still-hot ambiguity to record. The dangerous still-hot
        # case is the timeout path, which IS marked.
        try:
            return await asyncio.wait_for(runner.shutdown_heat_off(), timeout=timeout_seconds)
        except TimeoutError:
            _log.error(
                "SHUTDOWN heat-off did not confirm within %.1fs — retrying once before "
                "giving up; the roaster may still be commanded hot",
                timeout_seconds,
            )
        except Exception:  # noqa: BLE001 — fail closed: log loudly, never block teardown
            _log.error(
                "SHUTDOWN heat-off failed — retrying once before giving up; "
                "the roaster may still be commanded hot",
                exc_info=True,
            )
        try:
            return await asyncio.wait_for(runner.shutdown_heat_off(), timeout=timeout_seconds)
        except TimeoutError:
            _log.error(
                "SHUTDOWN heat-off did not confirm within %.1fs on retry — the roaster may "
                "still be commanded hot; use the Hottop power switch if needed",
                timeout_seconds,
            )
            await runner.record_shutdown_unconfirmed(command="shutdown_heat_off", reason="timeout")
            return False
        except Exception:  # noqa: BLE001 — fail closed: log loudly, never block teardown
            _log.error(
                "SHUTDOWN heat-off failed on retry — the roaster may still be commanded "
                "hot; use the Hottop power switch if needed",
                exc_info=True,
            )
            await runner.record_shutdown_unconfirmed(command="shutdown_heat_off", reason="error")
            return False

    async def shutdown(self) -> None:
        """Stop the live loop and release the background task (clean teardown /
        agent restart). Idempotent; safe in API-only mode."""
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        expiry_task = self._bean_draft_expiry_task
        self._bean_draft_expiry_task = None
        if expiry_task is not None and not expiry_task.done():
            expiry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await expiry_task
        await self._store.clear_unclaimed_bean_sourcing_drafts(owner_instance_id=self.instance_id)

    async def record_child_stop_unconfirmed(self, *, stop_unconfirmed: bool) -> None:
        """Persist a marker if the MCP child stop went unconfirmed (#177).

        Called by the live-serve teardown **after** ``mcp.stop`` (so the
        clean-teardown verdict is known) and **before** ``store.close`` (so the
        store is still open to write to). When
        ``MCPServerProcess.stop_unconfirmed`` is True, the owner exited
        unexpectedly or teardown did not confirm cleanly. This records that
        uncertainty in the decision trace for post-roast diagnosis. A no-op
        when teardown confirmed cleanly or there is no live runner to key the
        marker to (API-only / never started).

        Observability only — never an auto-resume trigger (a restart still
        enters ``operator_recovery_required``). Fail-closed: delegates to the
        runner's swallow-on-error marker write, so it can never abort teardown.

        Args:
            stop_unconfirmed: ``MCPServerProcess.stop_unconfirmed`` after
                ``mcp.stop`` — whether clean child teardown was unconfirmed.
        """
        if not stop_unconfirmed:
            return
        runner = self.runner
        if runner is None:
            return
        _log.error(
            "MCP child teardown went UNCONFIRMED — recording a trace marker; "
            "a restart will enter operator_recovery_required"
        )
        await runner.record_shutdown_unconfirmed(
            command="mcp_stop", reason="child_stop_unconfirmed"
        )

    async def history(self) -> RoastHistory:
        """The roast history list, newest first (plan §6)."""
        return RoastHistory(runs=await self._store.list_runs())

    async def detail(self, run_id: str) -> RoastDetail:
        """Run detail, or 404 (plan §6).

        For the *active* run the response is enriched with the live capture-alive
        ``mic_status`` (#197) projected from the MCP first-crack status — the
        store has no persisted mic status (it is transient), so historical runs
        carry ``None``. A pure read-only projection, mirroring the
        ``enabled_actions`` server-derived precedent (D25)."""
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        # Only a live (not-yet-completed) run is enriched. ``active_run_id`` is
        # set on start/recovery and not cleared at finalize, so a just-completed
        # run can still match the active pointer with ``raw_state.last_state``
        # populated; the persisted ``completed_at_utc`` is the authoritative
        # "this run is history" signal and history carries ``None`` (#200/Codex).
        if detail.completed_at_utc is None:
            mic_status = self._live_mic_status(run_id)
            if mic_status is not None:
                detail = detail.model_copy(update={"mic_status": mic_status})
        return detail

    async def _preempt_bean_drafts_for_roast_start(self) -> None:
        """Cancel idle-admitted bean drafts before a roast is persisted."""
        operations = tuple(
            operation
            for operation in self._bean_draft_operations.values()
            if not operation.task.done()
        )
        for operation in operations:
            operation.preempted_by_start = True
            operation.task.cancel()
        if not operations:
            return

        _, pending = await asyncio.wait(
            tuple(operation.task for operation in operations),
            timeout=_BEAN_DRAFT_CANCELLATION_GRACE_SECONDS,
        )
        if pending:
            _log.error(
                "roast start proceeding after %.3gs bean-draft cancellation grace "
                "with %d local task(s) still pending; cancellation is best-effort "
                "for remote provider work",
                _BEAN_DRAFT_CANCELLATION_GRACE_SECONDS,
                len(pending),
            )

    def _live_mic_status(self, run_id: str) -> MicStatus | None:
        """The active run's live capture-alive mic status, or ``None`` (#197).

        ``None`` for any non-active run, when no roaster/raw-state source is
        wired (API-only mode), or before the first MCP read. Read-only — never
        a write or a safety evaluation. The caller additionally suppresses this
        for completed runs (``completed_at_utc``), since the active pointer is
        not cleared at finalize."""
        if run_id != self.active_run_id or self._raw_state is None:
            return None
        state = self._raw_state.last_state
        if state is None:
            return None
        return project_mic_status(state.first_crack_status)

    async def telemetry(self, run_id: str, *, downsample: int) -> TelemetrySeries:
        """The downsampled telemetry series for a run, or 404 (plan §6)."""
        if await self._store.read_run(run_id) is None:
            raise RoastRunNotFoundError(run_id)
        points = await self._store.read_telemetry_points(run_id, downsample=downsample)
        return TelemetrySeries(
            run_id=run_id,
            downsample=downsample,
            point_count=len(points),
            points=points,
        )

    async def timeline(self, run_id: str) -> RoastTimeline:
        """The decision-trace timeline for a run, or 404 (plan §6)."""
        if await self._store.read_run(run_id) is None:
            raise RoastRunNotFoundError(run_id)
        return await self._store.read_timeline(run_id)

    async def log_manifest(self, run_id: str) -> LogManifest:
        """The export-log manifest for a run.

        404 when the run is unknown *or* has no export manifest yet (the
        export runs at roast completion via the MCP client; an in-progress or
        never-exported run carries none).
        """
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        if detail.export_manifest is None:
            raise RoastRunNotFoundError(f"run {run_id} has no export manifest")
        return detail.export_manifest

    async def log_artifact_path(self, run_id: str, artifact: LogArtifactName) -> Path:
        """Resolve a downloadable export artifact to its on-disk path, or 404.

        404 covers every missing case: unknown run, no manifest, the export
        not marked ``ready``, or the file absent on disk — the API never
        streams a half-written or stale export.
        """
        manifest = await self.log_manifest(run_id)
        paths: dict[str, str] = {
            "jsonl": manifest.jsonl_path,
            "csv": manifest.csv_path,
            "summary": manifest.summary_path,
        }
        if artifact not in paths:
            raise RoastRunNotFoundError(f"unknown log artifact {artifact!r}")
        if not manifest.ready:
            raise RoastRunNotFoundError(f"run {run_id} export is not ready")
        path = Path(paths[artifact])
        if not path.is_file():
            raise RoastRunNotFoundError(f"run {run_id} {artifact} file is not available")
        return path

    async def rate(self, run_id: str, rating: OperatorRatingRequest) -> RoastDetail:
        """Record the operator self-rating, or 404/409 (plan §6).

        404 when the run is unknown; 409 when it is still in progress — a
        rating is one of the explicit immutability exceptions the store allows
        only on completed runs, so the API surfaces the in-progress case as a
        conflict rather than letting the store's RuntimeError escape as a 500.
        """
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        if detail.completed_at_utc is None:
            raise RoastRunConflictError(
                f"run {run_id} is still in progress; rate it after completion"
            )
        await self._store.set_operator_rating(run_id, rating=rating.stars, notes=rating.notes)
        rated = await self._store.read_run(run_id)
        if rated is None:  # pragma: no cover — immutable once completed
            raise RuntimeError(f"read_run returned None for rated run {run_id}")
        return rated

    async def set_roasted_weight(self, run_id: str, request: RoastedWeightRequest) -> RoastDetail:
        """Record the operator-entered roasted-out weight (#388), or 404/409.

        Mirrors :meth:`rate`: 404 when the run is unknown; 409 when it is still in
        progress — the roasted weight is a completed-run immutability exception
        (entered post-weighing), so the in-progress case surfaces as a conflict
        rather than letting the store's RuntimeError escape as a 500. A roasted
        weight above the EFFECTIVE charge weight is physically impossible (a
        tare/scale error) and is rejected as a 409 too, rather than persisted as
        a row whose derived loss reads as null/"unweighed". The response carries
        the derived ``weight_loss_percent``.

        The bound is against ``corrected_charge_grams`` when present, else the
        frozen ``profile.bean_weight_grams`` (#520 safety review): bounding only
        against the frozen weight would let a charge correction BELOW the
        roasted weight slip through if it landed first (correct charge to 200g
        on an un-weighed run, then weigh 210g — 210 > the frozen 250g default
        passes, but 210 > the effective 200g charge is impossible) — the same
        physical-impossibility guard :meth:`set_charge_weight` enforces in the
        other direction, now symmetric regardless of which correction is
        entered first.

        This pre-check reads ``detail`` and then writes moments later — a
        concurrent :meth:`set_charge_weight` landing in between could move the
        effective charge out from under it. The store's own UPDATE carries
        the identical bound in its ``WHERE`` clause (#520 round-2 P3), so a
        :class:`~roastpilot_agent.store.PhysicallyImpossibleWeightError` from
        that atomic check is the real backstop; this pre-check exists only to
        give the common (non-racing) case a precise 409 message instead of
        the store's generic one.
        """
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        if detail.completed_at_utc is None:
            raise RoastRunConflictError(
                f"run {run_id} is still in progress; record its weight after completion"
            )
        effective_charge_grams = detail.corrected_charge_grams or detail.profile.bean_weight_grams
        if request.roasted_weight_grams > effective_charge_grams:
            raise RoastRunConflictError(
                f"roasted weight {request.roasted_weight_grams} g exceeds the charge "
                f"weight {effective_charge_grams} g (physically impossible)"
            )
        try:
            await self._store.set_roasted_weight(
                run_id, roasted_weight_grams=request.roasted_weight_grams
            )
        except PhysicallyImpossibleWeightError as exc:
            raise RoastRunConflictError(
                f"roasted weight {request.roasted_weight_grams} g exceeds run {run_id}'s "
                f"current charge weight (physically impossible)"
            ) from exc
        weighed = await self._store.read_run(run_id)
        if weighed is None:  # pragma: no cover — immutable once completed
            raise RuntimeError(f"read_run returned None for weighed run {run_id}")
        return weighed

    async def set_charge_weight(self, run_id: str, request: ChargeWeightRequest) -> RoastDetail:
        """Record an operator CHARGE-weight correction (#520), or 404/409.

        Mirrors :meth:`set_roasted_weight`: 404 when the run is unknown; 409
        when it is still in progress — the same completed-run immutability
        exception lifecycle. The frozen ``profile.bean_weight_grams`` is NEVER
        mutated (it is what the controller/advisor actually ran with); the
        corrected value lands in the separate ``corrected_charge_grams``
        column and drives ``weight_loss_percent`` in its place.

        A corrected charge BELOW the roasted-out weight is physically
        impossible (the beans cannot weigh more roasted than they weighed
        green) and is rejected as a 409, mirroring
        :meth:`set_roasted_weight`'s own bound in the other direction. When no
        roasted weight has been entered yet there is nothing to bound
        against, so any positive correction is accepted (the model's own
        ``gt=0`` already guards non-positive input).

        Records an audit event (``operator_actions``, action
        ``"charge_weight_correction"``) with the before/after values — the
        existing free-form ``operator_actions.action`` text column, not a new
        ``RoastEventKind`` member (no D15/enum-CHECK-rebuild surface for a
        one-off audit record).

        Like :meth:`set_roasted_weight`, this pre-check reads ``detail`` and
        writes moments later — a concurrent :meth:`set_roasted_weight` landing
        in between could move ``roasted_weight_grams`` out from under it. The
        store's own UPDATE carries the identical bound in its ``WHERE``
        clause (#520 round-2 P3) as the real, atomic backstop.
        """
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        if detail.completed_at_utc is None:
            raise RoastRunConflictError(
                f"run {run_id} is still in progress; correct its charge weight after completion"
            )
        if (
            detail.roasted_weight_grams is not None
            and request.corrected_charge_grams < detail.roasted_weight_grams
        ):
            raise RoastRunConflictError(
                f"corrected charge weight {request.corrected_charge_grams} g is below the "
                f"roasted-out weight {detail.roasted_weight_grams} g (physically impossible)"
            )
        previous_charge = detail.corrected_charge_grams or detail.profile.bean_weight_grams
        try:
            await self._store.set_corrected_charge(
                run_id, corrected_charge_grams=request.corrected_charge_grams
            )
        except PhysicallyImpossibleWeightError as exc:
            raise RoastRunConflictError(
                f"corrected charge weight {request.corrected_charge_grams} g is below run "
                f"{run_id}'s current roasted-out weight (physically impossible)"
            ) from exc
        await self._store.record_operator_action(
            action="charge_weight_correction",
            run_id=run_id,
            payload={
                "previous_charge_grams": previous_charge,
                "corrected_charge_grams": request.corrected_charge_grams,
            },
            result="accepted",
        )
        corrected = await self._store.read_run(run_id)
        if corrected is None:  # pragma: no cover — immutable once completed
            raise RuntimeError(f"read_run returned None for corrected run {run_id}")
        return corrected

    async def discard_roast(self, run_id: str) -> RoastDetail:
        """Soft-exclude a completed roast as bad-data (#582), or 404/409.

        Mirrors :meth:`rate`: 404 when the run is unknown; 409 when it is
        still in progress — the exclude flag is a completed-run immutability
        exception (a discard-worthy data problem is only knowable once the
        roast has run its course), so the in-progress case surfaces as a
        conflict rather than letting the store's ``RuntimeError`` escape as a
        500. Idempotent: discarding an already-discarded run is a no-op 200,
        not an error (the SPA can call it without first checking state).
        Nothing is deleted — the run row, telemetry, events, and any exported
        audio are untouched; the run simply drops out of
        :meth:`history` / corpus retrieval until :meth:`restore_roast`.
        """
        return await self._set_excluded(run_id, excluded=True)

    async def restore_roast(self, run_id: str) -> RoastDetail:
        """Reverse a discard (#582), or 404/409. See :meth:`discard_roast`."""
        return await self._set_excluded(run_id, excluded=False)

    async def _set_excluded(self, run_id: str, *, excluded: bool) -> RoastDetail:
        """Shared discard/restore body (#582) — the store write + 404/409 mapping
        both :meth:`discard_roast` and :meth:`restore_roast` share."""
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        if detail.completed_at_utc is None:
            verb = "discard" if excluded else "restore"
            raise RoastRunConflictError(
                f"run {run_id} is still in progress; {verb} it after completion"
            )
        await self._store.set_run_excluded(run_id, excluded=excluded)
        updated = await self._store.read_run(run_id)
        if updated is None:  # pragma: no cover — immutable once completed
            raise RuntimeError(f"read_run returned None for updated run {run_id}")
        return updated

    #: Floor on the guard (c) telemetry-recency window (#525), regardless of
    #: the configured ``telemetry_log_interval_seconds``. Scaled UP by
    #: ``_stale_session_recency_window_seconds`` for a longer interval, never
    #: down — a short/default interval still gets a comfortable multi-tick
    #: margin, and a misconfigured LONG interval cannot quietly shrink the
    #: safety margin below this floor.
    STALE_SESSION_RECENCY_FLOOR_SECONDS = 20.0

    def _stale_session_recency_window_seconds(self) -> float:
        """The guard (c) telemetry-recency window: 4 ticks of margin at the
        configured logging cadence, floored at
        :attr:`STALE_SESSION_RECENCY_FLOOR_SECONDS` (#525)."""
        return max(
            self.STALE_SESSION_RECENCY_FLOOR_SECONDS,
            4.0 * self._config.controller.telemetry_log_interval_seconds,
        )

    async def clear_stale_session(
        self, run_id: str, request: ClearStaleSessionRequest
    ) -> ClearStaleSessionResult:
        """Finalise a stranded STALE run (#525), or 404/409.

        A "stale session" is a run row this process does not recognise as its
        own tracked active/recovering run (guard (a)) — the FE's own
        detection is a history row with ``outcome: null`` that the current
        ``/health`` snapshot's ``active_run_id`` does not match (the 12 Jul
        impostor-listener incident signature, ``docs/recent-fixes.md``). This
        is an END/FINALISE action only — never drop, never cool — and issues
        **zero MCP writes**: by the time guard (a) passes, this process's
        controller/MCP session is provably not attached to the row, so there
        is no safety box or live loop for THIS row to command through here.

        **Why no own-process MCP-idle check (#525's original "MCP-state
        gate" ask, safety-reviewer PASS-WITH-CONDITIONS, Condition 2):**
        ``health()``/``store.active_run()`` read the newest UNFINALISED run
        DB-wide, not any one process's in-memory pointer — so a genuinely
        different, live process's run can look "stale" from here even though
        it is being actively driven elsewhere. An own-process
        ``mcp_child_status()`` read cannot observe THAT process's roaster —
        it would be a no-op (this process's MCP child is unrelated to the
        stale row) or actively misleading (reading idle here proves nothing
        about who else might be driving the row). The honest, generalised
        gate is shared DURABLE state: guard (c), a telemetry-recency check
        (:meth:`~roastpilot_agent.store.RoastStore.finalize_orphaned_run`) —
        a live roast persists a telemetry row every controller tick
        (confirmed: the very first tick of a run always writes, throttled
        only by ``telemetry_log_interval_seconds``, never by phase), so any
        row inside the recency window is durable, cross-process proof that
        SOME process is actively ticking this run right now. "Fail closed on
        unknown hardware state" is satisfied by refusing anything with recent
        write evidence; genuine physical verification (is the roaster
        actually hot) is left to the operator, exactly as the existing
        stale-session UI copy already says ("if the roaster is hot, don't
        start a new one... verify the hardware directly").

        Guards, all evaluated atomically against the CURRENT row (never a
        value read moments earlier):
        (a) ``run_id != self.active_run_id`` — this process's own tracked
            run (live OR ``operator_recovery_required``) can NEVER be
            cleared through this action; that path stays in the recovery
            flow. Checked here, before any store write.
        (b) ``completed_at_utc IS NULL`` — the store's own WHERE clause; a
            race with a concurrent finalize is a clean 409, never a silent
            no-op.
        (c) No telemetry within the recency window — the store's own
            ``NOT EXISTS`` subquery; a run some process is actively driving
            is refused with a distinct message, never "already finalized".

        Every outcome — success AND every rejection — is recorded as an
        ``operator_actions`` audit row (#525 requirement 4): a rejected clear
        attempt against what turns out to be a live/recovering run is itself
        a forensically significant event, not a silent 409.

        Args:
            run_id: The stale run to finalise.
            request: The operator's required ``reason`` for the audit trail.

        Returns:
            The finalised run's id, outcome (always ``"aborted"``), and the
            stamped ``completed_at_utc``.

        Raises:
            RoastRunNotFoundError: No run matches ``run_id`` (404) — still
                audited (``operator_actions`` with ``run_id=None``, the
                attempted id in the payload) per requirement 4: every
                rejection is recorded, not just the ones with a real row.
            RoastRunConflictError: One of: this IS the process's tracked
                active/recovering run (guard (a)); the run was already
                finalised (guard (b) race); the run shows recent telemetry —
                some process is actively driving it (guard (c)) (409).
        """

        async def _reject(message: str, *, run_id_for_audit: str | None = run_id) -> None:
            await self._store.record_operator_action(
                action="clear_stale_session",
                run_id=run_id_for_audit,
                payload={
                    "reason": request.reason,
                    "rejection": message,
                    "requested_run_id": run_id,
                },
                result="rejected",
            )

        detail = await self._store.read_run(run_id)
        if detail is None:
            # #525 requirement 4 / PR #548 round-2 P3: an unknown run_id has
            # no FK-valid row to attach the audit to (operator_actions.run_id
            # is `REFERENCES roast_runs(id)` and foreign_keys=ON — passing
            # the bogus id directly would raise an IntegrityError, not
            # record anything). Record with run_id=None (the existing
            # nullable-run_id lifecycle other pre-run actions like
            # emergency_stop already use) and the ATTEMPTED id in the
            # payload, so a 404 clear attempt is still forensically visible
            # — every rejection is audited, not just the ones with a real row.
            message = f"run {run_id} is unknown"
            await _reject(message, run_id_for_audit=None)
            raise RoastRunNotFoundError(run_id)
        if run_id == self.active_run_id:
            message = (
                f"run {run_id} is this process's tracked active/recovering run; "
                "use the recovery or emergency-stop actions instead"
            )
            await _reject(message)
            raise RoastRunConflictError(message)
        try:
            await self._store.finalize_orphaned_run(
                run_id, recency_window_seconds=self._stale_session_recency_window_seconds()
            )
        except RunActivelyDrivenError as exc:
            message = (
                f"run {run_id} appears to be actively driven (recent telemetry) — "
                "verify the hardware / use emergency stop; do not clear"
            )
            await _reject(message)
            raise RoastRunConflictError(message) from exc
        except RuntimeError as exc:
            message = f"run {run_id} is already finalized"
            await _reject(message)
            raise RoastRunConflictError(message) from exc
        await self._store.record_operator_action(
            action="clear_stale_session",
            run_id=run_id,
            payload={"reason": request.reason, "agent_phase_at_clear": detail.agent_phase.value},
            result="accepted",
        )
        cleared = await self._store.read_run(run_id)
        if cleared is None:  # pragma: no cover — immutable once completed
            raise RuntimeError(f"read_run returned None for cleared run {run_id}")
        if cleared.completed_at_utc is None:  # pragma: no cover — just stamped above
            raise RuntimeError(f"cleared run {run_id} has no completed_at_utc")
        return ClearStaleSessionResult(
            run_id=run_id,
            outcome="aborted",
            completed_at_utc=cleared.completed_at_utc,
        )

    async def add_tasting(self, run_id: str, request: TastingEntryRequest) -> TastingList:
        """Record one tasting entry (#522, D91), or 404/409.

        Mirrors :meth:`rate`: 404 when the run is unknown; 409 when it is still
        in progress — a tasting is the same completed-only lifecycle as the
        rating/roasted-weight, so the in-progress case surfaces as a conflict
        rather than a 500. Unlike :meth:`rate`, this ALWAYS appends a new row
        (multiple tastings per run is the point — a revisit tasting is an
        additional entry, never an overwrite), so the response is the full
        updated :class:`TastingList`, not a single entry.

        A ``tasted_at_utc`` earlier than the run's ``completed_at_utc`` is
        physically impossible (the beans cannot be tasted before the roast
        that produced them finished) and would yield a NEGATIVE degassing
        offset — a nonsense corpus label — so it is rejected as a 409 too,
        the same class of guard as :meth:`set_roasted_weight`'s
        roasted-exceeds-charge check. Exactly-at-completion is accepted (a
        tasting timestamped the instant cooling ended is not impossible, just
        unusual, and the >= bound keeps the check simple and symmetric with
        the rest of the completed-run comparisons in this module).

        The comparison is truncated to MINUTE precision (#522 round 4): the
        FE's ``datetime-local`` picker cannot express seconds, so an honest
        "tasted at the completion minute" entry would otherwise 409 against
        ``completed_at_utc``'s own seconds component (e.g. completion at
        ``:45`` seconds, the operator picks that same minute — a real,
        non-impossible entry the seconds-precision compare would wrongly
        reject). Same-minute reads as simultaneous at the input's resolution;
        the guard still catches any offset a human could actually express.

        A same-truncated-minute value that is nonetheless raw-earlier than
        ``completed_at_utc`` (#522 round 5 — the above example: input ``:00``
        against a ``:45`` completion) is CLAMPED to ``completed_at_utc``
        before storage, not stored as given: storing the raw sub-minute-early
        value would compute a small NEGATIVE ``degassing_offset_hours`` in the
        corpus export — exactly the garbage this whole guard chain exists to
        prevent, just shrunk to sub-minute scale. "Same minute" means "at
        completion," so it is stored as at completion.
        """
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        if detail.completed_at_utc is None:
            raise RoastRunConflictError(
                f"run {run_id} is still in progress; taste it after completion"
            )
        tasted_at_utc = request.tasted_at_utc
        if tasted_at_utc is not None:
            if _before_the_minute(tasted_at_utc, detail.completed_at_utc):
                raise RoastRunConflictError(
                    f"tasted_at_utc {tasted_at_utc} is before the run completed "
                    f"at {detail.completed_at_utc} (physically impossible)"
                )
            if tasted_at_utc < detail.completed_at_utc:
                # Admitted only by the minute truncation above: raw-earlier,
                # same minute. Clamp to completed_at_utc so the stored value
                # (and the derived degassing offset) is never negative.
                tasted_at_utc = detail.completed_at_utc
        await self._store.add_tasting(
            run_id,
            stars=request.stars,
            notes=request.notes,
            tasted_at_utc=tasted_at_utc,
            brew_method=request.brew_method,
            grind_note=request.grind_note,
            attributes=request.attributes,
            defects=request.defects,
        )
        return await self.list_tastings(run_id)

    async def list_tastings(self, run_id: str) -> TastingList:
        """The run's tasting entries, oldest first (#522), or 404 when unknown."""
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        return TastingList(run_id=run_id, tastings=await self._store.list_tastings(run_id))

    async def submit_operator_action(
        self, run_id: str, request: OperatorActionRequest
    ) -> OperatorActionResult:
        """Queue an operator action through safety policy (plan §6).

        The pipeline is: validate the action against the run's current phase
        via the existing command×phase matrix, record an ``operator_actions``
        row with the outcome, and — when accepted — place the action on the
        controller queue. The controller drains the queue each tick and runs
        the *full* safety policy again before any MCP write (E9): this method
        never writes hardware and never bypasses or reimplements safety. The
        phase pre-check exists only so the operator gets immediate feedback
        (e.g. a drop requested during preheating is rejected now, not silently
        held). 404s an unknown run id.

        Control actions with no MCP write (``pause_advisory`` /
        ``resume_advisory`` / ``acknowledge_recovery`` / ``acknowledge_fault``)
        skip the matrix check and are accepted for the controller to interpret on
        drain.

        A faulted-but-unacknowledged run (#206) is NOT terminal here — its
        ``completed_at_utc`` is null until the operator acknowledges it — so it is
        NOT 410'd and correctly accepts ``stop_cooling`` / ``start_cooling`` /
        ``emergency_stop`` / ``acknowledge_fault`` so a fault never strands a
        physically-running machine.
        """
        detail = await self._store.read_run(run_id)
        if detail is None:
            raise RoastRunNotFoundError(run_id)
        if detail.completed_at_utc is not None:
            # Terminal run (completed/aborted/faulted): no live loop drains its
            # queue and no hot hardware to act on — the action is gone (410).
            raise RoastRunGoneError(
                f"run {run_id} is {detail.outcome or 'terminal'}; operator actions "
                f"are no longer accepted"
            )

        command = _ACTION_COMMAND.get(request.action)
        if command is None:
            result: Literal["accepted", "rejected", "failed"] = "accepted"
            reason = f"{request.action.value} accepted: queued for the controller"
        else:
            evaluation = self._safety.evaluate_command_phase(
                command=command, phase=detail.agent_phase
            )
            if evaluation.verdict is SafetyVerdict.ALLOW:
                result = "accepted"
                reason = f"{request.action.value} accepted in phase {detail.agent_phase.value}"
            else:
                result = "rejected"
                reason = evaluation.reason

        queued = False
        if result == "accepted":
            try:
                self.operator_queue.put_nowait(
                    QueuedOperatorAction(
                        run_id=run_id, action=request.action, payload=request.payload
                    )
                )
                queued = True
            except asyncio.QueueFull:
                # Backstop bound hit (pathological spam). Report failed rather
                # than 500 or silently drop — the operator sees the action did
                # not take. The stored row reflects the final outcome (below).
                result = "failed"
                reason = "operator action queue is full; action not accepted"

        await self._store.record_operator_action(
            action=request.action.value,
            result=result,
            run_id=run_id,
            payload=request.payload,
        )
        return OperatorActionResult(
            action=request.action, result=result, reason=reason, queued=queued
        )

    # --- #303: bean-profile library CRUD (D45) ---
    #
    # The saved-profile library behind the Start-Roast dropdown. Plain REST over
    # the store's CRUD: no MCP, no phase coupling, no SSE — the SPA-renders-from-
    # server invariant holds (profiles come from the API). A roast still starts
    # from a RoastProfile; these endpoints never touch the start-roast path or a
    # frozen roast snapshot.

    async def seed_bean_profiles(self) -> None:
        """Idempotently seed the built-in bean profiles at startup (#303).

        Inserts each :data:`~roastpilot_agent.seed.SEED_BEAN_PROFILES` entry by
        its stable id (``INSERT OR IGNORE``) so the Ethiopia Koke profile is
        selectable for the first roast and a restart never double-inserts.
        """
        for seed in SEED_BEAN_PROFILES:
            await self._store.seed_bean_profile(seed)
        await self._store.reconcile_interrupted_bean_sourcing_attempts()
        await self._store.expire_bean_sourcing_drafts()
        self._ensure_bean_draft_expiry_task()

    def _ensure_bean_draft_expiry_task(self) -> None:
        """Start or wake the service-owned bounded-retention timer (#588)."""
        task = self._bean_draft_expiry_task
        if task is None or task.done():
            self._bean_draft_expiry_task = asyncio.create_task(
                self._bean_draft_expiry_loop(), name="bean-draft-expiry"
            )
        else:
            self._bean_draft_expiry_wakeup.set()

    async def _bean_draft_expiry_loop(self) -> None:
        """Clear successful unsaved draft snapshots at their 24-hour boundary."""
        while True:
            self._bean_draft_expiry_wakeup.clear()
            try:
                await self._store.reconcile_interrupted_bean_sourcing_attempts()
                await self._store.expire_bean_sourcing_drafts()
                next_expiry = await self._store.next_bean_sourcing_expiry()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - retention retries after visible failure
                _log.error("bean-draft expiry pass failed; retrying", exc_info=True)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._bean_draft_expiry_wakeup.wait(),
                        timeout=_BEAN_DRAFT_EXPIRY_RETRY_SECONDS,
                    )
                continue
            if next_expiry is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._bean_draft_expiry_wakeup.wait(),
                        timeout=_BEAN_DRAFT_EXPIRY_MAX_SLEEP_SECONDS,
                    )
                continue
            delay = min(
                _BEAN_DRAFT_EXPIRY_MAX_SLEEP_SECONDS,
                max(
                    0.0,
                    (datetime.fromisoformat(next_expiry) - datetime.now(UTC)).total_seconds(),
                ),
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._bean_draft_expiry_wakeup.wait(), timeout=delay)

    async def list_bean_profiles(self) -> BeanProfileList:
        """The active saved bean profiles for the dropdown, name-ordered (#303)."""
        return BeanProfileList(profiles=await self._store.list_bean_profiles())

    async def create_bean_profile(
        self,
        profile_input: BeanProfileInput,
        *,
        draft_attempt_id: str | None = None,
    ) -> BeanProfile:
        """Create a saved profile, optionally claiming one drafted attempt."""
        try:
            profile = await self._store.create_bean_profile(
                profile_input, draft_attempt_id=draft_attempt_id
            )
        except BeanDraftAttemptAlreadyClaimedError as exc:
            raise BeanDraftAlreadyClaimedConflictError(str(exc)) from exc
        except BeanDraftAttemptClaimError as exc:
            raise RoastRunConflictError(str(exc)) from exc
        self._ensure_bean_draft_expiry_task()
        return profile

    async def update_bean_profile(
        self, profile_id: str, profile_input: BeanProfileInput
    ) -> BeanProfile:
        """Edit a saved bean profile (future roasts only), or 404 (#303)."""
        try:
            return await self._store.update_bean_profile(profile_id, profile_input)
        except BeanProfileNotFoundError as exc:
            raise RoastRunNotFoundError(str(exc)) from exc

    async def delete_bean_profile(self, profile_id: str) -> None:
        """Archive (soft-delete) a saved bean profile, or 404 (#303)."""
        try:
            await self._store.delete_bean_profile(profile_id)
        except BeanProfileNotFoundError as exc:
            raise RoastRunNotFoundError(str(exc)) from exc

    # --- #573 phase 1: add-bean-from-URL (bean-sourcing assistant) ---
    #
    # Fetch a vendor product URL + LLM-extract a draft profile. Deliberately
    # Returns an unsaved draft. Runtime telemetry retains only a sanitized
    # field-value baseline (no URL/evidence/prose) with a 24-hour claim deadline.
    # It is cleared on claim or orderly shutdown, or at the deadline (including
    # after restart following an abrupt stop). Saving remains explicit.

    @staticmethod
    def _bean_attempt_usage(
        diagnostics: BeanSourcingDiagnostics,
    ) -> tuple[int | None, int | None, Literal["exact", "partial", "unknown"]]:
        """Qualify retry-inclusive usage from provider-response provenance."""
        if diagnostics.usage_reported_requests > 0 and diagnostics.usage_unreported_requests == 0:
            return diagnostics.request_tokens, diagnostics.response_tokens, "exact"
        if diagnostics.usage_reported_requests > 0:
            return diagnostics.request_tokens, diagnostics.response_tokens, "partial"
        return None, None, "unknown"

    async def _start_bean_attempt_bounded(
        self, *, provider: str, model_slug: str, prompt_version: str
    ) -> tuple[str, bool]:
        """Own admission until commit/rollback and report deferred cancellation."""
        admission = asyncio.create_task(
            self._store.start_bean_sourcing_attempt(
                provider=provider,
                model_slug=model_slug,
                prompt_version=prompt_version,
                owner_instance_id=self.instance_id,
            ),
            name="admit-bean-attempt",
        )
        cancellation_received = False
        while not admission.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(admission), timeout=_BEAN_DRAFT_FINALIZE_TIMEOUT_SECONDS
                )
            except asyncio.CancelledError:
                cancellation_received = True
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
            except TimeoutError as exc:
                admission.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await admission
                raise RuntimeError("timed out admitting bean-sourcing attempt") from exc
        return admission.result(), cancellation_received

    async def _renew_bean_attempt_lease(self, attempt_id: str) -> None:
        """Keep this process's live attempt from cross-process reconciliation."""
        while True:
            await asyncio.sleep(_BEAN_ATTEMPT_LEASE_HEARTBEAT_SECONDS)
            try:
                renewed = await self._store.renew_bean_sourcing_attempt_lease(
                    attempt_id, owner_instance_id=self.instance_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - retry while the request remains live
                _log.error("bean-sourcing lease renewal failed; retrying", exc_info=True)
                continue
            if not renewed:
                return

    async def _finish_bean_attempt_bounded(
        self,
        attempt_id: str,
        *,
        outcome: Literal[
            "success",
            "fetch_error",
            "extraction_error",
            "provider_error",
            "preempted",
            "cancelled",
        ],
        started_monotonic: float,
        diagnostics: BeanSourcingDiagnostics,
        draft: BeanProfileDraft | None = None,
    ) -> None:
        """Commit terminal telemetry in a separately owned shielded task."""
        request_tokens, response_tokens, evidence = self._bean_attempt_usage(diagnostics)
        finalizer = asyncio.create_task(
            self._store.finish_bean_sourcing_attempt(
                attempt_id,
                outcome=outcome,
                latency_ms=max(0, round((self._clock() - started_monotonic) * 1000)),
                request_tokens=request_tokens,
                response_tokens=response_tokens,
                usage_evidence=evidence,
                timed_out_runs=diagnostics.timed_out_runs,
                draft=draft,
            ),
            name=f"finish-bean-attempt-{attempt_id}",
        )
        deadline = self._clock() + _BEAN_DRAFT_FINALIZE_TIMEOUT_SECONDS
        cancellation_received = False
        while not finalizer.done():
            remaining = deadline - self._clock()
            if remaining <= 0:
                finalizer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await finalizer
                raise RuntimeError(f"timed out finalizing bean-sourcing attempt {attempt_id}")
            try:
                await asyncio.wait_for(asyncio.shield(finalizer), timeout=remaining)
            except asyncio.CancelledError:
                cancellation_received = True
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
            except TimeoutError as exc:
                finalizer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await finalizer
                raise RuntimeError(
                    f"timed out finalizing bean-sourcing attempt {attempt_id}"
                ) from exc
        finalizer.result()
        if cancellation_received:
            raise asyncio.CancelledError

    async def draft_bean_from_url(self, url: str) -> BeanProfileDraft:
        """Draft an unsaved bean profile from a vendor URL (#573 phase 1).

        A sanitized field-value baseline (excluding URL, evidence, and prose)
        has a 24-hour claim deadline for explicit save-time correlation. It is
        cleared on claim or orderly shutdown, or at the deadline (including
        after restart following an abrupt stop). No saved profile is created here.

        Delegates to :func:`roastpilot_agent.bean_sourcing.draft_bean_profile_from_url`
        with this service's configured advisor provider/key (BYOK, a SEPARATE
        LLM call from the roast advisor) and fetch limits. No roaster/MCP
        involvement — this path never touches the roast-control loop.

        Guarded against an active roast (#587 P1): the active-run check uses
        the SAME persisted signal :meth:`start_roast` and :meth:`health`
        already use (``self._store.active_run()``) — not a new state
        source. On a resource-constrained provider (especially the local
        Ollama path, which serialises inference), a bean-extraction call can
        occupy the same backend an active post-FC roast needs for control
        advice; those advice calls then time out at
        ``ControllerConfig.advisory_timeout_seconds`` and 3 consecutive
        availability failures trip the sustained-outage safety fallback.

        The persisted active-run check and task registration share
        :attr:`_start_lock` with :meth:`start_roast`, so a draft cannot pass
        the check while a roast is being created. The lock is released before
        the remote fetch and provider call (#657). If a roast start wins
        later, it marks and cancels every registered draft, briefly drains
        cooperative cancellation, then persists the run. The drain is bounded:
        local cancellation is reliable in this async pipeline, but cannot
        guarantee a remote provider stops processing an accepted request.

        Raises:
            RoastRunConflictError: A roast is currently active (maps to
                HTTP 409 at the route).
        """
        async with self._start_lock:
            active = await self._store.active_run()
            if active is not None:
                raise RoastRunConflictError(
                    "bean drafting is unavailable while a roast is active (run "
                    f"{active.run_id}, phase {active.agent_phase.value}) — it "
                    "would compete with the roast advisor for the same backend; "
                    "try again once the roast ends"
                )
            advisor_config = self._config.advisor
            sourcing_config = self._config.bean_sourcing
            started_monotonic = self._clock()
            diagnostics = BeanSourcingDiagnostics()
            attempt_id, cancelled_during_admission = await self._start_bean_attempt_bounded(
                provider=advisor_config.provider,
                model_slug=resolve_extraction_model_slug(advisor_config, sourcing_config),
                prompt_version=BEAN_EXTRACTION_PROMPT_VERSION,
            )
            if cancelled_during_admission:
                await self._finish_bean_attempt_bounded(
                    attempt_id,
                    outcome="cancelled",
                    started_monotonic=started_monotonic,
                    diagnostics=diagnostics,
                )
                raise asyncio.CancelledError
            lease_heartbeat = asyncio.create_task(
                self._renew_bean_attempt_lease(attempt_id),
                name=f"renew-bean-attempt-{attempt_id}",
            )
            task = asyncio.create_task(
                draft_bean_profile_from_url(
                    url,
                    advisor_config=advisor_config,
                    sourcing_config=sourcing_config,
                    diagnostics=diagnostics,
                )
            )
            operation = _BeanDraftOperation(task=task)
            self._bean_draft_operations[task] = operation
        try:
            try:
                result = await task
            except asyncio.CancelledError:
                outer_task = asyncio.current_task()
                preempted = operation.preempted_by_start
                await self._finish_bean_attempt_bounded(
                    attempt_id,
                    outcome="preempted" if preempted else "cancelled",
                    started_monotonic=started_monotonic,
                    diagnostics=diagnostics,
                )
                if outer_task is not None and outer_task.cancelling() == 0 and preempted:
                    raise RoastRunConflictError(
                        "bean drafting was preempted by a roast-start attempt; "
                        "retry once the roast ends, or retry now if the start failed"
                    ) from None
                raise
            except Exception as exc:
                outer_task = asyncio.current_task()
                if outer_task is not None and outer_task.cancelling() > 0:
                    await self._finish_bean_attempt_bounded(
                        attempt_id,
                        outcome="cancelled",
                        started_monotonic=started_monotonic,
                        diagnostics=diagnostics,
                    )
                    raise asyncio.CancelledError from None
                if operation.preempted_by_start:
                    await self._finish_bean_attempt_bounded(
                        attempt_id,
                        outcome="preempted",
                        started_monotonic=started_monotonic,
                        diagnostics=diagnostics,
                    )
                    raise RoastRunConflictError(
                        "bean drafting was preempted by a roast-start attempt; "
                        "retry once the roast ends, or retry now if the start failed"
                    ) from None
                if isinstance(exc, BeanFetchError):
                    outcome = "fetch_error"
                elif isinstance(exc, BeanExtractionUnavailableError):
                    outcome = "provider_error"
                elif isinstance(exc, BeanExtractionError):
                    outcome = "extraction_error"
                else:
                    outcome = "provider_error"
                await self._finish_bean_attempt_bounded(
                    attempt_id,
                    outcome=outcome,
                    started_monotonic=started_monotonic,
                    diagnostics=diagnostics,
                )
                raise

            outer_task = asyncio.current_task()
            if outer_task is not None and outer_task.cancelling() > 0:
                await self._finish_bean_attempt_bounded(
                    attempt_id,
                    outcome="cancelled",
                    started_monotonic=started_monotonic,
                    diagnostics=diagnostics,
                )
                raise asyncio.CancelledError
            if operation.preempted_by_start:
                await self._finish_bean_attempt_bounded(
                    attempt_id,
                    outcome="preempted",
                    started_monotonic=started_monotonic,
                    diagnostics=diagnostics,
                )
                raise RoastRunConflictError(
                    "bean drafting was preempted by a roast-start attempt; "
                    "retry once the roast ends, or retry now if the start failed"
                ) from None
            await self._finish_bean_attempt_bounded(
                attempt_id,
                outcome="success",
                started_monotonic=started_monotonic,
                diagnostics=diagnostics,
                draft=result,
            )
            self._ensure_bean_draft_expiry_task()
            return result.model_copy(update={"draft_attempt_id": attempt_id})
        finally:
            if "lease_heartbeat" in locals():
                lease_heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lease_heartbeat
            self._bean_draft_operations.pop(task, None)


def _get_service(request: Request) -> RoastService:
    """Dependency: the app's :class:`RoastService`, or 503 if unconfigured.

    ``create_app()`` with no service (the scaffold smoke app) leaves
    ``app.state`` bare; every store-backed route then fails closed with a
    clear 503 rather than an ``AttributeError`` 500.
    """
    service = getattr(request.app.state, "service", None)
    if not isinstance(service, RoastService):
        raise HTTPException(status_code=503, detail="roast service not configured")
    return service


ServiceDep = Annotated[RoastService, Depends(_get_service)]


async def health(request: Request) -> HealthResponse:
    """``GET /api/health`` — works with or without a configured service.

    Without a service (scaffold app) it still reports liveness and version so
    the probe never depends on a store being wired. ``instance_id`` (#516)
    falls back to the module-level ``_SCAFFOLD_INSTANCE_ID`` — minted once at
    import, not once per request — so the field is never absent, and the
    scaffold path gets the identical "one id per process" guarantee the
    service-backed path gets from ``RoastService.instance_id``.
    """
    service = getattr(request.app.state, "service", None)
    if isinstance(service, RoastService):
        return await service.health()
    return HealthResponse(
        version=__version__,
        instance_id=_SCAFFOLD_INSTANCE_ID,
        mcp_child=MCPChildStatus.NOT_CONFIGURED,
        active_run_id=None,
    )


async def start_roast(profile: RoastProfile, service: ServiceDep) -> RoastDetail:
    """``POST /api/roasts`` — start a roast (409 if one is active)."""
    try:
        return await service.start_roast(profile)
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def list_roasts(service: ServiceDep) -> RoastHistory:
    """``GET /api/roasts`` — roast history list."""
    return await service.history()


async def get_roast(run_id: str, service: ServiceDep) -> RoastDetail:
    """``GET /api/roasts/{run_id}`` — run detail."""
    try:
        return await service.detail(run_id)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def get_telemetry(
    run_id: str,
    service: ServiceDep,
    downsample: Annotated[int, Query(ge=1)] = 1,
) -> TelemetrySeries:
    """``GET /api/roasts/{run_id}/telemetry`` — downsampled snapshot series."""
    try:
        return await service.telemetry(run_id, downsample=downsample)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def get_timeline(run_id: str, service: ServiceDep) -> RoastTimeline:
    """``GET /api/roasts/{run_id}/timeline`` — the decision trace."""
    try:
        return await service.timeline(run_id)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def get_log_manifest(run_id: str, service: ServiceDep) -> LogManifest:
    """``GET /api/roasts/{run_id}/log`` — the export-log manifest."""
    try:
        return await service.log_manifest(run_id)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def download_log(
    run_id: str,
    artifact: str,
    service: ServiceDep,
) -> FileResponse:
    """``GET /api/roasts/{run_id}/log/{artifact}`` — download an export file.

    ``artifact`` is validated in :meth:`RoastService.log_artifact_path` (the
    single artifact-name check), which 404s an unknown name like any other
    missing artifact.
    """
    try:
        path = await service.log_artifact_path(run_id, artifact)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name)


async def rate_roast(
    run_id: str,
    rating: OperatorRatingRequest,
    service: ServiceDep,
) -> RoastDetail:
    """``POST /api/roasts/{run_id}/rating`` — operator self-rating."""
    try:
        return await service.rate(run_id, rating)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def set_roasted_weight(
    run_id: str,
    request: RoastedWeightRequest,
    service: ServiceDep,
) -> RoastDetail:
    """``POST /api/roasts/{run_id}/roasted-weight`` — operator roasted-out weight (#388)."""
    try:
        return await service.set_roasted_weight(run_id, request)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def set_charge_weight(
    run_id: str,
    request: ChargeWeightRequest,
    service: ServiceDep,
) -> RoastDetail:
    """``POST /api/roasts/{run_id}/charge-weight`` — operator charge-weight correction (#520)."""
    try:
        return await service.set_charge_weight(run_id, request)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def discard_roast(run_id: str, service: ServiceDep) -> RoastDetail:
    """``POST /api/roasts/{run_id}/discard`` — soft-exclude a bad-data roast (#582)."""
    try:
        return await service.discard_roast(run_id)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def restore_roast(run_id: str, service: ServiceDep) -> RoastDetail:
    """``POST /api/roasts/{run_id}/restore`` — reverse a discard (#582)."""
    try:
        return await service.restore_roast(run_id)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def clear_stale_session(
    run_id: str,
    request: ClearStaleSessionRequest,
    service: ServiceDep,
) -> ClearStaleSessionResult:
    """``POST /api/roasts/{run_id}/clear-stale-session`` — finalise a stranded
    STALE run (#525). See :meth:`RoastService.clear_stale_session` for the
    full guard/gate design."""
    try:
        return await service.clear_stale_session(run_id, request)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def add_tasting(
    run_id: str,
    request: TastingEntryRequest,
    service: ServiceDep,
) -> TastingList:
    """``POST /api/roasts/{run_id}/tastings`` — record a tasting entry (#522, D91)."""
    try:
        return await service.add_tasting(run_id, request)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def list_tastings(run_id: str, service: ServiceDep) -> TastingList:
    """``GET /api/roasts/{run_id}/tastings`` — the run's tasting entries (#522)."""
    try:
        return await service.list_tastings(run_id)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def submit_operator_action(
    run_id: str,
    action: OperatorActionRequest,
    service: ServiceDep,
) -> OperatorActionResult:
    """``POST /api/roasts/{run_id}/operator-actions`` — queue an operator action.

    Always 200 with the typed :class:`OperatorActionResult` for a live run: the
    request was recorded and its policy outcome (``accepted`` / ``rejected`` /
    ``failed``) is in the body, so the SPA renders one shape. 404 when the run is
    unknown; 410 when the run is terminal (completed/faulted); an unknown action
    value is a 422 from request validation.
    """
    try:
        return await service.submit_operator_action(run_id, action)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoastRunGoneError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc


async def list_bean_profiles(service: ServiceDep) -> BeanProfileList:
    """``GET /api/bean-profiles`` — the saved bean-profile library (#303)."""
    return await service.list_bean_profiles()


async def create_bean_profile(
    profile: BeanProfileInput,
    service: ServiceDep,
    draft_attempt_id: Annotated[
        str | None,
        Header(alias="X-RoastPilot-Draft-Attempt-Id", pattern=r"^[0-9a-f]{32}$"),
    ] = None,
) -> BeanProfile:
    """``POST /api/bean-profiles`` — create and optionally claim a draft."""
    try:
        return await service.create_bean_profile(profile, draft_attempt_id=draft_attempt_id)
    except BeanDraftAlreadyClaimedConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-RoastPilot-Conflict-Code": "draft_attempt_already_claimed"},
        ) from exc
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def update_bean_profile(
    profile_id: str, profile: BeanProfileInput, service: ServiceDep
) -> BeanProfile:
    """``PUT /api/bean-profiles/{profile_id}`` — edit a saved profile, 404 if
    unknown (#303). Future-roasts-only: a past roast's frozen snapshot is
    unaffected."""
    try:
        return await service.update_bean_profile(profile_id, profile)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def delete_bean_profile(profile_id: str, service: ServiceDep) -> dict[str, str]:
    """``DELETE /api/bean-profiles/{profile_id}`` — archive a saved profile, 404
    if unknown (#303).

    A soft archive, not a hard delete: the profile drops out of the dropdown but
    its row survives so a past roast that referenced it never dangles.
    """
    try:
        await service.delete_bean_profile(profile_id)
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": profile_id, "result": "archived"}


_DRAFT_BEAN_FROM_URL_PATH = "/api/beans/draft-from-url"
_DRAFT_BEAN_FROM_URL_MAX_URL_CHARS = 4096
_DRAFT_BEAN_FROM_URL_MAX_BODY_BYTES = 64 * 1024
_DRAFT_BEAN_FROM_URL_TOO_LONG_DETAIL = (
    f"URL exceeds {_DRAFT_BEAN_FROM_URL_MAX_URL_CHARS}-character limit"
)
_DRAFT_BEAN_FROM_URL_BODY_TOO_LARGE_DETAIL = (
    f"request body exceeds {_DRAFT_BEAN_FROM_URL_MAX_BODY_BYTES}-byte limit"
)


class DraftBeanFromUrlRequest(BaseModel):
    """``POST /api/beans/draft-from-url`` request body (#573 phase 1)."""

    url: str = Field(min_length=1, max_length=_DRAFT_BEAN_FROM_URL_MAX_URL_CHARS)
    """The vendor's green-coffee product page URL."""


#: #587 fix 5 (P1 #3353): bounds how many draft-from-url requests can be
#: ADMITTED at once (holding, or queued behind, the semaphore below). Each
#: request is a billable BYOK LLM call (plus a server-side fetch of an
#: operator-supplied URL) with no other rate limit on this route, so an
#: unbounded burst of concurrent requests could both run up the operator's
#: provider bill and tie up the process. Deliberately NOT an authentication
#: control: this app has no authentication anywhere (a single-operator LAN
#: tool, binds ``0.0.0.0`` via ``scripts/roast-live.sh``) — per-endpoint
#: auth would be an app-wide architectural decision, not something to bolt
#: onto one route, and is out of scope here by design.
#:
#: Fixed at 1, not 2 (#587 P2, round 6): only one billable fetch+provider
#: operation may be in flight. A second draft fails fast with 429 instead
#: of silently queuing behind the first. Roast starts do not use this
#: semaphore and therefore retain priority over a slow or abandoned draft
#: (#657).
_DRAFT_BEAN_FROM_URL_CONCURRENCY = 1

#: How long a request waits for a free concurrency slot before it fails
#: closed with 429 rather than queuing indefinitely behind other in-flight
#: LLM calls.
_DRAFT_BEAN_FROM_URL_ACQUIRE_TIMEOUT_SECONDS = 0.1

_draft_bean_from_url_semaphore = asyncio.Semaphore(_DRAFT_BEAN_FROM_URL_CONCURRENCY)


class _RouteBodyLimitMiddleware:
    """Bound one route's body before FastAPI buffers or JSON-decodes it."""

    def __init__(self, app: ASGIApp, *, path: str, max_body_bytes: int) -> None:
        self._app = app
        self._path = path
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != self._path:
            await self._app(scope, receive, send)
            return

        content_length = next(
            (value for name, value in scope["headers"] if name.lower() == b"content-length"),
            None,
        )
        try:
            declared_body_bytes = int(content_length) if content_length is not None else None
        except ValueError:  # pragma: no cover - Uvicorn rejects malformed Content-Length.
            declared_body_bytes = None
        if declared_body_bytes is not None and declared_body_bytes > self._max_body_bytes:
            await self._reject(scope, receive, send)
            return

        captured_messages: list[Message] = []
        received_body_bytes = 0
        while True:
            message = await receive()
            captured_messages.append(message)
            if message["type"] == "http.request":  # pragma: no branch - disconnect passes through.
                received_body_bytes += len(message.get("body", b""))
                if received_body_bytes > self._max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
                if message.get("more_body", False):
                    continue
            break

        replay_position = 0

        async def replay_receive() -> Message:
            nonlocal replay_position
            if replay_position < len(captured_messages):  # pragma: no branch - end stops reads.
                message = captured_messages[replay_position]
                replay_position += 1
                return message
            return await receive()  # pragma: no cover - defensive post-body receive.

        await self._app(scope, replay_receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": _DRAFT_BEAN_FROM_URL_BODY_TOO_LARGE_DETAIL},
        )
        await response(scope, receive, send)


async def _request_validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Keep oversized bean-source URL validation responses constant and secret-free."""
    validation_error = cast(RequestValidationError, exc)
    if request.url.path == _DRAFT_BEAN_FROM_URL_PATH and any(
        error["type"] == "string_too_long" and tuple(error["loc"]) == ("body", "url")
        for error in validation_error.errors()
    ):
        return JSONResponse(
            status_code=422,
            content={"detail": _DRAFT_BEAN_FROM_URL_TOO_LONG_DETAIL},
        )
    return await request_validation_exception_handler(request, validation_error)


async def draft_bean_from_url(
    body: DraftBeanFromUrlRequest, service: ServiceDep
) -> BeanProfileDraft:
    """``POST /api/beans/draft-from-url`` — draft a bean profile from a vendor
    URL (#573 phase 1).

    Fetches the page, extracts a bean identity via a SEPARATE LLM call from
    the roast advisor, and returns a conservative-target draft — it never
    persists anything (saving is the existing ``POST /api/bean-profiles``
    action, driven by the operator explicitly submitting the reviewed draft).
    A 422 on a bad/unreachable URL or a client-actionable extraction failure
    (the page yielded too little identity to draft from); the detail message
    names which. A 503 when extraction failed for a DEPENDENCY reason — a
    provider/transport timeout, error, or malformed output — rather than the
    page itself (#613: origin-mapped, not a uniform 422 for every
    ``BeanExtractionError``). A 409 while a roast is active (#587 P1) — see
    :meth:`RoastService.draft_bean_from_url`.

    Concurrency-bounded (#587 fix 5; fixed at 1, #587 P2 round 6): at most
    :data:`_DRAFT_BEAN_FROM_URL_CONCURRENCY` (one) request is ADMITTED at a
    time — each is a billable BYOK LLM request, so this is a cost/resource-
    exhaustion mitigation, not an access-control one. The semaphore turns a
    SECOND concurrent request into a fast 429 instead of silently queuing
    it, while roast starts remain independent of this draft-only admission
    control (#657).
    A request that cannot acquire the single slot within
    :data:`_DRAFT_BEAN_FROM_URL_ACQUIRE_TIMEOUT_SECONDS` gets 429. This
    endpoint has NO authentication, matching every other route in this
    single-operator LAN app — that is a deliberate, existing, app-wide
    decision, not something this fix changes or is meant to compensate for.
    """
    try:
        urlsplit(body.url)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"not a well-formed http(s) URL: {redact_url_for_error(body.url)!r} "
                "(invalid URL syntax)"
            ),
        ) from exc

    try:
        async with asyncio.timeout(_DRAFT_BEAN_FROM_URL_ACQUIRE_TIMEOUT_SECONDS):
            await _draft_bean_from_url_semaphore.acquire()
    except TimeoutError as exc:
        raise HTTPException(
            status_code=429,
            detail="too many concurrent bean-draft requests in flight; try again shortly",
        ) from exc
    try:
        return await service.draft_bean_from_url(body.url)
    except RoastRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BeanFetchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BeanExtractionUnavailableError as exc:
        # Dependency-origin failure (provider timeout/error/malformed
        # output) — the vendor page may have been fine; a 503, not a 422
        # accusing the caller of bad input (#613). Caught BEFORE the base
        # ``BeanExtractionError`` below since it is a subclass.
        raise HTTPException(
            status_code=503,
            detail=f"bean extraction temporarily unavailable (provider error) — try again: {exc}",
        ) from exc
    except BeanExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        _draft_bean_from_url_semaphore.release()


CONFIG_PATH = "/api/config"


async def get_config(request: Request) -> AppConfigSnapshot:
    """``GET /api/config`` — per-field config snapshot (D78, #418).

    Returns the full :class:`~roastpilot_agent.config_store.AppConfigSnapshot`
    for the Config UI: each managed field carries its saved value, effective
    value, schema default, ``env_overridden`` flag, and ``read_only`` flag.

    The effective config is read fresh from disk and env on every request so
    the UI reflects the current operator state.  The live :class:`RoastService`
    holds a snapshot baked at startup; this route reflects *current* env and
    file state, which may differ during a live roast if the operator has edited
    the file without restarting.

    A malformed saved-config file returns 500 — the operator must fix the YAML
    before the Config UI can render.
    """
    # load_app_config and load_saved_raw do sync file I/O; run in a thread
    # to avoid blocking the async event loop on disk reads.
    try:
        effective, injected_keys = await asyncio.to_thread(load_app_config)
        saved_raw = await asyncio.to_thread(load_saved_raw)
    except (ConfigFileError, ValidationError, OSError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return build_config_snapshot(effective, saved_raw, injected_keys)


async def put_config(edit: AppConfigEdit, request: Request) -> AppConfigSnapshot:
    """``PUT /api/config`` — write editable managed fields (D78, #418).

    Accepts an :class:`~roastpilot_agent.config_store.AppConfigEdit` body
    (controller + advisor only; safety is excluded by the type), merges it into
    the saved-config file, and returns the updated
    :class:`~roastpilot_agent.config_store.AppConfigSnapshot`.

    Out-of-range field values are rejected by Pydantic field validators on the
    *edit* body (FastAPI returns 422 automatically).  Schema violations caught
    only after merging (cross-field constraints) raise 422 from
    ``pydantic.ValidationError``; a malformed existing file raises 500.

    The change takes effect for the *next* roast — the running agent's in-memory
    config is not patched.  The FE should note this in the UI (plan §D78).
    """
    # persist_config_edit does sync file I/O (read + lock + write); run it in
    # a thread so the async event loop is not blocked during the write.
    try:
        await asyncio.to_thread(persist_config_edit, edit)
    except ValidationError as exc:
        # Cross-field constraints (e.g. min_trim > max_trim) are only caught
        # after merging the edit with the existing saved config.  Map to 422
        # so the client knows to fix the request body, not retry unchanged.
        # Use str() rather than exc.errors(): the pydantic v2 error ctx dict can
        # include non-JSON-serialisable objects (e.g. ValueError instances from
        # model validators), so .errors() may raise TypeError during serialisation.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConfigFileError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # Re-read effective config + saved raw after write so the response reflects
    # the just-written state, not a stale snapshot.  load_app_config also does
    # file I/O (saved-config read + env injection); run it in a thread too.
    try:
        effective, injected_keys = await asyncio.to_thread(load_app_config)
        saved_raw = await asyncio.to_thread(load_saved_raw)
    except ConfigFileError as exc:  # pragma: no cover — written successfully one line above
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return build_config_snapshot(effective, saved_raw, injected_keys)


# ---------------------------------------------------------------------------
# Device enumeration — GET /api/config/devices (D78 PR(c), #418)
# ---------------------------------------------------------------------------


class DeviceOption(BaseModel):
    """One enumerated device entry returned by ``GET /api/config/devices``.

    Attributes:
        value: The machine-readable identifier to store in the config.  For
            serial devices this is the port path (e.g.
            ``/dev/tty.usbmodem14101``); for audio input devices this is the
            device NAME substring (e.g. ``"USB PnP Sound Device"``), matching
            the ``mcp_device.audio_input_device`` config field which the MCP
            matches case-insensitively.  Note: two audio devices can share a
            name → duplicate values; the ``note`` disambiguates the display.
        label: Human-readable display name for the Config UI dropdown.
        note: Extra detail shown as secondary text (port description / HW id
            for serial; channel count + sample rate for audio input).
    """

    value: str
    label: str
    note: str


class DevicesSnapshot(BaseModel):
    """Response body for ``GET /api/config/devices`` (D78 PR(c), #418).

    Each source (serial / audio_input) is enumerated independently so a
    failure in one source (e.g. PortAudio unavailable) never prevents the
    other from returning results.

    Attributes:
        serial: Enumerated serial port devices, ordered by port path.
        serial_error: Non-``None`` when serial enumeration failed; the value
            is the exception message for operator diagnostics.
        audio_input: Enumerated audio input devices, ordered by device index.
        audio_input_error: Non-``None`` when audio enumeration failed; the
            value is the exception message for operator diagnostics.
    """

    serial: list[DeviceOption]
    serial_error: str | None
    audio_input: list[DeviceOption]
    audio_input_error: str | None


def _enumerate_serial() -> tuple[list[DeviceOption], str | None]:
    """List available serial ports via pyserial (blocking, run in a thread).

    Device enumeration is a between-roasts action. Calling this during an
    active MCP session is safe — ``comports()`` only reads the OS port list
    and does not open any port.

    The import of ``serial.tools.list_ports`` is deferred to this function so
    that a missing or unimportable pyserial wheel never crashes server startup
    — the error is returned as ``serial_error`` instead.

    Returns:
        A 2-tuple of ``(devices, error)``.  ``devices`` is empty on failure;
        ``error`` is ``None`` on success or the exception message on failure.
    """
    try:
        import serial.tools.list_ports as _lp  # noqa: PLC0415

        ports = _lp.comports()
        return (
            [
                DeviceOption(
                    value=p.device,
                    label=p.device,
                    note=p.description or p.hwid or "",
                )
                for p in sorted(ports, key=lambda p: p.device)
            ],
            None,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("Serial port enumeration failed: %s", exc)
        return [], str(exc)


def _enumerate_audio_inputs() -> tuple[list[DeviceOption], str | None]:
    """List audio input devices via sounddevice (blocking, run in a thread).

    Filters to devices with at least one input channel. The call only queries
    PortAudio device metadata — no audio stream is opened. Device enumeration
    is intended as a between-roasts action; calling it while the MCP child
    holds an open audio stream is safe (PortAudio supports concurrent device
    queries), but the operator should not reconfigure audio mid-capture.

    The import of ``sounddevice`` is deferred to this function so that a
    missing native PortAudio library (common in headless / CI environments)
    never crashes server startup — the ``ImportError`` is caught by the broad
    ``except`` and returned as ``audio_input_error`` instead.

    Returns:
        A 2-tuple of ``(devices, error)``.  ``devices`` is empty on failure;
        ``error`` is ``None`` on success or the exception message on failure.
    """
    try:
        import sounddevice as _sd  # type: ignore[import-untyped]  # noqa: PLC0415

        raw: object = _sd.query_devices()  # type: ignore[reportUnknownMemberType]
        # query_devices() returns a DeviceList (iterable of dicts) when called
        # with no arguments.  Explicitly cast to list[dict[str, object]] for
        # pyright; the runtime type is sounddevice.DeviceList which iterates
        # as dicts.
        all_devices: list[dict[str, object]] = list(raw)  # type: ignore[arg-type]
        options: list[DeviceOption] = []
        for idx, dev in enumerate(all_devices):
            max_in = dev.get("max_input_channels", 0)
            if not isinstance(max_in, int) or max_in <= 0:
                continue
            name = str(dev.get("name", f"Device {idx}"))
            rate = dev.get("default_samplerate", 0)
            rate_str = f"{int(rate):,}" if isinstance(rate, (int, float)) else "?"
            options.append(
                DeviceOption(
                    value=name,
                    label=name,
                    note=f"Input · {max_in} ch · {rate_str} Hz",
                )
            )
        return options, None
    except Exception as exc:  # noqa: BLE001
        _log.warning("Audio input enumeration failed: %s", exc)
        return [], str(exc)


DEVICES_PATH = "/api/config/devices"


async def get_devices(request: Request) -> DevicesSnapshot:
    """``GET /api/config/devices`` — read-only device enumeration (D78 PR(c), #418).

    Returns the available serial ports and audio input devices so the Config
    UI can populate its device-selection dropdowns.  Each source is wrapped in
    its own try/except so a PortAudio failure (common in headless CI) never
    prevents serial ports from being returned, and vice versa.

    This is a **read-only** endpoint — it opens no ports and starts no streams.
    It is intended for use **between roasts**.  Calling it during an active
    MCP capture session is safe (``comports()`` and ``query_devices()`` are
    non-destructive read-only queries), but device lists may be stale if the
    OS enumerates hardware asynchronously after the call returns.

    Both enumeration calls are blocking (native OS / PortAudio calls) and are
    dispatched to a thread via ``asyncio.to_thread`` to avoid blocking the
    async event loop.

    Args:
        request: Injected by FastAPI; unused but required for the route
            decorator signature.

    Returns:
        A :class:`DevicesSnapshot` with per-source device lists and error
        strings.  The response is always 200 — errors are surfaced in the
        ``*_error`` fields rather than as HTTP error codes.
    """
    del request  # unused
    # Both enumerations are blocking (native OS / PortAudio calls with no
    # ordering dependency) — run them in parallel via asyncio.gather so the
    # combined latency is max(serial, audio) rather than serial + audio.
    (serial_devices, serial_error), (audio_devices, audio_error) = await asyncio.gather(
        asyncio.to_thread(_enumerate_serial),
        asyncio.to_thread(_enumerate_audio_inputs),
    )
    return DevicesSnapshot(
        serial=serial_devices,
        serial_error=serial_error,
        audio_input=audio_devices,
        audio_input_error=audio_error,
    )


def _parse_last_event_id(raw: str | None) -> int | None:
    """Parse the SSE ``Last-Event-ID`` header into a sequence int (#339).

    Returns the int when ``raw`` is a clean integer, else ``None`` — a missing or
    malformed header (empty, non-numeric, whitespace) is treated as a fresh
    connection rather than raising on the SSE entry path. A negative value parses
    fine and simply replays the whole buffer, which is harmless (the client
    dedupes by id)."""
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


async def stream_events(
    run_id: str,
    request: Request,
    service: ServiceDep,
) -> StreamingResponse:
    """``GET /api/roasts/{run_id}/events`` — the typed SSE event stream (plan §6).

    Subscribes a fresh queue to the broadcaster and streams typed frames:
    every controller event, the per-tick ``telemetry``, and a ``heartbeat``
    every ``sse_heartbeat_seconds`` of idle. ``run_id`` names the run the
    client expects; E7 has one active run and a global broadcaster, so the
    stream carries that run's events. On disconnect the generator's ``finally``
    unsubscribes — a UI disconnect removes only that subscriber and never
    touches the controller (no cooling, no state change); backend safety runs
    on with no client attached.

    On reconnect the client's ``Last-Event-ID`` is parsed and passed to
    :meth:`EventBroadcaster.subscribe`, which replays the buffered gap before live
    frames resume (#339). It is read from the ``Last-Event-ID`` request header
    (set natively on EventSource auto-reconnect) and falls back to a
    ``last_event_id`` query param (the hook's explicit-backoff path, where a freshly
    constructed EventSource sends no header). A malformed value is ignored — the
    connection falls back to a fresh stream plus REST re-hydration.
    """
    try:
        await service.detail(run_id)  # 404 a stream for an unknown run
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Header first (native EventSource resume), query param as the explicit-reconnect
    # fallback. Either parses defensively to None on a malformed value. Use an
    # explicit None check (not `or`) so a legitimate id of 0 — replay the whole
    # buffer — is not collapsed to the query-param fallback.
    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))
    if last_event_id is None:
        last_event_id = _parse_last_event_id(request.query_params.get("last_event_id"))
    queue = service.events.subscribe(last_event_id)
    heartbeat = service.sse_heartbeat_seconds

    async def frames() -> AsyncIterator[bytes]:
        try:
            # An opening comment flushes headers so the client's onopen fires
            # immediately, before the first event or heartbeat.
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except TimeoutError:
                    yield SseEvent(event=SseEventType.HEARTBEAT).render().encode()
                    continue
                yield event.render().encode()
        finally:
            service.events.unsubscribe(queue)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        # Keep proxies and browsers from buffering or caching the live stream:
        # without no-cache an edge/browser cache may serve a stale body, and
        # X-Accel-Buffering: no disables Nginx response buffering.
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Startup/teardown for a wired service: on startup classify any
    possibly-active persisted run into recovery (never auto-resuming heat/fan,
    the restart invariant); on shutdown stop the live tick loop cleanly. A no-op
    for the API-only/scaffold app (no service, or no roaster wired)."""
    service = getattr(app.state, "service", None)
    if isinstance(service, RoastService):
        await service.seed_bean_profiles()
        await service.recover_on_start()
    yield
    if isinstance(service, RoastService):
        # Cancel the tick loop on lifespan teardown. The live-serve CLI path then
        # calls service.shutdown() again in _teardown_live (which is idempotent) —
        # that second call is a clean no-op, kept because _teardown_live also owns
        # the safety-critical heat-off-before-mcp.stop ordering (#142) that this
        # lifespan does not. Non-CLI uses of create_app rely on this call.
        await service.shutdown()


#: The app lifespan type: an async context manager over the FastAPI app.
Lifespan = Callable[[FastAPI], contextlib.AbstractAsyncContextManager[None]]


def create_app(
    service: RoastService | None = None,
    *,
    lifespan: Lifespan | None = None,
    spa_dir: Path | None = None,
) -> FastAPI:
    """Create the FastAPI application.

    ``service`` is the backend authority (store + active-run state + MCP
    handle); when omitted, only ``/api/health`` is functional and every
    store-backed route returns 503 — the shape the E1 scaffold smoke test
    relies on. The E9 vertical slice constructs a fully-wired service and
    passes it here; the lifespan then runs restart recovery on startup and
    stops the live loop on shutdown.

    ``lifespan`` overrides that default startup/teardown. The replay harness
    (E10-S1) passes a no-recovery lifespan: it drives the run itself and the
    run is already active, so the restart-recovery startup would wrongly force
    it into ``operator_recovery_required``.

    ``spa_dir`` opts the built SPA in. When set (and it contains an
    ``index.html``), the SPA is mounted at ``/`` *after* every ``/api/*`` route
    so it never shadows the API; unknown ``/api/*`` paths stay JSON 404s and any
    other path falls back to ``index.html`` for the SPA's client-side router
    (see :func:`roastpilot_agent.live.mount_spa`). When ``None``/missing,
    nothing is mounted — the scaffold/API-only shape is unchanged.
    """
    app = FastAPI(
        title="roastpilot-agent",
        version=__version__,
        lifespan=lifespan or _lifespan,
    )
    app.add_middleware(
        _RouteBodyLimitMiddleware,
        path=_DRAFT_BEAN_FROM_URL_PATH,
        max_body_bytes=_DRAFT_BEAN_FROM_URL_MAX_BODY_BYTES,
    )
    app.state.service = service
    app.add_exception_handler(RequestValidationError, _request_validation_error_handler)
    app.get(HEALTH_PATH)(health)
    app.get(CONFIG_PATH)(get_config)
    app.put(CONFIG_PATH)(put_config)
    app.get(DEVICES_PATH)(get_devices)
    app.post("/api/roasts", status_code=201)(start_roast)
    app.get("/api/roasts")(list_roasts)
    app.get("/api/roasts/{run_id}")(get_roast)
    app.get(TELEMETRY_PATH)(get_telemetry)
    app.get("/api/roasts/{run_id}/timeline")(get_timeline)
    app.get("/api/roasts/{run_id}/log")(get_log_manifest)
    app.get("/api/roasts/{run_id}/log/{artifact}")(download_log)
    app.post("/api/roasts/{run_id}/rating")(rate_roast)
    app.post("/api/roasts/{run_id}/roasted-weight")(set_roasted_weight)
    app.post("/api/roasts/{run_id}/charge-weight")(set_charge_weight)
    app.post("/api/roasts/{run_id}/discard")(discard_roast)
    app.post("/api/roasts/{run_id}/restore")(restore_roast)
    app.post("/api/roasts/{run_id}/clear-stale-session")(clear_stale_session)
    app.post("/api/roasts/{run_id}/tastings", status_code=201)(add_tasting)
    app.get("/api/roasts/{run_id}/tastings")(list_tastings)
    app.post("/api/roasts/{run_id}/operator-actions")(submit_operator_action)
    app.get("/api/bean-profiles")(list_bean_profiles)
    app.post("/api/bean-profiles", status_code=201)(create_bean_profile)
    app.put("/api/bean-profiles/{profile_id}")(update_bean_profile)
    app.delete("/api/bean-profiles/{profile_id}")(delete_bean_profile)
    app.post(_DRAFT_BEAN_FROM_URL_PATH)(draft_bean_from_url)
    app.get(EVENTS_PATH)(stream_events)
    if spa_dir is not None and (spa_dir / "index.html").is_file():
        # Imported lazily so the API-only/scaffold path carries no static-mount
        # cost and there is no import cycle (live.py imports api.create_app).
        from roastpilot_agent.live import mount_spa

        mount_spa(app, spa_dir)
    return app
