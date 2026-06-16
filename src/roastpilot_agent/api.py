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
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from roastpilot_agent import __version__
from roastpilot_agent.advisor import (
    AdvisorContext,
    AdvisorDescriptor,
    RoastAdvisor,
    RoastDecision,
)
from roastpilot_agent.config import AppConfig
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
    MCPServerProcess,
    RoastSessionState,
    project_mic_status,
)
from roastpilot_agent.models import (
    ACTIVE_ROAST_PHASES,
    AdvisorHealth,
    AdvisorTraceStatus,
    HealthResponse,
    LogManifest,
    MCPChildStatus,
    MicStatus,
    OperatorAction,
    OperatorActionRequest,
    OperatorActionResult,
    OperatorRatingRequest,
    RoastCommand,
    RoastDetail,
    RoastEventKind,
    RoastEventSource,
    RoastHistory,
    RoastPhase,
    RoastProfile,
    RoastTimeline,
    SseEvent,
    SseEventType,
    TelemetryEventData,
    TelemetrySeries,
)
from roastpilot_agent.safety import (
    OPERATOR_ACTION_COMMAND,
    SafetyEvaluation,
    SafetyPolicy,
    SafetyVerdict,
    enabled_operator_actions,
)
from roastpilot_agent.store import RoastStore

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
    """

    def __init__(self, *, max_queue: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[SseEvent]] = set()
        self._max_queue = max_queue
        self._sequence = 0

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

    def subscribe(self) -> asyncio.Queue[SseEvent]:
        """Register a new subscriber queue for one SSE connection."""
        queue: asyncio.Queue[SseEvent] = asyncio.Queue(maxsize=self._max_queue)
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


class RoastRunConflictError(Exception):
    """A request conflicts with the current run state (maps to HTTP 409):
    starting a roast while one is active, or rating an in-progress run."""


class RoastRunNotFoundError(Exception):
    """No run (or no requested artifact) matches the id (maps to HTTP 404)."""


class RoastRunGoneError(Exception):
    """An operator action targets a terminal (COMPLETE/FAULTED) run (HTTP 410).

    A completed or faulted run has no live controller loop draining its queue and
    no hot hardware to act on; the action is gone, not merely conflicting."""


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
        self._scheduler: TickScheduler | None = None

    async def start(self, profile: RoastProfile) -> None:
        """Begin the run: drive the controller's idle→preheating start, then
        flush its startup events and persist the resulting phase. Issues the
        profile's initial heat/fan through the controller's safety policy (never
        raw) — the controller owns that, not the runner."""
        await self._controller.start_run(profile)
        await self._flush_events()
        await self._persist_phase_if_changed()

    async def recover(
        self,
        profile: RoastProfile,
        persisted_phase: RoastPhase,
        *,
        t0_detected_at_utc: str | None = None,
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
        touches no transition, verdict, or hardware write."""
        self._controller.load_profile(profile)
        if t0_detected_at_utc is not None:
            self._controller.restore_charge_clock(t0_detected_at_utc)
            self._t0_persisted = True
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
        restart-never-auto-resumes invariant as :meth:`recover`."""
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
        behaviour, it never affects the live safety tick."""
        if self._t0_persisted or not snapshot.charge_detected:
            return
        try:
            await self._store.record_t0_detected_at(self._run_id)
        except Exception:  # pragma: no cover — fail-safe: a store error on this
            # advisory-only breadcrumb must never crash the safety tick; the only
            # cost is a resumed run's DTR degrading to the pre-#235 behaviour.
            return
        self._t0_persisted = True

    async def _publish_and_persist_telemetry(self) -> None:
        snapshot = self._controller.snapshot()
        await self._persist_t0_if_charged(snapshot)
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
                    development_elapsed_seconds=snapshot.development_elapsed_seconds,
                    development_percent=snapshot.development_percent,
                    t0_detected=telemetry.t0_detected,
                    first_crack_detected=telemetry.first_crack_detected,
                    mic_status=telemetry.mic_status,
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
            development_percent=None if raw is None else raw.development_percent,
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
        #: The most recent advisor reachability probe (issue #168), set at
        #: ``serve`` startup via :meth:`set_advisor_health` and surfaced on
        #: ``GET /api/health`` so the dashboard can render an ADVISOR-OFFLINE
        #: state. ``None`` until a probe runs (e.g. the E7 API-only path).
        self._advisor_health: AdvisorHealth | None = None
        self._exporter = exporter
        self._raw_state = raw_state
        self._run_loop = run_loop
        self._clock = clock
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

    async def health(self) -> HealthResponse:
        """Liveness + MCP child status + active run id + advisor health (plan §6).

        Reports the active run from persisted state without mutating the
        in-memory ``active_run_id`` pointer — a GET must not have a write
        side-effect, and once E9 wires the controller loop that pointer is the
        loop's to own, not a health poll's. ``advisor`` carries the startup
        reachability probe (issue #168) when one has run.
        """
        active = await self._store.active_run()
        return HealthResponse(
            version=__version__,
            mcp_child=self.mcp_child_status(),
            active_run_id=None if active is None else active.run_id,
            advisor=self._advisor_health,
        )

    async def start_roast(self, profile: RoastProfile) -> RoastDetail:
        """Start a roast: persist the run record, claim it as active (plan §6).

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
        return detail

    async def _begin_live_run(self, profile: RoastProfile, run_id: str) -> None:
        """Construct and start the live controller loop for a run (E9).

        Builds a fresh controller bound to this run id, wired to the roaster
        control surface, the store (via the snapshot sink + the runner's
        telemetry/event persistence), the advisor, and the SSE broadcaster. The
        controller's idle→preheating start runs before returning; the per-tick
        loop runs as a background task when ``run_loop`` is set (tests drive
        ``service.runner.tick_once()`` directly with ``run_loop=False``)."""
        runner = self._build_runner(run_id)
        if runner is None:  # pragma: no cover — guarded by the caller
            return
        await runner.start(profile)
        if self._run_loop:
            self._loop_task = asyncio.create_task(runner.run())

    def _build_runner(self, run_id: str) -> "RoastRunner | None":
        """Construct a controller + runner bound to ``run_id`` (shared by the
        fresh-start and restart-recovery paths). ``None`` in API-only mode."""
        roaster = self._roaster
        if roaster is None:  # pragma: no cover — guarded by the caller
            return None
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

        * **A persisted ``faulted`` run (#206)** re-enters the *operable-faulted*
          state via :meth:`RoastRunner.recover_faulted` — NOT
          ``operator_recovery_required``. A hard fault (or e-stop) must never
          offer resume-into-roasting (which the recovery transition row permits),
          because that would re-apply heat into an aborted run (operator decision,
          14 Jun). The loop stays alive, heat is off, and the operator may still
          engage/stop cooling or e-stop, then acknowledge the fault to finalise it.
        * **An active-roast phase** (preheating / pre-FC / development / cooling)
          enters ``operator_recovery_required`` via :meth:`RoastRunner.recover`,
          where explicit operator action (resume/drop/cool/end) is required and
          emergency stop stays available.
        """
        if self._roaster is None:
            return
        persisted = await self._store.read_latest_run()
        if persisted is None or persisted.completed_at_utc is not None:
            return  # fresh database, or a terminal run — nothing possibly active
        if persisted.agent_phase in (RoastPhase.IDLE, RoastPhase.COMPLETE):
            return
        runner = self._build_runner(persisted.run_id)
        if runner is None:  # pragma: no cover — guarded above
            return
        self.active_run_id = persisted.run_id
        if persisted.agent_phase is RoastPhase.FAULTED:
            # Fail-closed: a persisted hard fault with no completed_at (the now
            # common case — a fault no longer auto-finalises, #206) re-enters the
            # operable-faulted state, never resume-into-roast recovery.
            await runner.recover_faulted(persisted.profile)
        else:
            await runner.recover(
                persisted.profile,
                persisted.agent_phase,
                t0_detected_at_utc=persisted.t0_detected_at_utc,
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
        A timeout or any error is logged loudly (the operator must know the
        commanded stop did not confirm and may need the power switch) and
        swallowed so the rest of teardown — including ``mcp.stop`` — still runs.

        Args:
            timeout_seconds: Upper bound on the heat-off write before shutdown
                proceeds regardless (default 5 s — generous for one MCP call,
                short enough never to wedge a Ctrl-C).

        Returns:
            ``True`` if the heat-off safety path ran and the controller faulted
            (the e-stop was dispatched through ``operator_emergency_stop``).
            Note this is *not* a hardware acknowledgement: if the MCP
            ``emergency_stop`` write itself fails, ``operator_emergency_stop``
            still emits ``COMMAND_FAILED``, faults the run, and reports success
            here — fail-safe is the controller's job, not the caller's.
            ``False`` if it was a no-op (no active run / already hardware-off) or
            the safety path did not run to completion (the ``wait_for`` timeout
            or an unexpected error in this method, both logged + swallowed).
        """
        runner = self.runner
        if runner is None:
            return False
        try:
            return await asyncio.wait_for(runner.shutdown_heat_off(), timeout=timeout_seconds)
        except TimeoutError:
            _log.error(
                "SHUTDOWN heat-off did not confirm within %.1fs — the roaster may still "
                "be commanded hot; use the Hottop power switch if needed",
                timeout_seconds,
            )
            return False
        except Exception:  # noqa: BLE001 — fail closed: log loudly, never block teardown
            _log.error(
                "SHUTDOWN heat-off failed — the roaster may still be commanded hot; "
                "use the Hottop power switch if needed",
                exc_info=True,
            )
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
    the probe never depends on a store being wired.
    """
    service = getattr(request.app.state, "service", None)
    if isinstance(service, RoastService):
        return await service.health()
    return HealthResponse(
        version=__version__,
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
    """
    try:
        await service.detail(run_id)  # 404 a stream for an unknown run
    except RoastRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    queue = service.events.subscribe()
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
    app.state.service = service
    app.get(HEALTH_PATH)(health)
    app.post("/api/roasts", status_code=201)(start_roast)
    app.get("/api/roasts")(list_roasts)
    app.get("/api/roasts/{run_id}")(get_roast)
    app.get(TELEMETRY_PATH)(get_telemetry)
    app.get("/api/roasts/{run_id}/timeline")(get_timeline)
    app.get("/api/roasts/{run_id}/log")(get_log_manifest)
    app.get("/api/roasts/{run_id}/log/{artifact}")(download_log)
    app.post("/api/roasts/{run_id}/rating")(rate_roast)
    app.post("/api/roasts/{run_id}/operator-actions")(submit_operator_action)
    app.get(EVENTS_PATH)(stream_events)
    if spa_dir is not None and (spa_dir / "index.html").is_file():
        # Imported lazily so the API-only/scaffold path carries no static-mount
        # cost and there is no import cycle (live.py imports api.create_app).
        from roastpilot_agent.live import mount_spa

        mount_spa(app, spa_dir)
    return app
